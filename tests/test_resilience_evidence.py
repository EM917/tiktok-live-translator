"""合规证据这一组保险：报警能不能送到屏幕、审计能不能真的留下、留不下时有没有人知道。

逐条对应的缺口（均已离线复现过）：
  3  一个不读消息的页面把广播和识别循环一起卡死（界面仍显示「直播中」）；
  20/27  磁盘满或 logs/ 被移走时，审计写入被静默吞掉，报警照常上屏却没有留痕；
  35 logs/ 写不进去时整场没有审计文件，自检只给一条折叠起来的黄色提醒；
  36 窗口被盖住时新报警没有任何提示，审计也答不出「当时有没有页面开着」；
  37 settings.json 坏了被当成空的，下一次保存把密钥连同原文件一起抹掉；
  38 报警面板混着上一场的报警，只留 50 条却看起来像全部；
  39 直播中关窗口不确认，进程可能等不到 session_end 就退出。
"""
import asyncio
import base64
import errno
import json
import os
import socket
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from app import alert_notify, selfcheck, settings, window_attention, window_close
from app import audit as audit_mod
from app import pipeline as pipeline_mod
from app.audit import AuditLog, clean_error
from app.pipeline import Pipeline
from app.server import CaptionServer

# 界面文案只写观察和能做的事，不贴原因标签（CLAUDE.md 第八条）
CAUSE_LABELS = ("年龄", "限流", "封禁", "多半")


def run(coro):
    return asyncio.run(coro)


def records(path):
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]


def res(text):
    return SimpleNamespace(text=text, raw_text=text, language="es", rejected=[])


def assert_plain(text):
    assert text and not any(w in text for w in CAUSE_LABELS), text


class RecordingServer(CaptionServer):
    def __init__(self):
        super().__init__(port=8765)
        self.messages = []

    async def broadcast(self, msg):
        self.messages.append(dict(msg))
        await super().broadcast(msg)

    def of_type(self, t):
        return [m for m in self.messages if m.get("type") == t]

    def incidents(self, key):
        return [m for m in self.of_type("incident") if m.get("id") == key]


class HealthyWS:
    def __init__(self):
        self.got = []

    async def send_json(self, msg):
        self.got.append(msg)


class StuckWS:
    """一个不读消息的页面：发送永远等不到缓冲区腾出来。"""

    def __init__(self):
        self.calls = 0

    async def send_json(self, msg):
        self.calls += 1
        await asyncio.get_running_loop().create_future()


class FakeTransport:
    def __init__(self, buffered=0):
        self.buffered = buffered
        self.aborted = False

    def get_write_buffer_size(self):
        return self.buffered

    def abort(self):
        self.aborted = True


class QuietNotifier:
    def __init__(self):
        self.alerts = 0

    def note_alert(self):
        self.alerts += 1
        return False

    def send(self):
        raise AssertionError("测试里不该真的发系统通知")


def make_pipeline(monkeypatch, tmp_path, terms=("cura el cancer",), server=None):
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(settings, "_corrupt", {"backup": None, "announced": False})
    terms_file = tmp_path / "banned_terms.txt"
    terms_file.write_text("\n".join(terms), encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms_file)
    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source="es",
        beam=5, context=False, asr_temperature=None, glossary=None, backend="auto",
        model=None, device="auto", compute_type="auto", denoise="off", banned_terms=None,
        comments=False)
    server = server or RecordingServer()
    p = Pipeline(args, server)
    p.translator = None
    p._alert_notifier = QuietNotifier()

    async def nothing(*a, **k):
        return None

    p._provision_then_check = nothing      # 后台备模型/自检不是这里要测的
    p._translate_alert = nothing           # 报警上下文的强模型翻译同理
    return p, server


# ---------------------------------------------------------------------------
# 3：一个不读消息的页面不能卡住广播
# ---------------------------------------------------------------------------

def test_a_page_that_stops_reading_cannot_hold_up_the_broadcast():
    s = CaptionServer(port=8765)
    s.SEND_TIMEOUT_SEC = 0.2
    stuck, healthy = StuckWS(), HealthyWS()
    stuck_tr = FakeTransport(buffered=70000)
    s.clients = {stuck, healthy}
    s._transports = {stuck: stuck_tr, healthy: FakeTransport()}
    dropped = []
    s.on_client_dropped = lambda reason, buffered: dropped.append((reason, buffered))

    async def go():
        t0 = time.monotonic()
        await s.broadcast({"type": "alert", "term": "x", "alert_id": 1})
        first = time.monotonic() - t0
        await s.broadcast({"type": "caption", "id": 1, "original": "hola"})
        return first

    elapsed = run(go())
    assert elapsed < 1.5                          # 上限 0.2 秒，不是「直到页面关掉」
    assert stuck_tr.aborted                       # abort，不是等缓冲区发完的 close
    assert stuck not in s.clients and stuck not in s._transports
    assert [m["type"] for m in healthy.got] == ["alert", "caption"]
    assert dropped == [("send_timeout", 70000)]


