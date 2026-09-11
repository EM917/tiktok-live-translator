"""选了本地引擎、Ollama 装了没跑：程序要自己把它起起来，并把自检刷绿。
实录（2026-09-10）：brew 装的 Ollama + 引擎选 hymt2，自检一直红着「Ollama 没在运行」，
每句翻译 0.8 毫秒失败——两处原因：显式引擎被当成「别自作主张」直接跳过；
双击 .app 启动时 PATH 里没有 /opt/homebrew/bin，which 找不到 ollama。"""
import asyncio
import sys
from types import SimpleNamespace

import pytest

from app import localmodel, pipeline as pipeline_mod, translator
from app.pipeline import Pipeline


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


@pytest.fixture
def world(monkeypatch, tmp_path):
    from app import settings
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    args = SimpleNamespace(cookies=None, target="zh-CN", translator="none", source=None,
                           beam=5, context=False, asr_temperature=None, glossary=None,
                           backend="auto", model=None, device="auto", compute_type="auto",
                           denoise="off")
    p = Pipeline(args, StubServer())
    calls = {"start": 0, "pull": [], "selfcheck": 0, "create": [], "publish": 0}
    state = {"running": False, "installed": True, "has_small": True, "has_large": False,
             "has_gemma": False}

    async def is_running(timeout=2):
        return state["running"]

    async def start(timeout=25):
        calls["start"] += 1
        state["running"] = True
        return True

    async def pull(model, on_progress=None):
        calls["pull"].append(model)
        return True

    async def run_selfcheck():
        calls["selfcheck"] += 1

    async def publish_engine():
        calls["publish"] += 1

    monkeypatch.setattr(localmodel, "is_running", is_running)
    monkeypatch.setattr(localmodel, "is_installed", lambda: state["installed"])
    monkeypatch.setattr(localmodel, "start", start)
    monkeypatch.setattr(localmodel, "pull", pull)
    monkeypatch.setattr(translator, "_ollama_has_hymt2",
                        lambda large=False: state["has_large"] if large else state["has_small"])
    monkeypatch.setattr(translator, "_ollama_has_gemma", lambda: state["has_gemma"])
    monkeypatch.setattr(pipeline_mod, "create_translator",
                        lambda name: calls["create"].append(name) or object())
    monkeypatch.setattr(p, "run_selfcheck", run_selfcheck)
    monkeypatch.setattr(p, "_publish_engine", publish_engine)
    return p, calls, state


def test_explicit_local_engine_starts_ollama_and_rechecks(world):
    p, calls, state = world
    p.args.translator = "hymt2"
    before = p.translator
    run(p.ensure_local_translator())
    assert calls["start"] == 1                 # 以前这里直接 return，Ollama 永远起不来
    assert calls["pull"] == []                 # 模型已经有了
    assert calls["selfcheck"] == 1             # 红条要刷掉
    assert p.translator is before              # 显式引擎的对象不动，Ollama 起来就能用


def test_auto_engine_switches_off_google_once_ollama_is_up(world):
    p, calls, state = world
    p.args.translator = "auto"
    run(p.ensure_local_translator())
    assert calls["start"] == 1
    assert calls["create"] == ["auto"]          # 启动时落到 Google 的要换回本地
    assert calls["publish"] == 1 and calls["selfcheck"] == 1


def test_explicit_engine_pulls_the_model_it_needs(world):
    p, calls, state = world
    p.args.translator = "hymt2-7b"
    state["running"] = True
    run(p.ensure_local_translator())
    assert calls["start"] == 0
    assert calls["pull"] == [translator.HYMT2_LARGE]
    assert calls["create"] == []               # 显式引擎不重建
    assert calls["selfcheck"] == 1


def test_cloud_engines_never_touch_ollama(world):
    p, calls, state = world
    p.args.translator = "deepl"
    run(p.ensure_local_translator())
    assert calls["start"] == 0 and calls["pull"] == [] and calls["selfcheck"] == 0


def test_nothing_to_do_when_ollama_is_up_and_the_model_is_there(world):
    p, calls, state = world
    p.args.translator = "hymt2"
    state["running"] = True
    run(p.ensure_local_translator())
    assert calls["start"] == 0 and calls["pull"] == [] and calls["selfcheck"] == 0


def test_provisioning_waits_for_the_startup_selfcheck_before_rechecking(world):
    """启动自检和备模型并行跑；重查必须排在它后面，否则先出的绿结果会被后到的红结果盖掉。"""
    p, calls, state = world
    p.args.translator = "hymt2"
    order = []

    async def slow_startup_check():
        await asyncio.sleep(0.05)
        order.append("startup")

    async def recheck():
        order.append("recheck")

    async def scenario():
        p._selfcheck_task = asyncio.ensure_future(slow_startup_check())
        p.run_selfcheck = recheck
        await p.ensure_local_translator()

    run(scenario())
    assert order == ["startup", "recheck"]


def test_find_binary_looks_past_a_gui_launch_path(tmp_path, monkeypatch):
    """双击 .app 启动时 PATH 只有 /usr/bin:/bin，brew 装的 ollama 不在里面。"""
    import shutil as _sh
    monkeypatch.setattr(_sh, "which", lambda name: None)
    monkeypatch.setattr(sys, "platform", "darwin")
    brew = tmp_path / "opt" / "homebrew" / "bin"
    brew.mkdir(parents=True)
    (brew / "ollama").write_text("x")
    monkeypatch.setattr(localmodel, "_KNOWN_BIN_DIRS", {"darwin": (str(brew),)})
    monkeypatch.setattr(localmodel, "_MAC_APP_DIRS", ())
    assert localmodel.find_binary() == str(brew / "ollama")
    assert localmodel.is_installed() is True
