"""待机时提前下载语音识别模型：自检「语音识别」一行的「现在下载」（app/pipeline_asr_fetch.py）。

2026-10-29 新 Mac 上第一次真正开播：新装机第一次点「开始翻译」，要等拿到流地址之后才开始下载 large-v3
（约 3.1 GB），那时直播已经开始了，字幕要等它下完。自检早就知道模型还没下（「首次开播需先下载模型」），
但没写多大，也没有能点的地方。这里钉住：

* 自检那一行只在模型没下全时带 action（和大小），下全了就没有；下载中带 busy；
* asr_plan 是 _run_session 和自检共用的那一套推导，两边推出同一个模型；
* fetch_model 只下文件（mlx 整仓 snapshot_download，ct2 用 faster-whisper 自己的 download_model），不加载；
* 待机时点了才下；连接中 / 直播中 / 正在更新时收到指令不下；已经在下、已经下全时不再起一份；
* 下载还没完时点开始，_run_session 先等这一份下完再加载，不并行起第二份；
* 磁盘不够时不下，说剩多少、要多少；
* 只有本机控制页发得进来：手机同看、浮窗页都发不进来（手机那一半在 tests/test_viewer_hub.py）。

全部用替身：不联网、不下载、不加载模型。主播名是虚构的。
"""
import asyncio
import sys
import threading
import time
from types import ModuleType, SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app import asr, diskspace, i18n, selfcheck
from app import pipeline as pipeline_mod
from app.pipeline import Pipeline
from app.resolver import ResolveError
from app.server import CaptionServer
from tests.helpers import poll_until, run
from tests.test_asr_key import make_pipeline
from tests.test_resilience_stream import scripted_resolve

pytestmark = pytest.mark.usefixtures("v1_reconnect")

UNO = "https://www.tiktok.com/@ficticia_uno/live"
EN = i18n.EN


def en(text):
    return i18n.text(text, EN)


@pytest.fixture
def ct2(monkeypatch):
    """假的 faster_whisper（同 tests/test_asr_key.py 的 ct2 夹具）：WhisperModel 每构造一次记一笔——构造就是
    加载，加载时缓存里没有就是一次下载。recommend 给 ct2 + small。"""
    log = {"built": []}

    class WhisperModel:
        def __init__(self, size, device="cpu", compute_type="int8"):
            log["built"].append((size, device, compute_type))

        def transcribe(self, audio, **kw):
            return [], SimpleNamespace(language="es")

    module = ModuleType("faster_whisper")
    module.WhisperModel = WhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    from app import hwdetect
    monkeypatch.setattr(hwdetect, "recommend", lambda backend=None, device=None: {
        "backend": "ct2", "model": "small", "device": "cpu", "compute_type": "int8", "note": ""})
    return log


async def real_wait(sec):
    """真的等一会儿：整场桩件（tests/test_resilience_stream.make_pipeline）把 asyncio.sleep 换成了 sleep(0)。"""
    await asyncio.get_running_loop().run_in_executor(None, time.sleep, sec)


# ---- 自检那一行 ---------------------------------------------------------------------------------

def _asr_env(monkeypatch, cached, model="large-v3", backend="mlx"):
    from app import hwdetect
    monkeypatch.setattr(hwdetect, "recommend", lambda backend=None, device=None: {
        "backend": backend_name, "model": model, "device": "gpu", "compute_type": "float16",
        "note": "Apple GPU"})
    backend_name = backend
    monkeypatch.setattr(hwdetect, "detect", lambda: {"apple_silicon": backend == "mlx"})
    monkeypatch.setattr(selfcheck, "_importable", lambda name: True)
    monkeypatch.setattr(selfcheck, "_model_cached", lambda m, b: cached)


ARGS = SimpleNamespace(backend="auto", model=None, device="auto")


