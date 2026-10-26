"""识别器的 key 不含热词（专家审查 M7，2026-10-21）。

以前 _run_session 把 glossary.asr_prompt()（主播 / 品牌词表的热词提示）放进识别器的 key：换一位主播热词一变，
key 就变，于是新建一个识别器。ct2 后端（Windows、Intel、CPU 用户）加载新模型时旧的还在内存里，两份同时驻留
（规则三那类事故）；mlx 也要白白重新预热一次。改成：

  * key 只有识别配置（后端、模型、设备、精度、语言、beam、上下文、温度），换主播不重新加载；
  * 每场拿到识别器之后直接赋 hotwords，两个后端都在 transcribe 时现读这个属性，下一段的 initial_prompt
    就是这一场的热词；
  * 识别连续出错改用的 CPU 备用识别器也带这一场的热词；上一场改用的 CPU 模型被下一场（别的主播）接着用时，
    热词换成下一场的；
  * asr_config 审计照常记热词的指纹（prompt_sha16），记的是这一场的。

全部用桩件：假的 faster_whisper 模块，不加载模型、不联网、不开浏览器。主播名都是虚构的。
"""
import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor
from types import ModuleType, SimpleNamespace

import pytest

from app import asr, asr_risk, glossary
from app import pipeline as pipeline_mod
from app.audit import AuditLog
from app.pipeline import _ASRSlot
from app.resolver import ResolveError
from tests.helpers import run
from tests.test_resilience_asr import Broken, Working, mlx_config, rows
from tests.test_resilience_stream import make_pipeline as stream_pipeline
from tests.test_resilience_stream import scripted_resolve

# 一轮有声之后 V2 先用原来的流地址重连（不再解析），桩件就一直连下去；V1 每次都重新解析，第二次解析
# 「已结束」这一场就收尾。这里测的是识别器怎么拿、热词怎么给，和重连规则无关
pytestmark = pytest.mark.usefixtures("v1_reconnect")

UNO = "https://www.tiktok.com/@ficticia_uno/live"
DOS = "https://www.tiktok.com/@ficticia_dos/live"
PCM = b"\x00\x00" * 1600


@pytest.fixture
def ct2(monkeypatch):
    """假的 faster_whisper：WhisperModel 每构造一次记一笔，每次 transcribe 记下拿到的 initial_prompt。
    识别器本身是真的 app.asr.Transcriber（ct2 后端），只有底下的模型是替身。"""
    log = {"built": [], "prompts": []}

    class WhisperModel:
        def __init__(self, size, device="cpu", compute_type="int8"):
            log["built"].append((size, device, compute_type))

        def transcribe(self, audio, **kw):
            log["prompts"].append(kw.get("initial_prompt"))
            return [], SimpleNamespace(language="es")

    module = ModuleType("faster_whisper")
    module.WhisperModel = WhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    import app.hwdetect
    monkeypatch.setattr(app.hwdetect, "recommend", lambda backend=None, device=None: {
        "backend": "ct2", "model": "small", "device": "cpu", "compute_type": "int8", "note": ""})
    return log


def make_pipeline(monkeypatch, tmp_path):
    """tests/test_resilience_stream.py 的整场桩件（退避不真等），识别器换回真的 create_transcriber：底下的
    faster_whisper 已经是替身（ct2 夹具）。"""
    real = asr.create_transcriber
    p, server = stream_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(asr, "create_transcriber", real)
    return p, server


def profiles():
    """两位虚构主播的词表，热词提示各不相同；全局词表是空的（第三位主播就没有热词）。
    返回 (第一位的提示, 第二位的提示)。"""
    glossary.GLOSSARY_FILE.parent.mkdir(parents=True, exist_ok=True)
    glossary.GLOSSARY_FILE.write_text("", encoding="utf-8")
    glossary.PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    (glossary.PROFILE_DIR / "ficticia_uno.txt").write_text("el serum dorado => 金色精华\n",
                                                           encoding="utf-8")
    (glossary.PROFILE_DIR / "ficticia_dos.txt").write_text("la vela de coco => 椰子蜡烛\n",
                                                           encoding="utf-8")
    one = glossary.load(streamer="ficticia_uno").asr_prompt()
    two = glossary.load(streamer="ficticia_dos").asr_prompt()
    assert one and two and one != two
    return one, two


