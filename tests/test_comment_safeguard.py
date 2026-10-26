"""弹幕连接的保险（2026-09-14 事故之后加的四层，外加合并前审查确认的修正）。

那次：Euler 签名服务把连接派到备用推流线路，TikTokLive 7.0.0 拼错握手地址，
每次 HTTP 400；修好它的 7.0.1 四天前就发布了，而程序只在「没装」时才安装、从不升级。
面板上是英文原始报错，后台隔一分钟重试一次，会话日志里一条弹幕状态都没有。

这里钉住：
  1. 版本兜底：低于 7.0.1 就升，失败也不把弹幕判成不可用；
  2. 被拒时找更新：握手被拒（沿异常链认状态码）单独归类，先找补丁版本，升级了或
     子进程启动后版本已经变了就立刻重连，否则长等；检查失败不进六小时冷却；并发的
     检查合成一次；调用方被取消时 pip 锁不提前释放；
  3. 中文说明：原始英文报错不上面板，面板说的「正在检查」必须真的在检查，事后写明
     结果；文案不带规则八禁用的原因标签；
  4. 留证据：弹幕状态变化、服务端给的原始原因（只进审计）与组件版本写进会话日志，
     自检多一行「观众弹幕」且只写事实。

不 import 其它测试文件；不 import TikTokLive（CI 没装），异常类用同构的假模块。
等待时间都留足余量：Windows 跑器的计时粒度约 15.6 毫秒。
"""
import asyncio
import json
import sys
import time
from pathlib import Path
from types import MethodType, ModuleType, SimpleNamespace

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
# 1. 版本兜底与更新检查（updater）
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


def _updater(monkeypatch, settings=None, versions=("7.0.0",), pip_code=0, installed=True,
             pip_gate=None, index=(True, None), index_gate=None, busy=False):
    """造一个 Updater：设置存内存、版本号按序列返回、pip 只记参数（可选地等一个闸门）。
    index 是 _tiktoklive_index_versions 的结果：(问到没有, 发布满 7 天的 7.x 里最新的版本)。
    接上一个「此刻有没有在监听」的钩子（busy）：没接管线的 Updater 一律按「在忙」算，什么都不查。"""
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
    up._stream_active = lambda: busy

    async def fake_pip(args, label, timeout=600, skip_if=None):
        if skip_if is not None and skip_if():     # 同真的 _pip_install：拿到锁之后、开始之前问一次
            return updater_mod.PIP_SKIPPED
        pip_calls.append(list(args))
        if pip_gate is not None:
            await pip_gate()
        return pip_code

    monkeypatch.setattr(up, "_pip_install", fake_pip)

    async def fake_index():
        if index_gate is not None:
            await index_gate()
        return index

    monkeypatch.setattr(up, "_tiktoklive_index_versions", fake_index)
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


def test_outdated_upgrade_respects_its_cooldown_and_says_so(monkeypatch, capsys):
    up, store, pip_calls = _updater(
        monkeypatch, settings={"tiktoklive_upgrade_attempted_at": time.time()},
        versions=("7.0.0",))
    assert run(up.ensure_tiktoklive("comments")) is True
    assert pip_calls == []
    assert "一小时内已试过升级" in capsys.readouterr().out


def test_fresh_install_uses_the_pinned_spec(monkeypatch):
    up, store, pip_calls = _updater(monkeypatch, versions=(None,), installed=False, pip_code=1)
    assert run(up.ensure_tiktoklive("startup")) is False
    assert pip_calls == [[updater_mod.TIKTOKLIVE_SPEC]]


def test_freshen_upgrade_returns_the_change_and_starts_the_cooldown(monkeypatch):
    up, store, pip_calls = _updater(monkeypatch, versions=("7.0.1", "7.0.2"), index=(True, "7.0.2"))
    result = run(up.freshen_tiktoklive("comment-rejected"))
    assert result == {"outcome": "upgraded", "before": "7.0.1", "after": "7.0.2"}
    # 专家审查 I5（D9）：装的是挑出来的那一版（==），不再 -U 到当天最新
    assert pip_calls == [["--upgrade-strategy", "only-if-needed", "TikTokLive==7.0.2"]]
    assert "tiktoklive_freshen_at" in store


def test_freshen_without_an_eligible_release_says_no_update(monkeypatch):
    """发布满 7 天的 7.x 里一个都没有（或者都不比已装的新）：不跑 pip，照常进六小时冷却。"""
    up, store, pip_calls = _updater(monkeypatch, versions=("7.0.1", "7.0.1"), index=(True, None))
    assert run(up.freshen_tiktoklive("comment-rejected"))["outcome"] == "no-update"
    assert pip_calls == [] and "tiktoklive_freshen_at" in store


def test_freshen_is_skipped_while_monitoring_or_updating(monkeypatch):
    """专家审查 I5（D6）：在监听或一键更新进行中不查、不装，也不记冷却。"""
    up, store, pip_calls = _updater(monkeypatch, versions=("7.0.1", "7.0.1", "7.0.1", "7.0.2"),
                                    index=(True, "7.0.2"), busy=True)
    asked = []

    async def index():
        asked.append(1)
        return True, "7.0.2"

    up._tiktoklive_index_versions = index
    assert run(up.freshen_tiktoklive("comment-rejected")) == {"outcome": "busy", "before": "7.0.1"}
    up._stream_active = lambda: False
    up._applying = True
    assert run(up.freshen_tiktoklive("comment-rejected"))["outcome"] == "busy"
    assert asked == [] and pip_calls == [] and "tiktoklive_freshen_at" not in store
    up._applying = False
    assert run(up.freshen_tiktoklive("comment-rejected"))["outcome"] == "upgraded"


def test_index_says_installed_is_latest_so_pip_install_is_skipped(monkeypatch):
    up, store, pip_calls = _updater(monkeypatch, versions=("7.0.1",), index=(True, "7.0.1"))
    assert run(up.freshen_tiktoklive("comment-rejected"))["outcome"] == "no-update"
    assert pip_calls == [] and "tiktoklive_freshen_at" in store


