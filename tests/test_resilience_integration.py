"""五组保险各自开发、合到一起之后才出现的接缝，钉在这里。

全部用桩件驱动：不联网、不起 ffmpeg、不加载模型、不开浏览器。
"""
import ast
import asyncio
import inspect
import json
import textwrap
from types import SimpleNamespace

import pytest

import app.asr
import app.audio
import app.segmenter
from app import audit as audit_mod
from app import pipeline as pipeline_mod
from app.audio import FRAME_BYTES
from app.audit import AuditLog
from app.pipeline import Pipeline
from app.redact import strip_query
from app.server import CaptionServer

ROOM = "https://www.tiktok.com/@example/live"
SECRET = "SECRETSIGN"


def run(coro):
    return asyncio.run(coro)


class RecordingServer(CaptionServer):
    def __init__(self):
        super().__init__(port=0)
        self.messages = []

    async def broadcast(self, msg):
        self.messages.append(msg)
        await super().broadcast(msg)


def make_pipeline(monkeypatch, tmp_path):
    from app import settings
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    terms = tmp_path / "banned_terms.txt"
    terms.write_text("", encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms)
    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source="es", source_requested="es",
        beam=5, context=False, asr_temperature=None, glossary=None, backend="auto", model=None,
        device="auto", compute_type="auto", denoise="off", banned_terms=None, comments=False,
    )
    server = RecordingServer()
    p = Pipeline(args, server)

    async def noop(*a, **k):
        return None

    p.run_selfcheck = noop
    p.ensure_local_translator = noop
    monkeypatch.setattr(app.asr, "create_transcriber", lambda **kw: object())
    real_sleep = asyncio.sleep
    monkeypatch.setattr(pipeline_mod.asyncio, "sleep", lambda *_a, **_k: real_sleep(0))
    return p, server


def rows_of(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def of_type(rows, kind):
    return [r for r in rows if r.get("type") == kind]


def fake_audio(monkeypatch, frames):
    class Source:
        def __init__(self, media, denoise_model=None):
            self.stalled = False
            self.proc = SimpleNamespace(returncode=None)

        async def frames(self):
            for _ in range(frames):
                yield b"\x01" * FRAME_BYTES

        def stderr_tail(self):
            return ""

        async def stop(self):
            self.proc.returncode = 0

    class OneSegmentPerFrame:
        def __init__(self, *a, **k):
            pass

        def feed(self, frame):
            return [frame]

        def flush(self):
            return []

    monkeypatch.setattr(app.audio, "FFmpegAudioSource", Source)
    monkeypatch.setattr(app.segmenter, "SilenceSegmenter", OneSegmentPerFrame)


# ---- 网址脱敏只有一条规则 ----------------------------------------------------------
# asr 组的 strip_url_queries、evidence 组的 clean_error、stream 组的 redact.strip_query
# 原先是三份略有出入的正则（有的只认 http/rtmp/ws）。审计里任何一处漏掉，签名地址就落盘。

SAMPLES = [
    "Error opening input https://pull-flv-l1.tiktokcdn.com/stage/s.flv?expire=1790000000&sign="
    + SECRET + ": Server returned 403",
    "rtmp://push.example.com/live/abc?token=" + SECRET + " failed",
    "GET s3://bucket/key?X-Amz-Signature=" + SECRET,
    "wss://webcast16-ws.tiktok.com/webcast/im/ws?cursor=1#" + SECRET,
]


@pytest.mark.parametrize("text", SAMPLES)
def test_every_audit_scrubber_follows_the_one_rule(text):
    expected = strip_query(text)
    assert SECRET not in expected and "://" in expected
    assert audit_mod.strip_url_queries(text) == expected[:300]
    assert audit_mod.clean_error(text) == expected[:200]
    assert audit_mod.clean_error(RuntimeError(text)) == expected[:200]


# ---- 一轮拉流只写自己那一场的审计 --------------------------------------------------
# stream 组让每一轮拉流拿着 sess（里面是开这一场时的审计），asr 组在同一个循环里加了
# 识别失败的留痕。合并前三处写入还走 self.audit：停止撒手后晚结束的旧一轮，会把
# 识别失败、丢音频、识别超时写进下一场的合规证据里（CLAUDE.md 第七条 my_audit）。

def test_a_late_stream_round_writes_failures_into_its_own_session_audit(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    fake_audio(monkeypatch, frames=3)
    old = AuditLog(room_url=ROOM, log_dir=tmp_path / "old")
    new = AuditLog(room_url=ROOM, log_dir=tmp_path / "new")
    p.audit = new                        # 下一场已经开始
    sess = p._new_session_state(old)     # 这一轮还属于上一场

    class Broken:
        def transcribe(self, pcm):
            raise RuntimeError("Metal error")

    async def scenario():
        loop = asyncio.get_running_loop()
        return await p._stream_session("http://cdn/s.flv", Broken(), None, "note", loop, sess=sess)

    run(scenario())
    old.close()
    new.close()
    assert of_type(rows_of(old.path), "asr_failed")
    assert of_type(rows_of(new.path), "asr_failed") == []


def test_the_stream_round_never_calls_the_shared_audit():
    """防回退：拉流循环里对审计的调用都要经过 sess["audit"]。只有 sess 缺省时才读一次
    self.audit 去建 sess，那是传参，不是调用方法。"""
    tree = ast.parse(textwrap.dedent(inspect.getsource(Pipeline._stream_session)))
    calls = [node.lineno for node in ast.walk(tree)
             if isinstance(node, ast.Attribute)
             and isinstance(node.value, ast.Attribute) and node.value.attr == "audit"
             and isinstance(node.value.value, ast.Name) and node.value.value.id == "self"]
    assert calls == []


# ---- 一键更新停掉的那一场，session_end 写 update ------------------------------------
# update 组在停监听之前放 _stop_reason = "update"，stream 组的 _stop_locked 把它写进
# session_end。两组各自的测试都把对方那半截换成了桩，这里走真的停止流程。

def test_an_update_pause_is_the_recorded_end_reason(monkeypatch, tmp_path):
    from app import resolver
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def scenario():
        entered = asyncio.Event()

        async def endless_round(media, *a, **k):
            entered.set()
            await asyncio.Event().wait()

        async def fake_resolve(url, cookies=None, cookies_browser="auto", trace=None):
            return "http://cdn/a.flv"

        monkeypatch.setattr(resolver, "resolve_stream_url", fake_resolve)
        monkeypatch.setattr(p, "_stream_session", endless_round)
        await p.start_stream(ROOM)
        await asyncio.wait_for(entered.wait(), 10)
        server.config["room_url"] = ROOM
        return await p._pause_for_update("0.16.0", "0.16.1")

    token = run(scenario())
    assert token is not None and token["url"] == ROOM
    rows = []
    for f in sorted((tmp_path / "logs").glob("session-*.jsonl")):
        rows.extend(rows_of(f))
    kinds = [r["type"] for r in rows]
    assert "update_stop" in kinds and kinds.index("update_stop") < kinds.index("session_end")
    assert [r["reason"] for r in of_type(rows, "session_end")] == ["update"]
    assert p._stop_reason is None and not p._stream_active()
