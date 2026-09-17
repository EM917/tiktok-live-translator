"""登录优先：读得到浏览器里的 TikTok 登录，就一律先带着登录抓直播页。

2026-09-17 实测（macOS 27，房间 @daisycabral_ 在播，中控在 Safari 和 Chrome 里都登录着）：
  * 只带 Safari 的登录抓直播页：5 次里 5 次拿到 .flv 地址，0.4–0.8 秒；
  * 同一个请求放在一次匿名抓页之后 0.5 秒 / 3 秒：3 次里 3 次拿不到；隔 8 秒 / 20 秒：拿到；
  * 旧的「直播页兜底」层固定 匿名 → Chrome → Safari 背靠背，借登录的那一次永远落在匿名请求
    之后 0.5 秒，它前面的层也全是匿名的——整场没有监听。
产品负责人的决定：只要有登录就一律先用登录；不动 Chrome，直接用 Safari。

这里全部用假的：假的 cookie 读取函数、假的 aiohttp 会话、假的时钟和 sleep。不读真实浏览器
数据、不开浏览器 / WebKit、不连网、不 import main。

SENTINEL 是假 cookie 的值：它只许出现在发给 TikTok 的 Cookie 头里。
"""
import asyncio
import json
import sys
import time
import types
from types import SimpleNamespace

import pytest

from app import browser_login as bl
from app import pipeline as pipeline_mod
from app import resolver, settings
from app.pipeline import Pipeline
from app.server import CaptionServer

REAL_GET_JSON = resolver._get_json          # wire() 会把它换成假的；测它本身的用例用这个
SENTINEL = "S3NTINEL-login-first-VALUE-71c4"
GUESSED_LABELS = ("年龄", "限流", "封禁", "多半")
ROOM = "https://www.tiktok.com/@x/live"
FLV = "https://pull-flv-x.tiktokcdn-us.com/stage/stream-1_or4.flv?expire=1&sign=abc"
API_FLV = "https://pull-flv-x.tiktokcdn-us.com/stage/stream-1_ao.flv?only_audio=1"
LIVE_PAGE = '<html><script>{"flv":"' + FLV.replace("/", "\\/") + '"}</script></html>'
GATE_PAGE = "<html>a login gate; no stream address in this page</html>"
ENDED_PAGE = ('<html><script id="SIGI_STATE" type="application/json">'
              '{"LiveRoom":{"liveRoomUserInfo":{"liveRoom":{"status":4}}}}</script></html>')
_GATED = {"status_code": 4003110, "data": {"prompts": ""}}


def run(coro):
    return asyncio.run(coro)


class FakeCookie:
    def __init__(self, domain, name, value=SENTINEL):
        self.domain, self.name, self.value = domain, name, value


def logged_in_jar():
    return [FakeCookie(".tiktok.com", "sessionid"), FakeCookie(".tiktok.com", "ttwid")]


HEADER = "sessionid={0}; ttwid={0}".format(SENTINEL)


class World:
    """一台假的「本机 + TikTok」。events 按发生顺序记下每一次读取和每一个请求：
    ("read", 浏览器) / ("page", Cookie 头或 None) / ("api", Cookie 头或 None) /
    ("webkit",) / ("ytdlp", 浏览器或 None) / ("sleep", 秒)。"""

    def __init__(self):
        self.events = []
        self.jars = {}                  # 浏览器 → cookie 列表 或 异常
        self.page_with_login = LIVE_PAGE
        self.page_anonymous = GATE_PAGE
        self.api = "gated"              # "gated" / "url" / "nothing"
        self.page_delay = 0.0
        self.remembered = []

    def requests(self):
        return [e for e in self.events if e[0] != "read"]

    def anonymous_requests(self):
        return [e for e in self.events
                if e in (("page", None), ("api", None), ("webkit",), ("ytdlp", None))]

    def reads(self):
        return [e[1] for e in self.events if e[0] == "read"]


def wire(monkeypatch, tmp_path, world, platform="darwin", installed=("safari", "chrome")):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(resolver, "_installed_browsers", lambda: tuple(installed))
    monkeypatch.setattr(resolver, "_remember_browser", world.remembered.append)

    def extract(browser):
        world.events.append(("read", browser))
        got = world.jars.get(browser, [])
        if isinstance(got, BaseException):
            raise got
        return got

    monkeypatch.setattr(bl, "_load_extractor", lambda: extract)

    class FakeResponse:
        status, charset = 200, "utf-8"

        def __init__(self, body):
            self.body = body

        async def __aenter__(self):
            if world.page_delay:
                await asyncio.sleep(world.page_delay)
            return self

        async def __aexit__(self, *exc):
            return False

    class FakeSession:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, url, headers=None, **kwargs):
            cookie = (headers or {}).get("Cookie")
            assert "tiktok.com" in url                   # cookie 只发给 TikTok
            world.events.append(("page", cookie))
            logged_in = bool(cookie) and "sessionid=" in cookie
            return FakeResponse(world.page_with_login if logged_in else world.page_anonymous)

    async def read_all(resp, _limit):
        return resp.body.encode("utf-8")

    async def room_status(_session, _user):
        return "123", 2

    async def get_json(_session, _url, limit=None, headers=None):
        world.events.append(("api", (headers or {}).get("Cookie")))
        if world.api == "url":
            return {"status_code": 0, "data": {"status": 2, "stream_url": {
                "flv_pull_url": {"SD1": API_FLV}}}}
        return _GATED if world.api == "gated" else None

    async def webkit(_url, timeout=None):
        world.events.append(("webkit",))
        return None, False

    async def ytdlp(_url, cookies=None, browser=None, timeout=None):
        world.events.append(("ytdlp", browser))
        return 1, "", "ERROR: [tiktok:live] x: The channel is not currently live"

    async def check(url, trusted=False):
        return url

    async def works(_url, timeout=8):
        return True

    import aiohttp

    from app import nethttp
    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(nethttp, "read_all", read_all)
    monkeypatch.setitem(sys.modules, "yt_dlp", types.ModuleType("yt_dlp"))
    monkeypatch.setattr(resolver, "_room_status", room_status)
    monkeypatch.setattr(resolver, "_get_json", get_json)
    monkeypatch.setattr(resolver, "_resolve_via_webkit", webkit)
    monkeypatch.setattr(resolver, "_webkit_available", lambda: True)
    monkeypatch.setattr(resolver, "_run_ytdlp", ytdlp)
    monkeypatch.setattr(resolver, "_check_media_url", check)
    monkeypatch.setattr(resolver, "_media_url_works", works)
    return world


def resolve(trace=None, **kwargs):
    return run(resolver.resolve_stream_url(ROOM, trace=trace, **kwargs))


