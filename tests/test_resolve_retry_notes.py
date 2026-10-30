"""拿不到流地址、自动重试期间和收场时给中控的话（2026-10-29 新 Mac 上那一场之后）。

那天三次解析（13:27:50、13:28:49、13:29:48）每次约 39 秒，其中 WebKit 那一层占 25 秒多。界面上只有等待前发的那一句
「Retrying in 20 sec (attempt 2 of 3)」，接下来那次解析期间它一直挂着，屏幕上实际停了约 59 秒；三次一共 156 秒，
整段话里没有一件现在就能做的事，中控只能问「到底怎么回事」。登录优先那一层（没点名浏览器时只读 Safari）三次都
没读到 Safari（系统拒绝读取），后面几层借到了 Chrome 的登录；收场的话只说「已借用 Chrome 的登录再试，仍然没给」，
Safari 没读到这件事一个字没有，状态里也不带「完全磁盘访问权限」要加的路径。Safari 有了权限之后，同一个房间
第一层 791 毫秒就拿到了——这是一次观察，不当原因写。钉住：

* 每次重试开始前再发一句「正在第 i/n 次查流地址（每次约 40 秒）」，等待那句后面补一件能做的事；
* 登录优先那一层记着某个浏览器被系统拒绝读取时，等待那句再补一句纯观察（读不到哪个浏览器、步骤在顶部那条
  提示里），不写「所以」、不说是原因；
* 收场那句（browser_only_message）结尾也有「在浏览器里看一眼」；借到了别的浏览器的登录、而登录优先那一步要读
  的浏览器被拒时，照样留着它被拒的观察和完全磁盘访问权限的几步，状态里带路径；
* 这些句子过 tests/i18n_rules.py 的两级禁用词（中英文都查，第八条）。

全部用替身：不连 TikTok、不开浏览器、不读真的 cookie。主播名是虚构的。
"""
import asyncio

import pytest

from app import browser_login as bl
from app import i18n
from app import resolver
from app.pipeline import _fda_paths, browser_only_message
from app.resolver import LOGIN_LAYER, ResolveError
from tests import i18n_rules as rules
from tests.test_status_fields import RecordingServer, _pipeline, statuses

ROOM = "https://www.tiktok.com/@ficticia_estado/live"
EN = i18n.EN
TODAY = {"safari": bl.BLOCKED, "chrome": bl.OK}     # 2026-10-29 那三次解析看到的组合
TARGETS = ["/Users/someone/.local/python3.13/bin/python3.13"]


def en(text):
    return i18n.render(text, EN)


def rule8_clean(text):
    english = en(text)
    assert not rules.EN_LABELS.search(english), (rules.EN_LABELS.search(english).group(), english)
    assert not rules.EN_CAUSAL.search(english), (rules.EN_CAUSAL.search(english).group(), english)
    assert not rules.ZH_LABELS.search(str(text)), str(text)
    assert not rules.ZH_CAUSAL.search(str(text)), str(text)


@pytest.fixture
def lookups(monkeypatch, tmp_path):
    """每次解析都以 browser_only 收场；登录优先那一层记着 login（剧本里给），解析本身不花时间。"""
    server = RecordingServer()
    p = _pipeline(monkeypatch, tmp_path, server)
    p.BROWSER_ONLY_RETRY_SEC = 0.0
    monkeypatch.setattr(bl, "fda_targets", lambda environ=None, executable=None: list(TARGETS))
    monkeypatch.setattr(resolver, "_login_first_browsers", lambda cookies_browser: ("safari",))
    script = {"first_layer": {"safari": bl.BLOCKED}, "login": dict(TODAY)}

    async def fail(url, cookies=None, cookies_browser="auto", trace=None):
        if trace is not None:
            trace.append({"layer": LOGIN_LAYER, "outcome": "no_login", "ms": 3,
                          "login": dict(script["first_layer"])})
        raise ResolveError("4003110", kind="browser_only", login=dict(script["login"]))

    monkeypatch.setattr(resolver, "resolve_stream_url", fail)
    return p, server, script


def connecting(server):
    return [m["detail"] for m in statuses(server) if m["state"] == "connecting"]


# ---- 重试期间 -----------------------------------------------------------------------------------

def test_each_retry_says_which_lookup_is_running_and_the_wait_says_what_to_do(lookups):
    p, server, script = lookups
    script["first_layer"] = {"safari": bl.NOT_LOGGED_IN}          # 先看不带观察那一句的样子
    with pytest.raises(ResolveError):
        asyncio.run(p._resolve_media(ROOM))
    got = connecting(server)
    tip = "可以先在浏览器里打开这个直播间，看看主播是不是在播。"
    assert got == [
        "TikTok 暂时没有把这个直播间的流地址给程序，0 秒后自动重试（第 2/3 次）…" + tip,
        "正在第 2/3 次查流地址（每次约 40 秒）…",
        "TikTok 暂时没有把这个直播间的流地址给程序，0 秒后自动重试（第 3/3 次）…" + tip,
        "正在第 3/3 次查流地址（每次约 40 秒）…",
    ]
    assert [en(t) for t in got] == [
        "TikTok didn’t provide a stream URL for this live stream. Retrying in 0 sec (attempt 2 of 3)… "
        "Meanwhile, you can open this live stream in your browser to check whether the streamer is live.",
        "Looking up the stream URL, attempt 2 of 3 (about 40 sec each)…",
        "TikTok didn’t provide a stream URL for this live stream. Retrying in 0 sec (attempt 3 of 3)… "
        "Meanwhile, you can open this live stream in your browser to check whether the streamer is live.",
        "Looking up the stream URL, attempt 3 of 3 (about 40 sec each)…",
    ]
    for text in got:
        rule8_clean(text)


