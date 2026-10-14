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
import tempfile
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
                                 audit_records, item, new_runtime, of, start, use_fixture_rules, virtual_mono, virtual_perf)
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
    p.guard._perf = virtual_perf()                   # 单条耗时也是假的：机器卡一下不会误触预算
    p.guard.REVIEW_WINDOW_SEC = 0.0                  # 复核的攒批窗口（生产里 2 秒）：测试里不等
    return p, server


NOTICE_CODES = ("marks_corrupt", "marks_unreadable", "marks_write_failed", "marks_full", "rules_changed")


def guard_notices(server):
    """这一场发出的提示里，弹幕留意自己的那几条（别的提示，比如「违禁词表为空」，不算）。"""
    mine = {str(guard_notice(c)): c for c in NOTICE_CODES}
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


# ---- 默认开（PR-4 起）、关着时什么都不碰、应急开关 ----------------------------------------------

def test_a_fresh_install_has_the_guard_on_rules_only(monkeypatch, tmp_path):
    """settings.json 里没写过 comment_guard：默认开、只用规则（PR-4 起；PR-3 单独存在时默认关）。"""
    p, server = make_pipeline(monkeypatch, tmp_path, enabled=None)
    assert p.guard.cfg == {"enabled": True, "backend": "none", "weak": False}
    assert server.config["guard_state"]["enabled"] is True and server.config["guard_state"]["status"] == "rules"


def test_switched_off_observe_never_runs_nothing_is_read_or_written(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path, enabled=False)
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
    assert normalize_settings(None) == {"enabled": True, "backend": "none", "weak": False}      # PR-4 起默认开
    assert normalize_settings({"enabled": "yes", "backend": "local", "weak": 1}) == \
        {"enabled": True, "backend": "none", "weak": False}               # 用户决定 6：没有本地模型这个选项；类型不对按默认
    assert normalize_settings({"enabled": False, "backend": "claude", "weak": True}) == \
        {"enabled": False, "backend": "claude", "weak": True}             # 明确写过 false 的人照旧关着
    assert normalize_settings({"enabled": True, "backend": "claude", "weak": True}) == \
        {"enabled": True, "backend": "claude", "weak": True}
    assert normalize_settings([1, 2]) == {"enabled": True, "backend": "none", "weak": False}


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
    """守卫只出现在 on_items、开场/收尾、控制消息、统计循环这几处（spec I1）；PR-5 加了启动自检（run_selfcheck）：
    自检里的「弹幕留意」一行要读守卫的状态，它不在识别、翻译、报警的路径上。"""
    tree = ast.parse((ROOT / "app/pipeline.py").read_text(encoding="utf-8"))
    allowed = {"__init__", "handle_control", "_stats_loop", "_begin_session", "_end_session", "run_selfcheck"}
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
        assert out[0]["guard"]["lvl"] == "amber" and out[0]["guard"]["rv"] == "pending"      # 复核中…
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

    texts = set()
    for code in NOTICE_CODES:
        text = guard_notice(code)
        assert isinstance(text, i18n.Bi) and str(text) and text.en and text.en != str(text)
        texts.add(str(text))
    assert len(texts) == len(NOTICE_CODES)              # 每个代码一句，没有两个代码共用一句话
    assert guard_notice("nope") == ""


# ---- 设置与控制消息 ---------------------------------------------------------------------------

def test_guard_set_persists_once_and_updates_the_state(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path, enabled=False)
    saves = []
    real = p._save_setting
    monkeypatch.setattr(p, "_save_setting", lambda k, v: (saves.append((k, dict(v))), real(k, v)))

    async def scenario():
        await p.handle_control({"type": "guard_set", "enabled": True})
        await p.handle_control({"type": "guard_set", "enabled": True})               # 没变化：不再写
        await p.handle_control({"type": "guard_set", "backend": "claude", "weak": True})
        await p.handle_control({"type": "guard_set", "backend": "local"})            # 用户决定 6：没有这个选项
        await p.handle_control({"type": "guard_set", "enabled": "yes", "weak": 1})   # 类型不对：忽略

    run(scenario())
    assert saves == [("comment_guard", {"enabled": True, "backend": "none", "weak": False}),
                     ("comment_guard", {"enabled": True, "backend": "claude", "weak": True})]
    saved = settings.load_settings()["comment_guard"]
    assert saved == {"enabled": True, "backend": "claude", "weak": True}
    state = server.config["guard_state"]
    # 选了 Claude 而这台机器上没有 Claude 密钥（conftest 清掉了环境变量，临时 settings.json 里也没有）：只用规则
    assert state["enabled"] is True and state["backend"] == "claude" and state["status"] == "no_key"
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


def test_control_message_types_are_exactly_these_five(monkeypatch, tmp_path):
    assert GUARD_CONTROL_TYPES == frozenset({"guard_set", "guard_mark", "guard_dismiss", "guard_reenable",
                                             "guard_clear_auto"})


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
                                          "unreviewed", "weak", "streamer"}
    assert config["guard_state"]["streamer"] == "luna_demo"


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
    perf = virtual_perf()
    rt = new_runtime(monkeypatch, tmp_path, mono=lambda: 500.0, perf=perf)

    async def scenario():
        await start(rt)
        real = rt.engine.judge
        modes = []

        def slow(text, **kw):
            modes.append(kw["structure_only"])
            perf.advance(0.031)                          # 每条 31 ms 的慢规则：耗时从注入的时钟里量，不靠真等
            return real(text, **kw)

        monkeypatch.setattr(rt.engine, "judge", slow)
        rt.observe([item("s%d" % i, "V%d" % i, "hola buenas tardes", uid=str(70 + i)) for i in range(4)],
                   now=1000.0)
        return modes

    modes = run(scenario())
    assert modes[:2] == [False, False] and modes[2:] == [True, True]      # 超过 50 ms 之后降级
    assert rt.budget.trips == 1 and rt.observe_ms_max >= 30
    assert rt._session_end_record()["budget_trips"] == 1


