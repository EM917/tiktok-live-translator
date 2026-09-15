"""识别与检测侧的保险：每条用例对应一条已核实的缺口。

  * 识别连续出错：换一个已完整下载的 CPU 配置（一场最多一次），换不了就说实话；
  * 识别调用卡住：记审计、说清会丢段，不自动重载（两个模型同时驻留是 08-31 的事故）；
  * 模型加载失败：记审计、不猜原因、自检那一行变红，mlx 失败且 turbo 已下载时改用它；
  * 违禁词表：体检出永远匹配不上的条目（不改匹配）、非 UTF-8 不再让程序起不来；
  * 审计：本场实际加载的词表、识别配置、自检结论都要落盘。
"""
import asyncio
import gc
import json
import os
import sys
import threading
import time
import weakref
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from app import asr
from app import pipeline as pipeline_mod
from app import selfcheck
from app.audit import AuditLog
from app.pipeline import Pipeline, _ASRSlot, load_detector
from app.telemetry import Telemetry

ROOT = Path(__file__).resolve().parent.parent
LIVE_URL = "https://www.tiktok.com/@bellaallnatural/live"
DIRECT_URL = "https://cdn.example.com/s.flv"


def run(coro):
    return asyncio.run(coro)


def rows(path, kind=None):
    out = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]
    return [r for r in out if kind is None or r["type"] == kind]


class StubServer:
    def __init__(self):
        self.config = {}
        self.statuses = []
        self.broadcasts = []

    async def status(self, state, detail="", command=None):
        self.statuses.append((state, detail))

    async def broadcast(self, msg):
        self.broadcasts.append(msg)
        if msg.get("type") == "incident":
            incidents = self.config.setdefault("incidents", {})
            if msg.get("level") == "clear":
                incidents.pop(msg["id"], None)
            else:
                incidents[msg["id"]] = msg

    def of(self, kind):
        return [m for m in self.broadcasts if m.get("type") == kind]


def make_pipeline(monkeypatch, tmp_path, terms=""):
    from app import settings
    from app.comment_source import CommentSource

    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    terms_file = tmp_path / "banned_terms.txt"
    if isinstance(terms, bytes):
        terms_file.write_bytes(terms)
    else:
        terms_file.write_text(terms, encoding="utf-8")
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


def fake_audio(monkeypatch, frames):
    """一帧切一段的假音频：段数就是帧数，好数。"""
    import app.audio
    import app.segmenter
    from app.audio import FRAME_BYTES

    class Source:
        def __init__(self, media, denoise_model=None):
            pass

        async def frames(self):
            for _ in range(frames):
                yield b"\x00" * FRAME_BYTES

        def stderr_tail(self):
            return ""

        async def stop(self):
            pass

    class OneSegmentPerFrame:
        def __init__(self, *a, **k):
            pass

        def feed(self, frame):
            return [frame]

        def flush(self):
            return []

    monkeypatch.setattr(app.audio, "FFmpegAudioSource", Source)
    monkeypatch.setattr(app.segmenter, "SilenceSegmenter", OneSegmentPerFrame)


def stream_once(p, slot):
    async def scenario():
        loop = asyncio.get_running_loop()
        return await p._stream_session("http://cdn/s.flv", slot, None, "note", loop)
    return run(scenario())


def recognized(text="hola"):
    return SimpleNamespace(text=text, raw_text=text, language="es", rejected=[])


class Broken:
    """每次识别都抛异常的识别器（macOS/mlx 更新后 Metal 报错那种）。"""

    def __init__(self, events, backend="mlx", model="large-v3", device="gpu",
                 compute_type="float16"):
        self.events = events
        self.backend, self.model_size = backend, model
        self.device, self.compute_type = device, compute_type
        self.calls = 0

    def transcribe(self, pcm):
        self.calls += 1
        raise RuntimeError("[METAL] Command buffer execution failed "
                           "https://huggingface.co/api/x?token=hf_secret")

    def release(self):
        self.events.append("release")
        asr.release_mlx_model()


class Working:
    def __init__(self, **kw):
        self.kw = kw
        self.backend, self.model_size = kw["backend"], kw["model_size"]
        self.device, self.compute_type = kw["device"], kw["compute_type"]

    def transcribe(self, pcm):
        return recognized()


def mlx_config(**over):
    cfg = {"backend": "mlx", "model": "large-v3", "device": "auto", "compute_type": "auto",
           "language": "es", "beam_size": 5, "use_context": False, "temperature": 0.0,
           "hotwords": None, "note": "Apple Silicon GPU"}
    cfg.update(over)
    return cfg


def fake_mlx_cache(monkeypatch):
    holder = type("ModelHolder", (), {"model": object(), "model_path": "mlx-community/x"})
    module = ModuleType("mlx_whisper.transcribe")
    module.ModelHolder = holder
    monkeypatch.setitem(sys.modules, "mlx_whisper.transcribe", module)
    return holder


def health_texts(server):
    return [m["text"] for m in server.of("health")]


# ---- 识别连续出错：换模型（一场最多一次）或说实话 -----------------------------------

