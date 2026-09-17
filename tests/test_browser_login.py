"""借浏览器里的 TikTok 登录：读不到时要分清是哪一种读不到。

2026-09-17 实测（macOS 27）：Chrome 和 Safari 都是**系统拒绝读取**，两个借登录的层
记下的却都是「no_cookie」。这里每个用例都用假的读取函数、临时目录里自己建的库，
不读真实浏览器数据、不解密、不连网。

SENTINEL 是假 cookie 的值：它只许出现在发给 TikTok 的 Cookie 头里，任何 why、print、
审计记录、异常文本里出现都算泄漏。
"""
import asyncio
import json
import os
import sqlite3
import struct
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import browser_login as bl
from app import pipeline as pipeline_mod
from app import resolver, selfcheck
from app.pipeline import Pipeline, browser_only_message

SENTINEL = "S3NTINEL-c00kie-VALUE-9f2e"
GUESSED_LABELS = ("年龄", "限流", "封禁", "多半")


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class FakeCookie:
    def __init__(self, domain, name, value=SENTINEL):
        self.domain, self.name, self.value = domain, name, value


def _mac_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(bl, "_home", lambda: str(home))
    return home


def _chrome_dir(home):
    d = home / "Library" / "Application Support" / "Google" / "Chrome"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _refuse_listing(monkeypatch, refused):
    real = os.listdir

    def listdir(path="."):
        if str(path) == str(refused):
            raise PermissionError(1, "Operation not permitted", str(path))
        return real(path)

    monkeypatch.setattr(os, "listdir", listdir)


class _Calls(list):
    """假读取函数被叫到的浏览器，按顺序；profiles 是每次收到的 profile= 参数。"""

    def __init__(self):
        super().__init__()
        self.profiles = []


def _extractor(monkeypatch, behaviour):
    """behaviour：{浏览器: 异常实例 或 cookie 列表}。"""
    calls = _Calls()

    def extract(browser, profile=None):
        calls.append(browser)
        calls.profiles.append(profile)
        got = behaviour[browser]
        if isinstance(got, BaseException):
            raise got
        return got

    monkeypatch.setattr(bl, "_load_extractor", lambda: extract)
    return calls


def _chrome_not_found():
    # yt-dlp 2026.08.19 的原话：它列不了目录，于是「找不到 cookie 库」
    return FileNotFoundError('could not find chrome cookies database in '
                             '"/Users/x/Library/Application Support/Google/Chrome"')


def _safari_denied():
    return PermissionError(1, "Operation not permitted",
                           "/Users/x/Library/Containers/com.apple.Safari/Data/Library/"
                           "Cookies/Cookies.binarycookies")


# ---- 一个代码一条 --------------------------------------------------------

def test_safari_permission_error_is_blocked_by_system(monkeypatch, tmp_path):
    _mac_home(monkeypatch, tmp_path)
    _extractor(monkeypatch, {"safari": _safari_denied()})
    assert bl.read_login("safari") == bl.LoginRead(None, "blocked_by_system")


def test_macos27_chrome_shape_is_blocked_by_system(monkeypatch, tmp_path):
    """读取函数说「找不到 cookie 库」，而数据目录明明在、只是系统不让列——
    这是系统拒绝，不是没装、更不是没有 cookie。"""
    home = _mac_home(monkeypatch, tmp_path)
    _refuse_listing(monkeypatch, _chrome_dir(home))
    _extractor(monkeypatch, {"chrome": _chrome_not_found()})
    assert bl.read_login("chrome") == bl.LoginRead(None, "blocked_by_system")


def test_missing_store_is_no_browser_data(monkeypatch, tmp_path):
    home = _mac_home(monkeypatch, tmp_path)
    _extractor(monkeypatch, {"chrome": _chrome_not_found()})
    assert bl.read_login("chrome").code == "no_browser_data"      # 目录不存在
    _chrome_dir(home)
    assert bl.read_login("chrome").code == "no_browser_data"      # 目录在、列得了、没有库


def test_readable_store_without_tiktok_is_no_tiktok_cookie(monkeypatch, tmp_path):
    _mac_home(monkeypatch, tmp_path)
    _extractor(monkeypatch, {"chrome": [FakeCookie(".example.com", "sessionid")]})
    assert bl.read_login("chrome") == bl.LoginRead(None, "no_tiktok_cookie")


def test_tiktok_cookies_without_login_names_is_not_logged_in(monkeypatch, tmp_path):
    """没登录时 tiktok.com 的 cookie 照旧带上去（以前就是这样），只是代码说清楚没登录。"""
    _mac_home(monkeypatch, tmp_path)
    _extractor(monkeypatch, {"chrome": [FakeCookie(".tiktok.com", "ttwid", "w"),
                                        FakeCookie(".tiktok.com", "msToken", "m")]})
    assert bl.read_login("chrome") == bl.LoginRead("ttwid=w; msToken=m", "not_logged_in")


@pytest.mark.parametrize("name", bl.LOGIN_COOKIE_NAMES)
def test_login_cookie_builds_the_same_header_as_before(monkeypatch, tmp_path, name):
    _mac_home(monkeypatch, tmp_path)
    _extractor(monkeypatch, {"chrome": [FakeCookie(".tiktok.com", "ttwid", "w"),
                                        FakeCookie(".other.com", "x", "y"),
                                        FakeCookie("www.tiktok.com", name, "v")]})
    assert bl.read_login("chrome") == bl.LoginRead("ttwid=w; {}=v".format(name), "ok")


