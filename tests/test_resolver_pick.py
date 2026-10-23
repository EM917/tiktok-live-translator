"""没有纯音频档时挑哪一档（专家审查 M5，2026-10-23）。

报告 M5：现网历史 119 次成功解析，选中的全是原画（origin）或 _hd——房间不给纯音频档时，_pick_stream
取的是 stream_data 里排在第一个的那一档。改法是加一个开关（resolver.LOW_BITRATE_PICK，环境变量
TLT_LOW_BITRATE_STREAM），默认关（计划 D17：低档音轨可能是转码的、可能更晚，实测过、用户点头再开）：

  * 关着：挑法和改动前逐字节相同。下面拿改动前的函数原文（_pick_stream_before_m5）对拍，键的各种排列都试；
  * 开着：有纯音频档照旧取纯音频档；没有就按画质从低到高挑，stream_data 和 flv_pull_url 都这样排；
  * 开着但拉流 ffmpeg 退回了进程内重连（TLT_FFMPEG_LEGACY=1 或 TLT_RECONNECT_V2=0）：照原来的挑法。

夹具是结构固定的假房间：虚构的房间号和 CDN 主机名，不连网、不开浏览器。
"""
import ast
import importlib.util
import itertools
import json

import pytest

from app import audio, reconnect_policy, resolver
from tests.helpers import run

CDN = "https://pull-flv-demo.example.test/stage/stream-7400000000000000001"
ENVS = (resolver.LOW_BITRATE_ENV, "TLT_FFMPEG_LEGACY", "TLT_RECONNECT_V2")


@pytest.fixture(autouse=True)
def _clean_switches(monkeypatch):
    """开发机上设过这几个环境变量不该让结果变样；每个用例要哪一边自己显式设。"""
    for name in ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(resolver, "LOW_BITRATE_PICK", False)
    monkeypatch.setattr(resolver, "_low_bitrate_skip_told", False)
    monkeypatch.setattr(audio, "FFMPEG_LEGACY", False)
    monkeypatch.setattr(reconnect_policy, "RECONNECT_V2", True)


def _switch_on(monkeypatch):
    monkeypatch.setattr(resolver, "LOW_BITRATE_PICK", True)


# ---- 改动前的函数原文（origin/main 34054b6 的 app/resolver.py:651-678），只用来对拍 ----

def _pick_stream_before_m5(data):
    su = (data or {}).get("stream_url") or {}
    sdk = ((su.get("live_core_sdk_data") or {}).get("pull_data") or {})
    raw = sdk.get("stream_data")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    opts = ((raw or {}).get("data") or {})
    for quality in ("ao",):                      # 纯音频优先
        flv = ((opts.get(quality) or {}).get("main") or {}).get("flv")
        if flv:
            return flv
    for val in opts.values():                    # 其次任意档的 flv
        flv = ((val or {}).get("main") or {}).get("flv")
        if flv:
            return flv
    flvs = su.get("flv_pull_url") or {}
    if isinstance(flvs, dict):
        for v in flvs.values():
            if v:
                return v
    return su.get("rtmp_pull_url") or su.get("hls_pull_url") or None


# ---- 夹具：照 webcast room/info 的 data 摆的形状 ----

def _url(tier, ext="flv"):
    suffix = "" if tier == "origin" else "_" + tier
    return "{}{}.{}?expire=1700000000&sign=demo".format(CDN, suffix, ext)


def _tier(tier, flv=True):
    main = {"hls": _url(tier, "m3u8"),
            "sdk_params": json.dumps({"VCodec": "h264", "resolution": "demo"})}
    if flv:
        main["flv"] = _url(tier)
    return {"main": main}


def _room(tiers=(), pull=(), as_string=True, hls=True, rtmp=False, overrides=None):
    """tiers：stream_data 里的档（按给的顺序）；pull：flv_pull_url 的键（按给的顺序）。"""
    data = {t: _tier(t) for t in tiers}
    data.update(overrides or {})
    su = {}
    if tiers or overrides:
        stream_data = {"common": {"session_id": "demo"}, "data": data}
        su["live_core_sdk_data"] = {"pull_data": {
            "options": {"default_quality": {"sdk_key": "origin"}},
            "stream_data": json.dumps(stream_data) if as_string else stream_data}}
    if pull:
        su["flv_pull_url"] = {k: "{}_{}.flv?pull={}".format(CDN, k.lower(), k) for k in pull}
    if hls:
        su["hls_pull_url"] = CDN + ".m3u8"
    if rtmp:
        su["rtmp_pull_url"] = "rtmp://pull-rtmp-demo.example.test/stage/stream-7400000000000000001"
    return {"status": 2, "stream_url": su}


