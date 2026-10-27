"""弹幕作者的等级 / 徽章候选字段：只数、不显示（2026-10-21 用户定：先只记录量 1～2 场，再决定显不显示）。

已有的 `has.fans_club`（user.fans_club.data.level）5 场约 2400 条弹幕次次是 0，别的等级、徽章字段从没看过。
这里把能放账号等级或徽章的字段都数一遍：子进程（comment_worker.FieldStats）每条弹幕调一次
LevelStats.observe()，和原有计数一起每 50 条、断开时吐一次增量；父进程（CommentSource._add_field_stats）
用 merge() 累加，收尾时 snapshot() 写进 session_end.comment_fields 的三个新键：

    level_has    {候选: 带它的弹幕条数}            每次都带全部 24 个候选（HAS_KEYS），0 = 量过、这一场没有
    level_hist   {候选: {分桶: 条数}}             等级数值分桶（BUCKETS），只有数到过的候选才有
    level_kinds  {枚举字段: {枚举名或数值: 条数}}  徽章场景、显示类型这类枚举，只有数到过的才有

计数都按「条弹幕」算（一条弹幕带两枚同场景的徽章只算 1），所以每个数都不超过 n。

**只记数字和枚举名。** 昵称、id、徽章文字（粉丝团名、徽章上的字可能带人名或主播名）、弹幕内容一律不记；
从字符串里取等级只认 1～6 位纯数字（19 位的账号 id 进不来）。枚举名来自库里的枚举定义，不是网络数据；
认不出的枚举值记数值。

字段路径对照本机装的 TikTokLive 7.0.1 + TikTokLiveProto 0.2.2（v3 proto；CommentEvent 继承
webcast.model.message.WebcastChatMessage；CommentEvent().parse() 解析出来的 user 是普通的
webcast.model.base.user.User，不是库里的 ExtendedUser）：

    pay_grade          user.pay_grade.level（UserHonor，送礼等级）
    pay_grade_any      user.pay_grade 里任何一项有值：level / name / score / grade_icon_list / 各图标
                       （grade_icon_list[].level 另在 level_hist.pay_grade_icon 分档，见下）
    fans_club_any      user.fans_club.data 里任何一项有值：level / club_name / user_fans_club_status / badge
                       （等级本身仍是原有的 has.fans_club，分桶在 level_hist.fans_club）
    fans_club_prefer   user.fans_club.prefer_data（map<int, FansClubData>，另一条嵌套路径）非空
    fans_club_info     user.fans_club_info.fans_level（FansClubInfo，User 第 66 号字段）
    fans_club_info_any user.fans_club_info 里任何一项有值：fans_level / fans_score / is_sleeping /
                       fans_club_name / badge
    badge_list         user.badge_list（list[BadgeStruct]；v2 叫 badges）非空
    badge_level        至少一枚徽章读得出等级数：privilege_log_extra.level（字符串；v2 叫 log_extra）、
                       combine.str 或 str.str 是纯数字、combine.text.pieces 或 text.pieces 里有纯数字
    subscribe          user.is_subscribe、user.subscribe_info.is_subscribe 或 .is_subscribed_to_anchor
    subscribe_badge    user.subscribe_info.badge 有图（origin_img / preview_img）
    top_vip_no         user.top_vip_no（榜单名次）
    pay_score          user.pay_score 或 user.pay_scores
    border             user.border / user.border_list（BorderInfo，有 level、border_privilege_log_extra.level
                       或图标）
    badge_images       user.badge_image_list / user.media_badge_image_list 有图
    real_time_icons    user.real_time_icons / user.new_real_time_icons 有图
    combo_badge        user.combo_badge_info（combo_count 或图标）
    medal              user.medal 有图
    exp                user.exp
    anchor_level       user.webcast_anchor_level.level / user.anchor_level.level（主播等级）
    user_role          user.user_role 不是 0
    user_vip_info      user.user_vip_info（bytes，库里没解成消息）非空；只数有没有，内容不碰
    user_label         ev.public_area_common.user_label 有图（公屏上名字旁的标签）
    consume_in_room    ev.public_area_common.user_consume_in_room（本场消费）
    user_metrics       ev.public_area_message_common.portrait_info.user_metrics 非空（UserMetrics：
                       type 枚举 USER_METRICS_TYPE_GRADE / SUBSCRIBE / FOLLOW / FANS_CLUB / TOP_VIEWER / GIFT，
                       metrics_value 是字符串）

level_kinds 里的枚举：badge_scene（BadgeStruct.scene_type：ADMIN、SUBSCRIBER、USER_GRADE、FANS……）、
badge_display（display_type：IMAGE / TEXT / STRING / COMBINE）、badge_exhibition、badge_priority、
badge_level_src（「场景:来源」，徽章等级在哪个场景的徽章上、从哪读到的：FANS:log、USER_GRADE:str……，
来源是 log / str / pieces）、fans_club_status（data.user_fans_club_status）、user_role（数值）、
user_metrics_type（UserMetrics.type 的枚举名）、user_metrics_value（「type:形状」，metrics_value 长什么样：
digits = 1～6 位纯数字、text = 别的字、empty = 空；原文不记）。
level_hist 里 badge:<场景> 是那个场景徽章的等级，badge_count 是一条弹幕带几枚徽章，metrics:<type> 是那类
user_metrics 的 metrics_value 读出的数，pay_grade_icon 是 grade_icon_list[].level（或 level_str）里最大的，
pay_grade_icon_count 是 grade_icon_list 有几项（是本人的图标还是整张等级图标表没验证过，靠它分辨），
border_log 是 border_privilege_log_extra.level。

TikTokLive 7.0.1 自带的 ExtendedUser.member_level / gifter_level 走的是 v2 的名字
（badge.badge_scene、badge.log_extra），v3 的 BadgeStruct 叫 scene_type、privilege_log_extra，
所以这两个帮手在 7.0.1 上取不到值。这里直接读 v3 的字段，v2 的名字留作备选。

已用真 proto 往返验证过（TikTokLive 7.0.1 / TikTokLiveProto 0.2.2，本机 .venv，不联网）：用真 v3
WebcastChatMessage 造带 FANS / USER_GRADE 徽章、user_metrics 等的消息，bytes() 序列化后
CommentEvent().parse() 解析，再过 FieldStats，计数对、输出里没有昵称、id、团名、徽章文字、弹幕内容。钉在
tests/test_comment_levels.py 的 test_real_v3_proto_round_trip（没装 TikTokLive 的环境会跳过，比如 CI）。
同一个对象套上库自带的 ExtendedUser.from_user() 后 member_level / gifter_level 都是 None（2026-10-21 本机
跑过一次，没钉进测试：库升级修好了不该让测试变红）。

本文件不 import TikTokLive（父进程和 3.9 的测试也要用），只做鸭子类型的属性访问；
每个候选单独兜异常，哪个字段长得不对都只少数它自己，不影响别的、更不影响弹幕本身。
"""
import re

