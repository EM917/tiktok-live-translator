"""弹幕后端抓取（app/comment_source.py）：父进程侧的纯逻辑，全程不 import
TikTokLive——唯一 import 它的地方是子进程入口 app/comment_worker.py，这里
只用假事件/假子进程（鸭子类型）去驱动。

覆盖四件事：
  1. event_to_item / worker_available 两个纯函数；
  2. CommentSource 的监督协程：顺序转发、按退出码退避/重试/放弃、
     登录态重试、签名限流等待、stop() 收尾、每小时连接上限；
  3. Pipeline 与 CommentSource 的接线（开播即起、下播即停、直接流地址/
     --no-comments 时不起）；
  4. server.py 对 comment_source 广播的处理（config 落盘 + 插件连接数
     消息里带上后端状态）。

不 import 其它测试文件，避免耦合到别处 fixture 的变化；所有等待用有限
轮询（wait_until），绝不无限等；CommentSource 的秒级常量全部调到
0.01~0.05，让整套退避/限流场景在几十毫秒内跑完。
"""
import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from app import comment_source as cs_mod
from app import pipeline as pipeline_mod
from app.comment_source import CommentSource, event_to_item, worker_available
from app.pipeline import Pipeline
from app.server import CaptionServer

ROOT = Path(__file__).resolve().parent.parent


def run(coro):
    return asyncio.run(coro)


async def wait_until(cond, limit=300):
    """有限轮询：最多 limit * 0.01s，绝不无限等。"""
    for _ in range(limit):
        if cond():
            return True
        await asyncio.sleep(0.01)
    return cond()


# ---------------------------------------------------------------------------
# 1. event_to_item：鸭子类型，纯函数
# ---------------------------------------------------------------------------

class FakeUser:
    def __init__(self, nickname=None, unique_id=None):
        self.nickname = nickname
        self.unique_id = unique_id


class FakeCommon:
    def __init__(self, msg_id=None):
        self.msg_id = msg_id


class FakeEvent:
    def __init__(self, comment, user=None, msg_id=None, has_common=True):
        self.comment = comment
        self.user = user
        if has_common:
            self.common = FakeCommon(msg_id)


def test_event_to_item_basic_with_msg_id():
    ev = FakeEvent("hola mundo", user=FakeUser(nickname="Ana", unique_id="ana123"), msg_id=987)
    item = event_to_item(ev)
    # 昵称优先：中控看的是显示名，不是账号 id
    assert item == {"id": "987", "user": "Ana", "text": "hola mundo"}


def test_event_to_item_falls_back_to_unique_id_without_nickname():
    ev = FakeEvent("buenos dias", user=FakeUser(unique_id="ana123"), msg_id=1)
    item = event_to_item(ev)
    assert item["user"] == "ana123"


def test_event_to_item_empty_or_blank_text_is_none():
    assert event_to_item(FakeEvent("", user=FakeUser(nickname="a"), msg_id=1)) is None
    assert event_to_item(FakeEvent("   ", user=FakeUser(nickname="a"), msg_id=1)) is None
    assert event_to_item(FakeEvent(None, user=FakeUser(nickname="a"), msg_id=1)) is None


def test_event_to_item_missing_or_zero_msg_id_uses_counter_prefix():
    i1 = event_to_item(FakeEvent("hola", user=FakeUser(nickname="a"), msg_id=None))
    i2 = event_to_item(FakeEvent("adios", user=FakeUser(nickname="b"), msg_id=0))
    i3 = event_to_item(FakeEvent("hola de nuevo", user=FakeUser(nickname="c"), has_common=False))
    for item in (i1, i2, i3):
        assert item["id"].startswith("t")
    # 模块级计数器递增，三条互不相同
    assert len({i1["id"], i2["id"], i3["id"]}) == 3


# ---------------------------------------------------------------------------
# 2. worker_available：Python 版本 + 库是否已装
# ---------------------------------------------------------------------------

def test_worker_available_false_when_python_too_old(monkeypatch):
    monkeypatch.setattr(cs_mod.sys, "version_info", (3, 9, 0))
    assert worker_available() is False


def test_worker_available_false_when_lib_missing(monkeypatch):
    monkeypatch.setattr(cs_mod.sys, "version_info", (3, 13, 0))
    monkeypatch.setattr(cs_mod.importlib.util, "find_spec", lambda name: None)
    assert worker_available() is False


def test_worker_available_true_when_lib_present(monkeypatch):
    monkeypatch.setattr(cs_mod.sys, "version_info", (3, 13, 0))
    monkeypatch.setattr(cs_mod.importlib.util, "find_spec", lambda name: object())
    assert worker_available() is True


