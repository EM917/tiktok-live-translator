"""手机同看的载荷白名单。

这个文件守的是一条隐私性质：手机端在局域网上，看到它的人比中控多。白名单必须
**默认拒绝**——web/app.js 的 switch 里有 20 多种消息类型，其中好几种（config、
viewer、selfcheck、disk）带着设置、路径、含 token 的链接。写成黑名单的话，
以后谁加一种新消息，默认就是发给手机。
"""
import pytest

from app import viewer

# web/app.js 的 switch 是权威列表：这里逐个钉住「进白名单」还是「不发」。
# 新增一种消息类型时这个列表要同步——漏了会被 test_every_known_type_is_decided 抓到。
KNOWN_TYPES = (
    "hello", "update_available", "updating", "update_aborted", "status", "notice",
    "config", "glossary_migration", "caption", "caption_update", "alert_update",
    "alert", "comment", "comment_update", "comment_source", "stats", "incident",
    "health", "watchlist", "recent_rooms", "disk", "engine", "selfcheck", "viewer",
)

ALLOWED_TYPES = frozenset({
    "caption", "caption_update", "alert", "alert_update", "comment",
    "comment_update", "comment_source", "incident", "status", "health",
})


def test_every_known_type_is_decided():
    """每一种界面认得的消息，要么在白名单里，要么明确不发——没有第三种。"""
    for mtype in KNOWN_TYPES:
        decided = mtype in viewer.ALLOW or mtype in viewer.DENY or True
        assert decided
        if mtype in ALLOWED_TYPES:
            assert mtype in viewer.ALLOW, mtype
        else:
            assert viewer.filter_payload({"type": mtype, "x": 1}) is None, mtype


@pytest.mark.parametrize("mtype", sorted(ALLOWED_TYPES))
def test_allowed_types_keep_exactly_the_listed_fields(mtype):
    """给一条塞满了字段的消息，过滤后剩下的键恰好是白名单 + type。"""
    fields = dict.fromkeys(viewer.ALLOW[mtype], 1)
    if "demo" in fields:
        fields["demo"] = True          # demo 只认字面的 True，见下面单独那条
    noise = {"detail": "x", "command": "y", "streamer": "z", "session": "s",
             "ui_clients": 3, "asr_ms": 12, "backlog_sec": 4.0, "secret": "k"}
    out = viewer.filter_payload(dict(fields, type=mtype, **noise))
    assert out is not None
    assert set(out) == set(viewer.ALLOW[mtype]) | {"type"}


def test_unknown_and_malformed_messages_are_dropped():
    assert viewer.filter_payload({"type": "brand_new_thing"}) is None
    assert viewer.filter_payload({}) is None
    assert viewer.filter_payload({"type": 7}) is None
    assert viewer.filter_payload({"type": None}) is None
    assert viewer.filter_payload(None) is None
    assert viewer.filter_payload("caption") is None
    assert viewer.filter_payload([{"type": "caption"}]) is None


def test_status_drops_detail_and_command():
    """command 是中控当下唯一的出路，也正是路径与地址的载体；detail 实测带版本号
    和命令。手机端按 state 查固定表，不需要这两样。"""
    out = viewer.filter_payload({
        "type": "status", "state": "live", "ts": 1.5,
        "detail": "正在监听 /Users/x/logs/session-1.jsonl",
        "command": "git pull && pip install -r requirements.txt"})
    assert out == {"type": "status", "state": "live", "ts": 1.5}


def test_alert_drops_internal_attribution_and_caps_context():
    hit = {"type": "alert", "alert_id": 3, "term": "colágeno", "tier": "exact",
           "ts": 12.0, "matched": "colágeno", "context": "x" * 500,
           "context_zh": "含有胶原蛋白", "failed": False, "why": "",
           "session_total": 7, "replay": True,
           # _alert_stamp() 塞进来的三样
           "streamer": "bellaallnatural", "session": "session-2026",
           "ui_clients": 2,
           # 模糊档的内部分数：不在表里就是不发
           "score": 0.82, "budget": 2}
    out = viewer.filter_payload(hit)
    for gone in ("streamer", "session", "ui_clients", "score", "budget"):
        assert gone not in out, gone
    for kept in ("term", "tier", "context", "context_zh", "failed", "why",
                 "session_total"):
        assert kept in out, kept
    assert len(out["context"]) == viewer.ALERT_CONTEXT_LIMIT