def layers(trace):
    return [(r["layer"], r["outcome"]) for r in trace]


# ---- 1 / 2：登录优先；拿到就不走后面的匿名各层 -------------------------------

def test_the_first_request_to_tiktok_carries_the_login(monkeypatch, tmp_path):
    world = wire(monkeypatch, tmp_path, World())
    world.jars["safari"] = logged_in_jar()
    trace = []
    assert resolve(trace) == FLV
    # 这次解析向 TikTok 发的第一个请求就是带登录的直播页，它前面没有任何匿名请求
    assert world.requests()[0] == ("page", HEADER)
    assert world.anonymous_requests() == []
    # 拿到、校验过、拉得动 → 直接返回：官方接口、WebKit、yt-dlp、直播页兜底一概没走
    assert world.requests() == [("page", HEADER)]
    assert layers(trace) == [("登录直播页", "url")]
    assert trace[0]["browser"] == "safari" and trace[0]["login"] == {"safari": "ok"}
    assert isinstance(trace[0]["ms"], int) and "waited_ms" not in trace[0]
    assert world.remembered == ["safari"]


def test_the_same_order_holds_when_pipeline_reconnects(monkeypatch, tmp_path):
    """断流重连走的是同一个 resolve_stream_url：同样先带登录，审计的 resolve 记录里看得到这一层。"""
    world = wire(monkeypatch, tmp_path, World())
    world.jars["safari"] = logged_in_jar()
    p, _server, audit = make_pipeline(monkeypatch, tmp_path)
    assert run(p._resolve_media(ROOM, reconnect=2)) == FLV
    assert world.requests() == [("page", HEADER)]
    (rec,) = audit.records
    assert rec["ok"] and rec["reconnect"] == 2
    assert rec["layers"][0]["layer"] == "登录直播页" and rec["layers"][0]["browser"] == "safari"


def test_logged_in_page_saying_the_room_ended_reports_offline(monkeypatch, tmp_path):
    world = wire(monkeypatch, tmp_path, World())
    world.jars["safari"] = logged_in_jar()
    world.page_with_login = ENDED_PAGE
    trace = []
    with pytest.raises(resolver.ResolveError) as exc:
        resolve(trace)
    assert exc.value.kind == "offline" and exc.value.status == 4
    assert layers(trace) == [("登录直播页", "offline")]
    assert trace[0]["field"] == "liveRoom.status" and trace[0]["status"] == 4
    assert world.anonymous_requests() == []


# ---- 3 / 5 / 6：没拿到就走原来的链路；最后一层先登录后匿名；Safari 有登录就不读 Chrome ----

def test_failure_falls_through_and_the_last_layer_goes_login_then_anonymous(monkeypatch,
                                                                            tmp_path):
    world = wire(monkeypatch, tmp_path, World())
    world.jars["safari"] = logged_in_jar()
    world.jars["chrome"] = logged_in_jar()
    world.page_with_login = GATE_PAGE             # 带了登录，页面里还是没有地址
    probed = []
    monkeypatch.setattr(bl, "probe", lambda b: probed.append(b) or bl.NOT_READ)
    trace = []
    with pytest.raises(resolver.ResolveError) as exc:
        resolve(trace)
    assert exc.value.kind == "browser_only"
    assert exc.value.login == {"safari": "ok"}
    assert layers(trace) == [
        ("登录直播页", "none"), ("官方接口", "browser_only"), ("WebKit", "none"),
        ("yt-dlp匿名", "none"), ("yt-dlp借cookie", "none"), ("直播页兜底", "none")]
    assert world.requests() == [
        ("page", HEADER),                          # 登录优先
        ("api", None), ("api", HEADER),            # 官方接口：匿名，被拒后带登录再问一次
        ("webkit",), ("ytdlp", None), ("ytdlp", "safari"),
        ("page", HEADER), ("page", None)]          # 直播页兜底：先登录，匿名的放最后
    # Safari 里有登录：Chrome 的数据一次都没读（会解密的读取和不解密的探测都没有）
    assert set(world.reads()) == {"safari"} and probed == []


def test_login_step_reads_only_safari_when_no_browser_is_named(monkeypatch, tmp_path):
    """Safari 读不到、Chrome 里有登录、不登录也能解析的直播间——没给「完全磁盘访问权限」的现有
    安装都是这样。登录优先那一步每次解析、每次重连都走，排在约 1 秒就能拿到地址的匿名接口
    前面：它只读 Safari。Chrome 的会解密读取（实测约 5 秒、要向钥匙串要密钥）和不解密的
    探测都不发生——以前这类直播间的解析从不碰 Chrome，现在也不碰。"""
    world = wire(monkeypatch, tmp_path, World())
    world.jars["safari"] = PermissionError(1, "Operation not permitted")
    world.jars["chrome"] = logged_in_jar()
    world.api = "url"
    probed = []
    monkeypatch.setattr(bl, "probe", lambda b: probed.append(b) or bl.OK)
    settings.save_setting("cookies_browser", "chrome")             # 上次成功的是 Chrome 也一样
    for _ in range(3):                                             # 首次 + 两次重连
        trace = []
        assert resolve(trace) == API_FLV
        assert layers(trace) == [("登录直播页", "no_login"), ("官方接口", "url")]
        assert trace[0]["login"] == {"safari": "blocked_by_system"}
    assert world.reads() == ["safari"] * 3 and probed == []


def test_chrome_stays_available_to_the_later_layers(monkeypatch, tmp_path):
    """Safari 里没有登录、Chrome 里有：登录优先那一步不碰 Chrome；匿名各层都没拿到之后，
    原有的几层照旧借 Chrome 的登录（直播页兜底现在先登录后匿名）。"""
    world = wire(monkeypatch, tmp_path, World())
    world.jars["safari"] = [FakeCookie(".tiktok.com", "ttwid")]       # 没有登录 cookie
    world.jars["chrome"] = logged_in_jar()
    trace = []
    assert resolve(trace) == FLV
    assert layers(trace)[0] == ("登录直播页", "no_login")
    assert trace[0]["browser"] is None and trace[0]["login"] == {"safari": "not_logged_in"}
    # 第一次读 Chrome 发生在匿名接口之后（接口回 4003110、借登录重问的那一次）
    first_chrome = world.events.index(("read", "chrome"))
    assert ("api", None) in world.events[:first_chrome]
    assert world.events[:first_chrome].count(("read", "safari")) == 2   # 登录那一步 + 接口重问
    last = trace[-1]
    assert (last["layer"], last["outcome"], last["browser"]) == ("直播页兜底", "url", "chrome")
    # 读到 Chrome 的登录之后，后面的层只借它：yt-dlp 没有再去试 Safari
    assert ("ytdlp", "chrome") in world.events and ("ytdlp", "safari") not in world.events


