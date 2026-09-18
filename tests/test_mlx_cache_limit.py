"""MLX 缓冲缓存的可选上限，和写进会话日志的内存读数。

全部用假的 mlx.core / mlx_whisper / faster_whisper（塞进 sys.modules）：这里不 import mlx、
不加载任何模型。直播转写进行时也可以跑（项目规则三）。

  * 上限：默认不设（不碰 mlx 的任何接口）；环境变量 TLT_MLX_CACHE_MB 优先于 settings.json 的
    mlx_cache_limit_mb；不是非负整数的值提示一次后忽略，不抛异常；每加载一个 MLX 模型设一次；
    mx.set_cache_limit 没有时用 mx.metal.set_cache_limit，都没有就说一次；ct2 路径不碰 mlx；
  * asr_config 记下实际设上的值（没设是 null）；
  * memory_stats：三个读数，读不到的是 None；
  * asr_memory：模型就绪时一条、之后每 5 分钟一条，写进这一场自己的审计，不进识别线程，
    不进 WebSocket 统计，读数出错不带崩统计循环，非 mlx 后端不写。

变异检查（手工跑过，见提交说明）：去掉 apply_mlx_cache_limit 里的 set_limit(...) 调用，
或者把 _record_asr_memory 里的 sess["audit"] 换成 self.audit，这里都有用例变红。
"""
import asyncio
import json
import sys
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from app import asr, settings
from app import pipeline as pipeline_mod
from app.audit import AuditLog
from app.pipeline import Pipeline
from tests.helpers import run

LIVE_URL = "https://www.tiktok.com/@bellaallnatural/live"
DIRECT_URL = "https://cdn.example.com/s.flv"
MB = 1024 * 1024


def rows(path, kind=None):
    out = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]
    return [r for r in out if kind is None or r["type"] == kind]


class FakeCore:
    """假的 mlx.core：记下被碰过的每一个属性名，和每一次调用。"""

    def __init__(self, **functions):
        self.__dict__["touched"] = []
        self.__dict__["calls"] = []
        self.__dict__["_functions"] = functions

    def __getattr__(self, name):
        self.__dict__["touched"].append(name)
        functions = self.__dict__["_functions"]
        if name not in functions:
            raise AttributeError(name)
        return functions[name]


def recorder(core, name, result=None):
    def call(*args):
        core.calls.append((name,) + args)
        return result
    return call


def install_core(monkeypatch, core):
    """mlx 和 mlx.core 都换成假的：哪条路径都 import 不到真的 mlx。"""
    monkeypatch.setitem(sys.modules, "mlx", ModuleType("mlx"))
    monkeypatch.setitem(sys.modules, "mlx.core", core)
    return core


def core_with_limit(monkeypatch):
    core = FakeCore()
    core._functions["set_cache_limit"] = recorder(core, "set_cache_limit", 0)
    return install_core(monkeypatch, core)


def core_with_getters(monkeypatch, active=3300 * MB, cache=3648 * MB, peak=7000 * MB):
    core = FakeCore()
    core._functions.update(
        get_active_memory=recorder(core, "get_active_memory", active),
        get_cache_memory=recorder(core, "get_cache_memory", cache),
        get_peak_memory=recorder(core, "get_peak_memory", peak))
    return install_core(monkeypatch, core)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.delenv(asr.MLX_CACHE_ENV, raising=False)
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(asr, "_mlx_said", set())
    yield
    real = getattr(sys.modules.get("mlx.core"), "__file__", None)
    assert real is None, "这些用例不许 import 真的 mlx.core"


@pytest.fixture
def events(monkeypatch):
    """假的 mlx_whisper：transcribe 只记一笔 "load" / "transcribe"，不加载任何东西。"""
    log = []
    fake = ModuleType("mlx_whisper")

    def transcribe(audio, **kw):
        log.append("transcribe" if "temperature" in kw else "load")
        return {"segments": [], "language": "es"}

    fake.transcribe = transcribe
    monkeypatch.setitem(sys.modules, "mlx_whisper", fake)
    return log


def save_settings(value):
    settings.SETTINGS_FILE.write_text(json.dumps({asr.MLX_CACHE_SETTING: value}),
                                      encoding="utf-8")


# ---- 上限 ---------------------------------------------------------------------------

