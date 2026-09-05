"""解析链路的分层兜底。

存在的理由：一个确实在播的房间被报成「未开播」。yt-dlp 的 TikTok 提取器被挡
（HTTP 400）后，会把任何失败都翻译成 "The channel is not currently live"——
那句话是错的，而我们照抄给了用户。
"""
import asyncio

import pytest
import json

from app import resolver


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# ---- 从官方接口的返回里挑地址 ----

def _payload(qualities):
    return {"stream_url": {"live_core_sdk_data": {"pull_data": {
        "stream_data": json.dumps({"data": qualities})}}}}


def test_prefers_audio_only_over_video():
    """只要声音。纯音频档省掉整条视频码流——要连着盯几小时，这不是微优化。"""
    picked = resolver._pick_stream(_payload({
        "hd": {"main": {"flv": "https://cdn/hd.flv"}},
        "ao": {"main": {"flv": "https://cdn/a.flv?only_audio=1"}},
    }))
    assert picked == "https://cdn/a.flv?only_audio=1"


def test_falls_back_to_any_quality_when_no_audio_track():
    picked = resolver._pick_stream(_payload({"hd": {"main": {"flv": "https://cdn/hd.flv"}}}))
    assert picked == "https://cdn/hd.flv"


def test_falls_back_to_flv_pull_url_map():
    picked = resolver._pick_stream({"stream_url": {"flv_pull_url": {"HD1": "https://cdn/x.flv"}}})
    assert picked == "https://cdn/x.flv"


def test_pick_stream_survives_garbage():
    """接口改版时宁可返回 None 走下一层，不能抛异常把整条链路带崩。"""
    for bad in (None, {}, {"stream_url": None},
                {"stream_url": {"live_core_sdk_data": {"pull_data": {"stream_data": "не json"}}}}):
        assert resolver._pick_stream(bad) is None


# ---- 用户名解析 ----

def test_username_extraction():
    assert resolver._username("https://www.tiktok.com/@bellaallnatural/live") == "bellaallnatural"
    assert resolver._username("https://www.tiktok.com/@some.user_1/live") == "some.user_1"
    assert resolver._username("https://example.com/x") is None


# ---- 「确认下播」与「没解析出来」必须分开 ----

def test_api_reports_offline_only_on_an_explicit_status(monkeypatch):
    """只有接口明确说房间结束了，才敢告诉用户主播没在播。

    这个区分对重连策略有实质影响：确认下播就该收手，没解析出来则应该继续重试。
    """
    async def fake_status(_session, _user):
        return "123", 4                      # 4 = 已结束

    monkeypatch.setattr(resolver, "_room_status", fake_status)
    url, offline = run(resolver._resolve_via_api("https://www.tiktok.com/@x/live"))
    assert url is None and offline is True


def test_api_does_not_claim_offline_when_it_simply_failed(monkeypatch):
    async def fake_status(_session, _user):
        return None, None                    # 接口没答上来

    monkeypatch.setattr(resolver, "_room_status", fake_status)
    url, offline = run(resolver._resolve_via_api("https://www.tiktok.com/@x/live"))
    assert url is None and offline is False  # 不能替它下断言


def test_unknown_username_is_not_offline():
    url, offline = run(resolver._resolve_via_api("https://example.com/nope"))
    assert url is None and offline is False


# ---- 直播页解析：靠状态位把关 ----

def _page(status, with_stream=True):
    room = {"status": status}
    if with_stream:
        room["streamData"] = {"pull_data": {"stream_data": json.dumps(
            {"data": {"ao": {"main": {"flv": "https://cdn/a.flv?only_audio=1"}}}})}}
    sigi = {"LiveRoom": {"liveRoomUserInfo": {"liveRoom": room}}}
    return '<script id="SIGI_STATE" type="application/json">{}</script>'.format(
        json.dumps(sigi))


def test_live_page_yields_the_audio_only_url():
    url, offline = resolver._parse_live_page(_page(2))
    assert url == "https://cdn/a.flv?only_audio=1"
    assert offline is False