@pytest.mark.parametrize("how", ["flag", "setting"])
def test_a_named_browser_is_the_one_the_login_step_reads(monkeypatch, tmp_path, how):
    world = wire(monkeypatch, tmp_path, World())
    world.jars["safari"] = logged_in_jar()
    world.jars["chrome"] = logged_in_jar()
    monkeypatch.setattr(bl, "probe", lambda b: bl.OK)
    kwargs = {}
    if how == "flag":
        kwargs["cookies_browser"] = "chrome"
    else:
        settings.save_setting("cookies_browser_only", "chrome")
    trace = []
    assert resolve(trace, **kwargs) == FLV
    assert world.reads() == ["chrome"] and world.requests() == [("page", HEADER)]
    assert trace[0]["browser"] == "chrome" and trace[0]["login"] == {"chrome": "ok"}


def test_a_named_chrome_without_a_login_is_not_decrypted_in_the_login_step(monkeypatch,
                                                                            tmp_path):
    """中控点名了 Chrome：登录优先每次解析、每次重连都走，不解密的探测说 Chrome 里没有登录，
    就不花那 5 秒。"""
    world = wire(monkeypatch, tmp_path, World())
    world.jars["chrome"] = logged_in_jar()
    world.api = "url"
    monkeypatch.setattr(bl, "probe", lambda b: bl.NOT_LOGGED_IN)
    trace = []
    assert resolve(trace, cookies_browser="chrome") == API_FLV
    assert world.reads() == []
    assert trace[0]["login"] == {"chrome": "not_logged_in"}
    assert layers(trace) == [("登录直播页", "no_login"), ("官方接口", "url")]


# ---- 4：借登录抓直播页之前，和上一次匿名请求至少隔 8 秒 ----------------------

class Clock:
    def __init__(self, world, now=1000.0):
        self.world, self.now, self.slept = world, now, []

    def monotonic(self):
        return self.now

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.world.events.append(("sleep", seconds))
        self.now += seconds


def fake_clock(monkeypatch, world):
    clock = Clock(world)
    monkeypatch.setattr(resolver, "_monotonic", clock.monotonic)
    monkeypatch.setattr(resolver, "_gap_sleep", clock.sleep)
    return clock


def test_gap_constant_is_the_measured_one():
    assert resolver.LOGIN_AFTER_ANON_GAP_SEC == 8


@pytest.mark.parametrize("since_anon,expect_sleep", [
    (0.5, 7.5), (3.0, 5.0), (7.75, 0.25), (8.0, None), (20.0, None)])
def test_login_fetch_waits_out_the_rest_of_the_gap(monkeypatch, tmp_path, since_anon,
                                                   expect_sleep):
    world = wire(monkeypatch, tmp_path, World())
    world.jars["safari"] = logged_in_jar()
    clock = fake_clock(monkeypatch, world)
    resolver._stamp_anon()                         # 本进程刚发过一次匿名请求
    clock.now += since_anon
    trace = []
    assert resolve(trace) == FLV
    if expect_sleep is None:
        assert clock.slept == [] and "waited_ms" not in trace[0]
        assert world.requests() == [("page", HEADER)]
    else:
        assert clock.slept == [pytest.approx(expect_sleep)]
        assert trace[0]["waited_ms"] == int(round(expect_sleep * 1000))
        # 先等、后抓：等完之前带登录的请求没有发出去
        assert world.requests() == [("sleep", pytest.approx(expect_sleep)), ("page", HEADER)]


def test_no_wait_when_this_process_has_sent_no_anonymous_request(monkeypatch, tmp_path):
    world = wire(monkeypatch, tmp_path, World())
    world.jars["safari"] = logged_in_jar()
    clock = fake_clock(monkeypatch, world)
    assert resolver._ANON["last"] is None
    assert resolve() == FLV
    assert clock.slept == []


def test_last_layer_waits_out_the_gap_after_the_anonymous_layers(monkeypatch, tmp_path):
    """整条链路里：yt-dlp 匿名之后 2 秒轮到直播页兜底的登录那一次——补等 6 秒再抓。"""
    world = wire(monkeypatch, tmp_path, World())
    world.jars["safari"] = logged_in_jar()
    world.page_with_login = GATE_PAGE
    clock = fake_clock(monkeypatch, world)

    async def ytdlp(_url, cookies=None, browser=None, timeout=None):
        world.events.append(("ytdlp", browser))
        if browser is None:
            resolver._stamp_anon()                 # 真的 _run_ytdlp 在这里记匿名时刻
        else:
            clock.now += 2.0                       # 借 cookie 的那次 yt-dlp 花了 2 秒
        return 1, "", "ERROR: The channel is not currently live"

    monkeypatch.setattr(resolver, "_run_ytdlp", ytdlp)
    trace = []
    with pytest.raises(resolver.ResolveError):
        resolve(trace)
    assert clock.slept == [pytest.approx(6.0)]
    tail = world.requests()[-4:]
    assert tail == [("ytdlp", "safari"), ("sleep", pytest.approx(6.0)),
                    ("page", HEADER), ("page", None)]
    page_layer = [r for r in trace if r["layer"] == "直播页兜底"][0]
    assert page_layer["waited_ms"] == 6000


def test_the_gap_is_checked_again_after_waiting(monkeypatch, tmp_path):
    """等间隔的那几秒里，同一个进程里别的任务（弹幕子进程重连）又发了一次匿名请求：
    等完再看一眼，把新的差额也等完，两段都记进 waited_ms。"""
    world = wire(monkeypatch, tmp_path, World())
    world.jars["safari"] = logged_in_jar()
    clock = fake_clock(monkeypatch, world)
    plain_sleep = clock.sleep

    async def sleep(seconds):
        await plain_sleep(seconds)
        if len(clock.slept) == 1:
            resolver._ANON["last"] = clock.now - 2.0      # 醒来前 2 秒有过一次匿名请求

    monkeypatch.setattr(resolver, "_gap_sleep", sleep)
    resolver._stamp_anon()
    clock.now += 0.5
    trace = []
    assert resolve(trace) == FLV
    assert clock.slept == [pytest.approx(7.5), pytest.approx(6.0)]
    assert trace[0]["waited_ms"] == 13500 and "gap_not_clear" not in trace[0].get("why", "")
    assert world.requests()[-1] == ("page", HEADER)