def test_a_page_with_megabytes_unread_is_dropped_without_waiting():
    s = CaptionServer(port=8765)                  # 保持默认 2 秒上限：等了就会超过 1 秒
    stuck, healthy = StuckWS(), HealthyWS()
    stuck_tr = FakeTransport(buffered=s.MAX_CLIENT_BUFFER + 1)
    s.clients = {stuck, healthy}
    s._transports = {stuck: stuck_tr, healthy: FakeTransport()}
    dropped = []
    s.on_client_dropped = lambda reason, buffered: dropped.append((reason, buffered))

    async def go():
        t0 = time.monotonic()
        await s.broadcast({"type": "stats"})
        return time.monotonic() - t0

    assert run(go()) < 1.0
    assert stuck.calls == 0 and stuck_tr.aborted
    assert dropped == [("buffer_full", s.MAX_CLIENT_BUFFER + 1)]
    assert len(healthy.got) == 1


def test_two_broadcasts_stuck_on_the_same_page_report_it_once():
    s = CaptionServer(port=8765)
    s.SEND_TIMEOUT_SEC = 0.2
    stuck = StuckWS()
    s.clients = {stuck}
    s._transports = {stuck: FakeTransport(buffered=70000)}
    dropped = []
    s.on_client_dropped = lambda reason, buffered: dropped.append(reason)

    async def go():
        await asyncio.gather(s.broadcast({"type": "stats"}),
                             s.broadcast({"type": "caption", "id": 1}))

    run(go())
    assert dropped == ["send_timeout"]


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.mark.skipif(sys.platform == "win32",
                    reason="靠缩小内核缓冲区制造背压；Windows 的缓冲行为不同，逻辑由上面的假对象测试覆盖")