def test_the_limit_is_set_once_per_mlx_load_in_bytes_after_the_load(monkeypatch, events, capsys):
    core = core_with_limit(monkeypatch)
    core._functions["set_cache_limit"] = lambda n: events.append(("limit", n))
    monkeypatch.setenv(asr.MLX_CACHE_ENV, "1024")

    first = asr.create_transcriber("mlx", "large-v3", language="es")
    assert events == ["load", ("limit", 1024 * 1024 * 1024)]
    assert first.mlx_cache_limit_mb == 1024
    assert "[信息] MLX 缓冲缓存上限：1024 MB" in capsys.readouterr().out

    # 换模型后重新加载的那一个 MLX 模型：再设一次
    asr.create_transcriber("mlx", "large-v3-turbo", language="es")
    assert events == ["load", ("limit", 1073741824), "load", ("limit", 1073741824)]


def test_the_environment_variable_beats_settings_json(monkeypatch, events):
    core = core_with_limit(monkeypatch)
    save_settings(2048)
    assert asr.MLXTranscriber("large-v3").mlx_cache_limit_mb == 2048
    monkeypatch.setenv(asr.MLX_CACHE_ENV, "512")
    assert asr.MLXTranscriber("large-v3").mlx_cache_limit_mb == 512
    assert core.calls == [("set_cache_limit", 2048 * MB), ("set_cache_limit", 512 * MB)]


def test_zero_means_keep_no_cache_and_is_not_the_same_as_unset(monkeypatch, events):
    core = core_with_limit(monkeypatch)
    monkeypatch.setenv(asr.MLX_CACHE_ENV, "0")
    t = asr.MLXTranscriber("large-v3")
    assert core.calls == [("set_cache_limit", 0)] and t.mlx_cache_limit_mb == 0


DEFAULT = asr.DEFAULT_MLX_CACHE_MB


def test_the_default_is_256_mb_when_nothing_is_configured(monkeypatch, events, capsys):
    """2026-09-17 实测定的默认值（见 app/asr.py 的注释和 tools/bench_mlx_cache.py）。"""
    assert DEFAULT == 256
    core = core_with_limit(monkeypatch)
    t = asr.MLXTranscriber("large-v3")
    assert core.calls == [("set_cache_limit", 256 * MB)] and t.mlx_cache_limit_mb == 256
    assert "[警告]" not in capsys.readouterr().out
    # 空的环境变量、settings 里的 null 也是「没配置」：用默认值，不提示
    monkeypatch.setenv(asr.MLX_CACHE_ENV, "  ")
    save_settings(None)
    assert asr.MLXTranscriber("large-v3").mlx_cache_limit_mb == 256
    assert "[警告]" not in capsys.readouterr().out


@pytest.mark.parametrize("word", ["off", "OFF", " none ", "unlimited"])
@pytest.mark.parametrize("where", ["env", "settings"])
def test_off_means_no_limit_and_nothing_in_mlx_is_touched(monkeypatch, events, capsys, word, where):
    """明确写 off：回到以前的行为——mlx 的任何属性都不碰，识别时也一样。"""
    core = core_with_limit(monkeypatch)
    if where == "env":
        monkeypatch.setenv(asr.MLX_CACHE_ENV, word)
    else:
        save_settings(word)
    t = asr.MLXTranscriber("large-v3")
    t.transcribe(b"\x00\x00" * 1600)
    assert core.touched == [] and core.calls == []
    assert t.mlx_cache_limit_mb is None
    assert "MLX" not in capsys.readouterr().out


def test_off_in_the_environment_beats_a_number_in_settings(monkeypatch, events):
    core = core_with_limit(monkeypatch)
    monkeypatch.setenv(asr.MLX_CACHE_ENV, "off")
    save_settings(1024)
    assert asr.MLXTranscriber("large-v3").mlx_cache_limit_mb is None and core.calls == []


@pytest.mark.parametrize("junk", ["lots", "-1", "1.5", "1e3", "1024MB", "١٢٣"])
def test_a_junk_environment_value_is_ignored_with_one_warning(monkeypatch, events, capsys, junk):
    core = core_with_limit(monkeypatch)
    monkeypatch.setenv(asr.MLX_CACHE_ENV, junk)
    for _ in range(3):                         # 写错了的值当作没给：用默认值
        assert asr.MLXTranscriber("large-v3").mlx_cache_limit_mb == DEFAULT
    out = capsys.readouterr().out
    assert out.count("[警告]") == 1 and asr.MLX_CACHE_ENV in out and "已忽略" in out
    assert core.calls == [("set_cache_limit", DEFAULT * MB)] * 3