def test_anything_else_keeps_only_the_exception_class(monkeypatch, tmp_path):
    _mac_home(monkeypatch, tmp_path)
    _extractor(monkeypatch, {"chrome": RuntimeError("decrypt failed near " + SENTINEL)})
    got = bl.read_login("chrome")
    assert got == bl.LoginRead(None, "error:RuntimeError")
    assert SENTINEL not in repr(got)


def test_read_that_outlives_the_budget_is_keychain_wait(monkeypatch):
    monkeypatch.setattr(resolver, "BROWSER_ATTEMPT_TIMEOUT", 0.05)

    def slow(browser):
        time.sleep(0.4)
        return bl.LoginRead("sessionid=" + SENTINEL, "ok")

    monkeypatch.setattr(resolver, "_read_login", slow)

    async def go():
        note = resolver._fresh_note()
        seen_token = resolver._LOGIN_OBS.set({})
        try:
            header = await resolver._cookie_header_with_budget("chrome")
            return header, note, dict(resolver._LOGIN_OBS.get())
        finally:
            resolver._LOGIN_OBS.reset(seen_token)

    header, note, seen = asyncio.run(go())
    assert header is None
    assert note["why"] == ["chrome: keychain_wait"]
    assert seen == {"chrome": "keychain_wait"}


def test_nothing_real_is_read_under_pytest():
    """不 monkeypatch 的话，测试里既不调读取函数，也不去找真实的用户目录。"""
    assert bl._load_extractor() is None and bl._home() is None
    assert bl.read_login("chrome") == bl.LoginRead(None, "not_read")
    assert bl.probe("chrome") == "not_read" and bl.probe("safari") == "not_read"


@pytest.mark.parametrize("stderr,expected", [
    ("ERROR: [Errno 1] Operation not permitted: '/Users/x/Library/Containers/"
     "com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies'", "blocked_by_system"),
    ("ERROR: [tiktok:live] x: The channel is not currently live", None),
    ("", None),
])
def test_ytdlp_stderr_classification(monkeypatch, tmp_path, stderr, expected):
    _mac_home(monkeypatch, tmp_path)
    assert bl.classify_stderr("safari", stderr) == expected


def test_ytdlp_stderr_could_not_find_database_checks_the_directory(monkeypatch, tmp_path):
    home = _mac_home(monkeypatch, tmp_path)
    line = 'ERROR: could not find chrome cookies database in "{}"'.format(home)
    assert bl.classify_stderr("chrome", line) == "no_browser_data"
    _refuse_listing(monkeypatch, _chrome_dir(home))
    assert bl.classify_stderr("chrome", line) == "blocked_by_system"


# ---- 整条解析：代码进 trace、进 ResolveError.login，值哪里都不进 -------------

_GATED = {"status_code": 4003110, "data": {"prompts": ""}}


class _FakeResponse:
    status, charset = 200, "utf-8"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _wire_resolver(monkeypatch, ytdlp_stderr):
    """真的 _resolve_via_api / _resolve_from_page / _cookie_header_with_budget，
    假的网络：接口一直回 4003110，直播页不带流地址。返回发给 TikTok 的 Cookie 头列表。"""
    sent = []

    class FakeSession:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, url, headers=None, **kwargs):
            sent.append((headers or {}).get("Cookie"))
            return _FakeResponse()

    async def room_status(_session, _user):
        return "123", 2

    async def get_json(_session, _url, limit=None, headers=None):
        sent.append((headers or {}).get("Cookie"))
        return _GATED

    async def read_all(_resp, _limit):
        return b"<html>no stream address in this page</html>"

    async def ytdlp(_url, cookies=None, browser=None, timeout=None):
        return 1, "", ytdlp_stderr.get(browser, "ERROR: The channel is not currently live")

    import aiohttp

    from app import nethttp
    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(nethttp, "read_all", read_all)
    monkeypatch.setitem(sys.modules, "yt_dlp", types.ModuleType("yt_dlp"))
    monkeypatch.setattr(resolver, "_room_status", room_status)
    monkeypatch.setattr(resolver, "_get_json", get_json)
    monkeypatch.setattr(resolver, "_webkit_available", lambda: False)
    monkeypatch.setattr(resolver, "_run_ytdlp", ytdlp)
    monkeypatch.setattr(resolver, "_browser_order", lambda pref: ("chrome", "safari"))
    monkeypatch.setattr(resolver, "_remember_browser", lambda browser: None)
    return sent


def _resolve_and_fail(trace):
    with pytest.raises(resolver.ResolveError) as exc:
        run(resolver.resolve_stream_url("https://www.tiktok.com/@x/live", trace=trace))
    return exc.value