def _pull(key):
    return "{}_{}.flv?pull={}".format(CDN, key.lower(), key)


FULL_LADDER = ("origin", "uhd", "hd", "sd", "ld")
FULL_PULL = ("FULL_HD1", "HD1", "SD1", "SD2")

# 结构固定的几种房间：(名字, 房间, 开关关着该挑的, 开关开着该挑的)
ROOMS = [
    ("原画排第一、整套阶梯", _room(FULL_LADDER, FULL_PULL), _url("origin"), _url("ld")),
    ("键按字母排", _room(("hd", "ld", "origin", "sd")), _url("hd"), _url("ld")),
    ("只有原画和 hd", _room(("origin", "hd")), _url("origin"), _url("hd")),
    ("只有原画", _room(("origin",), ("FULL_HD1",)), _url("origin"), _url("origin")),
    ("stream_data 已是对象不是字符串", _room(FULL_LADDER, as_string=False), _url("origin"), _url("ld")),
    ("有纯音频档", _room(("origin", "hd", "ao", "ld"), FULL_PULL), _url("ao"), _url("ao")),
    ("纯音频档没有 flv", _room(("origin", "sd", "ld"), overrides={"ao": _tier("ao", flv=False)}),
     _url("origin"), _url("ld")),
    ("最低档的 flv 是空的", _room(("origin", "hd", "sd"), overrides={"ld": {"main": {"flv": ""}}}),
     _url("origin"), _url("sd")),
    ("只有 flv_pull_url", _room(pull=FULL_PULL), _pull("FULL_HD1"), _pull("SD1")),
    ("flv_pull_url 带原样拼法的 ORIGION", _room(pull=("ORIGION", "HD1")), _pull("ORIGION"), _pull("HD1")),
    ("stream_data 一个 flv 都没有", _room(overrides={"origin": _tier("origin", flv=False)},
                                          pull=("HD1", "SD2")), _pull("HD1"), _pull("SD2")),
    ("只剩 hls", _room(), CDN + ".m3u8", CDN + ".m3u8"),
    ("rtmp 排在 hls 前面", _room(rtmp=True),
     "rtmp://pull-rtmp-demo.example.test/stage/stream-7400000000000000001",
     "rtmp://pull-rtmp-demo.example.test/stage/stream-7400000000000000001"),
]


@pytest.mark.parametrize("name,room,off,on", ROOMS, ids=[r[0] for r in ROOMS])
def test_fixture_rooms_pick_the_expected_tier(monkeypatch, name, room, off, on):
    assert resolver._pick_stream(room) == off                 # 默认（开关关着）
    assert resolver._pick_stream(room, low_first=False) == off
    assert resolver._pick_stream(room, low_first=True) == on
    _switch_on(monkeypatch)
    assert resolver._pick_stream(room) == on


# ---- 关着：和改动前逐字节相同 ----

def _permuted_rooms():
    """各种键的排列：stream_data 的 5 档 + 纯音频档有/没有 flv，flv_pull_url 的 5 个键。"""
    for order in itertools.permutations(FULL_LADDER):
        yield _room(order, FULL_PULL)
        yield _room(order[:3], overrides={"ao": _tier("ao", flv=False)})
    for order in itertools.permutations(FULL_PULL + ("ORIGION",)):
        yield _room(pull=order)
    yield _room(("origin", "hd_60", "xq", "hd"))


def test_switch_off_matches_the_function_before_m5_on_every_key_order():
    rooms = list(_permuted_rooms())
    assert len(rooms) == 120 * 2 + 120 + 1
    for room in rooms:
        before = _pick_stream_before_m5(room)
        assert resolver._pick_stream(room) == before
        assert resolver._pick_stream(room, low_first=False) == before


def test_switch_off_matches_the_function_before_m5_on_garbage():
    """接口改版时宁可返回 None 走下一层；改动前会抛的形状，关着时照样抛同一种异常（不多也不少）。"""
    shapes = [None, {}, {"stream_url": None}, {"stream_url": {"flv_pull_url": []}},
              {"stream_url": {"flv_pull_url": {"HD1": ""}, "hls_pull_url": ""}},
              {"stream_url": {"live_core_sdk_data": {"pull_data": {"stream_data": "не json"}}}},
              {"stream_url": {"live_core_sdk_data": {"pull_data": {"stream_data": "{}"}}}},
              {"stream_url": {"live_core_sdk_data": {"pull_data": {
                  "stream_data": json.dumps({"data": {"origin": None, "hd": {"main": None}}})}}}},
              {"stream_url": {"live_core_sdk_data": {"pull_data": {
                  "stream_data": json.dumps({"data": {"origin": "x"}})}}}}]
    for shape in shapes:
        try:
            before = ("ok", _pick_stream_before_m5(shape))
        except Exception as exc:
            before = ("raise", type(exc))
        for low_first in (None, False):
            try:
                after = ("ok", resolver._pick_stream(shape, low_first=low_first))
            except Exception as exc:
                after = ("raise", type(exc))
            assert after == before, shape


