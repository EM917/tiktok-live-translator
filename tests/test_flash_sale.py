"""闪购助手的只读状态（app/flash_sale.py）：纯函数和计时器。不开网络、不碰真的 /Applications 和 9334。

夹具是闪购助手那边给的三份契约样例（tests/fixtures/flash_sale/，标题都是虚构的「(Fictional)」，
店铺是 example_shop），原样拷进来。
"""
import copy
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import flash_sale as fs

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "flash_sale"
# tests/conftest.py 的 autouse 夹具把 app_candidates 换成了不存在的路径；收集时这里拿到的是真的那个
REAL_APP_CANDIDATES = fs.app_candidates


def sample(name):
    return json.loads((FIXTURES / (name + ".json")).read_text(encoding="utf-8"))


# 后端和前端各有一份、必须一字不差的规则：同一张表，这里测后端，node 测试测 web/flash.js
SHARED = sample("shared-rules")


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
                     "next_ms": None, "stock_left": 30, "stock_total": 30, "renewals": 0, "enabled": True,
                     "position": None, "skip": None}      # 样例是 0.7.2 的：没有这两个键，当 null
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


@pytest.mark.parametrize("case", SHARED["attention"], ids=lambda c: c["why"])
def test_the_start_quiet_window_matches_the_shared_table(case):
    """开始、继续被接受后等生效的那 30 秒：页面加载中、出错、不在商品列表、超过 15 秒没更新都不算要人处理
    （用户 10-27 定：显示「正在开始…」，不变橙、不出红字）。后端 starting / attention 和前端 flashStarting、
    浮层主句的变色吃同一张表。"""
    assert fs.starting(case["state"]) is case["starting"]
    assert fs.attention(case["state"]) == case["attention"]


@pytest.mark.parametrize("control", [None, "waiting", [], {"action": "start"}, {"phase": "waiting"},
                                     {"action": "START", "phase": "waiting"}, {"action": "start", "phase": "done"}])
def test_starting_needs_exactly_an_accepted_start_or_resume(control):
    assert fs.starting({"control": control}) is False
    assert fs.starting(None) is False and fs.starting([]) is False
    assert fs.starting({"control": {"action": "start", "phase": "waiting"}}) is True


@pytest.mark.parametrize("conn, expected", [("checking", []), ("unreachable", []),
                                            ("bad_response", ["bad_response"])])
def test_attention_when_not_connected(conn, expected):
    assert fs.attention({"conn": conn, "stale": True, "page": "error", "paused_reason": "error"}) == expected


def test_empty_state_has_the_message_shape_and_no_data():
    for conn in ("checking", "unreachable", "bad_response"):
        state = fs.empty_state(conn)
        assert state == {"conn": conn, "why": None, "stale": False, "updated_ms": None, "app_version": None,
                         "page": None, "running": None, "paused_reason": None, "products": [],
                         "products_total": 0, "alerts": [], "stuck_count": 0, "control": None,
                         "attention": ["bad_response"] if conn == "bad_response" else []}
    assert fs.empty_state("checking") is not fs.empty_state("checking")
    # why 只在 bad_response 时下发（页面按它挑一句话）
    assert fs.empty_state("bad_response", "no_answer")["why"] == "no_answer"
    assert fs.empty_state("unreachable", "no_answer")["why"] is None


# ---- StatusTracker：陈旧、卡住、排序 -------------------------------------------------------------