def test_the_gap_wait_is_capped(monkeypatch, tmp_path):
    """匿名请求一直有：最多等三个间隔，然后照常去抓，trace 里记 gap_not_clear。"""
    world = wire(monkeypatch, tmp_path, World())
    world.jars["safari"] = logged_in_jar()
    clock = fake_clock(monkeypatch, world)
    plain_sleep = clock.sleep

    async def sleep(seconds):
        await plain_sleep(seconds)
        resolver._ANON["last"] = clock.now                # 每次醒来都刚好又有一次

    monkeypatch.setattr(resolver, "_gap_sleep", sleep)
    assert resolver.LOGIN_GAP_MAX_WAIT_SEC == 3 * resolver.LOGIN_AFTER_ANON_GAP_SEC
    resolver._stamp_anon()
    trace = []
    assert resolve(trace) == FLV                           # 没有一直等下去
    assert sum(clock.slept) == pytest.approx(24.0)
    assert trace[0]["waited_ms"] == 24000 and "gap_not_clear" in trace[0]["why"]


def test_anonymous_requests_are_stamped_and_logged_in_ones_are_not(monkeypatch):
    """真的 _get_json / _run_ytdlp（不经 wire），假的会话和子进程。"""
    clock = fake_clock(monkeypatch, World())
    monkeypatch.setattr(sys, "platform", "darwin")

    class Resp:
        status, charset = 200, "utf-8"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class Session:
        def get(self, url, headers=None, **kwargs):
            clock.now += 1.0
            return Resp()

    async def read_all(_resp, _limit):
        return b"{}"

    monkeypatch.setattr(resolver, "read_all", read_all)

    async def go():
        seen = []
        resolver._ANON["last"] = None
        await resolver._get_json(Session(), "https://www.tiktok.com/api", headers={
            "User-Agent": "x", "Cookie": "sessionid=" + SENTINEL})
        seen.append(resolver._ANON["last"])                       # 带登录：不记
        await resolver._get_json(Session(), "https://www.tiktok.com/api")
        seen.append(resolver._ANON["last"])                       # 匿名：记的是返回之后的时刻
        return seen

    assert run(go()) == [None, 1002.0]

    class Proc:
        returncode = 1

        async def communicate(self):
            clock.now += 3.0
            return b"", b"ERROR: not currently live"

    async def spawn(*cmd, **kwargs):
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    async def ytdlp():
        resolver._ANON["last"] = None
        await resolver._run_ytdlp(ROOM, browser="safari")
        borrowed = resolver._ANON["last"]
        await resolver._run_ytdlp(ROOM, cookies="/tmp/cookies.txt")
        with_file = resolver._ANON["last"]
        await resolver._run_ytdlp(ROOM)
        return borrowed, with_file, resolver._ANON["last"]

    assert run(ytdlp()) == (None, None, 1011.0)


def test_cookies_without_a_login_count_as_an_anonymous_request(monkeypatch, tmp_path):
    """有 tiktok.com 的 cookie、没有登录 cookie：照旧带上去，但它不是登录请求——不用等间隔，
    而且要记成匿名时刻（后面带登录的那一次得和它隔开）。"""
    world = wire(monkeypatch, tmp_path, World())
    world.jars["safari"] = [FakeCookie(".tiktok.com", "ttwid")]
    clock = fake_clock(monkeypatch, world)
    resolver._stamp_anon()
    clock.now += 1.0

    async def go():
        resolver._fresh_note()
        return await resolver._resolve_from_page(ROOM, browser="safari")

    assert run(go()) == (None, False)
    assert world.requests() == [("page", "ttwid=" + SENTINEL)] and clock.slept == []
    assert resolver._ANON["last"] == 1001.0
    assert resolver._carries_login("ttwid=a; sid_guard=b") is True
    assert resolver._carries_login("ttwid=a; msToken=sessionid") is False
    assert resolver._carries_login(None) is False


def test_hidden_webkit_page_load_is_stamped_as_anonymous(monkeypatch):
    """真的 _run_webkit_fetch，假的子进程：绝不真的起 WebKit。"""
    spawned = []

    class Proc:
        returncode = 0

        async def communicate(self):
            return b'{"url": null}\n', b""

    async def spawn(*cmd, **kwargs):
        spawned.append(cmd)
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)   # 先换掉，再放开下面两个闸
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("TLT_NO_WEBKIT", raising=False)
    monkeypatch.setattr(resolver, "_monotonic", lambda: 555.0)
    resolver._ANON["last"] = None
    assert run(resolver._run_webkit_fetch(ROOM, 10)) == {"url": None}
    assert len(spawned) == 1 and resolver._ANON["last"] == 555.0


# ---- 7：cookies_browser_only 与 --cookies-browser 的先后 ---------------------

def test_cookies_browser_only_restricts_the_order_and_the_flag_still_wins(monkeypatch,
                                                                          tmp_path):
    wire(monkeypatch, tmp_path, World(), installed=("safari", "chrome", "firefox"))
    assert resolver._browser_order("auto") == ("safari", "chrome", "firefox")
    settings.save_setting("cookies_browser", "chrome")             # 上次成功的是 Chrome
    assert resolver._browser_order("auto") == ("safari", "chrome", "firefox")
    settings.save_setting("cookies_browser_only", "safari")
    assert resolver._browser_order("auto") == ("safari",)
    assert resolver._browser_order("chrome") == ("chrome",)        # 显式的 --cookies-browser 赢
    assert resolver._browser_order("firefox") == ("firefox",)
    settings.save_setting("cookies_browser_only", " Chrome ")
    assert resolver._browser_order("auto") == ("chrome",)
    for junk in ("netscape", "", None, 7, ["safari"]):
        settings.save_setting("cookies_browser_only", junk)
        assert resolver._browser_order("auto") == ("safari", "chrome", "firefox")


def test_cookies_browser_only_safari_never_touches_chrome(monkeypatch, tmp_path):
    world = wire(monkeypatch, tmp_path, World())
    settings.save_setting("cookies_browser_only", "safari")
    world.jars["safari"] = PermissionError(1, "Operation not permitted")
    world.jars["chrome"] = logged_in_jar()
    probed = []
    monkeypatch.setattr(bl, "probe", lambda b: probed.append(b) or bl.OK)
    with pytest.raises(resolver.ResolveError):
        resolve([])
    assert set(world.reads()) == {"safari"} and "chrome" not in probed
    assert ("ytdlp", "chrome") not in world.events


def test_explicit_flag_reads_only_that_browser(monkeypatch, tmp_path):
    world = wire(monkeypatch, tmp_path, World())
    settings.save_setting("cookies_browser_only", "safari")
    world.jars["safari"] = logged_in_jar()
    world.jars["chrome"] = logged_in_jar()
    trace = []
    assert resolve(trace, cookies_browser="chrome") == FLV
    assert world.reads() == ["chrome"] and trace[0]["browser"] == "chrome"


def test_cookies_browser_none_skips_the_login_step(monkeypatch, tmp_path):
    world = wire(monkeypatch, tmp_path, World())
    world.jars["safari"] = logged_in_jar()
    world.api = "url"
    trace = []
    assert resolve(trace, cookies_browser="none") == API_FLV
    assert world.reads() == [] and layers(trace) == [("官方接口", "url")]