def test_repeated_mlx_failures_switch_to_the_cached_cpu_turbo(monkeypatch, tmp_path):
    """以前第 3 次失败只发一句「请点停止再开始翻译」——停止/开始复用的就是这个坏模型。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    p.audit = AuditLog(room_url=LIVE_URL, log_dir=tmp_path / "logs")
    events, created = [], []
    holder = fake_mlx_cache(monkeypatch)

    def create(**kw):
        events.append("create")
        created.append(kw)
        return Working(**kw)

    monkeypatch.setattr(asr, "create_transcriber", create)
    monkeypatch.setattr(selfcheck, "_model_cached",
                        lambda model, backend: (model, backend) == ("large-v3-turbo", "ct2"))
    broken = Broken(events)
    p._transcriber, p._transcriber_key = broken, ("key",)
    slot = _ASRSlot(broken, config=mlx_config(), key=("key",), audit=p.audit)
    fake_audio(monkeypatch, frames=8)

    stream_once(p, slot)

    assert broken.calls == 3
    assert events == ["release", "create"]          # 先放掉坏模型，再加载：不同时驻留两个
    assert holder.model is None and holder.model_path is None
    assert len(created) == 1
    assert {k: created[0][k] for k in ("backend", "model_size", "device", "compute_type",
                                       "language")} == {
        "backend": "ct2", "model_size": "large-v3-turbo", "device": "cpu",
        "compute_type": "int8", "language": "es"}
    assert slot.transcriber is p._transcriber and isinstance(p._transcriber, Working)
    assert p._transcriber_key == ("key",)          # 停止/开始沿用这个能用的 CPU 模型
    assert len(server.of("caption")) == 5          # 换上之后剩下的 5 段都识别了
    fallback = rows(p.audit.path, "asr_backend_fallback")
    assert len(fallback) == 1
    assert fallback[0]["from"].startswith("mlx/large-v3")
    assert fallback[0]["to"] == "ct2/large-v3-turbo/cpu/int8"
    assert "hf_secret" not in json.dumps(fallback) and "hf_secret" not in json.dumps(
        rows(p.audit.path, "asr_failed"))
    assert [m["level"] for m in server.of("health")] == ["degraded", "lagging", "ok"]
    assert any("已改用 CPU 识别" in t for t in health_texts(server))
    assert not any("请点「停止」再「开始翻译」" in t for t in health_texts(server))
    incidents = server.config["incidents"]
    assert "asr-fallback" in incidents and "session:asr-failing" not in incidents
    assert [r["reason"] for r in rows(p.audit.path, "health")] == [
        "asr_failing", "asr_fallback", "backlog"]
    assert p._asr_check_state()["fallback"]["to"] == "ct2/large-v3-turbo/cpu/int8"


def test_failures_without_a_cached_fallback_do_not_reload_and_say_so(monkeypatch, tmp_path):
    """没下载好的模型绝不换：直播中途开始下 1.6 GB 只会让检测停得更久。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    p.audit = AuditLog(room_url=LIVE_URL, log_dir=tmp_path / "logs")
    events = []
    monkeypatch.setattr(asr, "create_transcriber",
                        lambda **kw: events.append("create") or Working(**kw))
    monkeypatch.setattr(selfcheck, "_model_cached", lambda model, backend: False)
    broken = Broken(events)
    p._transcriber, p._transcriber_key = broken, ("key",)
    slot = _ASRSlot(broken, config=mlx_config(), key=("key",), audit=p.audit)
    fake_audio(monkeypatch, frames=7)

    stream_once(p, slot)

    assert events == [] and broken.calls == 7
    assert p._transcriber is broken
    texts = health_texts(server)
    assert len(texts) == 1 and "程序自动恢复不了——请关闭程序重新打开" in texts[0]
    assert "请点「停止」" not in texts[0]
    incident = server.config["incidents"]["session:asr-failing"]
    assert incident["level"] == "error" and "关闭程序重新打开" in incident["text"]
    fallback = rows(p.audit.path, "asr_backend_fallback")
    assert len(fallback) == 1 and fallback[0]["to"] is None
    assert fallback[0]["reason"] == "fallback_not_cached"


def test_cuda_failures_recreate_the_same_model_on_cpu(monkeypatch, tmp_path):
    """Windows NVIDIA 机器缺 cuBLAS 时第一次识别才报错，构造时的 CUDA→CPU 退路管不到。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    created = []
    monkeypatch.setattr(asr, "create_transcriber",
                        lambda **kw: created.append(kw) or Working(**kw))
    monkeypatch.setattr(selfcheck, "_model_cached",
                        lambda model, backend: (model, backend) == ("large-v3", "ct2"))
    broken = Broken([], backend="ct2", model="large-v3", device="cuda", compute_type="float16")
    slot = _ASRSlot(broken, key=("key",), config=mlx_config(
        backend="ct2", device="cuda", compute_type="float16"))
    fake_audio(monkeypatch, frames=4)

    stream_once(p, slot)

    assert [(c["backend"], c["model_size"], c["device"], c["compute_type"]) for c in created] \
        == [("ct2", "large-v3", "cpu", "int8")]
    assert len(server.of("caption")) == 1


def test_the_switch_happens_at_most_once_per_session(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    p.audit = AuditLog(room_url=LIVE_URL, log_dir=tmp_path / "logs")
    created = []

    def create(**kw):
        created.append(kw)
        return Broken([], backend=kw["backend"], model=kw["model_size"],
                      device=kw["device"], compute_type=kw["compute_type"])

    monkeypatch.setattr(asr, "create_transcriber", create)
    monkeypatch.setattr(selfcheck, "_model_cached", lambda model, backend: True)
    slot = _ASRSlot(Broken([]), config=mlx_config(), key=("key",), audit=p.audit)
    fake_audio(monkeypatch, frames=9)

    stream_once(p, slot)

    assert len(created) == 1                       # 换上的也坏了：不再来回加载
    assert "程序自动恢复不了" in health_texts(server)[-1]
    fallback = rows(p.audit.path, "asr_backend_fallback")
    assert [r["to"] for r in fallback] == ["ct2/large-v3-turbo/cpu/int8", None]
    assert fallback[1]["reason"] == "already_switched"


def test_a_fallback_that_fails_to_load_is_reported_not_retried(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    p.audit = AuditLog(room_url=LIVE_URL, log_dir=tmp_path / "logs")
    calls = []

    def create(**kw):
        calls.append(kw)
        raise OSError("model.bin truncated")

    monkeypatch.setattr(asr, "create_transcriber", create)
    monkeypatch.setattr(selfcheck, "_model_cached", lambda model, backend: True)
    slot = _ASRSlot(Broken([]), config=mlx_config(), key=("key",), audit=p.audit)
    fake_audio(monkeypatch, frames=8)

    stream_once(p, slot)

    assert len(calls) == 1
    assert "程序自动恢复不了" in health_texts(server)[-1]
    fallback = rows(p.audit.path, "asr_backend_fallback")
    assert len(fallback) == 1 and fallback[0]["to"] is None
    assert "truncated" in fallback[0]["fallback_error"]


def test_a_fallback_model_is_released_before_the_next_config_loads(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    events = []

    class Fallback:
        def release(self):
            events.append("release")

    p._transcriber, p._transcriber_key = Fallback(), ("old-key",)
    p._asr_fallback = {"from": "mlx/large-v3", "to": "ct2/large-v3-turbo/cpu/int8",
                       "error": "x"}
    server.config["incidents"] = {"asr-fallback": {"id": "asr-fallback"}}
    import app.hwdetect
    import app.resolver
    monkeypatch.setattr(app.hwdetect, "recommend", lambda backend=None, device=None: {
        "backend": "mlx", "model": "large-v3", "device": "auto", "compute_type": "auto",
        "note": ""})
    monkeypatch.setattr(asr, "create_transcriber",
                        lambda **kw: events.append("create") or Working(**kw))

    async def resolve(url, cookies=None, cookies_browser="auto", trace=None):
        return url

    async def session(media, *a, **k):
        return True, 60.0

    monkeypatch.setattr(app.resolver, "resolve_stream_url", resolve)
    monkeypatch.setattr(p, "_stream_session", session)
    run(p._run_stream_inner(DIRECT_URL))

    assert events == ["release", "create"]
    assert p._asr_fallback is None and "asr-fallback" not in server.config["incidents"]


def test_release_helpers_free_the_model(monkeypatch):
    holder = fake_mlx_cache(monkeypatch)
    cleared = []
    core = ModuleType("mlx.core")
    core.clear_cache = lambda: cleared.append(1)
    monkeypatch.setitem(sys.modules, "mlx.core", core)
    assert asr.release_mlx_model() is True
    assert holder.model is None and cleared == [1]

    t = asr.Transcriber.__new__(asr.Transcriber)
    t.model = object()
    asr.release_transcriber(t)
    assert t.model is None
    with pytest.raises(RuntimeError):
        t.transcribe(b"\x00\x00")


class _Model:
    """代替一个大模型：能挂弱引用，看它是不是真的从内存里没了。"""


def test_the_failing_model_is_gone_before_the_cpu_model_loads(monkeypatch, tmp_path):
    """mlx_whisper/transcribe.py 里模型是局部变量（model = ModelHolder.get_model(...)），异常回溯
    的帧连着它。以前清了 ModelHolder，模型还被留着的异常拽在内存里，CPU 模型照样开始加载。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    p.audit = AuditLog(room_url=LIVE_URL, log_dir=tmp_path / "logs")
    holder = fake_mlx_cache(monkeypatch)
    holder.model = _Model()
    model_ref = weakref.ref(holder.model)
    seen = []

    class MlxLike:
        backend, model_size, device, compute_type = "mlx", "large-v3", "gpu", "float16"

        def transcribe(self, pcm):
            model = holder.model          # 同 mlx_whisper：模型在出错那一帧的局部变量里
            raise RuntimeError("[METAL] Command buffer execution failed ({})".format(
                type(model).__name__))

        def release(self):
            asr.release_mlx_model()

    def create(**kw):
        gc.collect()                      # 引用环都回收之后还活着，就是被实打实地引用着
        seen.append((holder.model is None, model_ref() is None))
        return Working(**kw)

    monkeypatch.setattr(asr, "create_transcriber", create)
    monkeypatch.setattr(selfcheck, "_model_cached", lambda model, backend: True)
    broken = MlxLike()
    p._transcriber, p._transcriber_key = broken, ("key",)
    slot = _ASRSlot(broken, config=mlx_config(), key=("key",), audit=p.audit)
    fake_audio(monkeypatch, frames=5)

    stream_once(p, slot)

    assert seen == [(True, True)]          # 加载 CPU 模型那一刻，出错的模型已经不在了
    assert len(server.of("caption")) == 2


