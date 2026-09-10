"""2026-09-09 全项目扫描修掉的一批 bug，每条一个回归用例。

来源：静态扫描（ruff bugbear/async 规则 + 手工 grep）与三路子系统审查。
共同点是「静默」——音频静默停摆、识别异常无痕丢段、任务被 GC 悄悄收走、
DNS 失败报成安全限制、Windows 降噪永远起不来却说模型坏了。"""
import asyncio
import json
import socket
import sys
import time
import types
import unicodedata
from datetime import datetime
from types import SimpleNamespace

import pytest

from app import audio, audit, comment_source, detector, diskspace, resolver
from app.ffmpeg_bin import filter_path


# ---- ffmpeg 滤镜参数里的路径转义（Windows 降噪永远起不来的根因）----

def test_filter_path_escapes_windows_and_filter_specials():
    assert filter_path(r"C:\Users\elon\models\bd.rnnn") == r"C\:/Users/elon/models/bd.rnnn"
    assert filter_path("/tmp/a:b,c/bd.rnnn") == r"/tmp/a\:b\,c/bd.rnnn"
    assert filter_path("/plain/path/bd.rnnn") == "/plain/path/bd.rnnn"


# ---- 拉流静默停滞看门狗 ----

class _HangingStdout:
    async def read(self, n):
        await asyncio.sleep(3600)


class _EmptyStderr:
    async def readline(self):
        return b""


class _FakeProc:
    def __init__(self):
        self.stdout = _HangingStdout()
        self.stderr = _EmptyStderr()
        self.returncode = None
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