def test_macos27_run_records_blocked_by_system_in_both_login_layers(monkeypatch, tmp_path,
                                                                     capsys):
    """2026-09-17 那次的原样重放：Chrome 找不到库 + 目录不让列，Safari PermissionError。"""
    home = _mac_home(monkeypatch, tmp_path)
    _refuse_listing(monkeypatch, _chrome_dir(home))
    _extractor(monkeypatch, {"chrome": _chrome_not_found(), "safari": _safari_denied()})
    _wire_resolver(monkeypatch, {
        "chrome": 'ERROR: could not find chrome cookies database in "{}"'.format(home),
        "safari": "ERROR: [Errno 1] Operation not permitted: '/Users/x/Library/Containers/"
                  "com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies'"})
    trace = []
    err = _resolve_and_fail(trace)
    assert err.kind == "browser_only"
    assert err.login == {"chrome": "blocked_by_system", "safari": "blocked_by_system"}
    why = {r["layer"]: r.get("why", "") for r in trace}
    for layer in ("官方接口", "yt-dlp借cookie", "直播页兜底"):
        assert "chrome: blocked_by_system" in why[layer], (layer, why)
        assert "safari: blocked_by_system" in why[layer], (layer, why)
    assert "no_cookie" not in json.dumps(trace, ensure_ascii=False)
    assert "no_cookie" not in capsys.readouterr().out


def test_borrowed_login_is_sent_to_tiktok_and_nowhere_else(monkeypatch, tmp_path, capsys):
    _mac_home(monkeypatch, tmp_path)
    jar = [FakeCookie(".tiktok.com", "sessionid"), FakeCookie(".tiktok.com", "ttwid")]
    _extractor(monkeypatch, {"chrome": jar, "safari": RuntimeError("bad " + SENTINEL)})
    sent = _wire_resolver(monkeypatch, {})
    trace = []
    err = _resolve_and_fail(trace)
    # 登录照旧带给了 TikTok（接口层和直播页层各一次）……
    header = "sessionid={0}; ttwid={0}".format(SENTINEL)
    assert sent.count(header) == 2
    # ……而且只到 TikTok：代码、why、异常、终端输出里都没有值
    assert err.kind == "browser_only"
    assert err.login == {"chrome": "ok", "safari": "error:RuntimeError"}
    everything = "\n".join([json.dumps(trace, ensure_ascii=False), str(err), repr(err),
                            repr(err.login), json.dumps(err.login)])
    captured = capsys.readouterr()
    assert SENTINEL not in everything
    assert SENTINEL not in captured.out and SENTINEL not in captured.err
    assert "chrome: ok" in [r for r in trace if r["layer"] == "直播页兜底"][0]["why"]


def test_page_layer_note_carries_the_code(monkeypatch, tmp_path):
    _mac_home(monkeypatch, tmp_path)
    _extractor(monkeypatch, {"safari": _safari_denied()})

    async def go():
        note = resolver._fresh_note()
        got = await resolver._resolve_from_page("https://www.tiktok.com/@x/live",
                                                browser="safari")
        return got, note

    got, note = asyncio.run(go())
    assert got == (None, False)
    assert note["why"] == ["safari: blocked_by_system"]


# ---- 以 browser_only 收场时给中控的话 -------------------------------------

_FIXED_FACTS = ("代码 4003110", "已自动重试 3 次", "不是网络或限流", "原因 TikTok 不说明")
_PASTE = "把直播间链接和浏览器里的 .flv 地址一起粘进来"


def _advice(message):
    """固定话术之间新接上的那一段。"""
    return message.split("有时整场都不给。", 1)[1].split("想现在就看", 1)[0]


@pytest.mark.parametrize("login,must_have,must_not_have", [
    ({"chrome": "blocked_by_system", "safari": "blocked_by_system"},
     ["Chrome：系统拒绝读取；Safari：系统拒绝读取", bl.FDA_OBSERVED + bl.fda_steps()],
     [bl.LOGIN_STEPS]),
    ({"chrome": "not_logged_in"}, ["Chrome：能读取，没有 TikTok 登录 cookie", bl.LOGIN_STEPS],
     ["完全磁盘访问权限"]),
    ({"chrome": "no_tiktok_cookie"}, ["里面没有 tiktok.com 的 cookie", bl.LOGIN_STEPS],
     ["完全磁盘访问权限"]),
    ({"chrome": "blocked_by_system", "safari": "not_logged_in"},
     [bl.fda_steps(), bl.LOGIN_STEPS], []),
    ({"chrome": "ok", "safari": "blocked_by_system"},
     ["已借用 Chrome 里的 TikTok 登录再试，TikTok 仍然没有给出流地址。"],
     ["完全磁盘访问权限", bl.LOGIN_STEPS]),
    ({"chrome": "keychain_wait"}, ["Chrome：读取在限时内没有返回", bl.KEYCHAIN_STEPS], []),
    ({"chrome": "error:RuntimeError"}, ["Chrome：读取出错（RuntimeError）"], []),
])
def test_browser_only_message_matches_what_was_observed(login, must_have, must_not_have):
    message = browser_only_message(3, login)
    for fact in _FIXED_FACTS + (_PASTE,):
        assert fact in message
    for text in must_have:
        assert text in message, text
    for text in must_not_have:
        assert text not in message, text
    assert not any(word in _advice(message) for word in GUESSED_LABELS)


def test_browser_only_message_without_observations_is_the_old_text():
    assert _advice(browser_only_message(3, None)) == ""
    assert _advice(browser_only_message(3, {"chrome": "not_read"})) == ""


