"""updater：版本号解析与比较——排序错了更新提示就会失灵。"""
import asyncio

import pytest

from app import updater as updater_mod
from app.updater import Updater, _parse


@pytest.mark.parametrize("raw,expect", [
    ("v0.8.0", (0, 8, 0)),
    ("0.8.0", (0, 8, 0)),
    ("V1.2.3", (1, 2, 3)),
    ("0.8", (0, 8, 0)),            # 缺段补零
    ("1.2.3-beta", (1, 2, 3)),     # 段内非数字字符剔除
    ("1.2.3.4", (1, 2, 3)),        # 超过三段截断
    ("", (0, 0, 0)),
    ("garbage", (0, 0, 0)),
])
def test_parse(raw, expect):
    assert _parse(raw) == expect


def test_numeric_not_lexicographic_ordering():
    # 字典序会把 0.10.0 排在 0.9.9 前面——必须是数值比较
    assert _parse("v0.10.0") > _parse("v0.9.9")
    assert _parse("v1.0.0") > _parse("v0.99.99")


def test_equal_versions_do_not_trigger_update():
    # check_and_notify 的判定是 tag <= local 即不提示
    assert not (_parse("v0.8.0") > _parse("0.8.0"))


class _FakeInstallProc:
    """假的 pip install 子进程：只用来验证 kill() 之后有没有被 reap。"""

    def __init__(self):
        self.kill_called = False
        self.wait_called = 0

    async def wait(self):
        self.wait_called += 1
        return 0

    def kill(self):
        self.kill_called = True


def test_ensure_tiktoklive_reaps_process_after_timeout_kill(monkeypatch):
    """pip 卡死超时后 kill() 必须配一次 wait() 去 reap，否则子进程变成僵尸
    进程残留——这条路径可能被弹幕来源的 on_provision 回调反复触发。"""
    monkeypatch.setattr(updater_mod, "load_settings", lambda: {})
    monkeypatch.setattr(updater_mod, "save_setting", lambda *a, **k: None)
    monkeypatch.setattr(updater_mod.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(updater_mod.sys, "version_info", (3, 13, 0))

    fake_proc = _FakeInstallProc()

    async def fake_create_subprocess_exec(*args, **kwargs):
        return fake_proc

    async def fake_wait_for(aw, timeout):
        if hasattr(aw, "close"):
            aw.close()          # 避免「协程从未被 await」的警告
        raise asyncio.TimeoutError()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)

    up = Updater(server=None)
    ok = asyncio.run(up.ensure_tiktoklive("test"))

    assert ok is False
    assert fake_proc.kill_called is True
    assert fake_proc.wait_called == 1
