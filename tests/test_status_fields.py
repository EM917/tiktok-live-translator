"""status 消息的两个结构化字段（后端这一半；页面那一半在别的批次）。

* progress（专家审查 U7）：首次下载识别模型时，进度只在顶部横幅的 detail 文字里，页面中间一直写「正在连接
  @x…」。connecting 状态下 detail 几乎从不为空（计划审查 Q2），页面不能「看到 detail 就显示」，所以模型下载
  那一处另带 progress={kind: model_download, pct}，随 config["status"] 留存、hello 时重放；别的 connecting 不带。
* paths（专家审查 I8）：「完全磁盘访问权限」那一步要加的解释器路径。不靠「以 / 开头、不含空格」这种文本规则
  认（计划审查 Q14：conda、用户目录的路径可能带空格），也不复用 command（command 是能原样照做的命令）。

没有这两个字段时，status 消息和 config["status"] 逐字节和以前一样。
"""
import asyncio
import json
from types import SimpleNamespace

from app import pipeline as pipeline_mod
from app.pipeline import Pipeline
from app.server import CaptionServer

ROOM = "https://www.tiktok.com/@ficticia_estado/live"


class RecordingServer(CaptionServer):
    def __init__(self):
        super().__init__(port=0)
        self.messages = []

    async def broadcast(self, msg):
        self.messages.append(dict(msg))
        await super().broadcast(msg)


def statuses(server):
    return [m for m in server.messages if m.get("type") == "status"]


# ---- server.status ----

def test_a_plain_status_is_unchanged():
    server = RecordingServer()
    asyncio.run(server.status("connecting", "connecting"))
    (msg,) = statuses(server)
    assert set(msg) == {"type", "state", "detail", "ts"}
    assert server.config["status"] == {"state": "connecting", "detail": "connecting", "command": None}
    assert json.dumps(server.config["status"], ensure_ascii=False) == \
        '{"state": "connecting", "detail": "connecting", "command": null}'


def test_progress_rides_along_and_stays_in_config():
    server = RecordingServer()
    progress = {"kind": "model_download", "pct": 37, "mb": 580}
    asyncio.run(server.status("connecting", "downloading", progress=progress))
    assert statuses(server)[-1]["progress"] == progress
    assert server.config["status"] == {"state": "connecting", "detail": "downloading", "command": None,
                                       "progress": progress}


def test_the_phone_never_gets_the_progress():
    """手机同看的白名单只转发 status 的 state 和 ts。"""
    from app.viewer import filter_payload

    out = filter_payload({"type": "status", "state": "connecting", "detail": "x", "ts": 1.0,
                          "progress": {"kind": "model_download", "pct": 3}})
    assert "progress" not in out


def test_the_next_status_without_them_clears_them():
    server = RecordingServer()
    asyncio.run(server.status("connecting", "downloading", progress={"kind": "model_download", "pct": 99}))
    asyncio.run(server.status("live", "live"))
    assert "progress" not in server.config["status"]


def test_hello_replays_the_progress():
    """新连进来的页面从 hello 的 config 里拿到此刻的进度（页面刷新、晚打开）。"""
    from app import i18n

    server = RecordingServer()
    asyncio.run(server.status("connecting", "downloading", progress={"kind": "model_download", "pct": 12}))
    hello = i18n.render(server._float_config(server.config))      # 浮窗页拿的那份也带着 status
    assert hello["status"]["progress"] == {"kind": "model_download", "pct": 12}


# ---- pipeline：只有模型下载那一处带 progress ----

def _pipeline(monkeypatch, tmp_path, server):
    from app import settings
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    terms = tmp_path / "banned_terms.txt"
    terms.write_text("", encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms)
    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source="es", source_requested="es",
        beam=5, context=False, asr_temperature=None, glossary=None, backend="auto", model=None,
        device="auto", compute_type="auto", denoise="off", banned_terms=None, comments=False,
        cookies_browser="none")
    return Pipeline(args, server)


def test_the_model_download_status_carries_progress(monkeypatch, tmp_path):
    """真的 _model_download_progress：HF 缓存目录里长出一个文件（稀疏文件，不占磁盘），状态带上进度。"""
    server = RecordingServer()
    p = _pipeline(monkeypatch, tmp_path, server)
    hub = tmp_path / "hf" / "hub"
    hub.mkdir(parents=True)
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.setitem(pipeline_mod.MODEL_SIZES_MB, "ficticio-test", 60)
    monkeypatch.setattr(Pipeline, "MODEL_DOWNLOAD_POLL_SEC", 0.02)

    async def scenario():
        task = asyncio.ensure_future(p._model_download_progress("ficticio-test"))
        await asyncio.sleep(0.2)                     # 先让它量完起点
        with open(hub / "blob", "wb") as fh:
            fh.truncate(30_000_000)                  # 30 MB
        for _ in range(200):
            if statuses(server):
                break
            await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    got = statuses(server)
    assert got, "没有发出进度"
    assert got[0]["state"] == "connecting"
    assert got[0]["progress"] == {"kind": "model_download", "pct": 50, "mb": 30}
    assert server.config["status"]["progress"] == {"kind": "model_download", "pct": 50, "mb": 30}