def test_unreachable_index_is_a_failed_check_not_no_update(monkeypatch, capsys):
    """复查确认：索引连不上时 pip install -U 退回已装版本并返回 0，以前被记成「已是最新」、
    还进了六小时冷却。现在先问索引，连不上就是检查失败。"""
    up, store, pip_calls = _updater(monkeypatch, versions=("7.0.1",), index=(False, None))
    up._index_query_detail = "PyPI 的版本列表没拿到（HTTP 503）"
    result = run(up.freshen_tiktoklive("comment-rejected"))
    assert result["outcome"] == "pip-failed" and result["code"] == "index-query-failed"
    assert pip_calls == [] and "tiktoklive_freshen_at" not in store
    out = capsys.readouterr().out
    assert "HTTP 503" in out and "连不上" not in out      # 只记看到的，不下结论
    assert run(up.freshen_tiktoklive("comment-rejected"))["outcome"] == "recently-failed"


def test_index_versions_pick_the_newest_7x_released_a_week_ago(monkeypatch):
    """PyPI 的 JSON：只挑发布满 7 天的 7.x 正式版（含 .postN），不看 8.x、预发布、撤回的、刚发布的。
    以前问 pip index，只有版本号、没有发布时间；「pip 太旧不认 index」那条退路也跟着没了。"""
    from datetime import datetime, timezone

    now = 1_800_000_000.0

    def rel(days, yanked=False):
        when = datetime.fromtimestamp(now - days * 86400, tz=timezone.utc)
        return [{"yanked": yanked, "upload_time_iso_8601": when.strftime("%Y-%m-%dT%H:%M:%S.%fZ")}]

    up = updater_mod.Updater(server=None)
    replies = {}

    async def fake_pypi(dist):
        replies["dist"] = dist
        return replies["r"]

    monkeypatch.setattr(up, "_pypi_releases", fake_pypi)
    replies["r"] = ({"8.0.0": rel(30), "7.1.0b1": rel(30), "7.0.2": rel(30), "7.0.1": rel(60),
                     "7.0.3": rel(2), "7.0.4": rel(30, yanked=True)}, None)
    assert run(up._tiktoklive_index_versions(now=now)) == (True, "7.0.2")
    assert replies["dist"] == "TikTokLive"
    replies["r"] = ({"7.0.1.post1": rel(10), "7.0.1": rel(60)}, None)
    assert run(up._tiktoklive_index_versions(now=now)) == (True, "7.0.1.post1")
    replies["r"] = ({"8.0.0": rel(30), "7.0.5": rel(1)}, None)
    assert run(up._tiktoklive_index_versions(now=now)) == (True, None)
    replies["r"] = (None, "HTTP 503")
    assert run(up._tiktoklive_index_versions(now=now)) == (False, None)
    assert up._index_query_detail == "PyPI 的版本列表没拿到（HTTP 503）"


def test_week_old_means_a_full_seven_days(monkeypatch):
    """D9 的「发布满 7 天」按最早那个文件的上传时间算，差一秒都不算满。"""
    now = 1_800_000_000.0
    up = updater_mod.Updater(server=None)
    reply = {}

    async def fake_pypi(dist):
        return reply["r"], None

    def files(*ages):
        return [{"upload_time_iso_8601": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - age))}
                for age in ages]

    monkeypatch.setattr(up, "_pypi_releases", fake_pypi)
    week = updater_mod.COMPONENT_MIN_AGE_SEC
    assert week == 7 * 86400
    reply["r"] = {"7.0.2": files(week), "7.0.3": files(week - 1)}
    assert run(up._tiktoklive_index_versions(now=now)) == (True, "7.0.2")
    # 同一版本后来补传的文件不让它变「新」：看最早的那个
    reply["r"] = {"7.0.3": files(week - 1, week + 86400)}
    assert run(up._tiktoklive_index_versions(now=now)) == (True, "7.0.3")
    reply["r"] = {"7.0.3": files(week - 1, week - 60)}
    assert run(up._tiktoklive_index_versions(now=now)) == (True, None)


def test_index_versions_read_the_pypi_json(monkeypatch):
    """接上真的 _pypi_releases（只把 aiohttp 换成假的）：问的是 TikTokLive 的 JSON 接口；只记看到的
    （HTTP 状态码、超时），不替它下结论；老字段 upload_time 不带时区按 UTC 算。"""
    import aiohttp

    now = 1_800_000_000.0
    replies = {}

    class Resp:
        def __init__(self):
            self.status = replies["status"]

        async def json(self, content_type=None):
            return replies["json"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, url, **k):
            replies["url"] = url
            if replies.get("raise"):
                raise replies["raise"]
            return Resp()

    def iso(days):
        return time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime(now - days * 86400))

    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    up = updater_mod.Updater(server=None)
    old_field = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 9 * 86400))
    replies.update(status=200, json={"releases": {
        "7.0.1": [{"upload_time_iso_8601": iso(40), "yanked": False}],
        "7.0.2": [{"upload_time": old_field}],                               # 只有老字段
        "7.0.3": [{"upload_time_iso_8601": iso(3), "yanked": False}],        # 没满 7 天
        "7.1.0rc1": [{"upload_time_iso_8601": iso(20), "yanked": False}],    # 预发布
        "8.0.0": [{"upload_time_iso_8601": iso(20), "yanked": False}]}})     # 不是 7.x
    assert run(up._tiktoklive_index_versions(now=now)) == (True, "7.0.2")
    assert replies["url"] == "https://pypi.org/pypi/TikTokLive/json"
    # releases 被去掉时退一步用最新那一版（info.version + urls），照样按满 7 天挑
    replies.update(json={"info": {"version": "7.0.3"},
                         "urls": [{"upload_time_iso_8601": iso(3), "yanked": False}]})
    assert run(up._tiktoklive_index_versions(now=now)) == (True, None)
    replies.update(json={"info": {"version": "7.0.3"},
                         "urls": [{"upload_time_iso_8601": iso(8), "yanked": False}]})
    assert run(up._tiktoklive_index_versions(now=now)) == (True, "7.0.3")
    replies.update(status=503)
    assert run(up._tiktoklive_index_versions(now=now)) == (False, None)
    assert up._index_query_detail == "PyPI 的版本列表没拿到（HTTP 503）"
    replies.update(status=200, json=["not", "a", "dict"])
    assert run(up._tiktoklive_index_versions(now=now)) == (False, None)
    replies.update({"raise": asyncio.TimeoutError()})
    assert run(up._tiktoklive_index_versions(now=now)) == (False, None)
    assert up._index_query_detail == "PyPI 的版本列表没拿到（20 秒内没有返回）"