# ---------------------------------------------------------------------------
# 3. CommentSource 监督协程：假子进程驱动
# ---------------------------------------------------------------------------

class FakeStream:
    """模拟 asyncio 子进程的 stdout/stderr：readline() 是协程，逐行吐给定内容，
    吃完之后要么立刻回 b""（管道关闭/进程退出），要么挂起等外部 cancel
    （模拟仍在运行、暂时没有新内容）。"""

    def __init__(self, lines=(), hang_after=False):
        self._lines = list(lines)
        self._hang_after = hang_after

    async def readline(self):
        if self._lines:
            item = self._lines.pop(0)
            if isinstance(item, BaseException):
                # 模拟一行超长弹幕撑爆 StreamReader 行缓冲上限时
                # readline() 真实会抛出的那类异常（ValueError 等）。
                raise item
            return item
        if self._hang_after:
            await asyncio.sleep(3600)
        return b""


class FakeProc:
    """process-like 假对象：stdout/stderr 异步流，wait() 是协程，
    terminate()/kill() 只记录调用，不做真的事。"""

    def __init__(self, stdout_lines=(), stderr_lines=(), returncode=0, hang_after=False):
        self.stdout = FakeStream(stdout_lines, hang_after=hang_after)
        self.stderr = FakeStream(stderr_lines)
        self.returncode = returncode
        self.terminate_called = False
        self.kill_called = False

    async def wait(self):
        return self.returncode

    def terminate(self):
        self.terminate_called = True

    def kill(self):
        self.kill_called = True


def jline(d):
    return (json.dumps(d) + "\n").encode("utf-8")


def make_source(monkeypatch, procs, cookies_browser="none"):
    """造一个把秒级常量全部调小、_spawn 换成假子进程队列的 CommentSource。

    返回 (cs, items_log, state_log, calls)：
      items_log —— on_items 每次收到的 items 列表；
      state_log —— on_state 每次收到的 (state, detail)；
      calls     —— 每次 _spawn 被调用时收到的 args（list）。
    """
    items_log = []
    state_log = []

    async def on_items(items):
        items_log.append(items)

    async def on_state(state, detail=""):
        state_log.append((state, detail))

    cs = CommentSource(on_items=on_items, on_state=on_state, cookies_browser=cookies_browser)

    for name, value in (
        ("BACKOFF_MIN_SEC", 0.01), ("BACKOFF_MAX_SEC", 0.05),
        ("HEALTHY_SEC", 0.02), ("OFFLINE_RETRY_SEC", 0.02),
        ("SIGN_ERROR_WAIT_SEC", 0.2), ("MAX_CONNECTS_PER_HOUR", 30),
        ("STOP_GRACE_SEC", 0.05), ("HOUR_WINDOW_SEC", 0.2),
        ("PROVISION_POLL_SEC", 0.02),
    ):
        monkeypatch.setattr(CommentSource, name, value)

    monkeypatch.setattr(cs_mod, "worker_available", lambda: True)

    queue = list(procs)
    calls = []

    async def fake_spawn(self, args):
        calls.append(list(args))
        return queue.pop(0)

    monkeypatch.setattr(CommentSource, "_spawn", fake_spawn)
    return cs, items_log, state_log, calls


def test_supervise_forwards_status_and_comments_in_order(monkeypatch):
    proc = FakeProc(stdout_lines=[
        jline({"event": "status", "state": "connecting"}),
        jline({"event": "status", "state": "connected", "room_id": 1}),
        jline({"event": "comments", "items": [{"id": "1", "user": "a", "text": "hola"}]}),
        jline({"event": "comments", "items": [{"id": "2", "user": "b", "text": "adios"}]}),
    ], hang_after=True)   # 之后挂起——模拟仍然连着，不触发退出/重连
    cs, items_log, state_log, calls = make_source(monkeypatch, [proc])

    async def scenario():
        cs.start("abc")
        ok = await wait_until(lambda: len(items_log) >= 2)
        assert ok
        await cs.stop()

    run(scenario())
    assert items_log[0] == [{"id": "1", "user": "a", "text": "hola"}]
    assert items_log[1] == [{"id": "2", "user": "b", "text": "adios"}]
    states = [s for s, _ in state_log]
    assert states.index("connecting") < states.index("connected")
    assert calls == [["abc"]]


