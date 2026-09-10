"""第二轮全项目扫描（2026-09-09）钉下的回归测试。每个用例对应一条已核实的发现，
用例名说明原来会发生什么。"""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import comment_source, diskspace, glossary, hwdetect, localmodel, selfcheck, translator
from app.ffmpeg_bin import filter_path, find_ffmpeg
from app.pipeline import Pipeline
from app.relaunch import exec_args
from app.server import CaptionServer

ROOT = Path(__file__).resolve().parent.parent
_REAL_SLEEP = asyncio.sleep       # make_pipeline 会把 asyncio.sleep 全局换成 0 秒


def run(coro):
    return asyncio.run(coro)


class StubServer:
    def __init__(self):
        self.config = {}
        self.statuses = []
        self.broadcasts = []

    async def status(self, state, detail="", command=None):
        self.statuses.append((state, detail))

    async def broadcast(self, msg):
        self.broadcasts.append(msg)


def make_pipeline(monkeypatch, tmp_path):
    from app import pipeline as pipeline_mod
    from app import settings
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source=None,
        beam=5, context=False, asr_temperature=None, glossary=None, backend="auto",
        model=None, device="auto", compute_type="auto", denoise="off",
    )
    server = StubServer()
    p = Pipeline(args, server)
    import app.asr
    monkeypatch.setattr(app.asr, "create_transcriber", lambda **kw: object())
    real_sleep = asyncio.sleep
    monkeypatch.setattr(pipeline_mod.asyncio, "sleep", lambda *_a, **_k: real_sleep(0))
    return p, server


# ---- #1 流结束时翻译队列是满的 ----------------------------------------------

