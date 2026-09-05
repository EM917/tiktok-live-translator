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


# ---- 年龄限制（18+）直播间：借登录态，借不到就把话说清楚 ----

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
    _age_gate_setup(monkeypatch, cookie="sessionid=abc")
    url, offline = run(resolver._resolve_via_api("https://www.tiktok.com/@x/live"))
    assert url == "https://pull.example/room.flv" and offline is False


def test_age_gated_room_without_any_login_explains_itself(monkeypatch):
    _age_gate_setup(monkeypatch, cookie=None)
    with pytest.raises(resolver.ResolveError) as exc:
        run(resolver._resolve_via_api("https://www.tiktok.com/@x/live"))
    assert exc.value.kind == "login"
    assert "年龄限制" in str(exc.value)


def test_age_gated_room_names_the_browser_it_tried(monkeypatch):
    _age_gate_setup(monkeypatch, cookie="sessionid=abc", gated_even_with_cookie=True)
    with pytest.raises(resolver.ResolveError) as exc:
        run(resolver._resolve_via_api("https://www.tiktok.com/@x/live"))
    assert exc.value.kind == "login"
    assert "chrome" in str(exc.value) and "18+" in str(exc.value)


def test_age_gate_is_skipped_when_browser_cookies_are_disabled(monkeypatch):
    _age_gate_setup(monkeypatch, cookie="sessionid=abc")
    with pytest.raises(resolver.ResolveError) as exc:
        run(resolver._resolve_via_api("https://www.tiktok.com/@x/live", cookies_browser="none"))
    assert "Chrome 或 Safari" in str(exc.value)     # 没试任何浏览器，提示去登录