def test_supervise_respawns_after_clean_exit(monkeypatch):
    proc1 = FakeProc(stdout_lines=[jline({"event": "status", "state": "connected"})],
                      returncode=0)
    proc2 = FakeProc(stdout_lines=[jline({"event": "status", "state": "connected"})],
                      returncode=0, hang_after=True)
    cs, items_log, state_log, calls = make_source(monkeypatch, [proc1, proc2])

    async def scenario():
        cs.start("abc")
        ok = await wait_until(lambda: len(calls) >= 2)
        assert ok
        await cs.stop()

    run(scenario())
    assert len(calls) >= 2


def test_supervise_user_not_found_stops_without_retry(monkeypatch):
    proc = FakeProc(stdout_lines=[], returncode=6)
    cs, items_log, state_log, calls = make_source(monkeypatch, [proc])

    async def scenario():
        cs.start("nope")
        ok = await wait_until(lambda: state_log and state_log[-1][0] == "unavailable")
        assert ok
        await asyncio.sleep(0.1)   # 确认之后确实没有第二次 spawn
        return len(calls)

    n = run(scenario())
    assert n == 1
    assert state_log[-1][0] == "unavailable"


def test_supervise_login_required_retries_with_session_cookies(monkeypatch):
    proc1 = FakeProc(stdout_lines=[], returncode=5)
    proc2 = FakeProc(stdout_lines=[jline({"event": "status", "state": "connected"})],
                      returncode=0, hang_after=True)
    cs, items_log, state_log, calls = make_source(monkeypatch, [proc1, proc2])
    monkeypatch.setattr(cs_mod, "session_cookies", lambda cookies_browser: ("sid", "idc"))

    async def scenario():
        cs.start("abc")
        ok = await wait_until(lambda: len(calls) >= 2)
        assert ok
        await cs.stop()

    run(scenario())
    assert calls[0] == ["abc"]
    assert "--session-id" in calls[1] and "sid" in calls[1]
    assert "--tt-target-idc" in calls[1] and "idc" in calls[1]


def test_supervise_login_required_without_cookies_gives_up(monkeypatch):
    proc = FakeProc(stdout_lines=[], returncode=5)
    cs, items_log, state_log, calls = make_source(monkeypatch, [proc])
    monkeypatch.setattr(cs_mod, "session_cookies", lambda cookies_browser: (None, None))

    async def scenario():
        cs.start("abc")
        ok = await wait_until(lambda: state_log and state_log[-1][0] == "unavailable")
        assert ok
        await asyncio.sleep(0.1)
        return len(calls)

    n = run(scenario())
    assert n == 1
    assert "登录" in state_log[-1][1]


def test_supervise_sign_error_waits_before_retry(monkeypatch):
    proc1 = FakeProc(stdout_lines=[], returncode=4)
    proc2 = FakeProc(stdout_lines=[jline({"event": "status", "state": "connected"})],
                      returncode=0, hang_after=True)
    cs, items_log, state_log, calls = make_source(monkeypatch, [proc1, proc2])
    monkeypatch.setattr(CommentSource, "SIGN_ERROR_WAIT_SEC", 0.2)

    async def scenario():
        cs.start("abc")
        ok = await wait_until(lambda: any(s == "error" for s, _ in state_log))
        assert ok
        await asyncio.sleep(0.1)          # 仍在 SIGN_ERROR_WAIT_SEC 窗口内
        early = len(calls)
        ok2 = await wait_until(lambda: len(calls) >= 2, limit=100)
        assert ok2
        await cs.stop()
        return early

    early = run(scenario())
    assert early == 1


def test_stop_terminates_process_and_sets_idle(monkeypatch):
    proc = FakeProc(stdout_lines=[jline({"event": "status", "state": "connected"})],
                     hang_after=True)
    cs, items_log, state_log, calls = make_source(monkeypatch, [proc])

    async def scenario():
        cs.start("abc")
        ok = await wait_until(lambda: any(s == "connected" for s, _ in state_log))
        assert ok
        await cs.stop()

    run(scenario())
    assert proc.terminate_called
    assert state_log[-1][0] == "idle"
    assert len(calls) == 1   # stop 之后不该有额外 spawn


def test_supervise_recovers_from_stdout_read_exception(monkeypatch):
    """一行超长弹幕（或库本身吐出的畸形输出）能让 readline() 抛异常——
    这必须只终止当前这次子进程、退避重试，而不是把整条监督协程带死
    （否则弹幕来源会静默停止更新，且没人清理已经启动的子进程）。"""
    proc1 = FakeProc(stdout_lines=[
        jline({"event": "status", "state": "connecting"}),
        ValueError("Separator is found, but chunk is longer than limit"),
    ], hang_after=True)
    proc2 = FakeProc(stdout_lines=[jline({"event": "status", "state": "connected"})],
                      hang_after=True)
    cs, items_log, state_log, calls = make_source(monkeypatch, [proc1, proc2])

    async def scenario():
        cs.start("abc")
        ok = await wait_until(lambda: len(calls) == 2)
        assert ok
        ok = await wait_until(lambda: any(s == "connected" for s, _ in state_log))
        assert ok
        await cs.stop()

    run(scenario())
    # 出异常那次子进程必须被 terminate，绝不能变成没人管的孤儿进程
    assert proc1.terminate_called
    assert len(calls) == 2