# ---- 8：登录那一步有总预算 ---------------------------------------------------

def test_a_hanging_cookie_read_costs_at_most_the_budget(monkeypatch, tmp_path):
    world = wire(monkeypatch, tmp_path, World(), installed=("safari",))
    world.api = "url"
    monkeypatch.setattr(resolver, "LOGIN_STEP_BUDGET_SEC", 0.3)
    started = []

    def hangs(browser):
        started.append(browser)
        time.sleep(1.5)
        return bl.LoginRead("sessionid=" + SENTINEL, "ok")

    monkeypatch.setattr(resolver, "_read_login", hangs)

    async def go():
        t0 = time.monotonic()
        trace = []
        got = await resolver.resolve_stream_url(ROOM, trace=trace)
        first = time.monotonic() - t0
        # 第二次解析时那次读取还没返回：不再起一个线程，也不再陪它等一个预算
        t1 = time.monotonic()
        again = []
        await resolver.resolve_stream_url(ROOM, trace=again)
        return got, trace, first, again, time.monotonic() - t1

    got, trace, first, again, second = run(go())
    assert got == API_FLV                           # 不登录也能解析的直播间照常解析
    assert 0.3 <= first < 1.0                       # 被拖慢的只有预算那么多，不是 1.5 秒
    assert layers(trace) == [("登录直播页", "budget"), ("官方接口", "url")]
    assert trace[0]["login"] == {"safari": "keychain_wait"} and trace[0]["browser"] is None
    assert started == ["safari"] and second < 0.25   # 没有再起一次读取，也没有再等一个预算
    assert layers(again) == [("登录直播页", "no_login"), ("官方接口", "url")]
    assert again[0]["login"] == {"safari": "keychain_wait"}
    assert SENTINEL not in json.dumps([trace, again], ensure_ascii=False)


def test_a_slow_logged_in_page_costs_at_most_the_budget(monkeypatch, tmp_path):
    world = wire(monkeypatch, tmp_path, World(), installed=("safari",))
    world.jars["safari"] = logged_in_jar()
    world.api = "url"
    world.page_delay = 1.5
    monkeypatch.setattr(resolver, "LOGIN_STEP_BUDGET_SEC", 0.3)

    async def go():
        t0 = time.monotonic()
        trace = []
        got = await resolver.resolve_stream_url(ROOM, trace=trace)
        return got, trace, time.monotonic() - t0

    got, trace, elapsed = run(go())
    assert got == API_FLV and 0.3 <= elapsed < 1.0
    assert layers(trace) == [("登录直播页", "budget"), ("官方接口", "url")]
    assert trace[0]["browser"] == "safari" and "budget" in trace[0]["why"]


def test_budget_constant_is_a_few_seconds():
    assert 2 <= resolver.LOGIN_STEP_BUDGET_SEC <= 10


# ---- 9 / 10 / 11：没有登录时的持续提示、session_start、cookie 的值哪里都不进 -----

class RecordingServer(CaptionServer):
    def __init__(self):
        super().__init__(port=8765)
        self.messages = []

    async def broadcast(self, msg):
        self.messages.append(dict(msg))
        await super().broadcast(msg)

    async def status(self, state, detail=""):
        self.messages.append({"type": "status", "state": state, "detail": detail})


class StubAudit:
    def __init__(self):
        self.records = []

    def resolve(self, record):
        self.records.append(record)


def make_pipeline(monkeypatch, tmp_path, stub_audit=True):
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(settings, "_corrupt", {"backup": None, "announced": False})
    terms = tmp_path / "banned_terms.txt"
    terms.write_text("cura el cancer", encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms)
    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source="es", beam=5, context=False,
        asr_temperature=None, glossary=None, backend="auto", model=None, device="auto",
        compute_type="auto", denoise="off", banned_terms=None, comments=False)
    server = RecordingServer()
    p = Pipeline(args, server)
    p.translator = None
    p.BROWSER_ONLY_RETRY_SEC = 0.01

    async def nothing(*a, **k):
        return None

    p._provision_then_check = nothing
    audit = None
    if stub_audit:
        audit = p.audit = StubAudit()
    return p, server, audit


INCIDENT = Pipeline.LOGIN_INCIDENT


def test_no_readable_login_raises_a_persistent_incident_and_monitoring_still_starts(
        monkeypatch, tmp_path, capsys):
    world = wire(monkeypatch, tmp_path, World())
    world.jars["safari"] = PermissionError(1, "Operation not permitted")
    world.jars["chrome"] = [FakeCookie(".tiktok.com", "ttwid")]
    world.api = "url"
    p, server, audit = make_pipeline(monkeypatch, tmp_path)

    async def first():
        media = await p._resolve_media(ROOM)
        await p._clear_session_incidents()           # 换场：这条不属于哪一场，留着
        return media, dict(server.config["incidents"][INCIDENT])

    media, banner = run(first())
    assert media == API_FLV                           # 不挡开播：照常匿名解析、照常监听
    assert banner["level"] == "warn"
    text = banner["text"]
    assert "Safari：系统拒绝读取" in text
    assert "Chrome" not in text and world.reads() == ["safari"]     # 这一步只读 Safari
    assert "只把流地址给已登录的观众" in text and "其余直播间照常监听" in text
    assert bl.SAFARI_LOGIN_STEPS in text and bl.FDA_STEPS in text
    assert "自检「浏览器登录态」" in text
    assert not any(word in text for word in GUESSED_LABELS)
    assert not INCIDENT.startswith("session:")

    # 同样的观察再来一次：不重复推同一句话
    sent = len([m for m in server.messages if m.get("id") == INCIDENT])
    run(p._resolve_media(ROOM, reconnect=1))
    assert len([m for m in server.messages if m.get("id") == INCIDENT]) == sent

    # 中控在 Safari 里登录、给了权限：下一次解析读到登录，提示立刻撤掉
    world.jars["safari"] = logged_in_jar()
    assert run(p._resolve_media(ROOM, reconnect=2)) == FLV
    assert INCIDENT not in server.config["incidents"]
    assert [m["level"] for m in server.messages if m.get("id") == INCIDENT] == ["warn", "clear"]
    assert SENTINEL not in capsys.readouterr().out


