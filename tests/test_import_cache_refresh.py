"""刷新导入缓存是尽力而为的一步：它出错不能让读版本号、更不能让开播失败。"""
import importlib
import re
import threading
from pathlib import Path

from app import updater

ROOT = Path(__file__).resolve().parent.parent


def test_a_failing_cache_refresh_does_not_break_the_version_lookup(monkeypatch):
    """2026-09-17 Windows CI：两个线程同时 invalidate_caches，后到的抛 KeyError: '.'，
    调用栈在 _begin_session 里——真实程序里就是这一场开播以内部错误失败。"""
    def boom():
        raise KeyError(".")

    monkeypatch.setattr(importlib, "invalidate_caches", boom)
    updater._refresh_import_caches()                    # 不抛
    version = updater.tiktoklive_version()              # 不抛；装没装都行
    assert version is None or isinstance(version, str)


def test_concurrent_refreshes_are_serialized(monkeypatch):
    inside, overlaps = [], []

    def slow():
        inside.append(1)
        if len(inside) > 1:
            overlaps.append(1)
        threading.Event().wait(0.01)
        inside.pop()

    monkeypatch.setattr(importlib, "invalidate_caches", slow)
    threads = [threading.Thread(target=updater._refresh_import_caches) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert overlaps == []


def test_nothing_in_the_app_calls_the_stdlib_function_bare():
    """防回退：app/ 里只有 _refresh_import_caches 自己可以直接调标准库的那个函数。"""
    offenders = []
    for path in sorted((ROOT / "app").glob("*.py")) + [ROOT / "main.py"]:
        text = path.read_text(encoding="utf-8")
        calls = len(re.findall(r"importlib\.invalidate_caches\(\)", text))
        allowed = 1 if path.name == "updater.py" else 0
        if calls > allowed:
            offenders.append((path.name, calls))
    assert offenders == []