def test_stream_end_with_a_full_translation_queue_returns_instead_of_raising(monkeypatch, tmp_path):
    """翻译卡住（Ollama 换页/DeepL 超时）时队列正好满 4 条，这时流一断，裸的
    put_nowait(None) 抛 QueueFull，整场从「自动重连」变成「内部错误，已停止」。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    p.TRANSLATION_DRAIN_SEC = 0.05
    import app.audio
    import app.segmenter
    from app.audio import FRAME_BYTES

    class FakeSource:
        def __init__(self, media, denoise_model=None):
            pass

        async def frames(self):
            for _ in range(80):
                yield b"\x00" * FRAME_BYTES

        def stderr_tail(self):
            return ""

        async def stop(self):
            pass

    class ChattySegmenter:            # 每 8 帧切一段，10 段 > 队列上限 4
        def __init__(self, *a, **k):
            self.n = 0

        def feed(self, frame):
            self.n += 1
            return [frame * 8] if self.n % 8 == 0 else []

        def flush(self):
            return []

    monkeypatch.setattr(app.audio, "FFmpegAudioSource", FakeSource)
    monkeypatch.setattr(app.segmenter, "SilenceSegmenter", ChattySegmenter)

    class Transcriber:
        def transcribe(self, pcm):
            return SimpleNamespace(text="hola que tal", raw_text="hola que tal", language="es")

    async def never_returns(job):
        await asyncio.Event().wait()

    p.translator = object()                 # 有翻译引擎才会产生翻译任务
    monkeypatch.setattr(p, "_translate_and_update", never_returns)

    async def scenario():
        loop = asyncio.get_running_loop()
        return await p._stream_session("http://cdn/s.flv", Transcriber(), None, "note", loop)

    got_audio, secs = run(scenario())
    assert got_audio is True and secs > 0
    captions = [m for m in server.broadcasts if m.get("type") == "caption"]
    assert len(captions) >= 5                # 确实积压过：任务数超过队列容量
    dropped = [m for m in server.broadcasts
               if m.get("type") == "caption_update" and m.get("translate_state") == "dropped"]
    assert dropped                           # 被挤掉的任务告诉了界面，不会永远「翻译中…」


# ---- #2 后台备模型的进度不能把直播界面拽回待机 ---------------------------------

def test_provisioning_progress_is_a_notice_while_a_stream_is_live(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def scenario():
        p._stream_task = asyncio.ensure_future(asyncio.Event().wait())
        await p._provision_note("正在下载本地翻译模型：50%")
        p._stream_task.cancel()
        p._stream_task = None
        await p._provision_note("本地翻译已就绪")

    run(scenario())
    assert server.statuses == [("idle", "本地翻译已就绪")]
    assert server.broadcasts == [{"type": "notice", "text": "正在下载本地翻译模型：50%"}]


# ---- #3 停止超时放手后，弹幕子进程也要停 ---------------------------------------

def test_stop_still_stops_the_comment_source_when_the_task_will_not_die(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    p.STOP_GRACE_SEC = 0.05

    class FakeCS:
        stops = 0

        async def stop(self):
            self.stops += 1

    p.comment_source = FakeCS()

    async def wedged():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await _REAL_SLEEP(0.3)           # 识别线程卡着，取消后还要磨一阵
            raise

    async def scenario():
        task = asyncio.ensure_future(wedged())
        p._stream_task = task
        await asyncio.sleep(0)
        await p._stop_locked()
        assert p.comment_source.stops == 1
        assert not task.done()               # 确认走的是「超时放手」这条路
        try:
            await task
        except asyncio.CancelledError:
            pass

    run(scenario())


# ---- #4/#9/#27 历史只补消息里带的键 ---------------------------------------------

def test_state_only_updates_patch_history_instead_of_replacing_it():
    server = CaptionServer(port=8765)
    run(server.broadcast({"type": "caption", "id": 7, "original": "hola",
                          "translated": None, "translate_state": "pending"}))
    run(server.broadcast({"type": "caption_update", "id": 7,
                          "translated": "快译", "translate_state": "ok"}))
    run(server.broadcast({"type": "caption_update", "id": 7, "strong_state": "pending"}))
    stored = list(server.history)[-1]
    assert stored["translated"] == "快译"            # 以前这里被抹成 None
    assert stored["translate_state"] == "ok"
    assert stored["strong_state"] == "pending"
    run(server.broadcast({"type": "alert", "alert_id": 3, "context_zh": ""}))
    run(server.broadcast({"type": "alert_update", "alert_id": 3, "context_zh": "",
                          "failed": True, "why": "超时"}))
    stored = list(server.alerts)[-1]
    assert stored["failed"] is True and stored["why"] == "超时"   # 刷新后不再退回「翻译中…」


# ---- #5 词表文件不是 UTF-8 / 带 BOM ------------------------------------------------

def test_gbk_glossary_does_not_kill_the_session(monkeypatch, tmp_path, capsys):
    f = tmp_path / "glossary.txt"
    f.write_bytes("gotitas => 滴剂\n".encode("gbk"))
    g = glossary.load(str(f))
    assert g.entries == []
    assert "不是 UTF-8" in capsys.readouterr().out


def test_bom_glossary_keeps_its_first_entry(tmp_path):
    f = tmp_path / "glossary.txt"
    f.write_text("gotitas => 滴剂\nel brillo => 亮发精华\n", encoding="utf-8-sig")
    g = glossary.load(str(f))
    assert [zh for _v, zh in g.entries] == ["滴剂", "亮发精华"]


def test_bom_profile_option_line_is_honoured(monkeypatch, tmp_path):
    monkeypatch.setattr(glossary, "PROFILE_DIR", tmp_path)
    (tmp_path / "bella.txt").write_text("vocative_strip: on\n", encoding="utf-8-sig")
    assert glossary.profile_options("bella") == {"vocative_strip": True}


# ---- #6/#17 强模型探测：进线程池、本场只探一次 ------------------------------------

def test_missing_strong_model_is_probed_once_per_session(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(translator, "create_strong_translator", lambda: calls.append(1))

    async def scenario():
        assert await p._strong_translator() is None
        assert await p._strong_translator() is None
    run(scenario())
    assert calls == [1]


# ---- #7 子进程 stdout 固定 UTF-8 ------------------------------------------------------

def test_comment_worker_output_is_utf8_even_on_a_cp1252_console():
    code = ("from app.comment_worker import _utf8_stdio, _emit; _utf8_stdio(); "
            "_emit({'text': 'ni\\u00f1o \\U0001F389'})")
    env = dict(os.environ, PYTHONIOENCODING="cp1252", PYTHONUTF8="0")
    r = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env,
                       capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.decode("utf-8"))["text"] == "niño 🎉"


# ---- #8 stderr 排水遇到超长行不退出 ------------------------------------------------

class _Stream:
    def __init__(self, items):
        self.items = list(items)

    async def readline(self):
        if not self.items:
            return b""
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def test_stderr_drain_survives_an_overlong_line(capsys):
    cs = comment_source.CommentSource(on_items=lambda i: None, on_state=lambda s, d="": None)
    proc = SimpleNamespace(stderr=_Stream([b"first\n", ValueError("chunk is longer than limit"),
                                           b"second\n"]))
    run(cs._drain_stderr(proc))
    out = capsys.readouterr().out
    assert "first" in out and "second" in out


# ---- #12 Windows 上 execv 的参数要加引号 ----------------------------------------------

def test_exec_args_quotes_paths_with_spaces_on_windows(monkeypatch):
    from app import relaunch
    argv = [r"C:\Users\Zhang San\.venv\Scripts\python.exe", r"C:\Users\Zhang San\main.py", "--port", "8765"]
    monkeypatch.setattr(relaunch.os, "name", "nt")
    quoted = exec_args(argv)
    assert quoted[0] == '"C:\\Users\\Zhang San\\.venv\\Scripts\\python.exe"'
    assert quoted[2:] == ["--port", "8765"]
    monkeypatch.setattr(relaunch.os, "name", "posix")
    assert exec_args(argv) == argv


# ---- #13 滤镜路径转义：拿真 ffmpeg 验 -------------------------------------------------

def test_filter_path_round_trips_through_real_ffmpeg():
    ff = find_ffmpeg()
    if not ff:
        pytest.skip("本机没有 ffmpeg")
    spec = "arnndn=m=" + filter_path("C:/Users/x y/bd.rnnn")
    r = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                        "-i", "anullsrc=r=16000:cl=mono", "-t", "0.05", "-af", spec,
                        "-f", "null", "-"], capture_output=True, text=True, timeout=60)
    if "No such filter" in r.stderr:
        pytest.skip("这个 ffmpeg 没编 arnndn")
    # 路径被完整解析出来（然后因为文件不存在而失败）——冒号没有被当成分隔符
    assert "C:/Users/x y/bd.rnnn" in r.stderr, r.stderr


# ---- #14 CUDA 要真能用；不能用就退回 CPU ------------------------------------------------

def test_cuda_counts_only_when_ctranslate2_sees_a_device(monkeypatch):
    monkeypatch.setattr(hwdetect.shutil, "which", lambda n: "/usr/bin/nvidia-smi")
    monkeypatch.setitem(sys.modules, "ctranslate2", SimpleNamespace(get_cuda_device_count=lambda: 0))
    assert hwdetect._cuda_usable() is False
    monkeypatch.setitem(sys.modules, "ctranslate2", SimpleNamespace(get_cuda_device_count=lambda: 1))
    assert hwdetect._cuda_usable() is True
    monkeypatch.setitem(sys.modules, "ctranslate2", None)      # 没装
    assert hwdetect._cuda_usable() is False


def test_ct2_transcriber_falls_back_to_cpu_when_cuda_fails(monkeypatch):
    from app import asr
    calls = []

    class FakeWhisperModel:
        def __init__(self, model, device, compute_type):
            calls.append((device, compute_type))
            if device == "cuda":
                raise RuntimeError("CUDA driver version is insufficient")

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=FakeWhisperModel))
    t = asr.Transcriber("large-v3", device="cuda", compute_type="float16")
    assert isinstance(t.model, FakeWhisperModel)
    assert calls == [("cuda", "float16"), ("cpu", "int8")]


# ---- #15 取消后才加载完的模型下次直接用 ------------------------------------------------

def test_a_load_that_finished_after_cancel_is_reused(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    import app.asr
    import app.resolver
    loads = []
    monkeypatch.setattr(app.asr, "create_transcriber", lambda **kw: loads.append(1) or object())

    async def fake_resolve(url, cookies=None, cookies_browser="auto", trace=None):
        return url

    async def fake_session(media, *a, **k):
        return True, 60.0

    monkeypatch.setattr(app.resolver, "resolve_stream_url", fake_resolve)
    monkeypatch.setattr(p, "_stream_session", fake_session)

    async def scenario():
        await p._run_stream_inner("https://cdn.example.com/s.flv")
        assert loads == [1]
        # 模拟「上次加载被取消、后来才完成」：模型没提交，在途 future 已完成且 key 相同
        p._transcriber = None
        p._transcriber_key = None
        await p._run_stream_inner("https://cdn.example.com/s.flv")
        assert loads == [1]
        assert p._transcriber is not None

    run(scenario())


# ---- #16 有音频的轮次不算重连失败 --------------------------------------------------------

def test_short_but_audible_reconnects_do_not_exhaust_the_budget(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    import app.resolver
    sessions = []

    async def fake_session(media, *a, **k):
        sessions.append(media)
        return (True, 25.0) if len(sessions) <= 8 else (False, 0.0)

    async def fake_resolve(url, cookies=None, cookies_browser="auto", trace=None):
        return "http://cdn/s.flv"

    monkeypatch.setattr(app.resolver, "resolve_stream_url", fake_resolve)
    monkeypatch.setattr(p, "_stream_session", fake_session)
    run(p._run_stream_inner("https://www.tiktok.com/@x/live"))
    assert len(sessions) >= 9                # 以前第 6 轮就宣布「重连失败」放弃
    assert server.statuses[-1][0] == "error"  # 连续几轮一帧都没有才放弃


# ---- #18 换场清掉可重译的旧字幕 ----------------------------------------------------------

def test_recent_captions_do_not_survive_a_room_switch(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    import app.resolver

    async def fake_resolve(url, cookies=None, cookies_browser="auto", trace=None):
        return url

    async def fake_session(media, *a, **k):
        return True, 60.0

    monkeypatch.setattr(app.resolver, "resolve_stream_url", fake_resolve)
    monkeypatch.setattr(p, "_stream_session", fake_session)
    p._recent[1] = {"id": 1, "text": "vieja"}
    p._strong_missing = True
    run(p._run_stream_inner("https://cdn.example.com/s.flv"))
    assert not p._recent and p._strong_missing is False


# ---- #19 模板探测失败不缓存 ---------------------------------------------------------------

class _Resp:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def test_failed_template_probe_is_not_cached_as_raw(monkeypatch):
    tr = translator.OllamaHyMT2Translator.__new__(translator.OllamaHyMT2Translator)
    tr.model = "probe-test-model"
    tr.url = "http://127.0.0.1:11434/api/generate"
    responses = [_Resp(404, {}), _Resp(200, {"template": "{{ .Prompt }}"})]

    class Session:
        def post(self, url, data=None):
            return responses.pop(0)

    async def session():
        return Session()

    monkeypatch.setattr(tr, "session", session)
    monkeypatch.setattr(translator, "_RAW_MODE", {})
    assert run(tr._needs_raw()) is True           # 这次按 raw
    assert "probe-test-model" not in translator._RAW_MODE
    assert run(tr._needs_raw()) is False          # 模型拉好后问到了模板
    assert translator._RAW_MODE["probe-test-model"] is False


# ---- #20 Latin 品牌名里的 hy- 不是特殊标记 ----------------------------------------------

def test_brand_names_containing_hy_are_not_stripped():
    assert translator._strip_special("Healthy-Life 排毒粉") == "Healthy-Life 排毒粉"
    assert translator._strip_special("Shy_Girl 口红") == "Shy_Girl 口红"
    assert translator._strip_special("售罄。｜hy_begin▁of▁sentence") == "售罄。"
    assert translator._strip_special("<｜hy_Assistant｜>好的") == "好的"


# ---- #21 settings.json 里 api_keys 类型错不炸 -------------------------------------------

def test_malformed_api_keys_setting_is_ignored(monkeypatch, tmp_path):
    from app import settings
    f = tmp_path / "settings.json"
    f.write_text(json.dumps({"api_keys": "abcd:fx"}), encoding="utf-8")
    monkeypatch.setattr(settings, "SETTINGS_FILE", f)
    monkeypatch.delenv("DEEPL_API_KEY", raising=False)
    assert translator.api_key("DEEPL_API_KEY") is None
    assert translator.saved_keys() == {}
    assert translator.mask_key(1234567) == "…4567"


# ---- #23 macOS 上 open -a 失败要退回 ollama serve ------------------------------------------

def test_mac_start_falls_back_to_serve_when_there_is_no_app(monkeypatch):
    launched = []
    state = {"running": False}

    async def is_running(timeout=2):
        return state["running"]

    async def fake_exec(*cmd, **kw):
        launched.append(cmd)
        if cmd[0] == "open":
            return SimpleNamespace(wait=_rc(1))
        state["running"] = True
        return SimpleNamespace(wait=_rc(0))

    monkeypatch.setattr(localmodel, "is_running", is_running)
    monkeypatch.setattr(localmodel, "find_binary", lambda: "/opt/homebrew/bin/ollama")
    monkeypatch.setattr(localmodel.sys, "platform", "darwin")
    monkeypatch.setattr(localmodel.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(localmodel.asyncio, "sleep", _noop_sleep)
    assert run(localmodel.start(timeout=1)) is True
    assert launched == [("open", "-a", "Ollama"), ("/opt/homebrew/bin/ollama", "serve")]


def _rc(code):
    async def wait():
        return code
    return wait


async def _noop_sleep(_secs):
    return None


# ---- #24 兜底 id 带进程标识 ----------------------------------------------------------------

def test_fallback_comment_ids_carry_the_process_id():
    ev = SimpleNamespace(comment="hola", user=SimpleNamespace(nickname="Toñita"),
                         common=SimpleNamespace(msg_id=None))
    a = comment_source.event_to_item(ev)["id"]
    b = comment_source.event_to_item(ev)["id"]
    assert a != b
    assert a.startswith("t{}-".format(os.getpid()))


# ---- #25 读浏览器登录态有超时 ----------------------------------------------------------------

def test_cookie_lookup_gives_up_instead_of_hanging(monkeypatch):
    states = []

    async def on_state(state, detail=""):
        states.append(state)

    cs = comment_source.CommentSource(on_items=lambda i: None, on_state=on_state)
    cs.COOKIE_READ_TIMEOUT_SEC = 0.05
    monkeypatch.setattr(comment_source, "session_cookies",
                        lambda browser: (time.sleep(0.4), ("sid", "idc"))[1])

    async def scenario():
        t0 = time.monotonic()
        got = await cs._fetch_session_cookies()
        return got, time.monotonic() - t0

    got, elapsed = run(scenario())
    assert got == (None, None) and elapsed < 0.3
    assert "connecting" in states


# ---- #30 自检查 ct2 turbo 的正确仓库 -------------------------------------------------------

def test_selfcheck_finds_the_cached_turbo_model(monkeypatch, tmp_path):
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.delenv("HUGGINGFACE_HUB_CACHE", raising=False)
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    snap = tmp_path / "hub" / "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo" / "snapshots" / "abc"
    snap.mkdir(parents=True)
    (snap / "model.bin").write_bytes(b"x")
    assert selfcheck._ct2_repo("large-v3-turbo") == "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
    assert selfcheck._model_cached("large-v3-turbo", "ct2") is True
    assert selfcheck._model_cached("large-v3", "ct2") is False


# ---- #31 磁盘盘点认得 mlx turbo ------------------------------------------------------------

def test_disk_inventory_marks_the_loaded_mlx_turbo_in_use():
    d = "models--mlx-community--whisper-large-v3-turbo"
    assert diskspace._asr_in_use(d, ("mlx", "large-v3-turbo")) is True
    assert diskspace._asr_in_use(d, ("mlx", "large-v3")) is False
    assert diskspace._asr_in_use("models--mlx-community--whisper-large-v3-mlx", ("mlx", "large-v3")) is True


# ---- #32 / #35 启动脚本 ------------------------------------------------------------------------

def test_bootstrap_lock_left_by_a_dead_process_is_taken_over(tmp_path):
    """上次安装被强退：锁文件还在、持有者已死。以前要按 mtime 干等 20 分钟，
    每次启动都弹「另一个实例正在安装」。注意：绝不能 import main——它在模块级
    就跑 ensure_env()，会把 pytest 进程 execv 成 .venv 里的 main.py。"""
    from app import bootlock
    dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                          capture_output=True, text=True, check=True).stdout.strip()
    lock = tmp_path / ".venv.lock"
    lock.write_text(dead, encoding="utf-8")
    state, got = bootlock.acquire(lock, deps_ok=lambda: False)
    assert state == "acquired" and got == lock
    assert lock.read_text(encoding="utf-8") == str(os.getpid())


def test_bootstrap_lock_held_by_a_live_process_is_respected(tmp_path):
    """持有者活着就等，不按 mtime 抢——真的慢网安装不能被第二个实例插队。"""
    from app import bootlock
    lock = tmp_path / ".venv.lock"
    lock.write_text(str(os.getpid()), encoding="utf-8")
    os.utime(lock, (0, 0))                   # 看起来很旧
    now = [0.0]

    def clock():
        return now[0]

    def sleep(sec):
        now[0] += sec

    state, _ = bootlock.acquire(lock, deps_ok=lambda: False, wait_sec=10, sleep=sleep, clock=clock)
    assert state == "busy"
    assert lock.read_text(encoding="utf-8") == str(os.getpid())   # 没被抢


# ---- #33 更新预检没过就不停直播 ----------------------------------------------------------------

def test_update_precheck_failure_leaves_the_stream_running(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    stops, applied = [], []

    class FakeUpdater:
        def __init__(self, ok):
            self.ok = ok

        async def precheck(self):
            return self.ok

        async def apply(self):
            applied.append(1)

    async def stop_stream(quiet=False):
        stops.append(1)

    monkeypatch.setattr(p, "stop_stream", stop_stream)
    p.updater = FakeUpdater(False)
    run(p._apply_update())
    assert stops == [] and applied == []
    p.updater = FakeUpdater(True)
    run(p._apply_update())
    assert stops == [1] and applied == [1]