def test_the_row_offers_a_download_with_its_size_only_while_the_model_is_missing(monkeypatch):
    _asr_env(monkeypatch, cached=False)
    row = run(selfcheck.check_asr(ARGS))
    assert row["level"] == selfcheck.WARN
    assert row["action"] == "download_asr_model"
    assert row["size_mb"] == 3100 and row["busy"] is False
    assert row["detail"] == "mlx + large-v3（模型还没下载，约 3.1 GB）"
    assert en(row["detail"]) == "mlx + large-v3 (model not downloaded yet, about 3.1 GB)"
    assert row["fix"] == "开播前在这里点「现在下载」；不点的话，第一次点「开始翻译」时才下载，字幕要等它下完"
    assert en(row["fix"]) == ("Click Download Now here before you go live. Otherwise it downloads the "
                              "first time you click Start, and captions wait until it finishes.")

    _asr_env(monkeypatch, cached=True)
    row = run(selfcheck.check_asr(ARGS))
    assert row["level"] == selfcheck.OK
    assert "action" not in row and "size_mb" not in row and "busy" not in row
    assert row["detail"] == "mlx + large-v3（模型已下载）"


def test_while_downloading_the_row_says_so_and_drops_the_steps(monkeypatch):
    _asr_env(monkeypatch, cached=False)
    row = run(selfcheck.check_asr(ARGS, {"fetching": True}))
    assert row["action"] == "download_asr_model" and row["busy"] is True
    assert row["detail"] == "mlx + large-v3（正在下载模型，约 3.1 GB）"
    assert en(row["detail"]) == "mlx + large-v3 (model downloading, about 3.1 GB)"
    assert row["fix"] == ""


def test_a_model_of_unknown_size_still_gets_the_button_without_a_size(monkeypatch):
    _asr_env(monkeypatch, cached=False, model="ficticio/whisper-propio")
    row = run(selfcheck.check_asr(ARGS))
    assert row["action"] == "download_asr_model" and row["size_mb"] is None
    assert row["detail"] == "mlx + ficticio/whisper-propio（模型还没下载）"
    assert en(row["detail"]) == "mlx + ficticio/whisper-propio (model not downloaded yet)"


def test_the_cpu_on_apple_silicon_warning_keeps_its_own_steps_and_no_button(monkeypatch):
    """Apple 芯片却在用 CPU 时那一行要的是装 GPU 组件；这时下 ct2 的模型，装好之后用不上。"""
    from app import bootstrap, hwdetect
    _asr_env(monkeypatch, cached=False, model="large-v3-turbo", backend="ct2")
    monkeypatch.setattr(hwdetect, "detect", lambda: {"apple_silicon": True})
    monkeypatch.setattr(bootstrap, "mlx_giveup_note", lambda root: None)
    row = run(selfcheck.check_asr(ARGS))
    assert "action" not in row
    assert "ct2 + large-v3-turbo（模型还没下载，约 1.6 GB）" in row["detail"]


# ---- 两边共用的推导 ---------------------------------------------------------------------------

def test_asr_plan_is_what_both_the_session_and_the_check_load(monkeypatch):
    from app import hwdetect
    seen = []

    def recommend(backend=None, device=None):
        seen.append((backend, device))
        return {"backend": "ct2", "model": "large-v3-turbo", "device": "cpu", "compute_type": "int8",
                "note": "CPU"}

    monkeypatch.setattr(hwdetect, "recommend", recommend)
    args = SimpleNamespace(backend="ct2", model=None, device="cpu", compute_type="auto")
    assert asr.asr_plan(args) == {"backend": "ct2", "model": "large-v3-turbo", "device": "cpu",
                                  "compute": "int8", "note": "CPU"}
    args.model, args.compute_type = "small", "float32"          # 用户指定的照用
    assert asr.asr_plan(args) == {"backend": "ct2", "model": "small", "device": "cpu",
                                  "compute": "float32", "note": "CPU"}
    assert seen == [("ct2", "cpu"), ("ct2", "cpu")]
    monkeypatch.setattr(selfcheck, "_importable", lambda name: True)
    monkeypatch.setattr(selfcheck, "_model_cached", lambda m, b: True)
    monkeypatch.setattr(hwdetect, "detect", lambda: {})
    assert run(selfcheck.check_asr(args))["detail"] == "ct2 + small（模型已下载）"


# ---- 只下文件，不加载 -------------------------------------------------------------------------