@pytest.mark.parametrize("observed,must_have,must_not_have", [
    ({"safari": "blocked_by_system"}, [bl.SAFARI_LOGIN_STEPS, bl.FDA_STEPS], [bl.KEYCHAIN_STEPS]),
    ({"safari": "not_logged_in"}, [bl.SAFARI_LOGIN_STEPS], ["完全磁盘访问权限"]),
    ({"safari": "no_browser_data", "chrome": "keychain_wait"},
     [bl.SAFARI_LOGIN_STEPS, bl.KEYCHAIN_STEPS], ["完全磁盘访问权限"]),
])
def test_no_login_notice_says_what_was_seen_and_what_to_do(observed, must_have, must_not_have):
    text = bl.no_login_notice(observed)
    assert bl.observed_text(observed) in text
    for part in must_have:
        assert part in text
    for part in must_not_have:
        assert part not in text
    assert not any(word in text for word in GUESSED_LABELS)


def test_incident_is_left_alone_when_nothing_was_read(monkeypatch, tmp_path):
    """测试环境 / TLT_NO_BROWSER：什么都没去读，就不说「没读到」。"""
    world = wire(monkeypatch, tmp_path, World())
    world.api = "url"
    monkeypatch.setattr(bl, "_load_extractor", lambda: None)
    p, server, _audit = make_pipeline(monkeypatch, tmp_path)
    assert run(p._resolve_media(ROOM)) == API_FLV
    assert INCIDENT not in (server.config.get("incidents") or {})


def _session_start(path):
    return json.loads(path.read_text(encoding="utf-8").splitlines()[0])


def test_session_start_records_the_login_source_by_name_only(monkeypatch, tmp_path):
    wire(monkeypatch, tmp_path, World())
    probed = []

    def probe(browser):
        probed.append(browser)
        return bl.OK if browser == "safari" else bl.NOT_LOGGED_IN

    monkeypatch.setattr(bl, "probe", probe)
    p, _server, _ = make_pipeline(monkeypatch, tmp_path, stub_audit=False)

    async def go():
        await p._begin_session(ROOM)
        path = p.audit.path
        await p._end_session()
        return path

    head = _session_start(run(go()))
    assert head["type"] == "session_start" and head["login_source"] == "safari"
    assert probed == ["safari"]                      # Safari 有登录：Chrome 连探测都没做
    account_keys = [k for k in head if "login" in k or "cookie" in k or "account" in k]
    assert account_keys == ["login_source"]


def test_login_source_looks_only_at_safari_when_no_browser_is_named(monkeypatch, tmp_path):
    wire(monkeypatch, tmp_path, World())
    probed = []

    def probe(browser):
        probed.append(browser)
        return bl.OK if browser == "chrome" else bl.BLOCKED

    monkeypatch.setattr(bl, "probe", probe)
    assert resolver.login_source("auto") is None and probed == ["safari"]
    assert resolver.login_source("chrome") == "chrome"              # 点了名就看点名的那个
    settings.save_setting("cookies_browser_only", "chrome")
    assert resolver.login_source("auto") == "chrome"
    assert resolver._login_first_browsers("none") == ()


@pytest.mark.parametrize("platform,probe_code,flag,expected", [
    ("darwin", bl.BLOCKED, "auto", None),
    ("darwin", bl.OK, "none", None),
    ("linux", bl.OK, "auto", None),
    ("win32", bl.OK, "auto", None),
])
def test_session_start_login_source_is_null_otherwise(monkeypatch, tmp_path, platform,
                                                      probe_code, flag, expected):
    wire(monkeypatch, tmp_path, World(), platform=platform)
    monkeypatch.setattr(bl, "probe", lambda b: probe_code)
    p, _server, _ = make_pipeline(monkeypatch, tmp_path, stub_audit=False)
    p.args.cookies_browser = flag

    async def go():
        await p._begin_session(ROOM)
        path = p.audit.path
        await p._end_session()
        return path

    head = _session_start(run(go()))
    assert "login_source" in head and head["login_source"] is expected


def test_cookie_values_go_to_tiktok_and_nowhere_else(monkeypatch, tmp_path, capsys):
    """成功、失败各一次，整条管线：trace、层的 why、审计、提示文字、异常文本、终端输出、
    发给界面的每一条消息里都没有 cookie 的值。"""
    world = wire(monkeypatch, tmp_path, World())
    world.jars["safari"] = RuntimeError("could not parse near " + SENTINEL)
    world.jars["chrome"] = logged_in_jar()
    p, server, audit = make_pipeline(monkeypatch, tmp_path)

    assert run(p._resolve_media(ROOM)) == FLV                       # 借 Chrome 的登录拿到
    assert ("page", HEADER) in world.events                          # 值确实发给了 TikTok
    world.page_with_login = GATE_PAGE
    with pytest.raises(resolver.ResolveError) as exc:                # 带了登录也没拿到
        run(p._resolve_media(ROOM, reconnect=1))
    world.jars["chrome"] = PermissionError(1, "Operation not permitted: " + SENTINEL)
    with pytest.raises(resolver.ResolveError) as blocked:            # 两个都读不到：挂提示
        run(p._resolve_media(ROOM, reconnect=2))
    assert INCIDENT in server.config["incidents"]

    captured = capsys.readouterr()
    everything = "\n".join([
        json.dumps(audit.records, ensure_ascii=False, default=str),
        json.dumps(server.messages, ensure_ascii=False, default=str),
        json.dumps(server.config.get("incidents"), ensure_ascii=False, default=str),
        str(exc.value), repr(exc.value), repr(exc.value.login),
        str(blocked.value), repr(blocked.value), repr(blocked.value.login),
        captured.out, captured.err])
    assert "登录直播页" in everything and "error:RuntimeError" in everything
    assert SENTINEL not in everything
    assert "sessionid" not in json.dumps(audit.records)              # 审计里连 cookie 的名字都没有


# ---- 弹幕子进程：它一启动就匿名抓同一个直播页 --------------------------------

class _StopHere(Exception):
    pass


def _pipeline_with_comments(monkeypatch, tmp_path, world):
    from app import hwdetect
    from app.comment_source import CommentSource

    p, _server, _ = make_pipeline(monkeypatch, tmp_path, stub_audit=False)
    p.args.comments = True

    async def stop(self, _external=True):
        world.events.append(("comments_stop",))

    def stop_here(**_kwargs):
        raise _StopHere()                      # 解析之后就是加载识别模型：测试到此为止

    monkeypatch.setattr(CommentSource, "start",
                        lambda self, unique_id: world.events.append(("comments", unique_id)))
    monkeypatch.setattr(CommentSource, "stop", stop)
    monkeypatch.setattr(hwdetect, "recommend", stop_here)
    return p