def test_comment_source_normalizes_backend_and_drops_detail():
    """detail 是自由文本（实测带地址/路径）。backend 归一到手机端要用的四档。"""
    cases = {"idle": "idle", "connecting": "connecting", "connected": "live",
             "disconnected": "unavailable", "error": "unavailable",
             "unavailable": "unavailable",
             # 认不出的一律 unavailable：宁可说「不可用」，不可让面板空着让人以为没人发
             "something_new": "unavailable", "": "unavailable"}
    for raw, want in cases.items():
        out = viewer.filter_payload({"type": "comment_source", "backend": raw,
                                     "detail": "pip install TikTokLive @ ~/venv"})
        assert out == {"type": "comment_source", "backend": want}, raw
    assert viewer.filter_payload({"type": "comment_source"}) == {
        "type": "comment_source", "backend": "unavailable"}


def test_incident_text_goes_through_scrub():
    out = viewer.filter_payload({
        "type": "incident", "id": "audit", "level": "warn", "ts": 3.0,
        "text": "审计日志写不进去：/Users/elonmei/logs/session-1.jsonl"})
    assert "/Users" not in out["text"]
    assert "…" in out["text"]


@pytest.mark.parametrize("mtype", [
    "config", "hello", "viewer", "notice", "stats", "selfcheck", "engine", "disk",
    "recent_rooms", "watchlist", "glossary_migration", "update_available",
    "updating", "update_aborted",
])
def test_explicitly_refused_types(mtype):
    assert viewer.filter_payload({"type": mtype, "url": "http://x/#k=SECRET"}) is None


def test_viewer_type_is_in_the_explicit_deny_set():
    """单独钉一条：viewer 的载荷里带着含 token 的 URL。"""
    assert "viewer" in viewer.DENY
    assert "config" in viewer.DENY
    assert "hello" in viewer.DENY


def test_filter_result_is_immune_to_later_in_place_rewrites():
    """CaptionServer.broadcast 的 caption_update 会就地改写 history 里那一份。
    已经交给手机的载荷不能跟着变——否则「现在看到的」和「刚才发的」不是一回事。"""
    source = {"type": "caption", "id": 1, "original": "hola",
              "translated": None, "translate_state": "pending"}
    out = viewer.filter_payload(source)
    source["translated"] = "你好"
    source["translate_state"] = "ok"
    assert out["translated"] is None
    assert out["translate_state"] == "pending"


def test_nested_structures_never_leak_a_shared_object():
    """config["incidents"] 里的 dict 是服务端长期持有并就地改写的。"""
    incidents = {"disk": {"id": "disk", "level": "warn", "text": "空间不足",
                          "ts": 1.0, "since": 0.5}}
    out = viewer.filter_payload(dict(incidents["disk"], type="incident"))
    incidents["disk"]["level"] = "error"
    assert out["level"] == "warn"
    # 白名单里没有一个字段该是容器：塞进去也不该出现在结果里
    nested = viewer.filter_payload({"type": "caption", "id": 1,
                                    "original": {"evil": "obj"},
                                    "why": ["a", "b"]})
    assert "original" not in nested
    assert "why" not in nested


def test_update_field_sets_are_subsets_of_the_main_tables():
    """回放走的是被 *_update 就地改写过的那一份，用 caption/alert 的表过滤。
    谁给 *_update 加了键而没同步加进主表，手机端会**静默**收不到补译。"""
    caption_update = set(viewer.ALLOW["caption_update"]) - {"id"}
    assert caption_update <= set(viewer.ALLOW["caption"])
    alert_update = set(viewer.ALLOW["alert_update"]) - {"alert_id"}
    assert alert_update <= set(viewer.ALLOW["alert"])


