"""观众弹幕（评论）翻译：只翻译、只显示，绝不进检测/审计链路。

覆盖三件事：
  1. server 侧的白名单闸门——插件（view 来源）只能发 viewer_comments，
     绝不能借道 on_control 操控程序（这道闸门本身在 test_server_origin.py 里测）；
  2. app/comments.py 的批量攒批 / 去重 / 限流 / 让路字幕翻译；
  3. server 的评论历史留存与回放合并（重连不丢、译文能补）。

不 import 其它测试文件，避免耦合到别处的 fixture 变化。
"""
import asyncio
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app import comments as comments_mod
from app import pipeline as pipeline_mod
from app.comments import CommentTranslator
from app.pipeline import Pipeline
from app.server import CaptionServer


class StubServer:
    """照抄 tests/test_pipeline_decoupling.py 的写法：只记消息，不真的起网络。"""

    def __init__(self):
        self.config = {}
        self.messages = []

    async def status(self, state, detail=""):
        self.messages.append({"type": "status", "state": state, "detail": detail})

    async def broadcast(self, msg):
        self.messages.append(msg)

    def of_type(self, t):
        return [m for m in self.messages if m.get("type") == t]


def make_pipeline(monkeypatch, tmp_path, translator=None, terms=()):
    from app import settings
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    terms_file = tmp_path / "banned_terms.txt"
    terms_file.write_text("\n".join(terms), encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms_file)

    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source="es",
        beam=5, context=False, asr_temperature=None, glossary=None, backend="auto", model=None,
        device="auto", compute_type="auto", denoise="off", banned_terms=None,
    )
    server = StubServer()
    p = Pipeline(args, server)
    p.translator = translator
    return p, server


def run(coro):
    return asyncio.run(coro)


class FastTranslator:
    """逐条按序返回 "[zh] " + text，用于验证顺序与对应关系。"""

    def __init__(self):
        self.calls = []

    async def translate(self, text, target, source="auto", glossary=None):
        self.calls.append(text)
        return "[zh] " + text


class OneLineTranslator:
    """故意无视多行输入，永远只回一行——逼 worker 走逐条重译的兜底路径。"""

    def __init__(self):
        self.calls = []

    async def translate(self, text, target, source="auto", glossary=None):
        self.calls.append(text)
        if "\n" in text or text.strip().startswith("1"):
            return "只有一行"
        return "[zh] " + text


class BlockingTranslator:
    """永久不返回，用来测队列溢出与「等字幕翻译让路」。"""

    def __init__(self):
        self.calls = 0

    async def translate(self, text, target, source="auto", glossary=None):
        self.calls += 1
        await asyncio.sleep(3600)
        return "永远不会返回"


async def wait_until(cond, limit=200):
    """有限轮询：最多 limit * 0.01s，绝不无限等。"""
    for _ in range(limit):
        if cond():
            return True
        await asyncio.sleep(0.01)
    return cond()


# ---------------------------------------------------------------------------
# 1. server 白名单闸门：viewer_comments 只进 on_comments，绝不进 on_control
# ---------------------------------------------------------------------------

def test_view_origin_may_send_comments_but_not_control():
    control_calls = []
    comment_calls = []

    async def scenario():
        server = CaptionServer(port=8765)
        server.on_control = lambda data: control_calls.append(data)
        server.on_comments = lambda data: comment_calls.append(data)

        app = web.Application()
        app.router.add_get("/ws", server._ws)
        test_server = TestServer(app)
        client = TestClient(test_server)
        await client.start_server()
        try:
            # view 来源：TikTok 页面，插件以页面身份连接
            ws = await client.ws_connect("/ws", headers={"Origin": "https://www.tiktok.com"})
            await ws.send_json({"type": "start", "url": "https://x"})
            await ws.send_json({"type": "viewer_comments",
                                "items": [{"id": "c1", "user": "u", "text": "hola"}]})
            await asyncio.sleep(0.05)
            await ws.close()

            # 无 Origin 头：非浏览器客户端，走 control
            ws2 = await client.ws_connect("/ws")
            await ws2.send_json({"type": "start", "url": "https://x"})
            await asyncio.sleep(0.05)
            await ws2.close()
        finally:
            await client.close()

    run(scenario())
    # view 来源发的 start 不能被 on_control 收到；无 Origin 头的那次才能
    assert len(control_calls) == 1
    assert control_calls[0]["type"] == "start"
    assert len(comment_calls) == 1
    assert comment_calls[0]["type"] == "viewer_comments"