def test_fetch_model_downloads_the_same_files_loading_would_and_loads_nothing(monkeypatch):
    calls = []
    hub = ModuleType("huggingface_hub")
    hub.snapshot_download = lambda repo_id: calls.append(("snapshot", repo_id)) or "/cache/mlx"
    utils = ModuleType("faster_whisper.utils")
    utils.download_model = lambda model: calls.append(("ct2", model)) or "/cache/ct2"
    fw = ModuleType("faster_whisper")
    fw.utils = utils

    def no_model(*a, **k):
        raise AssertionError("不该加载模型")

    fw.WhisperModel = no_model
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    monkeypatch.setitem(sys.modules, "faster_whisper", fw)
    monkeypatch.setitem(sys.modules, "faster_whisper.utils", utils)
    assert asr.fetch_model("mlx", "large-v3") == "/cache/mlx"
    assert asr.fetch_model("ct2", "large-v3-turbo") == "/cache/ct2"
    monkeypatch.setitem(sys.modules, "mlx_whisper", None)          # auto：没有 mlx_whisper 就是 ct2
    asr.fetch_model("auto", "small")
    assert calls == [("snapshot", "mlx-community/whisper-large-v3-mlx"), ("ct2", "large-v3-turbo"),
                     ("ct2", "small")]


def test_fetch_model_leaves_a_local_model_folder_alone(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "huggingface_hub", None)       # 一碰就 ImportError
    assert asr.fetch_model("mlx", str(tmp_path)) == str(tmp_path)


# ---- 管线：什么时候下、什么时候不下 -------------------------------------------------------------

def _idle_pipeline(monkeypatch, tmp_path, ct2_log, cached=False):
    p, server = make_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(selfcheck, "_model_cached", lambda m, b: cached)
    monkeypatch.setattr(diskspace, "free_bytes", lambda path: 500 * 10 ** 9)
    fetched = []
    gate = threading.Event()
    gate.set()

    def fake_fetch(backend, model):
        fetched.append((backend, model))
        assert gate.wait(10)
        return "/cache"

    monkeypatch.setattr(asr, "fetch_model", fake_fetch)
    refreshed = []

    async def refresh():
        refreshed.append(p._asr_check_state().get("fetching"))

    monkeypatch.setattr(p, "_refresh_asr_check", refresh)
    return p, server, fetched, gate, refreshed


def statuses(server):
    return [m for m in server.messages if m.get("type") == "status"]


def notices(server):
    return [m["text"] for m in server.messages if m.get("type") == "notice"]


def test_idle_click_downloads_the_planned_model_once_and_refreshes_the_row(monkeypatch, tmp_path, ct2):
    p, server, fetched, gate, refreshed = _idle_pipeline(monkeypatch, tmp_path, ct2)

    async def scenario():
        assert p.handle_control({"type": "download_asr_model"}) is None
        await poll_until(lambda: not p._asr_fetching() and fetched, 5, "下载结束")
        await poll_until(lambda: len(refreshed) >= 2, 5, "下完刷新那一行")

    run(scenario())
    assert fetched == [("ct2", "small")]                 # ct2 夹具：recommend 给的是 ct2 + small
    assert refreshed[0] is True and refreshed[-1] is False   # 开始时那一行转成「正在下载」，下完刷回来
    assert ct2["built"] == []                             # 只下文件，不加载
    idle = [m["detail"] for m in statuses(server) if m["state"] == "idle"]
    assert idle[0] == "正在下载语音识别模型 small（约 484 MB，只需这一次）…"
    assert en(idle[0]) == "Downloading speech model small (about 484 MB, only needed once)…"
    assert idle[-1] == "语音识别模型已下载，可以开始了。"
    assert en(idle[-1]) == "The speech model is downloaded. You can start now."


@pytest.mark.parametrize("busy", ["stream", "update"])
def test_a_click_while_connecting_live_or_updating_downloads_nothing(monkeypatch, tmp_path, ct2, busy,
                                                                     capsys):
    p, server, fetched, gate, refreshed = _idle_pipeline(monkeypatch, tmp_path, ct2)
    if busy == "stream":
        monkeypatch.setattr(p, "_stream_active", lambda: True)
    else:
        monkeypatch.setattr(p, "_update_in_progress", lambda: True)

    async def scenario():
        assert p.handle_control({"type": "download_asr_model"}) is None
        await real_wait(0.05)

    run(scenario())
    assert fetched == []
    assert statuses(server) == []
    assert "这次不下载识别模型" in capsys.readouterr().out


