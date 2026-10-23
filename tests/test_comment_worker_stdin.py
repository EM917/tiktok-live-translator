"""弹幕子进程从 stdin 首行拿登录态（专家审查 S8 第 1 点，按计划审查 P2 改过）。

以前 sessionid 走命令行参数（--session-id），`ps aux` 谁都看得见，agent 跑一次 ps 就会把它打进
对话记录。现在父进程起子进程后往 stdin 写一行 JSON：登录时 {"session_id", "tt_target_idc"}，
匿名时 {}；写完不关 stdin，子进程靠它的 EOF 判断父进程还在不在（_watch_parent）。

这里钉的是子进程那一半（app/comment_worker.py）：
- 首行按字节读、一个字节都不多读，后面的留给看门狗；
- 5 秒读不到首行按匿名连，不卡住；
- 首行没写完父进程就没了（真读到 EOF）：子进程自己退出，不去连 TikTok；
- 读 stdin 出错不算父进程没了：按匿名连，父进程在不在交给看门狗；
- 首行的内容（登录凭据）一个字都不打出来。

全程不 import 真的 TikTokLive、不联网。唯一真起子进程的那条用一个假的 TikTokLive 包挡在前面：
万一首行处理出错往下走了，也只会在 import 时失败，碰不到网络。"""
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
    先关写端再关读端：后台还卡在 read 上的读线程先收到 EOF、自己退出。"""

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


def test_an_overlong_line_stops_reading_and_means_anonymous(pipe, capsys):
    r, w = pipe.r, pipe.w
    os.write(w, b"x" * (cw.HANDSHAKE_MAX_BYTES + 10))     # 没有换行、比上限还长
    assert cw._read_handshake(fd=r, timeout=5) == {}
    assert "太长" in capsys.readouterr().err


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

