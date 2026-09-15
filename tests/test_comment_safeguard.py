"""弹幕连接的保险（2026-09-14 事故之后加的四层）。

那次：Euler 签名服务把连接派到备用推流线路，TikTokLive 7.0.0 拼错握手地址，
每次 HTTP 400；修好它的 7.0.1 四天前就发布了，而程序只在「没装」时才安装、从不升级。
面板上是英文原始报错，后台隔一分钟重试一次，会话日志里一条弹幕状态都没有。

这里钉住：
  1. 版本兜底：低于 7.0.1 就升，失败也不把弹幕判成不可用；
  2. 被拒时找更新：握手被拒（沿异常链认状态码）单独归类，先找补丁版本，
     升级了立刻重连，没有就长等；
  3. 中文说明：原始英文报错不直接上面板，文案不带规则八禁用的原因标签；
  4. 留证据：弹幕状态变化与组件版本写进会话日志，自检多一行「观众弹幕」。

不 import 其它测试文件；不 import TikTokLive（CI 没装），异常类用同构的假模块。
"""
import asyncio
import json
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from app import audit as audit_mod
from app import comment_source as cs_mod
from app import pipeline as pipeline_mod
from app import selfcheck
from app import updater as updater_mod
from app.comment_source import CommentSource
from app.pipeline import Pipeline

ROOT = Path(__file__).resolve().parent.parent
BANNED_LABELS = ("年龄限制", "被限流", "IP 限流", "封禁", "打坏", "多半")


def run(coro):
    return asyncio.run(coro)


async def wait_until(cond, limit=300):
    for _ in range(limit):
        if cond():
            return True
        await asyncio.sleep(0.01)
    return cond()


# ---------------------------------------------------------------------------
# 1. 版本兜底（updater）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("version,outdated", [
    ("7.0.0", True), ("7.0.0b2", True), ("6.6.6", True),
    ("7.0.1", False), ("7.1", False), ("7.2.3", False), ("8.0.0", False),
    (None, False), ("", False), ("garbage", False),
])
def test_outdated_threshold(version, outdated):
    assert updater_mod.tiktoklive_outdated(version) is outdated


def test_requirements_pin_matches_the_minimum():
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert 'TikTokLive>=7.0.1,<8; python_version >= "3.10"' in req
    assert updater_mod.TIKTOKLIVE_SPEC == "TikTokLive>=7.0.1,<8"
    assert updater_mod.TIKTOKLIVE_MIN == (7, 0, 1)