@pytest.mark.parametrize("junk", [True, -5, 12.5, "big", [1024], {"mb": 1}])
def test_a_junk_settings_value_is_ignored_with_one_warning(monkeypatch, events, capsys, junk):
    core = core_with_limit(monkeypatch)
    save_settings(junk)
    for _ in range(2):
        assert asr.MLXTranscriber("large-v3").mlx_cache_limit_mb == DEFAULT
    out = capsys.readouterr().out
    assert out.count("[警告]") == 1 and asr.MLX_CACHE_SETTING in out
    assert core.calls == [("set_cache_limit", DEFAULT * MB)] * 2


def test_a_junk_environment_value_falls_through_to_settings(monkeypatch, events, capsys):
    core = core_with_limit(monkeypatch)
    monkeypatch.setenv(asr.MLX_CACHE_ENV, "plenty")
    save_settings("768")                       # 手改 settings.json 时写成了字符串：认
    assert asr.MLXTranscriber("large-v3").mlx_cache_limit_mb == 768
    assert core.calls == [("set_cache_limit", 768 * MB)]
    assert capsys.readouterr().out.count("[警告]") == 1


def test_an_unreadable_settings_file_never_raises(monkeypatch, events):
    core_with_limit(monkeypatch)

    def boom():
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(settings, "load_settings", boom)
    assert asr.mlx_cache_limit_mb() == DEFAULT          # 读不了设置：用默认值，不抛
    assert asr.MLXTranscriber("large-v3").mlx_cache_limit_mb == DEFAULT


def test_older_mlx_keeps_the_call_under_mx_metal(monkeypatch, events):
    core = FakeCore()
    core._functions["metal"] = SimpleNamespace(
        set_cache_limit=recorder(core, "metal.set_cache_limit", 0))
    install_core(monkeypatch, core)
    monkeypatch.setenv(asr.MLX_CACHE_ENV, "256")
    assert asr.MLXTranscriber("large-v3").mlx_cache_limit_mb == 256
    assert core.calls == [("metal.set_cache_limit", 256 * MB)]


def test_the_top_level_call_is_preferred_over_mx_metal(monkeypatch, events):
    core = core_with_limit(monkeypatch)
    core._functions["metal"] = SimpleNamespace(
        set_cache_limit=recorder(core, "metal.set_cache_limit", 0))
    monkeypatch.setenv(asr.MLX_CACHE_ENV, "256")
    asr.MLXTranscriber("large-v3")
    assert core.calls == [("set_cache_limit", 256 * MB)]


@pytest.mark.parametrize("present", [True, False])
def test_a_missing_api_is_said_once_and_loading_continues(monkeypatch, events, capsys, present):
    if present:
        install_core(monkeypatch, FakeCore())          # 有 mlx.core，但哪儿都没有 set_cache_limit
    else:
        monkeypatch.delitem(sys.modules, "mlx.core", raising=False)
    monkeypatch.setenv(asr.MLX_CACHE_ENV, "1024")
    made = [asr.MLXTranscriber("large-v3") for _ in range(2)]
    assert [t.mlx_cache_limit_mb for t in made] == [None, None]
    out = capsys.readouterr().out
    assert out.count("没有 set_cache_limit") == 1 and "上限：" not in out
    assert events == ["load", "load"]


def test_a_failing_set_cache_limit_is_reported_and_not_raised(monkeypatch, events, capsys):
    core = FakeCore()

    def refuse(n):
        raise OverflowError("limit too large")

    core._functions["set_cache_limit"] = refuse
    install_core(monkeypatch, core)
    monkeypatch.setenv(asr.MLX_CACHE_ENV, "1024")
    assert asr.MLXTranscriber("large-v3").mlx_cache_limit_mb is None
    out = capsys.readouterr().out
    assert "limit too large" in out and "上限：" not in out


