"""界面里填 API 密钥。

存在的理由：`--translator deepl` 加环境变量要开终端，而这个工具的用户里有
连终端是什么都不知道的人——对他们来说「设个环境变量」等于这个功能不存在。

安全上的硬要求：密钥存在本机的 settings.json（已在 .gitignore 里），
**从不出现在任何一条发给页面的消息里**，回传的只有打码后的尾四位。
"""
import asyncio

from app import translator as T
from app.pipeline import Pipeline

KEY = "0123456789abcdef:fx"


class FakeServer:
    def __init__(self):
        self.sent = []
        self.config = {}

    async def broadcast(self, msg):
        self.sent.append(msg)

    async def status(self, *a, **k):
        pass


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def make(monkeypatch, tmp_path):
    from app import settings as S

    monkeypatch.setattr(S, "SETTINGS_FILE", tmp_path / "settings.json")
    p = Pipeline.__new__(Pipeline)
    p.server = FakeServer()
    p.translator = None
    from types import SimpleNamespace
    p.args = SimpleNamespace(translator="auto")
    return p


def test_the_key_is_stored_but_never_broadcast(monkeypatch, tmp_path):
    p = make(monkeypatch, tmp_path)
    monkeypatch.setattr(T, "create_translator",
                        lambda name: type("X", (), {"name": name})())
    monkeypatch.setattr("app.pipeline.create_translator",
                        lambda name: type("X", (), {"name": name})())
    run(p.set_engine("deepl", KEY))

    from app.settings import load_settings
    assert load_settings()["api_keys"]["DEEPL_API_KEY"] == KEY   # 存下来了

    blob = repr(p.server.sent) + repr(p.server.config)
    assert KEY not in blob, "密钥出现在了发给页面的消息里"
    assert "…f:fx" in blob or "…" in blob                        # 只给尾四位


def test_translator_reads_the_stored_key(monkeypatch, tmp_path):
    from app import settings as S

    monkeypatch.setattr(S, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.delenv("DEEPL_API_KEY", raising=False)
    S.save_setting("api_keys", {"DEEPL_API_KEY": KEY})
    assert T.api_key("DEEPL_API_KEY") == KEY


def test_the_environment_variable_wins(monkeypatch, tmp_path):
    """环境变量是进阶用法，填了就该压过界面里存的那个。"""
    from app import settings as S

    monkeypatch.setattr(S, "SETTINGS_FILE", tmp_path / "settings.json")
    S.save_setting("api_keys", {"DEEPL_API_KEY": "stored"})
    monkeypatch.setenv("DEEPL_API_KEY", "from-env")
    assert T.api_key("DEEPL_API_KEY") == "from-env"


def test_masking_never_reveals_the_key():
    assert T.mask_key(KEY) == "…f:fx"
    assert KEY not in T.mask_key(KEY)
    assert T.mask_key("") == ""


def test_an_unknown_engine_is_ignored(monkeypatch, tmp_path):
    p = make(monkeypatch, tmp_path)
    run(p.set_engine("definitely-not-an-engine", "x"))
    assert p.server.sent == []
