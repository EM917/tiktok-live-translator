"""Pipeline 这一侧：手机「重译」按钮落到 self.retranslate() 之前的那一段。

这个文件不测 retranslate() 本身怎么翻（那是 test_translation_quality.py 等的
事）——它测的是 on_action 回调的校验、串行化、排队上限、留痕，四件事都是
ViewerHub 那边（tests/test_viewer_hub.py）钉不住的，因为 ViewerHub 从不
import pipeline，也从不知道 self._recent / self.retranslate 是什么。

一条不变式贯穿全文件：中控桌面那颗「重译」按钮走 handle_control ->
self.retranslate() 直接调用，完全不经过这里的信号量和排队计数——手机点得
再快，也不该让桌面端等一等。
"""
import asyncio
from types import SimpleNamespace

import pytest

from app import pipeline as pipeline_mod
from app import settings as settings_mod
from app.pipeline import Pipeline


class StubServer:
    port = 8765

    def __init__(self):
        self.config = {}
        self.messages = []
        self.viewer = None
        self.history = []
        self.alerts = []
        self.comments = []

    async def status(self, state, detail="", command=None):
        self.messages.append({"type": "status", "state": state, "detail": detail})

    async def broadcast(self, msg):
        self.messages.append(msg)


class FakeAudit:
    def __init__(self):
        self.records = []

    def viewer_action(self, action, seq, ip):
        self.records.append(("viewer_action", action, seq, ip))


@pytest.fixture
def pl(monkeypatch, tmp_path):
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", tmp_path / "settings.json")
    terms = tmp_path / "banned_terms.txt"
    terms.write_text("", encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms)
    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source="es", beam=5,
        context=False, asr_temperature=None, glossary=None, backend="auto",
        model=None, device="auto", compute_type="auto", denoise="off",
        banned_terms=None)
    return Pipeline(args, StubServer())


def run(coro):
    return asyncio.run(coro)


# ---- 校验：合法 id 才往下传，且 trigger 恰好是 "viewer" ----
def test_a_known_id_calls_retranslate_with_trigger_viewer(pl):
    pl._recent[7] = {"text": "hola", "lang": "es", "target": "zh-CN"}
    calls = []

    async def fake_retranslate(seq, trigger="manual"):
        calls.append((seq, trigger))

    pl.retranslate = fake_retranslate
    result = pl._on_viewer_action("retranslate", {"id": 7}, "192.168.1.50")
    assert asyncio.iscoroutine(result)
    run(result)
    assert calls == [(7, "viewer")]
    assert pl._viewer_action_pending == 0        # 跑完之后计数要归零


def test_an_id_the_program_does_not_recognize_is_ignored(pl):
    """self._recent 是权威表：程序自己都不认得的字幕，不该往下传给强模型。"""
    calls = []

    async def fake_retranslate(seq, trigger="manual"):
        calls.append(seq)

    pl.retranslate = fake_retranslate
    assert pl._on_viewer_action("retranslate", {"id": 999}, "1.2.3.4") is None
    assert calls == []
    assert pl._viewer_action_pending == 0


@pytest.mark.parametrize("action,payload", [
    ("something_else", {"id": 1}),
    ("retranslate", {"id": "not-an-int"}),
    ("retranslate", {"id": True}),
    ("retranslate", "not a dict"),
    ("retranslate", {}),
])
def test_malformed_calls_are_ignored_without_touching_retranslate(pl, action, payload):
    pl._recent[1] = {"text": "x"}
    calls = []
    pl.retranslate = lambda seq, trigger="manual": calls.append(seq)
    assert pl._on_viewer_action(action, payload, "1.2.3.4") is None
    assert calls == []