def test_real_socket_that_never_reads_is_aborted_and_the_healthy_page_keeps_receiving():
    import aiohttp

    async def go():
        port = _free_port()
        s = CaptionServer(port=port)
        s.SEND_TIMEOUT_SEC = 0.3
        dropped = []
        s.on_client_dropped = lambda reason, buffered: dropped.append(reason)
        await s.start()
        raw = socket.socket()
        raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        session = aiohttp.ClientSession()
        try:
            raw.connect(("127.0.0.1", port))
            key = base64.b64encode(os.urandom(16)).decode()
            raw.sendall((
                "GET /ws HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nUpgrade: websocket\r\n"
                "Connection: Upgrade\r\nSec-WebSocket-Key: {}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n".format(port, key)).encode())
            ws = await session.ws_connect("http://127.0.0.1:{}/ws".format(port))
            ids = set()

            async def reader():
                async for m in ws:
                    if m.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(m.data)
                        if data.get("type") == "comment":
                            ids.add(data["id"])

            reading = asyncio.ensure_future(reader())
            for _ in range(200):
                if len(s.clients) == 2:
                    break
                await asyncio.sleep(0.02)
            assert len(s.clients) == 2
            raw_port = raw.getsockname()[1]
            for tr in s._transports.values():
                if tr.get_extra_info("peername")[1] == raw_port:
                    tr.get_extra_info("socket").setsockopt(
                        socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            payload = "x" * 32000
            worst, n = 0.0, 0
            while not dropped and n < 400:
                n += 1
                t0 = time.monotonic()
                await s.broadcast({"type": "comment", "id": n, "text": payload})
                worst = max(worst, time.monotonic() - t0)
            assert dropped in (["send_timeout"], ["buffer_full"])
            assert worst < 2.5
            assert len(s.clients) == 1 and len(s._transports) == 1
            # 被断开的连接必须真的断掉（abort）：只是不再给它发的话，页面解冻后守着一条
            # 静默的连接，既收不到新报警，也不会重连去补回。实测断开后约 10 毫秒读到 EOF
            raw.setblocking(False)
            closed, deadline = False, time.monotonic() + 2.0
            while not closed and time.monotonic() < deadline:
                try:
                    closed = raw.recv(65536) == b""
                except BlockingIOError:
                    await asyncio.sleep(0.01)
                except ConnectionError:           # RST 也是断开
                    closed = True
            assert closed
            for _ in range(300):
                if len(ids) >= n:
                    break
                await asyncio.sleep(0.02)
            assert len(ids) == n                  # 正常的页面一条没少
            reading.cancel()
            await ws.close()
        finally:
            raw.close()
            await session.close()
            await asyncio.wait_for(s._runner.cleanup(), 10)

    run(go())


def test_a_dropped_page_is_audited_and_printed(tmp_path, capsys):
    p = Pipeline.__new__(Pipeline)
    p.server = CaptionServer(port=8765)
    p.server.clients = {HealthyWS()}
    p.audit = AuditLog(room_url="https://www.tiktok.com/@bella/live", log_dir=tmp_path)
    p.on_ui_client_dropped("send_timeout", 70000)
    p.audit.close()
    rec = [r for r in records(p.audit.path) if r["type"] == "ui_client_dropped"][0]
    assert rec["reason"] == "send_timeout" and rec["buffered_bytes"] == 70000
    assert rec["clients_left"] == 1 and rec["at"]
    out = capsys.readouterr().out
    assert "已断开" in out
    assert_plain(out)


# ---------------------------------------------------------------------------
# 20/27：审计写不进去——计数、回调、留住报警、恢复后补 audit_gap
# ---------------------------------------------------------------------------

class FlakyFile:
    """包住真实句柄：full 时每次写都抛 ENOSPC；partial 表示下一次写只成功写这么多字节。"""

    def __init__(self, real):
        self.real = real
        self.full = False
        self.partial = 0

    def write(self, b):
        if self.partial:
            n, self.partial = self.partial, 0
            return self.real.write(bytes(b[:n]))
        if self.full:
            raise OSError(errno.ENOSPC, "No space left on device")
        return self.real.write(b)

    def __getattr__(self, name):
        return getattr(self.real, name)


def _flaky_log(tmp_path):
    log = AuditLog(room_url="https://www.tiktok.com/@bella/live", log_dir=tmp_path)
    flaky = FlakyFile(log._fh)
    log._fh = flaky
    return log, flaky


def test_disk_full_is_counted_alerts_are_kept_and_a_gap_is_written_on_recovery(tmp_path):
    log, flaky = _flaky_log(tmp_path)
    calls = []
    log.on_write_error = calls.append
    log.segment(0, res("antes"), 1.0, 100.0, [])
    flaky.full = True
    for i in range(1, 31):
        log.segment(i, res("hola"), 1.0, 100.0, [])
    log.alert({"term": "cura el cancer", "tier": "exact", "context": "cura el cancer"})
    assert log.failing and log.failing_since
    assert log.write_failures == 31 and log.lost_records == 30
    assert len(calls) == 1 and "No space" in calls[0]["error"]    # 一次中断只回调一次
    flaky.full = False
    log.segment(99, res("despues"), 1.0, 100.0, [])
    assert not log.failing
    log.close()
    recs = records(log.path)
    types = [r["type"] for r in recs]
    assert types == ["session_start", "segment", "audit_gap", "alert", "segment",
                     "session_end"]
    gap = recs[2]
    assert gap["lost_records"] == 30 and gap["retained_records"] == 1
    assert gap["from"] and gap["to"] and "No space" in gap["error"]
    assert [r["seq"] for r in recs if r["type"] == "segment"] == [0, 99]
    assert log.last_gap == gap


def test_the_kept_records_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_mod, "RETAIN_MAX", 3)
    log, flaky = _flaky_log(tmp_path)
    flaky.full = True
    for i in range(5):
        log.alert({"term": "t{}".format(i), "tier": "exact", "context": ""})
    assert len(log._retained) == 3 and log.lost_records == 2
    flaky.full = False
    log.close()
    recs = records(log.path)
    assert [r["term"] for r in recs if r["type"] == "alert"] == ["t0", "t1", "t2"]
    assert [r for r in recs if r["type"] == "audit_gap"][0]["lost_records"] == 2


def test_a_write_that_fails_halfway_leaves_no_torn_line(tmp_path):
    log, flaky = _flaky_log(tmp_path)
    flaky.full = True
    flaky.partial = 7                               # 写进 7 个字节后磁盘满
    log.segment(1, res("hola"), 1.0, 100.0, [])
    flaky.full = False
    log.segment(2, res("otra"), 1.0, 100.0, [])
    log.close()
    recs = records(log.path)                        # 每一行都能解析
    assert [r["type"] for r in recs] == ["session_start", "audit_gap", "segment",
                                         "session_end"]


