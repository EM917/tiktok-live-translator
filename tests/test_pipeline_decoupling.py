"""ASR 与翻译解耦的核心契约。

业务前提：漏词不可接受，译文晚到可以接受。所以：
  * 翻译再慢也不能反压到识别链路（否则音频队列溢出 = 丢音频 = 漏词）；
  * 字幕先出西语原文，违禁词检测立刻在原文上跑，译文回来再原地补；
  * 翻译积压时丢的是**旧的翻译任务**，不是音频。
"""
import asyncio
from types import SimpleNamespace

from app import pipeline as pipeline_mod
from app.asr import ASRResult
from app.pipeline import Pipeline


class StubServer:
    def __init__(self):
        self.config = {}
        self.messages = []

    async def status(self, state, detail=""):
        self.messages.append({"type": "status", "state": state, "detail": detail})

    async def broadcast(self, msg):
        self.messages.append(msg)

    def of_type(self, t):
        return [m for m in self.messages if m.get("type") == t]


def make_pipeline(monkeypatch, tmp_path, translator=None, terms=()):
    from app import settings
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    terms_file = tmp_path / "banned_terms.txt"
    terms_file.write_text("\n".join(terms), encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms_file)

    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source="es",
        beam=5, context=False, asr_temperature=None, glossary=None, backend="auto", model=None,
        device="auto", compute_type="auto", denoise="off", banned_terms=None,
    )
    server = StubServer()
    p = Pipeline(args, server)
    p.translator = translator
    return p, server


def run(coro):
    return asyncio.run(coro)


class SlowTranslator:
    """模拟卡住的翻译引擎（Ollama 的超时预算是 60 秒）。"""

    def __init__(self, delay=3600):
        self.delay = delay
        self.calls = 0

    async def translate(self, text, target, source="auto"):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return "永远不会返回"


class FastTranslator:
    def __init__(self):
        self.seen = []

    async def translate(self, text, target, source="auto"):
        self.seen.append(text)
        return "[zh] " + text


def result(text, raw=None):
    return ASRResult(text=text, language="es", raw_text=raw or text, rejected=[])


def test_original_caption_emitted_before_translation(monkeypatch, tmp_path):
    tr = FastTranslator()
    p, server = make_pipeline(monkeypatch, tmp_path, translator=tr)

    async def scenario():
        job = await p._emit_original(result("hola amigos"), audio_end_ts=100.0, asr_ms=200)
        assert job is not None                     # 需要翻译 → 返回待翻译任务
        await p._translate_and_update(job)

    run(scenario())
    caption = server.of_type("caption")[0]
    assert caption["original"] == "hola amigos"
    assert caption["translated"] is None           # 原文先出，不等翻译
    assert caption["translate_state"] == "pending"
    update = server.of_type("caption_update")[0]
    assert update["id"] == caption["id"]           # 按 id 原地补译文
    assert update["translated"] == "[zh] hola amigos"


def test_slow_translation_never_blocks_asr(monkeypatch, tmp_path):
    """翻译卡住 1 小时，识别照常出字幕——这是「不能漏词」的地基。"""
    slow = SlowTranslator()
    p, server = make_pipeline(monkeypatch, tmp_path, translator=slow)

    async def scenario():
        jobs = []
        for i in range(5):
            job = await p._emit_original(result("frase %d" % i),
                                         audio_end_ts=100.0 + i, asr_ms=100)
            jobs.append(job)
        # 起一个永远卡住的翻译，识别链路不受任何影响
        stuck = asyncio.ensure_future(p._translate_and_update(jobs[0]))
        await asyncio.sleep(0)
        for i in range(5, 10):
            await p._emit_original(result("frase %d" % i),
                                   audio_end_ts=110.0 + i, asr_ms=100)
        stuck.cancel()

    run(scenario())
    assert len(server.of_type("caption")) == 10    # 10 段全部出了字幕
    assert server.of_type("caption_update") == []  # 一条译文都没回来，但不影响


def test_banned_words_detected_without_translation(monkeypatch, tmp_path):
    """违禁词检测跑在西语原文上，翻译引擎关掉也照样报警。"""
    p, server = make_pipeline(monkeypatch, tmp_path, translator=None,
                              terms=["cura el cancer"])
    assert p.detector.enabled

    async def scenario():
        await p._emit_original(result("este producto cura el cancer"),
                               audio_end_ts=100.0, asr_ms=150)

    run(scenario())
    alerts = server.of_type("alert")
    assert len(alerts) == 1
    assert alerts[0]["term"] == "cura el cancer"
    assert alerts[0]["tier"] == "exact"


def test_detector_scans_text_dropped_by_quality_filter(monkeypatch, tmp_path):
    """被质量过滤丢掉的低置信文本仍要参与违禁词检测——漏报代价更高。"""
    p, server = make_pipeline(monkeypatch, tmp_path, translator=None,
                              terms=["cura el cancer"])

    async def scenario():
        # text 为空（整段被过滤），但 raw_text 里有违禁词
        await p._emit_original(
            ASRResult(text="", language="es",
                      raw_text="este producto cura el cancer",
                      rejected=[{"text": "...", "reason": "low_confidence"}]),
            audio_end_ts=100.0, asr_ms=150)

    run(scenario())
    assert len(server.of_type("alert")) == 1       # 仍然报警
    assert server.of_type("caption") == []         # 但不产生字幕


