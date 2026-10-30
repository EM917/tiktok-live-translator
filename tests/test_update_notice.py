"""更新提示降噪。

这个项目的更新提示是**真实的打扰**：它弹在直播中控的界面上。连续发几个 patch
的日子（真实发生过：一天 12 个 release，全部来自实盘暴露的问题），不降噪就是
把用户轰一遍。

规则：短时间内已经提示过，就只静默刷新版本信息——按钮保持可用，但不再抢注意力。
手动点「检查更新」永远给回应，那是用户主动问的。
"""
import time

from app import updater as U
from tests.helpers import run


class FakeServer:
    def __init__(self):
        self.sent = []
        self.config = {}

    async def broadcast(self, msg):
        self.sent.append(msg)

    async def status(self, *a, **k):
        pass


def make(monkeypatch, last_notice=None, tag="v9.9.9"):
    """构造一个只到「发现新版本」那一步的更新器。"""
    u = U.Updater.__new__(U.Updater)
    u.server = FakeServer()
    u.latest = {}

    store = {"update_notice_at": last_notice} if last_notice else {}
    monkeypatch.setattr(U, "load_settings", lambda: dict(store))
    monkeypatch.setattr(U, "save_setting",
                        lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(U, "local_version", lambda: "0.0.1")

    class Resp:
        status = 200

        async def json(self):
            return {"tag_name": tag, "body": "", "html_url": "http://x"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Sess:
        def get(self, *a, **k):
            return Resp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: Sess())
    return u, store


def test_first_notice_is_prominent(monkeypatch):
    u, store = make(monkeypatch)
    run(u.check_and_notify(delay=0))
    assert u.server.sent[-1]["quiet"] is False
    assert store.get("update_notice_at")          # 记下这次提示的时间


def test_a_second_release_soon_after_does_not_shout(monkeypatch):
    """同一天连发几个 patch 时，用户只该被打扰一次。"""
    u, _ = make(monkeypatch, last_notice=time.time() - 600)
    run(u.check_and_notify(delay=0))
    assert u.server.sent[-1]["quiet"] is True
    # 版本信息仍然送达：按钮要能用，只是不抢注意力
    assert u.server.sent[-1]["version"] == "v9.9.9"


def test_the_notice_returns_after_the_quiet_window(monkeypatch):
    u, _ = make(monkeypatch, last_notice=time.time() - U.QUIET_NOTICE_SEC - 60)
    run(u.check_and_notify(delay=0))
    assert u.server.sent[-1]["quiet"] is False


def test_the_quiet_window_holds_within_a_run_when_settings_cannot_be_saved(monkeypatch):
    """设置文件读不出来时 update_notice_at 记不进去（save_setting 不写），只认文件的话每次检查都醒目弹一次。
    这个进程里也记一份：同一次运行内照样 6 小时只醒目一次。"""
    u, store = make(monkeypatch)
    monkeypatch.setattr(U, "save_setting", lambda k, v: False)
    run(u.check_and_notify(delay=0))
    run(u.check_and_notify(delay=0))
    assert [m["quiet"] for m in u.server.sent if m.get("type") == "update_available"] == [False, True]
    assert store == {}


def test_a_manual_check_always_answers(monkeypatch):
    """用户主动点「检查更新」时不能装没听见。"""
    u, _ = make(monkeypatch, last_notice=time.time() - 60)
    run(u.check_and_notify(delay=0, manual=True))
    assert u.server.sent[-1]["quiet"] is False