LEVEL_FIELDS = ("level_has", "level_hist", "level_kinds")

HAS_KEYS = ("pay_grade", "pay_grade_any", "fans_club_any", "fans_club_prefer", "fans_club_info",
            "fans_club_info_any", "badge_list", "badge_level", "subscribe", "subscribe_badge",
            "top_vip_no", "pay_score", "border", "badge_images", "real_time_icons", "combo_badge",
            "medal", "exp", "anchor_level", "user_role", "user_vip_info", "user_label", "consume_in_room",
            "user_metrics")

# 等级分桶：从大到小比下界。0 和负数不进桶（betterproto 没设的 int 是 0）
BUCKETS = ((1000, "1000+"), (100, "100-999"), (50, "50-99"), (30, "30-49"), (20, "20-29"),
           (10, "10-19"), (5, "5-9"), (2, "2-4"), (1, "1"))

MAX_TABLES = 48     # level_hist / level_kinds 各最多几张表（badge:<场景>、metrics:<type> 按名字开表）
MAX_LABELS = 24     # 一张表最多几种名字，多出来的并进 "other"
MAX_BADGES = 16     # 一条弹幕最多看几枚徽章 / 几项 user_metrics / 几个头像框
MAX_PIECES = 8      # 徽章文字的 pieces 最多看几段
MAX_ICONS = 64      # grade_icon_list 最多看几项（可能是整张等级图标表，取最大等级要看全）