def test_one_slow_moment_on_the_machine_does_not_degrade_the_rules(monkeypatch, tmp_path):
    """单条耗时从注入的时钟量：真实时间过去多久都不算（GC 停顿、别的进程抢 CPU 不该让琥珀和词表判定消失）。"""
    rt = new_runtime(monkeypatch, tmp_path)

    async def scenario():
        await start(rt)
        real = rt.engine.judge

        def stalled(text, **kw):
            time.sleep(0.06)                             # 真的卡 60 ms
            return real(text, **kw)

        monkeypatch.setattr(rt.engine, "judge", stalled)
        return rt.observe([item("w1", "Ana", RED_TEXT, uid=UID)], now=1000.0)

    out = run(scenario())
    assert out[0]["guard"]["lvl"] == "alert" and rt.budget.trips == 0 and rt.observe_ms_max == 0.0


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


# ---- 收尾期间下一场开始了：晚到的旧 end() 不碰新一场（CLAUDE.md 第七条）-----------------------------------

def test_an_old_end_still_awaiting_its_final_flush_leaves_the_next_session_alone(monkeypatch, tmp_path):
    """旧一场的 end() 挂在最后一次 flush 里（控制页面卡了一下），_stop_locked 的 3 秒宽限过了、下一场
    的 begin() + bind() 已经跑完——旧 end() 醒来后什么都不许碰：不关新一场的守卫，不替它冲名单，
    不把新一场的计数当成旧一场的交回去。"""
    rt = new_runtime(monkeypatch, tmp_path)
    audit_a, audit_b = object(), object()
    flushes = []
    real_flush_now = rt.marks.flush_now

    async def counting_flush_now():
        flushes.append(rt.marks.streamer)
        await real_flush_now()

    monkeypatch.setattr(rt.marks, "flush_now", counting_flush_now)

    async def scenario():
        await start(rt, audit=audit_a)
        rt.observe([item("c1", "Ana", RED_TEXT, uid=UID)], now=1000.0)      # 名单、事件、config 都有待发的
        gate = asyncio.Event()

        async def slow(msg):                                  # 卡住的控制页面
            await gate.wait()

        old = asyncio.ensure_future(rt.end(audit_a, broadcast=slow))
        await asyncio.sleep(0.05)                             # 旧 end() 现在挂在最后一次 flush 里
        await start(rt, streamer="marca_demo", audit=audit_b)
        rt.observe([item("b1", "Cy", RED_TEXT, uid="42"), item("b2", "Di", RED_TEXT, uid="43")], now=2000.0)
        before = list(flushes)
        gate.set()
        old_record = await old
        state = (rt.enabled, rt._open, rt._ready, rt.audit is audit_b, rt.streamer)
        assert flushes == before                              # 旧 end() 醒来后没有碰新一场的名单
        assert rt.guard_msgs == 0                             # 旧 flush 剩下的发送不算进新一场的计数
        new_record = await rt.end(audit_b)
        return old_record, state, new_record

    old_record, state, new_record = run(scenario())
    assert state == (True, True, True, True, "marca_demo")
    assert old_record["observed"] == 1 and old_record["counts"]["threat"]["alert"] == 1     # 旧一场自己的计数
    assert new_record is not None and new_record["observed"] == 2                          # 新一场收尾照常，且是自己的
    assert new_record["counts"]["threat"]["alert"] == 2


