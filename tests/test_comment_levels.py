"""弹幕作者的等级 / 徽章候选字段（app/comment_levels.py）：只数、不显示，写进 session_end.comment_fields。

2026-10-21 用户问「能不能显示账号等级、粉丝牌」，决定先只记录量 1～2 场再说。这里钉住五件事：
  1. 照 TikTokLive 7.0.1 / TikTokLiveProto 0.2.2（v3 proto）的形状造假事件，各候选数得对、等级分桶对、
     枚举记名字（认不出的记数值）；
  2. 吐出来的只有数字和枚举名：昵称、id、粉丝团名、徽章文字、弹幕内容一个字都不出现；
  3. 字段缺失、类型不对、属性一读就抛，都不抛出来，也不影响原有的计数；
  4. 父进程累加（封顶、坏行丢掉、表数有上限），并且 #107 修过的三条收尾路径写出的 session_end 都带着它；
  5. 真 v3 proto 往返（造消息 → bytes() → CommentEvent().parse() → FieldStats）计数对、不漏文字。

除了最后一组（第 5 件事，文件里第 6 节），全程不 import TikTokLive、不联网：假事件是 SimpleNamespace，
枚举是照库里的名字和数值写的 IntEnum。那一组用 importorskip：装了 TikTokLive 的本机 .venv 真跑，
没装的环境（CI）跳过。
昵称、团名、徽章文字都是虚构的。
"""
import asyncio
import json
import re
from enum import IntEnum
from types import SimpleNamespace

import pytest

from app import comment_levels as cl
from app.comment_source import CommentSource
from app.comment_worker import FieldStats


class Scene(IntEnum):
    """BadgeStruct.scene_type（webcast.model.data.BadgeSceneType）的一部分。认不出的值照 betterproto2 的做法
    造一个名字为空串的成员。"""
    BADGE_SCENE_TYPE_UNKNOWN = 0
    ADMIN = 1
    SUBSCRIBER = 4
    USER_GRADE = 8
    FANS = 10

    @classmethod
    def _missing_(cls, value):
        if not isinstance(value, int):
            return None
        obj = int.__new__(cls, value)
        obj._value_ = value
        obj._name_ = ""
        return obj


class Display(IntEnum):
    BADGE_DISPLAY_TYPE_UNKNOWN = 0
    IMAGE = 1
    TEXT = 2
    STRING = 3
    COMBINE = 4


class MetricsType(IntEnum):
    """UserMetrics.type（webcast.im 的 ...UserMetricsUserMetricsType）。认不出的值同 Scene。"""
    USER_METRICS_TYPE_UNKNOWN = 0
    USER_METRICS_TYPE_GRADE = 1
    USER_METRICS_TYPE_SUBSCRIBE = 2
    USER_METRICS_TYPE_FOLLOW = 3
    USER_METRICS_TYPE_FANS_CLUB = 4
    USER_METRICS_TYPE_TOP_VIEWER = 5
    USER_METRICS_TYPE_GIFT = 6

    @classmethod
    def _missing_(cls, value):
        if not isinstance(value, int):
            return None
        obj = int.__new__(cls, value)
        obj._value_ = value
        obj._name_ = ""
        return obj


class ClubStatus(IntEnum):
    NOT_JOINED = 0
    ACTIVE = 1
    INACTIVE = 2


def img(url="https://p16-webcast.example/badge.webp"):
    return SimpleNamespace(url_list=[url], uri="webcast-va/badge")


def badge(scene, display=Display.BADGE_DISPLAY_TYPE_UNKNOWN, log_level="", combine_str="", pieces=()):
    """BadgeStruct 的形状：文字和 id 都填上「不许出现」的值，看它们有没有漏进计数。"""
    return SimpleNamespace(
        scene_type=scene, display_type=display, exhibition_type=0, priority_type=0, display=True,
        privilege_log_extra=SimpleNamespace(level=log_level, privilege_id="7155001",
                                            privilege_order_id="order_99887766", data_version="2"),
        combine=SimpleNamespace(str=combine_str, icon=img(),
                                text=SimpleNamespace(key="pm_mt_badge_name", default_pattern="{0}",
                                                     pieces=list(pieces))),
        text=None, str=None, image=None)