def test_the_ct2_path_never_touches_mlx(monkeypatch, tmp_path):
    core = core_with_limit(monkeypatch)
    core._functions.update(get_active_memory=recorder(core, "get_active_memory", 1))
    fake = ModuleType("faster_whisper")
    fake.WhisperModel = lambda *a, **k: object()
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)
    monkeypatch.setenv(asr.MLX_CACHE_ENV, "1024")
    save_settings(2048)

    t = asr.create_transcriber("ct2", "small", device="cpu", compute_type="int8")
    assert isinstance(t, asr.Transcriber) and not hasattr(t, "mlx_cache_limit_mb")
    assert not hasattr(t, "memory_stats")

    p, _ = make_pipeline(monkeypatch, tmp_path)
    audit = AuditLog(room_url=LIVE_URL, log_dir=tmp_path / "logs")
    sess = p._new_session_state(audit)
    assert p._record_asr_memory(sess, t) is False
    assert p._asr_config_record({"backend": "ct2", "model": "small", "device": "cpu",
                                 "compute_type": "int8"}, t)["mlx_cache_limit_mb"] is None
    audit.close()
    assert rows(audit.path, "asr_memory") == []
    assert core.touched == [] and core.calls == []


# ---- memory_stats -------------------------------------------------------------------

def test_memory_stats_reports_three_figures_in_mb(monkeypatch, events):
    core = core_with_getters(monkeypatch, active=3300 * MB, cache=int(3648.26 * MB),
                             peak=7000 * MB)
    stats = asr.MLXTranscriber("large-v3").memory_stats()
    assert stats == {"active_mb": 3300.0, "cache_mb": 3648.3, "peak_mb": 7000.0}
    assert sorted(c[0] for c in core.calls) == [
        "get_active_memory", "get_cache_memory", "get_peak_memory"]


def test_memory_stats_falls_back_to_mx_metal_per_getter(monkeypatch, events):
    core = FakeCore()
    core._functions["get_active_memory"] = lambda: 10 * MB
    core._functions["metal"] = SimpleNamespace(get_active_memory=lambda: 99 * MB,
                                               get_cache_memory=lambda: 20 * MB)
    install_core(monkeypatch, core)
    assert asr.MLXTranscriber("large-v3").memory_stats() == {
        "active_mb": 10.0, "cache_mb": 20.0, "peak_mb": None}


def test_memory_stats_never_raises(monkeypatch, events):
    def boom():
        raise RuntimeError("[METAL] device lost")

    core = FakeCore()
    core._functions.update(get_active_memory=boom, get_cache_memory=lambda: "many",
                           get_peak_memory=lambda: 5 * MB)
    install_core(monkeypatch, core)
    t = asr.MLXTranscriber("large-v3")
    assert t.memory_stats() == {"active_mb": None, "cache_mb": None, "peak_mb": 5.0}
    monkeypatch.delitem(sys.modules, "mlx.core")
    assert t.memory_stats() == {"active_mb": None, "cache_mb": None, "peak_mb": None}


def test_transcribe_does_not_read_memory_or_set_limits(monkeypatch, events):
    """识别那条路上不多一步：上限只在加载时设，读数只由管线在事件循环里读。"""
    core = core_with_getters(monkeypatch)
    core._functions["set_cache_limit"] = recorder(core, "set_cache_limit", 0)
    monkeypatch.setenv(asr.MLX_CACHE_ENV, "1024")
    t = asr.MLXTranscriber("large-v3")
    loaded = list(core.calls)
    for _ in range(3):
        t.transcribe(b"\x00\x00" * 1600)
    assert core.calls == loaded == [("set_cache_limit", 1024 * MB)]


# ---- 管线：asr_config 与 asr_memory ---------------------------------------------------

class StubServer:
    def __init__(self):
        self.config = {}
        self.broadcasts = []

    async def status(self, state, detail="", command=None):
        pass

    async def broadcast(self, msg):
        self.broadcasts.append(msg)


def make_pipeline(monkeypatch, tmp_path):
    from app.comment_source import CommentSource

    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    terms_file = tmp_path / "banned_terms.txt"
    terms_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms_file)
    monkeypatch.setattr(pipeline_mod, "_tiktoklive_version", lambda: "7.0.1")
    monkeypatch.setattr(CommentSource, "start", lambda self, unique_id: None)

    async def quiet(self, *a, **k):
        return None

    monkeypatch.setattr(CommentSource, "stop", quiet)
    monkeypatch.setattr(Pipeline, "run_selfcheck", quiet)
    monkeypatch.setattr(Pipeline, "ensure_local_translator", quiet)
    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source="es", beam=5,
        context=False, asr_temperature=None, glossary=None, backend="auto", model=None,
        device="auto", compute_type="auto", denoise="off", banned_terms=None, comments=False)
    server = StubServer()
    return Pipeline(args, server), server