def test_a_late_old_end_session_does_not_strip_the_next_sessions_audit_block(monkeypatch, tmp_path):
    """同一个形态走完整条 Pipeline：旧一场的 _end_session 卡在守卫的最后一次 flush 里，
    新一场开始、收尾——新一场的 session_end 里必须有自己的 comment_guard。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    real_broadcast = server.broadcast
    state = {"block": False, "blocked": 0}

    async def scenario():
        gate = asyncio.Event()

        async def broadcast(msg):
            if state["block"] and not state["blocked"] and msg.get("type") in ("comment_guard", "guard_event",
                                                                              "guard_mark"):
                state["blocked"] += 1
                await gate.wait()
            await real_broadcast(msg)

        p.server.broadcast = broadcast
        await p._begin_session(ROOM)
        audit_a = p.audit
        await comments(p, item("c1", "Ana", RED_TEXT, uid=UID))
        p.guard._pending_cg["c1"] = {"lvl": "alert", "amb": None, "cats": ["threat"], "sub": "threat",
                                     "tier": "red", "src": "rule", "rv": None, "ev": "g1", "why": []}
        p.guard._cg_sent.clear()
        state["block"] = True
        old = asyncio.ensure_future(p._end_session(audit_a, reason="user_stop"))
        await asyncio.sleep(0.2)
        assert state["blocked"] == 1                           # 旧 _end_session 确实挂在守卫的 flush 里
        p._close_audit(audit_a, "user_stop")                   # _stop_locked 宽限过后做的事
        p.audit = None
        state["block"] = False
        await p._begin_session(ROOM)
        audit_b = p.audit
        newer = asyncio.ensure_future(comments(p, item("c9", "Bea", RED_TEXT, uid="222")))
        await asyncio.sleep(0.05)               # 新一场的 flush 排在旧的那次发送后面（同一把锁），不会插队
        gate.set()
        await old
        await newer
        assert p.guard.enabled and p.guard._open and p.guard.audit is audit_b
        await comments(p, item("c10", "Cy", RED_TEXT, uid="333"))
        await p._end_session(audit_b, reason="user_stop")
        return audit_b

    audit_b = run(scenario())
    end_b = of(audit_records(audit_b), "session_end")[0]
    assert end_b["comment_guard"]["observed"] == 2 and end_b["comment_guard"]["counts"]["threat"]["alert"] == 2


# ---- flush 串行化：先收集的消息先发出去 ----------------------------------------------------------------

def test_concurrent_flushes_send_in_collection_order_so_a_stale_add_cannot_follow_a_remove(monkeypatch, tmp_path):
    rt = new_runtime(monkeypatch, tmp_path)
    server = RecServer()
    sent = []

    async def scenario():
        await start(rt)
        gate = asyncio.Event()

        async def broadcast(msg):
            await server.broadcast(msg)                       # 缓存（config.guard_marked）在发之前就更新了
            if msg["type"] == "guard_mark":
                sent.append((msg["key"], msg["entry"] is not None))
            if len(sent) == 1:
                await gate.wait()                             # 第一条发给一个慢页面，挂在这里

        rt.mark("J", True, nick="J")
        rt.mark("K", True, nick="K")
        first = asyncio.ensure_future(rt.flush(broadcast))
        await asyncio.sleep(0.02)                             # 第一次 flush 卡在发 J；K 的「加入」已收集
        rt.mark("K", False)                                   # 中控又把 K 移出
        second = asyncio.ensure_future(rt.flush(broadcast))
        await asyncio.sleep(0.02)
        gate.set()
        await asyncio.gather(first, second)

    run(scenario())
    assert sent == [("J", True), ("K", True), ("K", False)]   # K 的移出在后，不会被旧的「加入」盖掉
    assert list(server.config["guard_marked"]) == ["J"] == list(rt.marks.viewers)


def test_a_flush_waiting_for_the_lock_does_not_send_for_the_next_session(monkeypatch, tmp_path):
    rt = new_runtime(monkeypatch, tmp_path)
    sent = []

    async def scenario():
        await start(rt)
        gate = asyncio.Event()

        async def slow(msg):
            sent.append(msg["type"])
            await gate.wait()

        rt.mark("J", True, nick="J")
        rt.observe([item("c1", "Ana", RED_TEXT, uid=UID)], now=1000.0)
        first = asyncio.ensure_future(rt.flush(slow))
        await asyncio.sleep(0.02)
        old_token = rt._token
        waiting = asyncio.ensure_future(rt.flush(slow, final=True, token=old_token))   # 旧一场的收尾 flush 在等锁
        await asyncio.sleep(0.02)
        rt._token += 1                                        # 下一场开始了（begin 做的事）
        rt._pending_marks["Z"] = {"keys": ["Z"]}
        gate.set()
        await asyncio.gather(first, waiting)

    run(scenario())
    assert rt._pending_marks == {"Z": {"keys": ["Z"]}}        # 等锁的旧 flush 没有替下一场把它发掉


# ---- 复核任务不配合取消：收尾最多等 1 秒 ------------------------------------------------------------------

def test_a_review_that_ignores_cancellation_cannot_hang_the_session_end(monkeypatch, tmp_path):
    class StubbornReviewer:
        async def review(self, batch):
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                pass                                          # 吞掉第一次取消，接着睡
            await asyncio.sleep(3600)                         # 退出时 asyncio.run 的最后一次取消仍能收掉它

    rt = new_runtime(monkeypatch, tmp_path)
    rt.reviewer = StubbornReviewer()
    audit = object()
    timings = {}

    async def broadcast(msg):
        pass

    async def scenario():
        await start(rt, audit=audit)
        rt.observe([item("q1", "Ana", ROUTED_TEXT, uid=UID)], now=1000.0)
        await rt.flush(broadcast)
        await asyncio.sleep(0.05)
        assert rt._review_task is not None and not rt._review_task.done()
        t0 = time.perf_counter()
        await asyncio.wait_for(rt.end(audit, broadcast=broadcast), timeout=5)
        timings["end"] = time.perf_counter() - t0
        assert not rt._open and rt.audit is None
        await start(rt, audit=audit)
        rt.observe([item("q2", "Bea", ROUTED_TEXT, uid="2")], now=1001.0)
        await rt.flush(broadcast)
        await asyncio.sleep(0.05)
        t0 = time.perf_counter()
        await asyncio.wait_for(rt.close_session(), timeout=5)
        timings["close"] = time.perf_counter() - t0

    run(scenario())
    assert rt.STOP_REVIEW_WAIT_SEC == 1.0
    assert 0.9 <= timings["end"] < 1.6 and 0.9 <= timings["close"] < 1.6    # 等满 1 秒就放弃，不无限等


# ---- 差一点没人钉住的接线：节流消息自己会到、统计循环里的那一跳 ------------------------------------------------

def test_a_throttled_guard_event_arrives_on_its_own_without_any_further_comment(monkeypatch, tmp_path):
    rt = new_runtime(monkeypatch, tmp_path)                    # 真时钟
    rt.CID_MSG_GAP_SEC = rt.EVENT_MSG_GAP_SEC = 0.05            # 把「每秒一条」缩短，别拖慢测试
    sent = []

    async def broadcast(msg):
        sent.append(msg)

    def events():
        return [m["ev"]["id"] for m in sent if m["type"] == "guard_event"]

    async def scenario():
        await start(rt)
        rt.observe([item("a1", "Ana", RED_TEXT, uid="111"), item("b1", "Bea", RED_TEXT, uid="222")],
                   now=time.time())                             # 一次来两个事件
        await rt.flush(broadcast)
        assert events() == ["g1"]                               # 全局节流：先发一个
        for _ in range(60):                                     # 之后没有新弹幕、没有统计循环：只靠定时器
            if len(events()) == 2:
                break
            await asyncio.sleep(0.05)
        await rt.close_session()

    run(scenario())
    assert events() == ["g1", "g2"]


def test_the_stats_loop_runs_the_guard_tick(monkeypatch, tmp_path):
    """词表被改的提示、名单 last_seen 的悄悄刷新只在统计循环那一跳里：把那一跳摘掉，这里要红。"""
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    for f in (ROOT / "tests" / "fixtures" / "comment_guard").glob("*.txt"):
        (rules_dir / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    p, server = make_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(cg, "DEFAULTS_DIR", rules_dir)

    async def scenario():
        await p._begin_session(ROOM)
        target = rules_dir / "insult.txt"
        target.write_text(target.read_text(encoding="utf-8") + "\nzzpalabra\n", encoding="utf-8")
        task = asyncio.ensure_future(p._stats_loop(interval=0.02))
        for _ in range(100):
            if guard_notices(server):
                break
            await asyncio.sleep(0.02)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await p._end_session(p.audit, reason="user_stop")

    run(scenario())
    assert guard_notices(server) == ["rules_changed"]


def test_the_stats_loop_source_calls_the_guard_tick():
    source = ast.parse((ROOT / "app" / "pipeline.py").read_text(encoding="utf-8"))
    loop = [n for n in ast.walk(source) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_stats_loop"][0]
    assert "_guard_tick" in _self_calls(loop)


# ---- 名单文件读不了、写不进去：提示要说真话 ----------------------------------------------------------------

def test_an_unreadable_marked_file_gets_a_notice_that_does_not_claim_a_backup(monkeypatch, tmp_path):
    folder = tmp_path / "marked"
    folder.mkdir()
    target = folder / "luna_demo.json"
    target.write_text(json.dumps({"version": 1, "streamer": "luna_demo", "viewers": {
        UID: {"keys": [UID], "nick": "Ana", "by": "manual", "first": 1, "last_seen": 1, "hits": 0}}}),
        encoding="utf-8")
    real = type(target).read_bytes

    def deny(self):
        if self == target:
            raise PermissionError("denied")
        return real(self)

    monkeypatch.setattr(type(target), "read_bytes", deny)
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def scenario():
        await p._begin_session(ROOM)
        p.guard.mark("777", True, nick="Zed")
        await p._end_session(p.audit, reason="user_stop")

    run(scenario())
    assert guard_notices(server) == ["marks_unreadable"]
    text = guard_notice("marks_unreadable")
    assert "备份" not in str(text) and "backup" not in text.en.lower()
    assert "不会存盘" in str(text) and "won’t be saved" in text.en           # 说清楚这一场的改动存不下来
    assert not list(folder.glob("*.bak-*")) and json.loads(target.read_text())["viewers"].keys() == {UID}


def test_a_failing_disk_is_announced_once_per_failure_and_again_after_it_recovers(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    real = comment_marks._write_safe
    disk = {"ok": False}
    monkeypatch.setattr(comment_marks, "_write_safe",
                        lambda path, text: real(path, text) if disk["ok"] else "OSError: no space")

    async def scenario():
        await p._begin_session(ROOM)
        marks = p.guard.marks
        p.guard.mark("100", True, nick="A")
        await marks.flush_now()
        await p._guard_tick()
        await p._guard_tick()
        assert guard_notices(server) == ["marks_write_failed"]       # 同一段失败只说一次
        disk["ok"] = True
        await marks.flush_now()                                       # 好了
        assert not marks.failing and (tmp_path / "marked" / "luna_demo.json").exists()
        await p._guard_tick()
        disk["ok"] = False
        p.guard.mark("200", True, nick="B")
        await marks.flush_now()
        await p._guard_tick()
        assert guard_notices(server) == ["marks_write_failed", "marks_write_failed"]   # 又坏了：再说一次
        disk["ok"] = True
        await p._end_session(p.audit, reason="user_stop")

    run(scenario())
    assert sorted(json.loads((tmp_path / "marked" / "luna_demo.json").read_text())["viewers"]) == ["100", "200"]
    assert guard_notices(server) == ["marks_write_failed", "marks_write_failed"]       # 收尾时写成功了：不再提示


def test_a_disk_that_is_still_failing_at_the_end_is_announced_after_the_stop(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(comment_marks, "_write_safe", lambda path, text: "OSError: no space")

    async def scenario():
        await p._begin_session(ROOM)
        await comments(p, item("a1", "Ana", RED_TEXT, uid=UID))      # 自动加入：名单里有一个人要落盘
        await p._end_session(p.audit, reason="user_stop")

    run(scenario())
    assert guard_notices(server) == ["marks_write_failed"]
    assert p.guard.marks.write_errors >= 1 and not (tmp_path / "marked" / "luna_demo.json").exists()


# ---- why 不丢：红条要有依据 ---------------------------------------------------------------------------------

def test_every_lexicon_entry_survives_as_a_why_item_and_the_long_ones_are_clipped_not_dropped():
    from app.comments import WHY_ITEM_MAX, clean_guard

    rules = cg.load_rules()                                           # 出厂词表（含最长的正则条目）
    longest = 0
    for (section, sub), terms in rules.entries.items():
        for term in terms:
            why = "%s/%s:%s" % (section, sub, term)
            longest = max(longest, len(why))
            kept = clean_guard({"lvl": "alert", "why": [why]})["why"]
            assert len(kept) == 1 and len(kept[0]) <= WHY_ITEM_MAX, why
            assert kept[0] == why if len(why) <= WHY_ITEM_MAX else kept[0] == why[:WHY_ITEM_MAX - 1] + "…"
    assert longest > WHY_ITEM_MAX                                     # 确实有被截的，这条测试不是空转


def test_a_confirmed_red_comment_keeps_its_reason_on_the_wire(monkeypatch, tmp_path):
    """出厂词表里最长的正则条目有 580 多字：红条的 why 不能因为它太长就变成空列表。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(cg, "DEFAULTS_DIR", ROOT / "comment_guard" / "defaults")

    async def scenario():
        await p._begin_session(ROOM)
        await comments(p, item("c1", "Ana", "no compren aqui", uid=UID))

    run(scenario())
    guard = server.of_type("comment")[0]["guard"]
    assert guard["lvl"] == "alert" and guard["why"]                    # 有依据；具体是哪条词条不钉（词表归业务改）