def test_every_new_outage_is_reported_again(tmp_path):
    log, flaky = _flaky_log(tmp_path)
    calls = []
    log.on_write_error = calls.append
    flaky.full = True
    log.segment(1, res("a"), 1.0, 1.0, [])
    log.segment(2, res("b"), 1.0, 1.0, [])
    flaky.full = False
    log.segment(3, res("c"), 1.0, 1.0, [])
    flaky.full = True
    log.segment(4, res("d"), 1.0, 1.0, [])
    assert len(calls) == 2
    flaky.full = False
    log.close()
    assert [r["type"] for r in records(log.path)].count("audit_gap") == 2


def test_disk_still_full_at_close_does_not_raise(tmp_path):
    log, flaky = _flaky_log(tmp_path)
    flaky.full = True
    log.alert({"term": "x", "tier": "exact", "context": ""})
    log.close()                                     # session_end 也写不进去：不抛
    assert log.failing and log.write_failures == 2


def test_unopenable_log_dir_records_the_reason(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    log = AuditLog(room_url="u", log_dir=blocker / "logs")
    assert log.path is None and log.open_error
    log.alert({"term": "x", "tier": "exact", "context": ""})     # 空操作，不抛
    log.close()


def test_error_text_drops_url_query_strings():
    text = clean_error("GET https://pull-flv.tiktokcdn.com/a.flv?sign=abc&token=secret failed")
    assert "sign=" not in text and "secret" not in text
    assert "https://pull-flv.tiktokcdn.com/a.flv" in text


@pytest.mark.skipif(os.name == "nt", reason="Windows 上打开着的文件所在目录改不了名")
def test_moving_the_log_folder_mid_session_is_noticed(tmp_path):
    logs = tmp_path / "logs"
    log = AuditLog(room_url="u", log_dir=logs)
    assert not log.detached()
    logs.rename(tmp_path / "moved")
    assert log.detached()
    log.close()


def _watched_pipeline(tmp_path, monkeypatch, free=100 * 1024 ** 3):
    p = Pipeline.__new__(Pipeline)
    p.server = RecordingServer()
    box = {"free": free}
    monkeypatch.setattr(Pipeline, "_log_free_bytes", staticmethod(lambda audit: box["free"]))
    return p, box


def test_a_write_failure_on_another_thread_raises_one_banner_and_recovery_replaces_it(
        tmp_path, monkeypatch):
    p, _ = _watched_pipeline(tmp_path, monkeypatch)

    async def go():
        p.audit = AuditLog(room_url="https://www.tiktok.com/@bella/live", log_dir=tmp_path)
        await p._watch_audit("bella")
        hooked = p.audit.on_write_error is not None
        flaky = FlakyFile(p.audit._fh)
        p.audit._fh = flaky
        flaky.full = True
        writer = threading.Thread(target=lambda: [
            p.audit.segment(i, res("hola"), 1.0, 1.0, []) for i in range(20)])
        writer.start()
        writer.join()
        for _ in range(100):
            if p.server.incidents("session:audit-write"):
                break
            await asyncio.sleep(0.01)
        # 横幅要来自写失败回调（切回事件循环），不是等 _stats_loop 10 秒一次的轮询
        immediate = list(p.server.incidents("session:audit-write"))
        await p._check_audit_health()               # 仍在失败：不重复提示
        await p._check_audit_health()
        failing = p.server.incidents("session:audit-write")
        flaky.full = False
        p.audit.segment(21, res("ya"), 1.0, 1.0, [])
        await p._check_audit_health()
        await p._check_audit_health()
        return hooked, immediate, failing

    hooked, immediate, failing = run(go())
    assert hooked
    assert len(immediate) == 1 and immediate[0]["level"] == "error"
    assert len(failing) == 1 and failing[0]["level"] == "error"
    assert "审计日志写不进去" in failing[0]["text"] and "No space" in failing[0]["text"]
    assert_plain(failing[0]["text"])
    all_msgs = p.server.incidents("session:audit-write")
    assert len(all_msgs) == 2
    assert all_msgs[1]["level"] == "warn" and "已恢复写入" in all_msgs[1]["text"]
    assert "20 条记录没有留下" in all_msgs[1]["text"]
    assert_plain(all_msgs[1]["text"])
    p.audit.close()


def test_a_late_callback_from_an_old_session_does_not_touch_this_sessions_banner(tmp_path):
    p = Pipeline.__new__(Pipeline)
    p.server = RecordingServer()
    old = AuditLog(room_url="u", log_dir=tmp_path / "a")

    async def go():
        p.audit = AuditLog(room_url="u", log_dir=tmp_path / "b")
        await p._watch_audit("bella")
        p._audit_write_failed(old)
        await asyncio.sleep(0.05)

    run(go())
    assert not p.server.incidents("session:audit-write")
    old.close()
    p.audit.close()


def test_low_disk_warns_once_and_clears_with_margin(tmp_path, monkeypatch):
    p, box = _watched_pipeline(tmp_path, monkeypatch, free=int(0.8 * 1024 ** 3))

    async def go():
        p.audit = AuditLog(room_url="u", log_dir=tmp_path)
        await p._watch_audit("bella")
        await p._check_audit_health()
        await p._check_audit_health()               # 仍然偏低：不重复
        box["free"] = int(1.2 * 1024 ** 3)          # 过了 1 GB 但没到撤销线：不闪
        await p._check_audit_health()
        box["free"] = 5 * 1024 ** 3
        await p._check_audit_health()

    run(go())
    msgs = p.server.incidents("session:disk-low")
    assert [m["level"] for m in msgs] == ["warn", "clear"]
    assert "0.8 GB" in msgs[0]["text"]
    assert_plain(msgs[0]["text"])
    p.audit.close()


def test_the_health_check_never_breaks_the_stats_loop(tmp_path, monkeypatch, capsys):
    p, _ = _watched_pipeline(tmp_path, monkeypatch)

    async def go():
        p.audit = AuditLog(room_url="u", log_dir=tmp_path)
        await p._watch_audit("bella")

        def boom():
            raise RuntimeError("stat 挂了")

        p.audit.detached = boom
        await p._check_audit_health()
        await p._check_audit_health()

    run(go())
    assert capsys.readouterr().out.count("审计健康检查出错") == 1
    p.audit.close()


def test_the_stats_loop_runs_the_audit_check(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    seen = []

    async def check():
        seen.append(1)

    p._check_audit_health = check

    async def go():
        task = asyncio.ensure_future(p._stats_loop(interval=0.02))
        for _ in range(100):
            if seen:
                break
            await asyncio.sleep(0.02)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    run(go())
    assert seen


# ---------------------------------------------------------------------------
# 35：logs/ 写不进去——自检 FAIL 带修法，开播挂红条
# ---------------------------------------------------------------------------

def test_unwritable_logs_is_a_selfcheck_failure_with_a_fix(monkeypatch, tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(audit_mod, "LOG_DIR", blocker / "logs")
    c = asyncio.run(selfcheck.check_audit())
    assert c["level"] == selfcheck.FAIL
    assert "logs/ 不可写" in c["detail"] and c["fix"]


def test_audit_fix_lines():
    assert "受控文件夹访问" in selfcheck._audit_fix(True)
    assert selfcheck._audit_fix(False).startswith('sudo chown -R "$(whoami)" ')


def test_a_session_without_an_audit_file_shows_a_red_banner(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(audit_mod, "LOG_DIR", blocker / "logs")

    async def go():
        await p._begin_session("https://www.tiktok.com/@bella/live")
        assert p.audit.path is None
        await p._end_session()
        banner = dict(server.config["incidents"]["session:audit-open"])
        await p._clear_session_incidents()          # 下一场开始时清掉
        return banner

    banner = run(go())
    assert banner["level"] == "error" and "不会留下任何证据" in banner["text"]
    assert_plain(banner["text"])
    assert "session:audit-open" not in server.config["incidents"]


# ---------------------------------------------------------------------------
# 36 / 38：报警带主播、场次、在线页面数；上一场的不删；本场总数
# ---------------------------------------------------------------------------

def test_alerts_carry_streamer_session_page_count_and_session_total(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def go():
        await p._begin_session("https://www.tiktok.com/@bella/live")
        first_session = server.config["alerts_session"]["session"]
        first_path = p.audit.path
        await p._emit_original(res("esto cura el cancer"), audio_end_ts=100.0, asr_ms=100)
        await p._end_session()
        await p._begin_session("https://www.tiktok.com/@jessy/live")
        server.clients = {HealthyWS(), HealthyWS()}   # 两个页面开着
        await p._emit_original(res("esto cura el cancer"), audio_end_ts=200.0, asr_ms=100)
        second_path = p.audit.path
        await p._end_session()
        return first_session, first_path, second_path

    first_session, first_path, second_path = run(go())
    alerts = server.of_type("alert")
    assert [a["streamer"] for a in alerts] == ["bella", "jessy"]
    assert alerts[0]["session"] == first_session == first_path.stem
    assert alerts[1]["session"] == second_path.stem != first_session
    assert [a["session_total"] for a in alerts] == [1, 1]
    assert [a["ui_clients"] for a in alerts] == [0, 2]
    # 上一场的报警留在服务端（刷新后回放），不因换场被删
    assert [a["streamer"] for a in server.alerts] == ["bella", "jessy"]
    assert server.config["alerts_session"] == {"session": second_path.stem,
                                               "streamer": "jessy", "total": 1}
    configs = [m for m in server.of_type("config") if "alerts_session" in m]
    assert [c["alerts_session"]["streamer"] for c in configs] == ["bella", "jessy"]
    # 审计里的报警记录带同样的几列（但不带只给页面用的 session_total）
    rec = [r for r in records(second_path) if r["type"] == "alert"][0]
    assert rec["ui_clients"] == 2 and rec["streamer"] == "jessy"
    assert rec["session"] == second_path.stem and "session_total" not in rec
    assert p._alert_notifier.alerts == 2


class FakeTitleWindow:
    """pywebview Window 的样子：expose 按 __name__ 挂函数，set_title 改原生标题。"""

    def __init__(self, fail=False):
        self.titles = []
        self.exposed = {}
        self.fail = fail

    def expose(self, *fns):
        for fn in fns:
            self.exposed[fn.__name__] = fn

    def set_title(self, title):
        if self.fail:
            raise RuntimeError("窗口已经关了")
        self.titles.append(title)


def test_the_desktop_window_title_follows_unseen_alerts():
    window = FakeTitleWindow()
    assert window_attention.expose_attention(window) is not None
    call = window.exposed["set_attention"]                 # 页面里是 pywebview.api.set_attention
    assert call(2, "page-a", 1) is True
    assert window.titles == ["(2) 疑似违禁词 · TikTok 直播同传"]   # 与 web/alerts.js 的 alertTitle 一致
    assert call(2, "page-a", 2) is False                  # 条数没变：不重设
    assert call(0, "page-a", 3) is True                   # 中控回到窗口
    assert window.titles[-1] == "TikTok 直播同传"
    assert call(3, "page-a", 3) is False                  # JS 桥里晚到的旧调用：丢掉
    assert call(3, "page-a", 2) is False
    assert window.titles[-1] == "TikTok 直播同传"
    assert call(1, "page-b", 1) is True                   # 刷新后的页面从头数 seq，照认
    assert window.titles[-1].startswith("(1) ")
    assert len(window.titles) == 3


def test_the_window_title_bridge_never_raises_and_retries_after_a_failure():
    window = FakeTitleWindow(fail=True)
    assert window_attention.expose_attention(window) is not None
    call = window.exposed["set_attention"]
    assert call(3, "p", 1) is False and window.titles == []
    window.fail = False
    assert call(3, "p", 2) is True                        # 上次没设上：这次照设
    for bad in ("x", None, float("inf")):
        assert call(bad, "p", 9) is False
    assert call(10 ** 9, "p", 10) is True and window.titles[-1].startswith("(999) ")
    assert window_attention.expose_attention(SimpleNamespace()) is None   # 老 pywebview 没有 expose


def test_os_notification_is_sent_off_the_loop_only_when_a_burst_starts():
    class Notifier:
        def __init__(self):
            self.fresh = [True, False]
            self.sends = []
            self.done = threading.Event()

        def note_alert(self):
            return self.fresh.pop(0)

        def send(self):
            self.sends.append(threading.current_thread())
            self.done.set()

    p = Pipeline.__new__(Pipeline)
    p._alert_notifier = Notifier()

    async def go():
        p._notify_alert_burst()
        p._notify_alert_burst()
        for _ in range(100):
            if p._alert_notifier.done.is_set():
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.05)
        return threading.current_thread()

    loop_thread = run(go())
    assert len(p._alert_notifier.sends) == 1
    assert p._alert_notifier.sends[0] is not loop_thread


def test_notifications_are_one_per_burst_with_a_slow_reminder():
    now = [0.0]
    n = alert_notify.AlertNotifier(clock=lambda: now[0], is_enabled=lambda: True,
                                   runner=lambda *a, **k: None)
    assert n.note_alert() is True
    now[0] = 30.0
    assert n.note_alert() is False
    now[0] = 59.0
    assert n.note_alert() is False
    now[0] = 130.0                                  # 安静了 71 秒：新的一阵
    assert n.note_alert() is True
    fired = 0
    for k in range(1, 41):                          # 之后 20 分钟每 30 秒一条
        now[0] = 130.0 + 30.0 * k
        fired += n.note_alert()
    assert fired == 2                               # 最多每 10 分钟提醒一次


def test_notification_has_no_term_text_and_no_sound():
    mac = alert_notify.command("darwin")
    assert mac[0] == "osascript" and "sound" not in " ".join(mac)
    assert alert_notify.TEXT in mac[-1]
    win = alert_notify.command("win32")
    script = base64.b64decode(win[-1]).decode("utf-16-le")
    assert 'silent="true"' in script and alert_notify.TEXT in script
    assert alert_notify.command("linux", which=lambda name: None) is None
    assert alert_notify.command("linux", which=lambda name: "/usr/bin/notify-send") == [
        "/usr/bin/notify-send", alert_notify.TITLE, alert_notify.TEXT]


def test_notifications_stay_off_unless_the_setting_is_true(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(settings, "_corrupt", {"backup": None, "announced": False})
    calls = []
    n = alert_notify.AlertNotifier(runner=lambda cmd, **k: calls.append(cmd),
                                   platform="darwin")
    assert n.send() is False and calls == []
    settings.save_setting(alert_notify.SETTING_KEY, "yes")
    assert n.send() is False
    settings.save_setting(alert_notify.SETTING_KEY, True)
    assert n.send() is True and calls[0][0] == "osascript"

    def boom(cmd, **k):
        raise OSError("osascript 不在")

    assert alert_notify.AlertNotifier(runner=boom, platform="darwin").send() is False


# ---------------------------------------------------------------------------
# 37：settings.json 坏了——改名备份一次、提示一次、不再被下一次保存抹掉
# ---------------------------------------------------------------------------

@pytest.fixture
def settings_file(monkeypatch, tmp_path):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings, "SETTINGS_FILE", path)
    monkeypatch.setattr(settings, "_corrupt", {"backup": None, "announced": False})
    return path


def test_corrupt_settings_are_renamed_once_and_announced_once(settings_file, tmp_path):
    settings_file.write_text('{"api_keys": {"DEEPL_API_KEY": "k"},', encoding="utf-8")
    assert settings.load_settings() == {}
    assert settings.load_settings() == {}           # 第二次读：文件已不在，不再备份
    backups = list(tmp_path.glob("settings.json.corrupt-*"))
    assert len(backups) == 1 and not settings_file.exists()
    assert settings.corrupt_backup_name() == backups[0].name
    assert settings.take_corrupt_notice() == backups[0].name
    assert settings.take_corrupt_notice() is None   # 每次运行只提示一次
    assert settings.corrupt_backup_name() == backups[0].name


@pytest.mark.parametrize("content", [b"", b"\xff\xfe{", b"{ not json"])
def test_unparsable_settings_are_backed_up(settings_file, tmp_path, content):
    settings_file.write_bytes(content)
    assert settings.load_settings() == {}
    assert len(list(tmp_path.glob("settings.json.corrupt-*"))) == 1


@pytest.mark.parametrize("content", [
    b'\xef\xbb\xbf{"k": 1}',          # 带 BOM 的 UTF-8：PowerShell 5.1 Set-Content -Encoding UTF8
    '{"k": 1}'.encode("utf-16"),     # 带 BOM 的 UTF-16：PowerShell 5.1 的 >、记事本「Unicode」
])
def test_hand_edited_settings_with_a_bom_are_read_not_backed_up(settings_file, tmp_path,
                                                                 content):
    settings_file.write_bytes(content)
    assert settings.load_settings() == {"k": 1}
    assert settings_file.exists()
    assert not list(tmp_path.glob("settings.json.corrupt-*"))
    assert settings.corrupt_backup_name() is None


def test_missing_or_non_object_settings_are_not_backed_up(settings_file, tmp_path):
    assert settings.load_settings() == {}
    settings_file.write_text('["a"]', encoding="utf-8")
    assert settings.load_settings() == {}
    assert not list(tmp_path.glob("settings.json.corrupt-*"))
    assert settings.corrupt_backup_name() is None


def test_save_setting_syncs_before_replacing(settings_file, monkeypatch):
    order = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(settings.os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd)))
    monkeypatch.setattr(settings.os, "replace",
                        lambda a, b: (order.append("replace"), real_replace(a, b)))
    settings.save_setting("k", "v")
    assert order == ["fsync", "replace"]
    assert settings.load_settings() == {"k": "v"}


def test_the_corrupt_settings_banner_shows_once_and_goes_after_an_engine_is_chosen(
        settings_file):
    settings_file.write_text("{", encoding="utf-8")
    settings.load_settings()
    p = Pipeline.__new__(Pipeline)
    p.server = RecordingServer()

    async def go():
        await p._announce_settings_backup()
        await p._announce_settings_backup()
        shown = dict(p.server.config["incidents"]["settings-corrupt"])
        settings.save_setting("translator", "hymt2")
        await p._announce_settings_backup()
        return shown

    shown = run(go())
    assert shown["level"] == "warn" and "settings.json.corrupt-" in shown["text"]
    assert "重新填写" in shown["text"]
    assert_plain(shown["text"])
    assert [m["level"] for m in p.server.incidents("settings-corrupt")] == ["warn", "clear"]


def test_session_start_records_the_settings_backup(monkeypatch, tmp_path):
    (tmp_path / "settings.json").write_text("{", encoding="utf-8")
    p, server = make_pipeline(monkeypatch, tmp_path)     # 构造时读设置 → 备份

    async def go():
        await p._begin_session("https://www.tiktok.com/@bella/live")
        path = p.audit.path
        await p._end_session()
        return path

    path = run(go())
    start = records(path)[0]
    assert start["type"] == "session_start"
    assert start["settings_backup"].startswith("settings.json.corrupt-")
    assert server.config["incidents"]["settings-corrupt"]["level"] == "warn"


# ---------------------------------------------------------------------------
# 39：直播中关窗口先确认；关了先留原因，停得慢也要让 session_end 落盘
# ---------------------------------------------------------------------------

class FakeEvent:
    """pywebview Event 的样子：+= 挂处理函数；触发时任一返回 False 就取消关闭。"""

    def __init__(self):
        self.items = []

    def __add__(self, fn):
        self.items.append(fn)
        return self

    def fire(self):
        return any(fn() is False for fn in self.items)


class FakeStreamPipeline:
    def __init__(self, audit=None, stop_delay=0.0, active=True):
        self.audit = audit
        self.stop_delay = stop_delay
        self.active = active
        self._stop_reason = None
        self.reason_at_stop = "not called"
        self.stopped = False

    def _stream_active(self):
        return self.active

    async def stop_stream(self, quiet=False):
        self.reason_at_stop = self._stop_reason
        await asyncio.sleep(self.stop_delay)
        if self.audit is not None:
            self.audit.close()
            self.audit = None
        self.active = False
        self.stopped = True


def test_close_localization_is_passed_only_where_supported():
    def create_window(title, url, localization=None):
        pass

    def old_create_window(title, url):
        pass

    got = window_close.localization_kwargs(create_window)["localization"]
    assert got["global.quitConfirmation"].startswith("正在监听直播")
    assert got["global.quit"] == "关闭" and got["global.cancel"] == "取消"
    assert window_close.localization_kwargs(old_create_window) == {}


def test_close_confirmation_follows_whether_a_stream_is_active():
    window = SimpleNamespace(events=SimpleNamespace(closing=FakeEvent()), confirm_close=False)
    pipe = FakeStreamPipeline(active=True)
    assert window_close.guard_close(window, pipe) is True
    assert window.events.closing.fire() is False           # 处理函数自己不取消关闭
    assert window.confirm_close is True
    pipe.active = False
    window.events.closing.fire()
    assert window.confirm_close is False                   # 待机时不打扰
    assert window_close.guard_close(SimpleNamespace(), pipe) is False


def test_window_close_records_the_reason_before_stopping(tmp_path):
    audit = AuditLog(room_url="u", log_dir=tmp_path)
    pipe = FakeStreamPipeline(audit=audit, stop_delay=0.01)
    assert run(window_close.close_session(pipe, audit_budget=1.0, total=2.0)) is True
    assert pipe.reason_at_stop == "window_closed"
    types = [r["type"] for r in records(audit.path)]
    assert types == ["session_start", "window_closed", "session_end"]


def test_a_slow_stop_still_gets_session_end_on_disk_in_time(tmp_path):
    audit = AuditLog(room_url="u", log_dir=tmp_path)
    pipe = FakeStreamPipeline(audit=audit, stop_delay=3.0)

    async def go():
        t0 = time.monotonic()
        finished = await window_close.close_session(pipe, audit_budget=0.2, total=0.8)
        return finished, time.monotonic() - t0, pipe.stopped

    finished, elapsed, stopped = run(go())
    assert finished is False and stopped is False
    assert elapsed < 2.0                                   # 远早于停止流程自己结束（3 秒）
    types = [r["type"] for r in records(audit.path)]
    assert types == ["session_start", "window_closed", "session_end"]
    # 慢路径抢先写的 session_end 也要带原因：之后停止流程再关是空操作，补不上
    assert records(audit.path)[-1].get("reason") == "window_closed"


def test_stop_after_close_from_the_window_thread(tmp_path):
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        audit = AuditLog(room_url="u", log_dir=tmp_path)
        pipe = FakeStreamPipeline(audit=audit, stop_delay=0.01)
        assert window_close.stop_after_close(loop, pipe, timeout=3.0, audit_budget=1.0) is True
        assert pipe.stopped and pipe.reason_at_stop == "window_closed"
        assert window_close.stop_after_close(None, pipe) is False
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()
