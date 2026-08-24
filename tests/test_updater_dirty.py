"""一键更新的「本地有修改」判断。

真实故障：用户目录里多了一个 .run.log 和两个词表备份（都是未跟踪文件），
自动更新就此彻底罢工，提示是「请自行处理后 git pull」——对一个不会用终端的
人来说，这是个没有出口的死胡同。而 git pull 本来就不会动未跟踪文件。
"""
import asyncio

from app.updater import Updater


class FakeServer:
    def __init__(self):
        self.statuses = []

    async def status(self, state, detail=""):
        self.statuses.append((state, detail))

    async def broadcast(self, msg):
        pass


def make(git_results):
    u = Updater.__new__(Updater)
    u.server = FakeServer()
    u.latest = {"can_auto": True, "url": ""}
    calls = []

    async def fake_git(*args):
        calls.append(args)
        return git_results.get(args[0], (0, "", ""))

    u._git = fake_git
    u._calls = calls
    return u


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_status_check_excludes_untracked_files():
    """必须带上 --untracked-files=no，否则一个日志文件就能挡死更新。"""
    u = make({"status": (0, "", ""), "pull": (1, "", "stop here")})
    run(u._apply_inner())
    status_call = next(c for c in u._calls if c[0] == "status")
    assert "--untracked-files=no" in status_call


def test_tracked_modifications_still_block_and_name_the_files():
    """真被改过的程序文件仍要拦下——但要说清是哪几个，别让人猜。"""
    u = make({"status": (0, " M app/pipeline.py\n M web/app.js\n", "")})
    run(u._apply_inner())
    state, detail = u.server.statuses[-1]
    assert "app/pipeline.py" in detail and "web/app.js" in detail
    # 程序本身没坏，只是这次没更新——不该显示成「出错了」
    assert state == "idle"


def test_clean_tree_proceeds_to_pull():
    u = make({"status": (0, "", ""), "pull": (1, "", "boom")})
    run(u._apply_inner())
    assert any(c[0] == "pull" for c in u._calls)
