"""弹幕留意的审计记录（spec §8）：只记计数、模型调用和闸门转折。

* 新记录可序列化，字段值只有代码和数字；
* 弹幕原文、昵称、账号 id 一律不进审计（这里造一场带着这些东西的会话，逐个检查）；
* 中英两种界面下审计逐字节相同（G8）；
* 现有的读取方（provenance.corpus、replay_alerts、retranslate_audit、build_annotation_queue）都按 type
  过滤，带新类型的会话文件照样读得出来、结果不变。

纯内存加临时目录；主播、昵称、账号都是虚构的。
"""
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import audit as audit_mod
from app import i18n, provenance
from app import pipeline as pipeline_mod
from app import resolver, settings
from app.asr import ASRResult
from app.audit import RETAIN_TYPES, AuditLog
from app.pipeline import Pipeline
from tests.guard_support import (RED_TEXT, UID, FakeSource, RecServer, audit_records, item, of,
                                 use_fixture_rules, virtual_mono, virtual_perf)
from tests.helpers import run

ROOM = "https://www.tiktok.com/@luna_demo/live"


def make_audit(tmp_path, **extra):
    return AuditLog(room_url=ROOM, log_dir=tmp_path / "logs", extra=extra)


def records_of(audit):
    return [json.loads(line) for line in Path(audit.path).read_text(encoding="utf-8").splitlines() if line]


def segment(audit, seq=1, text="hola amigos"):
    audit.segment(seq, ASRResult(text=text, language="es", raw_text=text, rejected=[]), 10.0 * seq, 120, [])


# ---- 新记录 --------------------------------------------------------------------------------

def test_review_and_gate_records_are_json_with_codes_and_numbers_only(tmp_path):
    audit = make_audit(tmp_path)
    audit.comment_guard_review("claude", "claude-haiku-5-5", n=3, ms=1840, outcome="ok", backlog_sec=0.2,
                               asr_inflight=False, waited_ms=300, in_tokens=420, out_tokens=60,
                               cache_read_tokens=0)
    audit.comment_guard_review("claude", "claude-haiku-5-5", n=2, ms=900, outcome="refusal",
                               refusal_category="general_harms")
    audit.comment_guard_gate("claude", "paused", reason="backlog", backlog_sec=11.5)
    audit.close(reason="user_stop")
    review1, review2, gate = [r for r in records_of(audit) if r["type"].startswith("comment_guard_")]
    assert review1["type"] == "comment_guard_review" and review1["outcome"] == "ok" and review1["n"] == 3
    assert review1["backend"] == "claude" and review1["model"] == "claude-haiku-5-5" and review1["ms"] == 1840
    assert review1["asr_inflight"] is False and review1["in_tokens"] == 420 and review1["cache_read_tokens"] == 0
    assert review2["outcome"] == "refusal" and review2["refusal_category"] == "general_harms"
    assert gate["type"] == "comment_guard_gate" and gate["state"] == "paused" and gate["reason"] == "backlog"
    assert gate["backlog_sec"] == 11.5 and gate["backend"] == "claude"
    for rec in (review1, review2, gate):
        assert rec["at"][:2] == "20"
        for value in rec.values():
            assert not isinstance(value, str) or value.isascii()         # 只有代码，没有界面文字


def test_the_new_record_types_are_not_retained_when_the_disk_is_full():
    """不进 RETAIN_TYPES：写不进去时只计数，不占内存（spec §8）。"""
    assert "comment_guard_review" not in RETAIN_TYPES and "comment_guard_gate" not in RETAIN_TYPES
    assert "comment_guard" not in RETAIN_TYPES


def test_no_audit_method_takes_comment_text_a_nickname_or_an_id():
    """新写入方法的参数里没有任何能装下弹幕内容的名字。"""
    import inspect

    for name in ("comment_guard_review", "comment_guard_gate"):
        params = set(inspect.signature(getattr(AuditLog, name)).parameters) - {"self"}
        assert not params & {"text", "comment", "user", "nick", "uid", "handle", "sec", "cid", "id", "evidence",
                             "why", "sample"}, params