def test_monitoring_that_starts_while_pypi_answers_is_not_upgraded(monkeypatch, capsys):
    """问 PyPI 的那几秒里点了开始（或一键更新）：不装、不记冷却，下次再看。"""
    state = {"busy": False}
    up, store, pip_calls = _updater(monkeypatch, versions=("7.0.1", "7.0.1", "7.0.2"))
    up._stream_active = lambda: state["busy"]

    async def index():
        state["busy"] = True                      # 这时中控点了开始
        return True, "7.0.2"

    up._tiktoklive_index_versions = index
    assert run(up.freshen_tiktoklive("comment-rejected")) == {"outcome": "busy", "before": "7.0.1"}
    assert pip_calls == [] and "tiktoklive_freshen_at" not in store
    assert "这次不装" in capsys.readouterr().out
    state["busy"] = False

    async def idle_index():
        return True, "7.0.2"

    up._tiktoklive_index_versions = idle_index
    assert run(up.freshen_tiktoklive("comment-rejected"))["outcome"] == "upgraded"   # 没记失败，马上能再查
    assert pip_calls == [["--upgrade-strategy", "only-if-needed", "TikTokLive==7.0.2"]]


class _FakePip:
    """顶替 asyncio.create_subprocess_exec：记下起过的 pip，wait 可以等一个闸门，kill 只记一笔。"""

    def __init__(self):
        self.spawned, self.killed, self.gate = [], [], None
        fake = self

        class Proc:
            returncode = 0

            async def wait(self):
                if fake.gate is not None:
                    await fake.gate.wait()
                return 0

            def kill(self):
                fake.killed.append(1)

        self.Proc = Proc

    async def __call__(self, *args, **kwargs):
        self.spawned.append(args)
        return self.Proc()


def test_monitoring_that_starts_while_waiting_for_the_pip_lock_is_not_upgraded(monkeypatch, capsys):
    """第二道忙碌闸在拿到 pip 锁之后才看（#147 审查；和 freshen_ytdlp 一样）。以前问完 PyPI、在锁外面就看了：
    锁被别的 pip 占着（一键更新、后台装 GPU 组件）、排队的这段时间里点了开始，拿到锁照样装。
    这里用真的 _pip_install，只把起子进程那一步换成假的。"""
    state = {"busy": False}
    up, store, pip_calls = _updater(monkeypatch, versions=("7.0.1", "7.0.1", "7.0.2"),
                                    index=(True, "7.0.2"))
    up._stream_active = lambda: state["busy"]
    monkeypatch.setattr(up, "_pip_install", MethodType(updater_mod.Updater._pip_install, up))
    pip = _FakePip()
    monkeypatch.setattr(updater_mod.asyncio, "create_subprocess_exec", pip)

    async def scenario():
        lock = up._get_pip_lock()
        await lock.acquire()                                  # 别的 pip 正占着锁
        check = asyncio.ensure_future(up.freshen_tiktoklive("comment-rejected"))
        assert await wait_until(lambda: up._pip_tasks, limit=100)   # 问完 PyPI 了，在排队等锁
        state["busy"] = True                                  # 这时中控点了开始
        lock.release()
        return await check

    assert run(scenario()) == {"outcome": "busy", "before": "7.0.1"}
    assert pip.spawned == [] and "tiktoklive_freshen_at" not in store
    assert not up._get_pip_lock().locked()
    assert "这次不装" in capsys.readouterr().out
    # 没记失败：空闲下来马上能再查，照常装（锁是上一个事件循环里建的，换一把新的）
    state["busy"] = False
    up._pip_lock = None
    assert run(up.freshen_tiktoklive("comment-rejected"))["outcome"] == "upgraded"
    assert len(pip.spawned) == 1 and "TikTokLive==7.0.2" in pip.spawned[0]


def test_skip_if_is_asked_once_after_the_lock_and_never_stops_a_running_pip(monkeypatch):
    """skip_if 只在拿到 pip 锁之后问一次：等锁时不问（问早了就等于在锁外面看）；pip 跑起来以后不再问，
    不会因为它 kill 掉正在写环境的 pip（要中途叫停的是 abort_if）。

    轮询间隔调到 0.01 秒：默认 2 秒一问，假 pip 只跑 0.05 秒，pip 跑着时就算一直在问 skip_if 也问不到，
    这条测试钉不住「不再问、不 kill」（审查时把 skip_if 接到 abort_if 上还原过，默认间隔下照样全绿）。"""
    monkeypatch.setattr(updater_mod, "PIP_ABORT_POLL_SEC", 0.01)
    pip = _FakePip()
    monkeypatch.setattr(updater_mod.asyncio, "create_subprocess_exec", pip)
    up = updater_mod.Updater(server=None)
    asked = []
    answer = {"skip": False}

    def skip_if():
        asked.append(answer["skip"])
        return answer["skip"]

    async def scenario():
        pip.gate = asyncio.Event()
        lock = up._get_pip_lock()
        await lock.acquire()
        call = asyncio.ensure_future(up._pip_install(["TikTokLive==7.0.2"], "测试", skip_if=skip_if))
        await asyncio.sleep(0.05)
        asked_while_waiting = list(asked)
        lock.release()
        assert await wait_until(lambda: pip.spawned, limit=100)
        answer["skip"] = True                     # pip 已经在跑：这时开始监听，也不停它
        await asyncio.sleep(0.05)
        pip.gate.set()
        code = await call
        skipped = await up._pip_install(["TikTokLive==7.0.2"], "测试", skip_if=skip_if)
        return asked_while_waiting, code, skipped

    waiting, code, skipped = run(scenario())
    assert waiting == [] and code == 0 and pip.killed == []
    assert asked == [False, True]
    assert skipped == updater_mod.PIP_SKIPPED and len(pip.spawned) == 1
    assert not up._get_pip_lock().locked()