_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,47}\Z")          # 枚举名
_KEY = re.compile(r"[A-Za-z0-9_:+-]{1,64}\Z")          # 父进程认的键名 / 桶名
_DIGITS = re.compile(r"[0-9]{1,6}\Z")

_HONOR_ICONS = ("icon", "diamond_icon", "im_icon", "im_icon_with_level", "new_im_icon_with_level",
                "live_icon", "new_live_icon")


def bucket(n):
    """正整数 -> 桶名；不是正整数返回 None。"""
    if isinstance(n, bool) or not isinstance(n, int):
        return None
    for low, name in BUCKETS:
        if n >= low:
            return name
    return None


def level_of(v):
    """等级数：正整数（bool 不算），或 1～6 位纯数字的字符串；其它一律 0。"""
    if isinstance(v, bool):
        return 0
    if isinstance(v, int):
        return int(v) if v > 0 else 0
    if isinstance(v, str):
        v = v.strip()
        if _DIGITS.match(v):
            return int(v)
    return 0


def enum_name(v):
    """枚举值 -> 名字（betterproto2 的枚举是 IntEnum，认不出的值名字是空串，记数值）；不是整数返回 None。"""
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    name = getattr(v, "name", None)
    if isinstance(name, str) and _NAME.match(name):
        return name
    return str(int(v))


def _seq(v):
    """repeated 字段：只认 list / tuple，别的当空。"""
    return v if isinstance(v, (list, tuple)) else ()


def _text(v):
    return isinstance(v, str) and bool(v.strip())


def _image(img):
    """ImageModel 有没有图：url_list 里有非空串，或 uri 非空。"""
    if img is None:
        return False
    if any(_text(u) for u in _seq(getattr(img, "url_list", None))[:4]):
        return True
    return _text(getattr(img, "uri", None))


def _first(obj, *names):
    """按顺序取第一个不是 None 的属性（v3 名字在前，v2 名字备选）。"""
    for name in names:
        v = getattr(obj, name, None)
        if v is not None:
            return v
    return None


def badge_level(badge):
    """一枚徽章上的 (等级, 来源)：privilege_log_extra.level -> "log"；combine.str / str.str 是纯数字 -> "str"；
    combine.text.pieces / text.pieces 里第一段纯数字 -> "pieces"。都没有返回 (0, None)。"""
    lv = level_of(getattr(_first(badge, "privilege_log_extra", "log_extra"), "level", None))
    if lv:
        return lv, "log"
    combine = getattr(badge, "combine", None)
    for v in (getattr(combine, "str", None), getattr(getattr(badge, "str", None), "str", None)):
        lv = level_of(v)
        if lv:
            return lv, "str"
    for text in (getattr(combine, "text", None), getattr(badge, "text", None)):
        for piece in _seq(getattr(text, "pieces", None))[:MAX_PIECES]:
            lv = level_of(piece)
            if lv:
                return lv, "pieces"
    return 0, None


class LevelStats:
    """子进程侧：上次 take() 以来每个候选的条数、等级分桶、枚举计数。弹幕条数 n 由 FieldStats 数。"""

    def __init__(self):
        self._reset()

    def _reset(self):
        self.has = dict.fromkeys(HAS_KEYS, 0)
        self.hist = {}
        self.kinds = {}

    def hit(self, key):
        self.has[key] += 1

    def level(self, key, value):
        """level_hist[key] 里 value 所在的桶 +1；value 不是正整数不记。"""
        name = bucket(value)
        if name is not None:
            _bump(self.hist, key, name)

    def kind(self, field, name):
        if name is not None:
            _bump(self.kinds, field, name)

    def observe(self, ev):
        """数一条弹幕。永不抛：每个候选单独兜异常，读坏一个只少数它自己。"""
        try:
            user = getattr(ev, "user", None)
        except Exception:
            user = None
        for probe in _PROBES:
            try:
                probe(self, ev, user)
            except Exception:
                pass

    def take(self):
        """增量（三个 level_* 键），吐完清零。由 FieldStats.take() 在 n > 0 时调。"""
        out = {"level_has": dict(self.has),
               "level_hist": {k: dict(v) for k, v in self.hist.items()},
               "level_kinds": {k: dict(v) for k, v in self.kinds.items()}}
        self._reset()
        return out