class _StubServer:
    def __init__(self):
        self.config, self.messages = {}, []

    async def status(self, state, detail=""):
        self.messages.append(detail)

    async def broadcast(self, msg):
        self.messages.append(msg)


class _StubAudit:
    def __init__(self):
        self.records = []

    def resolve(self, record):
        self.records.append(record)


def test_pipeline_passes_the_observation_through_not_the_text(monkeypatch, tmp_path):
    """resolver 把观察挂在 ResolveError.login 上；pipeline 只看它，不解析错误文本。"""
    from app import settings
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    terms = tmp_path / "banned_terms.txt"
    terms.write_text("", encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms)
    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source="es", beam=5, context=False,
        asr_temperature=None, glossary=None, backend="auto", model=None, device="auto",
        compute_type="auto", denoise="off", banned_terms=None)
    p = Pipeline(args, _StubServer())
    p.BROWSER_ONLY_RETRY_SEC = 0.01
    p.audit = _StubAudit()
    login = {"chrome": "blocked_by_system", "safari": "blocked_by_system"}

    async def gated(url, cookies=None, cookies_browser="auto", trace=None):
        raise resolver.ResolveError("4003110", kind="browser_only", login=login)

    monkeypatch.setattr(resolver, "resolve_stream_url", gated)
    with pytest.raises(resolver.ResolveError) as exc:
        asyncio.run(p._resolve_media("https://www.tiktok.com/@x/live"))
    assert exc.value.kind == "browser_only" and exc.value.login == login
    assert bl.fda_steps() in str(exc.value) and _PASTE in str(exc.value)
    assert bl.LOGIN_STEPS not in str(exc.value)
    assert [r.get("login") for r in p.audit.records] == [login] * 3


# ---- 自检「浏览器登录态」：不解密，只看读不读得到 + 登录 cookie 的名字 ---------

def _chrome_db(home, rows, profile="Default"):
    net = _chrome_dir(home) / profile / "Network"
    net.mkdir(parents=True)
    conn = sqlite3.connect(str(net / "Cookies"))
    conn.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB)")
    conn.executemany("INSERT INTO cookies VALUES (?, ?, ?)",
                     [(host, name, SENTINEL.encode()) for host, name in rows])
    conn.commit()
    conn.close()
    return net / "Cookies"


def _binarycookies(cookies):
    """按 Safari 的 Cookies.binarycookies 格式拼一个文件（一页）。"""
    records = []
    for domain, name in cookies:
        blob, offsets, pos = b"", [], 56
        for text in (domain, name, "/", SENTINEL):
            raw = text.encode() + b"\x00"
            offsets.append(pos)
            blob += raw
            pos += len(raw)
        records.append(struct.pack("<IIII", pos, 0, 0, 0) + struct.pack("<IIII", *offsets)
                       + b"\x00" * 8 + struct.pack("<dd", 0.0, 0.0) + blob)
    count = len(records)
    start = 8 + 4 * count + 4
    offsets = [start + sum(len(r) for r in records[:i]) for i in range(count)]
    page = (b"\x00\x00\x01\x00" + struct.pack("<I", count)
            + struct.pack("<{}I".format(count), *offsets) + b"\x00" * 4 + b"".join(records))
    return b"cook" + struct.pack(">II", 1, len(page)) + page + b"\x00" * 8


def _safari_file(home, cookies):
    folder = home / "Library" / "Containers" / "com.apple.Safari" / "Data" / "Library" / "Cookies"
    folder.mkdir(parents=True)
    path = folder / "Cookies.binarycookies"
    path.write_bytes(_binarycookies(cookies))
    return path


def _selfcheck_env(monkeypatch, tmp_path, browsers=("chrome", "safari")):
    home = _mac_home(monkeypatch, tmp_path)
    monkeypatch.setattr(resolver, "_installed_browsers", lambda: tuple(browsers))
    decrypting_calls = []

    def must_not_decrypt():
        decrypting_calls.append("load")
        raise AssertionError("自检不许碰会解密的读取函数")

    monkeypatch.setattr(bl, "_load_extractor", must_not_decrypt)
    return home, decrypting_calls


def _row_text(row):
    return row["detail"] + row["fix"]


def _assert_plain(row, decrypting_calls):
    assert row["name"] == "浏览器登录态"
    assert not any(word in _row_text(row) for word in GUESSED_LABELS)
    assert SENTINEL not in json.dumps(row, ensure_ascii=False)
    assert decrypting_calls == []


