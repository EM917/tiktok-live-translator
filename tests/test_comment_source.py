"""弹幕后端抓取（app/comment_source.py）：父进程侧的纯逻辑，全程不 import
TikTokLive——唯一 import 它的地方是子进程入口 app/comment_worker.py，这里
只用假事件/假子进程（鸭子类型）去驱动。

覆盖六件事：
  1. event_to_item / worker_available 两个纯函数；
  2. CommentSource 的监督协程：顺序转发、按退出码退避/重试/放弃、
     登录态重试、签名限流等待、stop() 收尾、每小时连接上限；
  3. Pipeline 与 CommentSource 的接线（开播即起、下播即停、直接流地址/
     --no-comments 时不起、session_end 里的 comments_received）；
  4. server.py 对 comment_source 广播的处理（config 落盘）；
  5. comment_worker._classify_exc 的异常分类——用一份和真库同构的假异常
     模块跑，不依赖真的 TikTokLive（测试环境承诺 Python 3.9+，那里装不了
     TikTokLive 7.x）；
  6. 弹幕静默看门狗（connected 却哑了 SILENCE_RESTART_SEC 才优雅重连、
     同一窗口最多一次）：编排完整子进程的用假时钟推进 _clock()，窗口
     守卫本身直接摆状态调 _silence_watchdog，不绕整条监督协程。

不 import 其它测试文件，避免耦合到别处 fixture 的变化；所有等待用有限
轮询（wait_until），绝不无限等；CommentSource 的秒级常量全部调到
0.01~0.05，让整套退避/限流场景在几十毫秒内跑完。
"""
import ast
import asyncio
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

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
    （模拟仍在运行、暂时没有新内容）。

    hang 时不是一次性 sleep(3600)，而是轮询检查 `terminated`：真实子进程
    收到 SIGTERM 会自己退出、stdout 随之关闭，FakeProc.terminate() 就置这个
    标记来模拟同样的效果（看门狗测试要看到 terminate() 之后主循环真的能
    读到 EOF，不是只记了一个「叫过 terminate」的布尔值）。也支持 push()
    在挂起期间追加新行，模拟「还连着、又来了新弹幕」。轮询用短 sleep，不用
    asyncio.Event/Future——这两者在协程外构造在 Python 3.9 下不安全，
    而这里的 FakeProc/FakeStream 都是在测试函数体（非协程）里构造的。"""

    def __init__(self, lines=(), hang_after=False):
        self._lines = list(lines)
        self._hang_after = hang_after
        self.terminated = False

    def push(self, line):
        self._lines.append(line)

    async def readline(self):
        while True:
            if self._lines:
                item = self._lines.pop(0)
                if isinstance(item, BaseException):
                    # 模拟一行超长弹幕撑爆 StreamReader 行缓冲上限时
                    # readline() 真实会抛出的那类异常（ValueError 等）。
                    raise item
                return item
            if not self._hang_after or self.terminated:
                return b""
            await asyncio.sleep(0.005)


class FakeProc:
    """process-like 假对象：stdout/stderr 异步流，wait() 是协程，
    terminate()/kill() 记录调用，并让 stdout 的 hang 循环看到「已经被终止」
    （见 FakeStream），这样看门狗真的 terminate() 之后主循环能读到 EOF、
    走完 _run_once 的正常收尾，不是永远卡在 readline() 上。"""

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
        self.stdout.terminated = True

    def kill(self):
        self.kill_called = True
        self.stdout.terminated = True


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
        ("SIGN_ERROR_WAIT_SEC", 0.2), ("BLOCKED_WAIT_SEC", 0.2),
        ("MAX_CONNECTS_PER_HOUR", 30),
        ("STOP_GRACE_SEC", 0.05), ("HOUR_WINDOW_SEC", 0.2),
        ("PROVISION_POLL_SEC", 0.02),
    ):
        monkeypatch.setattr(CommentSource, name, value)

    monkeypatch.setattr(cs_mod, "worker_available", lambda: True)

    queue = list(procs)
    calls = []

    async def fake_spawn(self, args):
        calls.append(list(args))
        if queue:
            return queue.pop(0)
        # 剧本演完：给一个永远不退出的子进程，让监督协程安静地挂在 readline
        # 上等 stop()。以前这里 pop 空列表抛 IndexError，监督协程按「启动
        # 失败」退避重试，calls 会随时间继续涨——断言精确次数的测试就成了
        # 和事件循环快慢赛跑。
        return FakeProc(stdout_lines=[], hang_after=True)

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


def _anon_notes(monkeypatch):
    """记下 CommentSource 向 resolver 报了几次「刚发过匿名请求」。"""
    from app import resolver
    notes = []
    monkeypatch.setattr(resolver, "note_anonymous_request", lambda: notes.append(True))
    return notes


def test_worker_without_session_id_is_noted_as_anonymous_requests(monkeypatch):
    """不带 sessionid 的子进程一启动就匿名抓 https://www.tiktok.com/@主播/live（TikTokLive 的
    fetch_room_id_from_html），连上评论 WebSocket 之后才不再发请求。resolver 的间隔规则
    （带登录抓直播页之前和上一次匿名请求隔开 8 秒）要看得见它：启动时记一次，连上时记一次。"""
    notes = _anon_notes(monkeypatch)
    proc = FakeProc(stdout_lines=[
        jline({"event": "status", "state": "connecting"}),
        jline({"event": "status", "state": "connected", "room_id": 1}),
    ], returncode=0)
    cs, _items, _states, _calls = make_source(monkeypatch, [proc])
    assert run(cs._run_once("abc", []))[0] == 0
    assert len(notes) == 2             # 连上之后才退出：退出那一刻没有新的匿名请求，不再记


def test_worker_that_exits_before_connecting_is_noted_at_exit(monkeypatch):
    notes = _anon_notes(monkeypatch)
    proc = FakeProc(stdout_lines=[
        jline({"event": "status", "state": "connecting"}),
        jline({"event": "status", "state": "offline"}),
    ], returncode=3)
    cs, _items, _states, _calls = make_source(monkeypatch, [proc])
    assert run(cs._run_once("abc", []))[0] == 3
    assert len(notes) == 2             # 启动 + 退出：它的匿名请求最晚发到退出那一刻


def test_worker_with_session_id_is_not_noted_as_anonymous(monkeypatch):
    notes = _anon_notes(monkeypatch)
    proc = FakeProc(stdout_lines=[jline({"event": "status", "state": "connected"})],
                    returncode=0)
    cs, _items, _states, calls = make_source(monkeypatch, [proc])
    run(cs._run_once("abc", ["--session-id", "sid-value"]))
    assert calls == [["abc", "--session-id", "sid-value"]] and notes == []


def test_anonymous_worker_start_moves_the_resolver_stamp(monkeypatch):
    """不经假的记录器：真的 resolver.note_anonymous_request，假的时钟。"""
    from app import resolver
    monkeypatch.setattr(resolver, "_monotonic", lambda: 4242.0)
    assert resolver._ANON["last"] is None
    proc = FakeProc(stdout_lines=[jline({"event": "status", "state": "offline"})],
                    returncode=3)
    cs, _items, _states, _calls = make_source(monkeypatch, [proc])
    run(cs._run_once("abc", []))
    assert resolver._ANON["last"] == 4242.0


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
    """每小时连接上限。用注入的假时钟：两次连接落在同一时刻，任何窗口都必然
    触发上限，不靠真实时间赛跑——Windows CI 的 time.time() 精度 15.6 毫秒，
    之前把窗口压到 50 毫秒去赛跑，反复偶发失败。"""
    procs = [FakeProc(stdout_lines=[], returncode=3) for _ in range(6)]   # 主播未开播，快速失败重试
    cs, items_log, state_log, calls = make_source(monkeypatch, procs)
    monkeypatch.setattr(CommentSource, "MAX_CONNECTS_PER_HOUR", 2)
    monkeypatch.setattr(CommentSource, "OFFLINE_RETRY_SEC", 0.01)
    cs._clock = lambda: 1_000_000.0          # 冻结：所有连接时间戳相同

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


def test_hourly_cap_releases_once_the_window_has_passed(monkeypatch):
    """窗口滑过之后要放行：拨表越过 HOUR_WINDOW_SEC，_enforce_hourly_limit
    必须立即返回、不报「过多」、并把过期的尝试记录清掉。"""
    cs, items_log, state_log, calls = make_source(monkeypatch, [])
    monkeypatch.setattr(CommentSource, "MAX_CONNECTS_PER_HOUR", 2)
    monkeypatch.setattr(CommentSource, "HOUR_WINDOW_SEC", 3600.0)
    clock = {"t": 1_000_000.0}
    cs._clock = lambda: clock["t"]
    cs._connect_times = [clock["t"], clock["t"]]      # 已经用满

    async def scenario():
        clock["t"] += 3601.0                          # 越过窗口
        await cs._enforce_hourly_limit()

    run(scenario())
    assert cs._connect_times == []
    assert not any(s == "error" for s, _ in state_log)


# ---------------------------------------------------------------------------
# 3b. 弹幕静默看门狗：connected 却哑了太久，优雅重连
# ---------------------------------------------------------------------------

def test_silence_watchdog_no_restart_while_comments_keep_arriving(monkeypatch):
    """连着且弹幕没断流：看门狗查很多轮也不该重连——用假时钟推进很久的模拟
    时间，配合持续补充的弹幕，证明触发条件是「没弹幕」，不是「查了很多次」。"""
    proc = FakeProc(stdout_lines=[
        jline({"event": "status", "state": "connected"}),
        jline({"event": "comments", "items": [{"id": "0", "user": "a", "text": "hola"}]}),
    ], hang_after=True)
    cs, items_log, state_log, calls = make_source(monkeypatch, [proc])
    monkeypatch.setattr(CommentSource, "SILENCE_RESTART_SEC", 5.0)
    monkeypatch.setattr(CommentSource, "SILENCE_CHECK_SEC", 0.02)
    clock = {"t": 1_000_000.0}
    cs._clock = lambda: clock["t"]

    async def scenario():
        cs.start("abc")
        assert await wait_until(lambda: len(items_log) >= 1)
        for i in range(4):
            clock["t"] += 3.0            # 每次都小于阈值 5 秒——弹幕一直在续命
            proc.stdout.push(jline({"event": "comments",
                             "items": [{"id": str(i + 1), "user": "a", "text": "hola"}]}))
            await asyncio.sleep(0.05)    # 给看门狗几轮真实时间的检查机会
        before_stop = proc.terminate_called   # cs.stop() 自己也会 terminate，别跟看门狗的混了
        await cs.stop()
        return before_stop

    before_stop = run(scenario())
    assert before_stop is False
    assert all(s != "silent_restart" for s, _ in state_log)
    assert calls == [["abc"]]


def test_silence_watchdog_restarts_after_threshold_and_reconnects_at_backoff_min(monkeypatch):
    """connected 满 15 分钟没有任何弹幕（这里用假时钟等价模拟）：优雅重连、
    audit 路径（on_state）收到 silent_restart 及分钟数，且下一次连接立刻按
    BACKOFF_MIN_SEC 重试，不是指数退避，也不算一次拒绝/出错。"""
    proc1 = FakeProc(stdout_lines=[jline({"event": "status", "state": "connected"})],
                     hang_after=True, returncode=0)
    proc2 = FakeProc(stdout_lines=[jline({"event": "status", "state": "connected"})],
                     hang_after=True)
    cs, items_log, state_log, calls = make_source(monkeypatch, [proc1, proc2])
    monkeypatch.setattr(CommentSource, "SILENCE_RESTART_SEC", 5.0)
    monkeypatch.setattr(CommentSource, "SILENCE_CHECK_SEC", 0.02)
    monkeypatch.setattr(CommentSource, "BACKOFF_MIN_SEC", 0.03)
    monkeypatch.setattr(CommentSource, "BACKOFF_MAX_SEC", 5.0)
    clock = {"t": 2_000_000.0}
    cs._clock = lambda: clock["t"]

    async def scenario():
        cs.start("abc")
        assert await wait_until(lambda: cs.state == "connected")
        clock["t"] += 900.0     # 模拟连着的这段时间里过了 15 分钟，一条弹幕都没有
        assert await wait_until(lambda: proc1.terminate_called)
        assert await wait_until(lambda: any(s == "silent_restart" for s, _ in state_log))
        assert await wait_until(lambda: len(calls) >= 2)
        assert await wait_until(lambda: cs.state == "connected" and len(calls) == 2)
        await cs.stop()

    run(scenario())
    detail = next(d for s, d in state_log if s == "silent_restart")
    assert "15" in detail and "分钟" in detail and "已重连评论流" in detail
    assert calls == [["abc"], ["abc"]]
    # returncode 是 0（子进程自己 disconnect 退出），不是被判定成拒绝/出错的
    # 任何分支——没有走 REJECTED_WAIT_SEC/BLOCKED_WAIT_SEC 那些长等待就是证据：
    # 上面 wait_until(len(calls)>=2) 早就在几十毫秒内通过了。
    assert not any(s == "error" for s, _ in state_log)


def test_silence_watchdog_ignores_silence_while_not_connected(monkeypatch):
    """还在 connecting（或 offline 等其它非 connected 状态）时，不管模拟时间
    推了多久都不该触发——看门狗只看「connected 却哑了」，不是「哑了」。"""
    proc = FakeProc(stdout_lines=[jline({"event": "status", "state": "connecting"})],
                    hang_after=True)
    cs, items_log, state_log, calls = make_source(monkeypatch, [proc])
    monkeypatch.setattr(CommentSource, "SILENCE_RESTART_SEC", 1.0)
    monkeypatch.setattr(CommentSource, "SILENCE_CHECK_SEC", 0.02)
    clock = {"t": 5_000_000.0}
    cs._clock = lambda: clock["t"]

    async def scenario():
        cs.start("abc")
        assert await wait_until(lambda: any(s == "connecting" for s, _ in state_log))
        clock["t"] += 10_000.0        # 远超阈值的模拟时间
        await asyncio.sleep(0.1)      # 给看门狗几轮真实时间的检查机会
        before_stop = proc.terminate_called
        await cs.stop()
        return before_stop

    before_stop = run(scenario())
    assert before_stop is False
    assert all(s != "silent_restart" for s, _ in state_log)


def test_silence_watchdog_task_is_cancelled_when_worker_exits_on_its_own(monkeypatch):
    """子进程自己退出（不是被看门狗打断）时，这次 _run_once 起的看门狗任务
    要跟着收尾，不能泄漏成一个还在查一个已经死掉的子进程的孤儿任务。

    模拟「自己退出」：不调用 terminate()，直接让 proc1 的 stdout 关掉——
    这样能确认收尾是 _run_once 的 finally 做的，不是看门狗自己触发的
    （下面反证 proc1.terminate_called 为假）。等了一小段真实时间才让它退出，
    是为了让看门狗任务真的先跑起来、查过至少一轮：如果子进程在看门狗第一次
    被调度之前就已经退出（零个事件循环轮次），看门狗任务会在从没执行过一行
    代码的情况下被取消，测不出「取消」这回事——只是测出「没机会开始」。"""
    proc1 = FakeProc(stdout_lines=[jline({"event": "status", "state": "connected"})],
                     returncode=0, hang_after=True)
    proc2 = FakeProc(stdout_lines=[jline({"event": "status", "state": "connected"})],
                     hang_after=True)
    cs, items_log, state_log, calls = make_source(monkeypatch, [proc1, proc2])
    monkeypatch.setattr(CommentSource, "SILENCE_RESTART_SEC", 5.0)
    monkeypatch.setattr(CommentSource, "SILENCE_CHECK_SEC", 0.02)

    watchdog_tasks = []
    orig = CommentSource._silence_watchdog

    async def spy(self, proc_arg):
        watchdog_tasks.append(asyncio.current_task())
        await orig(self, proc_arg)

    monkeypatch.setattr(CommentSource, "_silence_watchdog", spy)

    async def scenario():
        cs.start("abc")
        assert await wait_until(lambda: cs.state == "connected")
        await asyncio.sleep(0.05)          # 让第一个看门狗真的跑起来、查过至少一轮
        proc1.stdout.terminated = True     # 子进程自己退出：stdout 关掉，不经我们的 terminate()
        assert await wait_until(lambda: len(calls) >= 2)   # proc1 走完，重连出 proc2
        await asyncio.sleep(0.05)
        await cs.stop()

    run(scenario())
    assert not proc1.terminate_called        # 证明是「自己退出」，不是被看门狗 terminate 的
    assert len(watchdog_tasks) >= 2          # 每次 _run_once 都起了自己的看门狗
    assert watchdog_tasks[0].done()          # proc1 那次的看门狗没有跟着泄漏


def test_silence_watchdog_restarts_at_most_once_per_window(monkeypatch):
    """看门狗内部「同一 SILENCE_RESTART_SEC 窗口最多重连一次」的判断：直接
    摆时钟/状态调 _silence_watchdog，不经完整的子进程编排（那条路径已经在
    上面几个用例里测过）——重连要烧一次匿名请求加一次签名握手，看门狗自己
    不能在窗口内心跳式地反复触发。"""
    cs, items_log, state_log, calls = make_source(monkeypatch, [])
    monkeypatch.setattr(CommentSource, "SILENCE_RESTART_SEC", 10.0)
    monkeypatch.setattr(CommentSource, "SILENCE_CHECK_SEC", 0.02)
    clock = {"t": 100.0}
    cs._clock = lambda: clock["t"]
    cs.state = "connected"
    cs._connected_at = 0.0      # 已经「连了」100 秒，早就过了阈值
    cs._last_item_at = None

    class DummyProc:
        def __init__(self):
            self.terminate_called = False

        def terminate(self):
            self.terminate_called = True

        async def wait(self):
            return 0

    async def scenario():
        # 阶段一：5 秒前刚重连过，还没满 10 秒窗口——不该再重连
        cs._last_silent_restart_at = 95.0
        dummy1 = DummyProc()
        task1 = asyncio.ensure_future(cs._silence_watchdog(dummy1))
        await asyncio.sleep(0.1)
        task1.cancel()
        try:
            await task1
        except asyncio.CancelledError:
            pass

        # 阶段二：上一次重连是 50 秒前——窗口已经过了，这回该重连
        cs.state = "connected"
        cs._last_silent_restart_at = 50.0
        dummy2 = DummyProc()
        task2 = asyncio.ensure_future(cs._silence_watchdog(dummy2))
        ok = await wait_until(lambda: dummy2.terminate_called)
        assert ok
        await wait_until(lambda: task2.done())
        return dummy1.terminate_called

    dummy1_terminated = run(scenario())
    assert dummy1_terminated is False
    assert cs.state == "silent_restart"
    assert cs._last_silent_restart_at == 100.0


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
    # macOS 上弹幕等第一次流地址解析返回才起（tests/test_login_first.py 里验证）；
    # 这里验证的是其它平台一直以来的接线：开场即起、收尾即停
    from app import resolver
    monkeypatch.setattr(resolver, "_login_first_enabled", lambda: False)
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


def test_end_session_records_comments_received_in_session_end(monkeypatch, tmp_path):
    """收到过几条弹幕要跟着 session_end 一起落盘——哪怕弹幕连接全程显示
    「已连接」，这一列也能让事后一眼看出它是不是哑了一整场（配合看门狗，
    见 CommentSource.comments_received）。"""
    from app import resolver
    monkeypatch.setattr(resolver, "_login_first_enabled", lambda: False)

    class FakeCommentSource:
        def __init__(self):
            self.comments_received = 42
            self.stopped = False

        def start(self, unique_id):
            pass

        async def stop(self):
            self.stopped = True

    p, server = make_pipeline(monkeypatch, tmp_path)
    fake_cs = FakeCommentSource()
    p.comment_source = fake_cs

    async def scenario():
        await p._begin_session("https://www.tiktok.com/@abc/live")
        audit = p.audit
        await p._end_session(audit, reason="offline")
        return audit

    audit = run(scenario())
    assert fake_cs.stopped is True
    records = [json.loads(line) for line in audit.path.read_text(encoding="utf-8").splitlines()
               if line.strip()]
    end_rec = next(r for r in records if r["type"] == "session_end")
    assert end_rec["comments_received"] == 42
    assert end_rec["reason"] == "offline"


# ---------------------------------------------------------------------------
# 5. server.py：comment_source 广播落盘
# ---------------------------------------------------------------------------

def test_server_broadcast_comment_source_updates_config():
    async def scenario():
        server = CaptionServer(port=8765)
        await server.broadcast({"type": "comment_source", "backend": "connected",
                                "detail": ""})
        return server

    server = run(scenario())
    assert "extension_clients" not in server.config     # 插件计数连同插件一起没了
    assert server.config["comment_backend"] == "connected"
    assert server.config["comment_detail"] == ""


# ---------------------------------------------------------------------------
# 6. comment_worker.py：只做语法检查，绝不 import（它 import TikTokLive）
# ---------------------------------------------------------------------------

def test_comment_worker_module_parses_without_import():
    path = ROOT / "app" / "comment_worker.py"
    source = path.read_text(encoding="utf-8")
    ast.parse(source)   # 语法有效即可；真正的行为由子进程集成/人工验证覆盖


# ---------------------------------------------------------------------------
# 子进程的「父进程死了就退」看门狗：模块顶层不 import TikTokLive，可以直接导入
# ---------------------------------------------------------------------------

def test_worker_exits_when_parent_stdin_closes():
    from app import comment_worker

    calls = []

    async def scenario():
        await comment_worker._watch_parent(
            on_gone=lambda: calls.append("gone"),
            read=lambda: b"",                 # stdin 立刻 EOF = 父进程已死
            getppid=lambda: 4242,             # ppid 不变，只靠 stdin 这一路
            grace=0.01,
            exit_fn=lambda code: calls.append(("exit", code)),
        )

    run(scenario())
    assert calls == ["gone", ("exit", 0)]


def test_worker_exits_when_parent_pid_changes():
    import threading
    from app import comment_worker

    calls = []
    ppids = iter([7, 7, 1, 1, 1, 1])
    block = threading.Event()

    async def scenario():
        await comment_worker._watch_parent(
            on_gone=lambda: calls.append("gone"),
            read=lambda: block.wait(5) and b"",   # stdin 一直读不到 EOF
            getppid=lambda: next(ppids, 1),
            grace=0.01,
            # 顺手放开那根假 stdin，别让 daemon 线程在后面多挂 5 秒
            exit_fn=lambda code: (calls.append(("exit", code)), block.set()),
        )

    # ppid 轮询间隔是 2 秒，测试里把它缩短
    orig_sleep = asyncio.sleep

    async def fast_sleep(sec):
        await orig_sleep(min(sec, 0.01))

    import app.comment_worker as cw
    cw.asyncio.sleep = fast_sleep
    try:
        run(scenario())
    finally:
        cw.asyncio.sleep = orig_sleep
        block.set()
    assert calls == ["gone", ("exit", 0)]


# ---------------------------------------------------------------------------
# 5. comment_worker._classify_exc：异常 -> (状态, 退出码)
# ---------------------------------------------------------------------------

def _fake_tiktoklive_errors(monkeypatch, with_blocked=True):
    """造一份和真库**同构**的异常模块塞进 sys.modules。

    重点是复刻真实的继承关系：AuthenticatedWebSocketConnectionError 和
    SignatureRateLimitError 都是 SignAPIError 的子类。bug 就藏在这里——
    父类那条 isinstance 排在前面时，会把「需要登录态」整条吞掉。继承关系
    抄错了，下面这些测试就等于没测。
    """
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

    class WebcastBlocked200Error(TikTokLiveError):
        pass

    for name, obj in list(locals().items()):
        if isinstance(obj, type) and issubclass(obj, Exception):
            setattr(errors, name, obj)
    if not with_blocked:                       # 模拟老版本 TikTokLive
        delattr(errors, "WebcastBlocked200Error")

    client = ModuleType("TikTokLive.client")
    client.errors = errors
    root = ModuleType("TikTokLive")
    root.client = client
    monkeypatch.setitem(sys.modules, "TikTokLive", root)
    monkeypatch.setitem(sys.modules, "TikTokLive.client", client)
    monkeypatch.setitem(sys.modules, "TikTokLive.client.errors", errors)
    return errors


def test_login_required_is_not_swallowed_by_its_sign_api_parent(monkeypatch):
    """回归：AuthenticatedWebSocketConnectionError 是 SignAPIError 的子类，
    曾被父类那条先截走，退成 4 号白等 600 秒，而「取浏览器登录态重试」的
    分支永远走不到。"""
    errors = _fake_tiktoklive_errors(monkeypatch)
    from app import comment_worker

    assert issubclass(errors.AuthenticatedWebSocketConnectionError, errors.SignAPIError)
    assert comment_worker._classify_exc(
        errors.AuthenticatedWebSocketConnectionError("要登录")) == ("login_required", 5)


def test_plain_sign_api_errors_still_map_to_four(monkeypatch):
    """修子类顺序不能把父类那条弄丢。"""
    errors = _fake_tiktoklive_errors(monkeypatch)
    from app import comment_worker

    assert comment_worker._classify_exc(errors.SignAPIError("忙")) == ("error", 4)
    assert comment_worker._classify_exc(errors.SignatureRateLimitError("超额")) == ("error", 4)


def test_blocked_gets_its_own_code_instead_of_two_second_retry(monkeypatch):
    """回归：WebcastBlocked200Error 曾没进分类表，落到 1 号走 2 秒退避——
    顶着 TikTok 的风控反复敲门。"""
    errors = _fake_tiktoklive_errors(monkeypatch)
    from app import comment_worker

    assert comment_worker._classify_exc(
        errors.WebcastBlocked200Error("bot detected")) == ("blocked", 7)


def test_classify_survives_tiktoklive_without_blocked_error(monkeypatch):
    """requirements 允许 TikTokLive>=7,<8 的任意版本，老版本没有这个异常类：
    取不到就该退化成「永不匹配」，而不是让整个分类函数炸在 ImportError 上。"""
    errors = _fake_tiktoklive_errors(monkeypatch, with_blocked=False)
    from app import comment_worker

    assert not hasattr(errors, "WebcastBlocked200Error")
    assert comment_worker._classify_exc(RuntimeError("别的错")) == ("error", 1)
    assert comment_worker._classify_exc(errors.UserOfflineError("")) == ("offline", 3)


def test_classify_remaining_codes_unchanged(monkeypatch):
    errors = _fake_tiktoklive_errors(monkeypatch)
    from app import comment_worker

    assert comment_worker._classify_exc(errors.UserOfflineError("")) == ("offline", 3)
    assert comment_worker._classify_exc(errors.UserNotFoundError("")) == ("not_found", 6)
    assert comment_worker._classify_exc(
        errors.WebsocketURLMissingError("")) == ("login_required", 5)
    assert comment_worker._classify_exc(errors.AgeRestrictedError("")) == ("login_required", 5)
    assert comment_worker._classify_exc(ValueError("啥也不是")) == ("error", 1)


def test_supervise_blocked_backs_off_instead_of_reconnecting(monkeypatch):
    """7 号退出要走 BLOCKED_WAIT_SEC，不能像 1 号那样 2 秒就重连。"""
    proc1 = FakeProc(stdout_lines=[], returncode=7)
    proc2 = FakeProc(stdout_lines=[jline({"event": "status", "state": "connected"})],
                     returncode=0, hang_after=True)
    cs, items_log, state_log, calls = make_source(monkeypatch, [proc1, proc2])
    monkeypatch.setattr(CommentSource, "BACKOFF_MIN_SEC", 0.01)
    monkeypatch.setattr(CommentSource, "BLOCKED_WAIT_SEC", 0.3)

    async def scenario():
        cs.start("abc")
        assert await wait_until(lambda: any(s == "error" for s, _ in state_log))
        await asyncio.sleep(0.1)              # 仍在 BLOCKED_WAIT_SEC 窗口内
        early = len(calls)
        assert await wait_until(lambda: len(calls) >= 2, limit=100)
        await cs.stop()
        return early

    assert run(scenario()) == 1


def test_supervise_maps_blocked_status_line_when_exit_code_is_lost(monkeypatch):
    """子进程被信号打死、退出码不在约定表里时，靠它退出前写的最后一条
    status 行还原意图——「blocked」必须还原成 7，不能退化成 2 秒重连。"""
    proc1 = FakeProc(stdout_lines=[jline({"event": "status", "state": "blocked"})],
                     returncode=-9)
    proc2 = FakeProc(stdout_lines=[jline({"event": "status", "state": "connected"})],
                     returncode=0, hang_after=True)
    cs, items_log, state_log, calls = make_source(monkeypatch, [proc1, proc2])
    monkeypatch.setattr(CommentSource, "BACKOFF_MIN_SEC", 0.01)
    monkeypatch.setattr(CommentSource, "BLOCKED_WAIT_SEC", 0.3)

    async def scenario():
        cs.start("abc")
        assert await wait_until(lambda: any(s == "error" for s, _ in state_log))
        await asyncio.sleep(0.1)
        early = len(calls)
        assert await wait_until(lambda: len(calls) >= 2, limit=100)
        await cs.stop()
        return early

    assert run(scenario()) == 1