def test_comment_worker_starts_only_after_the_first_resolve_on_macos(monkeypatch, tmp_path):
    """TikTokLive 不带 sessionid 启动时先匿名抓 https://www.tiktok.com/@主播/live。弹幕在
    _begin_session 里就起的话，这一场第一个带登录的请求就排在它后面一两秒。"""
    world = wire(monkeypatch, tmp_path, World())
    world.jars["safari"] = logged_in_jar()
    p = _pipeline_with_comments(monkeypatch, tmp_path, world)

    async def go():
        await p._begin_session(ROOM)
        begun = list(world.events)
        with pytest.raises(_StopHere):
            await p._run_session(ROOM)
        await p._end_session()
        return begun

    begun = run(go())
    assert not [e for e in begun if e[0] == "comments"]      # 开场时还没起
    order = [e for e in world.events if e[0] in ("page", "comments")]
    assert order == [("page", HEADER), ("comments", "x")]    # 带登录的请求在前，弹幕在后
    assert p._comments_pending is None


def test_comment_worker_is_not_started_when_the_first_resolve_fails(monkeypatch, tmp_path):
    world = wire(monkeypatch, tmp_path, World())
    world.jars["safari"] = logged_in_jar()
    world.page_with_login = GATE_PAGE
    p = _pipeline_with_comments(monkeypatch, tmp_path, world)

    async def go():
        await p._begin_session(ROOM)
        await p._run_session(ROOM)               # 解析失败：这一场到此结束
        await p._end_session()
        await p._start_pending_comments(None)    # 收尾之后没有留下待起的那一笔

    run(go())
    assert not [e for e in world.events if e[0] == "comments"]
    assert p._comments_pending is None


def test_a_late_old_task_cannot_start_comments_for_the_next_session(monkeypatch, tmp_path):
    world = wire(monkeypatch, tmp_path, World())
    p = _pipeline_with_comments(monkeypatch, tmp_path, world)

    async def go():
        await p._begin_session(ROOM)
        old_audit = p.audit
        await p._begin_session("https://www.tiktok.com/@y/live")
        await p._start_pending_comments(old_audit)           # 上一场晚到的任务
        late = [e for e in world.events if e[0] == "comments"]
        await p._start_pending_comments(p.audit)             # 这一场自己的
        await p._end_session()
        return late

    assert run(go()) == []
    assert [e for e in world.events if e[0] == "comments"] == [("comments", "y")]


@pytest.mark.parametrize("platform,flag", [("linux", "auto"), ("win32", "auto"),
                                           ("darwin", "none")])
def test_comment_worker_starts_at_once_where_no_login_step_runs(monkeypatch, tmp_path,
                                                                 platform, flag):
    world = wire(monkeypatch, tmp_path, World(), platform=platform)
    p = _pipeline_with_comments(monkeypatch, tmp_path, world)
    p.args.cookies_browser = flag

    async def go():
        await p._begin_session(ROOM)
        begun = list(world.events)
        await p._end_session()
        return begun

    assert ("comments", "x") in run(go())


# ---- cookie 只发给 https 的 tiktok.com（含重定向的每一跳）---------------------

@pytest.mark.parametrize("room", [
    "https://www.tiktok.com.evil.example/@x/live",
    "https://evil.example/www.tiktok.com/@x/live",
    "http://www.tiktok.com.evil.example/@x/live",
    "http://www.tiktok.com@evil.example/@x/live",
])
def test_a_link_that_is_not_tiktok_never_gets_the_cookie(monkeypatch, tmp_path, room):
    world = wire(monkeypatch, tmp_path, World())
    world.jars["safari"] = logged_in_jar()
    seen = []

    class Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, url, headers=None, **kwargs):
            seen.append((url, (headers or {}).get("Cookie")))
            raise OSError("no network in tests")

    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    trace = []
    with pytest.raises(resolver.ResolveError):
        run(resolver.resolve_stream_url(room, trace=trace))
    assert "登录直播页" not in [r["layer"] for r in trace]      # 这一步整个没有发生
    assert seen and all(cookie is None for _url, cookie in seen)
    page = [r for r in trace if r["layer"] == "直播页兜底"][0]
    assert "cookie_not_sent: not_tiktok_host" in page["why"]


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_an_http_tiktok_link_is_upgraded_to_https_and_keeps_the_login(monkeypatch, tmp_path,
                                                                     platform):
    """界面接受 http:// 开头的链接。cookie 不走明文 http——换成 https 再解析，而不是整场不用
    登录：只对已登录观众给地址的直播间，以前（66f054e）粘 http 链接是解析得出来的。"""
    world = wire(monkeypatch, tmp_path, World(), platform=platform)
    world.jars["safari"] = logged_in_jar()
    world.jars["chrome"] = logged_in_jar()
    seen = []
    _redirecting_session(monkeypatch, {}, seen, anonymous_page=GATE_PAGE)
    trace = []
    got = run(resolver.resolve_stream_url("http://www.tiktok.com/@x/live", trace=trace))
    assert got == FLV
    assert seen and all(url == ROOM for url, _cookie, _redirects in seen)   # 没有一个明文请求
    assert (ROOM, HEADER, False) in seen                     # 登录带上了
    assert "cookie_not_sent" not in json.dumps(trace, ensure_ascii=False)
    if platform == "darwin":
        assert seen == [(ROOM, HEADER, False)]               # 第一个请求就是带登录的 https 直播页
        assert layers(trace) == [("登录直播页", "url")]
    else:
        assert layers(trace)[-1] == ("直播页兜底", "url")    # 其它平台：链路不变，登录照旧用得上


def test_upgrade_touches_only_plain_http_tiktok_links():
    up = resolver._upgrade_tiktok_scheme
    assert up("http://www.tiktok.com/@x/live?lang=en") == "https://www.tiktok.com/@x/live?lang=en"
    assert up("http://TikTok.com:80/@x/live") == "https://tiktok.com/@x/live"
    assert up("http://m.tiktok.com/@x/live") == "https://m.tiktok.com/@x/live"
    for untouched in (ROOM, "http://www.tiktok.com:8080/@x/live",
                      "http://user:pw@www.tiktok.com/@x/live",
                      "http://www.tiktok.com.evil.example/@x/live",
                      "http://www.tiktok.com@evil.example/@x/live",
                      "http://nottiktok.com/@x/live", "ftp://www.tiktok.com/@x/live",
                      "http://[::1", "", None):
        assert up(untouched) == untouched


def test_a_plain_http_fetch_never_carries_the_cookie(monkeypatch, tmp_path):
    """_fetch_live_page 自己仍然不把 cookie 发到明文 http 上，trace 里说的是 not_https。"""
    wire(monkeypatch, tmp_path, World())
    seen = []
    _redirecting_session(monkeypatch, {}, seen)

    async def go():
        note = resolver._fresh_note()
        got = await resolver._fetch_live_page("http://www.tiktok.com/@x/live", cookie=HEADER)
        return got, note

    got, note = run(go())
    assert got == (FLV, False)
    assert seen == [("http://www.tiktok.com/@x/live", None, None)]
    assert note["why"] == ["cookie_not_sent: not_https"]