def one_session(monkeypatch, p, url, seen, audits=None):
    """走 _run_stream_inner 的一整场：解析一次 → 一轮拉流（桩：用这一场的识别器识别一段）→ 再解析时「已结束」。
    audits 里按场次顺序放进这一场的审计文件（文件名里的主播名会打乱按名字排的顺序）。"""
    scripted_resolve(monkeypatch, ["http://cdn/s.flv", ResolveError("没开播", kind="offline", status=4)])

    async def session(media, slot, *a, **k):
        seen.append(slot.transcriber)
        if audits is not None:
            audits.append(slot.audit.path)
        slot.transcriber.transcribe(PCM)
        return True, 60.0

    monkeypatch.setattr(p, "_stream_session", session)

    run(p._run_stream_inner(url))


def asr_configs(audits):
    out = []
    for f in audits:
        out.extend(rows(f, "asr_config"))
    return out


def test_a_new_streamer_with_other_hotwords_reuses_the_loaded_model(monkeypatch, tmp_path, ct2):
    """换主播（热词不同）：ct2 模型只构造一次，第二场用的是同一个识别器；第二场那一段的 initial_prompt
    是第二位主播的热词，asr_config 记的指纹也换成第二场的。"""
    one, two = profiles()
    p, server = make_pipeline(monkeypatch, tmp_path)
    seen, audits = [], []
    one_session(monkeypatch, p, UNO, seen, audits)
    one_session(monkeypatch, p, DOS, seen, audits)
    assert len(ct2["built"]) == 1                              # 以前这里是 2：换主播就新建
    assert len(seen) == 2 and seen[0] is seen[1]
    assert ct2["prompts"] == [one, two]
    assert seen[1].hotwords == two
    got = [r["prompt_sha16"] for r in asr_configs(audits)]
    assert got == [asr_risk.prompt_sha16(one), asr_risk.prompt_sha16(two)]


def test_the_same_streamer_again_keeps_the_model_and_a_real_config_change_still_reloads(
        monkeypatch, tmp_path, ct2):
    """key 只少了热词：识别配置变了（这里是主播语言）照旧重新加载，同一位主播再开一场照旧复用。"""
    profiles()
    p, server = make_pipeline(monkeypatch, tmp_path)
    seen = []
    one_session(monkeypatch, p, UNO, seen)
    one_session(monkeypatch, p, UNO, seen)
    assert len(ct2["built"]) == 1
    p.args.source = "en"
    one_session(monkeypatch, p, UNO, seen)
    assert len(ct2["built"]) == 2 and seen[2] is not seen[1]


def test_a_streamer_without_a_glossary_clears_the_previous_hotwords(monkeypatch, tmp_path, ct2):
    """下一位主播没有词表（没有热词提示）：上一位的热词不能留在识别器上。"""
    one, _two = profiles()
    p, server = make_pipeline(monkeypatch, tmp_path)
    seen = []
    one_session(monkeypatch, p, UNO, seen)
    one_session(monkeypatch, p, "https://www.tiktok.com/@ficticia_tres/live", seen)
    assert len(ct2["built"]) == 1
    assert ct2["prompts"] == [one, None] and seen[1].hotwords is None


def test_the_mlx_backend_reads_the_hotwords_at_transcribe_time(monkeypatch):
    """mlx 后端同样在 transcribe 时现读 self.hotwords（不在构造时烘焙进别处）：改了属性，下一段就用新的。"""
    calls = []

    class FakeMLX:
        @staticmethod
        def transcribe(audio, **kw):
            calls.append(kw.get("initial_prompt"))
            return {"language": "es", "text": "", "segments": []}

    t = asr.MLXTranscriber.__new__(asr.MLXTranscriber)
    t._mlx = FakeMLX()
    t.repo = "mlx-community/x"
    t.language, t.allowed_langs, t.primary_lang = "es", None, "es"
    t.token_cap, t.mask_langs = None, False
    t.use_context, t.temperature, t._context = False, 0.0, ""
    t.hotwords = "Productos: uno."
    t.transcribe(PCM)
    t.hotwords = "Productos: dos."
    t.transcribe(PCM)
    pipeline_mod._set_hotwords(t, None)
    t.transcribe(PCM)
    assert calls == ["Productos: uno.", "Productos: dos.", None]