def test_switch_off_prints_nothing(capsys):
    resolver._pick_stream(_room(FULL_LADDER))
    assert capsys.readouterr().out == ""


def test_the_switch_ships_off():
    """计划 D17：开关默认关，实测结果给用户看、用户点头再改这里。改默认值的 PR 要连这条一起改。"""
    src = open(importlib.util.find_spec("app.resolver").origin, encoding="utf-8").read()
    consts = {t.id: n.value.value for n in ast.parse(src).body if isinstance(n, ast.Assign)
              for t in n.targets if isinstance(t, ast.Name) and isinstance(n.value, ast.Constant)}
    assert consts["LOW_BITRATE_PICK"] is False


# ---- 开着：有纯音频档仍取纯音频档，没有就从低档挑 ----

def test_switch_on_picks_the_lowest_tier_whatever_the_key_order(monkeypatch):
    _switch_on(monkeypatch)
    for order in itertools.permutations(FULL_LADDER):
        assert resolver._pick_stream(_room(order, FULL_PULL)) == _url("ld"), order
    for order in itertools.permutations(FULL_PULL + ("ORIGION",)):
        assert resolver._pick_stream(_room(pull=order)) == _pull("SD1"), order


def test_switch_on_still_takes_audio_only_first(monkeypatch):
    _switch_on(monkeypatch)
    for order in itertools.permutations(("ao", "origin", "hd", "ld")):
        assert resolver._pick_stream(_room(order)) == _url("ao"), order


def test_switch_on_goes_up_the_ladder_one_tier_at_a_time(monkeypatch):
    """低档没有 flv 时取上一档，直到原画；stream_data 一个都没有才退到 flv_pull_url。"""
    _switch_on(monkeypatch)
    ladder = ["ld", "sd", "hd", "uhd", "origin"]
    for i, expected in enumerate(ladder):
        missing = {t: _tier(t, flv=False) for t in ladder[:i]}
        room = _room([t for t in reversed(ladder) if t not in missing], ("FULL_HD1", "SD2"),
                     overrides=missing)
        assert resolver._pick_stream(room) == _url(expected)
    room = _room(pull=("FULL_HD1", "SD2"), overrides={t: _tier(t, flv=False) for t in ladder})
    assert resolver._pick_stream(room) == _pull("SD2")


def test_unknown_tiers_go_after_the_known_low_ones_and_before_origin(monkeypatch):
    """原画是源流，转出来的档不会比它高；不认得的档有多高不知道，不抢在认得的低档前面。"""
    _switch_on(monkeypatch)
    assert resolver._pick_stream(_room(("origin", "hd_60", "uhd"))) == _url("uhd")
    assert resolver._pick_stream(_room(("origin", "hd_60"))) == _url("hd_60")
    assert resolver._pick_stream(_room(("hd_60", "origin", "xq"))) == _url("hd_60")
    assert resolver._pick_stream(_room(("xq", "hd"))) == _url("hd")
    assert resolver._low_first(["ORIGION", "X1", "sd1", "HD1"], resolver.PULL_LOW_TO_HIGH) == [
        "sd1", "HD1", "X1", "ORIGION"]


def test_switch_on_survives_garbage(monkeypatch):
    _switch_on(monkeypatch)
    for bad in (None, {}, {"stream_url": None},
                {"stream_url": {"flv_pull_url": {"HD1": "", "SD1": None}}},
                {"stream_url": {"live_core_sdk_data": {"pull_data": {"stream_data": "не json"}}}}):
        assert resolver._pick_stream(bad) is None


# ---- 开关怎么读 ----

@pytest.mark.parametrize("value", ["1", "on", "TRUE", " yes "])
def test_env_turns_it_on(monkeypatch, value):
    monkeypatch.setenv(resolver.LOW_BITRATE_ENV, value)
    assert resolver.low_bitrate_pick() is True
    assert resolver._pick_stream(_room(FULL_LADDER)) == _url("ld")