def test_newer_post_release_is_installed_not_skipped(monkeypatch):
    """第三轮核对确认：7(.N){1,2} 的正则会跳过 7.0.1.post1 这种补丁后续版。"""
    up, store, pip_calls = _updater(monkeypatch, versions=("7.0.1", "7.0.1.post1"),
                                    index=(True, "7.0.1.post1"))
    assert run(up.freshen_tiktoklive("comment-rejected"))["outcome"] == "upgraded"
    assert len(pip_calls) == 1


def test_failed_check_is_logged_and_does_not_start_the_six_hour_cooldown(monkeypatch, capsys):
    """审查确认：以前 pip 失败也先记了六小时冷却，还一句不说——网络一分钟后恢复，
    修复版本也要再等六小时。现在失败只进十分钟的内存重试间隔。"""
    up, store, pip_calls = _updater(monkeypatch, versions=("7.0.1", "7.0.1"), pip_code=1,
                                    index=(True, "7.0.2"))
    assert run(up.freshen_tiktoklive("comment-rejected"))["outcome"] == "pip-failed"
    assert "tiktoklive_freshen_at" not in store
    assert "没成功" in capsys.readouterr().out
    assert run(up.freshen_tiktoklive("comment-rejected"))["outcome"] == "recently-failed"
    assert len(pip_calls) == 1


def test_freshen_respects_the_cooldown_without_announcing(monkeypatch):
    announced = []

    async def announce():
        announced.append(1)

    up, store, pip_calls = _updater(
        monkeypatch, settings={"tiktoklive_freshen_at": time.time() - 60},
        versions=("7.0.1", "7.0.2"))
    assert run(up.freshen_tiktoklive("comment-rejected", announce=announce))["outcome"] == "cooldown"
    assert pip_calls == [] and announced == []


def test_freshen_does_nothing_when_not_installed(monkeypatch):
    up, store, pip_calls = _updater(monkeypatch, versions=(None,), installed=False)
    assert run(up.freshen_tiktoklive("comment-rejected"))["outcome"] == "not-installed"
    assert pip_calls == []


def test_announce_runs_before_the_check(monkeypatch):
    order = []

    async def announce():
        order.append("announce")

    up, store, pip_calls = _updater(monkeypatch, versions=("7.0.1", "7.0.1"), index=(True, "7.0.2"))
    real_pip = up._pip_install

    async def pip_after_announce(args, label, **kw):
        order.append("pip")
        return await real_pip(args, label, **kw)

    monkeypatch.setattr(up, "_pip_install", pip_after_announce)
    run(up.freshen_tiktoklive("comment-rejected", announce=announce))
    assert order == ["announce", "pip"]


def test_joiner_is_announced_when_it_joins_a_running_check(monkeypatch):
    """复查确认：第二场被拒时跟着等第一场的检查，以前它的面板一直停在「连接中」。"""
    gate = {}
    announced = []

    async def pip_gate():
        await gate["event"].wait()

    up, store, pip_calls = _updater(monkeypatch, versions=("7.0.1", "7.0.2"), pip_gate=pip_gate,
                                    index=(True, "7.0.2"))

    def announcer(tag):
        async def announce():
            announced.append(tag)
        return announce

    async def scenario():
        gate["event"] = asyncio.Event()
        first = asyncio.ensure_future(up.freshen_tiktoklive("a", announce=announcer("A")))
        await wait_until(lambda: pip_calls, limit=100)
        second = asyncio.ensure_future(up.freshen_tiktoklive("b", announce=announcer("B")))
        await wait_until(lambda: "B" in announced, limit=100)
        seen_before_release = list(announced)
        gate["event"].set()
        await first
        await second
        return seen_before_release

    assert run(scenario()) == ["A", "B"]


def test_joiner_queued_before_the_check_starts_is_announced_once(monkeypatch):
    """第三轮核对确认：旧测试中间 sleep 了，走的是「马上通知」那条路，排队这条没被测到。
    两个调用在同一轮事件循环里发起，第二个必然赶在检查开始之前。"""
    announced = []
    up, store, pip_calls = _updater(monkeypatch, versions=("7.0.1",), index=(True, "7.0.1"))

    def announcer(tag):
        async def announce():
            announced.append(tag)
        return announce

    async def scenario():
        first = asyncio.ensure_future(up.freshen_tiktoklive("a", announce=announcer("A")))
        second = asyncio.ensure_future(up.freshen_tiktoklive("b", announce=announcer("B")))
        return await first, await second

    a, b = run(scenario())
    assert announced == ["A", "B"] and a == b


def test_joiner_before_the_check_starts_is_announced_with_the_first(monkeypatch):
    gate = {}
    announced = []

    async def index_gate():
        await gate["event"].wait()

    up, store, pip_calls = _updater(monkeypatch, versions=("7.0.1",), index=(True, "7.0.1"),
                                    index_gate=index_gate)

    def announcer(tag):
        async def announce():
            announced.append(tag)
        return announce

    async def scenario():
        gate["event"] = asyncio.Event()
        first = asyncio.ensure_future(up.freshen_tiktoklive("a", announce=announcer("A")))
        await asyncio.sleep(0.05)
        second = asyncio.ensure_future(up.freshen_tiktoklive("b", announce=announcer("B")))
        await asyncio.sleep(0.05)
        gate["event"].set()
        return await first, await second

    a, b = run(scenario())
    assert sorted(announced) == ["A", "B"] and a == b