def test_ended_room_is_reported_offline_not_given_a_stale_url():
    """已下播的页面里照样残留着上一场的完整流地址（含 only_audio）。

    裸正则会把它当成有效结果拿走，然后要等探活超时 8 秒才发现是 404。
    状态位一到手就该当场判定。"""
    url, offline = resolver._parse_live_page(_page(4))
    assert url is None
    assert offline is True


def test_page_without_sigi_state_falls_back_but_never_claims_offline():
    """解析不出状态时我们一无所知——绝不能替它断言主播没在播。"""
    live = "https://pull-flv-l77.tiktokcdn.com/game/stream-1.flv?only_audio=1"
    url, offline = resolver._parse_live_page("nothing structured " + live + " more")
    assert url == live
    assert offline is False


def test_malformed_sigi_state_does_not_crash():
    live = "https://pull-flv-l77.tiktokcdn.com/game/stream-2.flv"
    url, offline = resolver._parse_live_page(
        '<script id="SIGI_STATE">{not json</script> ' + live)
    assert url == live
    assert offline is False


def test_page_fallback_always_returns_a_pair_without_cookies(monkeypatch):
    """借用浏览器登录态抓直播页时读不到 cookie，曾经裸返回 None，调用方按
    (流地址, 是否确认下播) 拆包直接崩成「内部错误」——一个年龄限制的直播间
    把前面几条路全走失败后就撞上了它（2026-09-05 实录）。"""
    import asyncio

    from app import resolver

    monkeypatch.setattr(resolver, "_cookie_header", lambda browser: None)
    result = asyncio.run(resolver._resolve_from_page(
        "https://www.tiktok.com/@someone/live", browser="chrome"))
    assert result == (None, False)


# ---- 4003110（接口拒绝给流地址）：借登录态，借不到就交给 Chrome 桥 ----
#
# PR #9 曾把这个码误判成「年龄限制（18+）」专属信号，据此提前抛错。
# 2026-09-05 实录戳穿了这个假设：一个完全不涉及年龄限制的直播间
# （@itzesantana11）一样固定收到 4003110。现在的语义是 kind="browser_only"——
# 程序自己拿不到，交给上层隔一会儿自动重试（见 pipeline._resolve_media），
# 不再编一个不一定成立的「年龄限制」理由。下面三个用例改自 PR #9 的
# 「年龄限制」用例；「借 cookie 成功」那条语义不变，原样保留。

_AGE_GATE = {"status_code": 4003110, "data": {"prompts": "confirm your age"}}
_WITH_STREAM = {"status_code": 0, "data": {"status": 2, "stream_url": {
    "flv_pull_url": {"FULL_HD1": "https://pull.example/room.flv"}}}}


def _age_gate_setup(monkeypatch, cookie, gated_even_with_cookie=False):
    async def fake_status(_session, _user):
        return "123", 2

    async def fake_json(_session, _url, limit=None, headers=None):
        if headers and headers.get("Cookie") and not gated_even_with_cookie:
            return _WITH_STREAM
        return _AGE_GATE

    monkeypatch.setattr(resolver, "_room_status", fake_status)
    monkeypatch.setattr(resolver, "_get_json", fake_json)
    monkeypatch.setattr(resolver, "_browser_order", lambda pref: ("chrome",))
    monkeypatch.setattr(resolver, "_cookie_header", lambda browser: cookie)
    monkeypatch.setattr(resolver, "_remember_browser", lambda browser: None)


def test_age_gated_room_is_resolved_with_browser_login(monkeypatch):
    """借 cookie 成功：语义不变，保留原用例。"""
    _age_gate_setup(monkeypatch, cookie="sessionid=abc")
    url, offline = run(resolver._resolve_via_api("https://www.tiktok.com/@x/live"))
    assert url == "https://pull.example/room.flv" and offline is False


def test_gated_room_without_any_login_is_browser_only(monkeypatch):
    _age_gate_setup(monkeypatch, cookie=None)
    with pytest.raises(resolver.ResolveError) as exc:
        run(resolver._resolve_via_api("https://www.tiktok.com/@x/live"))
    assert exc.value.kind == "browser_only"
    assert "4003110" in str(exc.value)