def _bump(tables, key, label):
    table = tables.get(key)
    if table is None:
        if len(tables) >= MAX_TABLES:
            return
        table = tables[key] = {}
    if label not in table and len(table) >= MAX_LABELS:
        label = "other"
    table[label] = table.get(label, 0) + 1


# ---- 各候选（每个单独兜异常，见 LevelStats.observe） ----------------------------------------

def _pay_grade(st, ev, user):
    honor = getattr(user, "pay_grade", None)
    if honor is None:
        return
    lv = level_of(getattr(honor, "level", None))
    if lv:
        st.hit("pay_grade")
        st.level("pay_grade", lv)
    if (lv or _text(getattr(honor, "name", None)) or level_of(getattr(honor, "score", None))
            or _seq(getattr(honor, "grade_icon_list", None))
            or any(_image(getattr(honor, f, None)) for f in _HONOR_ICONS)):
        st.hit("pay_grade_any")


def _pay_grade_icons(st, ev, user):
    """user.pay_grade.grade_icon_list（GradeIcon）：最大等级（level，没有再看 level_str）和有几项分档。
    和 _pay_grade 分开兜异常：送礼等级那边读坏了不连累这里，反过来也一样。"""
    icons = _seq(getattr(getattr(user, "pay_grade", None), "grade_icon_list", None))
    if not icons:
        return
    st.level("pay_grade_icon_count", len(icons))
    best = 0
    for icon in icons[:MAX_ICONS]:
        if icon is None:
            continue
        try:                            # 一项长得不对，只少看这一项
            best = max(best, level_of(getattr(icon, "level", None))
                       or level_of(getattr(icon, "level_str", None)))
        except Exception:
            pass
    st.level("pay_grade_icon", best)


def _user_badge(badge):
    """FansClubData.badge（UserBadge：icons map + title）有没有内容。"""
    if badge is None:
        return False
    icons = getattr(badge, "icons", None)
    return (isinstance(icons, dict) and bool(icons)) or _text(getattr(badge, "title", None))


def _fans_club(st, ev, user):
    member = getattr(user, "fans_club", None)
    if member is None:
        return
    data = getattr(member, "data", None)
    if data is not None:
        lv = level_of(getattr(data, "level", None))
        st.level("fans_club", lv)
        raw_status = getattr(data, "user_fans_club_status", None)
        st.kind("fans_club_status", enum_name(raw_status))
        status_set = level_of(raw_status) > 0
        if lv or status_set or _text(getattr(data, "club_name", None)) \
                or _user_badge(getattr(data, "badge", None)):
            st.hit("fans_club_any")
    prefer = getattr(member, "prefer_data", None)
    if isinstance(prefer, dict) and prefer:
        st.hit("fans_club_prefer")
        best = max((level_of(getattr(d, "level", None)) for d in list(prefer.values())[:MAX_BADGES]),
                   default=0)
        st.level("fans_club_prefer", best)


def _fans_club_info(st, ev, user):
    info = getattr(user, "fans_club_info", None)
    if info is None:
        return
    lv = level_of(getattr(info, "fans_level", None))
    if lv:
        st.hit("fans_club_info")
        st.level("fans_club_info", lv)
    if (lv or getattr(info, "is_sleeping", None) is True or level_of(getattr(info, "fans_score", None))
            or _text(getattr(info, "fans_club_name", None)) or _image(getattr(info, "badge", None))):
        st.hit("fans_club_info_any")


def _badges(st, ev, user):
    badges = _seq(_first(user, "badge_list", "badges"))
    if not badges:
        return
    st.hit("badge_list")
    st.level("badge_count", len(badges))
    kinds = {"badge_scene": set(), "badge_display": set(), "badge_exhibition": set(),
             "badge_priority": set(), "badge_level_src": set()}
    levels = {}
    for badge in badges[:MAX_BADGES]:
        if badge is None:
            continue
        try:                            # 一枚徽章长得不对，只少数这一枚
            _one_badge(badge, kinds, levels)
        except Exception:
            pass
    for field, names in kinds.items():
        for name in names:
            st.kind(field, name)
    if levels:
        st.hit("badge_level")
        for scene, lv in levels.items():
            st.level("badge:" + scene, lv)