def test_viewer_comments_rate_limited_per_connection():
    """view 来源不需要经过插件，任何跑在 tiktok.com 页面的脚本都能直连高频灌
    viewer_comments；单条消息条目数虽然被 comments.py 截断，但消息本身的频率
    没有别的限制会挡，因此 server 侧要按连接限频——超频的直接丢，不进 on_comments。
    """
    comment_calls = []

    async def scenario():
        server = CaptionServer(port=8765)
        server.CMT_MSG_RATE_LIMIT = 3
        server.CMT_MSG_RATE_WINDOW_SEC = 1.0
        server.on_comments = lambda data: comment_calls.append(data)

        app = web.Application()
        app.router.add_get("/ws", server._ws)
        test_server = TestServer(app)
        client = TestClient(test_server)
        await client.start_server()
        try:
            ws = await client.ws_connect("/ws", headers={"Origin": "https://www.tiktok.com"})
            for i in range(10):
                await ws.send_json({"type": "viewer_comments",
                                    "items": [{"id": "c{}".format(i), "user": "u", "text": "hola"}]})
            await asyncio.sleep(0.05)
            await ws.close()
        finally:
            await client.close()

    run(scenario())
    # 10 条消息挤在同一秒内，限频为 3 → 只有前 3 条被放行到 on_comments
    assert len(comment_calls) == 3


# ---------------------------------------------------------------------------
# 2. CommentTranslator 行为
# ---------------------------------------------------------------------------

def make_translator(monkeypatch, tmp_path, translator_obj, busy=None, **overrides):
    """直接构造 CommentTranslator（不经 Pipeline），便于单测批量/去重/限流逻辑。"""
    messages = []

    async def broadcast(msg):
        messages.append(msg)

    ct = CommentTranslator(
        broadcast=broadcast,
        translator=lambda: translator_obj,
        target=lambda: "zh-CN",
        glossary=lambda: None,
        busy=busy or (lambda: False),
    )
    for k, v in overrides.items():
        setattr(ct, k, v)
    return ct, messages


def test_comment_translated_in_order(tmp_path, monkeypatch):
    tr = FastTranslator()
    ct, messages = make_translator(monkeypatch, tmp_path, tr)

    async def scenario():
        await ct.accept({"type": "viewer_comments", "items": [
            {"id": "c1", "user": "a", "text": "hola mundo"},
            {"id": "c2", "user": "b", "text": "buenos dias"},
        ]})
        ok = await wait_until(lambda: len([m for m in messages if m.get("type") == "comment_update"]) >= 2)
        assert ok

    run(scenario())
    pendings = [m for m in messages if m.get("type") == "comment" and m.get("state") == "pending"]
    assert [m["id"] for m in pendings] == ["c1", "c2"]
    updates = [m for m in messages if m.get("type") == "comment_update"]
    by_id = {m["id"]: m for m in updates}
    assert by_id["c1"]["state"] == "ok"
    assert by_id["c1"]["translated"] == "[zh] hola mundo"
    assert by_id["c2"]["state"] == "ok"
    assert by_id["c2"]["translated"] == "[zh] buenos dias"


