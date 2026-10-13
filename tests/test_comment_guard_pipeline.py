"""弹幕留意的接线（PR-3，spec §4.7、§6、§8、不变量 I1/I2/I4/I5/I14）。

守卫挂在弹幕读取协程上：`Pipeline._on_comment_items` 先让守卫看一遍、再交给 `accept`、最后 `flush`。
这里钉住的是：挂接顺序；守卫出任何错弹幕照常显示；识别/检测/审计热路径不引用守卫（AST）；
假复核永不返回时字幕和报警的时刻不变；不调通知和声音；换场/收尾/晚到的旧任务（my_audit）；
默认关时 observe 零调用、零写盘；设置持久化和应急开关；审计里只有计数。

纯内存：不联网、不加载模型、不 import main.py、不开浏览器。主播、昵称、账号都是虚构的。
"""
import ast
import asyncio
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import comment_guard as cg
from app import comment_marks
from app import pipeline as pipeline_mod
from app import resolver, settings
from app.asr import ASRResult
from app.comments import CommentTranslator
from app.pipeline import Pipeline
from app.pipeline_guard import (GUARD_CONTROL_TYPES, GuardRuntime, env_disabled, guard_notice,
                                normalize_settings)
from tests.guard_support import (LINK_TEXT, RED_TEXT, ROUTED_TEXT, UID, FakeSource, RecServer,
                                 audit_records, item, new_runtime, of, start, use_fixture_rules, virtual_mono)
from tests.helpers import run

ROOT = Path(__file__).resolve().parent.parent
ROOM = "https://www.tiktok.com/@luna_demo/live"
OTHER_ROOM = "https://www.tiktok.com/@marca_demo/live"


