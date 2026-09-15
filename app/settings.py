"""settings.json 读写：界面偏好与组件状态的小型持久化。

只依赖标准库——启动自举阶段（依赖还没装齐时）也要能安全导入。
"""
import json
import os
from datetime import datetime
from pathlib import Path

SETTINGS_FILE = Path(__file__).resolve().parent.parent / "settings.json"

# 本次运行里发现 settings.json 内容损坏时备份成的文件名，以及界面提示是否已发过
_corrupt = {"backup": None, "announced": False}


def load_settings():
    """读出全部设置。文件缺失、读不了、或是合法 JSON 但不是对象时返回空 dict。

    解析不了（不是合法 JSON、编码认不出、0 字节）时，先把文件改名备份成
    settings.json.corrupt-<时间>，再返回空 dict。以前直接返回 {}：启动几秒内
    必然有一次 save_setting（更新器记时间戳、开播记房间）在这个 {} 上合并写回，
    DeepL 等密钥、引擎选择、最近直播间被静默抹掉，连原文件都不剩。
    改名而不是复制：之后的读取看到的是「没有文件」，不会每读一次备份一份。"""
    try:
        raw = SETTINGS_FILE.read_bytes()
    except OSError:
        return {}
    try:
        # 整段字节交给 json，由它认编码：带 BOM 的 UTF-8（PowerShell 5.1 的
        # Set-Content -Encoding UTF8、老版记事本）和 UTF-16（PowerShell 5.1 的 > 或
        # Out-File、记事本存成「Unicode」）都读得出来。手改粘密钥正是这样存出来的，
        # 合法的文件不能被当成损坏挪走
        data = json.loads(raw)
    except Exception:            # JSONDecodeError、UnicodeDecodeError……
        _backup_corrupt(raw)
        return {}
    return data if isinstance(data, dict) else {}


def _backup_corrupt(raw):
    try:
        if SETTINGS_FILE.read_bytes() != raw:
            return               # 读完之后文件已被别处改写或备份过：别把新文件当坏的挪走
    except OSError:
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for n in range(1, 100):
        backup = SETTINGS_FILE.with_name("{}.corrupt-{}{}".format(
            SETTINGS_FILE.name, stamp, "" if n == 1 else "-{}".format(n)))
        if backup.exists():
            continue
        try:
            os.replace(str(SETTINGS_FILE), str(backup))
        except OSError:          # 含 FileNotFoundError：并发的另一次读取刚改过名
            return
        if _corrupt["backup"] is None:
            _corrupt["backup"] = backup.name
        print("[警告] settings.json 内容损坏，已备份为 {}".format(backup.name))
        return


def corrupt_backup_name():
    """本次运行里 settings.json 被判定损坏后备份成的文件名；没发生过是 None。"""
    return _corrupt["backup"]


def take_corrupt_notice():
    """同上，但每次运行只交出一次：界面提示发一遍，不是每读一次设置发一遍。"""
    if _corrupt["backup"] is None or _corrupt["announced"]:
        return None
    _corrupt["announced"] = True
    return _corrupt["backup"]


DEFAULT_SOURCE_LANG = "es"


def resolve_source(cli_value, saved):
    """启动时决定主播语言：CLI 显式指定 > 界面上次的选择 > 西语。

    最后一档是 es 而不是 auto：这个产品就是给西语带货直播做合规监听的，
    首次使用不该让每个 2.5 秒片段都重新猜一次语言——实测 auto 档一场里
    22.7% 的段被打上非西语标签（en/pt/hi/tr…），纯标点垃圾字幕和按错误
    语言翻出来的译文全从这里来。真要逐段自动检测的，在界面里选「自动检测」，
    这个选择会被记住（返回 None 即自动检测）。
    """
    if cli_value:
        # "auto" 要归一成 None——Whisper 的 language 参数只认语言码或 None
        return None if str(cli_value) == "auto" else cli_value
    s = str(saved or "").strip()
    if s == "auto":
        return None               # 用户明确选过自动检测，尊重它
    return s[:12] if s else DEFAULT_SOURCE_LANG


# 首页「最近直播间」最多留几个。够一屏点选即可，多了首页反而乱。
RECENT_ROOMS_MAX = 8


def push_recent_room(streamer, url, limit=RECENT_ROOMS_MAX):
    """把一个刚打开的直播间记进「最近直播间」，返回更新后的列表（新的在前）。

    按主播名去重：同一个主播反复打开只留最近一条，不然列表几分钟就被
    同一个人刷满。只收有主播名的房间——直接粘 .flv 流地址没有主播身份，
    进了列表也只能显示一串地址，反而是给中控添乱。写失败静默忽略：
    这是锦上添花，不能拖累开播。
    """
    from datetime import datetime
    streamer = (streamer or "").strip()
    url = (url or "").strip()
    if not streamer or not url:
        return recent_rooms()
    data = load_settings()
    items = data.get("recent_rooms")
    items = [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []
    # 同名的旧记录先剔掉，再把这一条放到最前
    items = [x for x in items if x.get("streamer") != streamer]
    items.insert(0, {"streamer": streamer, "url": url[:500],
                     "at": datetime.now().isoformat(timespec="seconds")})
    items = items[:max(1, int(limit))]
    save_setting("recent_rooms", items)
    return items


def recent_rooms(limit=RECENT_ROOMS_MAX):
    """读「最近直播间」，过滤掉结构不对的条目，最多返回 limit 条。"""
    items = load_settings().get("recent_rooms")
    if not isinstance(items, list):
        return []
    clean = [{"streamer": x.get("streamer", ""), "url": x.get("url", ""),
              "at": x.get("at", "")}
             for x in items
             if isinstance(x, dict) and x.get("streamer") and x.get("url")]
    return clean[:max(1, int(limit))]


def save_setting(key, value):
    """写入单个设置项（读-合并-原子替换）。写失败静默忽略——
    持久化是锦上添花，不能因为磁盘/权限问题影响主流程。"""
    data = load_settings()
    data[key] = value
    try:
        tmp = SETTINGS_FILE.with_suffix(".json.tmp")
        with open(str(tmp), "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))
            f.flush()
            # 先落盘再替换：写完就断电时，不至于换上一个 0 字节的 settings.json
            os.fsync(f.fileno())
        os.replace(tmp, SETTINGS_FILE)
    except OSError:
        pass