# ---- 串行化：手机的重译一次只跑一条 ----
def test_viewer_retranslates_are_serialized_by_the_semaphore(pl):
    for i in range(3):
        pl._recent[i] = {"text": "x"}
    concurrency = {"now": 0, "peak": 0}

    async def fake_retranslate(seq, trigger="manual"):
        concurrency["now"] += 1
        concurrency["peak"] = max(concurrency["peak"], concurrency["now"])
        await asyncio.sleep(0.02)
        concurrency["now"] -= 1

    pl.retranslate = fake_retranslate

    async def scenario():
        coros = [pl._on_viewer_action("retranslate", {"id": i}, "1.2.3.4")
                 for i in range(3)]
        await asyncio.gather(*coros)

    run(scenario())
    assert concurrency["peak"] == 1


def test_the_operator_button_does_not_go_through_the_viewer_semaphore(pl):
    """中控桌面走 handle_control -> self.retranslate() 直连，trigger 是
    "manual"，也完全不碰 _viewer_action_pending。"""
    calls = []

    async def fake_retranslate(seq, trigger="manual"):
        calls.append(trigger)

    pl.retranslate = fake_retranslate
    result = pl.handle_control({"type": "retranslate", "id": 5})
    assert asyncio.iscoroutine(result)
    run(result)
    assert calls == ["manual"]
    assert pl._viewer_action_pending == 0


# ---- 排队上限 ----
def test_pending_count_caps_further_viewer_actions(pl):
    for i in range(pipeline_mod.VIEWER_RETRANSLATE_QUEUE_MAX + 1):
        pl._recent[i] = {"text": "x"}
    gate = asyncio.Event()

    async def fake_retranslate(seq, trigger="manual"):
        await gate.wait()

    pl.retranslate = fake_retranslate

    async def scenario():
        started = [pl._on_viewer_action("retranslate", {"id": i}, "1.2.3.4")
                   for i in range(pipeline_mod.VIEWER_RETRANSLATE_QUEUE_MAX)]
        assert all(c is not None for c in started)
        assert pl._viewer_action_pending == pipeline_mod.VIEWER_RETRANSLATE_QUEUE_MAX
        # 排队已经够多了：下一次点击安静地什么都不返回，不是报错
        overflow = pl._on_viewer_action(
            "retranslate", {"id": pipeline_mod.VIEWER_RETRANSLATE_QUEUE_MAX}, "1.2.3.4")
        assert overflow is None

        tasks = [asyncio.ensure_future(c) for c in started]
        gate.set()
        await asyncio.gather(*tasks)
        assert pl._viewer_action_pending == 0

        # 跑完之后计数归零：下一次点击又能正常入队
        again = pl._on_viewer_action("retranslate", {"id": 0}, "1.2.3.4")
        assert again is not None
        gate.set()
        await again

    run(scenario())


# ---- 留痕 ----
def test_a_viewer_retranslate_writes_the_viewer_action_audit_event(pl):
    pl._recent[3] = {"text": "hola"}
    audit = FakeAudit()
    pl.audit = audit

    async def fake_retranslate(seq, trigger="manual"):
        return None

    pl.retranslate = fake_retranslate
    coro = pl._on_viewer_action("retranslate", {"id": 3}, "9.9.9.9")
    run(coro)
    assert ("viewer_action", "retranslate", 3, "9.9.9.9") in audit.records


def test_the_audit_event_is_still_attempted_when_retranslate_itself_blows_up(pl):
    """审计不能因为强模型那边炸了就整条一起丢——事后要能答「手机点没点过」。"""
    pl._recent[3] = {"text": "hola"}
    audit = FakeAudit()
    pl.audit = audit

    async def boom(seq, trigger="manual"):
        raise RuntimeError("strong model exploded")

    pl.retranslate = boom
    coro = pl._on_viewer_action("retranslate", {"id": 3}, "9.9.9.9")
    with pytest.raises(RuntimeError):
        run(coro)
    assert ("viewer_action", "retranslate", 3, "9.9.9.9") in audit.records
    assert pl._viewer_action_pending == 0          # 计数仍然要还回去