def _one_badge(badge, kinds, levels):
    scene = enum_name(_first(badge, "scene_type", "badge_scene"))
    found = {"badge_scene": scene,
             "badge_display": enum_name(getattr(badge, "display_type", None)),
             "badge_exhibition": enum_name(getattr(badge, "exhibition_type", None)),
             "badge_priority": enum_name(getattr(badge, "priority_type", None))}
    lv, src = badge_level(badge)
    for field, name in found.items():
        if name is not None:
            kinds[field].add(name)
    if lv:
        key = scene if scene is not None else "none"
        levels[key] = max(levels.get(key, 0), lv)
        kinds["badge_level_src"].add(key + ":" + src)     # 按场景分开记：FANS:log、USER_GRADE:str……


def _subscribe(st, ev, user):
    info = getattr(user, "subscribe_info", None)
    if (getattr(user, "is_subscribe", None) is True or getattr(info, "is_subscribe", None) is True
            or getattr(info, "is_subscribed_to_anchor", None) is True):
        st.hit("subscribe")
    badge = getattr(info, "badge", None)
    if badge is not None and (_image(getattr(badge, "origin_img", None))
                              or _image(getattr(badge, "preview_img", None))):
        st.hit("subscribe_badge")


def _top_vip(st, ev, user):
    rank = level_of(getattr(user, "top_vip_no", None))
    if rank:
        st.hit("top_vip_no")
        st.level("top_vip_no", rank)


def _pay_score(st, ev, user):
    if level_of(getattr(user, "pay_score", None)) or level_of(getattr(user, "pay_scores", None)):
        st.hit("pay_score")


def _border(st, ev, user):
    """头像框：level 和 border_privilege_log_extra.level（字符串）各自分档，各自兜异常。"""
    listed = _seq(getattr(user, "border_list", None))
    seen, best, best_log = bool(listed), 0, 0
    for border in [getattr(user, "border", None)] + list(listed[:MAX_BADGES]):
        if border is None:
            continue
        try:
            lv = level_of(getattr(border, "level", None))
            best = max(best, lv)
            seen = seen or bool(lv) or _image(getattr(border, "icon", None))
        except Exception:
            pass
        try:
            lv = level_of(getattr(getattr(border, "border_privilege_log_extra", None), "level", None))
            best_log = max(best_log, lv)
            seen = seen or bool(lv)
        except Exception:
            pass
    if seen:
        st.hit("border")
    st.level("border", best)
    st.level("border_log", best_log)


def _images(st, ev, user):
    for key, fields in (("badge_images", ("badge_image_list", "media_badge_image_list")),
                        ("real_time_icons", ("real_time_icons", "new_real_time_icons"))):
        if any(_image(img) for f in fields for img in _seq(getattr(user, f, None))[:MAX_BADGES]):
            st.hit(key)
    combo = getattr(user, "combo_badge_info", None)
    if combo is not None and (level_of(getattr(combo, "combo_count", None))
                              or _image(getattr(combo, "icon", None))):
        st.hit("combo_badge")
    if _image(getattr(user, "medal", None)):
        st.hit("medal")


def _exp_and_roles(st, ev, user):
    if level_of(getattr(user, "exp", None)):
        st.hit("exp")
    anchor = max(level_of(getattr(getattr(user, "webcast_anchor_level", None), "level", None)),
                 level_of(getattr(getattr(user, "anchor_level", None), "level", None)))
    if anchor:
        st.hit("anchor_level")
        st.level("anchor_level", anchor)
    role = getattr(user, "user_role", None)
    if isinstance(role, int) and not isinstance(role, bool) and role != 0:
        st.hit("user_role")
        st.kind("user_role", enum_name(role))


def _vip_info(st, ev, user):
    """user.user_vip_info 是 bytes（库里没解成消息）：只数非空，内容一个字节都不碰。"""
    raw = getattr(user, "user_vip_info", None)
    if isinstance(raw, (bytes, bytearray)) and raw:
        st.hit("user_vip_info")


def _public_area(st, ev, user):
    area = getattr(ev, "public_area_common", None)
    if area is None:
        return
    if _image(getattr(area, "user_label", None)):
        st.hit("user_label")
    spent = level_of(getattr(area, "user_consume_in_room", None))
    if spent:
        st.hit("consume_in_room")
        st.level("consume_in_room", spent)