def test_the_cpu_fallback_after_repeated_errors_carries_this_sessions_hotwords(monkeypatch, tmp_path):
    """识别连续出错改用 CPU：新识别器带的是这一场的热词（来自这一场的识别配置）。"""
    from app import selfcheck

    p, server = make_pipeline(monkeypatch, tmp_path)
    p.audit = AuditLog(room_url=DOS, log_dir=tmp_path / "logs")
    monkeypatch.setattr(selfcheck, "_model_cached", lambda model, backend: True)
    created = []

    def create(**kw):
        created.append(kw)
        return Working(**kw)

    monkeypatch.setattr(asr, "create_transcriber", create)
    broken = Broken([])
    broken.hotwords = "Productos: dos."
    slot = _ASRSlot(broken, config=mlx_config(hotwords="Productos: dos."), key=("k",), audit=p.audit)

    async def scenario():
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=1) as pool:
            return await p._on_asr_failures(slot, "boom", 3, 0.0, pool, loop)

    assert run(scenario()) is True
    assert len(created) == 1 and created[0]["hotwords"] == "Productos: dos."
    assert created[0]["backend"] == "ct2" and created[0]["device"] == "cpu"
    assert slot.transcriber is not broken


def test_the_previous_sessions_cpu_fallback_is_kept_with_the_new_streamers_hotwords(
        monkeypatch, tmp_path, ct2):
    """上一场（第一位主播）识别连续出错、改用了 CPU 模型：下一场换了主播，key 不变，接着用这个 CPU 模型（和
    同一位主播再开一场一样，不去重新加载那个出过错的配置；要再试原来的配置得重开程序，提示里写着），不另载
    一份。热词换成第二位主播的，asr_config 带着 fallback 的来龙去脉和第二场的指纹。"""
    one, two = profiles()
    p, server = make_pipeline(monkeypatch, tmp_path)
    seen, audits = [], []
    one_session(monkeypatch, p, UNO, seen, audits)
    first = seen[0]
    # 上一场改用的 CPU 模型：带着上一场的热词，挂在同一个 key 上
    fallback = Working(backend="ct2", model_size="large-v3-turbo", device="cpu", compute_type="int8")
    fallback.hotwords = one
    fallback.transcribe = lambda pcm: SimpleNamespace(text="", raw_text="", language="es", rejected=[])
    p._transcriber = fallback
    p._asr_fallback = {"from": "ct2/small/cpu/int8", "to": "ct2/large-v3-turbo/cpu/int8", "error": "x"}
    one_session(monkeypatch, p, DOS, seen, audits)
    assert len(ct2["built"]) == 1                        # 没有为第二位主播再加载一次
    assert seen[1] is fallback and seen[1] is not first
    assert fallback.hotwords == two
    last = asr_configs(audits)[-1]
    assert last["prompt_sha16"] == asr_risk.prompt_sha16(two)
    assert last["fallback"]["to"] == "ct2/large-v3-turbo/cpu/int8"


def test_the_key_has_no_hotwords_in_it(monkeypatch, tmp_path, ct2):
    """直接看 key：两位主播的 key 相同，里面没有热词提示的字样。"""
    one, two = profiles()
    p, server = make_pipeline(monkeypatch, tmp_path)
    keys = []
    for url in (UNO, DOS):
        one_session(monkeypatch, p, url, [])
        keys.append(p._transcriber_key)
    assert keys[0] == keys[1]
    flat = repr(keys[0])
    assert one not in flat and two not in flat and "Productos" not in flat