def load_world(monkeypatch, p, backend, create, during_session=None):
    """一场假的直播：解析直接返回、拉流那一步换成 during_session。模型走真的加载流程
    （线程池里调 create）。"""
    import app.hwdetect
    import app.resolver
    rec = {"backend": backend, "model": "large-v3", "device": "auto", "compute_type": "auto",
           "note": ""}
    monkeypatch.setattr(app.hwdetect, "recommend", lambda backend=None, device=None: dict(rec))
    monkeypatch.setattr(asr, "create_transcriber", create)

    async def resolve(url, cookies=None, cookies_browser="auto", trace=None):
        return url

    async def session(media, slot, *a, **k):
        if during_session is not None:
            during_session(k.get("sess"))
        return True, 60.0

    async def gone(url, timeout=8):
        return False                  # 直连地址「已拉不到数据」：这一场到此结束，不发真的请求

    monkeypatch.setattr(app.resolver, "resolve_stream_url", resolve)
    monkeypatch.setattr(app.resolver, "_media_url_works", gone)
    monkeypatch.setattr(p, "_stream_session", session)


def session_logs(tmp_path):
    return sorted((tmp_path / "logs").glob("session-*.jsonl"))


class FakeMLX:
    """管线眼里的 mlx 识别器：backend 是 mlx，memory_stats 记下是在哪个线程里被调的。"""

    backend, model_size, device, compute_type = "mlx", "large-v3", "gpu", "float16"
    mlx_cache_limit_mb = None

    def __init__(self, stats=None):
        self.stats = stats or {"active_mb": 3300.0, "cache_mb": 900.5, "peak_mb": 7000.0}
        self.read_on = []

    def memory_stats(self):
        self.read_on.append(threading.current_thread())
        if isinstance(self.stats, Exception):
            raise self.stats
        return dict(self.stats)


def test_asr_config_carries_the_limit_that_was_applied(monkeypatch, tmp_path, events):
    core = core_with_limit(monkeypatch)
    real_create = asr.create_transcriber
    p, _ = make_pipeline(monkeypatch, tmp_path)
    load_world(monkeypatch, p, "mlx", lambda **kw: real_create(**kw))
    monkeypatch.setenv(asr.MLX_CACHE_ENV, "1024")
    run(p._run_stream_inner(DIRECT_URL))
    run(p._run_stream_inner(DIRECT_URL))               # 第二场复用模型：不重新设，记录照样带
    configs = [rows(f, "asr_config") for f in session_logs(tmp_path)]
    assert [[c["mlx_cache_limit_mb"] for c in one] for one in configs] == [[1024], [1024]]
    assert core.calls == [("set_cache_limit", 1024 * MB)] and events == ["load"]


def test_asr_config_says_null_when_the_limit_is_switched_off(monkeypatch, tmp_path, events):
    core = core_with_limit(monkeypatch)
    monkeypatch.setenv(asr.MLX_CACHE_ENV, "off")
    real_create = asr.create_transcriber
    p, _ = make_pipeline(monkeypatch, tmp_path)
    load_world(monkeypatch, p, "mlx", lambda **kw: real_create(**kw))
    run(p._run_stream_inner(DIRECT_URL))
    config = rows(session_logs(tmp_path)[0], "asr_config")[0]
    assert "mlx_cache_limit_mb" in config and config["mlx_cache_limit_mb"] is None
    assert core.calls == []