def test_selfcheck_row_when_the_system_refuses(monkeypatch, tmp_path):
    home, calls = _selfcheck_env(monkeypatch, tmp_path)
    _refuse_listing(monkeypatch, _chrome_dir(home))
    denied = _safari_file(home, [(".tiktok.com", "sessionid")])
    real_open = open

    def guarded_open(path, *a, **k):
        if str(path) == str(denied):
            raise PermissionError(1, "Operation not permitted", str(path))
        return real_open(path, *a, **k)

    monkeypatch.setattr(bl, "open", guarded_open, raising=False)
    monkeypatch.setattr(bl, "fda_targets", lambda *a, **k: [
        "/opt/homebrew/Cellar/python@3.14/3.14.7/bin/python3.14", "/opt/anaconda3/bin/python3.13"])
    row = run(selfcheck.check_browser_login())
    assert row["level"] == "warn"
    assert "Chrome：系统拒绝读取；Safari：系统拒绝读取" in row["detail"]
    assert row["fix"] == (
        "系统不允许本程序读取浏览器数据。到「系统设置」→「隐私与安全性」→「完全磁盘访问权限」，"
        "点「+」，在选文件的窗口里按 ⌘⇧G，粘贴下面的路径后回车、点「打开」，再把它的开关打开"
        "（每个路径各做一遍）：「/opt/homebrew/Cellar/python@3.14/3.14.7/bin/python3.14」、"
        "「/opt/anaconda3/bin/python3.13」。列表里显示的名字是 python3.14、python3.13，"
        "不是本程序的名字。用 Start.command 启动的话把「终端」也加进去。然后完全退出程序再打开。")
    _assert_plain(row, calls)


def test_selfcheck_row_when_readable_but_not_logged_in(monkeypatch, tmp_path):
    home, calls = _selfcheck_env(monkeypatch, tmp_path)
    _chrome_db(home, [(".tiktok.com", "ttwid"), (".example.com", "sessionid")])
    _safari_file(home, [(".example.com", "sessionid")])
    row = run(selfcheck.check_browser_login())
    assert row["level"] == "warn"
    assert "Chrome：能读取，没有 TikTok 登录 cookie" in row["detail"]
    assert "Safari：能读取，里面没有 tiktok.com 的 cookie" in row["detail"]
    assert row["fix"] == "在 Chrome（或 Safari）里登录 TikTok 后再试。"
    _assert_plain(row, calls)


def test_selfcheck_row_is_ok_when_one_browser_shows_a_login(monkeypatch, tmp_path):
    home, calls = _selfcheck_env(monkeypatch, tmp_path)
    _refuse_listing(monkeypatch, _chrome_dir(home))
    _safari_file(home, [(".example.com", "a"), (".tiktok.com", "sid_guard")])
    row = run(selfcheck.check_browser_login())
    assert row["level"] == "ok"
    assert row["detail"] == "Chrome：系统拒绝读取；Safari：读到了 TikTok 登录"
    _assert_plain(row, calls)


def test_selfcheck_chrome_login_is_read_from_names_only(monkeypatch, tmp_path):
    """库是只读 + immutable 打开的：查完名字，文件旁边不许多出 -wal/-journal。"""
    home, calls = _selfcheck_env(monkeypatch, tmp_path, browsers=("chrome",))
    _chrome_db(home, [(".tiktok.com", "ttwid")], profile="Default")
    time.sleep(0.02)
    newest = _chrome_db(home, [(".tiktok.com", "sessionid_ss")], profile="Profile 1")
    os.utime(str(newest), (time.time() + 60, time.time() + 60))
    row = run(selfcheck.check_browser_login())
    assert row["level"] == "ok" and row["detail"] == "Chrome：读到了 TikTok 登录"
    assert sorted(p.name for p in newest.parent.iterdir()) == ["Cookies"]
    _assert_plain(row, calls)


def test_selfcheck_row_without_browser_data(monkeypatch, tmp_path):
    _home_dir, calls = _selfcheck_env(monkeypatch, tmp_path)
    row = run(selfcheck.check_browser_login())
    assert row["level"] == "warn"
    assert "Chrome：没有找到它的 cookie 数据；Safari：没有找到它的 cookie 数据" in row["detail"]
    assert row["fix"] == bl.LOGIN_STEPS
    _assert_plain(row, calls)


def test_selfcheck_row_does_not_apply_off_macos(monkeypatch, tmp_path):
    _home_dir, calls = _selfcheck_env(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "platform", "win32")
    row = run(selfcheck.check_browser_login())
    assert row["level"] == "ok" and "不适用" in row["detail"]
    _assert_plain(row, calls)


def test_selfcheck_unparseable_safari_file_is_only_readable(monkeypatch, tmp_path):
    home, calls = _selfcheck_env(monkeypatch, tmp_path, browsers=("safari",))
    _safari_file(home, []).write_bytes(b"not a cookie file " + SENTINEL.encode())
    row = run(selfcheck.check_browser_login())
    assert row["level"] == "warn"
    assert "Safari：能读取，没能看出有没有登录 TikTok" in row["detail"]
    _assert_plain(row, calls)


def test_run_all_lists_the_row_and_reads_nothing_under_pytest(monkeypatch):
    monkeypatch.setattr(resolver, "_installed_browsers", lambda: ("chrome", "safari"))
    args = SimpleNamespace(denoise="off", backend="auto", model=None, translator="none",
                           device="auto", comments=False)
    rows = [c for c in run(selfcheck.run_all(args, None, None)) if c["name"] == "浏览器登录态"]
    assert len(rows) == 1 and rows[0]["level"] == "ok"


# ---- 钥匙串没给密钥：名字在、值没出来，不能说成「没有 cookie」 -------------------
#
# yt-dlp 拿不到「Chrome Safe Storage」的密钥时（中控在钥匙串对话框上点了「拒绝」、钥匙串
# 锁着、security 命令退出码非 0），解不开的 v10 cookie 被它一条条丢掉，交回来的是一个
# 没有 tiktok.com 条目的 jar。以前这里于是说「能读取，里面没有 tiktok.com 的 cookie，去登录」
# ——同一个库里明明有 sessionid，自检那一行也写着「读到了 TikTok 登录」。