def test_gated_room_still_gated_after_cookie_is_browser_only(monkeypatch):
    _age_gate_setup(monkeypatch, cookie="sessionid=abc", gated_even_with_cookie=True)
    with pytest.raises(resolver.ResolveError) as exc:
        run(resolver._resolve_via_api("https://www.tiktok.com/@x/live"))
    assert exc.value.kind == "browser_only"
    assert "4003110" in str(exc.value)


def test_gate_is_skipped_when_browser_cookies_are_disabled(monkeypatch):
    _age_gate_setup(monkeypatch, cookie="sessionid=abc")
    with pytest.raises(resolver.ResolveError) as exc:
        run(resolver._resolve_via_api("https://www.tiktok.com/@x/live", cookies_browser="none"))
    assert exc.value.kind == "browser_only"
    assert "4003110" in str(exc.value)          # 没试任何浏览器，直接需要 Chrome 桥


# ---- resolve_stream_url 的分层保险：一层内部炸了，不能带崩整条链路 ----
#
# 2026-09-05 那次「内部错误」实录的教训：某一层解析代码本身有 bug（不是
# ResolveError 那种明确判断），就必须被当场接住、继续试后面几层，而不是让
# 异常裸着往上冒。这里不装真的 yt-dlp（测试环境未必有），用 sys.modules
# 塞一个空模块骗过 `import yt_dlp` 的存在性检查即可——resolve_stream_url
# 后续只调用被 monkeypatch 掉的 _run_ytdlp / _resolve_from_page，不会真的
# 用到 yt_dlp 本体。

def _fake_yt_dlp_module(monkeypatch):
    import sys
    import types

    monkeypatch.setitem(sys.modules, "yt_dlp", types.ModuleType("yt_dlp"))


def test_resolve_stream_url_survives_api_layer_crashing(monkeypatch):
    """接口层抛出非 ResolveError 异常（比如返回结构变了、代码本身有 bug）：
    不能把整条解析链路带崩，得继续往后面几层试。"""
    _fake_yt_dlp_module(monkeypatch)

    async def boom(_url, cookies_browser="auto"):
        raise TypeError("接口层假装崩溃")

    async def fake_run_ytdlp(_url, cookies=None, browser=None, timeout=45):
        return 1, "", "The channel is not currently live"

    async def fake_resolve_from_page(_url, browser=None):
        return None, False

    monkeypatch.setattr(resolver, "_resolve_via_api", boom)
    monkeypatch.setattr(resolver, "_run_ytdlp", fake_run_ytdlp)
    monkeypatch.setattr(resolver, "_resolve_from_page", fake_resolve_from_page)

    with pytest.raises(resolver.ResolveError) as exc:
        run(resolver.resolve_stream_url("https://www.tiktok.com/@x/live",
                                        cookies_browser="none"))
    assert "内部出错已跳过" in str(exc.value)
    assert "官方接口" in str(exc.value)


def test_resolve_stream_url_survives_page_fallback_crashing(monkeypatch):
    """直播页兜底层抛出非 ResolveError 异常：同样只记一笔、继续走完，
    不能变成一句干巴巴的「内部错误」。"""
    _fake_yt_dlp_module(monkeypatch)

    async def fake_resolve_via_api(_url, cookies_browser="auto"):
        return None, False

    async def fake_run_ytdlp(_url, cookies=None, browser=None, timeout=45):
        return 1, "", "The channel is not currently live"

    async def boom(_url, browser=None):
        raise TypeError("直播页兜底假装崩溃")

    monkeypatch.setattr(resolver, "_resolve_via_api", fake_resolve_via_api)
    monkeypatch.setattr(resolver, "_run_ytdlp", fake_run_ytdlp)
    monkeypatch.setattr(resolver, "_resolve_from_page", boom)

    with pytest.raises(resolver.ResolveError) as exc:
        run(resolver.resolve_stream_url("https://www.tiktok.com/@x/live",
                                        cookies_browser="none"))
    assert "内部出错已跳过" in str(exc.value)
    assert "直播页兜底" in str(exc.value)


# ---- 第 2 层：系统 WebKit 引擎（子进程）----