def test_concurrent_freshen_calls_share_one_check(monkeypatch):
    """审查确认：第二次被拒时第一次检查还没完，以前直接判「没有更新」再白等几分钟。"""
    gate = {}

    async def pip_gate():
        await gate["event"].wait()

    up, store, pip_calls = _updater(monkeypatch, versions=("7.0.1", "7.0.2"), pip_gate=pip_gate,
                                    index=(True, "7.0.2"))

    async def scenario():
        gate["event"] = asyncio.Event()
        first = asyncio.ensure_future(up.freshen_tiktoklive("a"))
        await asyncio.sleep(0.05)
        second = asyncio.ensure_future(up.freshen_tiktoklive("b"))
        await asyncio.sleep(0.05)
        gate["event"].set()
        return await first, await second

    a, b = run(scenario())
    assert a == b == {"outcome": "upgraded", "before": "7.0.1", "after": "7.0.2"}
    assert len(pip_calls) == 1


def test_cancelled_caller_does_not_release_the_pip_lock_before_pip_exits(monkeypatch):
    """审查确认：中控点停止会取消等待中的调用方，以前锁随之释放、pip 却还在写环境，
    一键更新的 pip 紧接着拿到锁并发写同一个 venv。"""
    spawned = []
    gate = {}

    class Proc:
        returncode = 0

        async def wait(self):
            await gate["event"].wait()
            return 0

        def kill(self):
            pass

    async def fake_exec(*args, **kwargs):
        spawned.append(args)
        return Proc()

    monkeypatch.setattr(updater_mod.asyncio, "create_subprocess_exec", fake_exec)
    up = updater_mod.Updater(server=None)

    async def scenario():
        gate["event"] = asyncio.Event()
        caller = asyncio.ensure_future(up._pip_install(["TikTokLive"], "测试"))
        await wait_until(lambda: spawned, limit=100)
        caller.cancel()
        try:
            await caller
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0.05)
        held_while_pip_runs = up._get_pip_lock().locked()
        gate["event"].set()
        await wait_until(lambda: not up._get_pip_lock().locked(), limit=100)
        return held_while_pip_runs, up._get_pip_lock().locked()

    held, after = run(scenario())
    assert held is True and after is False and len(spawned) == 1


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
    def __init__(self, status_code, msg=None):
        super().__init__("server rejected WebSocket connection: HTTP {}".format(status_code))
        self.status_code = status_code
        self.headers = {"Handshake-Msg": msg} if msg else {}


class InvalidStatus(Exception):              # 新版 websockets：状态码在 response 上
    def __init__(self, status_code):
        super().__init__("server rejected WebSocket connection: HTTP {}".format(status_code))
        self.response = SimpleNamespace(status_code=status_code)


def test_handshake_msg_is_read_from_the_headers(monkeypatch):
    """复查确认：7.0.0 的报错文本不带服务端的 Handshake-Msg，只有头里有。"""
    errors = _fake_errors(monkeypatch)
    from app import comment_worker
    exc = InvalidStatusCode(400, "invalid route params")
    assert comment_worker._handshake_info(exc) == (400, "invalid route params")
    try:
        try:
            raise InvalidStatus(403)
        except InvalidStatus as inner:
            raise errors.WebcastBlockedError("403") from inner
    except errors.WebcastBlockedError as wrapped:
        assert comment_worker._handshake_info(wrapped) == (403, None)
    assert comment_worker._handshake_fields(RuntimeError("x")) == {}
    assert comment_worker._handshake_fields(exc) == {"http_status": 400,
                                                     "handshake_msg": "invalid route params"}


def test_worker_run_reports_the_handshake_msg_when_rejected(monkeypatch, capsys):
    """第三轮核对确认：_run 里把 handshake_msg 带出来的那一行没有测试。"""
    _fake_errors(monkeypatch)
    from app import comment_worker

    class Client:
        def __init__(self, unique_id):
            self.room_id = None

        def on(self, _event):
            return lambda fn: fn

        async def is_live(self):
            raise InvalidStatusCode(400, "invalid route params")

        async def disconnect(self):
            return None

    events = ModuleType("TikTokLive.events")
    for name in ("CommentEvent", "ConnectEvent", "DisconnectEvent"):
        setattr(events, name, type(name, (), {}))
    sys.modules["TikTokLive"].TikTokLiveClient = Client
    monkeypatch.setitem(sys.modules, "TikTokLive.events", events)

    async def no_watch(*a, **k):
        return None

    monkeypatch.setattr(comment_worker, "_watch_parent", no_watch)
    code = run(comment_worker._run(SimpleNamespace(unique_id="abc", session_id=None,
                                                   tt_target_idc=None)))
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    rejected = [x for x in lines if x.get("state") == "rejected"]
    assert code == 8 and rejected
    assert rejected[0]["http_status"] == 400 and rejected[0]["handshake_msg"] == "invalid route params"


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
    comment_worker._status("rejected", "x", http_status=400, handshake_msg="bad params")
    comment_worker._status("connecting")
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[0]["http_status"] == 400 and lines[0]["handshake_msg"] == "bad params"
    assert "http_status" not in lines[1] and "handshake_msg" not in lines[1]


# ---------------------------------------------------------------------------
# 3. 监督循环：被拒先找更新，升级了立刻重连，没有就长等并说清结果
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
                      "http_status": 400, "handshake_msg": "invalid route params"})
BLOCKED_200 = jline({"event": "status", "state": "blocked",
                     "detail": 'WebcastBlockedError: rejected due to "illegal secret key"',
                     "http_status": 200})
LONG_WAIT = 5.0     # 「长等」设得远大于立刻重连所需的时间，Windows 计时粒度下也不会误判