def test_supervise_recovers_from_spawn_exception(monkeypatch):
    """_spawn() 本身抛异常（比如 fd 耗尽）也不能把监督协程带死。"""
    good_proc = FakeProc(stdout_lines=[jline({"event": "status", "state": "connected"})],
                          hang_after=True)
    cs, items_log, state_log, calls = make_source(monkeypatch, [good_proc])
    monkeypatch.setattr(CommentSource, "BACKOFF_MIN_SEC", 0.01)

    attempts = []

    async def flaky_spawn(self, args):
        attempts.append(list(args))
        if len(attempts) == 1:
            raise OSError("too many open files")
        return good_proc

    monkeypatch.setattr(CommentSource, "_spawn", flaky_spawn)

    async def scenario():
        cs.start("abc")
        ok = await wait_until(lambda: any(s == "connected" for s, _ in state_log))
        assert ok
        await cs.stop()

    run(scenario())
    assert len(attempts) == 2   # 第一次抛异常之后还会有第二次重试


def test_start_with_new_unique_id_switches_streamer_normally(monkeypatch):
    """没有竞态干扰的正常路径：换主播必须真的切过去，不能被过期检查
    误伤（这是给上面那条「过期重启不泄漏」修复配的对照测试）。"""
    proc_old = FakeProc(stdout_lines=[jline({"event": "status", "state": "connected"})],
                         hang_after=True)
    proc_new = FakeProc(stdout_lines=[jline({"event": "status", "state": "connected"})],
                         hang_after=True)
    cs, items_log, state_log, calls = make_source(monkeypatch, [proc_old, proc_new])

    async def scenario():
        cs.start("old")
        ok = await wait_until(lambda: cs.state == "connected")
        assert ok
        cs.start("new")
        ok = await wait_until(lambda: len(calls) == 2)
        assert ok
        ok = await wait_until(lambda: cs._unique_id == "new" and cs.state == "connected")
        assert ok
        await cs.stop()

    run(scenario())
    assert proc_old.terminate_called
    assert calls == [["old"], ["new"]]


def test_start_after_restart_scheduled_then_stopped_does_not_leak_task(monkeypatch):
    """换主播触发的 fire-and-forget 重启，如果在它真正 _launch 之前
    这个会话就自己结束调用了 stop()，重启不该再为一个已经不相关的
    目标起一个没人跟踪的子进程/监督任务。"""
    proc_old = FakeProc(stdout_lines=[jline({"event": "status", "state": "connected"})],
                         hang_after=True)
    proc_new = FakeProc(stdout_lines=[jline({"event": "status", "state": "connected"})],
                         hang_after=True)
    cs, items_log, state_log, calls = make_source(monkeypatch, [proc_old, proc_new])

    async def scenario():
        cs.start("old")
        ok = await wait_until(lambda: cs.state == "connected")
        assert ok
        cs.start("new")              # 触发 fire-and-forget _restart，尚未真正执行
        await cs.stop()              # 模拟新会话没等重启完成就自己结束了
        # 给可能还没跑完的 _restart 协程一些时间把剩下的代码跑完
        for _ in range(50):
            await asyncio.sleep(0.01)
        return True

    run(scenario())
    assert cs.state == "idle"
    # 过期的重启不该再 _launch：监督任务不该是活着的
    assert cs._task is None or cs._task.done()
    # 也不该为 "new" 起第二个子进程
    assert len(calls) == 1


def test_hourly_connect_cap(monkeypatch):
    procs = [FakeProc(stdout_lines=[], returncode=3) for _ in range(6)]   # 主播未开播，快速失败重试
    cs, items_log, state_log, calls = make_source(monkeypatch, procs)
    monkeypatch.setattr(CommentSource, "MAX_CONNECTS_PER_HOUR", 2)
    monkeypatch.setattr(CommentSource, "OFFLINE_RETRY_SEC", 0.01)
    monkeypatch.setattr(CommentSource, "HOUR_WINDOW_SEC", 0.05)

    async def scenario():
        cs.start("abc")
        ok = await wait_until(
            lambda: any(s == "error" and "过多" in d for s, d in state_log), limit=500)
        assert ok
        calls_at_cap = len(calls)
        await cs.stop()
        return calls_at_cap

    calls_at_cap = run(scenario())
    # 达到上限时最多只应该已经尝试过 MAX_CONNECTS_PER_HOUR 次——「过多」的
    # 提示必须出现在第三次尝试之前，不是之后才马后炮
    assert calls_at_cap == 2