# ---- 读取方忽略新类型 ----------------------------------------------------------------------------

@pytest.fixture
def session_with_guard_records(tmp_path):
    audit = make_audit(tmp_path, comment_guard={"enabled": True, "backend": "none", "rules_hash": "abc123def456",
                                                "rules_count": 510, "warnings": [], "model": None,
                                                "thresholds": {"accumulate": (2, 600)}})
    segment(audit, 1, "hola amigos")
    audit.comment_guard_review("claude", "claude-haiku-5-5", 1, 800, "ok")
    segment(audit, 2, "este producto cura el cancer")
    audit.comment_guard_gate("claude", "tripped", reason="backlog")
    segment(audit, 3, "gracias por venir")
    audit.close(reason="user_stop", comment_fields={"n": 3, "has": {"id": 3}},
                comment_guard={"counts": {"threat": {"alert": 1, "amber": 0, "note": 0}}, "observed": 5})
    return tmp_path / "logs", audit


def test_existing_readers_ignore_the_new_types(session_with_guard_records):
    from tools import build_annotation_queue, replay_alerts, retranslate_audit

    log_dir, audit = session_with_guard_records
    corpus = provenance.corpus(log_dir=log_dir)
    assert len(corpus) == 1 and corpus[0]["segments"] == 3 and corpus[0]["streamer"] == "luna_demo"
    rows = retranslate_audit.read_rows(audit.path)
    assert [r["type"] for r in rows if r["type"] == "segment"] == ["segment"] * 3
    assert {r["type"] for r in rows} >= {"comment_guard_review", "comment_guard_gate", "session_end"}

    class Detector:
        def reset_state(self):
            pass

        def scan(self, text, ts=0):
            return [{"term": "cura el cancer", "tier": "exact"}] if "cura el cancer" in text else []

    assert replay_alerts.session_hits(audit.path, Detector()) == [(2, "cura el cancer", "exact")]
    ctx = build_annotation_queue.load_contexts(log_dir)
    assert ctx[(Path(audit.path).name, 2)] == ("hola amigos", "gracias por venir")
    records = build_annotation_queue._read_records(audit.path)
    assert len(records) == len(rows)


def test_session_meta_ignores_the_new_session_start_key(session_with_guard_records):
    log_dir, audit = session_with_guard_records
    meta = provenance.session_meta(audit.path)
    assert meta["streamer"] == "luna_demo" and "comment_guard" not in meta


def test_session_start_and_end_blocks_survive_a_json_round_trip(session_with_guard_records):
    _, audit = session_with_guard_records
    records = records_of(audit)
    start = of(records, "session_start")[0]["comment_guard"]
    assert start["thresholds"] == {"accumulate": [2, 600]}            # 元组写成数组
    end = of(records, "session_end")[0]
    assert end["comment_fields"] == {"n": 3, "has": {"id": 3}}        # 原有的键还在
    assert end["comment_guard"]["counts"]["threat"]["alert"] == 1


# ---- 整场：弹幕里的东西不进审计；中英界面逐字节相同 ---------------------------------------------------------

