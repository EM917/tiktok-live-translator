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

    async def status(self, state, detail="", command=None):
        self.statuses.append((state, detail, command))

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
    state, detail, command = u.server.statuses[-1]
    assert "app/pipeline.py" in detail and "web/app.js" in detail
    # 程序本身没坏，只是这次没更新——不该显示成「出错了」
    assert state == "idle"
    # 必须给出一条能照做的出路，而且带真实路径
    assert command and command.startswith('cd "') and "git pull" in command


def test_clean_tree_proceeds_to_pull():
    u = make({"status": (0, "", ""), "pull": (1, "", "boom")})
    run(u._apply_inner())
    assert any(c[0] == "pull" for c in u._calls)


def test_every_failure_hands_the_user_a_runnable_command():
    """更新器不能把自己锁死。

    真实死局：它因为几个未跟踪文件拒绝更新，而修好这个判断的代码只能靠更新
    送达——一台机器就这样永远停在了 v0.6。所以每一条「没能更新」的出口都必须
    附带一条用户可以原样粘进终端的完整命令（含项目路径），哪怕程序自己已经
    帮不上忙。
    """
    scenarios = {
        "git 不可用": {"status": (127, "", "not found")},
        "有本地修改": {"status": (0, " M app/pipeline.py\n", "")},
        "pull 失败": {"status": (0, "", ""), "pull": (1, "", "diverged")},
    }
    for label, results in scenarios.items():
        u = make(results)
        run(u._apply_inner())
        state, detail, command = u.server.statuses[-1]
        assert command, "{}：没有给出任何可执行的出路".format(label)
        assert "git pull" in command, label
        assert command.startswith('cd "'), "{}：命令里必须带项目路径".format(label)
        assert state == "idle", "{}：更新没成不等于程序坏了".format(label)


def test_discard_variant_only_appears_for_local_modifications():
    """只有在「文件被改过」这一种情况下，才该建议丢弃改动。"""
    u = make({"status": (0, " M app/pipeline.py\n", "")})
    run(u._apply_inner())
    assert "git checkout -- ." in u.server.statuses[-1][2]

    u2 = make({"status": (0, "", ""), "pull": (1, "", "diverged")})
    run(u2._apply_inner())
    assert "git checkout" not in u2.server.statuses[-1][2]


def test_the_command_survives_a_page_reload():
    """出路必须留存。用户刷新一下页面就丢了唯一的操作指引，等于没给。"""
    import asyncio as _asyncio

    from app.server import CaptionServer

    srv = CaptionServer.__new__(CaptionServer)
    srv.clients = set()
    srv.history = []
    srv.alerts = []
    srv.config = {}

    async def go():
        await srv.broadcast({"type": "status", "state": "idle",
                             "detail": "本次没有自动更新",
                             "command": 'cd "/x" && git pull --ff-only'})

    _asyncio.get_event_loop_policy().new_event_loop().run_until_complete(go())
    # hello 重放的就是 config["status"]
    assert srv.config["status"]["command"] == 'cd "/x" && git pull --ff-only'