def make_pipeline(monkeypatch, tmp_path, enabled=True, terms=()):
    """enabled=None：settings.json 里没有 comment_guard 这个键（装机后的默认）。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    if enabled is not None:
        (tmp_path / "settings.json").write_text(json.dumps({"comment_guard": {"enabled": enabled}}),
                                                encoding="utf-8")
    terms_file = tmp_path / "banned_terms.txt"
    terms_file.write_text("\n".join(terms), encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms_file)
    monkeypatch.setattr(resolver, "_login_first_enabled", lambda: False)
    use_fixture_rules(monkeypatch, tmp_path)
    monkeypatch.setattr(comment_marks, "MARKED_DIR", tmp_path / "marked")
    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source="es",
        beam=5, context=False, asr_temperature=None, glossary=None, backend="auto", model=None,
        device="auto", compute_type="auto", denoise="off", banned_terms=None, comments=True)
    server = RecServer()
    p = Pipeline(args, server)
    p.comment_source = FakeSource()
    p.translator = None
    p.alerts_enabled = True
    p.guard._mono = virtual_mono()                   # 预算不被慢机器上的紧循环触发（要测预算的用例自己换）
    return p, server


def guard_notices(server):
    """这一场发出的提示里，弹幕留意自己的那几条（别的提示，比如「违禁词表为空」，不算）。"""
    mine = {str(guard_notice(c)): c for c in ("marks_corrupt", "marks_full", "rules_changed")}
    return [mine[str(m["text"])] for m in server.of_type("notice") if str(m["text"]) in mine]


def result(text):
    return ASRResult(text=text, language="es", raw_text=text, rejected=[])


async def comments(p, *items):
    await p._on_comment_items(list(items))


# ---- 挂接顺序、永不抛（I4）---------------------------------------------------------------

def test_hook_order_is_observe_then_accept_then_flush(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    order = []
    real_observe, real_accept, real_flush = p.guard.observe, p.comments.accept, p.guard.flush

    def observe(items, now=None):
        order.append("observe")
        return real_observe(items, now=now)

    async def accept(data):
        order.append("accept")
        return await real_accept(data)

    async def flush(broadcast, final=False):
        order.append("flush")
        return await real_flush(broadcast, final=final)

    async def scenario():
        await p._begin_session(ROOM)
        monkeypatch.setattr(p.guard, "observe", observe)
        monkeypatch.setattr(p.comments, "accept", accept)
        monkeypatch.setattr(p.guard, "flush", flush)
        await comments(p, item("c1", "Ana", RED_TEXT, uid=UID))

    run(scenario())
    assert order == ["observe", "accept", "flush"]


def test_the_comment_source_is_wired_to_the_guarded_hook_not_straight_to_accept(monkeypatch, tmp_path):
    from app.comment_source import CommentSource

    p, _ = make_pipeline(monkeypatch, tmp_path)
    real = CommentSource(on_items=p._on_comment_items, on_state=p._publish_comment_source)
    assert real._on_items.__func__ is Pipeline._on_comment_items         # Pipeline 构造时用的就是这个方法
    source = (ROOT / "app" / "pipeline.py").read_text(encoding="utf-8")
    assert "on_items=self._on_comment_items" in source and "on_items=lambda" not in source


def test_the_guard_sees_the_text_before_accept_truncates_it(monkeypatch, tmp_path):
    """accept 把文本截到 300 字：第 300 字之后的链接，只有挂在它前面才看得见。"""
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def scenario():
        await p._begin_session(ROOM)
        await comments(p, item("c1", "Ana", "hola " * 70 + "visita https://evil.example.com/x", uid=UID))

    run(scenario())
    shown = server.of_type("comment")[0]
    assert len(shown["text"]) <= CommentTranslator.MAX_TEXT and "evil.example.com" not in shown["text"]
    assert shown["guard"]["lvl"] == "alert" and shown["guard"]["cats"] == ["promo_link"]


def test_an_exception_in_observe_still_shows_the_comments_unannotated(monkeypatch, tmp_path, capsys):
    p, server = make_pipeline(monkeypatch, tmp_path)

    def boom(items, now=None):
        raise RuntimeError("rules exploded")

    async def scenario():
        await p._begin_session(ROOM)
        monkeypatch.setattr(p.guard, "observe", boom)
        await comments(p, item("c1", "Ana", RED_TEXT, uid=UID))

    run(scenario())
    shown = server.of_type("comment")
    assert len(shown) == 1 and shown[0]["text"] == RED_TEXT and "guard" not in shown[0]
    assert "[弹幕] 留意规则出错" in capsys.readouterr().out


def test_an_internal_error_in_the_rules_is_swallowed_per_item(monkeypatch, tmp_path):
    rt = new_runtime(monkeypatch, tmp_path)

    async def scenario():
        await start(rt)
        real = rt.engine.judge

        def judge(text, **kw):
            if "boom" in text:
                raise ValueError("bad item")
            return real(text, **kw)

        monkeypatch.setattr(rt.engine, "judge", judge)
        items = [item("c1", "Ana", RED_TEXT, uid=UID), item("c2", "Bea", "boom", uid="2"),
                 {"id": 5, "text": None}, "not a dict", item("c3", "Cy", "hola buenas", uid="3")]
        return items, rt.observe(items, now=1000.0)

    items, out = run(scenario())
    assert len(out) == 5 and out[1] is items[1] and out[2] is items[2] and out[3] == "not a dict"
    assert out[0]["guard"]["lvl"] == "alert" and "guard" not in out[4] and rt.errors == 1
    assert "guard" not in items[0]                    # 不改传进来的字典


def test_observe_returns_the_input_untouched_when_the_whole_batch_is_unusable(monkeypatch, tmp_path):
    rt = new_runtime(monkeypatch, tmp_path)

    async def scenario():
        await start(rt)
        assert rt.observe("nope") == "nope"
        bad_items = [item("c1", "Ana", RED_TEXT, uid=UID)]
        assert rt.observe(bad_items, now=float("nan")) == bad_items       # ts 不是有限的数：整批原样
        assert rt.observe(bad_items, now="x") is bad_items

    run(scenario())


def test_a_flush_error_never_reaches_the_comment_source(monkeypatch, tmp_path, capsys):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def scenario():
        await p._begin_session(ROOM)

        async def boom(broadcast, final=False):
            raise RuntimeError("flush exploded")

        monkeypatch.setattr(p.guard, "flush", boom)
        await comments(p, item("c1", "Ana", "hola buenas", uid=UID))

    run(scenario())
    assert len(server.of_type("comment")) == 1 and "留意消息发送出错" in capsys.readouterr().out


# ---- 默认关、应急开关 -------------------------------------------------------------------

def test_default_is_off_observe_never_runs_nothing_is_read_or_written(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path, enabled=None)
    assert p.guard.cfg == {"enabled": False, "backend": "none", "weak": False}
    called = []

    def load_rules(*a, **k):
        called.append("load_rules")
        raise AssertionError("规则不该载入")

    def observe(*a, **k):
        called.append("observe")
        raise AssertionError("observe 不该被调用")

    monkeypatch.setattr(cg, "load_rules", load_rules)
    monkeypatch.setattr(GuardRuntime, "observe", observe)

    async def scenario():
        await p._begin_session(ROOM)
        audit = p.audit
        await comments(p, item("c1", "Ana", RED_TEXT, uid=UID), item("c2", "Bea", LINK_TEXT, uid="2"))
        await p._guard_tick()
        await p._end_session(audit, reason="user_stop")
        return audit

    audit = run(scenario())
    assert called == []
    assert [m["text"] for m in server.of_type("comment")] == [RED_TEXT, LINK_TEXT]
    assert all("guard" not in m for m in server.of_type("comment"))
    assert server.guard_messages() == []
    assert not [m for m in server.of_type("config") if any(k.startswith("guard_") for k in m)]
    assert not (tmp_path / "marked").exists()
    records = audit_records(audit)
    assert of(records, "session_start")[0]["comment_guard"] == {"enabled": False}
    assert "comment_guard" not in of(records, "session_end")[0]
    assert server.config["guard_state"]["status"] == "off" and server.config["guard_state"]["enabled"] is False


@pytest.mark.parametrize("value", ["off", "OFF", " Off ", "0", "false", "no"])
def test_emergency_switch_turns_the_guard_off_even_when_settings_say_on(monkeypatch, tmp_path, value):
    monkeypatch.setenv("TLT_COMMENT_GUARD", value)
    p, server = make_pipeline(monkeypatch, tmp_path, enabled=True)
    assert env_disabled() and p.guard.cfg["enabled"] is True

    async def scenario():
        await p._begin_session(ROOM)
        audit = p.audit
        await comments(p, item("c1", "Ana", RED_TEXT, uid=UID))
        await p._end_session(audit, reason="user_stop")
        return audit

    audit = run(scenario())
    assert [m["text"] for m in server.of_type("comment")] == [RED_TEXT]       # 弹幕照常显示
    assert "guard" not in server.of_type("comment")[0] and server.guard_messages() == []
    assert of(audit_records(audit), "session_start")[0]["comment_guard"] == {"enabled": False, "env_off": True}
    assert server.config["guard_state"]["status"] == "env_off" and server.config["guard_state"]["enabled"] is False
    assert not (tmp_path / "marked").exists()


def test_other_values_of_the_switch_do_not_turn_it_off(monkeypatch):
    for value in ("", "on", "1", "yes", "whatever"):
        monkeypatch.setenv("TLT_COMMENT_GUARD", value)
        assert not env_disabled()


def test_settings_are_normalised_defensively():
    assert normalize_settings(None) == {"enabled": False, "backend": "none", "weak": False}
    assert normalize_settings({"enabled": "yes", "backend": "local", "weak": 1}) == \
        {"enabled": False, "backend": "none", "weak": False}              # 用户决定 6：没有本地模型这个选项
    assert normalize_settings({"enabled": True, "backend": "claude", "weak": True}) == \
        {"enabled": True, "backend": "claude", "weak": True}
    assert normalize_settings([1, 2]) == {"enabled": False, "backend": "none", "weak": False}


# ---- 开启后：标注随 comment 到、审计只有计数 ----------------------------------------------------

def test_an_enabled_session_annotates_comments_and_writes_the_audit_keys(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def scenario():
        await p._begin_session(ROOM)
        audit = p.audit
        await comments(p, item("c1", "Ana", RED_TEXT, uid=UID, sec="s:MS4wLjAB", handle="ana_demo"),
                       item("c2", "Bea", "hola buenas", uid="2"),
                       item("c3", "Cy", LINK_TEXT, uid="3", uid_src="handle"))
        await p._end_session(audit, reason="user_stop")
        return audit

    audit = run(scenario())
    shown = {m["id"]: m for m in server.of_type("comment")}
    assert shown["c1"]["uid"] == UID and shown["c1"]["uid_ok"] is True            # 19 位 id 原样成字符串
    assert shown["c1"]["sec"] == "s:MS4wLjAB" and shown["c1"]["handle"] == "ana_demo"
    assert shown["c1"]["guard"]["lvl"] == "alert" and shown["c1"]["guard"]["src"] == "rule"
    assert shown["c1"]["guard"]["rv"] is None and shown["c1"]["guard"]["cats"] == ["threat"]
    assert shown["c1"]["guard"]["why"] and "marked" not in shown["c1"] and "role" not in shown["c1"]
    assert "guard" not in shown["c2"] and shown["c2"]["uid_ok"] is True
    assert shown["c3"]["uid_ok"] is False                                          # 只有 handle：键不稳
    records = audit_records(audit)
    start_rec = of(records, "session_start")[0]["comment_guard"]
    assert start_rec["enabled"] is True and start_rec["backend"] == "none" and start_rec["model"] is None
    assert start_rec["rules_count"] > 0 and re.fullmatch(r"[0-9a-f]{12}", start_rec["rules_hash"])
    assert start_rec["thresholds"]["accumulate"] == [2, 600] and start_rec["warnings"] == []
    end = of(records, "session_end")[0]
    assert end["comment_fields"] == {"n": 3, "has": {"id": 3}}                     # 原有的 comment_fields 还在
    cg_end = end["comment_guard"]
    assert cg_end["counts"]["threat"] == {"alert": 1, "amber": 0, "note": 0}
    assert cg_end["counts"]["promo_link"]["alert"] == 1 and cg_end["observed"] == 3
    assert cg_end["marked_auto"] == 1 and cg_end["unmarked_auto"] == 0 and cg_end["dismissed"] == 0
    assert cg_end["reviews"] == 0 and cg_end["trips"] == 0 and cg_end["budget_trips"] == 0
    assert cg_end["observe_ms_max"] >= 0 and cg_end["guard_msgs"] >= 1


def test_the_audit_holds_counts_only_no_text_nicknames_or_ids(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    secret_nick, secret_handle, secret_sec = "Toñita Demo 🇭🇳", "tonita_demo_handle", "s:SECRETSEC123"

    async def scenario():
        await p._begin_session(ROOM)
        audit = p.audit
        await comments(p, item("cid-zzz-1", secret_nick, RED_TEXT + " hijueputa", uid=UID, sec=secret_sec,
                               handle=secret_handle),
                       item("cid-zzz-2", "Otra Demo", ROUTED_TEXT, uid="888777666"))
        await p._end_session(audit, reason="user_stop")
        return audit

    audit = run(scenario())
    raw = Path(audit.path).read_text(encoding="utf-8")
    for needle in (secret_nick, secret_handle, secret_sec, UID, "888777666", "cid-zzz", "te voy a matar",
                   "hijueputa", "es estafa o no", "Otra Demo"):
        assert needle not in raw, needle
    assert "comment_guard" in raw


def test_session_end_has_no_guard_key_when_the_guard_was_off_that_session(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path, enabled=False)

    async def scenario():
        await p._begin_session(ROOM)
        audit = p.audit
        await p._end_session(audit, reason="user_stop")
        return audit

    records = audit_records(run(scenario()))
    assert "comment_guard" not in of(records, "session_end")[0]
    assert of(records, "session_end")[0]["comment_fields"]["n"] == 3


# ---- I1：识别、检测、审计热路径不引用守卫 ---------------------------------------------------------

HOT_PATH = ("asr_worker", "translation_worker", "_emit_original", "_translate_and_update")
GUARD_FILES = ("app/pipeline_guard.py",)
GUARD_WORDS = re.compile(r"guard|review|comment_marks", re.IGNORECASE)


def _functions(path):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _identifiers(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            yield n.id
        elif isinstance(n, ast.Attribute):
            yield n.attr
        elif isinstance(n, ast.arg):
            yield n.arg
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield n.name


def _self_calls(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "self":
            yield n.attr


def test_the_recognition_and_detection_hot_path_never_references_the_guard():
    functions = _functions("app/pipeline.py")
    hot = [f for f in functions if f.name in HOT_PATH]
    assert sorted({f.name for f in hot}) == sorted(HOT_PATH)       # 都找到了：不是空转
    for f in hot:
        ids = [i for i in _identifiers(f) if "sleep_guard" not in i]
        assert not [i for i in ids if GUARD_WORDS.search(i)], (f.name, ids)

    # 传递闭包：从这些函数出发，沿着 self.<方法> 能走到的 Pipeline 方法里，没有守卫 mixin 的方法
    methods = {}
    for path in ("app/pipeline.py", "app/viewer_share.py", "app/pipeline_disk.py", "app/pipeline_engine.py"):
        for f in _functions(path):
            methods.setdefault(f.name, []).append(f)
    guard_methods = {f.name for f in _functions("app/pipeline_guard.py")}
    seen, queue = set(), [name for f in hot for name in _self_calls(f)]
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        for f in methods.get(name, ()):
            queue.extend(_self_calls(f))
    assert seen, "没有走到任何方法：检查本身失效"
    assert not (seen & guard_methods), seen & guard_methods


def test_guard_references_in_pipeline_py_are_only_at_the_allowed_places():
    """守卫只出现在 on_items、开场/收尾、控制消息、统计循环这几处（spec I1）。"""
    tree = ast.parse((ROOT / "app/pipeline.py").read_text(encoding="utf-8"))
    allowed = {"__init__", "handle_control", "_stats_loop", "_begin_session", "_end_session"}
    offenders = set()
    for f in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        if "sleep_guard" in f.name:              # 防睡眠断言的 guard 是另一回事
            continue
        own = [n for n in ast.walk(f) if isinstance(n, (ast.Name, ast.Attribute))]
        for n in own:
            name = n.id if isinstance(n, ast.Name) else n.attr
            if GUARD_WORDS.search(name) and "sleep_guard" not in name and f.name not in allowed:
                offenders.add((f.name, name))
    assert not offenders, offenders


# ---- I2：假复核永不返回，字幕和报警不受影响 ------------------------------------------------------

class StuckReviewer:
    def __init__(self):
        self.calls = 0

    async def review(self, batch):
        self.calls += 1
        await asyncio.Event().wait()                  # 永不返回


def _captions_and_alerts(server):
    return [(m["type"], m.get("original") or m.get("term")) for m in server.messages
            if m.get("type") in ("caption", "alert")]


async def _run_segments(p, n=20):
    spent = []
    for i in range(n):
        text = "este producto cura el cancer" if i % 4 == 0 else "frase numero %d de la tienda" % i
        t0 = time.perf_counter()
        await asyncio.wait_for(p._emit_original(result(text), audio_end_ts=100.0 + i, asr_ms=100), timeout=5)
        spent.append(time.perf_counter() - t0)
    return spent


def test_a_review_that_never_returns_does_not_delay_captions_or_alerts(monkeypatch, tmp_path):
    base, base_server = make_pipeline(monkeypatch, tmp_path / "base", enabled=False, terms=["cura el cancer"])
    guarded, server = make_pipeline(monkeypatch, tmp_path / "guarded", enabled=True, terms=["cura el cancer"])

    async def baseline():
        await base._begin_session(ROOM)
        return await _run_segments(base)

    async def with_stuck_review():
        reviewer = guarded.guard.reviewer = StuckReviewer()
        await guarded._begin_session(ROOM)
        await comments(guarded, *[item("q%d" % i, "Viewer %d" % i, ROUTED_TEXT, uid=str(100 + i))
                                  for i in range(3)])
        await asyncio.sleep(0.05)
        task = guarded.guard._review_task
        assert reviewer.calls >= 1 and task is not None and not task.done()   # 复核在途，且永不返回
        spent = await _run_segments(guarded)
        assert not task.done()
        await guarded._guard_tick()                   # 统计循环那一跳也不等它
        audit = guarded.audit
        await guarded._end_session(audit, reason="user_stop")   # 收尾取消它，不挂住
        assert task.cancelled() or task.done()
        return spent

    base_spent = run(baseline())
    guarded_spent = run(with_stuck_review())
    assert _captions_and_alerts(server) == _captions_and_alerts(base_server)
    assert len([m for m in server.messages if m["type"] == "alert"]) >= 1       # 报警确实发了
    assert len([m for m in server.messages if m["type"] == "caption"]) == 20
    assert max(guarded_spent) < 0.5 and max(base_spent) < 0.5


def test_queued_candidates_are_only_counted_when_no_reviewer_is_attached(monkeypatch, tmp_path):
    rt = new_runtime(monkeypatch, tmp_path)

    async def scenario():
        await start(rt)
        rt.observe([item("q1", "Ana", ROUTED_TEXT, uid=UID)], now=1000.0)

    run(scenario())
    assert rt.unreviewed == 1 and len(rt._review_q) == 0 and rt._review_task is None


def test_a_late_review_result_is_not_applied_to_the_next_session(monkeypatch, tmp_path):
    rt = new_runtime(monkeypatch, tmp_path)
    gate = {}

    class Reviewer:
        async def review(self, batch):
            gate["event"] = asyncio.Event()
            gate["called"] = True
            await gate["event"].wait()
            return {b["cid"]: "agree" for b in batch}

    async def scenario():
        rt.reviewer = Reviewer()
        await start(rt)
        sent = []

        async def broadcast(msg):
            sent.append(msg)

        rt.observe([item("q1", "Ana", ROUTED_TEXT, uid=UID)], now=1000.0)
        await rt.flush(broadcast)
        await asyncio.sleep(0.05)
        assert gate.get("called")
        await start(rt, streamer="marca_demo")        # 换场：上一场的复核任务被取消
        gate["event"].set()
        await asyncio.sleep(0.05)
        await rt.flush(broadcast)
        return sent

    sent = run(scenario())
    assert not [m for m in sent if m["type"] == "comment_guard"]
    assert "q1" not in rt._ann


def test_a_review_opinion_is_written_beside_the_verdict_and_never_changes_the_level(monkeypatch, tmp_path):
    rt = new_runtime(monkeypatch, tmp_path)

    class Reviewer:
        async def review(self, batch):
            return {b["cid"]: "friendly" for b in batch}

    async def scenario():
        rt.reviewer = Reviewer()
        await start(rt)
        sent = []

        async def broadcast(msg):
            sent.append(msg)

        out = rt.observe([item("q1", "Ana", "es falso", uid=UID)], now=1000.0)
        assert out[0]["guard"]["lvl"] == "amber" and out[0]["guard"]["rv"] is None
        await rt.flush(broadcast)
        await asyncio.sleep(0.05)
        await rt.flush(broadcast)
        return sent

    sent = run(scenario())
    updates = [m for m in sent if m["type"] == "comment_guard"]
    assert len(updates) == 1 and updates[0]["id"] == "q1"
    assert updates[0]["guard"]["rv"] == "friendly" and updates[0]["guard"]["lvl"] == "amber"


# ---- I5：不通知、不出声 -----------------------------------------------------------------------

def test_the_guard_never_notifies_or_plays_sounds(monkeypatch, tmp_path):
    from app import alert_notify, window_attention

    hits = []
    monkeypatch.setattr(Pipeline, "_notify_alert_burst", lambda self: hits.append("notify"))
    monkeypatch.setattr(alert_notify.AlertNotifier, "note_alert", lambda self: hits.append("note_alert"))
    monkeypatch.setattr(alert_notify.AlertNotifier, "send", lambda self: hits.append("send"))
    monkeypatch.setattr(window_attention, "expose_attention", lambda *a, **k: hits.append("attention"))
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def scenario():
        await p._begin_session(ROOM)
        for i in range(8):
            await comments(p, item("r%d" % i, "Ana", RED_TEXT, uid=UID), item("l%d" % i, "Bea", LINK_TEXT, uid="2"))
        await p._guard_tick()
        await p._end_session(p.audit, reason="user_stop")

    run(scenario())
    assert hits == [] and server.of_type("guard_event")
    assert not [m for m in server.messages if m.get("type") == "alert"]       # 也不冒充违禁词报警
    for path in GUARD_FILES + ("app/comment_marks.py",):
        source = (ROOT / path).read_text(encoding="utf-8")
        for needle in ("alert_notify", "window_attention", "AlertNotifier", "expose_attention", "winsound",
                       "afplay", "subprocess", "webbrowser"):
            assert needle not in source.replace("alert_notify / window_attention", ""), (path, needle)


# ---- 换场：事件、名单、刷屏状态按主播重置 ---------------------------------------------------------

def test_events_marks_and_flood_state_reset_per_streamer(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def scenario():
        await p._begin_session(ROOM)
        audit_a = p.audit
        await comments(p, item("a1", "Ana", RED_TEXT, uid=UID))
        for i in range(5):
            await comments(p, item("f%d" % i, "Flooder", "texto %s unico" % "abcde"[i], uid="55"))
        assert server.config["guard_events"] and UID in server.config["guard_marked"]
        assert p.guard.engine.flood.users                 # 刷屏引擎里有这个人
        await p._end_session(audit_a, reason="user_stop")
        # 下一场换了主播：事件表清空、名单换成这个主播的（空）、刷屏计数从零开始
        await p._begin_session(OTHER_ROOM)
        assert server.config["guard_events"] == {} and server.config["guard_marked"] == {}
        assert not p.guard.engine.flood.users and p.guard.events == {}
        assert p.guard.streamer == "marca_demo" and p.guard.marks.streamer == "marca_demo"
        await comments(p, item("b1", "Flooder", "texto f unico", uid="55"))
        assert [e["n"] for e in p.guard.events.values()] == [] and len(p.guard.marks) == 0
        await p._end_session(p.audit, reason="user_stop")
        # 回到第一个主播：名单从磁盘读回来（按主播记住，下一场还在）
        await p._begin_session(ROOM)
        assert UID in server.config["guard_marked"]
        assert server.config["guard_marked"][UID]["by"] == "auto"
        await p._end_session(p.audit, reason="user_stop")

    run(scenario())
    assert (tmp_path / "marked" / "luna_demo.json").exists() and not (tmp_path / "marked" / "marca_demo.json").exists()


def test_end_of_session_flushes_the_marks_before_the_process_could_exit(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def scenario():
        await p._begin_session(ROOM)
        await comments(p, item("a1", "Ana", RED_TEXT, uid=UID))
        assert not (tmp_path / "marked" / "luna_demo.json").exists()          # 2 秒合并窗口里
        await p._end_session(p.audit, reason="user_stop")

    run(scenario())
    assert json.loads((tmp_path / "marked" / "luna_demo.json").read_text())["viewers"][UID]["by"] == "auto"


def test_a_late_old_task_cannot_close_the_next_sessions_guard(monkeypatch, tmp_path):
    """晚死的旧任务关掉新会话的守卫、把计数写进错的审计——CLAUDE.md 第七条 my_audit 的形态。"""
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def scenario():
        await p._begin_session(ROOM)
        audit_a = p.audit
        await comments(p, item("a1", "Ana", RED_TEXT, uid=UID))
        await p._begin_session(OTHER_ROOM)             # 旧任务还没收尾，新一场已经开始
        audit_b = p.audit
        assert audit_b is not audit_a and p.guard.audit is audit_b and p.guard.enabled
        await p._end_session(audit_a, reason="user_stop")          # 旧任务终于走到 finally
        assert p.guard.enabled and p.guard.audit is audit_b and p.audit is audit_b
        await comments(p, item("b1", "Cy", RED_TEXT, uid="42"))
        assert server.of_type("comment")[-1]["guard"]["lvl"] == "alert"       # 新一场的守卫照常
        await p._end_session(audit_b, reason="user_stop")
        return audit_a, audit_b

    audit_a, audit_b = run(scenario())
    assert "comment_guard" not in " ".join(Path(audit_a.path).read_text(encoding="utf-8").splitlines()[1:])
    end_b = of(audit_records(audit_b), "session_end")[0]["comment_guard"]
    assert end_b["observed"] == 1 and end_b["counts"]["threat"]["alert"] == 1       # 只有这一场自己的计数


def test_ending_without_an_audit_argument_closes_the_current_sessions_guard(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def scenario():
        await p._begin_session(ROOM)
        assert p.guard.enabled
        await p._end_session()
        assert not p.guard.enabled and not p.guard._open

    run(scenario())


# ---- 词表文件被改、名单损坏的提示 ---------------------------------------------------------------

def test_editing_the_rules_during_a_session_notifies_once_and_does_not_hot_reload(monkeypatch, tmp_path):
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    for f in (ROOT / "tests" / "fixtures" / "comment_guard").glob("*.txt"):
        (rules_dir / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    p, server = make_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(cg, "DEFAULTS_DIR", rules_dir)

    async def scenario():
        await p._begin_session(ROOM)
        await p._guard_tick()
        assert guard_notices(server) == []                           # 没改：不提示
        target = rules_dir / "insult.txt"
        target.write_text(target.read_text(encoding="utf-8") + "\nzzpalabra\n", encoding="utf-8")
        await p._guard_tick()
        await p._guard_tick()
        assert guard_notices(server) == ["rules_changed"]            # 每次变化只说一次
        out = p.guard.observe([item("c1", "Ana", "eres zzpalabra", uid=UID)], now=time.time())
        assert "guard" not in out[0]                                 # 不热加载：新词条本场不生效
        target.write_text(target.read_text(encoding="utf-8").replace("\nzzpalabra\n", "\n"), encoding="utf-8")
        await p._guard_tick()
        # 改回本场载入的内容：mtime 变了，但这里不比内容，所以再提示一次也可以；至少不能抛
        await p._end_session(p.audit, reason="user_stop")

    run(scenario())


def test_a_corrupt_marked_file_gives_one_paired_notice_and_starts_empty(monkeypatch, tmp_path):
    (tmp_path / "marked").mkdir()
    (tmp_path / "marked" / "luna_demo.json").write_text("not json at all", encoding="utf-8")
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def scenario():
        await p._begin_session(ROOM)
        await p._end_session(p.audit, reason="user_stop")

    run(scenario())
    assert guard_notices(server) == ["marks_corrupt"]
    assert str(guard_notice("marks_corrupt")) == "关注名单文件读不了，已另存一份备份，先按空名单继续"
    assert list((tmp_path / "marked").glob("luna_demo.json.bak-*"))


def test_notices_are_chinese_and_english_pairs_and_free_of_cause_labels():
    from app import i18n

    for code in ("marks_corrupt", "marks_full", "rules_changed"):
        text = guard_notice(code)
        assert isinstance(text, i18n.Bi) and str(text) and text.en and text.en != str(text)
    assert guard_notice("nope") == ""


# ---- 设置与控制消息 ---------------------------------------------------------------------------

def test_guard_set_persists_once_and_updates_the_state(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path, enabled=None)
    saves = []
    real = p._save_setting
    monkeypatch.setattr(p, "_save_setting", lambda k, v: (saves.append((k, dict(v))), real(k, v)))

    async def scenario():
        await p.handle_control({"type": "guard_set", "enabled": True})
        await p.handle_control({"type": "guard_set", "enabled": True})               # 没变化：不再写
        await p.handle_control({"type": "guard_set", "backend": "claude", "weak": True})
        await p.handle_control({"type": "guard_set", "backend": "local"})            # 用户决定 6：没有这个选项
        await p.handle_control({"type": "guard_set", "enabled": "yes", "weak": 1})   # 类型不对：忽略
        await p.handle_control({"type": "guard_set", "key": "sk-test-not-a-real-key"})   # key 先不处理

    run(scenario())
    assert saves == [("comment_guard", {"enabled": True, "backend": "none", "weak": False}),
                     ("comment_guard", {"enabled": True, "backend": "claude", "weak": True})]
    saved = settings.load_settings()["comment_guard"]
    assert saved == {"enabled": True, "backend": "claude", "weak": True}
    assert "sk-test" not in (tmp_path / "settings.json").read_text(encoding="utf-8")
    state = server.config["guard_state"]
    assert state["enabled"] is True and state["backend"] == "claude" and state["status"] == "no_sdk"
    assert state["weak"] is True and state["rules"]["count"] > 0 and state["n_marked"] == 0
    configs = [m for m in server.of_type("config") if "guard_state" in m]
    assert len(configs) == 2 and configs[0]["guard_state"]["status"] == "rules"
    # 重新构造一个 Pipeline：设置读得回来（重启后还是这个选择）
    again, _ = make_pipeline(monkeypatch, tmp_path, enabled=None)
    assert again.guard.cfg == {"enabled": True, "backend": "claude", "weak": True}


def test_guard_set_is_ignored_while_a_session_is_running(monkeypatch, tmp_path, capsys):
    p, server = make_pipeline(monkeypatch, tmp_path, enabled=False)

    async def scenario():
        await p._begin_session(ROOM)
        await p.handle_control({"type": "guard_set", "enabled": True})
        assert p.guard.cfg["enabled"] is False and not p.guard.enabled     # 这一场的开关已经定了
        await p._end_session(p.audit, reason="user_stop")
        await p.handle_control({"type": "guard_set", "enabled": True})
        assert p.guard.cfg["enabled"] is True

    run(scenario())
    assert "直播进行中" in capsys.readouterr().out
    assert settings.load_settings()["comment_guard"]["enabled"] is True


def test_guard_mark_adds_and_removes_from_the_desktop_page(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def scenario():
        await p._begin_session(ROOM)
        await comments(p, item("c1", "Ana", "hola buenas", uid=UID, sec="s:MS4wLjAB", handle="ana_demo"))
        await p.handle_control({"type": "guard_mark", "key": UID, "on": True, "nick": "Ana B",
                                "handle": "ana_demo", "uid_src": "id"})
        marked = dict(server.config["guard_marked"])
        assert marked[UID]["by"] == "manual" and marked[UID]["nick"] == "Ana B"
        assert marked[UID]["keys"] == [UID, "s:MS4wLjAB"]                  # 守卫见过这个人的 sec：一并记下
        assert server.of_type("guard_mark")[-1] == {"type": "guard_mark", "key": UID, "entry": marked[UID]}
        # 缓存里的弹幕永远没有 marked 键：高亮由页面按 keys 现算
        assert all("marked" not in c for c in server.comments)
        await p.handle_control({"type": "guard_mark", "key": "n:Solo Nick", "on": True, "nick": "Solo Nick"})
        assert server.config["guard_marked"]["n:Solo Nick"]["session"] is True
        await p.handle_control({"type": "guard_mark", "key": UID, "on": False})
        assert UID not in server.config["guard_marked"]
        assert server.of_type("guard_mark")[-1] == {"type": "guard_mark", "key": UID, "entry": None}
        assert [c for c in server.comments if "marked" in c] == []
        await p._end_session(p.audit, reason="user_stop")

    run(scenario())
    data = json.loads((tmp_path / "marked" / "luna_demo.json").read_text(encoding="utf-8"))
    assert data["viewers"] == {}                                           # 手动加入又移出；昵称那条不落盘


def test_guard_mark_is_refused_when_the_guard_is_off_or_the_key_is_bad(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path, enabled=False)

    async def scenario():
        await p._begin_session(ROOM)
        await p.handle_control({"type": "guard_mark", "key": UID, "on": True})
        await p._end_session(p.audit, reason="user_stop")
        on, server_on = make_pipeline(monkeypatch, tmp_path / "on", enabled=True)
        await on._begin_session(ROOM)
        for bad in (None, "", 5, "x" * 200, "bad\x07key"):
            await on.handle_control({"type": "guard_mark", "key": bad, "on": True})
        await on._end_session(on.audit, reason="user_stop")
        return server_on

    server_on = run(scenario())
    assert server.of_type("guard_mark") == [] and server_on.of_type("guard_mark") == []
    assert not (tmp_path / "marked").exists()


def test_a_full_list_gives_a_paired_notice(monkeypatch, tmp_path):
    monkeypatch.setattr(comment_marks, "MAX_VIEWERS", 2)
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def scenario():
        await p._begin_session(ROOM)
        for key in ("1", "2", "3"):
            await p.handle_control({"type": "guard_mark", "key": key, "on": True})
        await p._end_session(p.audit, reason="user_stop")

    run(scenario())
    assert guard_notices(server) == ["marks_full"]
    assert sorted(server.config["guard_marked"]) == ["1", "2"]


def test_guard_dismiss_drops_the_event_and_is_counted(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def scenario():
        await p._begin_session(ROOM)
        await comments(p, item("c1", "Ana", LINK_TEXT, uid=UID))
        ev_id = next(iter(server.config["guard_events"]))
        await p.handle_control({"type": "guard_dismiss", "ev_id": ev_id})
        assert server.config["guard_events"] == {}
        assert server.of_type("guard_event")[-1] == {"type": "guard_event", "ev": {"id": ev_id, "dismissed": True}}
        await p.handle_control({"type": "guard_dismiss", "ev_id": ev_id})        # 再忽略一次：什么都不发生
        await p.handle_control({"type": "guard_dismiss", "ev_id": 7})
        audit = p.audit
        await p._end_session(audit, reason="user_stop")
        return audit

    audit = run(scenario())
    assert of(audit_records(audit), "session_end")[0]["comment_guard"]["dismissed"] == 1


def test_control_message_types_are_exactly_the_three(monkeypatch, tmp_path):
    assert GUARD_CONTROL_TYPES == frozenset({"guard_set", "guard_mark", "guard_dismiss"})


# ---- 状态随 hello 走 ---------------------------------------------------------------------------

def test_guard_state_events_and_marks_live_in_config_so_a_refreshed_page_gets_them(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def scenario():
        await p._begin_session(ROOM)
        await comments(p, item("c1", "Ana", RED_TEXT, uid=UID))
        return dict(server.config)

    config = run(scenario())
    assert config["guard_state"]["enabled"] is True and config["guard_state"]["status"] == "rules"
    assert config["guard_state"]["rules"]["count"] > 0 and config["guard_state"]["since"] > 0
    assert list(config["guard_events"].values())[0]["cat"] == "threat"
    assert config["guard_marked"][UID]["cat"] == "threat"
    assert set(config["guard_state"]) == {"enabled", "backend", "status", "since", "trip", "rules", "n_marked",
                                          "unreviewed", "weak"}


# ---- 刷屏与 accept 去重：升级打在已显示的那一条上，只发一条 comment_guard（spec §4.7）-----------------------

def test_same_sentence_five_times_shows_one_comment_and_escalates_it_with_one_message(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def scenario():
        await p._begin_session(ROOM)
        for i in range(6):
            await comments(p, item("s%d" % i, "Same", "quiero el combo rosa para mi hermana", uid="104"))

    run(scenario())
    shown = server.of_type("comment")
    assert len(shown) == 1 and shown[0]["id"] == "s0" and "guard" not in shown[0]    # accept 去重：只显示第一条
    updates = server.of_type("comment_guard")
    assert len(updates) == 1 and updates[0]["id"] == "s0"                            # 5 次起红，只发 1 条
    assert updates[0]["guard"]["lvl"] == "alert" and updates[0]["guard"]["cats"][0] == "flood_repeat"
    assert updates[0]["guard"]["tier"] == "struct"
    cached = server.comments[0]
    assert cached["guard"] == updates[0]["guard"]                                    # 缓存里的那条跟着变
    assert cached["translated"] is None and cached["state"] == "skipped" and "marked" not in cached
    assert len(server.config["guard_events"]) == 1 and p.guard.marked_auto == 0      # 刷屏一律不自动加入


def test_a_question_repeated_five_times_is_at_most_a_note_and_sends_nothing(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def scenario():
        await p._begin_session(ROOM)
        for i in range(5):
            await comments(p, item("q%d" % i, "Ask", "cuanto cuesta el de 30 dias?", uid="105"))

    run(scenario())
    assert len(server.of_type("comment")) == 1
    assert server.of_type("comment_guard") == [] and server.of_type("guard_event") == []


def test_several_accounts_with_the_same_sentence_are_one_amber_event_and_zero_red(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def scenario():
        await p._begin_session(ROOM)
        for i in range(5):
            await comments(p, item("a%d" % i, "Acc %d" % i, "quiero el combo rosa", uid="3%d" % i))
        await p._guard_tick()

    run(scenario())
    shown = server.of_type("comment")
    assert len(shown) == 5 and not [m for m in shown if m.get("guard", {}).get("lvl") == "alert"]
    assert shown[-1]["guard"]["lvl"] == "amber" and shown[-1]["guard"]["amb"] == "brigade"
    events = list(server.config["guard_events"].values())
    assert len(events) == 1 and events[0]["kind"] == "brigade" and events[0]["n"] == 5
    assert events[0]["lvl"] == "amber" and events[0]["cat"] == "flood_brigade" and events[0]["window_sec"] == 20
    assert len(events[0]["accounts"]) == 5 and events[0]["accounts"][0] == {"user": "Acc 0", "uid": "30"}
    assert shown[-1]["guard"]["ev"] == events[0]["id"] and events[0]["sample"]["cid"] == "a4"
    assert p.guard.marked_auto == 0 and len(p.guard.marks) == 0


def test_common_chatter_from_many_accounts_is_not_reported(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def scenario():
        await p._begin_session(ROOM)
        for i in range(30):
            for text in ("yo", "Feliz cumpleaños", "❤️"):
                await comments(p, item("n%d-%s" % (i, text[:2]), "Fan %d" % i, text, uid="9%d" % i))

    run(scenario())
    assert all(m.get("guard") is None for m in server.of_type("comment"))
    assert server.of_type("guard_event") == [] and server.of_type("comment_guard") == []


def test_replaying_the_same_comment_ids_twice_reports_nothing_new(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def scenario():
        await p._begin_session(ROOM)
        batch = [item("h%d" % i, "Hist", "texto %s unico" % "abcdefgh"[i], uid="77") for i in range(8)]
        await comments(p, *batch)
        first = len(server.guard_messages())
        await comments(p, *batch)
        return first

    first = run(scenario())
    assert len(server.of_type("comment")) == 8                       # 第二遍 accept 按 id 去重
    assert len(server.guard_messages()) == first                      # 守卫也不重复计数、不重复报


# ---- 事件聚合、节流、内存有界 --------------------------------------------------------------------

THREATS = ("te voy a matar", "ojala te mueras", "que te mueras", "te voy a matar a ti")


def test_events_of_the_same_kind_from_one_person_merge_within_60_seconds(monkeypatch, tmp_path):
    rt = new_runtime(monkeypatch, tmp_path)

    async def scenario():
        await start(rt)
        for i, (t, text) in enumerate([(1000.0, THREATS[0]), (1010.0, THREATS[1]), (1030.0, THREATS[2]),
                                       (1100.0, THREATS[3])]):
            out = rt.observe([item("t%d" % i, "Ana", text, uid=UID)], now=t)
            assert out[0]["guard"]["lvl"] == "alert"
        return out

    out = run(scenario())
    events = list(rt.events.values())
    assert [(e["id"], e["n"], e["kind"], e["cat"]) for e in events] == [("g1", 3, "comment", "threat"),
                                                                       ("g2", 1, "comment", "threat")]
    assert events[0]["first_ts"] == 1000.0 and events[0]["ts"] == 1030.0
    assert events[0]["sample"]["cid"] == "t2" and events[0]["sample"]["text"] == THREATS[2]
    assert events[0]["sample"]["uid"] == UID and out[0]["guard"]["ev"] == "g2"


def test_the_event_table_keeps_the_50_most_recent_and_memory_stays_bounded(monkeypatch, tmp_path):
    tick = iter(range(10 ** 6))
    rt = new_runtime(monkeypatch, tmp_path, mono=lambda: float(next(tick)))      # 预算按虚拟时间走，不被紧循环触发

    async def scenario():
        await start(rt)
        for i in range(120):
            word = chr(97 + i // 26) + chr(97 + i % 26)                 # 每条都不一样：不凑成多账号同句
            rt.observe([item("e%d" % i, "Viewer %d" % i, "te voy a matar " + word, uid=str(1000 + i))],
                       now=1000.0 + i)

    run(scenario())
    stamps = [e["ts"] for e in rt.events.values()]
    assert len(rt.events) == 50 and stamps == sorted(stamps)         # 留下最近更新的 50 个，最久没动的先淘汰
    assert "g1" not in rt.events and rt.events[next(reversed(rt.events))]["ts"] == 1119.0
    assert len(rt._ev_dirty) <= 50 and len(rt._ann) <= rt.ANN_MAX and len(rt._shown) <= rt.SHOWN_MAX


def test_guard_event_goes_out_at_most_once_a_second_and_new_events_go_first(monkeypatch, tmp_path):
    clock = {"t": 1000.0}
    rt = new_runtime(monkeypatch, tmp_path, clock=lambda: clock["t"])
    sent = []

    async def broadcast(msg):
        sent.append((clock["t"], msg))

    async def scenario():
        await start(rt)
        for i in range(5):
            rt.observe([item("e%d" % i, "Viewer %d" % i, THREATS[i % 3], uid=str(1000 + i))], now=clock["t"])
        await rt.flush(broadcast)
        assert [m["ev"]["id"] for _, m in sent if m["type"] == "guard_event"] == ["g1"]   # 先发一个
        await rt.flush(broadcast)
        assert len([1 for _, m in sent if m["type"] == "guard_event"]) == 1                # 同一秒内不再发
        for step in range(1, 5):
            clock["t"] = 1000.0 + step
            await rt.flush(broadcast)
        # 其间 g1 又有更新：新事件还没发过的先发，更新排在后面
        clock["t"] = 1000.5
        rt.observe([item("e9", "Viewer 0", THREATS[1], uid="1000")], now=1000.5)
        await rt.close_session()

    run(scenario())
    times = [t for t, m in sent if m["type"] == "guard_event"]
    assert all(b - a >= 1.0 for a, b in zip(times, times[1:])) and len(times) == 5
    assert [m["ev"]["id"] for _, m in sent if m["type"] == "guard_event"] == ["g1", "g2", "g3", "g4", "g5"]


def test_a_flush_with_nothing_pending_does_nothing(monkeypatch, tmp_path):
    rt = new_runtime(monkeypatch, tmp_path)
    sent = []

    async def broadcast(msg):
        sent.append(msg)

    async def scenario():
        await start(rt)
        await rt.flush(broadcast)
        rt.observe([item("c1", "Ana", "hola buenas", uid=UID)], now=1000.0)
        await rt.flush(broadcast)

    run(scenario())
    assert sent == []
    off = new_runtime(monkeypatch, tmp_path, enabled=False)
    run(off.flush(broadcast))
    assert sent == []


def test_a_burst_of_500_a_minute_for_ten_minutes_keeps_guard_messages_and_memory_bounded(monkeypatch, tmp_path):
    """spec §6：守卫消息总数 ≤ 600 + 标注变化数；内存有界。时钟是假的（一分钟 500 条，十分钟）。"""
    clock = {"t": 1_000.0}
    rt = new_runtime(monkeypatch, tmp_path, clock=lambda: clock["t"], mono=lambda: clock["t"])
    counts = {"guard_event": 0, "comment_guard": 0, "guard_mark": 0, "config": 0}
    per_cid = {}
    changes = [0]                                   # 标注真正变了的次数（每次进待发队列算一次）

    class Counting(type(rt._pending_cg)):
        def __setitem__(self, key, value):
            if key not in self:
                changes[0] += 1
            super().__setitem__(key, value)

    async def broadcast(msg):
        counts[msg["type"]] += 1
        if msg["type"] == "comment_guard":
            times = per_cid.setdefault(msg["id"], [])
            times.append(clock["t"])

    texts = ["quiero el combo rosa", "hola buenas tardes", "me encanta", "te voy a matar", LINK_TEXT,
             "texto %s unico", "participo en el sorteo", "es falso", "cuanto cuesta el de 30 dias?"]

    async def scenario():
        await start(rt)
        rt._pending_cg = Counting()                 # begin 会换新的待发队列：在它之后装计数器
        n = 0
        for second in range(600):
            clock["t"] = 1_000.0 + second
            batch = []
            for _ in range(8 + (1 if second % 3 else 0)):               # 每分钟约 500 条
                n += 1
                text = texts[n % len(texts)]
                if "%s" in text:
                    text = text % "abcdefghij"[n % 10]
                batch.append(item("b%d" % n, "Viewer %d" % (n % 40), text, uid=str(5000 + n % 40)))
            rt.observe(batch, now=clock["t"] + 0.5)
            await rt.flush(broadcast)
        await rt.close_session()
        return n

    total = run(scenario())
    assert total > 4500 and changes[0] > 20                          # 确实有标注被升级（accept 去重吞掉的重复）
    assert counts["guard_event"] <= 600                              # 全局每秒至多 1 条
    assert counts["config"] <= 300 + 1                               # guard_state 每 2 秒至多 1 条
    assert counts["guard_mark"] <= rt.marked_auto + rt.marked_manual + rt.unmarked_auto + 50
    assert changes[0] == counts["comment_guard"] + len(rt._pending_cg)
    assert all(b - a >= 1.0 for times in per_cid.values() for a, b in zip(times, times[1:]))   # 每个 cid 每秒至多 1 条
    assert len(rt.events) <= 50 and len(rt._shown) <= rt.SHOWN_MAX and len(rt._ann) <= rt.ANN_MAX
    assert len(rt._accounts) <= rt.ACCOUNTS_MAX and len(rt._cg_sent) <= rt.ANN_MAX
    assert len(rt._ev_index) <= 4 * rt.EVENT_MAX + 1 and len(rt.marks) <= comment_marks.MAX_VIEWERS
    flood = rt.engine.flood
    assert len(flood.users) <= cg.USER_TABLE and len(flood.recent) <= cg.RECENT_MAX
    # 总数 ≤ 600 + 标注变化数（+ guard_state 和名单增删这两项各自的上界）
    total_msgs = sum(counts.values())
    assert total_msgs <= 600 + changes[0] + 301 + counts["guard_mark"]


# ---- I3：规则判定的预算 --------------------------------------------------------------------------

def test_a_slow_rule_trips_the_budget_and_the_rest_run_structure_only(monkeypatch, tmp_path):
    rt = new_runtime(monkeypatch, tmp_path, mono=lambda: 500.0)

    async def scenario():
        await start(rt)
        real = rt.engine.judge
        modes = []

        def slow(text, **kw):
            modes.append(kw["structure_only"])
            end = time.perf_counter() + 0.03
            while time.perf_counter() < end:             # 每条 30 ms 的慢规则
                pass
            return real(text, **kw)

        monkeypatch.setattr(rt.engine, "judge", slow)
        rt.observe([item("s%d" % i, "V%d" % i, "hola buenas tardes", uid=str(70 + i)) for i in range(4)],
                   now=1000.0)
        return modes

    modes = run(scenario())
    assert modes[:2] == [False, False] and modes[2:] == [True, True]      # 超过 50 ms 之后降级
    assert rt.budget.trips == 1 and rt.observe_ms_max >= 30
    assert rt._session_end_record()["budget_trips"] == 1


def test_structure_mode_still_catches_links_and_flooding(monkeypatch, tmp_path):
    rt = new_runtime(monkeypatch, tmp_path, mono=lambda: 500.0)

    async def scenario():
        await start(rt)
        rt.budget.since, rt.budget.until = 499.0, 510.0                 # 正处在结构模式里
        assert rt.budget.structure_only(500.0)
        return rt.observe([item("l1", "Ana", LINK_TEXT, uid=UID), item("r1", "Bea", RED_TEXT, uid="2")],
                          now=1000.0)

    out = run(scenario())
    assert out[0]["guard"]["lvl"] == "alert" and out[0]["guard"]["cats"] == ["promo_link"]
    assert "guard" not in out[1]                       # 词表那一半在结构模式下不跑：只跑链接、电话、刷屏