def _metric_shape(value):
    """metrics_value 长什么样：digits（1～6 位纯数字）/ text（别的字）/ empty。原文不出这个函数。"""
    if not isinstance(value, str) or not value.strip():
        return "empty"
    return "digits" if _DIGITS.match(value.strip()) else "text"


def _user_metrics(st, ev, user):
    """ev.public_area_message_common.portrait_info.user_metrics（UserMetrics：type 枚举 + metrics_value 字符串）。
    记 type 的枚举名（user_metrics_type）、「type:形状」（user_metrics_value）、按 type 分档的数
    （level_hist 的 metrics:<type>）。同一条弹幕同一 type 只算 1、取高；metrics_value 原文不记。"""
    common = getattr(ev, "public_area_message_common", None)
    metrics = _seq(getattr(getattr(common, "portrait_info", None), "user_metrics", None))
    if not metrics:
        return
    st.hit("user_metrics")
    types, shapes, levels = set(), set(), {}
    for metric in metrics[:MAX_BADGES]:
        if metric is None:
            continue
        try:                            # 一项长得不对，只少数这一项
            name = enum_name(getattr(metric, "type", None))
            key = name if name is not None else "none"
            value = getattr(metric, "metrics_value", None)
            shape, lv = _metric_shape(value), level_of(value)
        except Exception:
            continue
        if name is not None:
            types.add(name)
        shapes.add(key + ":" + shape)
        if lv:
            levels[key] = max(levels.get(key, 0), lv)
    for name in types:
        st.kind("user_metrics_type", name)
    for name in shapes:
        st.kind("user_metrics_value", name)
    for key, lv in levels.items():
        st.level("metrics:" + key, lv)


_PROBES = (_pay_grade, _pay_grade_icons, _fans_club, _fans_club_info, _badges, _subscribe, _top_vip,
           _pay_score, _border, _images, _exp_and_roles, _vip_info, _public_area, _user_metrics)


# ---- 父进程侧：累加、收尾拷贝 ----------------------------------------------------------------

def _count(v):
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _key(v):
    return isinstance(v, str) and bool(_KEY.match(v))


def merge(totals, obj, n):
    """把子进程一行里的 level_* 并进累计（totals 就是 CommentSource.comment_fields）。
    只认短键名、非负整数，单个数按这一行的 n 封顶；形状不对的整块或整项丢掉，不抛。
    并进了任何东西返回 True（父进程据此决定打不打终端那行）。"""
    merged = False
    has = obj.get("level_has")
    if isinstance(has, dict):
        for key, value in list(has.items())[:len(HAS_KEYS) * 2]:
            if _key(key) and _count(value):
                dst = totals.setdefault("level_has", {})
                dst[key] = dst.get(key, 0) + min(value, n)
                merged = True
    for field in ("level_hist", "level_kinds"):
        tables = obj.get(field)
        if not isinstance(tables, dict):
            continue
        for name, table in list(tables.items())[:MAX_TABLES]:
            if not _key(name) or not isinstance(table, dict):
                continue
            for label, value in list(table.items())[:MAX_LABELS + 1]:
                if not _key(label) or not _count(value):
                    continue
                dst_tables = totals.setdefault(field, {})
                dst = dst_tables.get(name)
                if dst is None:
                    if len(dst_tables) >= MAX_TABLES:
                        break
                    dst = dst_tables[name] = {}
                if label not in dst and len(dst) >= MAX_LABELS:
                    label = "other"
                dst[label] = dst.get(label, 0) + min(value, n)
                merged = True
    return merged


def snapshot(fields):
    """session_end.comment_fields 要并进去的 level_* 拷贝（两层都拷，写盘时不和累计共用对象）；没有的键不带。"""
    out = {}
    if not isinstance(fields, dict):
        return out
    has = fields.get("level_has")
    if isinstance(has, dict) and has:
        out["level_has"] = dict(has)
    for field in ("level_hist", "level_kinds"):
        tables = fields.get(field)
        if isinstance(tables, dict) and tables:
            out[field] = {k: dict(v) for k, v in tables.items() if isinstance(v, dict)}
    return out