def test_a_second_click_while_downloading_starts_nothing_new(monkeypatch, tmp_path, ct2):
    p, server, fetched, gate, refreshed = _idle_pipeline(monkeypatch, tmp_path, ct2)
    gate.clear()

    async def scenario():
        p.handle_control({"type": "download_asr_model"})
        await poll_until(lambda: fetched, 5, "开始下载")
        p.handle_control({"type": "download_asr_model"})
        p.handle_control({"type": "download_asr_model"})
        await real_wait(0.05)
        gate.set()
        await poll_until(lambda: not p._asr_fetching(), 5, "下载结束")

    run(scenario())
    assert fetched == [("ct2", "small")]


def test_two_clicks_in_the_same_tick_still_start_one_download(monkeypatch, tmp_path, ct2):
    """第一下起的任务还没跑到线程那一步，第二下就进来了：也只起一份。"""
    p, server, fetched, gate, refreshed = _idle_pipeline(monkeypatch, tmp_path, ct2)

    async def scenario():
        p.handle_control({"type": "download_asr_model"})
        p.handle_control({"type": "download_asr_model"})
        await poll_until(lambda: fetched and not p._asr_fetching(), 5, "下载结束")
        await real_wait(0.05)

    run(scenario())
    assert fetched == [("ct2", "small")]


def test_an_already_downloaded_model_is_not_fetched_again(monkeypatch, tmp_path, ct2):
    p, server, fetched, gate, refreshed = _idle_pipeline(monkeypatch, tmp_path, ct2, cached=True)

    async def scenario():
        p.handle_control({"type": "download_asr_model"})
        await poll_until(lambda: refreshed, 5, "刷新那一行")

    run(scenario())
    assert fetched == []


def test_not_enough_storage_downloads_nothing_and_says_how_much_is_needed(monkeypatch, tmp_path, ct2):
    p, server, fetched, gate, refreshed = _idle_pipeline(monkeypatch, tmp_path, ct2)
    monkeypatch.setattr(diskspace, "free_bytes", lambda path: 800 * 10 ** 6)     # 0.8 GB

    async def scenario():
        p.handle_control({"type": "download_asr_model"})
        await poll_until(lambda: not p._asr_fetching() and statuses(server), 5, "说明")

    run(scenario())
    assert fetched == []
    (msg,) = statuses(server)
    assert msg["detail"] == ("磁盘剩余 0.8 GB，语音识别模型 small 约 0.5 GB，下载时还要多留 1 GB，这次没有下载。"
                             "腾出空间后再点「现在下载」。")
    assert en(msg["detail"]) == ("0.8 GB of storage is available. Speech model small needs about 0.5 GB, "
                                 "plus 1 GB to spare while downloading, so it wasn’t downloaded. Free up "
                                 "some space, then click Download Now again.")


def test_a_failed_download_says_what_happened_and_what_to_do(monkeypatch, tmp_path, ct2, capsys):
    p, server, fetched, gate, refreshed = _idle_pipeline(monkeypatch, tmp_path, ct2)

    def broken(backend, model):
        fetched.append((backend, model))
        raise OSError("Connection reset by peer")

    monkeypatch.setattr(asr, "fetch_model", broken)

    async def scenario():
        p.handle_control({"type": "download_asr_model"})
        await poll_until(lambda: len(refreshed) >= 2, 5, "下载失败后刷新那一行")

    run(scenario())
    last = [m["detail"] for m in statuses(server)][-1]
    assert last == ("语音识别模型没能下载完（Connection reset by peer）。可以再点一次「现在下载」；不点的话，"
                    "第一次点「开始翻译」时才下载。")
    assert en(last) == ("Couldn’t finish downloading the speech model. Click Download Now to try again. "
                        "Otherwise it downloads the first time you click Start. Details: Connection reset "
                        "by peer")
    assert "[警告] 识别模型没能提前下载完：Connection reset by peer" in capsys.readouterr().out