def test_asr_memory_is_written_when_the_model_is_ready_then_every_five_minutes(
        monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    t = FakeMLX()
    loaded_on = []
    ticks = []

    def create(**kw):
        loaded_on.append(threading.current_thread())
        return t

    def during_session(sess):
        start = sess["asr_memory_at"]                   # 模型就绪那一条的时刻（单调时钟）
        # 不踩精确的 300.0：start 是真实的单调时钟读数，(start + 300.0) - start 在浮点下可能是
        # 299.99999999999994。刚开机的 Linux CI 机器上 start 只有三百多，正好落进这个误差
        for offset in (10.0, 290.0, 299.0, 301.0, 310.0, 600.0, 602.0, 905.0):
            ticks.append((offset, p._asr_memory_tick(now=start + offset)))

    load_world(monkeypatch, p, "mlx", create, during_session)
    run(p._run_stream_inner(DIRECT_URL))

    assert [offset for offset, wrote in ticks if wrote] == [301.0, 602.0, 905.0]
    log = rows(session_logs(tmp_path)[0])
    memory = [r for r in log if r["type"] == "asr_memory"]
    assert len(memory) == 4
    assert all((r["active_mb"], r["cache_mb"], r["peak_mb"]) == (3300.0, 900.5, 7000.0)
               and r["at"] for r in memory)
    kinds = [r["type"] for r in log]
    assert kinds.index("asr_config") < kinds.index("asr_memory")     # 模型就绪之后才有第一条
    # 读数都在事件循环线程里读，不在加载/识别模型的线程里
    assert loaded_on and loaded_on[0] is not threading.main_thread()
    assert len(t.read_on) == 4 and all(th is threading.main_thread() for th in t.read_on)
    # 不进 WebSocket
    assert not any("active_mb" in m or m.get("type") == "asr_memory" for m in server.broadcasts)
    assert "active_mb" not in p.telemetry.snapshot()


def test_every_session_gets_its_own_first_record(monkeypatch, tmp_path):
    p, _ = make_pipeline(monkeypatch, tmp_path)
    load_world(monkeypatch, p, "mlx", lambda **kw: FakeMLX())
    run(p._run_stream_inner(DIRECT_URL))
    run(p._run_stream_inner(DIRECT_URL))
    assert [len(rows(f, "asr_memory")) for f in session_logs(tmp_path)] == [1, 1]


def test_no_periodic_record_before_the_model_is_ready(monkeypatch, tmp_path):
    """新配置还在加载时 _transcriber 可能还是上一场的模型：就绪那一条没写过，就不按周期写。"""
    p, _ = make_pipeline(monkeypatch, tmp_path)
    p.audit = AuditLog(room_url=LIVE_URL, log_dir=tmp_path / "logs")
    p._session_state = p._new_session_state(p.audit)
    p._transcriber = FakeMLX()
    assert p._asr_memory_tick(now=10_000.0) is False
    assert p._transcriber.read_on == []
    p.audit.close()
    assert rows(p.audit.path, "asr_memory") == []


def test_asr_memory_goes_to_the_sessions_own_audit(monkeypatch, tmp_path):
    """晚到的旧任务手里的 sess 是自己那一场的；这时 self.audit 已经是下一场的了。"""
    p, _ = make_pipeline(monkeypatch, tmp_path)
    mine = AuditLog(room_url=LIVE_URL, log_dir=tmp_path / "mine")
    theirs = AuditLog(room_url=LIVE_URL, log_dir=tmp_path / "theirs")
    sess = p._new_session_state(mine)
    p.audit = theirs
    assert p._record_asr_memory(sess, FakeMLX()) is True
    mine.close()
    theirs.close()
    assert len(rows(mine.path, "asr_memory")) == 1
    assert rows(theirs.path, "asr_memory") == []


def test_the_first_record_survives_a_new_session_starting_during_the_load(monkeypatch, tmp_path):
    """同一件事走真的流程：模型加载期间 self.audit 换成了下一场的。"""
    p, _ = make_pipeline(monkeypatch, tmp_path)
    theirs = AuditLog(room_url=LIVE_URL, log_dir=tmp_path / "theirs")

    def create(**kw):
        p.audit = theirs
        return FakeMLX()

    load_world(monkeypatch, p, "mlx", create)
    run(p._run_stream_inner(DIRECT_URL))
    theirs.close()
    assert len(rows(session_logs(tmp_path)[0], "asr_memory")) == 1
    assert rows(theirs.path, "asr_memory") == []


def test_the_stats_loop_tick_leaves_a_session_that_is_not_current_alone(monkeypatch, tmp_path):
    p, _ = make_pipeline(monkeypatch, tmp_path)
    mine = AuditLog(room_url=LIVE_URL, log_dir=tmp_path / "mine")
    theirs = AuditLog(room_url=LIVE_URL, log_dir=tmp_path / "theirs")
    sess = p._session_state = p._new_session_state(mine)
    p._transcriber = FakeMLX()
    assert p._record_asr_memory(sess, p._transcriber, now=0.0) is True
    p.audit = theirs                                    # 下一场已经开始，_session_state 还没换
    assert p._asr_memory_tick(now=1000.0) is False
    mine.close()
    theirs.close()
    assert len(rows(mine.path, "asr_memory")) == 1 and rows(theirs.path, "asr_memory") == []


def test_getters_that_throw_give_a_record_of_nulls(monkeypatch, tmp_path, events):
    def boom():
        raise RuntimeError("[METAL] device lost")

    core = FakeCore()
    core._functions.update(get_active_memory=boom, get_cache_memory=boom, get_peak_memory=boom)
    install_core(monkeypatch, core)
    p, _ = make_pipeline(monkeypatch, tmp_path)
    audit = AuditLog(room_url=LIVE_URL, log_dir=tmp_path / "logs")
    sess = p._new_session_state(audit)
    assert p._record_asr_memory(sess, asr.MLXTranscriber("large-v3")) is True
    audit.close()
    record = rows(audit.path, "asr_memory")[0]
    assert (record["active_mb"], record["cache_mb"], record["peak_mb"]) == (None, None, None)


def test_a_failing_read_never_raises_and_is_not_retried_every_tick(monkeypatch, tmp_path, capsys):
    p, _ = make_pipeline(monkeypatch, tmp_path)
    p.audit = AuditLog(room_url=LIVE_URL, log_dir=tmp_path / "logs")
    sess = p._session_state = p._new_session_state(p.audit)
    t = p._transcriber = FakeMLX(stats=RuntimeError("boom"))
    assert p._record_asr_memory(sess, t, now=0.0) is False
    assert p._asr_memory_tick(now=10.0) is False and p._asr_memory_tick(now=20.0) is False
    assert len(t.read_on) == 1                          # 5 分钟内不重试
    assert p._asr_memory_tick(now=300.0) is False and len(t.read_on) == 2
    assert capsys.readouterr().out.count("记录 MLX 内存读数出错") == 2
    # 连 sess 都不对也不抛
    assert p._record_asr_memory(None, t) is False
    assert p._record_asr_memory({"audit": object()}, t, now=0.0) is False
    p.audit.close()
    assert rows(p.audit.path, "asr_memory") == []


def test_the_stats_loop_ticks_the_memory_record_and_survives_a_failing_read(
        monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    p.audit = AuditLog(room_url=LIVE_URL, log_dir=tmp_path / "logs")
    sess = p._session_state = p._new_session_state(p.audit)
    t = p._transcriber = FakeMLX(stats=RuntimeError("boom"))
    sess["asr_memory_at"] = -1e9                        # 早就过了 5 分钟：每一跳都会去读
    monkeypatch.setattr(Pipeline, "ASR_MEMORY_EVERY_SEC", 0.0)
    health = []

    async def check():
        health.append(1)
        if len(health) == 4:
            raise asyncio.CancelledError

    monkeypatch.setattr(p, "_check_audit_health", check)
    with pytest.raises(asyncio.CancelledError):
        run(p._stats_loop(interval=0))
    # 前三跳每跳读一次、每次都抛：循环照样走到第四跳
    assert len(health) == 4 and len(t.read_on) == 3
    assert all(th is threading.main_thread() for th in t.read_on)
    stats = [m for m in server.broadcasts if m.get("type") == "stats"]
    assert len(stats) == 4 and not any("active_mb" in m or "cache_mb" in m for m in stats)
    p.audit.close()


def test_non_mlx_backends_write_nothing(monkeypatch, tmp_path):
    p, _ = make_pipeline(monkeypatch, tmp_path)

    class CT2(FakeMLX):
        backend, device, compute_type = "ct2", "cpu", "int8"

    made = []
    load_world(monkeypatch, p, "ct2", lambda **kw: made.append(CT2()) or made[-1],
               lambda sess: p._asr_memory_tick(now=1e9))
    run(p._run_stream_inner(DIRECT_URL))
    log = session_logs(tmp_path)[0]
    assert rows(log, "asr_memory") == [] and made[0].read_on == []
    assert rows(log, "asr_config")[0]["mlx_cache_limit_mb"] is None
