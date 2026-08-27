"""审计日志必须能回答「这一场、这一条，到底是哪个引擎翻的」。

起因是 2026-08-26 的排查：settings 里写着 deepl，实际整场跑的是本地 1.8B，
而日志里没有任何一个字段能说明这一点，只能靠延迟指纹（截距 21ms、19ms/字）
反推。做引擎对比实验之前，这个问题必须从「猜」变成「读一行」。
"""
import asyncio
import json
import time

from app.audit import AuditLog
from app.telemetry import Telemetry


def _rows(path):
    return [json.loads(line) for line in open(path, encoding="utf-8")]


def test_session_start_carries_runtime_provenance(tmp_path):
    log = AuditLog(room_url="https://www.tiktok.com/@real/live", log_dir=tmp_path,
                   extra={"app_version": "0.14.2",
                          "streamer": "real",
                          "source_requested": "es", "source_active": "es",
                          "translator_requested": "deepl",
                          "translator_active": "hymt2",
                          "profile_hash": None})
    log.close()
    start = _rows(log.path)[0]
    # requested 与 active 并排——0826 正是这两者不一致而无从发现
    assert start["translator_requested"] == "deepl"
    assert start["translator_active"] == "hymt2"
    assert start["source_active"] == "es"
    assert start["app_version"] == "0.14.2"
    assert start["profile_hash"] is None          # profile 未落地时的占位
    assert start["code_commit"]                   # 原有字段不受 extra 影响


def test_extra_cannot_overwrite_core_fields(tmp_path):
    """extra 撞上骨架字段名时骨架赢——审计核心不能被手滑的键名静默改写。"""
    log = AuditLog(room_url="https://www.tiktok.com/@real/live", log_dir=tmp_path,
                   extra={"room_url": "https://evil/live", "code_commit": "fake"})
    log.close()
    start = _rows(log.path)[0]
    assert start["room_url"] == "https://www.tiktok.com/@real/live"
    assert start["code_commit"] != "fake"


def test_translation_records_the_engine(tmp_path):
    log = AuditLog(room_url="https://www.tiktok.com/@real/live", log_dir=tmp_path)
    log.translation(7, "你好", 320.0, True, engine="deepl")
    log.translation(8, None, 10000.0, False)      # 旧调用形态也不能炸
    log.close()
    rows = [r for r in _rows(log.path) if r["type"] == "translation"]
    assert rows[0]["engine"] == "deepl"
    assert rows[1]["engine"] is None


def test_fast_translation_path_passes_its_engine(tmp_path):
    """管线的快译路径要把**当时用的**引擎写进日志。"""
    from app.pipeline import Pipeline

    class FakeServer:
        config = {}

        async def broadcast(self, msg):
            pass

    class Tr:
        name = "deepl"

        async def translate(self, *a, **k):
            return "你好"

    p = Pipeline.__new__(Pipeline)
    p.server = FakeServer()
    p.glossary = None
    p.telemetry = Telemetry()
    p.translator = Tr()
    p._quality = {}
    p.audit = AuditLog(room_url="https://www.tiktok.com/@real/live",
                       log_dir=tmp_path)
    loop = asyncio.get_event_loop_policy().new_event_loop()
    try:
        loop.run_until_complete(p._translate_and_update(
            {"id": 1, "text": "hola", "lang": "es", "target": "zh-CN",
             "audio_end_ts": time.time()}))
    finally:
        loop.close()
    p.audit.close()
    row = [r for r in _rows(p.audit.path) if r["type"] == "translation"][0]
    assert row["engine"] == "deepl"
    assert row["ok"] is True