def stamped(raw, wall):
    """同一份 /status，updated_at 换成 wall 这个时刻。"""
    doc = copy.deepcopy(raw)
    doc["updated_at"] = datetime.fromtimestamp(wall, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return doc


def track(tracker, raw, mono=1000.0, fresh=True):
    """喂一次 /status。fresh（缺省）：updated_at 盖成随单调时钟往前走的时刻——闪购助手每 2 秒刷新一次的样子，
    隔几十秒喂同一份样例才不会被当成陈旧（陈旧看的是同一个值停了多久）。测陈旧时传 fresh=False，原样喂。
    updated_at 为 null、认不出的不动。"""
    doc = copy.deepcopy(raw)
    if fresh and fs.parse_iso(doc.get("updated_at")) is not None:
        doc = stamped(doc, UPDATED_S + mono)
    return tracker.update("ok", doc, mono)


def test_the_published_state_has_exactly_the_message_keys():
    state = track(fs.StatusTracker(), sample("running"))
    assert set(state) == {"conn", "why", "stale", "updated_ms", "app_version", "page", "running", "paused_reason",
                          "products", "products_total", "alerts", "attention", "stuck_count", "control"}
    assert set(state["products"][0]) == {"id", "title", "state", "round", "ends_ms", "next_ms", "stock_left",
                                         "stock_total", "renewals", "enabled", "position", "skip", "stuck"}
    assert state["conn"] == "ok" and state["why"] is None and state["stale"] is False and state["updated_ms"] is None
    assert state["control"] is None           # 控制指令的进度由 app/pipeline_flash.py 发布前叠上
    assert set(state) == set(fs.empty_state("ok"))


@pytest.mark.parametrize("age, stale", [(14, False), (15, False), (15.5, True), (16, True), (600, True)])
def test_stale_is_the_same_updated_at_for_over_fifteen_seconds_of_the_monotonic_clock(age, stale):
    tracker = fs.StatusTracker()
    assert track(tracker, sample("running"), mono=100.0, fresh=False)["stale"] is False
    state = track(tracker, sample("running"), mono=100.0 + age, fresh=False)
    assert state["stale"] is stale
    assert ("stale" in state["attention"]) is stale
    assert state["updated_ms"] == (int(round(UPDATED_S * 1000)) if stale else None)   # 页面写「上次更新」用的还是它的墙钟时刻


def test_a_refreshing_updated_at_is_never_stale_and_the_first_sight_never_is():
    """闪购助手每 2 秒刷新一次：每一跳的值都不一样，永远新鲜。第一次看到的值不管多旧都先当新鲜（要看满 15 秒才判得出）。"""
    tracker = fs.StatusTracker()
    for i in range(20):
        assert track(tracker, stamped(sample("running"), UPDATED_S + 3 * i), mono=3.0 * i, fresh=False)["stale"] is False
    assert track(fs.StatusTracker(), stamped(sample("running"), UPDATED_S - 86400), mono=0.0, fresh=False)["stale"] is False


def test_waking_from_sleep_with_an_old_updated_at_is_not_stale():
    """合盖睡眠：单调时钟不走、墙钟照走。醒来第一跳闪购助手还没刷新，updated_at 还是睡前那个；
    不能拿墙钟去减它（那样差值就是睡了多久），只看同一个值在单调时钟上停了多久。下一跳它刷新了，照常新鲜。"""
    tracker = fs.StatusTracker()
    track(tracker, stamped(sample("running"), UPDATED_S), mono=500.0, fresh=False)
    track(tracker, stamped(sample("running"), UPDATED_S + 2), mono=503.0, fresh=False)
    woke = track(tracker, stamped(sample("running"), UPDATED_S + 2), mono=506.0, fresh=False)   # 墙钟已过去 1 小时
    assert woke["stale"] is False and woke["attention"] == []
    assert track(tracker, stamped(sample("running"), UPDATED_S + 3600), mono=509.0, fresh=False)["stale"] is False


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


def test_losing_the_connection_twice_in_a_row_resets_the_staleness_clock():
    tracker = fs.StatusTracker()
    raw = dict(sample("running"), updated_at="garbage")
    track(tracker, raw, mono=100.0)
    tracker.update("unreachable", None, 107.0)
    tracker.update("unreachable", None, 110.0)
    assert track(tracker, raw, mono=120.0)["stale"] is False
    assert track(tracker, raw, mono=136.0)["stale"] is True


def test_a_single_missed_poll_keeps_the_staleness_clock():
    tracker = fs.StatusTracker()
    track(tracker, sample("running"), mono=100.0, fresh=False)
    tracker.update("bad_response", "no_answer", 110.0)
    assert track(tracker, sample("running"), mono=115.5, fresh=False)["stale"] is True


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
    # 列表空了本身不清零（见下面「暂时不在列表里」那几条）；全局条件断了才连不在列表里的一起清
    {"running": False, "products": []},
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


FAILURES = [("unreachable", None), ("bad_response", "json"), ("bad_response", "no_answer"), ("ok", [1])]


@pytest.mark.parametrize("kind, value", FAILURES)
def test_two_failed_polls_in_a_row_reset_the_stuck_clock(kind, value):
    tracker = fs.StatusTracker()
    track(tracker, stopped_doc(), mono=0.0)
    tracker.update(kind, value, 30.0)
    tracker.update(kind, value, 33.0)
    assert stuck_of(track(tracker, stopped_doc(), mono=61.0))["3"] is False


@pytest.mark.parametrize("kind, value", FAILURES)
def test_a_single_failed_poll_keeps_the_last_state_and_the_stuck_clock(kind, value):
    """一次抖动（一次超时、读到一半断开、事件循环卡了一下）：交回上一份状态（调用方据此不广播、照 3 秒再查），
    卡住的计时不清零——57 秒时停着的商品，60 秒那一跳没查到，63 秒照样报卡住，而不是推迟到两分多钟。"""
    tracker = fs.StatusTracker()
    for mono in (0.0, 3.0, 54.0):
        track(tracker, stopped_doc(), mono=mono)
    before = track(tracker, stopped_doc(), mono=57.0)
    assert tracker.update(kind, value, 60.0) is before
    assert tracker.why is None
    after = track(tracker, stopped_doc(), mono=63.0)
    assert stuck_of(after)["3"] is True and after["attention"] == ["stuck"]


@pytest.mark.parametrize("kind, value", FAILURES)
def test_the_second_failed_poll_in_a_row_publishes_the_empty_state(kind, value):
    tracker = fs.StatusTracker()
    track(tracker, stopped_doc(), mono=0.0)
    assert tracker.update(kind, value, 3.0)["conn"] == "ok"
    second = tracker.update(kind, value, 6.0)
    expect_conn = "unreachable" if kind == "unreachable" else "bad_response"
    expect_why = "not_object" if kind == "ok" else value
    assert second == fs.empty_state(expect_conn, expect_why)
    # 已经不是连上的状态了：之后每一次没查到都照实发布（不再「先照旧」）
    assert tracker.update("unreachable", None, 21.0) == fs.empty_state("unreachable")


def test_a_failure_with_nothing_connected_before_is_published_at_once():
    """启动后第一次就没查到、或者上一次也没查到：没有「上一份连上的状态」可留，照实发布。"""
    tracker = fs.StatusTracker()
    assert tracker.update("unreachable", None, 0.0) == fs.empty_state("unreachable")
    assert tracker.update("bad_response", "no_answer", 15.0) == fs.empty_state("bad_response", "no_answer")


# ---- 卡住计时：商品暂时不在列表里（decisions-step2 追加第 1 条） --------------------------------------
# 闪购助手 0.8.0 起每翻完一轮刷新一次 Buy now，列表先变短再补回（102 个先变 30 个）。排在后面的商品每轮都会
# 暂时不在 /status 里；以前这一跳就把它的计时清零，真卡住的永远攒不满 60 秒、按钮永远不变色

def absent_doc(**over):
    """stopped_doc 去掉停着的那个（…003），只剩进行中的 …001。"""
    doc = stopped_doc(**over)
    doc["products"] = [p for p in doc["products"] if p["product_id"] != "1000000000000000003"]
    return doc


def test_a_product_missing_from_a_poll_keeps_its_stuck_clock():
    tracker = fs.StatusTracker()
    track(tracker, stopped_doc(), mono=0.0)
    gone = track(tracker, absent_doc(), mono=30.0)
    assert [p["id"] for p in gone["products"]] == ["1000000000000000001"] and gone["attention"] == []
    track(tracker, absent_doc(), mono=33.0)
    assert stuck_of(track(tracker, stopped_doc(), mono=59.9))["3"] is False
    back = track(tracker, stopped_doc(), mono=60.0)          # 从 0 秒那次接着计，不是从 59.9 秒重新计
    assert stuck_of(back)["3"] is True and back["attention"] == ["stuck"] and back["stuck_count"] == 1


@pytest.mark.parametrize("breaker", [{"running": False}, {"paused_reason": "awaiting_approval"},
                                     {"paused_reason": "user_interaction"}])
def test_a_broken_global_condition_also_resets_products_that_are_not_listed(breaker):
    tracker = fs.StatusTracker()
    track(tracker, stopped_doc(), mono=0.0)
    track(tracker, absent_doc(), mono=20.0)
    track(tracker, absent_doc(**breaker), mono=30.0)         # 它不在列表里，计时照样清掉
    track(tracker, absent_doc(), mono=40.0)
    assert stuck_of(track(tracker, stopped_doc(), mono=61.0))["3"] is False
    assert stuck_of(track(tracker, stopped_doc(), mono=120.9))["3"] is False     # 从 61 秒重新计
    assert stuck_of(track(tracker, stopped_doc(), mono=121.0))["3"] is True


def test_a_product_unseen_for_ten_minutes_is_dropped_from_the_table():
    tracker = fs.StatusTracker()
    track(tracker, stopped_doc(), mono=0.0)
    track(tracker, absent_doc(), mono=float(fs.STUCK_FORGET_SEC))       # 正好 10 分钟：还留着
    assert set(tracker._stopped_since) == {"1000000000000000003"}
    track(tracker, absent_doc(), mono=fs.STUCK_FORGET_SEC + 0.1)
    assert tracker._stopped_since == {}


@pytest.mark.parametrize("poll_between", [False, True])
def test_a_product_back_after_ten_minutes_starts_a_new_clock(poll_between):
    """隔了 10 分钟以上才回来：从回来那一刻重新计，不接着很久以前那次（那样一回来就算卡住）。
    两条路都走：中间有一跳把它从表里丢了；中间一跳都没有（表里还留着它，回来那一跳才发现隔太久）。"""
    tracker = fs.StatusTracker()
    track(tracker, stopped_doc(), mono=0.0)
    track(tracker, absent_doc(), mono=3.0)
    if poll_between:
        track(tracker, absent_doc(), mono=fs.STUCK_FORGET_SEC + 0.2)
        assert tracker._stopped_since == {}
    t = fs.STUCK_FORGET_SEC + 0.5
    assert stuck_of(track(tracker, stopped_doc(), mono=t))["3"] is False
    assert stuck_of(track(tracker, stopped_doc(), mono=t + 59.9))["3"] is False
    assert stuck_of(track(tracker, stopped_doc(), mono=t + 60))["3"] is True


def test_the_stuck_table_is_capped_and_drops_the_longest_unseen_first(monkeypatch):
    monkeypatch.setattr(fs, "STUCK_TRACK_MAX", 3)
    tracker = fs.StatusTracker()

    def only(pid):
        return stopped_doc(products=[{"product_id": pid, "state": "stopped", "enabled": True}])

    for mono, pid in enumerate("abcd"):
        track(tracker, only(pid), mono=float(mono))
    assert set(tracker._stopped_since) == {"b", "c", "d"}              # a 最久没出现，先丢
    assert tracker._stopped_since["d"] == [3.0, 3.0]
    back = track(tracker, only("a"), mono=4.0)                         # a 回来：重新计，不是从 0 秒算
    assert set(tracker._stopped_since) == {"a", "c", "d"} and tracker._stopped_since["a"] == [4.0, 4.0]
    assert back["products"][0]["stuck"] is False


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


# ---- 闪购助手 0.8.2 的 skip、position（decisions-step2 最后一节） ------------------------------------

@pytest.mark.parametrize("value, expected", [
    ("slot_1", "slot_1"), ("no_stock", "no_stock"), ("max_renewals", "max_renewals"),
    ("auto_start_off", "auto_start_off"), ("no_position", "no_position"),
    ("brand_new<b>", "brand_newb"), ("x" * 100, "x" * fs.MAX_CODE), ("", None), ("  ", None),
    (None, None), (7, None), (True, None), (["slot_1"], None),
])
def test_skip_is_a_cleaned_code_and_unknown_codes_are_kept(value, expected):
    assert fs.normalize({"products": [{"product_id": "1", "skip": value}]})["products"][0]["skip"] == expected


@pytest.mark.parametrize("value, expected", [
    (12, 12), (1, 1), (12.0, 12), (0, 0), (12.5, None), ("12", None), (True, None), (None, None),
    (float("nan"), None), (10**10, None),
])
def test_position_is_an_integer_or_null(value, expected):
    got = fs.normalize({"products": [{"product_id": "1", "position": value}]})["products"][0]["position"]
    assert got == expected and type(got) is type(expected)


def test_an_older_helper_without_skip_or_position_reads_as_null():
    """0.8.2 以前的闪购助手没有这两个键（契约样例就是 0.7.2 的）：都当 null，什么都不变。"""
    for name in ("idle", "running", "awaiting"):
        products = fs.normalize(sample(name))["products"]
        assert products and all(p["skip"] is None and p["position"] is None for p in products)


@pytest.mark.parametrize("case", SHARED["waiting_renewal"], ids=lambda c: c["why"])
def test_the_stuck_condition_matches_the_shared_table(case):
    """「等续开 / 算卡住」的条件：后端 stuck_candidate 和前端 flashWaitingRenewal 吃同一张表。"""
    assert fs.stuck_candidate(case["product"], case["state"]) is case["expect"]


@pytest.mark.parametrize("case", SHARED["control_done"], ids=lambda c: c["why"])
def test_control_done_matches_the_shared_table(case):
    """指令生效了没有：后端 control_done（等生效时收掉 control）和前端 flashControlDone（收掉浮层里那句结果）吃同一张表。"""
    assert fs.control_done(case["action"], case["state"]) is case["expect"]


def test_control_done_refuses_anything_that_is_not_a_state():
    for junk in (None, [], "ok", 3):
        assert fs.control_done("stop", junk) is False


def test_last_ok_is_true_only_right_after_a_poll_that_was_read():
    """发控制指令之前看的是它：连上以后第一次没查到，交回的照旧是上一份 ok 状态，但 last_ok 已经是 False。"""
    tracker = fs.StatusTracker()
    assert tracker.last_ok() is False                               # 还没查过
    track(tracker, sample("idle"), mono=0.0)
    assert tracker.last_ok() is True
    kept = tracker.update("bad_response", "no_answer", 3.0)
    assert kept["conn"] == "ok" and tracker.last_ok() is False
    track(tracker, sample("idle"), mono=6.0)
    assert tracker.last_ok() is True
    tracker.update("ok", [1], 9.0)                                  # 回的不是对象
    assert tracker.last_ok() is False
    tracker.update("unreachable", None, 12.0)
    assert tracker.last_ok() is False


@pytest.mark.parametrize("case", SHARED["sort"], ids=lambda c: c["why"])
def test_the_sort_order_matches_the_shared_table(case):
    """商品的先后：后端 product_sort_key 和前端 flashSortProducts 吃同一张表。"""
    products = copy.deepcopy(case["products"])
    assert [p["id"] for p in sorted(products, key=fs.product_sort_key)] == case["ids"]


@pytest.mark.parametrize("skip", ["slot_1", "no_stock", "brand_new_reason"])
def test_a_product_the_helper_skips_on_purpose_is_never_stuck(skip):
    """skip 不为 null：闪购助手说了故意不续开（1 号位、没库存……）。停多久都不算卡住，按钮不变色。"""
    tracker = fs.StatusTracker()
    doc = stopped_doc()
    doc["products"][0]["skip"] = skip
    track(tracker, doc, mono=0.0)
    state = track(tracker, doc, mono=600.0)
    assert stuck_of(state)["3"] is False and state["attention"] == [] and state["stuck_count"] == 0
    assert state["products"][-1]["skip"] == skip
    assert tracker._stopped_since == {}


def test_a_skip_that_appears_clears_the_stuck_clock_and_one_that_goes_away_starts_it_again():
    tracker = fs.StatusTracker()
    track(tracker, stopped_doc(), mono=0.0)
    track(tracker, stopped_doc(), mono=50.0)
    skipped = stopped_doc()
    skipped["products"][0]["skip"] = "no_stock"
    track(tracker, skipped, mono=55.0)
    assert stuck_of(track(tracker, stopped_doc(), mono=70.0))["3"] is False      # 从 70 秒重新计
    assert stuck_of(track(tracker, stopped_doc(), mono=129.9))["3"] is False
    assert stuck_of(track(tracker, stopped_doc(), mono=130.0))["3"] is True


def test_the_tracker_sorts_by_card_position_within_a_tier():
    """同一档（这里都是进行中）按卡片序号从小到大，没有序号的排最后、保持原来的顺序。卡住的照样在最前。"""
    tracker = fs.StatusTracker()
    doc = stopped_doc(products=[
        {"product_id": "1000000000000000011", "state": "live", "enabled": True, "position": 7},
        {"product_id": "1000000000000000012", "state": "live", "enabled": True},
        {"product_id": "1000000000000000013", "state": "live", "enabled": True, "position": 2},
        {"product_id": "1000000000000000014", "state": "live", "enabled": True, "position": None},
        {"product_id": "1000000000000000015", "state": "stopped", "enabled": True, "position": 30},
    ])
    track(tracker, doc, mono=0.0)
    state = track(tracker, doc, mono=60.0)
    assert [p["id"][-2:] for p in state["products"]] == ["15", "13", "11", "12", "14"]
    assert [p["position"] for p in state["products"]] == [30, 2, 7, None, None]


def test_the_running_sample_puts_the_stopped_product_first_once_it_is_stuck():
    tracker = fs.StatusTracker()
    first = track(tracker, sample("running"), mono=0.0)
    assert [p["state"] for p in first["products"]][:3] == ["pending", "unknown", "live"]
    later = track(tracker, sample("running"), mono=60.0)
    assert later["products"][0]["id"] == "1000000000000000003" and later["products"][0]["stuck"] is True
    assert later["attention"] == ["stuck"]


def test_the_node_fixture_is_what_the_tracker_sends():
    """前端的 node 测试（tests/flash.test.mjs、flash.en.test.mjs、flash-button.test.mjs）吃的是 message-running.json：
    推给页面的那份状态。
    它必须就是后端 StatusTracker 对契约样例 running.json 计满 60 秒后的输出——逐字段相同，只有商品顺序不同
    （那份照契约样例的顺序，前端会再排一次）。两边谁改了形状、谁改了排序，这里就红，不会各测各的。
    后端排出来的状态先后，和 tests/flash.test.mjs 里前端 flashSortProducts(SAMPLE.products) 的期望是同一张表。"""
    tracker = fs.StatusTracker()
    track(tracker, sample("running"), mono=0.0)
    sent = track(tracker, sample("running"), mono=float(fs.STUCK_SEC))
    fixture = sample("message-running")
    assert set(fixture) == set(sent)
    assert {k: v for k, v in fixture.items() if k != "products"} == {k: v for k, v in sent.items() if k != "products"}
    by_id = {p["id"]: p for p in fixture["products"]}
    assert len(by_id) == len(fixture["products"]) == len(sent["products"])
    assert [by_id[p["id"]] for p in sent["products"]] == sent["products"]
    assert [p["state"] for p in sent["products"]] == ["stopped", "pending", "unknown", "live", "live", "live",
                                                      "between_rounds", "no_sale", "ended_by_user"]


def test_the_panel_ranks_orders_and_quotes_numbers_like_the_backend():
    """前端 web/flash.js 有三处照抄了后端的东西，这里逐项对上，哪边改了另一边没跟就红：
    - flashRank 的排序表和 flash_sale._RANK（卡住的 0 档、认不出的最后一档）；
    - FLASH_ATTN_ORDER（浮层主句的先后）和 attention() 排出来的顺序；
    - 文案里写死的数：连不上「每 15 秒再查一次」= POLL_DOWN_SEC，「停了超过 1 分钟」= STUCK_SEC，
      没有回应「在 2 秒内」= HTTP_TIMEOUT_SEC，「30 秒内没看到」= CONTROL_WAIT_SEC；
    - 控制那几张表：FLASH_ACTIONS = CONTROL_ACTIONS，FLASH_OWN_ERRORS = CONTROL_OWN_ERRORS（页面按它分「本程序这边的」和
      「闪购助手没有接受」），FLASH_QUIET_PAGES = START_QUIET_PAGES（开始后的过渡里不算要人处理的页面状态）。"""
    js = (Path(__file__).resolve().parent.parent / "web" / "flash.js").read_text(encoding="utf-8")
    body = re.search(r"function flashRank\(p\) \{(.*?)\n\}", js, re.S).group(1)
    assert {code: int(n) for code, n in re.findall(r'case "(\w+)": return (\d+);', body)} == fs._RANK
    assert "p.stuck === true) return {};".format(fs._RANK_STUCK) in body
    assert "default: return {};".format(fs._RANK_OTHER) in body
    order = json.loads(re.search(r"var FLASH_ATTN_ORDER = (\[[^\]]*\]);", js).group(1))
    every = fs.attention({"conn": "ok", "stale": True, "page": "captcha", "running": True, "paused_reason": "error",
                          "products": [{"stuck": True}]})
    assert order == every + fs.attention({"conn": "bad_response"})
    assert fs.POLL_DOWN_SEC == 15 and "每 15 秒再查一次" in js and "Checking again every 15 sec." in js
    assert fs.HTTP_TIMEOUT_SEC == 2 and "在 2 秒内没有回应" in js and "didn’t answer within 2 sec." in js
    assert fs.STUCK_SEC == 60 and "停了超过 1 分钟" in js and "Stopped for over 1 min" in js
    assert fs.CONTROL_WAIT_SEC == 30 and js.count("30 秒内没看到闪购助手") == 3 and js.count("within 30 sec.") == 3
    assert "在 2 秒内没有回应。看看状态" in js and "didn’t answer within 2 sec. Check" in js
    for name, want in (("FLASH_ACTIONS", fs.CONTROL_ACTIONS), ("FLASH_OWN_ERRORS", fs.CONTROL_OWN_ERRORS),
                       ("FLASH_QUIET_PAGES", fs.START_QUIET_PAGES)):
        got = json.loads(re.search(r"var {} = (\[[^\]]*\]);".format(name), js).group(1))
        assert got == list(want), name


def test_the_samples_raise_the_documented_attention():
    assert track(fs.StatusTracker(), sample("idle"))["attention"] == []
    assert track(fs.StatusTracker(), sample("running"))["attention"] == []
    assert track(fs.StatusTracker(), sample("awaiting"))["attention"] == ["paused"]


def test_a_non_object_body_becomes_bad_response():
    tracker = fs.StatusTracker()
    state = tracker.update("ok", [1, 2], 0.0)
    assert state == fs.empty_state("bad_response", "not_object") and tracker.why == "not_object"


def test_failures_publish_empty_states_and_remember_the_reason():
    tracker = fs.StatusTracker()
    assert tracker.update("unreachable", None, 0.0) == fs.empty_state("unreachable")
    assert tracker.why is None
    assert tracker.update("bad_response", "http_404", 0.0) == fs.empty_state("bad_response", "http_404")
    assert tracker.why == "http_404"
    track(tracker, sample("running"))
    assert tracker.why is None and tracker.schema == 1


def test_the_same_status_gives_an_equal_state_even_as_updated_at_ticks():
    """去重靠这个：updated_at 每 2 秒变一次，但不下发它，只要内容没变两次的结果就相等。"""
    tracker = fs.StatusTracker()
    a = track(tracker, sample("running"), mono=0.0, fresh=False)
    later = dict(sample("running"), updated_at="2026-10-27T21:36:09.201Z")
    b = track(tracker, later, mono=2.0, fresh=False)
    assert a == b
    changed = copy.deepcopy(later)
    changed["products"][0]["stock_left"] = 29
    assert track(tracker, changed, mono=2.0, fresh=False) != b