# ---- 其它写在报告里、却没有测试钉住的行为 --------------------------------------------------------------------

def test_a_duplicate_longer_than_300_chars_escalates_the_row_accept_actually_shows(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    head = "hola " * 70                                                  # 前 300 字完全一样（accept 只认这一段）

    async def scenario():
        await p._begin_session(ROOM)
        await comments(p, item("c1", "Ana", head + "uno", uid=UID))
        await comments(p, item("c2", "Ana", head + "visita https://evil.example.com/x", uid=UID))

    run(scenario())
    shown = server.of_type("comment")
    assert [m["id"] for m in shown] == ["c1"]                             # accept 去重：第二条没有显示
    updates = server.of_type("comment_guard")
    assert [u["id"] for u in updates] == ["c1"]                           # 标注打在显示出来的那一条上
    assert updates[0]["guard"]["lvl"] == "alert" and updates[0]["guard"]["cats"] == ["promo_link"]


def test_events_go_out_never_sent_first_then_higher_level_then_oldest(monkeypatch, tmp_path):
    clock = {"t": 1000.0}
    rt = new_runtime(monkeypatch, tmp_path, clock=lambda: clock["t"])
    sent = []

    async def broadcast(msg):
        if msg["type"] == "guard_event":
            sent.append(msg["ev"]["id"])

    async def scenario():
        await start(rt)
        rt.observe([item("a1", "Ana", THREATS[0], uid="1")], now=1000.0)           # g1：红条
        await rt.flush(broadcast)
        assert sent == ["g1"]
        rt.observe([item("a2", "Ana", THREATS[1], uid="1")], now=1001.0)           # g1 又有更新（已发过）
        for i in range(5):                                                         # b1：琥珀的多账号同句事件（没发过）
            rt.observe([item("m%d" % i, "Acc %d" % i, "quiero el combo rosa", uid="3%d" % i)], now=1002.0 + i * 0.1)
        rt.observe([item("a3", "Bea", THREATS[2], uid="2")], now=1003.0)           # g2：红条（没发过）
        brigade = [eid for eid in rt.events if eid.startswith("b")]
        assert len(brigade) == 1 and rt.events[brigade[0]]["lvl"] == "amber"
        assert set(rt._ev_dirty) == {"g1", "g2", brigade[0]}
        for step in range(1, 4):
            clock["t"] = 1000.0 + 2 * step
            await rt.flush(broadcast)
        await rt.close_session()
        return brigade[0]

    brigade = run(scenario())
    assert sent == ["g1", "g2", brigade, "g1"]                             # 没发过的先发（红的在琥珀之前），更新过的 g1 最后


def test_a_lead_verdict_only_decides_routing_it_never_gets_a_guard_object_or_a_count(monkeypatch, tmp_path):
    rt = new_runtime(monkeypatch, tmp_path)

    async def scenario():
        await start(rt)
        return rt.observe([item("l1", "Ana", "🤡", uid=UID)], now=1000.0)

    out = run(scenario())
    assert "guard" not in out[0] and rt.counts == {} and rt._ann == {} and rt.events == {}


# ---- PR-4 界面要用的后端：密钥、待机时改名单、清空自动加入的、打开规则文件夹、演示弹幕 ---------------------

PRODUCTION_DEFAULTS = cg.DEFAULTS_DIR            # 模块导入时还没被夹具改过：出厂词表目录


def test_guard_set_saves_the_claude_key_without_switching_the_engine(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path, enabled=True)
    p.args.translator = "auto"
    key = "sk-ant-test-not-a-real-key-4242"

    async def scenario():
        await p.handle_control({"type": "guard_set", "backend": "claude", "key": "  " + key + "  "})
        await p.handle_control({"type": "guard_set", "key": 5})            # 类型不对：忽略
        await p.handle_control({"type": "guard_set", "key": "   "})        # 空白：忽略

    run(scenario())
    assert settings.load_settings()["api_keys"] == {"ANTHROPIC_API_KEY": key}
    assert p.args.translator == "auto"                                      # 翻译引擎没被切换
    assert settings.load_settings().get("translator") is None
    assert p.guard.cfg["backend"] == "claude"
    # 回传页面的只有打码后的尾四位，完整的密钥不出现在任何一条消息里
    assert server.config["engine"]["keys"]["ANTHROPIC_API_KEY"] == "…" + key[-4:]
    assert key not in json.dumps(server.messages, ensure_ascii=False, default=str)
    assert key not in json.dumps(dict(server.config), ensure_ascii=False, default=str)


def test_guard_set_key_is_ignored_while_a_session_is_running(monkeypatch, tmp_path, capsys):
    p, server = make_pipeline(monkeypatch, tmp_path, enabled=True)

    async def scenario():
        await p._begin_session(ROOM)
        await p.handle_control({"type": "guard_set", "key": "sk-ant-test-not-a-real-key-4242"})
        await p._end_session(p.audit, reason="user_stop")

    run(scenario())
    assert "api_keys" not in settings.load_settings()
    assert "直播进行中" in capsys.readouterr().out


def _marked_file(tmp_path):
    return json.loads((tmp_path / "marked" / "luna_demo.json").read_text(encoding="utf-8"))["viewers"]


def test_marked_viewers_can_be_unmarked_between_sessions_and_it_is_written_at_once(monkeypatch, tmp_path):
    """设置分组在直播中整块隐藏，所以「移出」在待机时也得能用：改的是上一场载入的那份名单，马上写盘。"""
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def scenario():
        await p._begin_session(ROOM)
        await comments(p, item("c1", "Ana", RED_TEXT, uid=UID), item("c2", "Bea", "hola buenas", uid="2"))
        await p.handle_control({"type": "guard_mark", "key": "2", "on": True, "nick": "Bea"})
        await p._end_session(p.audit, reason="user_stop")
        assert set(server.config["guard_marked"]) == {UID, "2"}
        assert server.config["guard_state"]["streamer"] == "luna_demo" and server.config["guard_state"]["n_marked"] == 2
        await p.handle_control({"type": "guard_mark", "key": "2", "on": False})
        assert set(server.config["guard_marked"]) == {UID}
        assert set(_marked_file(tmp_path)) == {UID}                       # 不等 2 秒的合并：马上落盘
        assert server.of_type("guard_mark")[-1] == {"type": "guard_mark", "key": "2", "entry": None}
        assert server.config["guard_state"]["n_marked"] == 1
        # 待机时不能加入（没有弹幕可点，名单也不是这一场的）
        await p.handle_control({"type": "guard_mark", "key": "3", "on": True, "nick": "Cai"})
        assert set(server.config["guard_marked"]) == {UID} and set(_marked_file(tmp_path)) == {UID}
        # 下一场开场：名单从文件读回来，是移出之后的样子
        await p._begin_session(ROOM)
        assert set(server.config["guard_marked"]) == {UID}
        await p._end_session(p.audit, reason="user_stop")

    run(scenario())


def test_clear_auto_removes_only_the_automatically_added_ones(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def scenario():
        await p._begin_session(ROOM)
        await comments(p, item("c1", "Ana", RED_TEXT, uid=UID), item("c2", "Bea", "hola buenas", uid="2"))
        await p.handle_control({"type": "guard_mark", "key": "2", "on": True, "nick": "Bea"})
        assert server.config["guard_marked"][UID]["by"] == "auto" and server.config["guard_marked"]["2"]["by"] == "manual"
        await p.handle_control({"type": "guard_clear_auto"})                   # 直播中也能用
        assert set(server.config["guard_marked"]) == {"2"}
        audit = p.audit
        await comments(p, item("c3", "Cai", RED_TEXT, uid="3"))
        await p._end_session(audit, reason="user_stop")
        assert set(server.config["guard_marked"]) == {"2", "3"}
        await p.handle_control({"type": "guard_clear_auto"})                   # 待机时：马上写盘
        assert set(server.config["guard_marked"]) == {"2"} and set(_marked_file(tmp_path)) == {"2"}
        await p.handle_control({"type": "guard_clear_auto"})                   # 已经没有自动加入的：不发消息
        n = len(server.of_type("guard_mark"))
        await p.handle_control({"type": "guard_clear_auto"})
        assert len(server.of_type("guard_mark")) == n
        return audit

    audit = run(scenario())
    # session_end 里只有这一场之内的（待机时清掉的那一个不在审计里）
    assert of(audit_records(audit), "session_end")[0]["comment_guard"]["unmarked_auto"] == 1
    assert [m["key"] for m in server.of_type("guard_mark") if m["entry"] is None] == [UID, "3"]


def test_a_session_with_the_guard_off_leaves_nothing_to_edit_in_standby(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path, enabled=False)

    async def scenario():
        await p._begin_session(ROOM)
        await p._end_session(p.audit, reason="user_stop")
        await p.handle_control({"type": "guard_mark", "key": UID, "on": False})
        await p.handle_control({"type": "guard_clear_auto"})

    run(scenario())
    assert server.guard_messages() == [] and not (tmp_path / "marked").exists()
    assert p.guard.clear_auto() == 0 and p.guard.mark(UID, False) == "disabled"


def test_reenable_has_nothing_to_do_before_model_review_exists(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def scenario():
        await p._begin_session(ROOM)
        before = len(server.messages)
        await p.handle_control({"type": "guard_reenable"})
        assert len(server.messages) == before
        await p._end_session(p.audit, reason="user_stop")

    run(scenario())
    assert p.guard.reenable() is False


def test_open_guard_dir_creates_the_local_folder_and_opens_it(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    opened = []
    p._brand_dir_opener = opened.append
    local = tmp_path / "no-local-dir"
    assert not local.exists()                                          # 平时程序不生成它

    assert p.handle_control({"type": "open_guard_dir"}) is None          # 同步完成，没有要等的东西
    assert local.is_dir() and opened == [local]
    assert server.of_type("notice") == []


def test_open_guard_dir_failure_is_one_paired_notice_not_an_error(monkeypatch, tmp_path, capsys):
    from app import i18n
    p, server = make_pipeline(monkeypatch, tmp_path)

    def boom(path):
        raise OSError("no file manager")

    p._brand_dir_opener = boom
    run(p.handle_control({"type": "open_guard_dir"}))      # 失败时返回一个要等的广播
    assert "打开规则文件夹失败" in capsys.readouterr().out
    assert [m["text"] for m in server.of_type("notice")] == [
        i18n.L("无法打开文件夹：{}", "Couldn’t open the folder: {}").format(tmp_path / "no-local-dir")]


def test_the_demo_comment_stream_goes_through_the_guard_and_leaves_no_trace(monkeypatch, tmp_path):
    """--demo：虚构弹幕走和真实弹幕一样的守卫；关注名单进临时目录，演示完丢掉，真实的名单对象原样还回去。"""
    from app import pipeline_guard
    p, server = make_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(cg, "DEFAULTS_DIR", PRODUCTION_DEFAULTS)          # 演示用的是出厂词表
    monkeypatch.setattr(pipeline_guard, "DEMO_TRANSLATE_DELAY", 0.0)
    monkeypatch.setattr(pipeline_guard, "DEMO_COMMENTS", tuple(
        c[:5] + (0.0,) for c in pipeline_guard.DEMO_COMMENTS))
    total = len(pipeline_guard.DEMO_COMMENTS)
    real_marks = p.guard.marks
    p.guard.EVENT_MSG_GAP_SEC = p.guard.CID_MSG_GAP_SEC = 0.0           # 演示在毫秒内跑完：别让节流压住事件

    async def scenario():
        task = asyncio.ensure_future(p._demo_comment_stream())
        for _ in range(500):
            if len(server.of_type("comment")) >= total + 2:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    run(scenario())
    sent = server.of_type("comment")
    assert len(sent) >= total
    assert all(re.fullmatch(r"\d{19}", c["uid"]) and c["user"] and c["text"] for c in sent)
    assert len(server.of_type("comment_update")) >= total - 1
    assert p.guard.marks is real_marks and not p.guard._open and not p.guard.enabled
    assert not (tmp_path / "marked").exists()                              # 没往真实的 marked/ 里写
    import app.comment_marks as cm
    assert not list(Path(cm.MARKED_DIR).glob("*")) if Path(cm.MARKED_DIR).exists() else True
    guards = [c["guard"] for c in sent if "guard" in c]
    assert guards, "演示弹幕里至少有一条被守卫标出来"
    if ("accuse", "call") in __import__("tests.comment_guard_confirmed", fromlist=["x"]).RED_CONFIRMED:
        assert any(g["lvl"] == "alert" and "accuse_call" in g["cats"] for g in guards)
        assert any(g["lvl"] == "alert" and "promo_link" in g["cats"] for g in guards)
        assert server.of_type("guard_mark"), "号召举报那条红条自动加入了关注名单"
    assert any(e["kind"] == "brigade" for m in server.of_type("guard_event") for e in [m["ev"]] if "kind" in e)


def test_the_demo_stream_with_the_guard_off_only_shows_comments(monkeypatch, tmp_path):
    from app import pipeline_guard
    p, server = make_pipeline(monkeypatch, tmp_path, enabled=False)
    monkeypatch.setattr(pipeline_guard, "DEMO_TRANSLATE_DELAY", 0.0)
    monkeypatch.setattr(pipeline_guard, "DEMO_COMMENTS", tuple(c[:5] + (0.0,) for c in pipeline_guard.DEMO_COMMENTS)[:3])

    async def scenario():
        task = asyncio.ensure_future(p._demo_comment_stream())
        for _ in range(500):
            if len(server.of_type("comment")) >= 3:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    run(scenario())
    assert all("guard" not in c for c in server.of_type("comment")) and server.guard_messages() == []


# ---- 待机时改名单写盘失败、--demo 收尾（PR-4 复审）-----------------------------------------------------------------

@pytest.mark.parametrize("edit", [{"type": "guard_mark", "key": UID, "on": False}, {"type": "guard_clear_auto"}])
def test_a_standby_edit_whose_write_fails_is_announced_and_retried_at_the_next_start(monkeypatch, tmp_path, edit):
    """设置分组在直播中整块隐藏，「移出」「清空自动加入的」是待机时做的：统计循环那时没有在跑，写盘失败要在这里
    告诉页面（打包后的程序没有终端）；没写成的改动也不能被下一场的载入悄悄丢掉。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    real = comment_marks._write_safe
    disk = {"ok": True}
    monkeypatch.setattr(comment_marks, "_write_safe",
                        lambda path, text: real(path, text) if disk["ok"] else "OSError: no space")

    async def scenario():
        await p._begin_session(ROOM)
        await comments(p, item("c1", "Ana", RED_TEXT, uid=UID))          # 自动加入
        await p._end_session(p.audit, reason="user_stop")
        assert set(_marked_file(tmp_path)) == {UID} and guard_notices(server) == []
        disk["ok"] = False
        await p.handle_control(edit)
        assert server.config["guard_marked"] == {}                          # 页面上已经移出了
        assert p.guard.marks.failing and set(_marked_file(tmp_path)) == {UID}
        assert guard_notices(server) == ["marks_write_failed"]              # 所以必须说一声
        await p.handle_control({"type": "guard_mark", "key": "9", "on": False})   # 同一段失败里再改：不重复说
        assert guard_notices(server) == ["marks_write_failed"]
        disk["ok"] = True
        await p._begin_session(ROOM)                                        # 磁盘好了：下一场开场先把没写的写掉
        assert server.config["guard_marked"] == {} and _marked_file(tmp_path) == {}
        await p._end_session(p.audit, reason="user_stop")

    run(scenario())
    assert guard_notices(server) == ["marks_write_failed"]


def _run_the_demo_to_the_end(monkeypatch, p, server):
    from app import pipeline_guard
    monkeypatch.setattr(cg, "DEFAULTS_DIR", PRODUCTION_DEFAULTS)
    monkeypatch.setattr(pipeline_guard, "DEMO_TRANSLATE_DELAY", 0.0)
    monkeypatch.setattr(pipeline_guard, "DEMO_COMMENTS", tuple(c[:5] + (0.0,) for c in pipeline_guard.DEMO_COMMENTS))
    total = len(pipeline_guard.DEMO_COMMENTS)
    p.guard.EVENT_MSG_GAP_SEC = p.guard.CID_MSG_GAP_SEC = 0.0

    async def scenario():
        task = asyncio.ensure_future(p._demo_comment_stream())
        for _ in range(500):
            if len(server.of_type("comment")) >= total + 2:
                break
            await asyncio.sleep(0.01)
        assert server.config["guard_marked"], "演示进行中，演示的关注名单在页面上"
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    run(scenario())


def test_after_the_demo_stops_the_settings_row_does_not_keep_the_demo_list(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    _run_the_demo_to_the_end(monkeypatch, p, server)
    assert server.config["guard_marked"] == {} and server.config["guard_events"] == {}
    assert server.config["guard_state"]["n_marked"] == 0 and not server.config["guard_state"]["streamer"]
    assert server.of_type("config")[-1]["guard_marked"] == {}              # 页面也收到了，不只是缓存


def test_after_the_demo_the_standby_controls_work_on_the_real_list_not_the_demo_one(monkeypatch, tmp_path):
    """不经 main.py 也走得到的顺序：同一个进程里先有过一场真实的，再演示。演示完设置行回到真实的名单，
    「清空自动加入的」改的就是页面上这份。"""
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def real_session():
        await p._begin_session(OTHER_ROOM)
        await comments(p, item("c1", "Ana", RED_TEXT, uid=UID))
        await p._end_session(p.audit, reason="user_stop")

    run(real_session())
    assert set(server.config["guard_marked"]) == {UID}
    _run_the_demo_to_the_end(monkeypatch, p, server)
    assert set(server.config["guard_marked"]) == {UID}                       # 演示的人不在，真实的还在
    assert server.config["guard_state"]["streamer"] == "marca_demo" and server.config["guard_state"]["n_marked"] == 1
    run(p.handle_control({"type": "guard_clear_auto"}))
    assert server.config["guard_marked"] == {}
    assert json.loads((tmp_path / "marked" / "marca_demo.json").read_text(encoding="utf-8"))["viewers"] == {}


@pytest.mark.parametrize("guard_on", [True, False])
def test_the_demo_leaves_no_temp_folder_behind_and_only_makes_one_when_the_guard_is_on(monkeypatch, tmp_path, guard_on):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    made = []
    real_mkdtemp = tempfile.mkdtemp
    monkeypatch.setattr(tempfile, "mkdtemp", lambda *a, **k: made.append(1) or real_mkdtemp(*a, **k))
    p, server = make_pipeline(monkeypatch, tmp_path, enabled=guard_on)
    from app import pipeline_guard
    monkeypatch.setattr(pipeline_guard, "DEMO_TRANSLATE_DELAY", 0.0)
    monkeypatch.setattr(pipeline_guard, "DEMO_COMMENTS", tuple(c[:5] + (0.0,) for c in pipeline_guard.DEMO_COMMENTS)[:3])

    async def scenario():
        task = asyncio.ensure_future(p._demo_comment_stream())
        for _ in range(500):
            if len(server.of_type("comment")) >= 3:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    run(scenario())
    assert len(made) == (1 if guard_on else 0)
    assert list(scratch.iterdir()) == []


# ---- 模型复核（PR-5）接线：设置、密钥、识别健康探针、自检那一行 -----------------------------------------------------

def test_choosing_claude_with_a_key_builds_the_reviewer_and_the_key_never_reaches_the_page(monkeypatch, tmp_path):
    from app import comment_review as cr
    p, server = make_pipeline(monkeypatch, tmp_path)
    key = "sk-ant-test-wiring-0123456789"

    async def scenario():
        await p.handle_control({"type": "guard_set", "backend": "claude"})
        assert p.guard.reviewer is None and server.config["guard_state"]["status"] == "no_key"
        await p.handle_control({"type": "guard_set", "key": key})            # 只补了密钥，选择没变
        assert isinstance(p.guard.reviewer, cr.ClaudeReviewer)
        assert server.config["guard_state"]["status"] == "claude"
        await p.handle_control({"type": "guard_set", "backend": "none"})
        assert p.guard.reviewer is None and server.config["guard_state"]["status"] == "rules"

    run(scenario())
    assert settings.load_settings()["api_keys"]["ANTHROPIC_API_KEY"] == key        # 和翻译引擎同一把密钥
    assert key not in json.dumps(server.messages) and key not in json.dumps(server.config, default=str)
    # 重启后：设置里选的是 none，密钥还在；再选 Claude 的话重启就能直接用
    again, _ = make_pipeline(monkeypatch, tmp_path, enabled=None)
    assert again.guard.cfg["backend"] == "none" and again.guard.reviewer is None
    settings.save_setting("comment_guard", {"enabled": True, "backend": "claude", "weak": False})
    third, _ = make_pipeline(monkeypatch, tmp_path, enabled=None)
    assert isinstance(third.guard.reviewer, cr.ClaudeReviewer) and third.guard.state()["status"] == "claude"


def test_the_health_probe_reads_the_live_backlog_and_whether_recognition_is_in_flight(monkeypatch, tmp_path):
    p, _ = make_pipeline(monkeypatch, tmp_path)
    assert p.guard._probe_health() == (0.0, False)
    p.telemetry.audio_backlog_sec = 12.5
    p._asr_inflight = time.monotonic()
    assert p.guard._probe_health() == (12.5, True)


def test_the_model_review_code_is_not_reachable_from_the_recognition_hot_path():
    """I1 的补充：复核（comment_review）和守卫一样，不出现在识别、翻译、报警的热路径里。"""
    tree = ast.parse((ROOT / "app" / "pipeline.py").read_text(encoding="utf-8"))
    names = {"comment_review", "reviewer", "review_once", "_review_loop", "_queue_review", "tick_review"}
    hot = {"asr_worker", "translation_worker", "_emit_original", "_translate_and_update"}
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in hot:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Attribute) and sub.attr in names or isinstance(sub, ast.Name) and sub.id in names:
                    hits.append(node.name)
    assert hits == []


def test_run_selfcheck_adds_a_comment_review_row_that_is_never_worse_than_ok(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    p.guard.cfg["backend"] = "claude"                      # 选了 Claude 而没有密钥：这一行说清楚，但不变成警告

    async def scenario():
        await p.run_selfcheck()

    run(scenario())
    rows = {c["name"]: c for c in server.config["selfcheck"]["checks"]}
    row = rows["弹幕留意"]
    assert row["level"] == "ok" and "密钥" in row["detail"] and "规则" in row["detail"]