def test_a_first_layer_refused_by_macos_adds_only_the_observation(lookups):
    p, server, script = lookups
    with pytest.raises(ResolveError):
        asyncio.run(p._resolve_media(ROOM))
    waits = [t for t in connecting(server) if "自动重试" in t]
    assert len(waits) == 2
    seen = "查流地址时没有读到 Safari 里的 TikTok 登录，步骤见顶部那条提示。"
    assert all(t.endswith("看看主播是不是在播。" + seen) for t in waits), waits
    assert en(waits[0]).endswith(
        "check whether the streamer is live. The stream URL lookup couldn’t read the TikTok sign-in "
        "from Safari. The steps are in the notice at the top.")
    for text in waits:
        rule8_clean(text)
        assert "所以" not in text and "so " not in en(text)


def test_no_observation_when_the_first_layer_read_a_sign_in_or_read_nothing(lookups):
    p, server, script = lookups
    for first in ({"safari": bl.OK}, {"safari": bl.NOT_READ}, {}):
        server.messages.clear()
        script["first_layer"] = first
        with pytest.raises(ResolveError):
            asyncio.run(p._resolve_media(ROOM))
        assert not any("顶部那条提示" in t for t in connecting(server)), first


def test_the_first_lookup_is_not_announced_twice(lookups):
    """第一次查流地址之前 _run_session 已经发过「正在解析直播流地址…」：只在重试前发「第 i/n 次」。"""
    p, server, script = lookups
    with pytest.raises(ResolveError):
        asyncio.run(p._resolve_media(ROOM))
    assert not any("第 1/3 次查流地址" in t for t in connecting(server))


# ---- 收场那句 -----------------------------------------------------------------------------------

def test_the_final_message_ends_with_checking_in_the_browser():
    message = browser_only_message(3, None)
    assert message.endswith("一次约两周有效。也可以在浏览器里打开这个直播间，看看主播是不是在播。")
    assert en(message).endswith("A .flv URL usually works for about two weeks. You can also open this live "
                                "stream in your browser to check whether the streamer is live.")
    rule8_clean(message)


def test_borrowed_but_the_first_browser_refused_keeps_the_refusal_and_the_steps(monkeypatch):
    monkeypatch.setattr(bl, "fda_targets", lambda environ=None, executable=None: list(TARGETS))
    for first in (("safari",), None):
        message = browser_only_message(3, TODAY, first=first)
        assert "已借用 Chrome 里的 TikTok 登录再试，TikTok 仍然没有给出流地址。" in message
        assert "Safari：" + "系统拒绝读取" in message
        assert str(bl.FDA_OBSERVED + bl.fda_steps()) in message
        assert "「{}」".format(TARGETS[0]) in message
        english = en(message)
        assert ("Tried again with the TikTok sign-in from Chrome. TikTok still didn’t provide a stream URL. "
                "Looked for a TikTok sign-in in your browsers. Safari: macOS didn’t allow access.") in english
        assert "Full Disk Access" in english
        # 4003110 那句单独是第一行（横幅折叠时只露它）
        assert english.split("\n")[0] == en(browser_only_message(3, None)).split(" To watch now")[0].rstrip()
        rule8_clean(message)


def test_borrowed_and_the_first_browser_was_fine_stays_one_line(monkeypatch):
    """借到了登录、被拒的不是登录优先那一步要读的浏览器：照旧一句话，不带那几步。"""
    login = {"safari": bl.OK, "chrome": bl.BLOCKED}
    message = browser_only_message(3, login, first=("safari",))
    assert "\n" not in str(message) and "完全磁盘访问权限" not in message
    assert "已借用 Safari 里的 TikTok 登录再试" in message


@pytest.mark.parametrize("login,first,wants", [
    (TODAY, ("safari",), True),                                     # 2026-10-29 那一场
    (TODAY, None, True),                                            # 不知道读哪个：被拒了就给
    ({"safari": bl.OK, "chrome": bl.BLOCKED}, ("safari",), False),   # 登录优先那一步读到了
    ({"safari": bl.BLOCKED, "chrome": bl.NOT_READ}, ("safari",), True),
    ({"chrome": bl.BLOCKED, "safari": bl.BLOCKED}, (), True),         # 没借到：照旧给
    ({"safari": bl.NOT_LOGGED_IN}, ("safari",), False),
])
def test_fda_paths_follow_the_same_rule_as_the_text(monkeypatch, login, first, wants):
    monkeypatch.setattr(bl, "fda_targets", lambda environ=None, executable=None: list(TARGETS))
    assert bl.fda_needed(login, first) is wants
    exc = ResolveError(browser_only_message(3, login, first=first), kind="browser_only", login=login)
    exc.login_first = first
    assert _fda_paths(exc) == (TARGETS if wants else [])
    assert (str(bl.fda_steps()) in exc.args[0]) is wants


def test_todays_lookup_ends_with_the_paths_in_the_status(lookups):
    """整条路走一遍：_resolve_media 收场的错误交给 _run_session，状态里带上要加进完全磁盘访问权限的路径。"""
    p, server, script = lookups
    p.args.cookies_browser = "auto"
    p._session_state = p._new_session_state(None)
    asyncio.run(p._run_session(ROOM))
    (err,) = [m for m in statuses(server) if m["state"] == "error"]
    assert err["paths"] == TARGETS
    assert "已借用 Chrome 里的 TikTok 登录再试" in err["detail"] and "Safari：系统拒绝读取" in err["detail"]
    assert err["detail"].endswith("看看主播是不是在播。")