def _redirecting_session(monkeypatch, hops, seen, anonymous_page=LIVE_PAGE):
    """hops：{地址: 重定向到哪}；不在里面的地址回 200 + 带流地址的页面（不带 cookie 的请求
    回 anonymous_page）。"""
    class Resp:
        charset = "utf-8"

        def __init__(self, url, cookie=None):
            self.status = 302 if url in hops else 200
            self.headers = {"Location": hops[url]} if url in hops else {}
            self.body = LIVE_PAGE if cookie else anonymous_page

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

        def get(self, url, headers=None, **kwargs):
            cookie = (headers or {}).get("Cookie")
            seen.append((url, cookie, kwargs.get("allow_redirects")))
            return Resp(url, cookie)

    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession", Session)


def test_logged_in_fetch_follows_redirects_inside_tiktok_only(monkeypatch, tmp_path):
    world = wire(monkeypatch, tmp_path, World())
    world.jars["safari"] = logged_in_jar()
    seen = []
    _redirecting_session(monkeypatch, {ROOM: "/@x/live?lang=en"}, seen)
    assert resolve([]) == FLV
    assert seen == [(ROOM, HEADER, False),
                    ("https://www.tiktok.com/@x/live?lang=en", HEADER, False)]


def test_logged_in_fetch_does_not_follow_a_redirect_off_tiktok(monkeypatch, tmp_path):
    world = wire(monkeypatch, tmp_path, World())
    world.jars["safari"] = logged_in_jar()
    world.api = "url"
    seen = []
    _redirecting_session(monkeypatch, {ROOM: "https://evil.example/collect"}, seen)
    trace = []
    assert resolve(trace) == API_FLV
    assert seen == [(ROOM, HEADER, False)]                  # 没有跟出去，cookie 没到别的主机
    assert layers(trace)[0] == ("登录直播页", "none")
    assert "redirect_off_tiktok" in trace[0]["why"]


def _api_session(monkeypatch, redirect_to, seen):
    """房间接口的假会话：匿名请求回 4003110；带 Cookie 的请求回 302 → redirect_to。
    和 aiohttp 一样：没传 allow_redirects=False 就自己跟过去，请求头（含 Cookie）原样带上。"""
    class Resp:
        charset = "utf-8"

        def __init__(self, status, body=b"", location=None):
            self.status, self.body = status, body
            self.headers = {"Location": location} if location else {}

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

        def get(self, url, headers=None, **kwargs):
            cookie = (headers or {}).get("Cookie")
            seen.append((url, cookie))
            if not cookie:
                return Resp(200, json.dumps(_GATED).encode("utf-8"))
            if kwargs.get("allow_redirects", True):
                seen.append((redirect_to, cookie))            # aiohttp 会这样跟过去
                return Resp(200, b'{"data": {}}')
            return Resp(302, location=redirect_to)

    async def read_all(resp, _limit):
        return resp.body

    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    monkeypatch.setattr(resolver, "read_all", read_all)


def test_logged_in_api_retry_does_not_follow_a_redirect_off_tiktok(monkeypatch, tmp_path):
    """4003110 之后借登录重问房间接口的那一次，用真的 _get_json。接口回 30x 指到别的主机时，
    cookie 不跟过去——和直播页那一条一样。"""
    world = wire(monkeypatch, tmp_path, World())
    world.jars["safari"] = logged_in_jar()
    monkeypatch.setattr(resolver, "_get_json", REAL_GET_JSON)      # wire() 换掉的那个换回来
    seen = []
    _api_session(monkeypatch, "https://evil.example/collect", seen)

    async def go():
        note = resolver._fresh_note()
        with pytest.raises(resolver.ResolveError) as exc:
            await resolver._resolve_via_api(ROOM)
        return exc.value, note

    err, note = run(go())
    assert err.kind == "browser_only"
    webcast = resolver._WEBCAST_API.format(room="123")
    assert seen == [(webcast, None), (webcast, HEADER)]      # 带 cookie 的请求只到过 TikTok
    assert "http=302" in note["why"]
    assert SENTINEL not in json.dumps(note, ensure_ascii=False) and SENTINEL not in str(err)


def test_get_json_never_sends_a_cookie_to_another_host(monkeypatch):
    seen = []
    _api_session(monkeypatch, "https://evil.example/collect", seen)
    import aiohttp

    async def go():
        note = resolver._fresh_note()
        async with aiohttp.ClientSession() as session:
            got = await REAL_GET_JSON(session, "https://evil.example/webcast/room/info/",
                                      headers={"User-Agent": "x", "Cookie": HEADER})
        return got, note

    got, note = run(go())
    assert got == _GATED                                      # 照常请求，只是不带 cookie
    assert seen == [("https://evil.example/webcast/room/info/", None)]
    assert note["why"] == ["cookie_not_sent: not_tiktok_host"]


# ---- 12：其它平台保持原样 ----------------------------------------------------

@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_other_platforms_keep_the_old_chain(monkeypatch, tmp_path, platform):
    world = wire(monkeypatch, tmp_path, World(), platform=platform,
                 installed=("chrome", "safari"))
    world.jars["chrome"] = logged_in_jar()
    world.jars["safari"] = logged_in_jar()
    world.page_with_login = GATE_PAGE
    clock = fake_clock(monkeypatch, world)
    settings.save_setting("cookies_browser", "chrome")
    assert resolver._browser_order("auto") == ("chrome", "safari")
    trace = []
    with pytest.raises(resolver.ResolveError) as exc:
        resolve(trace)
    assert exc.value.kind == "browser_only"
    assert "登录直播页" not in [r["layer"] for r in trace]
    assert world.requests()[0] == ("api", None)                      # 第一个请求照旧是匿名接口
    # 直播页兜底照旧：匿名 → 各浏览器，背靠背，不等间隔
    assert world.requests()[-3:] == [("page", None), ("page", HEADER), ("page", HEADER)]
    assert clock.slept == [] and all("waited_ms" not in r for r in trace)
    assert exc.value.login == {"chrome": "ok", "safari": "ok"}       # 两个浏览器照旧都读


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_other_platforms_raise_no_login_incident(monkeypatch, tmp_path, platform):
    world = wire(monkeypatch, tmp_path, World(), platform=platform,
                 installed=("chrome",))
    world.jars["chrome"] = PermissionError(1, "Operation not permitted")
    world.api = "url"
    p, server, _audit = make_pipeline(monkeypatch, tmp_path)
    assert run(p._resolve_media(ROOM)) == API_FLV
    assert INCIDENT not in (server.config.get("incidents") or {})
    assert world.reads() == []                       # 匿名就解析出来了：和以前一样不读浏览器