def test_names_in_the_store_but_nothing_decrypted_is_cannot_decrypt(monkeypatch, tmp_path):
    home = _mac_home(monkeypatch, tmp_path)
    _chrome_db(home, [(".tiktok.com", "sessionid"), (".tiktok.com", "sid_tt"),
                      (".tiktok.com", "ttwid")])
    _extractor(monkeypatch, {"chrome": [FakeCookie(".example.com", "plain", "p")]})
    assert bl.read_login("chrome") == bl.LoginRead(None, "cannot_decrypt")
    assert bl.probe("chrome") == "ok"          # 只看名字的自检和它不再互相矛盾

    message = browser_only_message(3, {"chrome": "cannot_decrypt"})
    assert "Chrome：能读取，cookie 的值没能解开" in message
    assert "「Chrome Safe Storage」" in message and "始终允许" in message
    assert "没有 tiktok.com 的 cookie" not in message
    assert bl.LOGIN_STEPS not in message and "完全磁盘访问权限" not in message
    assert not any(word in _advice(message) for word in GUESSED_LABELS)


def test_login_name_in_the_store_missing_from_the_jar_is_cannot_decrypt(monkeypatch, tmp_path):
    """解出来一部分（没加密的那几条）：照旧带给 TikTok，但代码不能说成「没登录」。"""
    home = _mac_home(monkeypatch, tmp_path)
    _chrome_db(home, [(".tiktok.com", "sessionid"), (".tiktok.com", "ttwid")])
    _extractor(monkeypatch, {"chrome": [FakeCookie(".tiktok.com", "ttwid", "w")]})
    assert bl.read_login("chrome") == bl.LoginRead("ttwid=w", "cannot_decrypt")


def test_tiktok_names_without_login_and_an_empty_jar_is_cannot_decrypt(monkeypatch, tmp_path):
    home = _mac_home(monkeypatch, tmp_path)
    _chrome_db(home, [(".tiktok.com", "ttwid"), (".tiktok.com", "msToken")])
    _extractor(monkeypatch, {"chrome": []})
    assert bl.read_login("chrome") == bl.LoginRead(None, "cannot_decrypt")


def test_cannot_decrypt_needs_the_names_to_be_there(monkeypatch, tmp_path):
    """库里确实没有 tiktok.com 的名字、或者确实只有没登录的那几条：原来的代码不变。"""
    home = _mac_home(monkeypatch, tmp_path)
    _chrome_db(home, [(".example.com", "sessionid")])
    _extractor(monkeypatch, {"chrome": []})
    assert bl.read_login("chrome") == bl.LoginRead(None, "no_tiktok_cookie")
    _chrome_db(home, [(".tiktok.com", "ttwid")], profile="Profile 1")
    _extractor(monkeypatch, {"chrome": [FakeCookie(".tiktok.com", "ttwid", "w")]})
    assert bl.read_login("chrome") == bl.LoginRead("ttwid=w", "not_logged_in")


def test_cannot_decrypt_is_only_said_about_chromium_family(monkeypatch, tmp_path):
    """Safari 的 cookie 值不加密：名字在、jar 里没有，不是「没能解开」，不换说法。"""
    home = _mac_home(monkeypatch, tmp_path)
    _safari_file(home, [(".tiktok.com", "sessionid")])
    _extractor(monkeypatch, {"safari": []})
    assert bl.read_login("safari") == bl.LoginRead(None, "no_tiktok_cookie")


def test_cannot_decrypt_names_each_browsers_own_keychain_item():
    text = bl.steps_text({"chrome": "cannot_decrypt", "brave": "cannot_decrypt"})
    assert "「Chrome Safe Storage」" in text and "「Brave Safe Storage」" in text


def test_cannot_decrypt_reaches_the_trace_and_the_resolve_error(monkeypatch, tmp_path):
    home = _mac_home(monkeypatch, tmp_path)
    _chrome_db(home, [(".tiktok.com", "sessionid")])
    _extractor(monkeypatch, {"chrome": [], "safari": _safari_denied()})
    _wire_resolver(monkeypatch, {})
    trace = []
    err = _resolve_and_fail(trace)
    assert err.login == {"chrome": "cannot_decrypt", "safari": "blocked_by_system"}
    assert "chrome: cannot_decrypt" in [r for r in trace if r["layer"] == "直播页兜底"][0]["why"]
    assert SENTINEL not in json.dumps(trace, ensure_ascii=False) + str(err)


# ---- 好几个 Chrome 个人资料：登录不一定在最近写过的那个里 ------------------------
#
# yt-dlp 不给 profile 时只看 Chrome 目录下最新的那个 Cookies 文件，自检以前也一样。
# 中控在「Profile 1」里登录了 TikTok、而「Default」最后落了盘，结论就是「没有 cookie、去登录」；
# 两个文件的 mtime 一换，结论又变回「读到了登录」。