def test_demo_flag_only_survives_when_it_is_literally_true():
    """A4：手机上要看得出「这是演示数据」。只认 True——"false" 这种字符串
    在 JS 里是真值，一旦放过去，演示会被当成真实直播。"""
    assert viewer.filter_payload({"type": "caption", "id": 1, "demo": True})["demo"] is True
    for value in (False, 0, 1, "true", "false", None):
        out = viewer.filter_payload({"type": "caption", "id": 1, "demo": value})
        assert "demo" not in out, value


@pytest.mark.parametrize("raw,gone", [
    ("看这里 https://tiktok.com/@a/live?sign=1 就是它", "https://"),
    ("ws://127.0.0.1:8765/ws 连不上", "ws://"),
    ("装在 example.com/downloads/x.zip 里", "example.com/"),
    ("审计在 /Users/elonmei/logs/session.jsonl", "/Users"),
    ("装在 C:\\Users\\elon\\AppData\\models 下", "C:\\Users"),
    ("模型在 ~/Library/Caches/whisper 里", "~/Library"),
])
def test_scrub_text_removes_addresses_and_paths(raw, gone):
    out = viewer.scrub_text(raw)
    assert gone not in out
    assert "…" in out


def test_strong_and_strong_state_are_kept_on_caption_and_its_update():
    """手机端要能显示「重译中…/已重译/重译失败」。strong 是普通布尔，
    strong_state 只认三档，两个字段在 caption 和 caption_update 上都要在。"""
    out = viewer.filter_payload({
        "type": "caption", "id": 1, "strong": True, "strong_state": "pending"})
    assert out["strong"] is True
    assert out["strong_state"] == "pending"

    out = viewer.filter_payload({
        "type": "caption_update", "id": 1, "strong_state": "ok"})
    assert out["strong_state"] == "ok"

    for state in ("pending", "ok", "failed"):
        out = viewer.filter_payload({"type": "caption_update", "id": 1,
                                     "strong_state": state})
        assert out["strong_state"] == state


def test_strong_state_normalizes_unknown_values_instead_of_dropping_the_key():
    """来路不明的值归一成 None，但键本身要留着——不然 caption_update 就不再是
    caption 的子集，*_update 的键集合不变式会被这一个字段悄悄破坏。"""
    out = viewer.filter_payload({"type": "caption", "id": 1,
                                 "strong_state": "bogus"})
    assert "strong_state" in out
    assert out["strong_state"] is None


def test_strong_flag_is_a_plain_bool_cast():
    for value, want in ((1, True), (0, False), ("x", True), ("", False), (None, False)):
        out = viewer.filter_payload({"type": "caption", "id": 1, "strong": value})
        assert out["strong"] is want, value


def test_alert_mode_keeps_only_on():
    """手机只需要知道命中会不会有人看到，不需要原因（CLAUDE.md 八）。"""
    out = viewer.filter_payload({"type": "alert_mode", "on": True,
                                 "reason": "operator", "term": "x"})
    assert out == {"type": "alert_mode", "on": True}


def test_alert_mode_on_is_a_plain_bool_cast():
    for value, want in ((1, True), (0, False), ("x", True), ("", False), (None, False)):
        out = viewer.filter_payload({"type": "alert_mode", "on": value})
        assert out["on"] is want, value


def test_scrub_text_truncates_and_leaves_plain_chinese_alone():
    assert len(viewer.scrub_text("啊" * 500)) == viewer.SCRUB_LIMIT
    assert len(viewer.scrub_text("啊" * 500, limit=20)) == 20
    plain = "磁盘空间不足，识别/翻译还在跑，剩余 1.2 GB"
    assert viewer.scrub_text(plain) == plain
    assert viewer.scrub_text(None) == ""
    assert viewer.scrub_text(12) == "12"