def test_a_model_that_failed_to_load_is_gone_before_the_cpu_model_loads(monkeypatch, tmp_path):
    """mlx 预热失败时模型已经进了 ModelHolder，也在异常回溯的帧里。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    holder = fake_mlx_cache(monkeypatch)
    holder.model = None
    refs, seen = [], []

    def create(**kw):
        if kw["backend"] == "mlx":
            model = _Model()              # 预热把模型装进了类级缓存，随后出错
            holder.model = model
            refs.append(weakref.ref(model))
            raise RuntimeError("[METAL] failed during warm-up")
        gc.collect()
        seen.append((holder.model is None, refs[0]() is None))
        return Working(**kw)

    _load_world(monkeypatch, p, {"backend": "mlx", "model": "large-v3", "device": "auto",
                                 "compute_type": "auto", "note": ""}, create)
    monkeypatch.setattr(selfcheck, "_model_cached",
                        lambda model, backend: (model, backend) == ("large-v3-turbo", "ct2"))

    run(p._run_stream_inner(DIRECT_URL))

    assert seen == [(True, True)]


def test_releasing_the_mlx_model_collects_it_before_clearing_the_cache(monkeypatch):
    """模型对象内部有引用环时，只置 None 放不掉；不先回收，mx.clear_cache 什么也清不出来。"""
    holder = fake_mlx_cache(monkeypatch)
    holder.model = _Model()
    holder.model.cycle = holder.model
    model_ref = weakref.ref(holder.model)
    seen = []
    core = ModuleType("mlx.core")
    core.clear_cache = lambda: seen.append(model_ref() is None)
    monkeypatch.setitem(sys.modules, "mlx.core", core)
    enabled = gc.isenabled()
    gc.disable()                          # 只让显式的回收起作用，结果不看自动 GC 的时机
    try:
        assert asr.release_mlx_model() is True
    finally:
        if enabled:
            gc.enable()
    assert seen == [True]


class _CudaModel:
    """CUDA 上能加载、第一次识别才报错的识别器（Windows 缺 cuBLAS 那种）。"""

    def __init__(self, events=None, **kw):
        self.events = events if events is not None else []
        self.backend, self.model_size = "ct2", kw["model_size"]
        self.device, self.compute_type = kw["device"], kw["compute_type"]
        self.model = object()

    def release(self):
        self.events.append("release-cuda")
        self.model = None

    def transcribe(self, pcm):
        if self.model is None:
            raise RuntimeError("识别模型已释放")
        raise RuntimeError("Library cublas64_12.dll is not found")


def _cuda_world(monkeypatch, create):
    """配置是 CUDA large-v3；真的 _stream_session（假音频），每轮按「直连地址播完了」收尾。"""
    import app.hwdetect
    import app.resolver
    monkeypatch.setattr(app.hwdetect, "recommend", lambda backend=None, device=None: {
        "backend": "ct2", "model": "large-v3", "device": "cuda", "compute_type": "float16",
        "note": ""})
    monkeypatch.setattr(asr, "create_transcriber", create)
    monkeypatch.setattr(selfcheck, "_model_cached", lambda model, backend: True)

    async def resolve(url, cookies=None, cookies_browser="auto", trace=None):
        return url

    monkeypatch.setattr(app.resolver, "resolve_stream_url", resolve)
    real = Pipeline._stream_session
    slots = []

    async def once(self_, media, slot, denoise, live_note, loop, sess=None):
        slots.append(slot)
        await real(self_, media, slot, denoise, live_note, loop, sess=sess)
        return True, 60.0

    monkeypatch.setattr(Pipeline, "_stream_session", once)
    return slots


async def _until(cond, spins=2000):
    """让出事件循环直到 cond() 成立；要等线程的步骤再用 10 ms 小睡兜底（最多约 10 秒）。
    只用来等状态出现，不做任何计时断言。"""
    for i in range(spins + 1000):
        if cond():
            return True
        await asyncio.sleep(0 if i < spins else 0.01)
    return cond()


def test_start_after_a_failed_cpu_switch_loads_the_configured_model_again(monkeypatch, tmp_path):
    """以前换 CPU 没载成功，下一场「开始」会从上次的加载结果里拿回已释放的识别器：
    每段都报「识别模型已释放」，配置的模型根本没再跑，还要再丢 3 段才轮到换模型。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    created = []

    def create(**kw):
        created.append(kw["device"])
        if kw["device"] == "cpu":
            raise OSError("model.bin truncated")
        return _CudaModel(**kw)

    _cuda_world(monkeypatch, create)
    fake_audio(monkeypatch, frames=4)

    run(p._run_stream_inner(DIRECT_URL))
    run(p._run_stream_inner(DIRECT_URL))           # 中控再点「开始翻译」，配置没变

    assert created == ["cuda", "cpu", "cuda", "cpu"]
    logs = list((tmp_path / "logs").glob("session-*.jsonl"))
    assert len(logs) == 2
    for log in logs:                                # 两场都真的在跑配置的 CUDA 模型
        assert [r["error"] for r in rows(log, "asr_failed")][:3] == [
            "Library cublas64_12.dll is not found"] * 3