def v3_user(**over):
    """User 的形状（CommentEvent().parse() 出来是普通 User；只放这里用到的字段；没设的照 betterproto：
    子消息 None，int 0，list 空，bytes 空）。"""
    fields = dict(
        id=7381234567890123456, id_str="7381234567890123456", sec_uid="MS4wLjABAAAA_secret",
        display_id="toni_live", nickname="Toñita 🇭🇳",
        pay_grade=SimpleNamespace(level=23, name="Nivel Oro", score=0, grade_icon_list=[],
                                  icon=None, im_icon_with_level=None),
        fans_club=SimpleNamespace(
            data=SimpleNamespace(club_name="Club Girasol", level=7, user_fans_club_status=ClubStatus.ACTIVE,
                                 badge=SimpleNamespace(icons={1: img()}, title="Fans Girasol"), anchor_id=0),
            prefer_data={}),
        fans_club_info=None,
        badge_list=[
            badge(Scene.FANS, Display.COMBINE, log_level="7", combine_str="Marisol"),
            badge(Scene.USER_GRADE, Display.COMBINE, combine_str="23"),
            badge(Scene.SUBSCRIBER, Display.IMAGE, pieces=["Girasol", "3"]),
            badge(Scene(99), Display.TEXT),
        ],
        subscribe_info=None, is_subscribe=False, top_vip_no=0, pay_score=0, pay_scores=0,
        border=None, border_list=[], badge_image_list=[], media_badge_image_list=[],
        real_time_icons=[], new_real_time_icons=[], combo_badge_info=None, medal=None, exp=0,
        webcast_anchor_level=None, anchor_level=None, user_role=0, user_vip_info=b"",
    )
    fields.update(over)
    return SimpleNamespace(**fields)


def v3_event(user, text="hola chicas", **over):
    fields = dict(comment=text, content=text, user=user, user_identity=None, at_user=None,
                  mention_users=[], common=SimpleNamespace(msg_id=77),
                  public_area_common=SimpleNamespace(user_label=img(), user_consume_in_room=150),
                  public_area_message_common=None)
    fields.update(over)
    return SimpleNamespace(**fields)


