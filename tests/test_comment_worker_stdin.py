"""弹幕子进程从 stdin 首行拿登录态（专家审查 S8 第 1 点，按计划审查 P2 改过）。

以前 sessionid 走命令行参数（--session-id），`ps aux` 谁都看得见，agent 跑一次 ps 就会把它打进
对话记录。现在父进程起子进程后往 stdin 写一行 JSON：登录时 {"session_id", "tt_target_idc"}，
匿名时 {}；写完不关 stdin，子进程靠它的 EOF 判断父进程还在不在（_watch_parent）。

这里主要钉子进程那一半（app/comment_worker.py）：
- 首行按字节读、一个字节都不多读，后面的留给看门狗；
- 5 秒读不到首行按匿名连，不卡住；
- 首行没写完父进程就没了（真读到 EOF）：子进程自己退出，不去连 TikTok；
- 读 stdin 出错不算父进程没了：按匿名连，父进程在不在交给看门狗；
- 首行的内容（登录凭据）一个字都不打出来。
最后一组走真父进程到真子进程的交接（CommentSource._run_once → _spawn → comment_worker）。

全程不 import 真的 TikTokLive、不联网。真起子进程的几条都用一个假的 TikTokLive 包挡在前面。"""
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import comment_worker as cw

ROOT = Path(__file__).resolve().parent.parent
SID = "fake-session-0123456789abcdef"          # 测试用的假 sessionid


class Pipe:
    """一根真管道。每一端只关一次：关过的 fd 号会被别处复用，二次 close 会关掉别人的文件。
    先关写端再关读端：后台还卡在 read 上的读线程先收到 EOF、自己退出。
    后台还卡在 write 上的写线程正相反，要先关读端，见 BackgroundWrite.finish。"""

    def __init__(self):
        self.r, self.w = os.pipe()
        self._open = {self.r, self.w}
        self._lock = threading.Lock()

    def close(self, fd):
        with self._lock:
            if fd not in self._open:
                return
            self._open.discard(fd)
        os.close(fd)

    def close_all(self):
        self.close(self.w)
        self.close(self.r)


@pytest.fixture
def pipe():
    p = Pipe()
    yield p
    p.close_all()


class BackgroundWrite:
    """在后台线程里往管道写，和读端同时进行——真父进程就是这样：写首行的同时子进程在读。

    比管道缓冲还长的内容不能先在测试线程里整块写完再去读：读端还没开始读，os.write 就一直
    等着有人腾出缓冲。Windows 上 os.pipe() 的缓冲只有几 KB（CreatePipe 不指定大小、用系统
    默认值），HANDSHAKE_MAX_BYTES + 10 就已经装不下：PR #121 的 Windows CI 卡在那一行
    os.write 上 120 秒，pytest-timeout 把整个测试进程掐掉。macOS / Linux 的管道有 64 KB，
    同样的写法在那里看不出来。"""

    def __init__(self, pipe, data):
        self.pipe = pipe
        self.error = None
        self._thread = threading.Thread(target=self._run, args=(data,), name="test-pipe-writer",
                                        daemon=True)
        self._thread.start()

    def _run(self, data):
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(self.pipe.w, view):]
        except OSError as exc:
            self.error = exc

    def finish(self, timeout=30):
        """等写完。写不完（读端停得太早）就先关读端，让卡住的 write 出错返回，再算失败。
        不能先关写端：Windows 的 C 运行库给每个 fd 一把锁，卡在 write 里的线程握着写端那把，
        close(写端) 会跟着一起卡住，测试就又变成挂到超时。"""
        self._thread.join(timeout)
        stuck = self._thread.is_alive()
        if stuck:
            self.pipe.close(self.pipe.r)
            self._thread.join(timeout)
        assert not stuck, "写端一直卡着：读端没把该读的读走"
        assert self.error is None, "写端出错：{!r}".format(self.error)


def _read_to_eof(fd):
    out = bytearray()
    while True:
        chunk = os.read(fd, 4096)
        if not chunk:
            return bytes(out)
        out += chunk


def test_reads_the_login_line_and_leaves_the_rest_for_the_watchdog(pipe):
    r, w = pipe.r, pipe.w
    os.write(w, (json.dumps({"session_id": SID, "tt_target_idc": "useast5"}) + "\nREST").encode())
    got = cw._read_handshake(fd=r, timeout=5)
    assert got == {"session_id": SID, "tt_target_idc": "useast5"}
    # 一个字节都没多读：看门狗接着读到的就是首行之后的内容
    assert os.read(r, 100) == b"REST"