def _updater(monkeypatch, settings=None, versions=("7.0.0",), pip_code=0, installed=True):
    """造一个 Updater：设置存内存、版本号按序列返回、pip 只记参数。"""
    store = dict(settings or {})
    seq = list(versions)
    pip_calls = []
    monkeypatch.setattr(updater_mod, "load_settings", lambda: dict(store))
    monkeypatch.setattr(updater_mod, "save_setting", lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(updater_mod.sys, "version_info", (3, 13, 0))
    monkeypatch.setattr(updater_mod.importlib.util, "find_spec",
                        lambda name: object() if installed else None)

    def version():
        return seq.pop(0) if len(seq) > 1 else (seq[0] if seq else None)

    monkeypatch.setattr(updater_mod, "tiktoklive_version", version)
    up = updater_mod.Updater(server=None)

    async def fake_pip(args, label, timeout=600):
        pip_calls.append(list(args))
        return pip_code

    monkeypatch.setattr(up, "_pip_install", fake_pip)
    return up, store, pip_calls


def test_installed_but_outdated_is_upgraded(monkeypatch):
    """事故本身：装着 7.0.0，以前这里一看「装了」就直接返回。"""
    up, store, pip_calls = _updater(monkeypatch, versions=("7.0.0", "7.0.1"))
    assert run(up.ensure_tiktoklive("startup")) is True
    assert pip_calls == [[updater_mod.TIKTOKLIVE_SPEC]]
    assert "tiktoklive_upgrade_attempted_at" in store


def test_new_enough_install_is_left_alone(monkeypatch):
    up, store, pip_calls = _updater(monkeypatch, versions=("7.0.1",))
    assert run(up.ensure_tiktoklive("startup")) is True
    assert pip_calls == []


def test_failed_upgrade_still_reports_available(monkeypatch):
    """升不上去也不能把弹幕判成不可用：旧版本还装着，签名服务给真地址时照样能连。"""
    up, store, pip_calls = _updater(monkeypatch, versions=("7.0.0", "7.0.0"), pip_code=1)
    assert run(up.ensure_tiktoklive("startup")) is True
    assert len(pip_calls) == 1


def test_outdated_upgrade_respects_its_cooldown(monkeypatch):
    up, store, pip_calls = _updater(
        monkeypatch, settings={"tiktoklive_upgrade_attempted_at": time.time()},
        versions=("7.0.0",))
    assert run(up.ensure_tiktoklive("comments")) is True
    assert pip_calls == []


def test_fresh_install_uses_the_pinned_spec(monkeypatch):
    up, store, pip_calls = _updater(monkeypatch, versions=(None,), installed=False, pip_code=1)
    assert run(up.ensure_tiktoklive("startup")) is False
    assert pip_calls == [[updater_mod.TIKTOKLIVE_SPEC]]


def test_freshen_returns_the_version_change(monkeypatch):
    up, store, pip_calls = _updater(monkeypatch, versions=("7.0.1", "7.0.2"))
    assert run(up.freshen_tiktoklive("comment-rejected")) == ("7.0.1", "7.0.2")
    assert pip_calls and "-U" in pip_calls[0] and updater_mod.TIKTOKLIVE_SPEC in pip_calls[0]
    assert "tiktoklive_freshen_at" in store


def test_freshen_without_a_newer_release_returns_none(monkeypatch):
    up, store, pip_calls = _updater(monkeypatch, versions=("7.0.1", "7.0.1"))
    assert run(up.freshen_tiktoklive("comment-rejected")) is None
    assert len(pip_calls) == 1


def test_freshen_respects_the_cooldown(monkeypatch):
    """拒绝往往连着来：冷却期内不再拉 pip。"""
    up, store, pip_calls = _updater(
        monkeypatch, settings={"tiktoklive_freshen_at": time.time() - 60},
        versions=("7.0.1", "7.0.2"))
    assert run(up.freshen_tiktoklive("comment-rejected")) is None
    assert pip_calls == []


def test_freshen_does_nothing_when_not_installed(monkeypatch):
    up, store, pip_calls = _updater(monkeypatch, versions=(None,), installed=False)
    assert run(up.freshen_tiktoklive("comment-rejected")) is None
    assert pip_calls == []


# ---------------------------------------------------------------------------
# 2. 子进程归类：握手被拒沿异常链认状态码
# ---------------------------------------------------------------------------

def _fake_errors(monkeypatch):
    errors = ModuleType("TikTokLive.client.errors")

    class TikTokLiveError(Exception):
        pass

    class SignAPIError(TikTokLiveError):
        pass

    class SignatureRateLimitError(SignAPIError):
        pass

    class AuthenticatedWebSocketConnectionError(SignAPIError):
        pass

    class UserOfflineError(TikTokLiveError):
        pass

    class UserNotFoundError(TikTokLiveError):
        pass

    class WebsocketURLMissingError(TikTokLiveError):
        pass

    class AgeRestrictedError(TikTokLiveError):
        pass

    class WebcastBlockedError(TikTokLiveError):
        pass

    for name, obj in list(locals().items()):
        if isinstance(obj, type) and issubclass(obj, Exception):
            setattr(errors, name, obj)
    errors.WebcastBlocked200Error = WebcastBlockedError          # 7.0.1 的向后兼容别名
    client = ModuleType("TikTokLive.client")
    client.errors = errors
    root = ModuleType("TikTokLive")
    root.client = client
    monkeypatch.setitem(sys.modules, "TikTokLive", root)
    monkeypatch.setitem(sys.modules, "TikTokLive.client", client)
    monkeypatch.setitem(sys.modules, "TikTokLive.client.errors", errors)
    return errors


class InvalidStatusCode(Exception):          # websockets.legacy 的同名异常（类名是识别依据）
    def __init__(self, status_code):
        super().__init__("server rejected WebSocket connection: HTTP {}".format(status_code))
        self.status_code = status_code


class InvalidStatus(Exception):              # 新版 websockets：状态码在 response 上
    def __init__(self, status_code):
        super().__init__("server rejected WebSocket connection: HTTP {}".format(status_code))
        self.response = SimpleNamespace(status_code=status_code)


def test_tiktoklive_700_handshake_400_is_rejected(monkeypatch):
    _fake_errors(monkeypatch)
    from app import comment_worker
    assert comment_worker._classify_exc(InvalidStatusCode(400)) == ("rejected", 8)


def test_tiktoklive_701_wrapped_400_is_rejected_not_blocked(monkeypatch):
    """7.0.1 把 400 包成 WebcastBlockedError，原异常挂在 __cause__——必须先认状态码。"""
    errors = _fake_errors(monkeypatch)
    from app import comment_worker
    try:
        try:
            raise InvalidStatusCode(400)
        except InvalidStatusCode as inner:
            raise errors.WebcastBlockedError("400 Bad Request") from inner
    except errors.WebcastBlockedError as exc:
        assert comment_worker._classify_exc(exc) == ("rejected", 8)
        assert comment_worker._handshake_status(exc) == 400


def test_handshake_200_stays_blocked(monkeypatch):
    errors = _fake_errors(monkeypatch)
    from app import comment_worker
    try:
        try:
            raise InvalidStatusCode(200)
        except InvalidStatusCode as inner:
            raise errors.WebcastBlockedError("illegal secret key") from inner
    except errors.WebcastBlockedError as exc:
        assert comment_worker._classify_exc(exc) == ("blocked", 7)


def test_new_websockets_invalid_status_is_recognised(monkeypatch):
    _fake_errors(monkeypatch)
    from app import comment_worker
    assert comment_worker._classify_exc(InvalidStatus(403)) == ("rejected", 8)


def test_plain_errors_and_cyclic_chains_are_safe(monkeypatch):
    _fake_errors(monkeypatch)
    from app import comment_worker
    a, b = RuntimeError("a"), RuntimeError("b")
    a.__context__, b.__context__ = b, a                 # 环形异常链不能死循环
    assert comment_worker._handshake_status(a) is None
    assert comment_worker._classify_exc(a) == ("error", 1)


def test_worker_status_line_carries_the_http_status(capsys):
    from app import comment_worker
    comment_worker._status("rejected", "x", http_status=400)
    comment_worker._status("connecting")
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[0]["http_status"] == 400
    assert "http_status" not in lines[1]


# ---------------------------------------------------------------------------
# 3. 监督循环：被拒先找更新，升级了立刻重连，没有就长等
# ---------------------------------------------------------------------------

class FakeStream:
    def __init__(self, lines=(), hang_after=False):
        self._lines = list(lines)
        self._hang_after = hang_after

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        if self._hang_after:
            await asyncio.sleep(3600)
        return b""


class FakeProc:
    def __init__(self, stdout_lines=(), returncode=0, hang_after=False):
        self.stdout = FakeStream(stdout_lines, hang_after=hang_after)
        self.stderr = FakeStream()
        self.returncode = returncode

    async def wait(self):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


def jline(d):
    return (json.dumps(d) + "\n").encode("utf-8")


REJECTED_400 = jline({"event": "status", "state": "rejected",
                      "detail": "InvalidStatusCode: server rejected WebSocket connection: HTTP 400",
                      "http_status": 400})


def make_source(monkeypatch, procs, on_stale=None):
    state_log = []

    async def on_items(items):
        pass

    async def on_state(state, detail=""):
        state_log.append((state, detail))

    cs = CommentSource(on_items=on_items, on_state=on_state, cookies_browser="none")
    for name, value in (("BACKOFF_MIN_SEC", 0.01), ("BACKOFF_MAX_SEC", 0.05),
                        ("HEALTHY_SEC", 0.02), ("OFFLINE_RETRY_SEC", 0.02),
                        ("SIGN_ERROR_WAIT_SEC", 0.3), ("BLOCKED_WAIT_SEC", 0.3),
                        ("REJECTED_WAIT_SEC", 0.3), ("MAX_CONNECTS_PER_HOUR", 30),
                        ("STOP_GRACE_SEC", 0.05), ("HOUR_WINDOW_SEC", 5.0),
                        ("PROVISION_POLL_SEC", 0.02)):
        monkeypatch.setattr(CommentSource, name, value)
    monkeypatch.setattr(cs_mod, "worker_available", lambda: True)
    queue = list(procs)
    calls = []

    async def fake_spawn(self, args):
        calls.append(list(args))
        return queue.pop(0) if queue else FakeProc(hang_after=True)

    monkeypatch.setattr(CommentSource, "_spawn", fake_spawn)
    cs.on_stale = on_stale
    return cs, state_log, calls


def _no_banned_labels(state_log):
    for _state, detail in state_log:
        for word in BANNED_LABELS:
            assert word not in detail, (word, detail)


def test_rejected_then_upgraded_reconnects_immediately(monkeypatch):
    asked = []

    async def stale(reason):
        asked.append(reason)
        return ("7.0.0", "7.0.1")

    cs, state_log, calls = make_source(
        monkeypatch, [FakeProc([REJECTED_400], returncode=8)], on_stale=stale)

    async def scenario():
        cs.start("bella")
        ok = await wait_until(lambda: len(calls) >= 2, limit=15)   # 远小于 REJECTED_WAIT_SEC
        await cs.stop()
        return ok

    assert run(scenario()) is True
    assert asked == ["comment-rejected"]
    details = [d for _, d in state_log]
    assert any("7.0.0" in d and "7.0.1" in d for d in details)
    assert not any("InvalidStatusCode" in d for d in details)    # 原始英文报错没上面板
    _no_banned_labels(state_log)


def test_rejected_without_update_waits_and_says_so(monkeypatch):
    async def stale(reason):
        return None

    cs, state_log, calls = make_source(
        monkeypatch, [FakeProc([REJECTED_400], returncode=8)], on_stale=stale)

    async def scenario():
        cs.start("bella")
        await wait_until(lambda: any("分钟后自动重试" in d for _, d in state_log))
        await asyncio.sleep(0.1)                  # 仍在 REJECTED_WAIT_SEC 窗口内
        early = len(calls)
        await cs.stop()
        return early

    assert run(scenario()) == 1
    assert any("HTTP 400" in d and "分钟后自动重试" in d for _, d in state_log)
    _no_banned_labels(state_log)


def test_rejected_status_with_unexpected_exit_code_maps_to_eight(monkeypatch):
    """子进程被信号打死（退出码不在表里）：以最后一条 status 为准。"""
    asked = []

    async def stale(reason):
        asked.append(reason)
        return None

    cs, state_log, calls = make_source(
        monkeypatch, [FakeProc([REJECTED_400], returncode=-9)], on_stale=stale)

    async def scenario():
        cs.start("bella")
        await wait_until(lambda: asked)
        await cs.stop()

    run(scenario())
    assert asked == ["comment-rejected"]


def test_freshen_callback_that_raises_is_treated_as_no_update(monkeypatch):
    async def stale(reason):
        raise RuntimeError("pip exploded")

    cs, state_log, calls = make_source(
        monkeypatch, [FakeProc([REJECTED_400], returncode=8)], on_stale=stale)

    async def scenario():
        cs.start("bella")
        ok = await wait_until(lambda: any("分钟后自动重试" in d for _, d in state_log))
        await cs.stop()
        return ok

    assert run(scenario()) is True


def test_blocked_also_asks_for_an_update(monkeypatch):
    asked = []

    async def stale(reason):
        asked.append(reason)
        return ("7.0.1", "7.0.2")

    blocked = jline({"event": "status", "state": "blocked",
                     "detail": "WebcastBlockedError: rejected", "http_status": 200})
    cs, state_log, calls = make_source(
        monkeypatch, [FakeProc([blocked], returncode=7)], on_stale=stale)

    async def scenario():
        cs.start("bella")
        ok = await wait_until(lambda: len(calls) >= 2, limit=15)
        await cs.stop()
        return ok

    assert run(scenario()) is True
    assert asked == ["comment-rejected"]
    assert not any("WebcastBlockedError" in d for _, d in state_log)


def test_generic_error_gets_a_chinese_lead(monkeypatch):
    err = jline({"event": "status", "state": "error", "detail": "RuntimeError: boom"})
    cs, state_log, calls = make_source(monkeypatch, [FakeProc([err], returncode=1)])

    async def scenario():
        cs.start("bella")
        ok = await wait_until(lambda: any(d.startswith("评论连接出错") for _, d in state_log))
        await cs.stop()
        return ok

    assert run(scenario()) is True
    assert any("RuntimeError: boom" in d for _, d in state_log if d.startswith("评论连接出错"))


# ---------------------------------------------------------------------------
# 4. 留证据：会话日志、开场版本、自检
# ---------------------------------------------------------------------------

class FakeAudit:
    def __init__(self):
        self.records = []

    def comment_source(self, state, detail=""):
        self.records.append((state, detail))


class StubServer:
    def __init__(self):
        self.config = {}
        self.messages = []

    async def broadcast(self, msg):
        self.messages.append(msg)

    async def status(self, state, detail="", command=None):
        self.messages.append({"type": "status", "state": state, "detail": detail})


def test_comment_states_are_written_to_the_session_audit_once_per_change():
    p = Pipeline.__new__(Pipeline)
    p.server = StubServer()
    p.audit = FakeAudit()

    async def scenario():
        await p._publish_comment_source("connecting", "")
        await p._publish_comment_source("connecting", "")          # 重复：只记一条
        await p._publish_comment_source("error", "评论服务拒绝了连接（HTTP 400）")
        await p._publish_comment_source("connected", "")
        first = p.audit
        p.audit = FakeAudit()                                      # 换场
        await p._publish_comment_source("connected", "")          # 新一场照记
        p.audit = None
        await p._publish_comment_source("idle", "")               # 没有审计也不崩
        return first

    first = run(scenario())
    assert first.records == [("connecting", ""), ("error", "评论服务拒绝了连接（HTTP 400）"),
                             ("connected", "")]
    assert len([m for m in p.server.messages if m["type"] == "comment_source"]) == 6


def test_audit_log_writes_comment_source_records(tmp_path):
    log = audit_mod.AuditLog(room_url="https://www.tiktok.com/@x/live", log_dir=tmp_path)
    log.comment_source("error", "x" * 500)
    log.close()
    rows = [json.loads(line) for line in Path(log.path).read_text(encoding="utf-8").splitlines()]
    rec = [r for r in rows if r["type"] == "comment_source"][0]
    assert rec["state"] == "error" and len(rec["detail"]) == 300 and rec["at"]


def test_session_start_records_the_tiktoklive_version(monkeypatch, tmp_path):
    from app import settings
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    terms = tmp_path / "banned_terms.txt"
    terms.write_text("", encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms)
    monkeypatch.setattr(audit_mod, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(pipeline_mod, "_tiktoklive_version", lambda: "7.0.1")
    monkeypatch.setattr(CommentSource, "start", lambda self, unique_id: None)

    async def fake_stop(self, _external=True):
        pass

    monkeypatch.setattr(CommentSource, "stop", fake_stop)
    args = SimpleNamespace(cookies=None, target="zh-CN", translator="none", source="es",
                           beam=5, context=False, asr_temperature=None, glossary=None,
                           backend="auto", model=None, device="auto", compute_type="auto",
                           denoise="off", banned_terms=None, comments=True)
    p = Pipeline(args, StubServer())

    async def scenario():
        await p._begin_session("https://www.tiktok.com/@abc/live")
        path = p.audit.path
        await p._end_session()
        return path

    path = run(scenario())
    start = json.loads(Path(path).read_text(encoding="utf-8").splitlines()[0])
    assert start["type"] == "session_start" and start["tiktoklive_version"] == "7.0.1"


def test_pipeline_wires_the_freshen_callback():
    p = Pipeline.__new__(Pipeline)
    p.comment_source = CommentSource(on_items=lambda i: None, on_state=lambda s, d="": None)
    asked = []

    class FakeUpdater:
        async def freshen_tiktoklive(self, reason):
            asked.append(reason)
            return ("7.0.0", "7.0.1")

    # 与 __init__ 里同一个写法：惰性读 self.updater
    p.comment_source.on_stale = lambda reason: (
        p.updater.freshen_tiktoklive(reason) if getattr(p, "updater", None) is not None else None)
    assert p.comment_source.on_stale("x") is None                  # updater 还没注入
    p.updater = FakeUpdater()
    assert run(p.comment_source.on_stale("comment-rejected")) == ("7.0.0", "7.0.1")
    src = (ROOT / "app" / "pipeline.py").read_text(encoding="utf-8")
    assert "self.comment_source.on_stale = lambda reason:" in src


def test_startup_provisioning_reruns_selfcheck_only_when_the_version_changed(monkeypatch):
    seq = ["7.0.0", "7.0.1"]
    monkeypatch.setattr(pipeline_mod, "_tiktoklive_version", lambda: seq.pop(0) if seq else "7.0.1")
    p = Pipeline.__new__(Pipeline)
    checks = []

    class FakeUpdater:
        async def ensure_tiktoklive(self, reason="startup"):
            return True

    async def run_selfcheck():
        checks.append(1)

    p.updater = FakeUpdater()
    p.run_selfcheck = run_selfcheck
    p._selfcheck_task = None
    assert run(p.provision_comments()) is True
    assert checks == [1]
    monkeypatch.setattr(pipeline_mod, "_tiktoklive_version", lambda: "7.0.1")
    assert run(p.provision_comments()) is True
    assert checks == [1]                                            # 版本没变不重跑


def _args(**kw):
    base = dict(denoise="off", backend="auto", model=None, translator="none", comments=True)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.parametrize("version,level,needle", [
    (None, "warn", "还没装"),
    ("7.0.0", "warn", "低于 7.0.1"),
    ("7.0.1", "ok", "7.0.1"),
])
def test_selfcheck_comments_row(monkeypatch, version, level, needle):
    # 固定 Python 版本：CI 有一台 3.9 跑器，那里这一行先报「需要 3.10」，走不到版本判断
    monkeypatch.setattr(sys, "version_info", (3, 13, 0))
    monkeypatch.setattr(updater_mod, "tiktoklive_version", lambda: version)
    c = run(selfcheck.check_comments(_args()))
    assert c["name"] == "观众弹幕" and c["level"] == level and needle in c["detail"]
    for word in BANNED_LABELS:
        assert word not in c["detail"] + c["fix"]


def test_selfcheck_comments_on_python_39_says_unavailable(monkeypatch):
    monkeypatch.setattr(sys, "version_info", (3, 9, 18))
    monkeypatch.setattr(updater_mod, "tiktoklive_version", lambda: "7.0.1")
    c = run(selfcheck.check_comments(_args()))
    assert c["level"] == "warn" and "Python 3.10" in c["detail"]


def test_selfcheck_comments_off_by_choice_is_ok():
    c = run(selfcheck.check_comments(_args(comments=False)))
    assert c["level"] == "ok" and "--no-comments" in c["detail"]


def test_selfcheck_lists_the_comments_row(monkeypatch):
    monkeypatch.setattr(sys, "version_info", (3, 13, 0))
    monkeypatch.setattr(updater_mod, "tiktoklive_version", lambda: "7.0.1")
    for name in ("check_ffmpeg", "check_denoise", "check_asr", "check_translator",
                 "check_watchlist", "check_glossary", "check_audit", "check_resolver",
                 "check_disk"):
        async def ok(*a, _n=name, **k):
            return selfcheck._check(_n, "ok", "stub")
        monkeypatch.setattr(selfcheck, name, ok)
    checks = run(selfcheck.run_all(_args(), None, None))
    assert any(c["name"] == "观众弹幕" and c["level"] == "ok" for c in checks)