@pytest.mark.parametrize("same_config", [True, False])
def test_stop_during_the_cpu_switch_never_loads_a_model_next_to_it(monkeypatch, tmp_path,
                                                                   same_config):
    """「正在改用 CPU 识别…」时点了停止：线程里的加载取消不掉，会继续跑完。以前下一场「开始」
    拿回已释放的 GPU 识别器，或在它没载完时另载一份。现在先等它载完：配置没变就接着用，
    变了就先放掉再按新配置加载。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    events = []
    started, gate = threading.Event(), threading.Event()

    class CpuModel(Working):
        def release(self):
            events.append("release-cpu")

    def create(**kw):
        events.append("create-" + kw["device"])
        if kw["device"] == "cpu":
            started.set()
            gate.wait(10)
            return CpuModel(**kw)
        return _CudaModel(events, **kw)

    slots = _cuda_world(monkeypatch, create)
    fake_audio(monkeypatch, frames=4)

    async def scenario():
        loop = asyncio.get_running_loop()
        first = asyncio.ensure_future(p._run_stream_inner(DIRECT_URL))
        assert await loop.run_in_executor(None, started.wait, 10)
        assert await _until(lambda: p._fallback_load is not None)
        first.cancel()                                   # 中控点了停止
        with pytest.raises(asyncio.CancelledError):
            await first
        if not same_config:
            p.args.beam = 3                              # 改了识别参数：配置 key 变了
        second = asyncio.ensure_future(p._run_stream_inner(DIRECT_URL))
        waited = await _until(lambda: any("还在加载" in d for _, d in server.statuses))
        gate.set()
        await second
        return waited

    try:
        assert run(scenario())
    finally:
        gate.set()
    assert p._fallback_load is None
    if same_config:
        assert events == ["create-cuda", "release-cuda", "create-cpu"]   # 没有再载任何模型
        assert isinstance(slots[-1].transcriber, CpuModel)
        assert p._transcriber is slots[-1].transcriber
        assert p._asr_fallback["to"] == "ct2/large-v3/cpu/int8"
        assert "asr-fallback" in server.config["incidents"]
        record = rows(_latest_log(tmp_path), "asr_config")[0]
        assert record["device"] == "cpu" and record["fallback"]["from"] == "ct2/large-v3/cuda/float16"
    else:
        assert events[:5] == ["create-cuda", "release-cuda", "create-cpu", "release-cpu",
                              "create-cuda"]                 # 先放掉那份 CPU 模型，再加载


def test_a_config_with_no_fallback_is_not_recorded_as_a_missing_download(monkeypatch, tmp_path):
    """本来就在 CPU 上的配置没有更稳的退路。以前也记成 no_cached_fallback，复查的人会去查下载。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    p.audit = AuditLog(room_url=LIVE_URL, log_dir=tmp_path / "logs")
    asked = []
    monkeypatch.setattr(selfcheck, "_model_cached",
                        lambda model, backend: asked.append(model) or True)
    cfg = mlx_config(backend="ct2", model="large-v3-turbo", device="cpu", compute_type="int8")
    broken = Broken([], backend="ct2", model="large-v3-turbo", device="cpu", compute_type="int8")
    fake_audio(monkeypatch, frames=4)

    stream_once(p, _ASRSlot(broken, config=cfg, key=("key",), audit=p.audit))

    assert asked == []
    assert [r["reason"] for r in rows(p.audit.path, "asr_backend_fallback")] == [
        "no_fallback_for_config"]


# ---- 识别调用卡住：记审计、说清会丢段、不自动重载 ------------------------------------

def _half_pipeline(tmp_path=None):
    p = Pipeline.__new__(Pipeline)
    p.server = StubServer()
    p.telemetry = Telemetry()
    p.audit = AuditLog(room_url=LIVE_URL, log_dir=tmp_path) if tmp_path else None
    return p


def test_a_stalled_call_is_audited_once_and_reannounced_only_on_change(tmp_path):
    p = _half_pipeline(tmp_path)
    p._asr_inflight = [1000.0]
    calm = {"audio_segments_dropped": 0, "audio_backlog_sec": 40.0}

    run(p._check_asr_stall(calm, now=1030.0))
    assert p.server.of("health") == []

    run(p._check_asr_stall(calm, now=1061.0))
    run(p._check_asr_stall(calm, now=1071.0))          # 没新变化：不重复发
    health = p.server.of("health")
    assert len(health) == 1 and health[0]["level"] == "degraded"
    assert "61 秒没有返回" in health[0]["text"]
    assert "「停止/开始」不会重新加载识别模型" in health[0]["text"]
    stalled = rows(p.audit.path, "asr_stalled")
    assert stalled == [dict(stalled[0], inflight_sec=61.0, backlog_sec=40.0, dropped=0)]

    p.telemetry.audio_segments_dropped = 2
    dropping = {"audio_segments_dropped": 2, "audio_backlog_sec": 58.0}
    run(p._check_asr_stall(dropping, now=1081.0))      # 丢段数变了：更新那句话
    assert "已丢弃 2 段" in p.server.of("health")[-1]["text"]
    assert len(rows(p.audit.path, "asr_stalled")) == 1

    p._asr_inflight = None                             # 调用终于返回了
    run(p._check_asr_stall(dropping, now=1091.0))
    last = p.server.of("health")[-1]
    assert last["level"] == "degraded" and "已丢弃 2 段" in last["text"]
    assert "不会漏掉" not in last["text"]
    assert [r["reason"] for r in rows(p.audit.path, "health")] == ["asr_stalled", "backlog"]


def test_the_degraded_banner_no_longer_promises_no_loss():
    p = _half_pipeline()
    run(p._announce_health("degraded", 45.0))
    assert "不会漏掉" not in p.server.of("health")[-1]["text"]
    p.telemetry.audio_segments_dropped = 3
    run(p._announce_health("degraded", 59.0))
    assert "已丢弃 3 段" in p.server.of("health")[-1]["text"]