def test_telemetry_records_latencies(monkeypatch, tmp_path):
    tr = FastTranslator()
    p, server = make_pipeline(monkeypatch, tmp_path, translator=tr)

    async def scenario():
        job = await p._emit_original(result("hola"), audio_end_ts=100.0, asr_ms=250)
        await p._translate_and_update(job)

    run(scenario())
    snap = p.telemetry.snapshot()
    assert snap["audio_segments_total"] == 1
    assert snap["asr"]["p50"] == 250
    assert snap["e2e"]["n"] == 1
    assert snap["translate"]["n"] == 1


def test_no_translator_skips_translation_job(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path, translator=None)

    async def scenario():
        return await p._emit_original(result("hola"), audio_end_ts=100.0, asr_ms=100)

    assert run(scenario()) is None                 # 无需翻译 → 不入翻译队列
    assert server.of_type("caption")[0]["translate_state"] == "skipped"


def test_word_list_reloaded_every_session(monkeypatch, tmp_path):
    """词表必须每场重读：模板生成出来是空的，用户填好后点「停止→开始」
    如果不生效，头号卖点就是 100% 静默漏报。"""
    p, server = make_pipeline(monkeypatch, tmp_path, translator=None, terms=[])
    assert not p.detector.enabled                      # 首场：空表

    # 用户编辑词表后重新开始
    (tmp_path / "banned_terms.txt").write_text("cura el cancer\n", encoding="utf-8")

    async def scenario():
        await p._begin_session("https://www.tiktok.com/@x/live")
        await p._emit_original(result("este producto cura el cancer"),
                               audio_end_ts=100.0, asr_ms=150)
        await p._end_session()

    run(scenario())
    assert p.detector.enabled
    assert len(server.of_type("alert")) == 1           # 新词表真的生效了


def test_empty_word_list_warns_the_operator(monkeypatch, tmp_path):
    """空词表 = 这场不会有任何报警，必须让运维在界面上看到。"""
    p, server = make_pipeline(monkeypatch, tmp_path, translator=None, terms=[])

    async def scenario():
        await p._begin_session("https://www.tiktok.com/@x/live")
        await p._end_session()

    run(scenario())
    notices = [m for m in server.of_type("notice") if "词表为空" in m.get("text", "")]
    assert notices


def test_detector_state_reset_between_rooms(monkeypatch, tmp_path):
    """换直播间要清冷却：否则 A 房间的 30 秒冷却会吞掉 B 房间的同词报警。"""
    p, server = make_pipeline(monkeypatch, tmp_path, translator=None,
                              terms=["cura el cancer"])
    p.detector.cooldown_sec = 30

    async def scenario():
        await p._begin_session("https://www.tiktok.com/@a/live")
        await p._emit_original(result("esto cura el cancer"),
                               audio_end_ts=100.0, asr_ms=100)
        await p._end_session()
        await p._begin_session("https://www.tiktok.com/@b/live")   # 换房间
        await p._emit_original(result("esto cura el cancer"),
                               audio_end_ts=110.0, asr_ms=100)     # 仅隔 10 秒
        await p._end_session()

    run(scenario())
    assert len(server.of_type("alert")) == 2           # 两场各报一次


def test_telemetry_reset_between_sessions(monkeypatch, tmp_path):
    """统计按场计：上一场的「丢音频 7」不能挂在这一场的面板上。"""
    p, server = make_pipeline(monkeypatch, tmp_path, translator=None)

    async def scenario():
        await p._begin_session("https://www.tiktok.com/@a/live")
        await p._emit_original(result("hola"), audio_end_ts=100.0, asr_ms=100)
        p.telemetry.drop_audio()
        assert p.telemetry.snapshot()["audio_segments_dropped"] == 1
        await p._end_session()
        await p._begin_session("https://www.tiktok.com/@b/live")
        snap = p.telemetry.snapshot()
        assert snap["audio_segments_dropped"] == 0
        assert snap["audio_segments_total"] == 0
        await p._end_session()

    run(scenario())


def test_stats_task_stops_when_stream_ends(monkeypatch, tmp_path):
    """直播自然结束后统计循环必须停——否则界面继续刷新冻结的数字。"""
    p, server = make_pipeline(monkeypatch, tmp_path, translator=None)

    async def scenario():
        await p._begin_session("https://www.tiktok.com/@x/live")
        assert p._stats_task is not None and not p._stats_task.done()
        await p._end_session()
        assert p._stats_task is None
        assert p.audit is None

    run(scenario())


def test_asr_overrun_counted_and_audited(monkeypatch, tmp_path):
    """识别耗时超过片段时长 = 复读跑飞，是丢音频的前兆，必须计数并留痕。"""
    from app.telemetry import Telemetry
    t = Telemetry()
    assert t.snapshot()["asr_overruns"] == 0
    t.note_overrun()
    assert t.snapshot()["asr_overruns"] == 1
    t.reset()
    assert t.snapshot()["asr_overruns"] == 0        # 按场重置


def test_audit_records_overrun(tmp_path):
    import json
    from app.audit import AuditLog
    log = AuditLog(room_url="x", log_dir=tmp_path)
    log.asr_overrun(asr_ms=37700, segment_ms=4500)
    log.close()
    lines = [json.loads(ln) for ln in log.path.read_text(encoding="utf-8").splitlines()]
    overruns = [ln for ln in lines if ln["type"] == "asr_overrun"]
    assert overruns and overruns[0]["asr_ms"] == 37700
