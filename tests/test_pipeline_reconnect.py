"""pipeline 断流自动重连的决策循环：
  * 流断开 → 重新解析地址重连；
  * 解析明确说「没在播」（kind=offline）→ 宣布直播结束；
  * 连续重连预算用尽 → 报错收手；
  * 播得久（≥30s 音频）→ 重连预算重置；
  * 非 offline 的解析失败连续出现 → 触发 yt-dlp 保鲜。
全部用桩件驱动，不碰网络/模型/ffmpeg。"""
import asyncio
from types import SimpleNamespace


from app import pipeline as pipeline_mod
from app.pipeline import Pipeline
from app.resolver import ResolveError


class StubServer:
    def __init__(self):
        self.config = {}
        self.statuses = []

    async def status(self, state, detail=""):
        self.statuses.append((state, detail))

    async def broadcast(self, msg):
        pass


def make_pipeline(monkeypatch, tmp_path):
    # settings 落到临时目录，别碰真仓库；HF 缓存也指过去，
    # 免得下载进度 watcher 去扫真实缓存目录拖慢测试
    from app import settings
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setenv("HF_HOME", str(tmp_path))

    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source=None,
        beam=5, no_context=False, backend="auto", model=None,
        device="auto", compute_type="auto", denoise="off",
    )
    server = StubServer()
    p = Pipeline(args, server)

    # 模型加载旁路（不下载任何东西）
    import app.asr
    monkeypatch.setattr(app.asr, "create_transcriber", lambda **kw: object())
    # 退避等待旁路（否则一次测试要等一分钟）
    real_sleep = asyncio.sleep
    monkeypatch.setattr(pipeline_mod.asyncio, "sleep",
                        lambda *_a, **_k: real_sleep(0))
    return p, server


def run(coro):
    return asyncio.run(coro)