def test_the_worker_marks_a_call_in_flight_only_while_it_runs(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    seen = []

    class Watching:
        def transcribe(self, pcm):
            seen.append(p._asr_inflight)
            return recognized()

    fake_audio(monkeypatch, frames=2)
    stream_once(p, Watching())
    assert len(seen) == 2 and all(isinstance(m, list) and m[0] > 0 for m in seen)
    assert p._asr_inflight is None


def test_the_stats_loop_runs_the_detection_watch_every_tick():
    p = _half_pipeline()
    calls = []

    async def watch(snap, now=None):
        calls.append(snap)
        if len(calls) == 2:
            raise asyncio.CancelledError

    p._watch_detection = watch

    async def scenario():
        try:
            await p._stats_loop(interval=0)
        except asyncio.CancelledError:
            pass

    run(scenario())
    assert len(calls) == 2 and "audio_segments_dropped" in calls[0]


def test_health_stays_red_while_every_asr_call_fails(monkeypatch, tmp_path):
    """出错的调用返回得快、积压归零，丢段数却在涨。以前下一轮统计就把「程序自动恢复不了」
    刷成「✅ 识别已追上」，审计里还记一条 ok——检测停摆时报恢复。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    p.audit = AuditLog(room_url=LIVE_URL, log_dir=tmp_path / "logs")
    monkeypatch.setattr(selfcheck, "_model_cached", lambda model, backend: False)
    slot = _ASRSlot(Broken([]), config=mlx_config(), key=("key",), audit=p.audit)
    fake_audio(monkeypatch, frames=5)

    async def scenario():
        loop = asyncio.get_running_loop()
        await p._check_asr_stall(p.telemetry.snapshot(), now=0.0)
        await p._stream_session("http://cdn/s.flv", slot, None, "note", loop)
        snap = p.telemetry.snapshot()
        await p._check_asr_stall(snap, now=10.0)              # 下一轮统计：丢段数涨了
        await p._announce_health(p._health_level(0.0), 0.0)   # 统计循环：积压回到 0
        return snap

    snap = run(scenario())

    assert snap["audio_segments_dropped"] == 5 and snap["audio_segments_asr_failed"] == 5
    health = server.of("health")
    assert [m["level"] for m in health] == ["degraded"]
    assert "程序自动恢复不了" in health[0]["text"]
    assert [(r["level"], r["reason"]) for r in rows(p.audit.path, "health")] == [
        ("degraded", "asr_failing")]
    assert "session:asr-failing" in server.config["incidents"]


def test_the_degraded_text_counts_only_audio_the_backlog_pushed_out():
    """「积压超过 60 秒的旧音频已丢弃 N 段」不能把识别出错没检测的段也算进去。"""
    p = _half_pipeline()
    p.telemetry.drop_audio(asr_failed=True)
    p.telemetry.drop_audio(asr_failed=True)
    run(p._announce_health("degraded", 45.0))
    assert "已丢弃" not in p.server.of("health")[-1]["text"]
    p.telemetry.drop_audio()                            # 这一段才是积压挤掉的
    run(p._announce_health("degraded", 59.0))
    assert "已丢弃 1 段" in p.server.of("health")[-1]["text"]


def test_the_stall_check_counts_only_audio_the_backlog_pushed_out(tmp_path):
    """识别出错也记进丢段数。以前卡住检查把它当成「积压丢了段」：每次出错都按积压重报一次，
    卡住时的那句「本场已丢弃 N 段」和 asr_stalled 审计也把出错的段算了进去。"""
    p = _half_pipeline(tmp_path)
    run(p._announce_health("lagging", 25.0))
    run(p._check_asr_stall({"audio_segments_dropped": 0, "audio_backlog_sec": 25.0}, now=0.0))
    failed = {"audio_segments_dropped": 2, "audio_segments_asr_failed": 2,
              "audio_backlog_sec": 31.0}
    run(p._check_asr_stall(failed, now=10.0))           # 两段识别出错（还没到换模型的次数）
    assert len(p.server.of("health")) == 1
    p._asr_inflight = [0.0]
    run(p._check_asr_stall(failed, now=70.0))           # 接着一段卡住了
    assert "本场已丢弃 0 段" in p.server.of("health")[-1]["text"]
    assert rows(p.audit.path, "asr_stalled")[0]["dropped"] == 0


# ---- 模型加载失败：记审计、不猜原因、自检变红、安全时改用 CPU ------------------------

def _load_world(monkeypatch, p, rec, create):
    import app.hwdetect
    import app.resolver
    monkeypatch.setattr(app.hwdetect, "recommend", lambda backend=None, device=None: dict(rec))
    monkeypatch.setattr(asr, "create_transcriber", create)

    async def resolve(url, cookies=None, cookies_browser="auto", trace=None):
        return url

    slots = []

    async def session(media, slot, *a, **k):
        slots.append(slot)
        return True, 60.0

    monkeypatch.setattr(app.resolver, "resolve_stream_url", resolve)
    monkeypatch.setattr(p, "_stream_session", session)
    return slots


def _latest_log(tmp_path):
    """最新的会话文件。同一秒的第二场叫 session-<stamp>-2.jsonl，按文件名排序会排到前面。"""
    def order(f):
        parts = f.stem.split("-")
        return parts[1], parts[2], int(parts[3]) if len(parts) > 3 else 1
    return max((tmp_path / "logs").glob("session-*.jsonl"), key=order)


def test_model_load_failure_is_audited_and_names_no_cause(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    def boom(**kw):
        raise RuntimeError("Unable to open file 'model.bin' "
                           "https://huggingface.co/x/resolve/main/model.bin?token=hf_secret")

    slots = _load_world(monkeypatch, p, {"backend": "ct2", "model": "small", "device": "cpu",
                                         "compute_type": "int8", "note": ""}, boom)
    run(p._run_stream_inner(DIRECT_URL))

    assert slots == []
    state, text = server.statuses[-1]
    assert state == "error"
    assert "识别模型（ct2/small）没能加载" in text and "确认网络和磁盘空间" in text
    assert "请检查网络后" not in text and "下载/加载识别模型失败" not in text
    assert "hf_secret" not in text
    failed = rows(_latest_log(tmp_path), "asr_load_failed")
    assert len(failed) == 1
    assert (failed[0]["backend"], failed[0]["model"], failed[0]["device"]) == ("ct2", "small", "cpu")
    assert "hf_secret" not in failed[0]["error"] and "model.bin" in failed[0]["error"]
    row = run(selfcheck.check_asr(p.args, p._asr_check_state()))
    assert row["level"] == "fail" and "没能加载" in row["detail"] and row["fix"]

    # 之后加载成功：那条失败作废
    monkeypatch.setattr(asr, "create_transcriber", lambda **kw: Working(**kw))
    run(p._run_stream_inner(DIRECT_URL))
    assert p._asr_load_error is None


def test_mlx_load_failure_falls_back_only_to_a_fully_cached_cpu_turbo(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    holder = fake_mlx_cache(monkeypatch)
    calls = []

    def create(**kw):
        calls.append(kw["backend"])
        if kw["backend"] == "mlx":
            raise RuntimeError("[METAL] failed to initialize")
        return Working(**kw)

    rec = {"backend": "mlx", "model": "large-v3", "device": "auto", "compute_type": "auto",
           "note": "Apple Silicon GPU"}
    slots = _load_world(monkeypatch, p, rec, create)
    monkeypatch.setattr(selfcheck, "_model_cached", lambda model, backend: False)
    run(p._run_stream_inner(DIRECT_URL))
    assert calls == ["mlx"] and slots == []            # 没下全：不换，不开始下载
    assert server.statuses[-1][0] == "error"

    calls.clear()
    p._transcriber = None
    monkeypatch.setattr(selfcheck, "_model_cached",
                        lambda model, backend: (model, backend) == ("large-v3-turbo", "ct2"))
    run(p._run_stream_inner(DIRECT_URL))
    assert calls == ["mlx", "ct2"] and holder.model is None
    assert slots[0].transcriber.kw["model_size"] == "large-v3-turbo"
    log = _latest_log(tmp_path)
    assert rows(log, "asr_load_failed")[0]["backend"] == "mlx"
    assert rows(log, "asr_backend_fallback")[0]["to"] == "ct2/large-v3-turbo/cpu/int8"
    config = rows(log, "asr_config")[0]
    assert (config["backend"], config["model"], config["device"]) == \
        ("ct2", "large-v3-turbo", "cpu")
    assert config["requested"]["backend"] == "mlx" and config["fallback"]["from"]
    assert "asr-fallback" in server.config["incidents"]
    row = run(selfcheck.check_asr(p.args, p._asr_check_state()))
    assert row["level"] == "warn" and "已改用" in row["detail"]


def test_asr_config_is_recorded_for_every_session(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    class CudaFellBack(Working):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.device, self.compute_type = "cpu", "int8"   # 构造时 CUDA 退回了 CPU

    rec = {"backend": "ct2", "model": "large-v3", "device": "cuda", "compute_type": "float16",
           "note": "NVIDIA GPU：CUDA 跑 large-v3"}
    _load_world(monkeypatch, p, rec, lambda **kw: CudaFellBack(**kw))
    run(p._run_stream_inner(DIRECT_URL))
    run(p._run_stream_inner(DIRECT_URL))                  # 第二场复用模型，也要有一条
    logs = sorted((tmp_path / "logs").glob("session-*.jsonl"))
    records = [rows(f, "asr_config") for f in logs]
    assert [len(r) for r in records] == [1, 1]
    first = records[0][0]
    assert (first["backend"], first["model"], first["device"], first["compute_type"]) == \
        ("ct2", "large-v3", "cpu", "int8")
    assert first["note"] == rec["note"] and first["requested"]["device"] == "cuda"


def test_check_asr_trusts_the_pipelines_load_result():
    args = SimpleNamespace(backend="auto", model=None, device="auto")
    fail = run(selfcheck.check_asr(args, {"load_error": {
        "backend": "mlx", "model": "large-v3", "device": "auto", "error": "boom"}}))
    assert fail["level"] == "fail" and "boom" in fail["detail"] and fail["fix"]
    warn = run(selfcheck.check_asr(args, {"fallback": {
        "from": "mlx/large-v3", "to": "ct2/large-v3-turbo/cpu/int8", "error": "boom"}}))
    assert warn["level"] == "warn" and "ct2/large-v3-turbo" in warn["detail"]


def _asr_check_world(monkeypatch):
    """check_asr 按配置那条路要用的探测，全换成确定的结果（不碰真硬件、不 import 大库）。"""
    from app import hwdetect
    monkeypatch.setattr(hwdetect, "detect", lambda: {
        "apple_silicon": False, "has_mlx": False, "has_cuda": False, "cores": 10, "ram_gb": 16})
    monkeypatch.setattr(selfcheck, "_model_cached", lambda m, b: True)
    monkeypatch.setattr(selfcheck, "_importable", lambda name: True)


def _asr_row(server):
    return [c["level"] for c in server.config["selfcheck"]["checks"] if c["name"] == "语音识别"]


def test_a_session_selfcheck_does_not_paint_over_a_newer_load_failure(monkeypatch, tmp_path):
    """整轮自检和开播时的模型加载并行跑。以前自检开跑时记下的旧状态，会在它跑完时把
    「没能加载」那一行刷回绿色。"""
    from app import hwdetect
    p = _half_pipeline(tmp_path)
    p.args = SimpleNamespace(backend="auto", model=None, device="auto", comments=False)
    p.detector = p.glossary = p.translator = None
    p._asr_load_error = p._asr_fallback = None
    monkeypatch.setattr(pipeline_mod, "_tiktoklive_version", lambda: None)
    _asr_check_world(monkeypatch)
    monkeypatch.setattr(hwdetect, "recommend", lambda backend="auto", device="auto": {
        "backend": "ct2", "model": "small", "device": "cpu", "compute_type": "int8", "note": ""})

    async def quick(*a, **k):
        return selfcheck._check("x", "ok", "fine")

    for name in ("check_ffmpeg", "check_denoise", "check_translator", "check_watchlist",
                 "check_glossary", "check_audit", "check_resolver", "check_comments"):
        monkeypatch.setattr(selfcheck, name, quick)
    p.server.config["selfcheck"] = {"checks": [selfcheck._check("语音识别", "ok", "startup")],
                                    "summary": {}}

    async def scenario():
        disk_done = asyncio.Event()

        async def slow_disk():
            await disk_done.wait()
            return selfcheck._check("磁盘空间", "ok", "fine")

        monkeypatch.setattr(selfcheck, "check_disk", slow_disk)
        task = asyncio.ensure_future(p.run_selfcheck())     # 这一场的自检开跑
        assert await _until(lambda: hasattr(p, "_selfcheck_tiktoklive"))
        p._asr_load_error = {"backend": "ct2", "model": "small", "device": "cpu",
                             "error": "boom"}               # 同时模型加载失败了
        await p._refresh_asr_check()
        mid = _asr_row(p.server)
        disk_done.set()
        await task                                          # 自检这时才跑完
        return mid, _asr_row(p.server)

    mid, end = run(scenario())
    assert mid == ["fail"] and end == ["fail"]


def test_the_published_asr_row_follows_load_failures_and_recoveries(monkeypatch, tmp_path):
    """开播那轮自检早就发布了：加载失败、之后又加载成功，界面上那一行都要跟着变。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    _asr_check_world(monkeypatch)

    def boom(**kw):
        raise RuntimeError("Unable to open file 'model.bin'")

    _load_world(monkeypatch, p, {"backend": "ct2", "model": "small", "device": "cpu",
                                 "compute_type": "int8", "note": ""}, boom)
    server.config["selfcheck"] = {"checks": [selfcheck._check("语音识别", "ok", "startup"),
                                             selfcheck._check("磁盘空间", "ok", "fine")],
                                  "summary": {}}

    run(p._run_stream_inner(DIRECT_URL))
    assert _asr_row(server) == ["fail"]
    assert server.of("selfcheck")[-1]["summary"]["fail"] == 1

    monkeypatch.setattr(asr, "create_transcriber", lambda **kw: Working(**kw))
    run(p._run_stream_inner(DIRECT_URL))
    assert _asr_row(server) == ["ok"]
    assert [c["name"] for c in server.config["selfcheck"]["checks"]] == ["语音识别", "磁盘空间"]


@pytest.mark.parametrize("form", ["json", "plain"])
def test_cpu_row_reads_the_mlx_giveup_marker_in_both_forms(monkeypatch, tmp_path, form):
    """记号在时启动不会再装 GPU 组件：「重开会自动补装」是假的，要给日期和真能做的事。"""
    from app import hwdetect

    monkeypatch.setattr(hwdetect, "detect", lambda: {
        "apple_silicon": True, "has_mlx": False, "has_cuda": False, "cores": 10, "ram_gb": 16})
    monkeypatch.setattr(hwdetect, "recommend", lambda backend="auto", device="auto": {
        "backend": "ct2", "model": "large-v3-turbo", "device": "cpu", "compute_type": "auto",
        "note": ""})
    monkeypatch.setattr(selfcheck, "_model_cached", lambda m, b: True)
    monkeypatch.setattr(selfcheck, "_importable", lambda name: True)
    from app import bootstrap

    # 记号由 bootstrap 写、也由它读（合并时两组各写了一个读取函数，留了 bootstrap 那份）
    marker = tmp_path / ".venv" / bootstrap.MLX_GIVEUP
    marker.parent.mkdir()
    if form == "json":
        marker.write_text(json.dumps({"at": "2026-09-10T08:30:00", "pip_exit": 1,
                                      "note": "pip failed"}), encoding="utf-8")
        day = "2026-09-10"
    else:
        marker.write_text("装不上，改用 CPU 后端\n", encoding="utf-8")
        stamp = time.mktime((2026, 9, 3, 12, 0, 0, 0, 0, -1))
        os.utime(str(marker), (stamp, stamp))
        day = "2026-09-03"
    monkeypatch.setattr(selfcheck, "ROOT", tmp_path)

    row = run(selfcheck.check_asr(SimpleNamespace(backend="auto", model=None, device="auto")))

    assert row["level"] == "warn" and day in row["fix"]
    assert ("pip 返回 1" in row["fix"]) == (form == "json")
    assert "自动补装" not in row["fix"] and "每天在后台重试" in row["fix"]
    assert "mlx-whisper" in row["fix"]


# ---- 违禁词表体检：只报告，不改匹配 ------------------------------------------------

DEAD_LIST = ("# comentario\n"
             "derretir  # 说明\n"                       # 2 行尾注释
             "re:perdí \\d+ kilos\n"                    # 3 重音
             "re:\\d+% natural\n"                       # 4 标点
             "re:\\$\\s*\\d+\n"                         # 5 标点
             "re:(sin cerrar\n"                         # 6 正则写错
             "re:perd[ií]\\s*\\d+\\s*kilos\n"           # 7 能用
             "re:\\d+%?\\s*natural\n"                   # 8 能用（% 可有可无）
             "re:\\d+\\s*(%|por ciento)\n"              # 9 能用（另一支能匹配）
             "bajar de peso\n")                         # 10 能用


def test_lint_names_entries_that_can_never_match(tmp_path):
    f = tmp_path / "terms.txt"
    f.write_text(DEAD_LIST, encoding="utf-8")
    det = load_detector(f)
    assert {w["line"]: w["reason"] for w in det.load_warnings} == {
        2: "trailing_comment", 3: "accent", 4: "punctuation", 5: "punctuation",
        6: "invalid_regex"}
    # 匹配不变：体检认定的死条目照样加载，能用的照样命中
    assert [t["raw"] for t in det.terms] == ["derretir  # 说明", "bajar de peso"]
    assert [x["raw"] for x in det.patterns] == [
        "re:perdí \\d+ kilos", "re:\\d+% natural", "re:\\$\\s*\\d+",
        "re:perd[ií]\\s*\\d+\\s*kilos", "re:\\d+%?\\s*natural", "re:\\d+\\s*(%|por ciento)"]
    assert det.count == 8 and det.effective_count == 4
    hits = {h["term"] for h in det.scan("perdí 8 kilos con 50% natural y 20 por ciento", ts=1000.0)}
    assert hits == {"re:perd[ií]\\s*\\d+\\s*kilos", "re:\\d+%?\\s*natural",
                    "re:\\d+\\s*(%|por ciento)"}


def test_the_shipped_example_list_is_not_flagged():
    """例表六条正则都用 [eé] 这种写法，是能用的——体检不能把它们报成坏的。"""
    det = load_detector(ROOT / "banned_terms.example.txt")
    assert det.load_warnings == [] and len(det.patterns) == 6


def test_watchlist_row_names_the_lines_and_counts_only_live_entries(tmp_path):
    f = tmp_path / "banned_terms.txt"
    f.write_text(DEAD_LIST, encoding="utf-8")
    row = run(selfcheck.check_watchlist(load_detector(f)))
    assert row["level"] == "warn" and row["fix"]
    assert row["detail"].startswith("4 条已生效")
    assert "第 2 行" in row["detail"] and "第 3 行" in row["detail"]

    f.write_text("re:perdí \\d+ kilos\n", encoding="utf-8")
    assert run(selfcheck.check_watchlist(load_detector(f)))["level"] == "fail"


def test_a_bom_no_longer_kills_a_first_line_regex(tmp_path):
    f = tmp_path / "terms.txt"
    f.write_bytes("re:pas[eé]\\s*de\\s*\\d+\\s*a\\s*\\d+\nbajar de peso\n".encode("utf-8-sig"))
    det = load_detector(f)
    assert [x["raw"] for x in det.patterns] == ["re:pas[eé]\\s*de\\s*\\d+\\s*a\\s*\\d+"]
    assert det.scan("pasé de 97 a 82", ts=1000.0)


def test_a_regex_with_a_trailing_comment_is_told_to_move_the_comment(tmp_path):
    """以前报「删掉 #」——照做之后「 说明」几个字还是必须出现，条目照样是死的。"""
    f = tmp_path / "terms.txt"
    f.write_text("re:perd[ií]\\s*\\d+\\s*kilos  # 说明\n"      # 1 # 之前那段能用
                 "re:perdí \\d+ kilos  # 说明\n"               # 2 # 之前那段自己也是死的
                 "re:precio[ #]\\d+ kilos  # nota\n",          # 3 第一个「 #」在字符类里
                 encoding="utf-8")
    det = load_detector(f)
    found = {w["line"]: (w["reason"], w["text"]) for w in det.load_warnings}
    assert found[1][0] == "trailing_comment" and "单独写一行" in found[1][1]
    assert "删掉这个符号" not in found[1][1]
    assert found[2][0] == "accent" and "[ií]" in found[2][1] and "单独一行" in found[2][1]
    assert found[3][0] == "trailing_comment"
    assert len(det.patterns) == 3                     # 照样加载：体检不改匹配


# ---- 非 UTF-8 词表：不再让程序起不来，读不出的行点名 ------------------------------

def test_a_cp1252_list_keeps_readable_lines_and_names_the_skipped_ones(tmp_path):
    f = tmp_path / "banned_terms.txt"
    f.write_bytes("# lista de términos\nbajar de peso\nadelgazá\nquemagrasas\n".encode("cp1252"))
    det = load_detector(f)                              # 以前：UnicodeDecodeError
    assert det.decode_error and det.skipped_lines == [3]
    assert [t["raw"] for t in det.terms] == ["bajar de peso", "quemagrasas"]
    row = run(selfcheck.check_watchlist(det))
    assert row["level"] == "warn"
    assert "不是 UTF-8" in row["detail"] and "第 3 行" in row["detail"]
    assert "其余 2 条照常生效" in row["detail"]


def test_a_gbk_saved_list_keeps_its_ascii_terms(tmp_path):
    f = tmp_path / "banned_terms.txt"
    f.write_bytes("# 违禁词表——一行一个\nbajar de peso\nre:perd[i]\\s*\\d+\n".encode("gbk"))
    det = load_detector(f)
    assert det.decode_error and det.skipped_lines == [] and det.count == 2
    assert "读不出的只有注释行" in run(selfcheck.check_watchlist(det))["detail"]


def test_load_detector_never_raises(monkeypatch, tmp_path):
    folder = tmp_path / "banned_terms.txt"
    folder.mkdir()                                      # 读不了的「文件」
    det = load_detector(folder)
    assert det.read_error and not det.enabled
    row = run(selfcheck.check_watchlist(det))
    assert row["level"] == "fail" and "读不出来" in row["detail"]

    policy = tmp_path / "policy.txt"
    policy.write_bytes("curar => fuzzy 0\n# comentário\n".encode("cp1252"))
    monkeypatch.setattr(pipeline_mod, "FUZZY_POLICY_FILE", policy)
    ok = tmp_path / "ok.txt"
    ok.write_text("bajar de peso\n", encoding="utf-8")
    assert load_detector(ok).count == 1


def test_the_app_starts_and_records_a_non_utf8_list(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path,
                              terms="bajar de peso\nadelgazá\n".encode("cp1252"))

    async def scenario():
        await p._begin_session(DIRECT_URL)
        path = p.audit.path
        await p._end_session()
        return path

    start = rows(run(scenario()), "session_start")[0]
    assert start["banned_terms_decode_error"]["skipped_lines"] == [2]
    assert start["banned_terms"] == ["bajar de peso"]


# ---- 词表来源进审计；直播中改词表提示一次 ----------------------------------------

def test_session_start_records_the_list_that_was_in_effect(monkeypatch, tmp_path):
    from app.provenance import file_hash

    terms = "# c\nbajar de peso\nre:perd[ií]\\s*\\d+\\s*kilos\nQuemagrasas\n"
    p, server = make_pipeline(monkeypatch, tmp_path, terms=terms)

    async def scenario():
        await p._begin_session(DIRECT_URL)
        path = p.audit.path
        await p._end_session()
        return path

    start = rows(run(scenario()), "session_start")[0]
    assert start["detector_enabled"] is True
    assert start["banned_terms_hash"] == file_hash(tmp_path / "banned_terms.txt") != "?"
    assert start["banned_terms_count"] == 3
    assert start["banned_terms"] == [                  # 原样、按文件顺序，不归一化
        "bajar de peso", "re:perd[ií]\\s*\\d+\\s*kilos", "Quemagrasas"]
    assert start["fuzzy_policy_hash"] == file_hash(pipeline_mod.FUZZY_POLICY_FILE)
    assert start["banned_terms_warnings"] == [] and start["banned_terms_decode_error"] is None
    assert start["tiktoklive_version"] == "7.0.1"      # 原有字段还在


def test_a_mid_live_list_edit_is_announced_once_per_change(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path, terms="bajar de peso\n")
    f = tmp_path / "banned_terms.txt"

    def edit(text, ns):
        f.write_text(text, encoding="utf-8")
        os.utime(str(f), ns=(ns, ns))

    async def scenario():
        await p._begin_session(DIRECT_URL)
        path = p.audit.path
        base = f.stat().st_mtime_ns
        await p._check_terms_changed()                 # 没改：不提示
        edit("bajar de peso\nquemagrasas\n", base + 10 ** 9)
        await p._check_terms_changed()
        await p._check_terms_changed()                 # 同一次修改不重复提示
        edit("bajar de peso\nquemagrasas\nadelgazar\n", base + 2 * 10 ** 9)
        await p._check_terms_changed()
        edit("bajar de peso\n", base + 3 * 10 ** 9)    # 改回本场加载的那份：不提示
        await p._check_terms_changed()
        await p._end_session()
        return path

    path = run(scenario())
    notices = [m for m in server.of("notice") if "违禁词表已修改" in m.get("text", "")]
    assert len(notices) == 2
    changed = rows(path, "terms_changed")
    assert len(changed) == 2 and changed[0]["hash"] != changed[1]["hash"]


# ---- 自检结论进审计 ------------------------------------------------------------

def test_selfcheck_verdicts_reach_the_audit_once_then_only_changes(monkeypatch, tmp_path):
    p = _half_pipeline(tmp_path)
    p.args, p.detector, p.glossary, p.translator = SimpleNamespace(), None, None, None
    monkeypatch.setattr(pipeline_mod, "_tiktoklive_version", lambda: None)

    def verdicts(b_level):
        return [selfcheck._check("A", "ok", "fine"),
                selfcheck._check("B", b_level, "https://x.test/p?sign=secret")]

    script = [verdicts("warn"), verdicts("warn"), verdicts("fail")]

    async def fake_run_all(args, detector=None, glossary=None, translator=None,
                           asr_state=None):
        return script.pop(0)

    monkeypatch.setattr(selfcheck, "run_all", fake_run_all)
    for _ in range(3):
        run(p.run_selfcheck())
    records = rows(p.audit.path, "selfcheck")
    assert len(records) == 2
    assert records[0]["full"] is True and [c["name"] for c in records[0]["checks"]] == ["A", "B"]
    assert records[1]["full"] is False
    assert [(c["name"], c["level"]) for c in records[1]["checks"]] == [("B", "fail")]
    assert "secret" not in json.dumps(records)

    p.audit = AuditLog(room_url=LIVE_URL, log_dir=tmp_path / "next")   # 下一场：整份再写一次
    script.append(verdicts("fail"))
    run(p.run_selfcheck())
    assert [r["full"] for r in rows(p.audit.path, "selfcheck")] == [True]


def test_new_audit_records_never_carry_url_query_strings(tmp_path):
    log = AuditLog(room_url=LIVE_URL, log_dir=tmp_path)
    signed = "https://pull-flv.tiktokcdn.com/stage/a.flv?expire=1&sign=deadbeef"
    log.asr_load_failed("mlx", "large-v3", "auto", "open failed " + signed)
    log.asr_backend_fallback("mlx/large-v3", None, "x " + signed, fallback_error="y " + signed)
    log.close()
    text = Path(log.path).read_text(encoding="utf-8")
    assert "deadbeef" not in text and "expire=" not in text
    assert "pull-flv.tiktokcdn.com/stage/a.flv" in text