# ---------------------------------------------------------------------------
# 4. Pipeline 接线：开播起、下播停、直接流地址/--no-comments 不起
# ---------------------------------------------------------------------------

class StubServer:
    """照抄 tests/test_comments.py 的写法：只记消息，不真的起网络。"""

    def __init__(self):
        self.config = {}
        self.messages = []

    async def status(self, state, detail=""):
        self.messages.append({"type": "status", "state": state, "detail": detail})

    async def broadcast(self, msg):
        self.messages.append(msg)

    def of_type(self, t):
        return [m for m in self.messages if m.get("type") == t]


def make_pipeline(monkeypatch, tmp_path, comments=True):
    from app import settings
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    terms_file = tmp_path / "banned_terms.txt"
    terms_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms_file)

    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source="es",
        beam=5, context=False, asr_temperature=None, glossary=None, backend="auto", model=None,
        device="auto", compute_type="auto", denoise="off", banned_terms=None,
        comments=comments,
    )
    server = StubServer()
    p = Pipeline(args, server)
    return p, server


def test_begin_session_starts_comment_source_by_streamer(monkeypatch, tmp_path):
    started = []
    stopped = []

    def fake_start(self, unique_id):
        started.append(unique_id)

    async def fake_stop(self):
        stopped.append(True)

    monkeypatch.setattr(CommentSource, "start", fake_start)
    monkeypatch.setattr(CommentSource, "stop", fake_stop)
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def scenario():
        await p._begin_session("https://www.tiktok.com/@abc/live")
        await p._end_session()

    run(scenario())
    assert started == ["abc"]
    assert stopped == [True]


def test_begin_session_direct_url_skips_comment_source(monkeypatch, tmp_path):
    started = []
    monkeypatch.setattr(CommentSource, "start", lambda self, unique_id: started.append(unique_id))
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def scenario():
        await p._begin_session("https://cdn.example.com/stream.m3u8")

    run(scenario())
    assert started == []
    sources = server.of_type("comment_source")
    assert sources and sources[-1]["backend"] == "unavailable"


def test_begin_session_no_comments_flag_skips_comment_source(monkeypatch, tmp_path):
    started = []
    monkeypatch.setattr(CommentSource, "start", lambda self, unique_id: started.append(unique_id))
    p, server = make_pipeline(monkeypatch, tmp_path, comments=False)

    async def scenario():
        await p._begin_session("https://www.tiktok.com/@abc/live")

    run(scenario())
    assert started == []
    sources = server.of_type("comment_source")
    assert sources and sources[-1]["backend"] == "unavailable"
    assert "--no-comments" in sources[-1]["detail"]


# ---------------------------------------------------------------------------
# 5. server.py：comment_source 广播落盘 + 插件连接数消息里带上后端状态
# ---------------------------------------------------------------------------

def test_server_broadcast_comment_source_updates_config():
    async def scenario():
        server = CaptionServer(port=8765)
        await server.broadcast({"type": "comment_source", "backend": "connected",
                                "detail": "", "extension_clients": 2})
        return server

    server = run(scenario())
    assert server.config["extension_clients"] == 2
    assert server.config["comment_backend"] == "connected"
    assert server.config["comment_detail"] == ""


def test_server_set_extension_clients_message_carries_backend():
    async def scenario():
        server = CaptionServer(port=8765)
        server.config["comment_backend"] = "connected"
        server.config["comment_detail"] = "已连接"
        sent = []
        real_broadcast = server.broadcast

        async def spy(msg):
            sent.append(dict(msg))
            await real_broadcast(msg)

        server.broadcast = spy
        await server._set_extension_clients(1)
        return sent

    sent = run(scenario())
    assert len(sent) == 1
    assert sent[0]["type"] == "comment_source"
    assert sent[0]["extension_clients"] == 1
    assert sent[0]["backend"] == "connected"
    assert sent[0]["detail"] == "已连接"


# ---------------------------------------------------------------------------
# 6. comment_worker.py：只做语法检查，绝不 import（它 import TikTokLive）
# ---------------------------------------------------------------------------

def test_comment_worker_module_parses_without_import():
    path = ROOT / "app" / "comment_worker.py"
    source = path.read_text(encoding="utf-8")
    ast.parse(source)   # 语法有效即可；真正的行为由子进程集成/人工验证覆盖