def make_source(monkeypatch, procs, on_stale=None, versions=("7.0.1",), two_arg_state=False):
    state_log = []
    raw_log = []

    async def on_items(items):
        pass

    if two_arg_state:
        async def on_state(state, detail=""):
            state_log.append((state, detail))
    else:
        async def on_state(state, detail="", raw=""):
            state_log.append((state, detail))
            if raw:
                raw_log.append((state, detail, raw))

    cs = CommentSource(on_items=on_items, on_state=on_state, cookies_browser="none")
    for name, value in (("BACKOFF_MIN_SEC", 0.01), ("BACKOFF_MAX_SEC", 0.05),
                        ("HEALTHY_SEC", 0.02), ("OFFLINE_RETRY_SEC", 0.02),
                        ("SIGN_ERROR_WAIT_SEC", LONG_WAIT), ("BLOCKED_WAIT_SEC", LONG_WAIT),
                        ("REJECTED_WAIT_SEC", LONG_WAIT), ("MAX_CONNECTS_PER_HOUR", 30),
                        ("STOP_GRACE_SEC", 0.05), ("HOUR_WINDOW_SEC", 60.0),
                        ("PROVISION_POLL_SEC", 0.02)):
        monkeypatch.setattr(CommentSource, name, value)
    monkeypatch.setattr(cs_mod, "worker_available", lambda: True)
    seq = list(versions)
    monkeypatch.setattr(cs_mod, "_installed_tiktoklive",
                        lambda: seq.pop(0) if len(seq) > 1 else (seq[0] if seq else None))
    queue = list(procs)
    calls = []

    async def fake_spawn(self, args):
        calls.append(list(args))
        return queue.pop(0) if queue else FakeProc(hang_after=True)

    monkeypatch.setattr(CommentSource, "_spawn", fake_spawn)
    cs.on_stale = on_stale
    return cs, state_log, raw_log, calls


def make_stale(outcome, announce_first=True, before="7.0.0", after="7.0.1"):
    asked = []

    async def stale(reason, announce=None):
        asked.append(reason)
        if announce_first and announce is not None:
            await announce()
        result = {"outcome": outcome, "before": before}
        if outcome in ("upgraded", "no-update", "pip-failed"):
            result["after"] = after if outcome == "upgraded" else before
        return result

    return stale, asked


def _no_banned_labels(state_log):
    for _state, detail in state_log:
        for word in BANNED_LABELS:
            assert word not in detail, (word, detail)


def _run_until(cs, cond, limit=300, settle=0.0):
    async def scenario():
        cs.start("abc")
        ok = await wait_until(cond, limit=limit)
        if settle:
            await asyncio.sleep(settle)
        await cs.stop()
        return ok
    return run(scenario())


def test_rejected_then_upgraded_reconnects_immediately(monkeypatch):
    stale, asked = make_stale("upgraded")
    cs, state_log, raw_log, calls = make_source(
        monkeypatch, [FakeProc([REJECTED_400], returncode=8)], on_stale=stale)
    assert _run_until(cs, lambda: len(calls) >= 2, limit=100) is True
    assert asked == ["comment-rejected"]
    details = [d for _, d in state_log]
    assert any("正在检查" in d for d in details)
    assert any("7.0.0" in d and "7.0.1" in d for d in details)
    assert not any("InvalidStatusCode" in d for d in details)     # 原始英文报错没上面板
    assert any("InvalidStatusCode" in raw and "http_status=400" in raw
               and "handshake_msg=invalid route params" in raw for _, _, raw in raw_log)
    _no_banned_labels(state_log)


def test_rejected_without_update_waits_and_says_what_happened(monkeypatch):
    stale, asked = make_stale("no-update")
    cs, state_log, raw_log, calls = make_source(
        monkeypatch, [FakeProc([REJECTED_400], returncode=8)], on_stale=stale)
    assert _run_until(cs, lambda: any("分钟后自动重试" in d for _, d in state_log),
                      settle=0.2) is True
    assert len(calls) == 1                                           # 仍在长等窗口内
    final = [d for _, d in state_log if "分钟后自动重试" in d][-1]
    assert "HTTP 400" in final and "已是可用的最新版本" in final
    assert any("分钟后自动重试" in d and "http_status=400" in raw for _, d, raw in raw_log)
    _no_banned_labels(state_log)


def test_rejected_during_cooldown_does_not_claim_a_check(monkeypatch):
    """审查确认：以前每次被拒都先写「正在检查」，冷却期内其实什么都没查，日志会误导人。"""
    stale, asked = make_stale("cooldown", announce_first=False)
    cs, state_log, raw_log, calls = make_source(
        monkeypatch, [FakeProc([REJECTED_400], returncode=8)], on_stale=stale)
    assert _run_until(cs, lambda: any("分钟后自动重试" in d for _, d in state_log)) is True
    assert not any("正在检查" in d for _, d in state_log)
    assert any("近几个小时已检查过" in d for _, d in state_log)
    assert any("近几个小时已检查过" in d and "InvalidStatusCode" in raw for _, d, raw in raw_log)


def test_rejection_without_an_updater_still_records_the_raw_reason(monkeypatch):
    cs, state_log, raw_log, calls = make_source(
        monkeypatch, [FakeProc([REJECTED_400], returncode=8)], on_stale=None)
    assert _run_until(cs, lambda: any("分钟后自动重试" in d for _, d in state_log)) is True
    assert any("分钟后自动重试" in d and "http_status=400" in raw for _, d, raw in raw_log)


def test_rejected_while_monitoring_says_the_component_is_not_updated(monkeypatch):
    """专家审查 I5（D6）：监听中不更新弹幕组件——面板照实说这次没更新，不写「正在检查」。"""
    stale, asked = make_stale("busy", announce_first=False)
    cs, state_log, raw_log, calls = make_source(
        monkeypatch, [FakeProc([REJECTED_400], returncode=8)], on_stale=stale)
    assert _run_until(cs, lambda: any("分钟后自动重试" in d for _, d in state_log)) is True
    assert not any("正在检查" in d for _, d in state_log)
    final = [d for _, d in state_log if "分钟后自动重试" in d][-1]
    assert "监听中不更新弹幕组件" in final
    _no_banned_labels(state_log)


def test_failed_update_check_is_reported_on_the_panel(monkeypatch):
    stale, asked = make_stale("pip-failed")
    cs, state_log, raw_log, calls = make_source(
        monkeypatch, [FakeProc([REJECTED_400], returncode=8)], on_stale=stale)
    assert _run_until(cs, lambda: any("更新检查没成功" in d for _, d in state_log)) is True


