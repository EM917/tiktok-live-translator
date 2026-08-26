"""restore_engine：界面里选过的翻译引擎，重启后必须还在。

曾经只有 save_setting("translator", ...) 而没有任何读回——用户在页面选了
DeepL，重启后被静默重置回 auto，一整场直播跑在 1.8B 上（2026-08-26 实测）。
"""
from app.translator import restore_engine


def _key(value):
    return lambda env: value


def test_cli_choice_beats_saved_setting():
    assert restore_engine("google", "deepl", key_lookup=_key(None)) == ("google", None)


def test_saved_engine_restored_when_key_present():
    assert restore_engine(None, "deepl", key_lookup=_key("sk")) == ("deepl", None)


def test_saved_engine_without_key_falls_back_with_warning():
    """密钥没了不能照单恢复：带密钥的引擎构造时抛错会让程序直接退出，
    而重新填密钥恰恰得先把界面打开。"""
    engine, warn = restore_engine(None, "deepl", key_lookup=_key(None))
    assert engine == "auto"
    assert warn and "deepl" in warn


def test_keyless_engine_needs_no_lookup():
    assert restore_engine(None, "hymt2", key_lookup=_key(None)) == ("hymt2", None)


def test_nothing_saved_means_auto():
    assert restore_engine(None, None, key_lookup=_key("sk")) == ("auto", None)


def test_garbage_setting_falls_back_to_auto():
    """settings.json 是用户可编辑的文件，写坏了不能让启动炸掉。"""
    for junk in ("DEEPL", "banana", 42, {"a": 1}):
        assert restore_engine(None, junk, key_lookup=_key("sk")) == ("auto", None)


def test_hand_edited_whitespace_tolerated():
    assert restore_engine(None, "deepl\n", key_lookup=_key("sk")) == ("deepl", None)


# ---- 回退提示必须到界面上：窗口应用没有可见终端，只 print 用户看不见 ----

def _run(coro):
    import asyncio
    loop = asyncio.get_event_loop_policy().new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()          # 不关的话 GC 时会往 stderr 吐 fd 报错噪音


def _pipeline(monkeypatch, tmp_path, note):
    from types import SimpleNamespace

    from app import settings as S
    from app.pipeline import Pipeline

    monkeypatch.setattr(S, "SETTINGS_FILE", tmp_path / "settings.json")

    class FakeServer:
        def __init__(self):
            self.sent = []
            self.config = {}

        async def broadcast(self, msg):
            self.sent.append(msg)

        async def status(self, *a, **k):
            pass

    p = Pipeline.__new__(Pipeline)
    p.server = FakeServer()
    p.translator = None
    p.args = SimpleNamespace(translator="auto", translator_note=note)
    return p


def test_fallback_note_reaches_engine_panel(monkeypatch, tmp_path):
    p = _pipeline(monkeypatch, tmp_path, note="deepl 还没有密钥，本次先用 auto")
    _run(p._publish_engine())
    assert p.server.sent[-1]["note"] == "deepl 还没有密钥，本次先用 auto"
    assert p.server.config["engine"]["note"]        # 后连上的页面也要看到


def test_note_cleared_after_user_picks_an_engine(monkeypatch, tmp_path):
    """用户亲手换过引擎后，启动时的回退提示就不再适用了。"""
    p = _pipeline(monkeypatch, tmp_path, note="旧提示")
    monkeypatch.setattr("app.pipeline.create_translator",
                        lambda name: type("X", (), {"name": name})())
    _run(p.set_engine("hymt2"))
    assert p.args.translator_note is None
    assert p.server.sent[-1].get("note") is None