def play_session(monkeypatch, tmp_path, lang):
    """跑一场真的开场-弹幕-收尾，返回审计记录。lang 是 "zh" 或 "en"（界面语言）。"""
    root = tmp_path / lang
    root.mkdir()
    monkeypatch.setattr(settings, "SETTINGS_FILE", root / "settings.json")
    (root / "settings.json").write_text(json.dumps({"comment_guard": {"enabled": True}}), encoding="utf-8")
    terms = root / "banned_terms.txt"
    terms.write_text("", encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms)
    monkeypatch.setattr(resolver, "_login_first_enabled", lambda: False)
    monkeypatch.setattr(audit_mod, "LOG_DIR", root / "logs")
    use_fixture_rules(monkeypatch, root)
    from app import comment_marks

    monkeypatch.setattr(comment_marks, "MARKED_DIR", root / "marked")
    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source="es", beam=5, context=False,
        asr_temperature=None, glossary=None, backend="auto", model=None, device="auto",
        compute_type="auto", denoise="off", banned_terms=None, comments=True)

    async def scenario():
        server = RecServer()
        p = Pipeline(args, server)
        p.comment_source = FakeSource()
        p.translator = None
        p.guard._mono = virtual_mono()
        p.guard._perf = virtual_perf()
        await p._begin_session(ROOM)
        audit = p.audit
        batches = [
            [item("cid-secret-1", "Toñita Secreta 🇭🇳", RED_TEXT, uid=UID, sec="s:SECRETSEC123",
                  handle="tonita_secret"),
             item("cid-secret-2", "Bea", "hola buenas", uid="2")],
            [item("cid-secret-3", "Cy", "te mando link https://evil.example.com/x", uid="3"),
             item("cid-secret-4", "Di", "es falso", uid="4", uid_src="handle")]]
        for batch in batches:
            await p._on_comment_items(batch)
        await p._end_session(audit, reason="user_stop")
        return audit

    with (i18n.use("en") if lang == "en" else nullcontext()):
        audit = run(scenario())
    return audit


def blocks(audit):
    records = audit_records(audit)
    start = of(records, "session_start")[0]["comment_guard"]
    end = dict(of(records, "session_end")[0]["comment_guard"])
    end.pop("observe_ms_max")                                  # 耗时是实测值，两次不会一样
    return start, end


def test_chinese_and_english_sessions_write_byte_identical_guard_blocks(monkeypatch, tmp_path):
    zh = play_session(monkeypatch, tmp_path, "zh")
    en = play_session(monkeypatch, tmp_path, "en")
    zh_start, zh_end = blocks(zh)
    en_start, en_end = blocks(en)
    assert json.dumps(zh_start, sort_keys=True, ensure_ascii=False) == json.dumps(en_start, sort_keys=True,
                                                                                   ensure_ascii=False)
    assert json.dumps(zh_end, sort_keys=True, ensure_ascii=False) == json.dumps(en_end, sort_keys=True,
                                                                                 ensure_ascii=False)
    assert zh_end["observed"] == 4 and zh_end["counts"]["threat"]["alert"] == 1
    assert zh_end["counts"]["promo_link"]["alert"] == 1 and zh_end["counts"]["accuse_claim"]["amber"] == 1
    assert zh_end["amber_by_reason"] == {"maybe_question": 1} and zh_end["marked_auto"] == 1


def test_the_guard_blocks_have_only_codes_and_numbers(monkeypatch, tmp_path):
    audit = play_session(monkeypatch, tmp_path, "zh")
    start, end = blocks(audit)

    def strings(node):
        if isinstance(node, str):
            yield node
        elif isinstance(node, dict):
            for k, v in node.items():
                yield k
                yield from strings(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                yield from strings(v)

    for text in list(strings(start)) + list(strings(end)):
        assert text.isascii(), text                              # 没有界面文字，也没有弹幕里的非 ASCII 昵称


def test_nothing_from_the_comments_reaches_the_audit_file(monkeypatch, tmp_path):
    audit = play_session(monkeypatch, tmp_path, "zh")
    raw = Path(audit.path).read_text(encoding="utf-8")
    for needle in ("Toñita", "Secreta", "tonita_secret", "SECRETSEC", "cid-secret", UID, "te voy a matar",
                   "evil.example.com", "es falso", "hola buenas", "Bea", "\"Cy\"", "\"Di\""):
        assert needle not in raw, needle


def test_every_new_key_is_additive_other_session_start_keys_are_unchanged(monkeypatch, tmp_path):
    audit = play_session(monkeypatch, tmp_path, "zh")
    start = of(audit_records(audit), "session_start")[0]
    for key in ("type", "room_url", "started_at", "code_commit", "glossary_hash", "alerts_enabled",
                "streamer", "translator_active", "banned_terms_hash", "comment_guard"):
        assert key in start, key
    end = of(audit_records(audit), "session_end")[0]
    assert end["reason"] == "user_stop" and "ended_at" in end and "comments_received" in end
    assert "comment_fields" in end and "comment_guard" in end