def leaves(obj, path=()):
    """(路径, 值) 逐个叶子。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from leaves(v, path + (k,))
    else:
        yield path, obj


# ---- 1. 照 v3 的形状数 ---------------------------------------------------------------------

def test_counts_candidates_buckets_levels_and_names_enums():
    stats = FieldStats()
    stats.observe(v3_event(v3_user()))
    stats.observe(v3_event(v3_user(pay_grade=None, fans_club=None, badge_list=[]),
                           public_area_common=None))
    line = stats.take()
    assert line["n"] == 2
    has = line["level_has"]
    assert set(has) == set(cl.HAS_KEYS)                       # 每次都带全部候选，0 = 量过、没有
    assert has["pay_grade"] == has["pay_grade_any"] == 1
    assert has["fans_club_any"] == 1 and line["has"]["fans_club"] == 1      # 原有计数照旧
    assert has["badge_list"] == has["badge_level"] == 1
    assert has["user_label"] == has["consume_in_room"] == 1
    assert has["fans_club_info"] == has["top_vip_no"] == has["subscribe"] == 0
    hist = line["level_hist"]
    assert hist["pay_grade"] == {"20-29": 1}
    assert hist["fans_club"] == {"5-9": 1}
    assert hist["badge:FANS"] == {"5-9": 1}                   # privilege_log_extra.level（字符串 "7"）
    assert hist["badge:USER_GRADE"] == {"20-29": 1}           # combine.str 是纯数字
    assert hist["badge:SUBSCRIBER"] == {"2-4": 1}             # text.pieces 里的纯数字
    assert hist["badge_count"] == {"2-4": 1}
    assert hist["consume_in_room"] == {"100-999": 1}
    kinds = line["level_kinds"]
    assert kinds["badge_scene"] == {"FANS": 1, "USER_GRADE": 1, "SUBSCRIBER": 1, "99": 1}
    assert kinds["badge_display"] == {"COMBINE": 1, "IMAGE": 1, "TEXT": 1}
    assert kinds["badge_level_src"] == {"FANS:log": 1, "USER_GRADE:str": 1, "SUBSCRIBER:pieces": 1}   # 按场景分开
    assert kinds["fans_club_status"] == {"ACTIVE": 1}
    assert stats.take() is None                               # 增量，吐完清零


def test_level_source_is_kept_per_scene():
    """FANS 和 USER_GRADE 的等级来源分开记：同一来源 log 在两个场景上是两个标签，不并成一个 log。"""
    stats = cl.LevelStats()
    stats.observe(v3_event(v3_user(badge_list=[badge(Scene.FANS, log_level="7"),
                                               badge(Scene.USER_GRADE, log_level="23"),
                                               badge(Scene.USER_GRADE, combine_str="24")])))
    stats.observe(v3_event(v3_user(badge_list=[badge(Scene.FANS, pieces=["Girasol", "4"])])))
    out = stats.take()
    assert out["level_kinds"]["badge_level_src"] == {"FANS:log": 1, "USER_GRADE:log": 1, "USER_GRADE:str": 1,
                                                     "FANS:pieces": 1}
    assert out["level_hist"]["badge:USER_GRADE"] == {"20-29": 1}


def test_two_badges_of_one_scene_count_once_and_keep_the_higher_level():
    stats = cl.LevelStats()
    user = v3_user(badge_list=[badge(Scene.FANS, log_level="3"), badge(Scene.FANS, log_level="12")])
    stats.observe(v3_event(user))
    out = stats.take()
    assert out["level_kinds"]["badge_scene"] == {"FANS": 1}
    assert out["level_hist"]["badge:FANS"] == {"10-19": 1}


def test_v2_names_are_read_when_the_v3_ones_are_missing():
    """库要是把名字改回 v2（badges / badge_scene / log_extra），照样读得到。"""
    old = SimpleNamespace(badge_scene=Scene.FANS, display_type=Display.TEXT,
                          log_extra=SimpleNamespace(level="20"))
    stats = cl.LevelStats()
    stats.observe(v3_event(SimpleNamespace(badges=[old])))
    out = stats.take()
    assert out["level_has"]["badge_list"] == out["level_has"]["badge_level"] == 1
    assert out["level_hist"]["badge:FANS"] == {"20-29": 1}


def test_other_candidates_and_their_alternative_paths():
    user = v3_user(
        pay_grade=SimpleNamespace(level=0, name="", score=0, grade_icon_list=[], icon=img()),
        fans_club=SimpleNamespace(data=None, prefer_data={3: SimpleNamespace(level=4), 5: SimpleNamespace(level=9)}),
        fans_club_info=SimpleNamespace(fans_level=2, fans_score=0, is_sleeping=False, fans_club_name="", badge=None),
        badge_list=[], subscribe_info=SimpleNamespace(is_subscribe=False, is_subscribed_to_anchor=True,
                                                      badge=SimpleNamespace(origin_img=img(), preview_img=None)),
        top_vip_no=3, pay_score=1200, border=SimpleNamespace(level=0, icon=None),
        border_list=[SimpleNamespace(level=5, icon=None)], media_badge_image_list=[img()],
        new_real_time_icons=[img()], combo_badge_info=SimpleNamespace(combo_count=0, icon=img()),
        medal=img(), exp=40, anchor_level=SimpleNamespace(level=11), user_role=2)
    stats = cl.LevelStats()
    stats.observe(v3_event(user, public_area_common=SimpleNamespace(user_label=None, user_consume_in_room=0)))
    out = stats.take()
    has = out["level_has"]
    expected_one = {"pay_grade_any", "fans_club_prefer", "fans_club_info", "fans_club_info_any", "subscribe",
                    "subscribe_badge", "top_vip_no", "pay_score", "border", "badge_images", "real_time_icons",
                    "combo_badge", "medal", "exp", "anchor_level", "user_role"}
    assert {k for k, v in has.items() if v} == expected_one
    assert out["level_hist"] == {"fans_club_prefer": {"5-9": 1}, "fans_club_info": {"2-4": 1},
                                 "top_vip_no": {"2-4": 1}, "border": {"5-9": 1}, "anchor_level": {"10-19": 1}}
    assert out["level_kinds"] == {"user_role": {"2": 1}}


def metrics_area(*metrics):
    """PublicAreaMessageCommon 的形状：portrait_info.user_metrics + 一个带文字的 portrait_tag（不许漏出来）。"""
    tag = SimpleNamespace(tag_id="tag_marisol_77", priority=1, show_value="Marisol", show_args="")
    return SimpleNamespace(portrait_info=SimpleNamespace(user_metrics=list(metrics), portrait_tag=[tag]),
                           is_anchor_marked=False)


def metric(kind, value):
    return SimpleNamespace(type=kind, metrics_value=value)


def test_user_metrics_count_type_names_buckets_and_value_shapes_only():
    stats = cl.LevelStats()
    area = metrics_area(metric(MetricsType.USER_METRICS_TYPE_GRADE, "23"),
                        metric(MetricsType.USER_METRICS_TYPE_GRADE, "31"),      # 同 type 两项：算 1 条、取高
                        metric(MetricsType.USER_METRICS_TYPE_FANS_CLUB, " 7 "),
                        metric(MetricsType.USER_METRICS_TYPE_TOP_VIEWER, "Top 3 Marisol"),
                        metric(MetricsType.USER_METRICS_TYPE_GIFT, ""),
                        metric(MetricsType(42), "5"),                           # 认不出的 type 记数值
                        metric(MetricsType.USER_METRICS_TYPE_SUBSCRIBE, "7381234567890123456"),
                        Boom(), None)                                           # 坏的一项只少数它自己
    stats.observe(v3_event(v3_user(), public_area_message_common=area))
    stats.observe(v3_event(v3_user(), public_area_message_common=metrics_area()))   # 空列表不算
    out = stats.take()
    assert out["level_has"]["user_metrics"] == 1
    assert out["level_kinds"]["user_metrics_type"] == {
        "USER_METRICS_TYPE_GRADE": 1, "USER_METRICS_TYPE_FANS_CLUB": 1, "USER_METRICS_TYPE_TOP_VIEWER": 1,
        "USER_METRICS_TYPE_GIFT": 1, "42": 1, "USER_METRICS_TYPE_SUBSCRIBE": 1}
    assert out["level_kinds"]["user_metrics_value"] == {
        "USER_METRICS_TYPE_GRADE:digits": 1, "USER_METRICS_TYPE_FANS_CLUB:digits": 1,
        "USER_METRICS_TYPE_TOP_VIEWER:text": 1, "USER_METRICS_TYPE_GIFT:empty": 1, "42:digits": 1,
        "USER_METRICS_TYPE_SUBSCRIBE:text": 1}                                 # 19 位 id 不是等级
    metrics_hist = {k: v for k, v in out["level_hist"].items() if k.startswith("metrics:")}
    assert metrics_hist == {"metrics:USER_METRICS_TYPE_GRADE": {"30-49": 1},
                            "metrics:USER_METRICS_TYPE_FANS_CLUB": {"5-9": 1}, "metrics:42": {"5-9": 1}}
    dumped = json.dumps(out, ensure_ascii=False)
    for secret in ("Marisol", "Top 3", "7381234567890123456", "tag_marisol"):
        assert secret not in dumped, secret


def test_grade_icons_vip_info_and_border_log_level():
    user = v3_user(
        pay_grade=SimpleNamespace(level=0, name="", score=0, icon=None, grade_icon_list=[
            SimpleNamespace(level=0, level_str="12", icon=img()), SimpleNamespace(level=25, level_str="", icon=None),
            Boom(), None]),
        user_vip_info=b"\x08\x01vip-girasol",
        border=SimpleNamespace(level=0, icon=None, name="Marco Girasol",
                               border_privilege_log_extra=SimpleNamespace(level="4", privilege_id="7155001")))
    stats = cl.LevelStats()
    stats.observe(v3_event(user))
    stats.observe(v3_event(v3_user(pay_grade=None, user_vip_info="not bytes",
                                   border=SimpleNamespace(level=0, icon=None, border_privilege_log_extra=Boom()))))
    out = stats.take()
    has, hist = out["level_has"], out["level_hist"]
    assert has["pay_grade"] == 0 and has["pay_grade_any"] == 1     # 等级没填、图标表来了
    assert hist["pay_grade_icon"] == {"20-29": 1}                   # level 和 level_str 里最大的
    assert hist["pay_grade_icon_count"] == {"2-4": 1}
    assert has["user_vip_info"] == 1                                 # 只认 bytes，只数非空
    assert has["border"] == 1 and hist["border_log"] == {"2-4": 1} and "border" not in hist
    dumped = json.dumps(out, ensure_ascii=False)
    for secret in ("vip-girasol", "Girasol", "7155001"):
        assert secret not in dumped, secret


@pytest.mark.parametrize("value,expected", [(1, "1"), (4, "2-4"), (9, "5-9"), (19, "10-19"), (29, "20-29"),
                                            (49, "30-49"), (99, "50-99"), (999, "100-999"), (10 ** 9, "1000+"),
                                            (0, None), (-3, None), (True, None), ("7", None)])
def test_buckets(value, expected):
    assert cl.bucket(value) == expected


@pytest.mark.parametrize("value,expected", [(7, 7), ("12", 12), (" 5 ", 5), ("0", 0), ("12abc", 0),
                                            ("١٢", 0), ("1234567", 0), ("7381234567890123456", 0),
                                            (True, 0), (-1, 0), (None, 0), (1.5, 0)])
def test_level_of_takes_only_short_ascii_numbers(value, expected):
    assert cl.level_of(value) == expected


# ---- 2. 只有数字和枚举名 ----------------------------------------------------------------------

SECRETS = ("Toñita", "toni_live", "7381234567890123456", "MS4wLjABAAAA", "Girasol", "Marisol", "Nivel Oro",
           "pm_mt_badge_name", "order_99887766", "7155001", "hola chicas", "p16-webcast", "webcast-va")


def test_the_line_holds_numbers_and_enum_names_only():
    stats = FieldStats()
    for _ in range(3):
        stats.observe(v3_event(v3_user()))
    line = stats.take()
    dumped = json.dumps(line, ensure_ascii=False)
    for secret in SECRETS:
        assert secret not in dumped, secret
    key_ok = re.compile(r"[A-Za-z0-9_:+-]{1,64}\Z")
    for path, value in leaves({k: line[k] for k in cl.LEVEL_FIELDS}):
        assert all(key_ok.match(str(p)) for p in path), path
        assert isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= line["n"], (path, value)


# ---- 3. 缺字段、类型不对、一读就抛：都不抛 ---------------------------------------------------------

class Boom:
    """任何属性一读就抛（不是 AttributeError，getattr 的默认值兜不住）。"""

    def __getattr__(self, name):
        raise RuntimeError("boom " + name)


def weird_user():
    return v3_user(
        pay_grade=Boom(),
        fans_club=SimpleNamespace(data=Boom(), prefer_data=[1, 2]),
        fans_club_info="not a message",
        badge_list=[None, 5, "FANS", Boom(),
                    SimpleNamespace(scene_type="FANS", display_type=True,
                                    privilege_log_extra=SimpleNamespace(level="12abc"),
                                    combine=SimpleNamespace(str="١٢", text=SimpleNamespace(pieces="12")))],
        subscribe_info=Boom(), top_vip_no=True, pay_score=-5, pay_scores=None,
        border=SimpleNamespace(level="9999999"), border_list=None,
        badge_image_list=[SimpleNamespace(url_list=None, uri=None)], real_time_icons="x",
        combo_badge_info=Boom(), medal=Boom(), exp=None, anchor_level=Boom(), user_role="admin",
        user_vip_info="vip as text")


@pytest.mark.parametrize("event", [
    pytest.param(lambda: SimpleNamespace(comment="sin usuario"), id="no_user"),
    pytest.param(lambda: v3_event(None), id="user_none"),
    pytest.param(lambda: v3_event(SimpleNamespace()), id="user_without_fields"),
    pytest.param(lambda: v3_event(Boom()), id="user_raises"),
    pytest.param(lambda: v3_event(weird_user(), public_area_common=Boom()), id="weird_fields"),
    pytest.param(lambda: v3_event(weird_user(), public_area_message_common=Boom()), id="metrics_raise"),
    pytest.param(lambda: v3_event(weird_user(), public_area_message_common=SimpleNamespace(
        portrait_info=SimpleNamespace(user_metrics="GRADE 23"))), id="metrics_not_a_list"),
    pytest.param(lambda: Boom(), id="event_raises"),
])
def test_missing_or_odd_fields_never_raise(event):
    stats = cl.LevelStats()
    stats.observe(event())
    out = stats.take()
    assert set(out["level_has"]) == set(cl.HAS_KEYS)
    for _path, value in leaves(out):
        assert isinstance(value, int) and value >= 0


def test_odd_level_fields_leave_the_existing_counts_alone():
    stats = FieldStats()
    stats.observe(v3_event(weird_user(), public_area_common=Boom()))
    line = stats.take()
    assert line["n"] == 1 and line["has"]["id"] == 1 and line["has"]["display_id"] == 1
    has = line["level_has"]
    # 徽章列表本身非空要数；那几枚长得不对的徽章读不出场景、等级
    assert has["badge_list"] == 1 and has["badge_level"] == 0
    assert all(has[k] == 0 for k in ("pay_grade", "top_vip_no", "pay_score", "user_role", "border",
                                     "fans_club_prefer", "badge_images", "real_time_icons", "exp",
                                     "user_vip_info", "user_metrics"))
    assert "badge_scene" not in line["level_kinds"] and "badge_display" not in line["level_kinds"]


def test_one_odd_badge_does_not_hide_the_others():
    stats = cl.LevelStats()
    stats.observe(v3_event(v3_user(badge_list=[Boom(), badge(Scene.FANS, log_level="2")])))
    out = stats.take()
    assert out["level_kinds"]["badge_scene"] == {"FANS": 1}
    assert out["level_hist"]["badge:FANS"] == {"2-4": 1}


def test_unknown_enum_names_overflow_into_other():
    stats = cl.LevelStats()
    stats.observe(v3_event(v3_user(badge_list=[badge(Scene(1000 + i)) for i in range(cl.MAX_BADGES)])))
    stats.observe(v3_event(v3_user(badge_list=[badge(Scene(2000 + i)) for i in range(cl.MAX_BADGES)])))
    scenes = stats.take()["level_kinds"]["badge_scene"]
    assert len(scenes) == cl.MAX_LABELS + 1 and scenes["other"] == 2 * cl.MAX_BADGES - cl.MAX_LABELS


# ---- 4. 父进程累加 ------------------------------------------------------------------------------

def make_source():
    async def on_items(items):
        pass

    async def on_state(state, detail):
        pass

    return CommentSource(on_items, on_state, cookies_browser="none")


def test_parent_accumulates_caps_and_drops_bad_entries(capsys):
    cs = make_source()
    cs._add_field_stats({"event": "field_stats", "n": 50, "has": {"id": 50},
                         "level_has": {"badge_list": 30, "pay_grade": 0, "bad key": 5, "medal": -1, "exp": True},
                         "level_hist": {"badge:FANS": {"5-9": 10, "1": 99}, "x y": {"1": 1}, "pay_grade": [1]},
                         "level_kinds": {"badge_scene": {"FANS": 10, "USER_GRADE": 30, "名字": 3}}})
    cs._add_field_stats({"event": "field_stats", "n": 20, "has": {"id": 20},
                         "level_has": {"badge_list": 20, "pay_grade": 0},
                         "level_hist": {"badge:FANS": {"5-9": 5}},
                         "level_kinds": {"badge_scene": {"FANS": 5}, "badge_display": "COMBINE"}})
    fields = cs.comment_fields
    assert fields["n"] == 70 and fields["has"] == {"id": 70}
    assert fields["level_has"] == {"badge_list": 50, "pay_grade": 0}           # 0 也留着：量过、没有
    assert fields["level_hist"] == {"badge:FANS": {"5-9": 15, "1": 50}}        # 超过这一行 n 的按 n 封顶
    assert fields["level_kinds"] == {"badge_scene": {"FANS": 15, "USER_GRADE": 30}}
    out = capsys.readouterr().out
    assert "[弹幕] 等级与徽章：累计 70 条里 徽章 50、徽章场景 FANS 15、徽章场景 USER_GRADE 30" in out
    assert "送礼等级" not in out                                              # 只打非 0 的


def test_parent_line_says_nothing_found_and_old_lines_add_no_level_keys(capsys):
    cs = make_source()
    cs._add_field_stats({"event": "field_stats", "n": 5, "has": {"id": 5}})
    assert cs.comment_fields == {"n": 5, "has": {"id": 5}}                     # 老格式的行：不多出键
    assert "等级与徽章" not in capsys.readouterr().out
    cs._add_field_stats({"event": "field_stats", "n": 5, "has": {"id": 5},
                         "level_has": dict.fromkeys(cl.HAS_KEYS, 0), "level_hist": {}, "level_kinds": {}})
    assert "[弹幕] 等级与徽章：累计 10 条里 都没有" in capsys.readouterr().out
    assert cs.comment_fields["level_has"] == dict.fromkeys(cl.HAS_KEYS, 0)


def zero_line(n, **has):
    return {"event": "field_stats", "n": n, "has": {"id": n},
            "level_has": dict(dict.fromkeys(cl.HAS_KEYS, 0), **has), "level_hist": {}, "level_kinds": {}}


def test_parent_line_is_printed_only_when_it_changes(capsys):
    """每 50 条一行，全是「都没有」时不要每次都打：除了累计条数和上一次一样就不打，变了再打。"""
    cs = make_source()
    cs._add_field_stats(zero_line(50))
    assert "[弹幕] 等级与徽章：累计 50 条里 都没有" in capsys.readouterr().out
    cs._add_field_stats(zero_line(50))
    out = capsys.readouterr().out
    assert "[弹幕] 字段：累计 100 条里" in out and "等级与徽章" not in out     # 原有那行照打
    cs._add_field_stats(zero_line(50, badge_list=3))
    assert "[弹幕] 等级与徽章：累计 150 条里 徽章 3" in capsys.readouterr().out
    cs._add_field_stats(zero_line(50))
    assert "等级与徽章" not in capsys.readouterr().out                         # 还是「徽章 3」：不重打
    cs._add_field_stats(zero_line(50, badge_list=1))
    assert "[弹幕] 等级与徽章：累计 250 条里 徽章 4" in capsys.readouterr().out


def test_parent_line_starts_over_for_a_new_session(monkeypatch, capsys):
    """换一场（_launch）从头记：新的一场第一次「都没有」照样打出来。"""
    cs = make_source()
    cs._add_field_stats(zero_line(50))
    capsys.readouterr()

    async def idle(unique_id):
        pass

    monkeypatch.setattr(cs, "_supervise", idle)

    async def go():
        cs._launch("otra_sala")
        await cs._task

    asyncio.run(go())
    cs._add_field_stats(zero_line(50))
    assert "[弹幕] 等级与徽章：累计 50 条里 都没有" in capsys.readouterr().out


def test_parent_caps_the_number_of_tables():
    totals = {"n": 0, "has": {}}
    many = {"badge:S{}".format(i): {"1": 1} for i in range(cl.MAX_TABLES + 10)}
    assert cl.merge(totals, {"level_hist": many}, 1)
    assert len(totals["level_hist"]) == cl.MAX_TABLES
    more = {"badge:T{}".format(i): {"1": 1} for i in range(5)}
    cl.merge(totals, {"level_hist": more, "level_kinds": {"badge_scene": {"FANS": 1}}}, 1)
    assert len(totals["level_hist"]) == cl.MAX_TABLES                # 满了的表不再加新名字
    assert totals["level_kinds"] == {"badge_scene": {"FANS": 1}}       # 别的块照常并


def test_snapshot_copies_both_levels():
    fields = {"n": 3, "has": {"id": 3}, "level_has": {"badge_list": 2},
              "level_hist": {"badge:FANS": {"1": 2}}, "level_kinds": {"badge_scene": {"FANS": 2}}}
    snap = cl.snapshot(fields)
    fields["level_hist"]["badge:FANS"]["1"] = 99
    fields["level_has"]["badge_list"] = 99
    assert snap == {"level_has": {"badge_list": 2}, "level_hist": {"badge:FANS": {"1": 2}},
                    "level_kinds": {"badge_scene": {"FANS": 2}}}
    assert cl.snapshot({"n": 3, "has": {}}) == {} and cl.snapshot(None) == {}


def test_end_to_end_child_lines_to_parent_totals():
    """子进程那边真的 FieldStats 吐的行，父进程原样累加：两边对得上。"""
    stats, cs = FieldStats(), make_source()
    for i in range(120):
        stats.observe(v3_event(v3_user() if i % 3 == 0 else v3_user(badge_list=[], pay_grade=None)))
        if stats.n >= stats.FLUSH_EVERY:
            cs._add_field_stats(json.loads(json.dumps(stats.take())))
    cs._add_field_stats(json.loads(json.dumps(stats.take())))
    fields = cs.comment_fields
    assert fields["n"] == 120
    assert fields["level_has"]["badge_list"] == fields["level_has"]["pay_grade"] == 40
    assert fields["level_hist"]["badge:FANS"] == {"5-9": 40}
    assert fields["level_kinds"]["badge_scene"]["FANS"] == 40


# ---- 5. #107 的三条收尾路径都带着 ---------------------------------------------------------------

LEVELS = {"level_has": {"badge_list": 2, "pay_grade": 0}, "level_hist": {"badge:FANS": {"5-9": 2}},
          "level_kinds": {"badge_scene": {"FANS": 2}}}


@pytest.mark.parametrize("close", ["own", "stand_in", "window_close"])
def test_every_session_end_path_carries_the_level_counts(monkeypatch, tmp_path, close):
    from app import window_close
    from tests.test_session_end_paths import GRACE, Comments, make, session_end_of, started

    class LevelComments(Comments):
        def start(self, unique_id):
            super().start(unique_id)
            self.comment_fields.update(json.loads(json.dumps(LEVELS)))

    stop_sec, grace = {"own": (0.02, 1.0), "stand_in": (0.6, GRACE), "window_close": (0.6, 0.4)}[close]
    p = make(monkeypatch, tmp_path, stop_sec, grace=grace)
    p.comment_source = LevelComments()

    async def go():
        task = await started(p)
        audit = p.audit
        if close == "window_close":
            assert await window_close.close_session(p, audit_budget=0.1, total=4.0)
        else:
            await p.stop_stream()
        await asyncio.wait({task}, timeout=5)
        return task, audit

    task, audit = asyncio.run(go())
    assert task.done()
    end = session_end_of(audit)
    assert end["comment_fields"] == dict({"n": 3, "has": {"id": 3}}, **LEVELS)   # 原有的键原样，新键并在旁边


# ---- 6. 真 v3 proto 往返（本机 .venv 真跑；没装 TikTokLive 的环境跳过） ---------------------------------

REAL_SECRETS = ("Toñita", "toni_live", "7381234567890123456", "MS4wLjABAAAA", "Girasol", "Marisol", "Nivel Oro",
                "pm_mt_badge_name", "order_99887766", "7155001", "hola chicas", "p16-webcast", "webcast-va",
                "tag_marisol", "Top 3", "vip-blob")


def test_real_v3_proto_round_trip():
    """造真 WebcastChatMessage → bytes() → CommentEvent().parse() → FieldStats：计数对、文字一个都不漏。
    文件头写的「已用真 proto 往返验证」就是这一条。"""
    pytest.importorskip("TikTokLiveProto")
    events = pytest.importorskip("TikTokLive.events")
    from TikTokLiveProto.v3.webcast.im import PublicAreaMessageCommonPortraitInfoUserMetricsUserMetricsType as MT
    from TikTokLiveProto.v3.webcast.model import (BorderInfo, FansClubData, FansClubMember, GradeIcon, UserBadge,
                                                  UserHonor)
    from TikTokLiveProto.v3.webcast.model.base import ImageModel
    from TikTokLiveProto.v3.webcast.model.base.user import (BadgeStruct, BadgeText, CombineBadgeStruct,
                                                            PrivilegeLogExtra, User)
    from TikTokLiveProto.v3.webcast.model.data import BadgeDisplayType, BadgeSceneType
    from TikTokLiveProto.v3.webcast.model.message import (PortraitInfo, PortraitTag, PublicAreaCommon,
                                                          PublicAreaMessageCommon, UserMetrics,
                                                          WebcastChatMessage)

    def image():
        return ImageModel(url_list=["https://p16-webcast.example/badge.webp"], uri="webcast-va/badge")

    user = User(
        id=7381234567890123456, id_str="7381234567890123456", sec_uid="MS4wLjABAAAA_secret",
        display_id="toni_live", nickname="Toñita 🇭🇳",
        pay_grade=UserHonor(level=23, name="Nivel Oro",
                            grade_icon_list=[GradeIcon(level=23, level_str="23", icon=image())]),
        fans_club=FansClubMember(data=FansClubData(level=7, club_name="Club Girasol", user_fans_club_status=1,
                                                   badge=UserBadge(icons={1: image()}, title="Fans Girasol"))),
        badge_list=[
            BadgeStruct(scene_type=BadgeSceneType.FANS, display_type=BadgeDisplayType.COMBINE,
                        privilege_log_extra=PrivilegeLogExtra(level="7", privilege_id="7155001",
                                                              privilege_order_id="order_99887766"),
                        combine=CombineBadgeStruct(str="Club Girasol", icon=image(),
                                                   text=BadgeText(key="pm_mt_badge_name", pieces=["Girasol"]))),
            BadgeStruct(scene_type=BadgeSceneType.USER_GRADE, display_type=BadgeDisplayType.COMBINE,
                        combine=CombineBadgeStruct(str="23", icon=image())),
        ],
        border=BorderInfo(icon=image(), name="Marco Marisol",
                          border_privilege_log_extra=PrivilegeLogExtra(level="3", privilege_id="7155001")),
        user_vip_info=b"vip-blob Marisol")
    msg = WebcastChatMessage(
        user=user, content="hola chicas, soy Toñita",
        public_area_common=PublicAreaCommon(user_label=image(), user_consume_in_room=150),
        public_area_message_common=PublicAreaMessageCommon(portrait_info=PortraitInfo(
            user_metrics=[UserMetrics(type=MT.USER_METRICS_TYPE_GRADE, metrics_value="23"),
                          UserMetrics(type=MT.USER_METRICS_TYPE_FANS_CLUB, metrics_value="7"),
                          UserMetrics(type=MT.USER_METRICS_TYPE_TOP_VIEWER, metrics_value="Top 3 Marisol")],
            portrait_tag=[PortraitTag(tag_id="tag_marisol_77", show_value="Marisol")])))
    ev = events.CommentEvent().parse(bytes(msg))
    assert type(ev.user) is User                    # 解析出来是普通 User（文件头这么写的）

    stats = FieldStats()
    stats.observe(ev)
    stats.observe(events.CommentEvent().parse(bytes(WebcastChatMessage(content="x"))))   # 没有 user 的
    line = stats.take()
    assert line["n"] == 2 and line["has"]["fans_club"] == 1
    has = line["level_has"]
    assert set(has) == set(cl.HAS_KEYS)
    assert {k for k, v in has.items() if v} == {
        "pay_grade", "pay_grade_any", "fans_club_any", "badge_list", "badge_level", "border", "user_vip_info",
        "user_label", "consume_in_room", "user_metrics"}
    assert all(v == 1 for v in has.values() if v)
    hist = line["level_hist"]
    assert hist["badge:FANS"] == {"5-9": 1} and hist["badge:USER_GRADE"] == {"20-29": 1}
    assert hist["pay_grade"] == {"20-29": 1} and hist["fans_club"] == {"5-9": 1}
    assert hist["pay_grade_icon"] == {"20-29": 1} and hist["pay_grade_icon_count"] == {"1": 1}
    assert hist["border_log"] == {"2-4": 1} and hist["badge_count"] == {"2-4": 1}
    assert hist["metrics:USER_METRICS_TYPE_GRADE"] == {"20-29": 1}
    assert hist["metrics:USER_METRICS_TYPE_FANS_CLUB"] == {"5-9": 1}
    assert "metrics:USER_METRICS_TYPE_TOP_VIEWER" not in hist
    kinds = line["level_kinds"]
    assert kinds["badge_scene"] == {"FANS": 1, "USER_GRADE": 1}
    assert kinds["badge_level_src"] == {"FANS:log": 1, "USER_GRADE:str": 1}
    assert kinds["fans_club_status"] == {"ACTIVE": 1}
    assert kinds["user_metrics_type"] == {"USER_METRICS_TYPE_GRADE": 1, "USER_METRICS_TYPE_FANS_CLUB": 1,
                                          "USER_METRICS_TYPE_TOP_VIEWER": 1}
    assert kinds["user_metrics_value"] == {"USER_METRICS_TYPE_GRADE:digits": 1,
                                           "USER_METRICS_TYPE_FANS_CLUB:digits": 1,
                                           "USER_METRICS_TYPE_TOP_VIEWER:text": 1}
    dumped = json.dumps(line, ensure_ascii=False)
    for secret in REAL_SECRETS:
        assert secret not in dumped, secret
    for path, value in leaves({k: line[k] for k in cl.LEVEL_FIELDS}):
        assert isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= line["n"], (path, value)