@pytest.mark.parametrize("value", ["0", "off", "False", "no"])
def test_env_forces_it_off_even_when_the_default_is_on(monkeypatch, value):
    _switch_on(monkeypatch)
    monkeypatch.setenv(resolver.LOW_BITRATE_ENV, value)
    assert resolver.low_bitrate_pick() is False
    assert resolver._pick_stream(_room(FULL_LADDER)) == _url("origin")


@pytest.mark.parametrize("default", [False, True])
def test_unrecognised_env_values_follow_the_default(monkeypatch, default):
    monkeypatch.setattr(resolver, "LOW_BITRATE_PICK", default)
    for value in ("", "maybe", "2"):
        monkeypatch.setenv(resolver.LOW_BITRATE_ENV, value)
        assert resolver.low_bitrate_pick() is default


def test_env_is_read_on_every_pick(monkeypatch):
    room = _room(FULL_LADDER)
    assert resolver._pick_stream(room) == _url("origin")
    monkeypatch.setenv(resolver.LOW_BITRATE_ENV, "1")
    assert resolver._pick_stream(room) == _url("ld")
    monkeypatch.setenv(resolver.LOW_BITRATE_ENV, "0")
    assert resolver._pick_stream(room) == _url("origin")


# ---- 开着但拉流 ffmpeg 退回了进程内重连：照原来的挑法 ----

@pytest.mark.parametrize("env,value", [("TLT_FFMPEG_LEGACY", "1"), ("TLT_RECONNECT_V2", "0")])
def test_in_process_reconnect_keeps_the_old_pick(monkeypatch, capsys, env, value):
    """M1 之前 FLV 断开时 ffmpeg 自己重连、跳读那 5 MB，低码率下要更久，会撞上看门狗。退回进程内重连的两个
    应急开关任一个开着，低档开关就不生效；终端提示一次（中文），不重复刷屏。"""
    _switch_on(monkeypatch)
    monkeypatch.setenv(env, value)
    room = _room(FULL_LADDER, FULL_PULL)
    assert resolver.low_bitrate_pick() is False
    assert resolver._pick_stream(room) == _pick_stream_before_m5(room) == _url("origin")
    resolver._pick_stream(room)
    out = capsys.readouterr().out
    assert out.count("[信息]") == 1
    assert resolver.LOW_BITRATE_ENV in out and "进程内重连" in out


def test_in_process_reconnect_by_module_default_also_keeps_the_old_pick(monkeypatch):
    _switch_on(monkeypatch)
    monkeypatch.setattr(audio, "FFMPEG_LEGACY", True)
    assert resolver.low_bitrate_pick() is False
    monkeypatch.setattr(audio, "FFMPEG_LEGACY", False)
    monkeypatch.setattr(reconnect_policy, "RECONNECT_V2", False)
    assert resolver.low_bitrate_pick() is False
    monkeypatch.setattr(reconnect_policy, "RECONNECT_V2", True)
    assert resolver.low_bitrate_pick() is True


# ---- 经过真正的调用方：官方接口层、直播页（含登录直播页）的 SIGI_STATE ----

def _api_setup(monkeypatch, room):
    async def fake_status(_session, _user):
        return "7400000000000000001", 2

    async def fake_json(_session, _url, limit=None, headers=None):
        return {"status_code": 0, "data": room}

    monkeypatch.setattr(resolver, "_room_status", fake_status)
    monkeypatch.setattr(resolver, "_get_json", fake_json)


def test_api_layer_uses_the_switch(monkeypatch):
    _api_setup(monkeypatch, _room(FULL_LADDER, FULL_PULL))
    url, offline = run(resolver._resolve_via_api("https://www.tiktok.com/@demo_room_one/live"))
    assert (url, offline) == (_url("origin"), False)
    _switch_on(monkeypatch)
    url, offline = run(resolver._resolve_via_api("https://www.tiktok.com/@demo_room_one/live"))
    assert (url, offline) == (_url("ld"), False)


def _page(tiers):
    room = {"status": 2, "streamData": {"pull_data": {"stream_data": json.dumps(
        {"data": {t: _tier(t) for t in tiers}})}}}
    sigi = {"LiveRoom": {"liveRoomUserInfo": {"liveRoom": room}}}
    return '<script id="SIGI_STATE" type="application/json">{}</script>'.format(json.dumps(sigi))


def test_live_page_uses_the_switch(monkeypatch):
    html = _page(FULL_LADDER)
    assert resolver._parse_live_page(html) == (_url("origin"), False)
    _switch_on(monkeypatch)
    assert resolver._parse_live_page(html) == (_url("ld"), False)
    assert resolver._parse_live_page(_page(("origin", "ao", "ld"))) == (_url("ao"), False)
