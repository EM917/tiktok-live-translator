"""弹幕留意接线（PR-3）几个测试文件共用的小件：测试词表、造守卫运行态、造弹幕条目。

不是测试文件（不以 test_ 开头）。词表用 tests/fixtures/comment_guard 的测试词表（红条档在里面是打开的），
不依赖出厂词表的内容；主播名、昵称、账号一律是虚构的（luna_demo、Ana、Marca Ejemplo 这一类）。
"""
import itertools
import json
from pathlib import Path

from app import comment_guard as cg
from app.comment_marks import MarkStore
from app.pipeline_guard import GuardRuntime
from app.server import CaptionServer

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures" / "comment_guard"
STREAMER = "luna_demo"

UID = "7234567890123456789"          # 19 位：超过 JS 的 2^53，一律当字符串
RED_TEXT = "te voy a matar"          # 测试词表里 insult/threat：红条档、已确认
LINK_TEXT = "te mando link https://evil.example.com/x"      # 链接：红条，但是结构信号
ROUTED_TEXT = "es estafa o no"       # note + route == "upgrade"：会进复核候选


def use_fixture_rules(monkeypatch, tmp_path):
    """让 comment_guard.load_rules / load_trusted 读测试词表，本机叠加目录指向一个不存在的目录。"""
    monkeypatch.setattr(cg, "DEFAULTS_DIR", FIXTURES)
    monkeypatch.setattr(cg, "LOCAL_DIR", Path(tmp_path) / "no-local-dir")


def virtual_mono():
    """每次读数前进 1 秒的单调时钟：规则预算（滚动 1 秒 50 ms）不会被慢机器上的紧循环触发。
    要测预算的用例自己传 mono。"""
    ticks = itertools.count()
    return lambda: float(next(ticks))


class VirtualPerf:
    """量单条判定耗时的假时钟：读数自己不走，机器卡一下、垃圾回收一下，都不会让某一条显得「慢」。
    要测「慢规则」的用例用 advance() 把耗时注进去（在被量的那一段里调）。"""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def virtual_perf():
    return VirtualPerf()


def new_runtime(monkeypatch, tmp_path, enabled=True, **kw):
    use_fixture_rules(monkeypatch, tmp_path)
    kw.setdefault("mono", virtual_mono())
    kw.setdefault("perf", virtual_perf())
    runtime = GuardRuntime(marks=MarkStore(directory=Path(tmp_path) / "marked"), **kw)
    runtime.cfg["enabled"] = enabled
    runtime.REVIEW_WINDOW_SEC = 0.0                 # 复核的攒批窗口（生产里 2 秒）：测试里不等
    return runtime


async def start(runtime, streamer=STREAMER, audit=None):
    """begin + bind，返回 begin 给 session_start 的字典。audit 不传就用一个占位对象。"""
    info = await runtime.begin(streamer)
    runtime.bind(audit if audit is not None else object())
    return info


def item(cid, user, text, uid=None, uid_src="id", **extra):
    """一条抓取端给的弹幕。uid=None 且没给 handle：只有昵称的人。"""
    out = {"id": cid, "user": user, "text": text}
    if uid is not None:
        out["uid"], out["uid_src"] = uid, uid_src
    out.update(extra)
    return out


class RecServer(CaptionServer):
    """真服务端的缓存逻辑加上一份广播记录（没有页面连着）。"""

    def __init__(self):
        super().__init__(port=0)
        self.messages = []

    async def broadcast(self, msg):
        self.messages.append(dict(msg))             # 记一份拷贝：缓存里的 comment 之后会被就地改写
        await super().broadcast(msg)

    def of_type(self, mtype):
        return [m for m in self.messages if m.get("type") == mtype]

    def guard_messages(self):
        return [m for m in self.messages if m.get("type") in ("comment_guard", "guard_event", "guard_mark")]


class FakeSource:
    def __init__(self):
        self.comments_received = 0
        self.comment_fields = {"n": 3, "has": {"id": 3}}

    def start(self, unique_id):
        pass

    async def stop(self):
        pass


def audit_records(audit):
    return [json.loads(line) for line in Path(audit.path).read_text(encoding="utf-8").splitlines() if line]


def of(records, rtype):
    return [r for r in records if r.get("type") == rtype]