def _two_profiles(home, newest):
    """Default 里没有 TikTok，Profile 1 里有登录；newest 指定哪个库的 mtime 更新。"""
    plain = _chrome_db(home, [(".example.com", "sessionid")], profile="Default")
    login = _chrome_db(home, [(".tiktok.com", "sessionid"), (".tiktok.com", "ttwid")],
                       profile="Profile 1")
    now = time.time()
    for path in (plain, login):
        stamp = now if path.parent.parent.name == newest else now - 3600
        os.utime(str(path), (stamp, stamp))
    return login.parent.parent


@pytest.mark.parametrize("newest", ["Default", "Profile 1"])
def test_probe_finds_the_login_in_any_profile(monkeypatch, tmp_path, newest):
    home, calls = _selfcheck_env(monkeypatch, tmp_path, browsers=("chrome",))
    _two_profiles(home, newest)
    assert bl.probe("chrome") == "ok"
    row = run(selfcheck.check_browser_login())
    assert row["level"] == "ok" and row["detail"] == "Chrome：读到了 TikTok 登录"
    _assert_plain(row, calls)


@pytest.mark.parametrize("newest", ["Default", "Profile 1"])
def test_read_login_reads_the_profile_that_holds_the_login(monkeypatch, tmp_path, newest):
    home = _mac_home(monkeypatch, tmp_path)
    profile_dir = _two_profiles(home, newest)
    calls = _extractor(monkeypatch, {"chrome": [FakeCookie(".tiktok.com", "sessionid", "v")]})
    assert bl.read_login("chrome") == bl.LoginRead("sessionid=v", "ok")
    assert calls == ["chrome"] and calls.profiles == [str(profile_dir)]


def test_read_login_leaves_the_profile_to_ytdlp_when_no_profile_shows_tiktok(monkeypatch,
                                                                            tmp_path):
    home = _mac_home(monkeypatch, tmp_path)
    _chrome_db(home, [(".example.com", "a")], profile="Default")
    calls = _extractor(monkeypatch, {"chrome": []})
    assert bl.read_login("chrome").code == "no_tiktok_cookie"
    assert calls.profiles == [None]


def test_among_profiles_with_a_login_the_most_recently_written_wins(monkeypatch, tmp_path):
    home = _mac_home(monkeypatch, tmp_path)
    old = _chrome_db(home, [(".tiktok.com", "sessionid")], profile="Default")
    new = _chrome_db(home, [(".tiktok.com", "sid_tt")], profile="Profile 2")
    now = time.time()
    os.utime(str(old), (now - 3600, now - 3600))
    os.utime(str(new), (now, now))
    assert bl.login_profile("chrome") == str(new.parent.parent)


def test_one_unreadable_profile_does_not_hide_the_login_in_another(monkeypatch, tmp_path):
    home, calls = _selfcheck_env(monkeypatch, tmp_path, browsers=("chrome",))
    broken = _chrome_db(home, [(".tiktok.com", "ttwid")], profile="Default")
    broken.write_bytes(b"this is not a sqlite file " + SENTINEL.encode())
    _chrome_db(home, [(".tiktok.com", "sessionid")], profile="Profile 1")
    assert bl.probe("chrome") == "ok"
    broken_only = tmp_path / "second"
    broken_only.mkdir()
    monkeypatch.setattr(bl, "_home", lambda: str(broken_only))
    lone = _chrome_db(broken_only, [(".tiktok.com", "sessionid")])
    lone.write_bytes(b"this is not a sqlite file")
    assert bl.probe("chrome") == "error:DatabaseError"


def test_ytdlp_subprocess_layer_is_given_the_same_profile(monkeypatch, tmp_path):
    home = _mac_home(monkeypatch, tmp_path)
    profile_dir = _two_profiles(home, "Default")
    _extractor(monkeypatch, {"chrome": [FakeCookie(".tiktok.com", "sessionid")],
                             "safari": _safari_denied()})
    _wire_resolver(monkeypatch, {})
    seen = []

    async def ytdlp(_url, cookies=None, browser=None, timeout=None, profile=None):
        seen.append((browser, profile))
        return 1, "", "ERROR: The channel is not currently live"

    monkeypatch.setattr(resolver, "_run_ytdlp", ytdlp)
    _resolve_and_fail([])
    assert ("chrome", str(profile_dir)) in seen
    # 与 login-first 合并之后：某个浏览器读到了登录，后面的层就只借它，不再去读别的浏览器。
    # 这里 Safari 被系统拒绝、Chrome 有登录，所以 yt-dlp 不再带着 Safari 多跑一次
    assert ("safari", None) not in seen