def test_an_empty_object_means_anonymous(pipe):
    r, w = pipe.r, pipe.w
    os.write(w, b"{}\n")
    assert cw._read_handshake(fd=r, timeout=5) == {}


def test_no_first_line_within_the_timeout_means_anonymous(pipe, capsys):
    r = pipe.r                                 # 父进程活着（写端开着），但一个字节都没写
    started = time.monotonic()
    assert cw._read_handshake(fd=r, timeout=0.2) == {}
    assert time.monotonic() - started < 3        # 按超时放行，没有卡住
    err = capsys.readouterr().err
    assert "没收到父进程的首行" in err and "匿名" in err


def test_a_parent_that_dies_halfway_through_the_line_means_exit(pipe, capsys):
    r, w = pipe.r, pipe.w
    os.write(w, b'{"session_id": "' + SID.encode())
    pipe.close(w)                                # 写到一半父进程就没了：管道关了
    assert cw._read_handshake(fd=r, timeout=5) is cw._PARENT_GONE
    assert SID not in capsys.readouterr().err


def test_a_parent_that_dies_before_writing_means_exit(pipe):
    r, w = pipe.r, pipe.w
    pipe.close(w)
    assert cw._read_handshake(fd=r, timeout=5) is cw._PARENT_GONE


@pytest.mark.parametrize("line", [
    b"not json at all " + SID.encode(),
    json.dumps(["session_id", SID]).encode(),
    b"\xff\xfe" + SID.encode(),
])
def test_a_malformed_line_means_anonymous_and_is_never_echoed(pipe, capsys, line):
    r, w = pipe.r, pipe.w
    os.write(w, line + b"\n")
    assert cw._read_handshake(fd=r, timeout=5) == {}
    err = capsys.readouterr().err
    assert "不是 JSON 对象" in err and SID not in err


# 第二档的上限比常见的默认管道缓冲（Windows 几 KB、macOS / Linux 64 KB）都大：mac、Linux 上也走一遍
# 「父进程写的比管道缓冲还多」，测试的写法要是退回「先整块写完再读」，在本机就会卡住，不用等 Windows CI
@pytest.mark.parametrize("limit", [None, 128 * 1024], ids=["real-limit", "beyond-pipe-buffer"])
def test_an_overlong_line_stops_reading_and_means_anonymous(pipe, capsys, monkeypatch, limit):
    if limit is not None:
        monkeypatch.setattr(cw, "HANDSHAKE_MAX_BYTES", limit)
    extra = 10
    writer = BackgroundWrite(pipe, b"x" * (cw.HANDSHAKE_MAX_BYTES + 1 + extra))   # 没有换行、比上限还长
    try:
        got = cw._read_handshake(fd=pipe.r, timeout=30)
    finally:
        writer.finish()          # 断言之前先了结写线程：夹具收尾要先关写端，写线程卡着就会跟着卡住
    assert got == {}
    assert "太长" in capsys.readouterr().err
    pipe.close(pipe.w)
    # 读到上限多一个字节就停：后面的一个字节都没碰，还在管道里
    assert _read_to_eof(pipe.r) == b"x" * extra


def test_a_terminal_stdin_is_not_read(monkeypatch):
    """人在终端里手动跑（stdin 是终端）：不等首行，用命令行参数。"""
    reads = []
    monkeypatch.setattr(cw.os, "isatty", lambda fd: True)
    assert cw._read_handshake(fd=0, timeout=5, read=lambda fd, n: reads.append(n) or b"") == {}
    assert reads == []


def test_a_read_error_means_anonymous_not_that_the_parent_is_gone(capsys):
    """stdin 读不了不等于父进程没了：按匿名连，父进程在不在交给看门狗（stdin 读不了就看 ppid）。
    要是当成父进程没了直接退出，父进程活着时每次都只收到「退出码 0、没有输出」，一直重起。"""
    def broken(fd, n):
        raise OSError("bad fd " + SID)

    assert cw._read_handshake(fd=99999, timeout=5, read=broken) == {}
    err = capsys.readouterr().err
    assert "出错" in err and "OSError" in err and "匿名" in err
    assert SID not in err                        # 只打异常类型，不打异常内容


def test_a_read_error_halfway_through_the_line_still_means_anonymous(capsys):
    chunks = iter([b'{', b'"'])

    def flaky(fd, n):
        try:
            return next(chunks)
        except StopIteration:
            raise OSError("gone mid-line") from None

    assert cw._read_handshake(fd=99999, timeout=5, read=flaky) == {}
    assert "OSError" in capsys.readouterr().err


# ---- main()：首行先于一切 ----------------------------------------------------------------

