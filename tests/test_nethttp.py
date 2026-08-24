"""read_all：把响应体读到 EOF。

同一个 bug 在这个仓库里犯过两次——降噪模型下载和直播页兜底解析，症状完全
不同（一个是「降噪永远关着」，一个是「每个直播间都解析失败」），根因都是
`content.read(n)` 只返回缓冲区里现有的字节，不保证读满 n。
"""
import asyncio

from app.nethttp import read_all


class FakeContent:
    """模拟 aiohttp 的分块行为：每次 read 只吐一块，不管你要多少。"""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, _n):
        return self._chunks.pop(0) if self._chunks else b""


class FakeResp:
    def __init__(self, chunks):
        self.content = FakeContent(chunks)


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_reads_past_the_first_chunk():
    """核心回归：单次 read 只会拿到第一块，正是两次事故的成因。"""
    resp = FakeResp([b"<html>", b"...flv...", b"</html>"])
    assert run(read_all(resp, 1024)) == b"<html>...flv...</html>"


def test_empty_body():
    assert run(read_all(FakeResp([]), 1024)) == b""


def test_returns_none_when_over_limit():
    """上限是防撑爆内存的护栏，超了要明确返回 None，不能给出半截数据。"""
    resp = FakeResp([b"x" * 600, b"x" * 600])
    assert run(read_all(resp, 1000)) is None


def test_limit_not_exceeded_is_fine():
    resp = FakeResp([b"x" * 500, b"x" * 400])
    assert len(run(read_all(resp, 1000))) == 900
