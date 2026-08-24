"""本地翻译模型的自动就绪。

存在的理由：没装 Ollama 的机器会退回 Google 免费接口（按 IP 限流，长时间监听
经常整段翻译失败），而原来的提示是让用户自己去装 Ollama、再敲一行 ollama pull。
对连终端是什么都不知道的人来说，那条本地翻译的路是永远走不通的。
"""
import asyncio
import sys

from app import localmodel


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_install_hint_is_honest_per_platform(monkeypatch):
    """Windows 的安装器要管理员权限，程序代劳不了——就得说清楚，不能含糊。"""
    monkeypatch.setattr(sys, "platform", "win32")
    text, url = localmodel.install_hint()
    assert "管理员" in text and "无法代劳" in text
    assert url and url.endswith(".exe")

    monkeypatch.setattr(sys, "platform", "darwin")
    text, url = localmodel.install_hint()
    assert "应用程序" in text
    assert url and url.endswith(".zip")


def test_install_hint_never_tells_the_user_to_run_ollama_pull(monkeypatch):
    """模型现在由程序自己拉。提示里再出现 ollama pull 就是回到老路上了。"""
    for platform in ("darwin", "win32", "linux"):
        monkeypatch.setattr(sys, "platform", platform)
        text, _ = localmodel.install_hint()
        assert "ollama pull" not in text, platform


def test_start_gives_up_when_not_installed(monkeypatch):
    monkeypatch.setattr(localmodel, "find_binary", lambda: None)

    async def not_running(timeout=2):
        return False

    monkeypatch.setattr(localmodel, "is_running", not_running)
    assert run(localmodel.start(timeout=0.1)) is False


def test_start_is_a_noop_when_already_running(monkeypatch):
    calls = []
    monkeypatch.setattr(localmodel, "find_binary", lambda: calls.append(1))

    async def running(timeout=2):
        return True

    monkeypatch.setattr(localmodel, "is_running", running)
    assert run(localmodel.start()) is True
    assert not calls          # 已经在跑就别再启动一次


def test_base_url_honours_the_environment(monkeypatch):
    monkeypatch.setenv("OLLAMA_URL", "http://elsewhere:1234/")
    assert localmodel.base_url() == "http://elsewhere:1234"