def test_offline_resolve_ends_stream(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    sessions = []

    async def fake_session(media, *a, **k):
        sessions.append(media)
        return True, 60.0            # 播了 60 秒后断流

    resolves = []

    async def fake_resolve(url, cookies=None):
        resolves.append(url)
        if len(resolves) == 1:
            return "http://cdn/stream.flv"
        raise ResolveError("主播现在没有开播。", kind="offline")

    import app.resolver
    monkeypatch.setattr(app.resolver, "resolve_stream_url", fake_resolve)
    monkeypatch.setattr(p, "_stream_session", fake_session)

    run(p._run_stream_inner("https://www.tiktok.com/@x/live"))
    assert sessions == ["http://cdn/stream.flv"]
    assert server.statuses[-1][0] == "ended"           # 真下播 → 正常结束
    assert "直播已结束" in server.statuses[-1][1]


def test_reconnect_resumes_with_fresh_url(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    sessions = []
    urls = iter(["http://cdn/a.flv", "http://cdn/b.flv"])

    async def fake_session(media, *a, **k):
        sessions.append(media)
        return True, 60.0

    calls = []

    async def fake_resolve(url, cookies=None):
        calls.append(url)
        try:
            return next(urls)
        except StopIteration:
            raise ResolveError("没开播", kind="offline") from None

    import app.resolver
    monkeypatch.setattr(app.resolver, "resolve_stream_url", fake_resolve)
    monkeypatch.setattr(p, "_stream_session", fake_session)

    run(p._run_stream_inner("https://www.tiktok.com/@x/live"))
    # 断流后拿到新地址重连了一次，且第二轮会话用的是新地址
    assert sessions == ["http://cdn/a.flv", "http://cdn/b.flv"]
    assert any(s[0] == "connecting" and "自动重连" in s[1] for s in server.statuses)
    assert server.statuses[-1][0] == "ended"


def test_reconnect_budget_exhausts(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    sessions = []

    async def fake_session(media, *a, **k):
        sessions.append(media)
        return False, 0.0            # 每轮都立刻失败（连不上音频）

    async def fake_resolve(url, cookies=None):
        return "http://cdn/dead.flv"   # 地址能解析但流拉不动

    import app.resolver
    monkeypatch.setattr(app.resolver, "resolve_stream_url", fake_resolve)
    monkeypatch.setattr(p, "_stream_session", fake_session)

    run(p._run_stream_inner("https://www.tiktok.com/@x/live"))
    assert server.statuses[-1][0] == "error"           # 预算用尽 → 明确报错
    assert len(sessions) == 6                          # 恰好首连 1 次 + 重连 5 次
    assert any("自动重连" in s[1] for s in server.statuses)


def test_good_run_resets_budget(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    outcomes = [(False, 0.0)] * 3 + [(True, 120.0)] + [(False, 0.0)] * 3
    sessions = []

    async def fake_session(media, *a, **k):
        sessions.append(media)
        return outcomes[len(sessions) - 1]

    async def fake_resolve(url, cookies=None):
        if len(sessions) >= len(outcomes):     # 剧本演完 → 主播下播收尾
            raise ResolveError("没开播", kind="offline")
        return "http://cdn/s.flv"

    import app.resolver
    monkeypatch.setattr(app.resolver, "resolve_stream_url", fake_resolve)
    monkeypatch.setattr(p, "_stream_session", fake_session)

    run(p._run_stream_inner("https://www.tiktok.com/@x/live"))
    # 3 次快速失败没有耗尽预算——中间那次 120 秒的正常播放把预算重置了，
    # 之后还扛得住 3 次失败并以「直播结束」正常收尾。
    # 若没有重置逻辑，第 6 轮会话前预算就会用尽、以 error 收场。
    assert len(sessions) == 7
    assert server.statuses[-1][0] == "ended"


def test_stream_session_accounts_real_audio_duration(monkeypatch, tmp_path):
    """不 stub _stream_session 的窄集成：音频时长必须按真实帧数累计
    （墙钟记账会在网络劣化时把「假连接」当成播得好好的）。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    import app.audio
    from app.audio import FRAME_BYTES

    class FakeSource:
        def __init__(self, media, denoise_model=None):
            pass

        async def frames(self):
            for _ in range(40):                  # 4.0 秒静音音频后流结束
                yield b"\x00" * FRAME_BYTES

        def stderr_tail(self):
            return ""

        async def stop(self):
            pass

    monkeypatch.setattr(app.audio, "FFmpegAudioSource", FakeSource)

    class DummyTranscriber:
        def transcribe(self, pcm):
            return "", None

    async def scenario():
        loop = asyncio.get_running_loop()
        return await p._stream_session(
            "http://cdn/s.flv", DummyTranscriber(), None, "live-note", loop)

    got_audio, audio_secs = run(scenario())
    assert got_audio is True
    assert abs(audio_secs - 4.0) < 1e-6
    assert ("live", "live-note") in server.statuses   # 收到首帧才宣布直播中


def test_direct_url_ends_cleanly_after_good_run(monkeypatch, tmp_path):
    """直连 .flv 地址播过一阵后断流 → 直接按「已结束」收尾，不做徒劳重连
    （直连地址重新解析不出「主播是否还在播」）。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    sessions = []

    async def fake_session(media, *a, **k):
        sessions.append(media)
        return True, 60.0

    async def fake_resolve(url, cookies=None):
        return url                               # 直连地址原样放行

    import app.resolver
    monkeypatch.setattr(app.resolver, "resolve_stream_url", fake_resolve)
    monkeypatch.setattr(p, "_stream_session", fake_session)

    run(p._run_stream_inner("https://cdn.example.com/room/stream.flv"))
    assert len(sessions) == 1                    # 播完即收，零重连
    assert server.statuses[-1][0] == "ended"


def test_direct_url_dead_stream_gets_one_retry(monkeypatch, tmp_path):
    """直连地址拉不到音频：预算压到 1 次重试，不空耗五轮退避。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    sessions = []

    async def fake_session(media, *a, **k):
        sessions.append(media)
        return False, 0.0

    async def fake_resolve(url, cookies=None):
        return url

    import app.resolver
    monkeypatch.setattr(app.resolver, "resolve_stream_url", fake_resolve)
    monkeypatch.setattr(p, "_stream_session", fake_session)

    run(p._run_stream_inner("https://cdn.example.com/room/dead.flv"))
    assert len(sessions) == 2                    # 首连 + 1 次重试
    assert server.statuses[-1][0] == "error"


def test_resolve_failures_trigger_ytdlp_freshen(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    freshened = []

    class StubUpdater:
        async def freshen_ytdlp(self, reason="periodic"):
            freshened.append(reason)

    p.updater = StubUpdater()

    async def fake_resolve(url, cookies=None):
        raise ResolveError("HTTP Error 500", kind="unknown")

    import app.resolver
    monkeypatch.setattr(app.resolver, "resolve_stream_url", fake_resolve)

    async def scenario():
        await p._run_stream_inner("https://www.tiktok.com/@x/live")
        await p._run_stream_inner("https://www.tiktok.com/@x/live")
        await asyncio.sleep(0)       # 让 ensure_future 的保鲜任务跑起来

    run(scenario())
    assert freshened == ["resolve-failures"]           # 连续 2 次失败才触发，且只一次
    assert p._resolve_fail_streak == 2


def test_offline_failures_do_not_trigger_freshen(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    freshened = []

    class StubUpdater:
        async def freshen_ytdlp(self, reason="periodic"):
            freshened.append(reason)

    p.updater = StubUpdater()

    async def fake_resolve(url, cookies=None):
        raise ResolveError("没开播", kind="offline")

    import app.resolver
    monkeypatch.setattr(app.resolver, "resolve_stream_url", fake_resolve)

    async def scenario():
        for _ in range(4):           # 主播没开播是常态，绝不能触发升级
            await p._run_stream_inner("https://www.tiktok.com/@x/live")
        await asyncio.sleep(0)

    run(scenario())
    assert freshened == []
    assert p._resolve_fail_streak == 0