class _Exit(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _patch_main(monkeypatch, handshake):
    ran = []

    async def fake_run(args):
        ran.append(SimpleNamespace(**vars(args)))
        return 0

    def fake_exit(code):
        raise _Exit(code)

    monkeypatch.setattr(cw, "_utf8_stdio", lambda: None)
    monkeypatch.setattr(cw, "_read_handshake", lambda: handshake)
    monkeypatch.setattr(cw, "_run", fake_run)
    monkeypatch.setattr(cw.os, "_exit", fake_exit)
    return ran


def test_main_exits_quietly_before_connecting_when_the_parent_is_gone(monkeypatch):
    ran = _patch_main(monkeypatch, cw._PARENT_GONE)
    with pytest.raises(_Exit) as exc:
        cw.main(["someone"])
    assert exc.value.code == 0 and ran == []        # 没走到 _run：不会去连 TikTok


def test_main_connects_anonymously_when_stdin_cannot_be_read(monkeypatch, capsys):
    """真的 _read_handshake 遇到读错误：main 照常往下连（匿名），不在连之前 os._exit(0)。"""
    real = cw._read_handshake

    def broken(fd, n):
        raise OSError("bad fd")

    ran = _patch_main(monkeypatch, None)
    monkeypatch.setattr(cw, "_read_handshake", lambda: real(fd=99999, timeout=5, read=broken))
    with pytest.raises(_Exit) as exc:
        cw.main(["someone"])
    assert exc.value.code == 0                      # fake_run 的返回值，说明走到了 _run
    assert len(ran) == 1 and ran[0].session_id is None
    assert "按匿名连接" in capsys.readouterr().err


def test_main_hands_the_session_from_stdin_to_the_client(monkeypatch):
    ran = _patch_main(monkeypatch, {"session_id": SID, "tt_target_idc": "useast5"})
    with pytest.raises(_Exit) as exc:
        cw.main(["someone"])
    assert exc.value.code == 0
    assert ran[0].session_id == SID and ran[0].tt_target_idc == "useast5"


def test_main_anonymous_line_keeps_manual_command_line_values(monkeypatch):
    """--session-id 只留给人手动调试：首行是 {} 时照用命令行上的值；首行带了就以首行为准。"""
    ran = _patch_main(monkeypatch, {})
    with pytest.raises(_Exit):
        cw.main(["someone", "--session-id", "manual"])
    assert ran[0].session_id == "manual"
    ran = _patch_main(monkeypatch, {})
    with pytest.raises(_Exit):
        cw.main(["someone"])
    assert ran[0].session_id is None and ran[0].tt_target_idc is None


def test_apply_handshake_ignores_non_string_values():
    args = SimpleNamespace(session_id=None, tt_target_idc=None)
    cw._apply_handshake(args, {"session_id": 123, "tt_target_idc": "x"})
    assert args.session_id is None
    cw._apply_handshake(args, {"session_id": SID, "tt_target_idc": 5})
    assert args.session_id == SID and args.tt_target_idc is None


def test_the_watchdog_still_sees_eof_after_the_first_line(pipe):
    """首行读完以后，看门狗用同一个 fd 照样能等到父进程关管道。"""
    r, w = pipe.r, pipe.w
    os.write(w, b"{}\n")
    assert cw._read_handshake(fd=r, timeout=5) == {}
    calls = []

    def read():
        while os.read(r, 4096):
            pass
        return b""

    async def scenario():
        threading.Timer(0.1, lambda: pipe.close(w)).start()      # 父进程 0.1 秒后「死掉」
        await cw._watch_parent(on_gone=lambda: calls.append("gone"), read=read,
                               getppid=lambda: 4242, grace=0.01,
                               exit_fn=lambda code: calls.append(("exit", code)))

    asyncio.run(asyncio.wait_for(scenario(), 10))
    assert calls == ["gone", ("exit", 0)]


# ---- 真子进程：写到一半就关管道 -------------------------------------------------------------

def test_a_real_worker_exits_when_the_parent_closes_stdin_halfway(tmp_path):
    stub = tmp_path / "stub" / "TikTokLive"
    stub.mkdir(parents=True)
    # 假的 TikTokLive：万一首行处理出错往下走了，也只在 import 这里失败，绝不联网
    (stub / "__init__.py").write_text("raise ImportError('stub: tests never load the real library')\n",
                                      encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(stub.parent))
    proc = subprocess.Popen([sys.executable, "-m", "app.comment_worker", "fictional_streamer"],
                            cwd=str(ROOT), env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        proc.stdin.write(b'{"session_id": "' + SID.encode())
        proc.stdin.flush()
        proc.stdin.close()
        proc.stdin = None                   # 已经关了：别让 communicate() 再去 flush 它
        out, err = proc.communicate(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert proc.returncode == 0
    assert out == b""                       # 一条 status 都没写：根本没开始连
    assert SID.encode() not in err


# ---- 真父进程 + 真子进程：登录、匿名各起一次 ---------------------------------------------------
# 走真的 CommentSource._run_once → 真的 _spawn → 真的 app.comment_worker，只把 TikTokLive 换成一个
# 不联网的假包：它把子进程真实的 sys.argv 和 set_session 的参数记进一个文件。钉住三件事：
# 命令行里只有主播名、登录态经 stdin 首行到了 set_session、匿名请求只在匿名那次记（登录 0 次，匿名 2 次）。
# 不用 ps：Windows CI 上也要能跑到真管道上的首行交接。

_STUB_INIT = '''
import json, os, sys

_REPORT = os.environ["TLT_TEST_STUB_REPORT"]
_LIVE = os.environ.get("TLT_TEST_STUB_OUTCOME") == "connected"


def _report(**kw):
    with open(_REPORT, "a", encoding="utf-8") as f:
        f.write(json.dumps(kw) + "\\n")


class _Web:
    def set_session(self, session_id, tt_target_idc):
        _report(set_session=[session_id, tt_target_idc])


class TikTokLiveClient:
    def __init__(self, unique_id):
        _report(argv=sys.argv[1:], unique_id=unique_id)
        self.web = _Web()
        self.room_id = 1
        self._handlers = {}

    def on(self, event):
        def register(fn):
            self._handlers[event] = fn
            return fn
        return register

    async def is_live(self):
        return _LIVE

    async def connect(self):
        from TikTokLive.events import ConnectEvent
        await self._handlers[ConnectEvent](ConnectEvent())

    async def disconnect(self):
        pass
'''
_STUB_EVENTS = "class CommentEvent: pass\nclass ConnectEvent: pass\nclass DisconnectEvent: pass\n"


@pytest.mark.parametrize("outcome,returncode", [("offline", 3), ("connected", 0)])
@pytest.mark.parametrize("mode", ["login", "anonymous"])
def test_a_real_parent_hands_the_session_to_a_real_worker_over_stdin(
        tmp_path, monkeypatch, capsys, mode, outcome, returncode):
    from app import resolver
    from app.comment_source import CommentSource

    pkg = tmp_path / "stub" / "TikTokLive"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(_STUB_INIT, encoding="utf-8")
    (pkg / "events.py").write_text(_STUB_EVENTS, encoding="utf-8")
    report = tmp_path / "report.jsonl"
    monkeypatch.setenv("PYTHONPATH", str(pkg.parent))      # 子进程继承：先找到假包，绝不联网
    monkeypatch.setenv("TLT_TEST_STUB_REPORT", str(report))
    monkeypatch.setenv("TLT_TEST_STUB_OUTCOME", outcome)
    notes = []
    monkeypatch.setattr(resolver, "note_anonymous_request", lambda: notes.append(True))

    class Recording(CommentSource):
        spawned = []

        async def _spawn(self, args):
            self.spawned.append(list(args))
            return await super()._spawn(args)           # 真的 _spawn：真起子进程、真管道

    async def on_items(items):
        pass

    async def on_state(state, detail=""):
        pass

    cs = Recording(on_items=on_items, on_state=on_state, cookies_browser="none", root=ROOT)
    session = {"session_id": SID, "tt_target_idc": "useast5"} if mode == "login" else None
    rc, _healthy = asyncio.run(asyncio.wait_for(cs._run_once("fictional_streamer", session), 60))

    assert rc == returncode
    assert cs.spawned == [["fictional_streamer"]]                 # 父进程这边：只给了主播名
    lines = [json.loads(ln) for ln in report.read_text(encoding="utf-8").splitlines()]
    assert lines[0] == {"argv": ["fictional_streamer"], "unique_id": "@fictional_streamer"}
    if mode == "login":
        assert lines[1:] == [{"set_session": [SID, "useast5"]}]   # 登录态经 stdin 首行送到了
        assert notes == []                                         # 登录模式不记匿名请求
    else:
        assert lines[1:] == []
        assert len(notes) == 2      # 启动时一次；连上时、或没连上就退出时再一次
    terminal = capsys.readouterr().out
    assert SID not in terminal
    assert "首行" not in terminal       # 没走超时、读错误、格式不对这几条退路：首行是当场送到的
