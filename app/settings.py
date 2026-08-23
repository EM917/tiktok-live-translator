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