def test_ytdlp_command_line_carries_the_profile(monkeypatch):
    captured = {}

    class Proc:
        returncode = 0

        async def communicate(self):
            return b"https://pull.example/stream.flv\n", b""

    async def create(*cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    run(resolver._run_ytdlp("https://www.tiktok.com/@x/live", browser="chrome",
                            profile="/Users/x/Library/Application Support/Google/Chrome/Profile 1"))
    at = captured["cmd"].index("--cookies-from-browser")
    assert captured["cmd"][at + 1] == (
        "chrome:/Users/x/Library/Application Support/Google/Chrome/Profile 1")
    run(resolver._run_ytdlp("https://www.tiktok.com/@x/live", browser="safari"))
    at = captured["cmd"].index("--cookies-from-browser")
    assert captured["cmd"][at + 1] == "safari"


# ---- 「完全磁盘访问权限」要加的是哪一项 ----------------------------------------
#
# 2026-09-17 本机 TCC 日志：.app 的 CFBundleExecutable 是 bash 脚本，exec 进 python、main.py
# 再 exec 进 .venv 的 python，系统从头到尾没见过这个 bundle——登记的是解释器文件的**路径**
# （identifier_type=Path，subject=/opt/homebrew/…/python3.14）。照着「加入 TikTok Live
# Translator」去做，加进去的是一条对不上的 bundle 记录，自检那一行不会变。

REPO = Path(__file__).resolve().parents[1]


def test_fda_targets_are_the_interpreter_files_the_system_sees(tmp_path):
    real_launch = tmp_path / "Cellar" / "python3.14"
    real_venv = tmp_path / "anaconda" / "python3.13"
    for f in (real_launch, real_venv):
        f.parent.mkdir(parents=True)
        f.write_text("", encoding="utf-8")
    bundle_python = tmp_path / "TikTok Live Translator.app" / "Contents" / "MacOS" / "python"
    bundle_python.parent.mkdir(parents=True)
    os.symlink(str(real_venv), str(bundle_python))
    got = bl.fda_targets(environ={"TLT_LAUNCH_PYTHON": str(real_launch)},
                         executable=str(bundle_python))
    assert got == [os.path.realpath(str(real_launch)), os.path.realpath(str(real_venv))]
    # 启动用的和现在跑的是同一个文件：只列一次；没记下启动用的：只列现在跑的
    assert bl.fda_targets(environ={"TLT_LAUNCH_PYTHON": str(real_venv)},
                          executable=str(bundle_python)) == [os.path.realpath(str(real_venv))]
    assert bl.fda_targets(environ={}, executable=str(bundle_python)) == [
        os.path.realpath(str(real_venv))]


def test_fda_steps_name_the_interpreter_paths_not_the_bundle():
    paths = ["/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/"
             "Versions/3.14/bin/python3.14", "/opt/anaconda3/bin/python3.13"]
    text = bl.fda_steps(paths)
    for path in paths:
        assert "「{}」".format(path) in text
    assert "python3.14、python3.13" in text and "⌘⇧G" in text
    assert "完全磁盘访问权限" in text and "「终端」" in text and "完全退出程序再打开" in text
    assert "TikTok Live Translator" not in text and "TikTok 直播同传" not in text
    assert not any(word in text for word in GUESSED_LABELS)


def test_blocked_message_and_selfcheck_row_use_the_runtime_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(bl, "fda_targets", lambda *a, **k: ["/opt/x/bin/python3.12"])
    message = browser_only_message(3, {"chrome": "blocked_by_system"})
    assert "「/opt/x/bin/python3.12」" in message and "TikTok Live Translator" not in message
    row = selfcheck._browser_login_row({"chrome": "blocked_by_system"})
    assert "「/opt/x/bin/python3.12」" in row["fix"] and "TikTok Live Translator" not in row["fix"]


def test_launch_python_is_remembered_once_and_survives_the_execs():
    from app import macbundle

    env = {}
    assert macbundle.remember_launch_python(env, "/opt/homebrew/bin/python3") == env[
        "TLT_LAUNCH_PYTHON"]
    first = env["TLT_LAUNCH_PYTHON"]
    assert first == os.path.realpath("/opt/homebrew/bin/python3")
    # execv 进 .venv、再 execve 进 .app 之后 main.py 从头再跑：不许被后来的解释器盖掉
    macbundle.remember_launch_python(env, "/somewhere/.venv/bin/python")
    assert env["TLT_LAUNCH_PYTHON"] == first


def test_main_remembers_the_launch_python_before_anything_execs():
    """main.py 测不了（import 就 execv）：读语法树，确认这一句在模块级、排在 ensure_env() 之前。"""
    import ast

    tree = ast.parse((REPO / "main.py").read_text(encoding="utf-8"))

    def first_top_level_call(name):
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                        and sub.func.id == name):
                    return node.lineno
        return None

    remember = first_top_level_call("remember_launch_python")
    ensure = first_top_level_call("ensure_env")
    assert remember is not None and ensure is not None and remember < ensure


def test_bundle_relaunch_passes_the_launch_python_along(tmp_path, monkeypatch):
    from app import macbundle

    monkeypatch.setenv("TLT_LAUNCH_PYTHON", "/opt/homebrew/bin/python3.14")
    monkeypatch.setattr(macbundle, "should_relaunch", lambda root, argv: True)
    monkeypatch.setattr(macbundle, "ensure_bundle_shell", lambda root: tmp_path / "python")
    seen = {}
    macbundle.relaunch_inside_bundle(tmp_path, argv=["main.py"],
                                     execve=lambda path, argv, env: seen.update(env))
    assert seen["TLT_LAUNCH_PYTHON"] == "/opt/homebrew/bin/python3.14"


@pytest.mark.parametrize("readme", ["README.md", "README.zh-CN.md"])
def test_readme_does_not_tell_the_operator_to_add_the_bundle(readme):
    text = (REPO / readme).read_text(encoding="utf-8")
    assert 'add "TikTok Live Translator"' not in text
    assert "加入「TikTok Live Translator」" not in text
    assert "cannot_decrypt" in text and "⌘⇧G" in text