def _run_frames(monkeypatch, url, model=None):
    captured = {}

    async def fake_exec(*cmd, **kw):
        captured["cmd"] = cmd
        captured["proc"] = _FakeProc()
        return captured["proc"]

    monkeypatch.setattr(audio, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(audio.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(audio.FFmpegAudioSource, "STALL_SEC", 0.05)
    src = audio.FFmpegAudioSource(url, denoise_model=model)

    async def go():
        return [f async for f in src.frames()]

    frames = asyncio.run(asyncio.wait_for(go(), 5))
    return src, captured, frames


def test_stalled_stream_is_cut_off_instead_of_hanging_forever(monkeypatch):
    """半开 TCP 连接：read 永不返回，以前界面一直显示「直播中」而检测无声停摆。"""
    src, captured, frames = _run_frames(monkeypatch, "https://cdn.example/a.flv")
    assert frames == [] and src.stalled is True
    assert captured["proc"].terminated                     # 交给自动重连前先收掉 ffmpeg
    cmd = captured["cmd"]
    assert "-rw_timeout" in cmd and cmd[cmd.index("-i") - 1] == audio.FFmpegAudioSource.RW_TIMEOUT_US


def test_local_file_input_gets_no_http_options_and_escaped_filter(monkeypatch):
    src, captured, _ = _run_frames(monkeypatch, "file:///x.flv", model=r"C:\m\bd.rnnn")
    cmd = captured["cmd"]
    assert "-rw_timeout" not in cmd and "-reconnect" not in cmd
    assert cmd[cmd.index("-af") + 1] == r"highpass=f=70,arnndn=m=C\:/m/bd.rnnn"


# ---- 审计：同秒两场不共用文件；识别异常留痕 ----

def test_audit_files_never_collide_within_a_second(tmp_path):
    p1, fh1 = audit._open_new(tmp_path, "20260101-000000")
    p2, fh2 = audit._open_new(tmp_path, "20260101-000000")
    fh1.close()
    fh2.close()
    assert p1.name == "session-20260101-000000.jsonl"
    assert p2.name == "session-20260101-000000-2.jsonl"
    # 磁盘盘点按文件名判日期，带后缀的也要认得
    assert diskspace._log_time(p2) == datetime(2026, 1, 1, 0, 0, 0)


def test_asr_failure_is_written_to_the_audit(tmp_path):
    log = audit.AuditLog(room_url="https://www.tiktok.com/@x/live", log_dir=tmp_path)
    log.asr_failed(segment_ms=2500.0, error="Metal error", queue_depth=3)
    log.close()
    recs = [json.loads(ln) for ln in log.path.read_text(encoding="utf-8").splitlines()]
    rec = [r for r in recs if r["type"] == "asr_failed"][0]
    assert rec["segment_ms"] == 2500.0 and rec["error"] == "Metal error" and rec["queue_depth"] == 3


# ---- 磁盘删除：服务端自己拒删正在用的模型 ----

def test_delete_refuses_models_in_use_even_if_asked(tmp_path):
    d = tmp_path / "hub" / "models--mlx-community--whisper-large-v3-mlx" / "blobs"
    d.mkdir(parents=True)
    (d / "b").write_bytes(b"x" * 10)

    async def go():
        return await diskspace.delete(
            ["hf:models--mlx-community--whisper-large-v3-mlx"], hf_dir=tmp_path / "hub",
            log_dir=tmp_path / "nologs", active_asr=("mlx", "large-v3"))

    freed, done, failed = asyncio.run(go())
    assert done == [] and freed == 0
    assert failed and "正在使用中" in failed[0]
    assert d.exists()


# ---- 解析：DNS 失败如实说、派生地址被拒不终止链路、cookie 读取有预算、退出码 0 无地址不算下播 ----

def _run(coro):
    return asyncio.run(coro)


def test_dns_failure_is_reported_as_network_not_security(monkeypatch):
    def boom(*a, **k):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises(resolver.ResolveError) as exc:
        _run(resolver._check_media_url("https://pull-flv-x.tiktokcdn-us.com/a.flv"))
    assert exc.value.kind == "network" and "域名解析失败" in str(exc.value)


def test_rejected_derived_address_does_not_end_the_chain(monkeypatch):
    """接口层给了个内网地址（不可信来源）：以前整条解析在第一层就抛错、trace 为空；
    现在记一笔 rejected，继续让 yt-dlp 那层拿到真地址。"""
    monkeypatch.setitem(sys.modules, "yt_dlp", types.ModuleType("yt_dlp"))

    async def api(_url, cookies_browser="auto"):
        return "http://127.0.0.1/inner.flv", False

    async def ytdlp(_url, cookies=None, browser=None, timeout=45):
        return 0, "https://pull.example/a.flv\n", ""

    async def works(url):
        return True

    def public(host, *a, **k):
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(resolver, "_resolve_via_api", api)
    monkeypatch.setattr(resolver, "_webkit_available", lambda: False)
    monkeypatch.setattr(resolver, "_run_ytdlp", ytdlp)
    monkeypatch.setattr(resolver, "_media_url_works", works)
    monkeypatch.setattr(socket, "getaddrinfo", public)
    trace = []
    got = _run(resolver.resolve_stream_url("https://www.tiktok.com/@x/live",
                                           cookies_browser="none", trace=trace))
    assert got == "https://pull.example/a.flv"
    assert ("官方接口", "rejected") in [(r["layer"], r["outcome"]) for r in trace]


def test_cookie_read_has_a_budget(monkeypatch):
    """yt-dlp 读 Chrome cookie 会弹钥匙串对话框，线程可能永远不返回——协程不能陪它等。"""
    monkeypatch.setattr(resolver, "BROWSER_ATTEMPT_TIMEOUT", 0.05)

    def slow(browser):
        time.sleep(0.5)
        return "sessionid=x"

    monkeypatch.setattr(resolver, "_cookie_header", slow)

    async def go():
        # 计时放在协程里：asyncio.run 收尾时会等那个还在睡的线程池线程，
        # 那是测试环境的收尾开销，不是协程等了它
        t0 = time.monotonic()
        got = await resolver._cookie_header_with_budget("chrome")
        return got, time.monotonic() - t0

    got, elapsed = asyncio.run(go())
    assert got is None and elapsed < 0.4


def test_ytdlp_exit_zero_without_a_url_is_not_offline(monkeypatch):
    """只有接口确认才敢说「下播」。yt-dlp 退出码 0 但没输出地址时，以前直接抛
    kind=offline——重连循环据此宣布直播结束并停止。"""
    monkeypatch.setitem(sys.modules, "yt_dlp", types.ModuleType("yt_dlp"))

    async def api(_url, cookies_browser="auto"):
        return None, False

    async def ytdlp(_url, cookies=None, browser=None, timeout=45):
        return 0, "", ""

    async def page(_url, browser=None):
        return None, False

    monkeypatch.setattr(resolver, "_resolve_via_api", api)
    monkeypatch.setattr(resolver, "_webkit_available", lambda: False)
    monkeypatch.setattr(resolver, "_run_ytdlp", ytdlp)
    monkeypatch.setattr(resolver, "_resolve_from_page", page)
    trace = []
    with pytest.raises(resolver.ResolveError) as exc:
        _run(resolver.resolve_stream_url("https://www.tiktok.com/@x/live",
                                         cookies_browser="none", trace=trace))
    assert exc.value.kind != "offline"
    assert "直播页兜底" in [r["layer"] for r in trace]     # 后面的层照常走到了


# ---- 检测：NFD 形式的 ñ 不再折成 n ----

def test_nfd_enye_is_not_folded_into_n():
    nfd = unicodedata.normalize("NFD", "hace un año")
    assert detector.normalize(nfd) == "hace un año"


# ---- 后台任务保引用 ----

def test_comment_source_keeps_the_restart_task():
    async def go():
        cs = comment_source.CommentSource(on_items=lambda i: None, on_state=lambda s, d="": None)
        cs._task = asyncio.ensure_future(asyncio.sleep(10))
        cs._unique_id = "old"
        cs.start("new")
        assert isinstance(cs._restart_task, asyncio.Task)
        cs._task.cancel()
        cs._restart_task.cancel()
        for t in (cs._task, cs._restart_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    asyncio.run(go())


def test_pipeline_spawn_holds_a_reference_until_done(monkeypatch, tmp_path):
    from app import pipeline as pipeline_mod
    from app import settings
    from app.pipeline import Pipeline
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    terms = tmp_path / "banned_terms.txt"
    terms.write_text("", encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms)
    args = SimpleNamespace(cookies=None, target="zh-CN", translator="none", source="es",
                           beam=5, context=False, asr_temperature=None, glossary=None,
                           backend="auto", model=None, device="auto", compute_type="auto",
                           denoise="off", banned_terms=None)
    server = SimpleNamespace(config={}, broadcast=None, status=None)
    p = Pipeline(args, server)

    async def go():
        t = p._spawn(asyncio.sleep(0.01))
        assert t in p._bg_tasks
        await t
        await asyncio.sleep(0)
        assert t not in p._bg_tasks

    asyncio.run(go())
