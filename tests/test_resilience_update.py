"""更新、启动自举与组件保鲜的几道保险。

钉住的问题：
  * 直播中点「一键更新」：监听无声停掉、界面还显示「直播中」、git 没有时限、重启后回到
    待机，审计里只剩一条 session_end；
  * pip 失败照样重启进新代码；启动只查固定模块，新依赖永远没人补；
  * mlx-whisper 一次没装上就永久停在 CPU，自检还说「重开会自动补装」；
  * yt-dlp 后台升级不留审计，curl_cffi 的版本范围不受 yt-dlp 约束；
  * bootstrap 的 pip 失败一律说成网络问题、给英文原文、什么日志都不留；
  * 检查更新失败一律无声。

不 import main（它在模块级就会 execv），不连网络，不真起 git 或 pip，不靠真实计时。
"""
import ast
import asyncio
import importlib.util
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import bootstrap, selfcheck
from app import settings as settings_mod
from app import updater as updater_mod
from app.audit import AuditLog
from app.pipeline import Pipeline
from app.server import CaptionServer
from app.updater import Updater

REPO = Path(__file__).resolve().parent.parent
URL = "https://www.tiktok.com/@someone/live"
MEDIA = "https://pull.example.com/stage/stream.flv?expire=1&sign=secret-token"
TARGET = "a" * 40
REQS = "aiohttp>=3.9\nyt-dlp[curl-cffi]\nbrand-new-dep>=1\n"


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class RecordingServer(CaptionServer):
    """真的 CaptionServer（config/incident 落盘逻辑照旧），只是把广播都记下来。"""

    def __init__(self):
        super().__init__(port=8765)
        self.sent = []

    async def broadcast(self, msg):
        self.sent.append(dict(msg))
        await super().broadcast(msg)

    def of_type(self, kind):
        return [m for m in self.sent if m.get("type") == kind]

    def statuses(self):
        return [(m["state"], m.get("detail", ""), m.get("command")) for m in self.of_type("status")]


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(updater_mod, "ROOT", tmp_path)
    return tmp_path


def make_updater(monkeypatch, server, events, git_overrides=None, pip_code=0):
    monkeypatch.setattr(updater_mod, "local_version", lambda: "1.0.0")
    results = {
        "status": (0, "", ""),
        "fetch": (0, "", ""),
        "rev-parse": (0, TARGET + "\n", ""),
        "show:VERSION": (0, "9.9.9\n", ""),
        "show:requirements.txt": (0, REQS, ""),
        "merge-base": (0, "", ""),
        "merge": (0, "", ""),
    }
    results.update(git_overrides or {})

    async def fake_git(*args, **kw):
        key = args[0]
        if key == "show":
            key = "show:" + args[1].split(":", 1)[1]
        events.append((key, args, kw))
        return results[key]

    u = Updater(server)
    u.latest = {"can_auto": True, "url": "http://x"}
    u._git = fake_git
    pip_calls = []

    async def fake_pip(args, label, timeout=600, log_path=None, abort_if=None):
        req = Path(args[1]).read_text(encoding="utf-8") if list(args[:1]) == ["-r"] else None
        pip_calls.append({"args": list(args), "timeout": timeout, "log_path": log_path,
                          "requirements": req})
        events.append(("pip", tuple(args), {}))
        return pip_code

    u._pip_install = fake_pip
    execs = []

    def fake_execv(exe, argv):
        events.append(("execv", tuple(argv), {}))
        execs.append(list(argv))

    monkeypatch.setattr(updater_mod.os, "execv", fake_execv)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(updater_mod.asyncio, "sleep", lambda *_a, **_k: real_sleep(0))
    return u, pip_calls, execs


def make_live_pipeline(server, events, tmp_path, active=True):
    p = Pipeline.__new__(Pipeline)
    p.server = server
    p.args = SimpleNamespace()
    p.audit = AuditLog(room_url=URL, log_dir=tmp_path / "logs")
    p._media_override = MEDIA
    server.config["room_url"] = URL
    state = {"active": active, "stops": [], "starts": []}

    async def stop_stream(quiet=False):
        events.append(("stop", (), {"quiet": quiet}))
        state["stops"].append(quiet)
        state["active"] = False
        if p.audit is not None:
            p.audit.close()
            p.audit = None

    async def start_stream(url, media=None):
        events.append(("start", (url, media), {}))
        state["starts"].append((url, media))
        state["active"] = True

    p.stop_stream = stop_stream
    p.start_stream = start_stream
    p._stream_active = lambda: state["active"]
    return p, state