def test_version_changed_since_spawn_reconnects_without_a_check(monkeypatch):
    """审查确认：启动时的升级在子进程已经起来之后才落地，被拒时版本其实已经是新的。"""
    stale, asked = make_stale("no-update")
    cs, state_log, raw_log, calls = make_source(
        monkeypatch, [FakeProc([REJECTED_400], returncode=8)], on_stale=stale,
        versions=("7.0.0", "7.0.1"))
    assert _run_until(cs, lambda: len(calls) >= 2, limit=100) is True
    assert asked == []
    assert any("已更新到 7.0.1" in d for _, d in state_log)


def test_rejected_status_with_unexpected_exit_code_maps_to_eight(monkeypatch):
    stale, asked = make_stale("no-update")
    cs, state_log, raw_log, calls = make_source(
        monkeypatch, [FakeProc([REJECTED_400], returncode=-9)], on_stale=stale)
    assert _run_until(cs, lambda: asked) is True


def test_freshen_callback_that_raises_is_treated_as_no_update(monkeypatch):
    async def stale(reason, announce=None):
        raise RuntimeError("pip exploded")

    cs, state_log, raw_log, calls = make_source(
        monkeypatch, [FakeProc([REJECTED_400], returncode=8)], on_stale=stale)
    assert _run_until(cs, lambda: any("分钟后自动重试" in d for _, d in state_log)) is True


def test_blocked_also_asks_for_an_update(monkeypatch):
    stale, asked = make_stale("upgraded", before="7.0.1", after="7.0.2")
    cs, state_log, raw_log, calls = make_source(
        monkeypatch, [FakeProc([BLOCKED_200], returncode=7)], on_stale=stale)
    assert _run_until(cs, lambda: len(calls) >= 2, limit=100) is True
    assert asked == ["comment-rejected"]
    assert not any("WebcastBlockedError" in d for _, d in state_log)
    assert any("illegal secret key" in raw for _, _, raw in raw_log)


def test_generic_error_gets_a_chinese_lead(monkeypatch):
    """面板只说程序在做什么，英文原文只进审计 raw（专家审查 U5）。以前这条断言钉的是中文说明后面括号里
    带着原文——中文界面的弹幕栏标题因此一直挂着半句英文。"""
    err = jline({"event": "status", "state": "error", "detail": "RuntimeError: boom"})
    cs, state_log, raw_log, calls = make_source(monkeypatch, [FakeProc([err], returncode=1)])
    assert _run_until(cs, lambda: any(d.startswith("评论连接出错") for _, d in state_log)) is True
    assert "评论连接出错，稍后自动重试" in [d for _, d in state_log]
    assert not any("RuntimeError" in d or "RuntimeError" in getattr(d, "en", "") for _, d in state_log)
    assert any("RuntimeError: boom" in raw for _, _, raw in raw_log)


def test_two_argument_state_callbacks_still_work(monkeypatch):
    stale, asked = make_stale("no-update")
    cs, state_log, raw_log, calls = make_source(
        monkeypatch, [FakeProc([REJECTED_400], returncode=8)], on_stale=stale, two_arg_state=True)
    assert _run_until(cs, lambda: any("分钟后自动重试" in d for _, d in state_log)) is True


def test_accepts_raw_detection():
    p = Pipeline.__new__(Pipeline)
    assert cs_mod._accepts_raw(p._publish_comment_source) is True
    assert cs_mod._accepts_raw(lambda state, detail="": None) is False

    def varargs(*a):
        return None

    assert cs_mod._accepts_raw(varargs) is True


# ---------------------------------------------------------------------------
# 4. 留证据：会话日志、开场版本、接线、自检
# ---------------------------------------------------------------------------

class FakeAudit:
    def __init__(self):
        self.records = []

    def comment_source(self, state, detail="", raw=""):
        self.records.append((state, detail, raw))


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
    first = FakeAudit()
    second = FakeAudit()
    p.audit = first

    async def scenario():
        await p._publish_comment_source("connecting", "")
        await p._publish_comment_source("connecting", "")          # 重复：只记一条
        await p._publish_comment_source("error", "评论服务拒绝了连接（HTTP 400）", "raw reason http_status=400")
        await p._publish_comment_source("connected", "")
        p.audit = second                                           # 换场
        await p._publish_comment_source("connected", "")          # 新一场照记
        p.audit = None
        await p._publish_comment_source("idle", "")               # 没有审计也不崩

    run(scenario())
    assert first.records == [("connecting", "", ""),
                             ("error", "评论服务拒绝了连接（HTTP 400）", "raw reason http_status=400"),
                             ("connected", "", "")]
    assert second.records == [("connected", "", "")]
    broadcasts = [m for m in p.server.messages if m["type"] == "comment_source"]
    assert len(broadcasts) == 6
    assert all("raw" not in m and "raw reason" not in m["detail"] for m in broadcasts)


def test_audit_log_writes_comment_source_records(tmp_path):
    log = audit_mod.AuditLog(room_url="https://www.tiktok.com/@x/live", log_dir=tmp_path)
    log.comment_source("error", "x" * 500, raw="y" * 500)
    log.comment_source("connected")
    log.close()
    rows = [json.loads(line) for line in Path(log.path).read_text(encoding="utf-8").splitlines()]
    recs = [r for r in rows if r["type"] == "comment_source"]
    assert recs[0]["state"] == "error" and len(recs[0]["detail"]) == 300 and len(recs[0]["raw"]) == 300
    assert recs[1]["raw"] == "" and recs[1]["at"]


