"""闪购助手的只读状态（app/flash_sale.py）：纯函数和计时器。不开网络、不碰真的 /Applications 和 9334。

夹具是闪购助手那边给的三份契约样例（tests/fixtures/flash_sale/，标题都是虚构的「(Fictional)」，
店铺是 example_shop），原样拷进来。
"""
import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import flash_sale as fs

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "flash_sale"
# tests/conftest.py 的 autouse 夹具把 app_candidates 换成了不存在的路径；收集时这里拿到的是真的那个
REAL_APP_CANDIDATES = fs.app_candidates


def sample(name):
    return json.loads((FIXTURES / (name + ".json")).read_text(encoding="utf-8"))


def iso_s(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%f%z").timestamp()


UPDATED_S = iso_s("2026-10-27T21:36:07.165+0000")


# ---- 找程序、打开程序 --------------------------------------------------------------------

@pytest.mark.parametrize("platform", ["linux", "win32", "cygwin"])
def test_not_macos_means_not_installed_even_if_the_folder_exists(platform, tmp_path):
    app = tmp_path / fs.APP_NAME
    app.mkdir()
    assert fs.find_app(platform=platform, candidates=[app]) is None
    assert fs.installed(platform=platform, candidates=[app]) is False


def test_macos_with_no_app_at_the_known_places_is_not_installed(tmp_path):
    assert fs.find_app(platform="darwin", candidates=[tmp_path / "a" / fs.APP_NAME,
                                                      tmp_path / "b" / fs.APP_NAME]) is None


def test_macos_finds_the_first_existing_app_folder(tmp_path):
    first, second = tmp_path / "a" / fs.APP_NAME, tmp_path / "b" / fs.APP_NAME
    second.mkdir(parents=True)
    assert fs.find_app(platform="darwin", candidates=[first, second]) == second
    first.mkdir(parents=True)
    assert fs.find_app(platform="darwin", candidates=[first, second]) == first
    assert fs.installed(platform="darwin", candidates=[first]) is True


def test_a_plain_file_with_the_app_name_is_not_an_app(tmp_path):
    fake = tmp_path / fs.APP_NAME
    fake.write_text("x", encoding="utf-8")
    assert fs.find_app(platform="darwin", candidates=[fake]) is None


def test_the_known_places_are_applications_and_the_home_applications(tmp_path):
    got = REAL_APP_CANDIDATES(home=tmp_path)
    assert got == (Path("/Applications") / fs.APP_NAME, tmp_path / "Applications" / fs.APP_NAME)
    assert fs.APP_PATH == got[0]


def test_tests_never_see_the_real_install():
    """tests/conftest.py 的网：默认位置是不存在的路径，默认地址不是 9334。"""
    assert fs.find_app(platform="darwin") is None
    assert ":9334" not in fs.STATUS_URL


def test_open_argv_opens_by_full_path():
    assert fs.open_argv(Path("/Applications") / fs.APP_NAME) == [
        "open", "-a", str(Path("/Applications") / fs.APP_NAME)]
    assert fs.open_argv() == ["open", "-a", str(fs.APP_PATH)]


# ---- parse_iso -----------------------------------------------------------------------------

BASE = datetime(2026, 10, 27, 21, 36, 7, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize("text, expected", [
    ("2026-10-27T21:36:07Z", BASE),
    ("2026-10-27T21:36:07z", BASE),
    ("2026-10-27T21:36:07", BASE),                       # 不带时区按 UTC
    ("2026-10-27 21:36:07Z", BASE),
    ("2026-10-27T21:36:07.1Z", BASE + 0.1),
    ("2026-10-27T21:36:07.165Z", BASE + 0.165),
    ("2026-10-27T21:36:07.165123Z", BASE + 0.165123),
    ("2026-10-27T21:36:07.165123999Z", BASE + 0.165123),  # 9 位小数截到微秒
    ("2026-10-28T05:36:07+08:00", BASE),
    ("2026-10-28T05:36:07+0800", BASE),
    ("2026-10-28T05:36:07+08", BASE),
    ("2026-10-27T16:36:07-05:00", BASE),
    ("  2026-10-27T21:36:07Z  ", BASE),
])
def test_parse_iso_reads_the_usual_spellings(text, expected):
    assert fs.parse_iso(text) == pytest.approx(expected, abs=1e-6)


@pytest.mark.parametrize("value", [
    None, "", "yesterday", "2026-10-27", "2026-13-01T00:00:00Z", "2026-02-30T00:00:00Z",
    "2026-10-27T25:00:00Z", "2026-10-27T21:36:60Z", "2026-10-27T21:36:07+25:00",
    "2026-10-27T21:36:07.Z", "2026-10-27T21:36:07.1234567890Z", "x2026-10-27T21:36:07Z",
    "2026-10-27T21:36:07Zjunk", "1999-01-01T00:00:00Z", "9999-01-01T00:00:00Z",
    "2026-10-27T21:36:07" + "0" * 80, True, False, [], {}, float("nan"), float("inf"), -5, 0, 12345,
])
def test_parse_iso_returns_none_for_anything_else(value):
    assert fs.parse_iso(value) is None


def test_parse_iso_takes_epoch_seconds_and_milliseconds():
    assert fs.parse_iso(BASE) == BASE
    assert fs.parse_iso(int(BASE)) == int(BASE)
    assert fs.parse_iso(BASE * 1000) == pytest.approx(BASE)
    assert fs.parse_iso(1e17) is None           # 毫秒也太大：超出合理范围


# ---- normalize ------------------------------------------------------------------------------

def test_the_running_sample_normalizes_to_the_documented_shape():
    out = fs.normalize(sample("running"))
    assert out["_schema"] == 1
    assert out["_updated_s"] == pytest.approx(UPDATED_S)
    assert out["app_version"] == "0.7.2" and out["page"] == "ok"
    assert out["running"] is True and out["paused_reason"] is None
    assert out["products_total"] == 9 and len(out["products"]) == 9
    assert set(out) == {"_schema", "_updated_s", "app_version", "page", "running", "paused_reason",
                        "products", "products_total", "alerts"}
    first = out["products"][0]
    assert first == {"id": "1000000000000000001", "title": "Glow Serum Trio (Fictional)", "state": "live",
                     "round": 2, "ends_ms": int(round(iso_s("2026-10-27T21:51:01.000+0000") * 1000)),
                     "next_ms": None, "stock_left": 30, "stock_total": 30, "renewals": 0, "enabled": True}
    between = out["products"][1]
    assert between["state"] == "between_rounds" and between["ends_ms"] is None
    assert between["next_ms"] == int(round(iso_s("2026-10-27T21:37:10.000+0000") * 1000))
    # 保持闪购助手给的顺序（排序在 StatusTracker 里做）
    assert [p["state"] for p in out["products"]] == [
        "live", "between_rounds", "stopped", "pending", "ended_by_user", "no_sale", "unknown", "live", "live"]
    assert [a["level"] for a in out["alerts"]] == ["warn", "warn", "info", "info", "info", "info"]
    assert out["alerts"][0]["text"].startswith("❌ Toner Mist 250ml (Fictional)")
    assert out["alerts"][0]["at_ms"] == int(round(iso_s("2026-10-27T21:31:02.000+0000") * 1000))
    blob = json.dumps(out, ensure_ascii=False)
    for gone in ("example_shop", "shop", "page_version", "product_id", "round_ends_at", "updated_at"):
        assert gone not in blob, gone


def test_the_idle_and_awaiting_samples_normalize():
    idle = fs.normalize(sample("idle"))
    assert idle["running"] is False and idle["paused_reason"] == "not_started" and idle["alerts"] == []
    assert {p["state"] for p in idle["products"]} == {"live"}
    awaiting = fs.normalize(sample("awaiting"))
    assert awaiting["paused_reason"] == "awaiting_approval" and len(awaiting["alerts"]) == 4


@pytest.mark.parametrize("raw", [None, [], [1, 2], "ok", 3, True])
def test_a_non_object_is_not_normalized(raw):
    assert fs.normalize(raw) is None


def test_an_empty_object_gives_all_unknowns():
    assert fs.normalize({}) == {
        "_schema": None, "_updated_s": None, "app_version": None, "page": None, "running": None,
        "paused_reason": None, "products": [], "products_total": 0, "alerts": []}


def test_unknown_keys_are_dropped():
    raw = dict(sample("running"), extra={"a": 1}, token="secret")
    raw["products"][0]["secret"] = "x"
    raw["alerts"][0]["who"] = "x"
    out = fs.normalize(raw)
    blob = json.dumps(out)
    assert "secret" not in blob and "token" not in blob and "who" not in blob and "extra" not in blob


def test_wrong_types_become_none():
    raw = {"schema": "1", "app_version": 7, "updated_at": 5, "page": 3, "running": "true",
           "paused_reason": ["x"], "products": {"0": {}}, "alerts": "none"}
    out = fs.normalize(raw)
    assert out == {"_schema": "1", "_updated_s": None, "app_version": None, "page": None, "running": None,
                   "paused_reason": None, "products": [], "products_total": 0, "alerts": []}


@pytest.mark.parametrize("value, expected", [
    (3, 3), (3.0, 3), (-2, -2), (True, None), (False, None), (3.5, None), ("3", None), (None, None),
    (float("nan"), None), (float("inf"), None), (10**9, 10**9), (10**9 + 1, None), (-(10**10), None),
])
def test_integer_fields_refuse_bool_fractions_and_huge_values(value, expected):
    raw = {"products": [{"product_id": "1", "round": value, "stock_left": value, "stock_total": value,
                         "renewals": value}]}
    got = fs.normalize(raw)["products"][0]
    assert got["round"] == got["stock_left"] == got["stock_total"] == got["renewals"] == expected
    assert type(got["round"]) is type(expected)


@pytest.mark.parametrize("value, expected", [(True, True), (False, False), (1, None), ("true", None),
                                             (None, None)])
def test_boolean_fields_take_only_real_booleans(value, expected):
    out = fs.normalize({"running": value, "products": [{"enabled": value}]})
    assert out["running"] is expected and out["products"][0]["enabled"] is expected


def test_product_ids_are_strings():
    out = fs.normalize({"products": [{"product_id": 1000000000000000001}, {"product_id": True},
                                     {"product_id": 1.5}, {"product_id": ""}, {"product_id": "a\x00b"},
                                     {"product_id": "9" * 100}, {}]})
    assert [p["id"] for p in out["products"]] == [
        "1000000000000000001", None, None, None, "ab", "9" * fs.MAX_CODE, None]


def test_codes_are_cleaned_but_unknown_codes_are_kept():
    out = fs.normalize({"page": "weird<script>alert(1)</script>", "paused_reason": "new_reason.v2",
                        "products": [{"state": "  "}, {"state": "brand-new"}, {"state": "x" * 100}],
                        "alerts": [{"level": "critical!"}]})
    assert out["page"] == "weirdscriptalert1script"
    assert out["paused_reason"] == "new_reason.v2"
    assert [p["state"] for p in out["products"]] == [None, "brand-new", "x" * fs.MAX_CODE]
    assert out["alerts"][0]["level"] == "critical"


def test_texts_lose_control_characters_and_lone_surrogates_and_are_capped():
    title = "Serum\x00\x07\n (Fictional) \x9f" + "\ud800"
    out = fs.normalize({"app_version": "0.7.2\n; rm -rf ~" + "9" * 50,
                        "products": [{"title": title}, {"title": "长" * 1000}],
                        "alerts": [{"text": "❌ a\r\nb" + "x" * 2000}]})
    assert out["products"][0]["title"] == "Serum (Fictional)"
    assert out["products"][1]["title"] == "长" * fs.MAX_TITLE
    text = out["alerts"][0]["text"]
    assert text.startswith("❌ ab") and len(text) == fs.MAX_TEXT
    assert out["app_version"] == ("0.7.2rm-rf" + "9" * 50)[:fs.MAX_VERSION]
    json.dumps(out).encode("utf-8")            # 编得出来：没有孤立代理


def test_schema_two_is_still_read():
    raw = dict(sample("running"), schema=2)
    out = fs.normalize(raw)
    assert out["_schema"] == 2 and len(out["products"]) == 9


def test_products_are_capped_at_300_in_the_helpers_order_and_the_total_is_kept():
    raw = {"products": [{"product_id": str(i)} for i in range(350)] + ["junk", 3, None]}
    out = fs.normalize(raw)
    assert len(out["products"]) == fs.MAX_PRODUCTS == 300
    assert out["products"][0]["id"] == "0" and out["products"][-1]["id"] == "299"
    assert out["products_total"] == 350


def test_non_object_products_and_alerts_are_skipped():
    out = fs.normalize({"products": [1, "x", None, {"product_id": "7"}, []],
                        "alerts": [None, {"text": "hola"}, "x"]})
    assert [p["id"] for p in out["products"]] == ["7"]
    assert [a["text"] for a in out["alerts"]] == ["hola"]


def test_only_the_last_twenty_alerts_are_kept_oldest_first():
    alerts = [{"at": "2026-10-27T21:%02d:00.000Z" % i, "level": "info", "text": "n%d" % i} for i in range(30)]
    out = fs.normalize({"alerts": alerts})
    assert [a["text"] for a in out["alerts"]] == ["n%d" % i for i in range(10, 30)]


# ---- attention 真值表（闪购助手会话 10-27 定稿） ---------------------------------------------------

def ok_state(**over):
    state = {"conn": "ok", "stale": False, "page": "ok", "running": True, "paused_reason": None,
             "products": []}
    state.update(over)
    return state


@pytest.mark.parametrize("over, expected", [
    ({}, []),
    ({"stale": True}, ["stale"]),
    ({"page": "login_required", "running": False}, ["page"]),
    ({"page": "captcha"}, ["page"]),
    ({"page": "error", "running": False}, ["page"]),
    ({"page": "no_list", "running": True}, ["page"]),
    ({"page": "no_list", "running": False, "paused_reason": "not_started"}, []),
    ({"page": "no_page", "running": False}, []),
    ({"page": "loading", "running": False}, []),
    ({"page": "brand_new_code"}, []),
    ({"paused_reason": "awaiting_approval"}, ["paused"]),
    ({"paused_reason": "user_interaction"}, ["paused"]),
    ({"paused_reason": "captcha"}, ["paused"]),
    ({"paused_reason": "error"}, ["paused"]),
    ({"paused_reason": "brand_new_reason"}, ["paused"]),
    ({"paused_reason": "not_started", "running": False}, []),
    ({"products": [{"state": "stopped", "stuck": True}]}, ["stuck"]),
    ({"products": [{"state": "stopped", "stuck": False}]}, []),
    ({"products": [{"state": s, "stuck": False} for s in
                   ("ended_by_user", "pending", "no_sale", "unknown", "live", "between_rounds")]}, []),
    # 顺序就是主句的优先级
    ({"stale": True, "page": "error", "running": False}, ["stale", "page"]),
    ({"page": "captcha", "paused_reason": "captcha", "products": [{"stuck": True}]},
     ["page", "paused", "stuck"]),
])
def test_attention_truth_table(over, expected):
    assert fs.attention(ok_state(**over)) == expected


@pytest.mark.parametrize("conn, expected", [("checking", []), ("unreachable", []),
                                            ("bad_response", ["bad_response"])])
def test_attention_when_not_connected(conn, expected):
    assert fs.attention({"conn": conn, "stale": True, "page": "error", "paused_reason": "error"}) == expected


def test_empty_state_has_the_message_shape_and_no_data():
    for conn in ("checking", "unreachable", "bad_response"):
        state = fs.empty_state(conn)
        assert state == {"conn": conn, "stale": False, "updated_ms": None, "app_version": None,
                         "page": None, "running": None, "paused_reason": None, "products": [],
                         "products_total": 0, "alerts": [], "stuck_count": 0,
                         "attention": ["bad_response"] if conn == "bad_response" else []}
    assert fs.empty_state("checking") is not fs.empty_state("checking")


# ---- StatusTracker：陈旧、卡住、排序 -------------------------------------------------------------

def track(tracker, raw, wall=UPDATED_S + 1, mono=1000.0):
    return tracker.update("ok", copy.deepcopy(raw), wall, mono)


def test_the_published_state_has_exactly_the_message_keys():
    state = track(fs.StatusTracker(), sample("running"))
    assert set(state) == {"conn", "stale", "updated_ms", "app_version", "page", "running", "paused_reason",
                          "products", "products_total", "alerts", "attention", "stuck_count"}
    assert set(state["products"][0]) == {"id", "title", "state", "round", "ends_ms", "next_ms", "stock_left",
                                         "stock_total", "renewals", "enabled", "stuck"}
    assert state["conn"] == "ok" and state["stale"] is False and state["updated_ms"] is None
    assert set(state) == set(fs.empty_state("ok"))


@pytest.mark.parametrize("age, stale", [(14, False), (15, False), (15.5, True), (16, True), (-30, False)])
def test_stale_is_wall_clock_minus_updated_at(age, stale):
    state = track(fs.StatusTracker(), sample("running"), wall=UPDATED_S + age)
    assert state["stale"] is stale
    assert ("stale" in state["attention"]) is stale
    assert state["updated_ms"] == (int(round(UPDATED_S * 1000)) if stale else None)


def test_a_null_updated_at_turns_stale_after_fifteen_seconds_of_the_monotonic_clock():
    tracker = fs.StatusTracker()
    raw = dict(sample("running"), updated_at=None)
    assert track(tracker, raw, mono=100.0)["stale"] is False
    assert track(tracker, raw, mono=115.0)["stale"] is False
    state = track(tracker, raw, mono=115.1)
    assert state["stale"] is True and state["updated_ms"] is None and state["attention"][0] == "stale"
    # 拿到一次能认的 updated_at 就清零
    assert track(tracker, sample("running"), mono=116.0)["stale"] is False
    assert track(tracker, raw, mono=117.0)["stale"] is False
    assert track(tracker, raw, mono=131.0)["stale"] is False
    assert track(tracker, raw, mono=132.5)["stale"] is True


def test_losing_the_connection_resets_the_null_clock():
    tracker = fs.StatusTracker()
    raw = dict(sample("running"), updated_at="garbage")
    track(tracker, raw, mono=100.0)
    tracker.update("unreachable", None, UPDATED_S, 110.0)
    assert track(tracker, raw, mono=120.0)["stale"] is False
    assert track(tracker, raw, mono=136.0)["stale"] is True


def stopped_doc(**over):
    doc = {"schema": 1, "updated_at": "2026-10-27T21:36:07.165Z", "page": "ok", "running": True,
           "paused_reason": None,
           "products": [{"product_id": "1000000000000000003", "title": "Calm Gel Mask x4 (Fictional)",
                         "state": "stopped", "enabled": True},
                        {"product_id": "1000000000000000001", "title": "Glow Serum Trio (Fictional)",
                         "state": "live", "enabled": True}],
           "alerts": []}
    doc.update(over)
    return doc


def stuck_of(state):
    return {p["id"][-1]: p["stuck"] for p in state["products"]}


def test_a_stopped_product_is_stuck_only_after_sixty_seconds():
    tracker = fs.StatusTracker()
    assert stuck_of(track(tracker, stopped_doc(), mono=0.0)) == {"3": False, "1": False}
    assert track(tracker, stopped_doc(), mono=59.9)["attention"] == []
    state = track(tracker, stopped_doc(), mono=60.0)
    assert stuck_of(state) == {"3": True, "1": False}
    assert state["attention"] == ["stuck"] and state["stuck_count"] == 1


@pytest.mark.parametrize("breaker", [
    {"running": False},
    {"paused_reason": "awaiting_approval"},
    {"products": [{"product_id": "1000000000000000003", "state": "stopped", "enabled": False}]},
    {"products": [{"product_id": "1000000000000000003", "state": "pending", "enabled": True}]},
    {"products": []},
])
def test_any_broken_condition_resets_the_stuck_clock(breaker):
    tracker = fs.StatusTracker()
    track(tracker, stopped_doc(), mono=0.0)
    track(tracker, stopped_doc(), mono=50.0)
    broken = track(tracker, stopped_doc(**breaker), mono=55.0)
    assert not any(p["stuck"] for p in broken["products"])
    assert stuck_of(track(tracker, stopped_doc(), mono=70.0))["3"] is False      # 从 70 秒重新计
    assert stuck_of(track(tracker, stopped_doc(), mono=129.0))["3"] is False
    assert stuck_of(track(tracker, stopped_doc(), mono=130.0))["3"] is True


@pytest.mark.parametrize("kind, value", [("unreachable", None), ("bad_response", "json"), ("ok", [1])])
def test_a_failed_poll_resets_the_stuck_clock(kind, value):
    tracker = fs.StatusTracker()
    track(tracker, stopped_doc(), mono=0.0)
    tracker.update(kind, value, UPDATED_S, 30.0)
    assert stuck_of(track(tracker, stopped_doc(), mono=61.0))["3"] is False


def test_a_product_without_an_id_is_never_stuck():
    tracker = fs.StatusTracker()
    doc = stopped_doc(products=[{"state": "stopped", "enabled": True}])
    track(tracker, doc, mono=0.0)
    state = track(tracker, doc, mono=500.0)
    assert state["products"][0]["stuck"] is False and state["attention"] == []


def test_products_are_sorted_by_what_needs_a_person_first():
    tracker = fs.StatusTracker()
    doc = {"updated_at": "2026-10-27T21:36:07.165Z", "running": True, "paused_reason": None, "page": "ok",
           "products": [{"product_id": str(i), "state": s, "enabled": True} for i, s in enumerate(
               ["ended_by_user", "live", "brand_new", "no_sale", "stopped", "between_rounds", "unknown",
                "pending", "live", None, "stopped"])]}
    doc["products"][10]["enabled"] = False        # 不自动续开的 stopped 永远不算卡住
    track(tracker, doc, mono=0.0)
    state = track(tracker, doc, mono=61.0)
    order = [(p["state"], p["stuck"]) for p in state["products"]]
    assert order == [("stopped", True), ("pending", False), ("unknown", False), ("live", False),
                     ("live", False), ("between_rounds", False), ("stopped", False), ("no_sale", False),
                     ("ended_by_user", False), ("brand_new", False), (None, False)]
    # 同一档保持闪购助手给的顺序
    assert [p["id"] for p in state["products"] if p["state"] == "live"] == ["1", "8"]


def test_the_running_sample_puts_the_stopped_product_first_once_it_is_stuck():
    tracker = fs.StatusTracker()
    first = track(tracker, sample("running"), mono=0.0)
    assert [p["state"] for p in first["products"]][:3] == ["pending", "unknown", "live"]
    later = track(tracker, sample("running"), mono=60.0)
    assert later["products"][0]["id"] == "1000000000000000003" and later["products"][0]["stuck"] is True
    assert later["attention"] == ["stuck"]


def test_the_samples_raise_the_documented_attention():
    assert track(fs.StatusTracker(), sample("idle"))["attention"] == []
    assert track(fs.StatusTracker(), sample("running"))["attention"] == []
    assert track(fs.StatusTracker(), sample("awaiting"))["attention"] == ["paused"]


def test_a_non_object_body_becomes_bad_response():
    tracker = fs.StatusTracker()
    state = tracker.update("ok", [1, 2], UPDATED_S, 0.0)
    assert state == fs.empty_state("bad_response") and tracker.why == "not_object"


def test_failures_publish_empty_states_and_remember_the_reason():
    tracker = fs.StatusTracker()
    assert tracker.update("unreachable", None, UPDATED_S, 0.0) == fs.empty_state("unreachable")
    assert tracker.why is None
    assert tracker.update("bad_response", "http_404", UPDATED_S, 0.0) == fs.empty_state("bad_response")
    assert tracker.why == "http_404"
    track(tracker, sample("running"))
    assert tracker.why is None and tracker.schema == 1


def test_the_same_status_gives_an_equal_state_even_as_updated_at_ticks():
    """去重靠这个：updated_at 每 2 秒变一次，但不下发它，只要内容没变两次的结果就相等。"""
    tracker = fs.StatusTracker()
    a = track(tracker, sample("running"), wall=UPDATED_S + 1, mono=0.0)
    later = dict(sample("running"), updated_at="2026-10-27T21:36:09.201Z")
    b = track(tracker, later, wall=UPDATED_S + 3, mono=2.0)
    assert a == b
    changed = copy.deepcopy(later)
    changed["products"][0]["stock_left"] = 29
    assert track(tracker, changed, wall=UPDATED_S + 3, mono=2.0) != b
