"""晚到的旧流任务收尾，不得碰下一场的东西。

实录事故（2026-08-31）：识别一段要几十秒时，点「开始」后旧任务在 3 秒
宽限内取消不掉、被放手自行收尾；等它终于走到 finally，self.audit 已经是
新会话的了——一关，新会话 102 条字幕的合规证据全部静默丢失（audit 文件
只剩 544 字节的头）。stats 循环同理会被砍，健康提示随之失踪。
"""
import asyncio

from app.pipeline import Pipeline


class FakeAudit:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeServer:
    config = {}

    async def broadcast(self, msg):
        pass


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _pipeline():
    p = Pipeline.__new__(Pipeline)
    p.server = FakeServer()
    p._stats_task = None
    return p


def test_late_finalizer_closes_only_its_own_audit():
    p = _pipeline()
    old_audit, new_audit = FakeAudit(), FakeAudit()
    p.audit = new_audit                        # 新会话已经开始
    _run(p._end_session(old_audit))            # 旧任务晚到的收尾
    assert old_audit.closed                    # 自己的关了
    assert not new_audit.closed                # 新会话的动都没动
    assert p.audit is new_audit


def test_late_finalizer_leaves_the_new_stats_loop_alone():
    async def scenario():
        p = _pipeline()
        old_audit = FakeAudit()
        p.audit = FakeAudit()                  # 已是新会话
        p._stats_task = asyncio.ensure_future(asyncio.sleep(30))
        stats = p._stats_task
        await p._end_session(old_audit)
        alive = not stats.cancelled()
        stats.cancel()
        return alive

    assert _run(scenario())                    # 新会话的 stats 循环还活着


def test_current_session_teardown_still_cleans_everything():
    async def scenario():
        p = _pipeline()
        mine = FakeAudit()
        p.audit = mine                         # 我就是当前会话
        p._stats_task = asyncio.ensure_future(asyncio.sleep(30))
        stats = p._stats_task
        await p._end_session(mine)
        await asyncio.sleep(0)                 # 让取消真正传播到任务
        return mine.closed, p.audit, stats.cancelled() or stats.done()

    closed, audit, stats_gone = _run(scenario())
    assert closed and audit is None and stats_gone