def _real_pipeline(monkeypatch, tmp_path, version="7.0.1"):
    from app import settings
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    terms = tmp_path / "banned_terms.txt"
    terms.write_text("", encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms)
    monkeypatch.setattr(audit_mod, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(pipeline_mod, "_tiktoklive_version", lambda: version)
    monkeypatch.setattr(CommentSource, "start", lambda self, unique_id: None)

    async def fake_stop(self, _external=True):
        pass

    monkeypatch.setattr(CommentSource, "stop", fake_stop)
    args = SimpleNamespace(cookies=None, target="zh-CN", translator="none", source="es",
                           beam=5, context=False, asr_temperature=None, glossary=None,
                           backend="auto", model=None, device="auto", compute_type="auto",
                           denoise="off", banned_terms=None, comments=True)
    return Pipeline(args, StubServer())


def test_session_start_records_the_tiktoklive_version(monkeypatch, tmp_path):
    p = _real_pipeline(monkeypatch, tmp_path)

    async def scenario():
        await p._begin_session("https://www.tiktok.com/@abc/live")
        path = p.audit.path
        await p._end_session()
        return path

    path = run(scenario())
    start = json.loads(Path(path).read_text(encoding="utf-8").splitlines()[0])
    assert start["type"] == "session_start" and start["tiktoklive_version"] == "7.0.1"


def test_real_pipeline_wires_freshen_and_raw_evidence(monkeypatch, tmp_path):
    """审查确认：旧测试给自己写的 lambda 断言，真正的 __init__ 接线写错了也照样绿。"""
    p = _real_pipeline(monkeypatch, tmp_path)
    assert p.comment_source.on_stale("comment-rejected", None) is None     # updater 还没注入
    asked = []

    class FakeUpdater:
        async def freshen_tiktoklive(self, reason, announce=None):
            asked.append((reason, announce))
            return {"outcome": "upgraded", "before": "7.0.0", "after": "7.0.1"}

    p.updater = FakeUpdater()

    async def announce():
        pass

    result = run(p.comment_source.on_stale("comment-rejected", announce))
    assert result == {"outcome": "upgraded", "before": "7.0.0", "after": "7.0.1"}
    assert asked == [("comment-rejected", announce)]
    assert p.comment_source._on_state_takes_raw is True


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


def test_selfcheck_pip_command_is_valid_for_each_shell(monkeypatch):
    """复查确认：Windows 默认终端是 PowerShell，带引号路径开头的一行会报语法错误，必须加 &。"""
    monkeypatch.setattr(selfcheck.os, "name", "nt")
    win = selfcheck._pip_command("TikTokLive>=7.0.1,<8")
    assert "PowerShell" in win and "& \"" in win and "pip install -U" in win
    monkeypatch.setattr(selfcheck.os, "name", "posix")
    posix = selfcheck._pip_command("TikTokLive>=7.0.1,<8", upgrade=False)
    assert "& " not in posix and "pip install \"TikTokLive" in posix


def test_selfcheck_not_installed_row_states_the_real_rule(monkeypatch):
    monkeypatch.setattr(sys, "version_info", (3, 13, 0))
    monkeypatch.setattr(updater_mod, "tiktoklive_version", lambda: None)
    c = run(selfcheck.check_comments(_args()))
    assert "自动安装" not in c["detail"] + c["fix"]
    assert "一小时内只试一次" in c["fix"] and updater_mod.TIKTOKLIVE_SPEC in c["fix"]


def test_run_selfcheck_records_the_component_version(monkeypatch):
    """第三轮核对确认：记版本那一行删掉测试照样全过（旧测试手动设了这个值）。"""
    p = Pipeline.__new__(Pipeline)
    p.server = StubServer()
    p.args = SimpleNamespace()
    p.detector = p.glossary = p.translator = None
    monkeypatch.setattr(pipeline_mod, "_tiktoklive_version", lambda: "7.0.1")

    async def fake_run_all(*a, **k):
        return []

    monkeypatch.setattr(selfcheck, "run_all", fake_run_all)
    run(p.run_selfcheck())
    assert p._selfcheck_tiktoklive == "7.0.1"


def test_selfcheck_row_refreshes_on_any_state_not_only_connected(monkeypatch):
    """第三轮核对确认：装上了但主播没开播、升级后仍被拒，状态不会到「已连接」。"""
    p = Pipeline.__new__(Pipeline)
    p.server = StubServer()
    p.audit = None
    p._bg_tasks = set()
    runs = []

    async def run_selfcheck():
        runs.append(1)

    p.run_selfcheck = run_selfcheck
    p._selfcheck_tiktoklive = None
    monkeypatch.setattr(pipeline_mod, "_tiktoklive_version", lambda: "7.0.1")

    async def scenario():
        await p._publish_comment_source("offline", "主播未开播")
        await p._publish_comment_source("offline", "主播未开播")
        await asyncio.sleep(0.01)

    run(scenario())
    assert runs == [1] and p._selfcheck_tiktoklive == "7.0.1"


def test_selfcheck_is_rerun_when_the_component_changed_since_the_last_check(monkeypatch):
    """复查确认：被拒时升级了组件，自检那一行一直停在旧版本。"""
    p = Pipeline.__new__(Pipeline)
    p.server = StubServer()
    p.audit = None
    p._bg_tasks = set()
    runs = []

    async def run_selfcheck():
        runs.append(1)

    p.run_selfcheck = run_selfcheck
    p._selfcheck_tiktoklive = "7.0.0"
    monkeypatch.setattr(pipeline_mod, "_tiktoklive_version", lambda: "7.0.1")

    async def scenario():
        await p._publish_comment_source("connecting", "")
        await p._publish_comment_source("connected", "")
        await asyncio.sleep(0.01)

    run(scenario())
    assert runs == [1]
    p._selfcheck_tiktoklive = "7.0.1"
    runs.clear()
    run(scenario())
    assert runs == []


def test_selfcheck_outdated_row_states_facts_not_an_upgrade_in_progress(monkeypatch):
    """审查确认：以前写「程序正在后台自动升级」，升级失败或在冷却期时这句话是假的。"""
    monkeypatch.setattr(sys, "version_info", (3, 13, 0))
    monkeypatch.setattr(updater_mod, "tiktoklive_version", lambda: "7.0.0")
    c = run(selfcheck.check_comments(_args()))
    assert "正在" not in c["detail"]
    assert "一小时内只试一次" in c["fix"] and "pip install -U" in c["fix"]
    assert updater_mod.TIKTOKLIVE_SPEC in c["fix"]


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