def audit_records(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


# ---- 一键更新：直播中 -------------------------------------------------------------------------

def test_live_update_that_cannot_fetch_keeps_monitoring_and_says_so(monkeypatch, isolated):
    server, events = RecordingServer(), []
    u, pip_calls, execs = make_updater(monkeypatch, server, events,
                                       git_overrides={"fetch": (None, "", "timeout")})
    p, state = make_live_pipeline(server, events, isolated)
    audit_path = p.audit.path
    p.updater = u

    run(p._apply_update())

    assert state["stops"] == [] and state["starts"] == []
    assert pip_calls == [] and execs == []
    assert not any(e[0] in ("rev-parse", "merge") for e in events)
    notice = server.of_type("notice")[-1]["text"]
    assert "监听没有中断" in notice and "{} 秒".format(updater_mod.FETCH_TIMEOUT_SEC) in notice
    assert server.of_type("update_aborted")                     # 按钮要恢复，能再试
    assert not [s for s in server.statuses() if s[0] == "idle"]  # 直播中不许把界面打回待机
    p.audit.close()
    assert "update_stop" not in [r["type"] for r in audit_records(audit_path)]


def test_git_has_a_time_limit_and_never_waits_for_credentials(monkeypatch):
    seen = {}

    class Proc:
        returncode = None

        def __init__(self):
            self.killed, self.waited = False, 0

        async def communicate(self):
            return b"", b""

        def kill(self):
            self.killed = True

        async def wait(self):
            self.waited += 1
            return -9

    proc = Proc()

    async def fake_exec(*args, **kw):
        seen["args"], seen["env"] = args, kw.get("env")
        return proc

    async def fake_wait_for(aw, timeout):
        seen["timeout"] = timeout
        aw.close()
        raise asyncio.TimeoutError()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    code, _out, err = run(Updater(server=None)._git("fetch", timeout=updater_mod.FETCH_TIMEOUT_SEC))

    assert (code, err) == (None, "timeout")
    assert proc.killed and proc.waited == 1                     # kill 之后 reap
    assert seen["timeout"] == updater_mod.FETCH_TIMEOUT_SEC
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert "http.lowSpeedLimit=1000" in seen["args"] and "http.lowSpeedTime=30" in seen["args"]
    assert seen["args"][-1] == "fetch"


def test_live_update_fetches_first_then_pauses_installs_merges_and_leaves_a_resume_marker(
        monkeypatch, isolated):
    (isolated / ".venv").mkdir()
    server, events = RecordingServer(), []
    u, pip_calls, execs = make_updater(monkeypatch, server, events)
    p, state = make_live_pipeline(server, events, isolated)
    audit_path = p.audit.path
    p.updater = u
    t0 = time.time()

    run(p._apply_update())

    order = [e[0] for e in events]
    assert order.index("fetch") < order.index("stop") < order.index("pip") \
        < order.index("merge") < order.index("execv")
    merge = next(e for e in events if e[0] == "merge")
    assert merge[1] == ("merge", "--ff-only", TARGET)           # 合并的就是取下来的那个提交
    assert pip_calls[0]["args"][0] == "-r" and pip_calls[0]["requirements"] == REQS
    assert not Path(pip_calls[0]["args"][1]).exists()           # 临时清单用完即删
    assert pip_calls[0]["timeout"] == updater_mod.UPDATE_PIP_TIMEOUT_SEC
    assert state["stops"] == [True] and state["starts"] == []
    assert p._stop_reason == "update"
    details = [s[1] for s in server.statuses()]
    assert "正在更新，监听已暂停（约 1 分钟后自动恢复）" in details
    assert execs and "main.py" in " ".join(execs[0])

    records = audit_records(audit_path)
    stop = [r for r in records if r["type"] == "update_stop"]
    assert stop and stop[0]["from_version"] == "1.0.0" and stop[0]["to_version"] == "9.9.9"
    assert stop[0]["at"]
    assert "secret-token" not in audit_path.read_text(encoding="utf-8")   # 签名地址不进审计

    marker = settings_mod.load_settings()["resume_after_update"]
    assert marker["url"] == URL and marker["media"] == MEDIA and marker["to_version"] == "9.9.9"
    assert t0 - 1 <= marker["at"] <= time.time() + 1
    # 新清单装成功、代码也合并了：启动时的清单指纹对得上，不会再补装一遍
    (isolated / "requirements.txt").write_text(REQS.replace("\n", "\r\n"), encoding="utf-8")
    assert bootstrap.requirements_pending(isolated) is False


def test_live_update_pip_failure_restores_monitoring_on_the_current_version(monkeypatch, isolated):
    server, events = RecordingServer(), []
    u, pip_calls, execs = make_updater(monkeypatch, server, events, pip_code=1)
    p, state = make_live_pipeline(server, events, isolated)
    p.updater = u

    run(p._apply_update())

    assert "merge" not in [e[0] for e in events] and execs == []   # 新代码没落地
    assert state["stops"] == [True] and state["starts"] == [(URL, MEDIA)]
    text = server.config["incidents"]["update"]["text"]
    assert "pip 返回 1" in text and "logs/update-" in text
    assert "继续使用当前版本 v1.0.0" in text and "已在当前版本上自动恢复监听" in text
    assert p._resume_reason == "update_failed"
    assert settings_mod.load_settings().get("resume_after_update") is None
    assert server.of_type("update_aborted")
    assert pip_calls[0]["log_path"] is not None


def test_live_update_merge_failure_restores_monitoring_too(monkeypatch, isolated):
    server, events = RecordingServer(), []
    u, _pip, execs = make_updater(monkeypatch, server, events,
                                  git_overrides={"merge": (1, "", "fatal: Not possible to fast-forward")})
    p, state = make_live_pipeline(server, events, isolated)
    p.updater = u

    run(p._apply_update())

    assert execs == [] and state["starts"] == [(URL, MEDIA)]
    assert "Not possible to fast-forward" in server.config["incidents"]["update"]["text"]
    assert settings_mod.load_settings().get("resume_after_update") is None


def test_retrying_the_update_clears_the_previous_failure_banner(monkeypatch, isolated):
    server, events = RecordingServer(), []
    u, _pip, _execs = make_updater(monkeypatch, server, events, pip_code=1)
    p, _state = make_live_pipeline(server, events, isolated)
    p.updater = u
    run(p._apply_update())
    assert "update" in server.config["incidents"]
    u._git = None                     # 第二次只看横幅：latest 清掉，apply 直接返回
    u.latest = None
    run(p._apply_update())
    assert "update" not in server.config["incidents"]


def test_update_does_not_pause_monitoring_while_another_pip_holds_the_lock(monkeypatch, isolated):
    server, events = RecordingServer(), []
    u, pip_calls, _execs = make_updater(monkeypatch, server, events)
    p, state = make_live_pipeline(server, events, isolated)
    p.updater = u

    async def scenario():
        lock = u._get_pip_lock()
        await lock.acquire()
        try:
            await p._apply_update()
        finally:
            lock.release()

    run(scenario())
    assert state["stops"] == [] and pip_calls == []
    assert "后台正在安装别的组件" in server.of_type("notice")[-1]["text"]


# ---- 一键更新：没在监听 ----------------------------------------------------------------------

def test_idle_update_pip_failure_does_not_merge_and_hands_over_a_command(monkeypatch, isolated):
    server, events = RecordingServer(), []
    u, _pip, execs = make_updater(monkeypatch, server, events, pip_code=None)

    run(u.apply())

    assert "merge" not in [e[0] for e in events] and execs == []
    state, detail, command = server.statuses()[-1]
    assert state == "idle" and "pip 返回 超时" in detail and "继续使用当前版本" in detail
    assert command.startswith('cd "') and "pip install -r requirements.txt" in command


def test_idle_update_success_writes_the_requirements_stamp_then_restarts(monkeypatch, isolated):
    (isolated / ".venv").mkdir()
    server, events = RecordingServer(), []
    u, _pip, execs = make_updater(monkeypatch, server, events)

    run(u.apply())

    assert execs
    assert (isolated / ".venv" / bootstrap.REQ_STAMP).read_text().strip() == \
        bootstrap.requirements_digest(REQS)
    assert server.statuses()[-1][:2] == ("idle", "更新完成，正在自动重启…")
    assert settings_mod.load_settings().get("resume_after_update") is None


# ---- 重启后接着监听 ---------------------------------------------------------------------------

def test_resume_marker_is_used_once_and_only_when_fresh(isolated):
    server = RecordingServer()
    p = Pipeline.__new__(Pipeline)
    p.server = server
    starts = []

    async def start_stream(url, media=None):
        starts.append((url, media))

    p.start_stream = start_stream
    now = 1_800_000_000.0

    def put(marker):
        settings_mod.save_setting("resume_after_update", marker)

    put({"url": URL, "media": MEDIA, "at": now - 30, "to_version": "9.9.9"})
    assert run(p.resume_after_update(now=now)) is True
    assert starts == [(URL, MEDIA)] and p._resume_reason == "update"
    assert settings_mod.load_settings().get("resume_after_update") is None   # 读到即删
    assert run(p.resume_after_update(now=now)) is False and len(starts) == 1
    assert "@someone" in server.statuses()[-1][1]

    for stale in ({"url": URL, "at": now - Pipeline.RESUME_AFTER_UPDATE_MAX_SEC - 1},
                  {"url": "file:///etc/passwd", "at": now},
                  {"url": URL, "at": "yesterday"},
                  "junk"):
        put(stale)
        assert run(p.resume_after_update(now=now)) is False
        assert settings_mod.load_settings().get("resume_after_update") is None
    assert len(starts) == 1


def test_session_start_records_versions_update_check_and_why_it_started(monkeypatch, isolated):
    from app import pipeline as pipeline_mod
    monkeypatch.setenv("HF_HOME", str(isolated))
    monkeypatch.setattr(updater_mod, "component_version",
                        lambda name: {"yt-dlp": "2026.8.19", "curl_cffi": "0.16.1"}.get(name))
    ok_at = datetime(2026, 9, 1, 8, 0, 0).timestamp()
    settings_mod.save_setting("update_check_ok_at", ok_at)
    args = SimpleNamespace(cookies=None, target="zh-CN", translator="none", source=None,
                           beam=5, context=False, asr_temperature=None, glossary=None,
                           backend="auto", model=None, device="auto", compute_type="auto",
                           denoise="off", comments=False)
    p = Pipeline(args, RecordingServer())

    async def nothing():
        return None

    p._provision_then_check = nothing
    p._resume_reason = "update"

    async def scenario():
        await p._begin_session(URL)
        path = p.audit.path
        p._stats_task.cancel()
        try:
            await p._stats_task
        except asyncio.CancelledError:
            pass
        await p._provision_task
        p.audit.close()
        return path

    path = run(scenario())
    start = audit_records(path)[0]
    assert start["type"] == "session_start"
    assert start["ytdlp_version"] == "2026.8.19" and start["curl_cffi_version"] == "0.16.1"
    assert start["update_check_ok_at"] == "2026-09-01T08:00:00"
    assert start["resumed_after"] == "update"
    assert p._resume_reason is None                              # 只用一次
    assert pipeline_mod is not None


# ---- 启动时的清单指纹 -------------------------------------------------------------------------

def test_requirements_pending_follows_the_last_successful_install(tmp_path):
    (tmp_path / ".venv").mkdir()
    (tmp_path / "requirements.txt").write_text("aiohttp>=3.9\n", encoding="utf-8")
    assert bootstrap.requirements_pending(tmp_path) is True        # 从没记过：要补装一次
    assert bootstrap.write_requirements_stamp(tmp_path, bootstrap.requirements_file_digest(tmp_path))
    assert bootstrap.requirements_pending(tmp_path) is False
    (tmp_path / "requirements.txt").write_bytes(b"aiohttp>=3.9\r\n")   # 只是换行不同
    assert bootstrap.requirements_pending(tmp_path) is False
    (tmp_path / "requirements.txt").write_text("aiohttp>=3.9\nnew-dep\n", encoding="utf-8")
    assert bootstrap.requirements_pending(tmp_path) is True


def test_stamp_is_not_written_into_a_missing_venv(tmp_path):
    assert bootstrap.write_requirements_stamp(tmp_path, "abc") is False
    assert not (tmp_path / ".venv").exists()


def test_managed_env_covers_the_project_venv_and_the_app_shell_only(tmp_path):
    assert bootstrap.managed_env(tmp_path, prefix=tmp_path / ".venv")
    assert bootstrap.managed_env(tmp_path, prefix=tmp_path / bootstrap.APP_DIR_NAME / "Contents")
    assert not bootstrap.managed_env(tmp_path, prefix=tmp_path / "someone-elses-python")


def test_clean_install_writes_the_stamp(tmp_path):
    (tmp_path / ".venv").mkdir()
    (tmp_path / "requirements.txt").write_text("aiohttp>=3.9\n", encoding="utf-8")
    calls = []

    def ok(cmd, log_path):
        calls.append(cmd)
        return 0, ""

    result = bootstrap.install_requirements("py", tmp_path, ["aiohttp"], ["mlx-whisper"], run=ok)
    assert result == {"full_ok": True, "failed_optional": []}
    assert calls[0][-2:] == ["-r", str(tmp_path / "requirements.txt")] and len(calls) == 1
    assert bootstrap.requirements_pending(tmp_path) is False


def test_failed_install_falls_back_records_the_attempt_and_writes_a_json_mlx_marker(tmp_path):
    venv = tmp_path / ".venv"
    venv.mkdir()
    (tmp_path / "requirements.txt").write_text("aiohttp>=3.9\n", encoding="utf-8")

    def flaky(cmd, log_path):
        if "-r" in cmd or cmd[-1] == "mlx-whisper":
            return 1, "ERROR: boom"
        return 0, ""

    now = 1_800_000_000.0
    result = bootstrap.install_requirements("py", tmp_path, ["aiohttp"],
                                            ["mlx-whisper", "pywebview>=5"], run=flaky, now=now)
    assert result == {"full_ok": False, "failed_optional": ["mlx-whisper"]}
    assert not (venv / bootstrap.REQ_STAMP).exists()
    raw = json.loads((venv / bootstrap.MLX_GIVEUP).read_text(encoding="utf-8"))
    assert set(raw) == {"at", "pip_exit", "note"} and raw["pip_exit"] == 1
    assert datetime.fromisoformat(raw["at"]).timestamp() == now
    # 同一份清单刚装失败过：24 小时内启动不再重跑；过了再试
    assert bootstrap.requirements_pending(tmp_path, now=now + 3600) is False
    assert bootstrap.requirements_pending(tmp_path, now=now + bootstrap.REQ_RETRY_SEC + 1) is True
    # 程序因为缺模块起不来时忘掉这次失败：下次启动立刻重试，不等 24 小时
    assert bootstrap.forget_requirements_failure(tmp_path)
    assert bootstrap.requirements_pending(tmp_path, now=now + 3600) is True


def test_core_dependency_failure_raises_with_the_log_for_the_dialog(tmp_path):
    (tmp_path / "requirements.txt").write_text("aiohttp>=3.9\n", encoding="utf-8")
    log = tmp_path / "logs" / "bootstrap-x.log"

    def full_disk(cmd, log_path):
        return 1, "ERROR: Could not install packages due to an OSError: [Errno 28] No space left on device"

    with pytest.raises(bootstrap.PipFailed) as info:
        bootstrap.install_requirements("py", tmp_path, ["aiohttp"], [], log_path=log, run=full_disk)
    assert info.value.exit_code == 1 and info.value.log_path == log
    text = bootstrap.failure_text(info.value, python_version="3.13", root=tmp_path)
    assert "磁盘空间不够" in text and str(log) in text


# ---- mlx-whisper 放弃记号 --------------------------------------------------------------------

def test_giveup_marker_reader_accepts_json_and_the_old_plain_text(tmp_path):
    marker = tmp_path / bootstrap.MLX_GIVEUP
    assert bootstrap.read_mlx_giveup(marker) is None
    marker.write_text("装不上，改用 CPU 后端\n", encoding="utf-8")
    old = time.time() - 3 * 86400
    os.utime(marker, (old, old))
    info = bootstrap.read_mlx_giveup(marker)
    assert info["at"] is None and info["pip_exit"] is None and "装不上" in info["note"]
    assert abs(info["ts"] - old) < 5 and bootstrap.mlx_retry_due(info)
    bootstrap.write_mlx_giveup(marker, 2, "x")
    info = bootstrap.read_mlx_giveup(marker)
    assert info["pip_exit"] == 2 and info["at"] and not bootstrap.mlx_retry_due(info)


def test_cpu_warning_names_the_failed_gpu_install_instead_of_promising_a_reinstall(
        monkeypatch, tmp_path):
    from app import hwdetect
    (tmp_path / ".venv").mkdir()
    bootstrap.write_mlx_giveup(tmp_path / ".venv" / bootstrap.MLX_GIVEUP, 1, "x",
                               now=datetime(2026, 9, 10, 12, 0, 0).timestamp())
    monkeypatch.setattr(selfcheck, "ROOT", tmp_path)
    monkeypatch.setattr(hwdetect, "detect", lambda: {"apple_silicon": True, "has_mlx": False})
    monkeypatch.setattr(hwdetect, "recommend",
                        lambda backend="auto", device="auto": {
                            "backend": "ct2", "model": "large-v3-turbo",
                            "device": "cpu", "compute_type": "auto", "reason": ""})
    monkeypatch.setattr(selfcheck, "_model_cached", lambda m, b: True)
    monkeypatch.setattr(selfcheck, "_importable", lambda name: True)
    c = run(selfcheck.check_asr(SimpleNamespace(backend="auto", model=None, device="auto")))
    assert c["level"] == "warn"
    assert "2026-09-10" in c["fix"] and "pip 返回 1" in c["fix"]
    assert "会自动补装" not in c["fix"] and "mlx-whisper" in c["fix"]


def make_mlx_updater(monkeypatch, isolated, live, pip_result, installed):
    monkeypatch.setattr(updater_mod, "is_apple_silicon", lambda: True)
    monkeypatch.setattr(updater_mod, "component_version",
                        lambda name: "2.1.0" if name == "numpy" else None)
    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(updater_mod.importlib.util, "find_spec",
                        lambda name, *a: (object() if installed["mlx"] else None)
                        if name == "mlx_whisper" else real_find_spec(name, *a))
    server = RecordingServer()
    u = Updater(server)
    incidents = []

    async def incident(key, level, text=""):
        incidents.append((key, level, text))

    u.attach_pipeline(SimpleNamespace(_stream_active=lambda: live["on"], _incident=incident))
    calls = []

    async def fake_pip(args, label, timeout=600, log_path=None, abort_if=None):
        calls.append({"args": list(args), "timeout": timeout, "abort_if": abort_if})
        code = pip_result["code"]
        if code == 0:
            installed["mlx"] = True
        return code

    u._pip_install = fake_pip
    (isolated / ".venv").mkdir(exist_ok=True)
    return u, calls, incidents


def test_mlx_background_retry_waits_a_day_stays_off_live_and_clears_the_marker(monkeypatch, isolated):
    marker = isolated / ".venv" / bootstrap.MLX_GIVEUP
    live, pip_result, installed = {"on": True}, {"code": 0}, {"mlx": False}
    u, calls, incidents = make_mlx_updater(monkeypatch, isolated, live, pip_result, installed)
    now = time.time()
    bootstrap.write_mlx_giveup(marker, 1, "x", now=now - 3600)
    assert run(u.retry_mlx_install(now=now)) == "cooldown"
    bootstrap.write_mlx_giveup(marker, 1, "x", now=now - 2 * 86400)
    assert run(u.retry_mlx_install(now=now)) == "busy" and calls == []    # 在监听：不装
    live["on"] = False

    assert run(u.retry_mlx_install(now=now)) == "installed"
    assert calls[0]["args"] == ["mlx-whisper", "numpy==2.1.0"]            # 不许动在用的 numpy
    assert calls[0]["timeout"] == updater_mod.MLX_RETRY_TIMEOUT_SEC
    live["on"] = True
    assert calls[0]["abort_if"]() is True                                 # 开播就叫停
    assert not marker.exists()
    assert incidents == [("mlx-ready", "info", "GPU 加速组件已装好，下次停播后重开程序生效")]


def test_mlx_background_retry_failure_and_abort(monkeypatch, isolated):
    marker = isolated / ".venv" / bootstrap.MLX_GIVEUP
    live, pip_result, installed = {"on": False}, {"code": 1}, {"mlx": False}
    u, calls, incidents = make_mlx_updater(monkeypatch, isolated, live, pip_result, installed)
    now = time.time()
    bootstrap.write_mlx_giveup(marker, 1, "x", now=now - 2 * 86400)

    assert run(u.retry_mlx_install(now=now)) == "failed"
    info = bootstrap.read_mlx_giveup(marker)
    assert info["pip_exit"] == 1 and abs(info["ts"] - time.time()) < 5    # 失败重新计 24 小时
    assert run(u.retry_mlx_install()) == "cooldown" and len(calls) == 1

    bootstrap.write_mlx_giveup(marker, 1, "x", now=now - 2 * 86400)
    before = marker.read_text(encoding="utf-8")
    pip_result["code"] = updater_mod.PIP_ABORTED
    assert run(u.retry_mlx_install(now=now)) == "aborted"
    assert marker.read_text(encoding="utf-8") == before and incidents == []


def test_mlx_retry_does_nothing_without_the_pipeline_hook_or_off_apple_silicon(monkeypatch, isolated):
    marker = isolated / ".venv" / bootstrap.MLX_GIVEUP
    (isolated / ".venv").mkdir()
    bootstrap.write_mlx_giveup(marker, 1, "x", now=time.time() - 2 * 86400)
    monkeypatch.setattr(updater_mod.importlib.util, "find_spec",
                        lambda name, *a: None)
    u = Updater(RecordingServer())
    monkeypatch.setattr(updater_mod, "is_apple_silicon", lambda: False)
    assert run(u.retry_mlx_install()) == "not-applicable"
    monkeypatch.setattr(updater_mod, "is_apple_silicon", lambda: True)
    assert run(u.retry_mlx_install()) == "busy"         # 不知道有没有在监听：按在忙算


class _PipProc:
    returncode = 0

    def __init__(self):
        self.killed, self.waits = False, 0

    async def wait(self):
        self.waits += 1
        return 0

    def kill(self):
        self.killed = True


def test_background_pip_is_killed_as_soon_as_monitoring_starts(monkeypatch):
    proc = _PipProc()
    spawned = []

    async def fake_exec(*a, **k):
        spawned.append(a)
        return proc

    async def fake_wait_for(aw, timeout):
        aw.close()
        raise asyncio.TimeoutError()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    asks = {"n": 0}

    def abort_if():
        asks["n"] += 1
        return asks["n"] >= 3          # 开始前问一次、第一轮等完问一次，第二轮等完开播了

    u = Updater(server=None)

    async def scenario():
        code = await u._pip_install(["mlx-whisper"], "测试", timeout=60, abort_if=abort_if)
        return code, u._get_pip_lock().locked()

    code, locked = run(scenario())
    assert code == updater_mod.PIP_ABORTED and not locked
    assert proc.killed and proc.waits == 1 and len(spawned) == 1


def test_background_pip_still_has_a_total_time_limit(monkeypatch):
    proc = _PipProc()

    async def fake_exec(*a, **k):
        return proc

    async def fake_wait_for(aw, timeout):
        aw.close()
        raise asyncio.TimeoutError()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    code = run(Updater(server=None)._pip_install(["x"], "测试", timeout=6, abort_if=lambda: False))
    assert code is None and proc.killed and proc.waits == 1


def test_background_pip_does_not_start_if_monitoring_began_while_waiting_for_the_lock(monkeypatch):
    spawned = []

    async def fake_exec(*a, **k):
        spawned.append(a)
        return _PipProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    code = run(Updater(server=None)._pip_install(["x"], "测试", abort_if=lambda: True))
    assert code == updater_mod.PIP_ABORTED and spawned == []


# ---- yt-dlp 保鲜：配对范围与审计 ----------------------------------------------------------------

def test_requirements_pin_the_ytdlp_curl_cffi_pair_through_the_extra():
    lines = [ln.split("#")[0].strip()
             for ln in (REPO / "requirements.txt").read_text(encoding="utf-8").splitlines()]
    assert "yt-dlp[curl-cffi]" in lines and "yt-dlp" not in lines
    assert updater_mod.YTDLP_SPEC == "yt-dlp[curl-cffi]"


def test_ytdlp_freshen_during_a_session_is_written_into_its_audit(monkeypatch, isolated):
    versions = {"yt-dlp": ["2026.8.19", "2026.9.1"], "curl_cffi": ["0.16.1", "0.16.1"]}

    def fake_version(name):
        seq = versions[name]
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(updater_mod, "component_version", fake_version)
    seen = {}

    class Proc:
        returncode = 0

        async def communicate(self):
            return b"Successfully installed yt-dlp-2026.9.1\n", b""

    async def fake_exec(*args, **kw):
        seen["args"] = args
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    p = Pipeline.__new__(Pipeline)
    p.audit = AuditLog(room_url=URL, log_dir=isolated / "logs")
    u = Updater(RecordingServer())
    u.attach_pipeline(p)

    run(u.freshen_ytdlp(reason="periodic"))

    assert "yt-dlp[curl-cffi]" in seen["args"]
    p.audit.close()
    recs = [r for r in audit_records(p.audit.path) if r["type"] == "component_updated"]
    assert len(recs) == 1                                        # curl_cffi 没变，不记
    assert {k: recs[0][k] for k in ("name", "from", "to", "reason")} == {
        "name": "yt-dlp", "from": "2026.8.19", "to": "2026.9.1", "reason": "periodic"}
    assert recs[0]["at"]


def test_component_update_without_an_open_session_is_harmless(monkeypatch, isolated):
    p = Pipeline.__new__(Pipeline)
    p.audit = None
    p.note_component_updated("yt-dlp", "1", "2", "periodic")        # 不抛


# ---- bootstrap 的失败说明与日志 -------------------------------------------------------------------

OFFLINE_TAIL = (
    "WARNING: Retrying (Retry(total=0, connect=None, read=None, redirect=None, status=None)) "
    "after connection broken by 'NewConnectionError('<HTTPSConnection>: Failed to establish a "
    "new connection: [Errno 8] nodename nor servname provided, or not known')': /simple/aiohttp/\n"
    "ERROR: Could not find a version that satisfies the requirement aiohttp>=3.9 (from versions: none)\n"
    "ERROR: No matching distribution found for aiohttp>=3.9")
NO_WHEEL_TAIL = (
    "ERROR: Ignored the following versions that require a different python version: "
    "0.4.3 Requires-Python >=3.10\n"
    "ERROR: Could not find a version that satisfies the requirement mlx-whisper (from versions: none)\n"
    "ERROR: No matching distribution found for mlx-whisper")


def test_pip_failures_are_described_by_what_pip_actually_printed(tmp_path):
    log = tmp_path / "logs" / "bootstrap-1.log"
    offline = bootstrap.pip_failure_text(OFFLINE_TAIL, 1, log, "3.14", tmp_path)
    assert "连不上软件包服务器" in offline and "Python" not in offline.split("\n")[0]
    wheel = bootstrap.pip_failure_text(NO_WHEEL_TAIL, 1, log, "3.14", tmp_path)
    assert "当前 Python 3.14" in wheel and "3.12 或 3.13" in wheel
    disk = bootstrap.pip_failure_text("OSError: [Errno 28] No space left on device", 1, log,
                                      "3.13", tmp_path)
    assert "磁盘空间不够" in disk
    other = bootstrap.pip_failure_text("ERROR: something new", 2, log, "3.13", tmp_path)
    assert "pip 返回 2" in other and "检查网络" in other
    for text in (offline, wheel, disk, other):
        assert str(log) in text and "Command '[" not in text and "CalledProcessError" not in text


def test_non_pip_bootstrap_errors_go_to_the_log_not_the_dialog(tmp_path):
    try:
        raise RuntimeError("venv exploded")
    except RuntimeError as exc:
        text = bootstrap.failure_text(exc, None, python_version="3.13", root=tmp_path)
    logs = sorted((tmp_path / "logs").glob("bootstrap-*.log"))
    assert logs and "venv exploded" in logs[-1].read_text(encoding="utf-8")
    assert str(logs[-1]) in text and "venv exploded" not in text


def test_run_logged_tees_the_output_into_the_log_and_keeps_the_tail(tmp_path, capsys):
    log = tmp_path / "bootstrap.log"
    code, tail = bootstrap.run_logged(
        [sys.executable, "-c",
         "import sys; print('Collecting aiohttp'); sys.stdout.flush(); "
         "sys.stderr.write('ERROR: boom\\n'); sys.exit(3)"], log)
    assert code == 3 and "Collecting aiohttp" in tail and "ERROR: boom" in tail
    text = log.read_text(encoding="utf-8")
    assert "Collecting aiohttp" in text and "ERROR: boom" in text and "[退出码 3]" in text
    assert "Collecting aiohttp" in capsys.readouterr().out         # 终端照样看得到进度


def test_install_dialog_says_what_actually_changed():
    assert bootstrap.install_dialog_text(None, "3.13").startswith("首次运行：正在自动安装运行组件")
    drift = bootstrap.install_dialog_text("3.13", "3.14", venv_existed=True)
    assert drift.startswith("Python 版本从 3.13 变成了 3.14，需要重新安装运行组件")
    assert "组件清单" in bootstrap.install_dialog_text("3.13", "3.13", True, requirements_only=True)
    assert "不完整" in bootstrap.install_dialog_text("3.13", "3.13", venv_existed=True)


def test_pyvenv_and_interpreter_versions(tmp_path):
    (tmp_path / "pyvenv.cfg").write_text("home = /opt/x/bin\nversion = 3.13.5\n", encoding="utf-8")
    assert bootstrap.pyvenv_version(tmp_path) == "3.13"
    (tmp_path / "pyvenv.cfg").write_text("version_info = 3.14.0.final.0\n", encoding="utf-8")
    assert bootstrap.pyvenv_version(tmp_path) == "3.14"
    assert bootstrap.pyvenv_version(tmp_path / "missing") is None
    ok, version = bootstrap.interpreter_version(sys.executable)
    assert ok and version == "{}.{}".format(*sys.version_info[:2])
    assert bootstrap.interpreter_version(tmp_path / "no-python") == (False, None)


def test_log_files_are_pruned_per_kind(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    for n in range(12):
        (logs / "bootstrap-20260901-0000{:02d}.log".format(n)).write_text("x")
    (logs / "update-20260901-000000.log").write_text("x")
    path = bootstrap.new_log_path(tmp_path, "bootstrap", now=datetime(2026, 9, 15).timestamp())
    assert path.name == "bootstrap-20260915-000000.log"
    assert len(list(logs.glob("bootstrap-*.log"))) == bootstrap.KEEP_LOGS - 1
    assert (logs / "update-20260901-000000.log").exists()


def test_main_defines_the_first_run_flag_it_reads():
    """_first_run_dialog 读 global _FIRST_RUN_SHOWN，而模块级从没赋过值：第一次安装时
    NameError，被 ensure_env 的 except 接住，报「自动安装未完成」。不 import main，读语法树。"""
    tree = ast.parse((REPO / "main.py").read_text(encoding="utf-8"))
    names = {t.id for node in tree.body if isinstance(node, ast.Assign)
             for t in node.targets if isinstance(t, ast.Name)}
    assert "_FIRST_RUN_SHOWN" in names


# ---- 检查更新失败不再无声 ---------------------------------------------------------------------

def fake_github(monkeypatch, status=200, exc=None):
    class Resp:
        async def json(self):
            return {"tag_name": "v0.0.1", "body": "", "html_url": "http://x"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    Resp.status = status

    class Sess:
        def get(self, *a, **k):
            if exc is not None:
                raise exc
            return Resp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: Sess())


def test_update_check_results_are_recorded(monkeypatch, isolated):
    monkeypatch.setattr(updater_mod, "local_version", lambda: "1.0.0")
    u = Updater(RecordingServer())
    fake_github(monkeypatch, status=403)
    run(u.check_and_notify(delay=0))
    first = settings_mod.load_settings()["update_check_error"]
    assert first["status_or_exc"] == "HTTP 403" and first["since"] == first["at"]

    fake_github(monkeypatch, exc=asyncio.TimeoutError())
    run(u.check_and_notify(delay=0))
    second = settings_mod.load_settings()["update_check_error"]
    assert second["status_or_exc"] == "超时" and second["since"] == first["since"]

    fake_github(monkeypatch, status=200)
    run(u.check_and_notify(delay=0))
    saved = settings_mod.load_settings()
    assert saved.get("update_check_error") is None and saved["update_check_ok_at"] >= first["at"]


def test_footer_note_only_after_two_weeks_without_reaching_the_server(isolated):
    now = 1_800_000_000.0
    day = 86400
    never = {"update_check_error": {"at": now, "since": now - 15 * day, "status_or_exc": "HTTP 403"}}
    assert updater_mod.update_check_note(never, now=now) == "已 15 天没能连上更新服务器（最近一次：HTTP 403）"
    recent = dict(never, update_check_ok_at=now - 3 * day)
    assert updater_mod.update_check_note(recent, now=now) is None
    young = {"update_check_error": {"at": now, "since": now - 13 * day, "status_or_exc": "超时"}}
    assert updater_mod.update_check_note(young, now=now) is None
    assert updater_mod.update_check_note({"update_check_ok_at": now - 90 * day}, now=now) is None

    server = RecordingServer()
    u = Updater(server)
    settings_mod.save_setting("update_check_error",
                              {"at": time.time(), "since": time.time() - 20 * day,
                               "status_or_exc": "HTTP 403"})
    run(u._publish_check_health())
    run(u._publish_check_health())                  # 没变化不重复广播
    configs = server.of_type("config")
    assert len(configs) == 1 and "20 天" in configs[0]["update_check"]["note"]
    assert "20 天" in server.config["update_check"]["note"]     # 刷新页面也在


def test_update_check_stays_out_of_the_selfcheck_panel():
    assert "版本更新" not in (REPO / "app" / "selfcheck.py").read_text(encoding="utf-8")