def test_batch_parse_and_fallback(tmp_path, monkeypatch):
    assert comments_mod.parse_batch("1. 甲\n2. 乙", 2) == ["甲", "乙"]
    assert comments_mod.parse_batch("1. 甲\n2. 乙\n3. 丙", 2) is None   # 行数不符

    tr = OneLineTranslator()
    ct, messages = make_translator(monkeypatch, tmp_path, tr)
    ct.BATCH_MAX = 2
    ct.BATCH_WINDOW_SEC = 0.05

    async def scenario():
        await ct.accept({"type": "viewer_comments", "items": [
            {"id": "c1", "user": "a", "text": "hola"},
            {"id": "c2", "user": "b", "text": "adios"},
        ]})
        ok = await wait_until(lambda: len([m for m in messages if m.get("type") == "comment_update"]) >= 2)
        assert ok

    run(scenario())
    updates = {m["id"]: m for m in messages if m.get("type") == "comment_update"}
    # 批量返回单行 → 行数对不上 → 逐条重译，每条仍拿到自己对应的译文
    assert updates["c1"]["translated"] == "[zh] hola"
    assert updates["c2"]["translated"] == "[zh] adios"
    assert tr.calls.count("hola") == 1   # 逐条重译时确实拿单条文本调用了一次


def test_same_language_and_emoji_not_translated(tmp_path, monkeypatch):
    tr = FastTranslator()
    ct, messages = make_translator(monkeypatch, tmp_path, tr)

    async def scenario():
        await ct.accept({"type": "viewer_comments", "items": [
            {"id": "c1", "user": "a", "text": "谢谢主播"},
            {"id": "c2", "user": "b", "text": "😂😂😂"},
        ]})
        await asyncio.sleep(0.1)

    run(scenario())
    assert tr.calls == []
    comment_msgs = {m["id"]: m for m in messages if m.get("type") == "comment"}
    assert comment_msgs["c1"]["state"] == "same"
    assert comment_msgs["c2"]["state"] == "same"
    assert not any(m.get("type") == "comment_update" for m in messages)


def test_needs_translation_helper():
    assert comments_mod.needs_translation("hola amigos", "zh-CN") is True
    assert comments_mod.needs_translation("谢谢主播", "zh-CN") is False
    assert comments_mod.needs_translation("😂😂😂", "zh-CN") is False
    assert comments_mod.needs_translation("hi", "zh-CN") is True    # 恰好2个字母，够格
    assert comments_mod.needs_translation("h", "zh-CN") is False    # 少于2个字母
    assert comments_mod.needs_translation("1", "zh-CN") is False    # 0个字母
    assert comments_mod.needs_translation("hola amigos", "es") is True


def test_queue_drops_oldest_when_full(tmp_path, monkeypatch):
    tr = BlockingTranslator()
    ct, messages = make_translator(monkeypatch, tmp_path, tr)
    ct.MAX_QUEUE = 3

    async def scenario():
        for i in range(6):
            await ct.accept({"type": "viewer_comments", "items": [
                {"id": "c%d" % i, "user": "u", "text": "texto numero %d aqui" % i},
            ]})
        ok = await wait_until(lambda: any(
            m.get("type") == "comment_update" and m.get("state") == "dropped"
            for m in messages))
        assert ok

    run(scenario())
    dropped = [m for m in messages if m.get("type") == "comment_update" and m.get("state") == "dropped"]
    assert dropped
    # 被丢的是最早的 id（c0 先入队，队满后先被挤掉）
    assert dropped[0]["id"] == "c0"


def test_comments_wait_for_subtitle_translation(tmp_path, monkeypatch):
    """字幕翻译永远优先：p._subtitle_busy 非零时，弹幕 worker 原地等，不抢引擎。"""
    tr = FastTranslator()
    p, server = make_pipeline(monkeypatch, tmp_path, translator=tr)
    p._subtitle_busy = 1

    async def scenario():
        await p.handle_viewer_comments({"type": "viewer_comments", "items": [
            {"id": "c1", "user": "a", "text": "hola mundo"},
        ]})
        await asyncio.sleep(0.3)
        assert tr.calls == []          # 字幕翻译占线时弹幕翻译不动
        p._subtitle_busy = 0
        ok = await wait_until(lambda: len(tr.calls) >= 1)
        assert ok

    run(scenario())


