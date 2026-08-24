"""解析链路的分层兜底。

存在的理由：一个确实在播的房间被报成「未开播」。yt-dlp 的 TikTok 提取器被挡
（HTTP 400）后，会把任何失败都翻译成 "The channel is not currently live"——
那句话是错的，而我们照抄给了用户。
"""
import asyncio
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