def test_a_model_of_unknown_size_reports_megabytes_without_a_percentage(monkeypatch, tmp_path):
    server = RecordingServer()
    p = _pipeline(monkeypatch, tmp_path, server)
    hub = tmp_path / "hf" / "hub"
    hub.mkdir(parents=True)
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.setattr(Pipeline, "MODEL_DOWNLOAD_POLL_SEC", 0.02)

    async def scenario():
        task = asyncio.ensure_future(p._model_download_progress("ficticio-sin-talla"))
        await asyncio.sleep(0.2)
        with open(hub / "blob", "wb") as fh:
            fh.truncate(25_000_000)
        for _ in range(200):
            if statuses(server):
                break
            await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert statuses(server)[0]["progress"] == {"kind": "model_download", "pct": None, "mb": 25}


def test_other_connecting_statuses_carry_no_progress(monkeypatch, tmp_path):
    server = RecordingServer()
    p = _pipeline(monkeypatch, tmp_path, server)

    async def scenario():
        await p.server.status("connecting", "resolving")
        server.config["status"]["state"] = "connecting"
        await p._resend_status()

    asyncio.run(scenario())
    assert all("progress" not in m for m in statuses(server))


def test_a_resent_status_keeps_its_progress(monkeypatch, tmp_path):
    """重发的 start 只要一个回执（_resend_status）：原样再发时进度不能丢。"""
    server = RecordingServer()
    p = _pipeline(monkeypatch, tmp_path, server)
    progress = {"kind": "model_download", "pct": 40, "mb": 640}

    async def scenario():
        await server.status("connecting", "downloading", progress=progress)
        await p._resend_status()

    asyncio.run(scenario())
    assert [m.get("progress") for m in statuses(server)] == [progress, progress]


# ==== paths（专家审查 I8）====

import pytest  # noqa: E402

from app import browser_login as bl  # noqa: E402
from app import resolver  # noqa: E402


def test_paths_ride_along_and_stay_in_config():
    server = RecordingServer()
    progress = {"kind": "model_download", "pct": 37, "mb": 580}
    asyncio.run(server.status("connecting", "downloading", progress=progress))
    assert statuses(server)[-1]["progress"] == progress
    assert server.config["status"]["progress"] == progress
    asyncio.run(server.status("error", "blocked", paths=["/opt/conda envs/bin/python3.13"]))
    assert statuses(server)[-1]["paths"] == ["/opt/conda envs/bin/python3.13"]
    assert server.config["status"] == {"state": "error", "detail": "blocked", "command": None,
                                       "paths": ["/opt/conda envs/bin/python3.13"]}   # 进度跟着换掉了


def test_the_phone_never_gets_the_paths():
    """手机同看的白名单只转发 status 的 state 和 ts：本机路径不出这台电脑。"""
    from app.viewer import filter_payload

    out = filter_payload({"type": "status", "state": "error", "detail": "x", "ts": 1.0,
                          "paths": ["/Users/someone/python"], "progress": {"kind": "model_download"}})
    assert "paths" not in out and "progress" not in out


def test_a_resent_status_keeps_its_paths(monkeypatch, tmp_path):
    server = RecordingServer()
    p = _pipeline(monkeypatch, tmp_path, server)
    paths = ["/opt/conda envs/bin/python3.13"]

    async def scenario():
        await server.status("connecting", "x", paths=paths)
        await p._resend_status()

    asyncio.run(scenario())
    assert [m.get("paths") for m in statuses(server)] == [paths, paths]


# ---- pipeline：系统拒绝读取时带上 paths ----

def _resolve_fails(monkeypatch, login, kind="browser_only"):
    async def fail(url, cookies=None, cookies_browser="auto", trace=None):
        raise resolver.ResolveError("4003110", kind=kind, login=login)
    monkeypatch.setattr(resolver, "resolve_stream_url", fail)


@pytest.mark.parametrize("login, kind, wants", [
    ({"chrome": bl.BLOCKED, "safari": bl.BLOCKED}, "browser_only", True),
    ({"safari": bl.BLOCKED, "chrome": bl.NOT_READ}, "browser_only", True),
    ({"safari": bl.OK, "chrome": bl.BLOCKED}, "browser_only", False),     # 借到了：话里没有那一步
    ({"safari": bl.NOT_LOGGED_IN}, "browser_only", False),
    ({"safari": bl.BLOCKED}, "unknown", False),                          # 别的 kind 的话里没有那一步
    (None, "browser_only", False),
])
def test_the_error_status_carries_the_fda_paths_only_with_that_step(monkeypatch, tmp_path, login, kind,
                                                                    wants):
    server = RecordingServer()
    p = _pipeline(monkeypatch, tmp_path, server)
    p.BROWSER_ONLY_RETRY_SEC = 0.0
    targets = ["/opt/conda envs/bin/python3.13", "/Users/someone/.venv/bin/python"]
    monkeypatch.setattr(bl, "fda_targets", lambda environ=None, executable=None: list(targets))
    _resolve_fails(monkeypatch, login, kind)
    sess = p._new_session_state(None)
    p._session_state = sess

    async def scenario():
        await p._run_session(ROOM)

    asyncio.run(scenario())
    (err,) = [m for m in statuses(server) if m["state"] == "error"]
    if wants:
        assert err["paths"] == targets                   # 带空格的路径整条一项，不靠文字切
        assert server.config["status"]["paths"] == targets
        assert all(t in err["detail"] for t in targets)  # 和文字里列的是同一份
    else:
        assert "paths" not in err and "paths" not in server.config["status"]