def test_invalid_payload_ignored(tmp_path, monkeypatch):
    tr = FastTranslator()
    ct, messages = make_translator(monkeypatch, tmp_path, tr)

    async def scenario():
        # items 不是 list
        n1 = await ct.accept({"type": "viewer_comments", "items": "not a list"})
        # 元素不是 dict
        n2 = await ct.accept({"type": "viewer_comments", "items": ["not a dict"]})
        # 缺 id
        n3 = await ct.accept({"type": "viewer_comments", "items": [{"user": "a", "text": "hola"}]})
        # text 为空 / 非字符串
        n4 = await ct.accept({"type": "viewer_comments", "items": [
            {"id": "c1", "user": "a", "text": ""},
            {"id": "c2", "user": "a", "text": None},
        ]})
        # 超长 text 被截到 300
        long_text = "a" * 500
        n5 = await ct.accept({"type": "viewer_comments", "items": [
            {"id": "c3", "user": "a", "text": long_text},
        ]})
        await asyncio.sleep(0.05)
        return n1, n2, n3, n4, n5

    n1, n2, n3, n4, n5 = run(scenario())
    assert n1 == 0
    assert n2 == 0
    assert n3 == 0
    assert n4 == 0
    assert n5 == 1
    comment_msgs = [m for m in messages if m.get("type") == "comment"]
    assert len(comment_msgs) == 1
    assert len(comment_msgs[0]["text"]) == 300


def test_no_translator_shows_original(tmp_path, monkeypatch):
    """engine=none（p.translator is None）时只显示原文，不入翻译队列。"""
    p, server = make_pipeline(monkeypatch, tmp_path, translator=None)

    async def scenario():
        n = await p.handle_viewer_comments({"type": "viewer_comments", "items": [
            {"id": "c1", "user": "a", "text": "hola mundo"},
        ]})
        await asyncio.sleep(0.1)
        return n

    n = run(scenario())
    assert n == 1
    comment_msgs = server.of_type("comment")
    assert comment_msgs[0]["state"] == "skipped"
    assert not server.of_type("comment_update")   # 不入队


def test_dedupe_by_id_and_content(tmp_path, monkeypatch):
    tr = FastTranslator()
    ct, messages = make_translator(monkeypatch, tmp_path, tr)

    async def scenario():
        n1 = await ct.accept({"type": "viewer_comments", "items": [
            {"id": "c1", "user": "a", "text": "hola mundo"},
        ]})
        n2 = await ct.accept({"type": "viewer_comments", "items": [
            {"id": "c1", "user": "a", "text": "hola mundo"},   # 同 id
        ]})
        n3 = await ct.accept({"type": "viewer_comments", "items": [
            {"id": "c2", "user": "a", "text": "hola mundo"},   # 同 (user,text)，不同 id
        ]})
        await asyncio.sleep(0.05)
        return n1, n2, n3

    n1, n2, n3 = run(scenario())
    assert n1 == 1
    assert n2 == 0
    assert n3 == 0
    comment_msgs = [m for m in messages if m.get("type") == "comment"]
    assert len(comment_msgs) == 1


# ---------------------------------------------------------------------------
# 3. server 侧评论历史留存与回放合并
# ---------------------------------------------------------------------------

def test_server_keeps_comment_history_with_translation():
    async def scenario():
        server = CaptionServer(port=8765)
        await server.broadcast({"type": "comment", "id": "c1", "user": "a", "text": "hola",
                                "ts": 1.0, "translated": None, "state": "pending"})
        await server.broadcast({"type": "comment_update", "id": "c1",
                                "translated": "[zh] hola", "state": "ok"})
        return server

    server = run(scenario())
    stored = [c for c in server.comments if c.get("id") == "c1"]
    assert stored
    assert stored[0]["translated"] == "[zh] hola"
    assert stored[0]["state"] == "ok"