def test_starting_while_the_download_runs_waits_for_it_instead_of_starting_another(
        monkeypatch, tmp_path, ct2):
    """下载还没完就点了开始：_run_session 先等这一份下完，再加载（加载时命中缓存，不再下一遍）。"""
    p, server, fetched, gate, refreshed = _idle_pipeline(monkeypatch, tmp_path, ct2)
    gate.clear()
    scripted_resolve(monkeypatch, ["http://cdn/s.flv", ResolveError("没开播", kind="offline", status=4)])

    async def session(media, slot, *a, **k):
        return True, 60.0

    monkeypatch.setattr(p, "_stream_session", session)

    async def scenario():
        p.handle_control({"type": "download_asr_model"})
        await poll_until(lambda: fetched, 5, "开始下载")
        run_task = asyncio.ensure_future(p._run_stream_inner(UNO))
        await poll_until(lambda: any("还在下载" in str(m["detail"]) for m in statuses(server)), 5,
                         "开播时说在等下载")
        await real_wait(0.2)
        assert ct2["built"] == []                 # 下载没完：不加载（加载就是第二份下载）
        gate.set()
        await asyncio.wait_for(run_task, 10)

    run(scenario())
    assert fetched == [("ct2", "small")]
    assert len(ct2["built"]) == 1
    waiting = next(m for m in statuses(server) if "还在下载" in str(m["detail"]))
    assert waiting["state"] == "connecting"
    assert waiting["detail"] == "语音识别模型 small 还在下载，等它下完再开始识别…"
    assert en(waiting["detail"]) == ("Speech model small is still downloading. Recognition starts when it "
                                     "finishes…")


def test_progress_goes_to_the_idle_status_and_stays_quiet_once_a_session_starts(monkeypatch, tmp_path, ct2):
    """进度复用 _model_download_progress 的轮询：待机时走 _provision_note（status idle）；开播之后由开播那边的
    进度接手，这边不再发（不然每 3 秒一条 notice）。"""
    p, server, fetched, gate, refreshed = _idle_pipeline(monkeypatch, tmp_path, ct2)
    gate.clear()
    monkeypatch.setitem(pipeline_mod.MODEL_SIZES_MB, "small", 60)
    monkeypatch.setattr(Pipeline, "MODEL_DOWNLOAD_POLL_SEC", 0.02)
    hub = tmp_path / "hub"
    hub.mkdir(parents=True, exist_ok=True)
    active = {"on": False}
    monkeypatch.setattr(p, "_stream_active", lambda: active["on"])

    async def scenario():
        p.handle_control({"type": "download_asr_model"})
        await poll_until(lambda: fetched, 5, "开始下载")
        await real_wait(0.2)
        with open(hub / "blob1", "wb") as fh:
            fh.truncate(30_000_000)
        await poll_until(lambda: any("%" in str(m["detail"]) for m in statuses(server)), 5, "进度")
        active["on"] = True
        before = len(server.messages)
        with open(hub / "blob2", "wb") as fh:
            fh.truncate(20_000_000)
        await real_wait(0.3)
        quiet = server.messages[before:]
        gate.set()
        await poll_until(lambda: not p._asr_fetching(), 5, "下载结束")
        return quiet

    quiet = run(scenario())
    progress = next(m for m in statuses(server) if "%" in str(m["detail"]))
    assert progress["state"] == "idle"
    assert progress["detail"] == "正在下载识别模型 small：50%（30 / 60 MB，仅首次需要，请保持窗口打开）…"
    assert "progress" not in progress                    # 页面中间那张「正在连接」卡片只认开播时的
    assert quiet == []


# ---- 只有本机控制页发得进来 ---------------------------------------------------------------------

def test_only_the_control_page_can_send_download_asr_model():
    server = CaptionServer(port=0)
    got = []
    server.on_control = lambda msg: got.append(msg)

    async def body():
        app = web.Application()
        app.router.add_get("/ws", server._ws)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            float_ws = await client.ws_connect("/ws?role=float")
            await float_ws.receive_json()
            await float_ws.send_json({"type": "download_asr_model"})
            await asyncio.sleep(0.1)
            assert got == []
            control = await client.ws_connect("/ws")
            await control.receive_json()
            await control.send_json({"type": "download_asr_model"})
            await asyncio.sleep(0.1)
            await control.close()
            await float_ws.close()
        finally:
            await client.close()

    run(body())
    assert got == [{"type": "download_asr_model"}]
