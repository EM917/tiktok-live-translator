"""settings.json 读写：界面偏好与组件状态的小型持久化。

只依赖标准库——启动自举阶段（依赖还没装齐时）也要能安全导入。
"""
import json
import os
from pathlib import Path

SETTINGS_FILE = Path(__file__).resolve().parent.parent / "settings.json"


def load_settings():
    """读出全部设置。文件缺失/损坏/不是对象都静默返回空 dict。"""
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


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


def save_setting(key, value):
    """写入单个设置项（读-合并-原子替换）。写失败静默忽略——
    持久化是锦上添花，不能因为磁盘/权限问题影响主流程。"""
    data = load_settings()
    data[key] = value
    try:
        tmp = SETTINGS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, SETTINGS_FILE)
    except OSError:
        pass