def test_webkit_layer_parses_worker_output(monkeypatch):
    async def fake_run(url, timeout):
        return {"url": "https://pull-flv-x.tiktokcdn-us.com/game/a.flv?sign=1", "status": 2}

    monkeypatch.setattr(resolver, "_webkit_available", lambda: True)
    monkeypatch.setattr(resolver, "_run_webkit_fetch", fake_run)
    url, offline = run(resolver._resolve_via_webkit("https://www.tiktok.com/@x/live"))
    assert url == "https://pull-flv-x.tiktokcdn-us.com/game/a.flv?sign=1" and offline is False


def test_webkit_layer_reports_offline_and_absence(monkeypatch):
    monkeypatch.setattr(resolver, "_webkit_available", lambda: True)

    async def offline(url, timeout):
        return {"offline": True, "status": 4}

    monkeypatch.setattr(resolver, "_run_webkit_fetch", offline)
    assert run(resolver._resolve_via_webkit("https://www.tiktok.com/@x/live")) == (None, True)

    async def nothing(url, timeout):
        return None

    monkeypatch.setattr(resolver, "_run_webkit_fetch", nothing)
    assert run(resolver._resolve_via_webkit("https://www.tiktok.com/@x/live")) == (None, False)


def test_webkit_layer_is_skipped_where_unavailable(monkeypatch):
    called = []

    async def fake_run(url, timeout):
        called.append(url)
        return {"url": "https://pull-flv-x.tiktokcdn-us.com/a.flv"}

    monkeypatch.setattr(resolver, "_webkit_available", lambda: False)
    monkeypatch.setattr(resolver, "_run_webkit_fetch", fake_run)
    assert run(resolver._resolve_via_webkit("https://www.tiktok.com/@x/live")) == (None, False)
    assert called == []


def test_webkit_runner_never_spawns_under_pytest():
    """真的起浏览器引擎的那一步在 pytest 里必须短路——这条测试本身就在 pytest
    里跑，PYTEST_CURRENT_TEST 已由 pytest 设置。"""
    assert run(resolver._run_webkit_fetch("https://www.tiktok.com/@x/live", 5)) is None


def _browser_only_api(monkeypatch):
    async def api(url, cookies_browser="auto"):
        raise resolver.ResolveError("TikTok 没有把这个直播间的流地址给程序（代码 4003110）",
                                    kind="browser_only")

    monkeypatch.setattr(resolver, "_resolve_via_api", api)
    monkeypatch.setattr(resolver, "_check_media_url", _identity_check)
    monkeypatch.setattr(resolver, "_media_url_works", _always_works)


async def _identity_check(url, trusted=False):
    return url


async def _always_works(url):
    return True


def test_api_browser_only_is_rescued_by_webkit(monkeypatch):
    """接口说不给（4003110）时不能就此放弃：WebKit 那层拿到了就用它的。"""
    _browser_only_api(monkeypatch)

    async def webkit(url, timeout=None):
        return "https://pull-flv-x.tiktokcdn-us.com/game/a.flv", False

    monkeypatch.setattr(resolver, "_resolve_via_webkit", webkit)
    assert run(resolver.resolve_stream_url("https://www.tiktok.com/@x/live", cookies_browser="none")) \
        == "https://pull-flv-x.tiktokcdn-us.com/game/a.flv"


def test_api_browser_only_survives_to_the_end_when_nothing_else_works(monkeypatch):
    """接口说不给、WebKit/yt-dlp/直播页也都没拿到：最终错误必须仍是 browser_only，
    上层据此去借用户的 Chrome，而不是被 yt-dlp 的「未开播」措辞盖掉。"""
    _browser_only_api(monkeypatch)
    _fake_yt_dlp_module(monkeypatch)

    async def webkit(url, timeout=None):
        return None, False

    async def ytdlp(url, cookies=None, browser=None, timeout=None):
        return 1, "", "ERROR: [tiktok:live] x: The channel is not currently live"

    async def page(url, browser=None):
        return None, False

    monkeypatch.setattr(resolver, "_resolve_via_webkit", webkit)
    monkeypatch.setattr(resolver, "_run_ytdlp", ytdlp)
    monkeypatch.setattr(resolver, "_resolve_from_page", page)
    with pytest.raises(resolver.ResolveError) as exc:
        run(resolver.resolve_stream_url("https://www.tiktok.com/@x/live", cookies_browser="none"))
    assert exc.value.kind == "browser_only"
    assert "4003110" in str(exc.value)
