"""pipeline 断流自动重连的决策循环：
  * 流断开 → 重新解析地址重连；
  * 解析明确说「没在播」（kind=offline）→ 宣布直播结束；
  * 连续重连预算用尽 → 报错收手；
  * 播得久（≥30s 音频）→ 重连预算重置；
  * 非 offline 的解析失败连续出现 → 触发 yt-dlp 保鲜；
  * 界面上的「第 N/M 次」跟着放弃规则数：N 不超过 M，放弃前最后一次正好是 M/M。
全部用桩件驱动，不碰网络/模型/ffmpeg。"""
import asyncio
import random
import re
from types import SimpleNamespace

import pytest

from app import pipeline as pipeline_mod
from app.pipeline import Pipeline
from app.resolver import ResolveError
from tests.helpers import run


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
        beam=5, context=False, asr_temperature=None, glossary=None, backend="auto", model=None,
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


def test_offline_resolve_ends_stream(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    sessions = []

    async def fake_session(media, *a, **k):
        sessions.append(media)
        return True, 60.0            # 播了 60 秒后断流

    resolves = []

    async def fake_resolve(url, cookies=None, cookies_browser="auto", trace=None):
        resolves.append(url)
        if len(resolves) == 1:
            return "http://cdn/stream.flv"
        raise ResolveError("主播现在没有开播。", kind="offline", status=4)

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

    async def fake_resolve(url, cookies=None, cookies_browser="auto", trace=None):
        calls.append(url)
        try:
            return next(urls)
        except StopIteration:
            raise ResolveError("没开播", kind="offline", status=4) from None

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

    async def fake_resolve(url, cookies=None, cookies_browser="auto", trace=None):
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

    async def fake_resolve(url, cookies=None, cookies_browser="auto", trace=None):
        if len(sessions) >= len(outcomes):     # 剧本演完 → 主播下播收尾
            raise ResolveError("没开播", kind="offline", status=4)
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
    """直连 .flv 地址播过一阵后断流、而且这个地址已经拉不到数据 → 按「已结束」收尾，
    不做徒劳重连（直连地址重新解析不出「主播是否还在播」）。地址还出数据的情况见
    tests/test_resilience_stream.py：那时要重连。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    sessions = []
    probes = []

    async def dead(url, timeout=8):
        probes.append(url)
        return False

    import app.resolver
    monkeypatch.setattr(app.resolver, "_media_url_works", dead)

    async def fake_session(media, *a, **k):
        sessions.append(media)
        return True, 60.0

    async def fake_resolve(url, cookies=None, cookies_browser="auto", trace=None):
        return url                               # 直连地址原样放行

    import app.resolver
    monkeypatch.setattr(app.resolver, "resolve_stream_url", fake_resolve)
    monkeypatch.setattr(p, "_stream_session", fake_session)

    run(p._run_stream_inner("https://cdn.example.com/room/stream.flv"))
    assert len(sessions) == 1                    # 播完即收，零重连
    assert probes == ["https://cdn.example.com/room/stream.flv"]
    assert server.statuses[-1][0] == "ended"
    assert "拉不到数据" in server.statuses[-1][1]


def test_direct_url_dead_stream_gets_one_retry(monkeypatch, tmp_path):
    """直连地址拉不到音频：预算压到 1 次重试，不空耗五轮退避。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    sessions = []

    async def fake_session(media, *a, **k):
        sessions.append(media)
        return False, 0.0

    async def fake_resolve(url, cookies=None, cookies_browser="auto", trace=None):
        return url

    import app.resolver
    monkeypatch.setattr(app.resolver, "resolve_stream_url", fake_resolve)
    monkeypatch.setattr(p, "_stream_session", fake_session)

    run(p._run_stream_inner("https://cdn.example.com/room/dead.flv"))
    assert len(sessions) == 2                    # 首连 + 1 次重试
    assert server.statuses[-1][0] == "error"


# ---- 「第 N/M 次」：显示跟放弃规则一致，放弃时机不变 ------------------------------------------
#
# 放弃规则：连续无声的轮次（拉流一帧音频都没有，或重连时解析失败）超过预算就放弃；拿到过音频的
# 轮次把这个计数清零。房间地址预算 5，粘贴的流地址预算 1。以前界面显示的 N 是退避用的计数，
# 它只在播满 30 秒时清零——网络劣化、每轮只播二十几秒时会出现「第 6/5 次」「第 11/5 次」。

ATTEMPT = re.compile(r"自动重连（第 (\d+)/(\d+) 次）")
F = (False, 0.0)       # 一帧音频都没有
S = (True, 20.0)       # 播了 20 秒就断（有声，但不到 30 秒，不清退避）
LONG = (True, 60.0)    # 播了 60 秒
ROOM = "https://www.tiktok.com/@x/live"
DIRECT = "https://cdn.example.com/room/stream.flv"


def drive(monkeypatch, tmp_path, url, rounds, resolves=(), back_live=False):
    """按剧本跑一场，返回 (事件序列, server)。

    rounds：每轮拉流的 (有没有音频, 音频秒数)，演完之后一律无声。resolves：首次解析之后每次
    重连解析的结果，缺省给地址；ResolveError 照抛。back_live：解析说「没在播」时，房间复查
    立刻回到在播（_confirm_offline 回 live）。事件按发生顺序记：("round", 有没有音频)、
    ("resolve_fail",)、("back_live",)、("shown", N, M)。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    events = []
    rounds, resolves = iter(rounds), iter(resolves)
    first = [True]

    async def fake_session(media, *a, **k):
        got, secs = next(rounds, F)
        events.append(("round", got))
        return got, secs

    async def fake_resolve(url, cookies=None, cookies_browser="auto", trace=None):
        if first[0]:
            first[0] = False
            return "http://cdn/s.flv"
        item = next(resolves, "http://cdn/s.flv")
        if isinstance(item, ResolveError):
            events.append(("back_live",) if item.kind == "offline" else ("resolve_fail",))
            raise item
        return item

    async def still_serving(url, timeout=8):
        return True                      # 直连地址播满 30 秒后探活：地址还出数据，照常重连

    async def room_back_live(url, exc, sess, waited):
        return "live", waited

    real_status = server.status

    async def status(state, detail=""):
        m = ATTEMPT.search(detail)
        if m:
            events.append(("shown", int(m.group(1)), int(m.group(2))))
        await real_status(state, detail)

    import app.resolver
    monkeypatch.setattr(app.resolver, "resolve_stream_url", fake_resolve)
    monkeypatch.setattr(app.resolver, "_media_url_works", still_serving)
    monkeypatch.setattr(p, "_stream_session", fake_session)
    if back_live:
        monkeypatch.setattr(p, "_confirm_offline", room_back_live)
    server.status = status
    run(p._run_stream_inner(url))
    return events, server


def shown(events):
    return [(e[1], e[2]) for e in events if e[0] == "shown"]


def rounds_run(events):
    return sum(1 for e in events if e[0] == "round")


def assert_counts_follow_the_rule(events, budget):
    """从事件序列复核，不照抄实现：
      * 放弃恰好发生在「上一次有声之后第 budget+1 个无声轮次」，之前一次都没超过；
      * 每串无声（两次有声之间）里：第一条提示是第 1 次，每多一个无声轮次加 1，只是房间回到
        在播（没耗预算）时不加；M 在这一串里不变，而且等于从这里起规则还允许的尝试次数；
      * N 永远不超过 M，放弃前的最后一条提示正好是 M/M。"""
    silent, prev = 0, None      # prev：这一串里上一条提示 (N, M, 当时的 silent)
    for ev in events:
        if ev[0] == "round":
            silent = 0 if ev[1] else silent + 1
            if ev[1]:
                prev = None
        elif ev[0] == "resolve_fail":
            silent += 1
        elif ev[0] == "shown":
            n, m = ev[1], ev[2]
            assert 1 <= n <= m, events
            if prev is None:
                assert n == 1, events
                assert m == budget + 1 - silent, events     # 从这里起还能试几次
            else:
                assert m == prev[1], events
                assert n == prev[0] + (silent - prev[2]), events
            prev = (n, m, silent)
        assert silent <= budget + 1, events
    assert silent == budget + 1, events
    assert prev is not None and prev[0] == prev[1], events


@pytest.mark.parametrize("url,rounds,want_shown,want_rounds", [
    # 首连就没声音：首连算掉一轮，剩 5 次重连，1/5 … 5/5（这条以前就对）
    (ROOM, [F] * 6, [(n, 5) for n in range(1, 6)], 6),
    # 播了一阵之后断：还能试 6 次（无声 0…5 各试一次），以前显示到「第 6/5 次」
    (ROOM, [LONG] + [F] * 6, [(n, 6) for n in range(1, 7)], 7),
    # 网络劣化、每轮只播 20 秒：每轮有声都把预算清零，以前的 N 却一路涨到 11
    (ROOM, [S] * 6 + [F] * 6, [(1, 6)] * 6 + [(n, 6) for n in range(2, 7)], 12),
    # 有声无声交替：每串无声都从第 1 次数起
    (ROOM, [F, F, S, F, F, F, S] + [F] * 6,
     [(1, 5), (2, 5), (1, 6), (2, 6), (3, 6), (4, 6)] + [(n, 6) for n in range(1, 7)], 13),
    # 播满 30 秒同样清零
    (ROOM, [F, F, F, LONG] + [F] * 6, [(1, 5), (2, 5), (3, 5)] + [(n, 6) for n in range(1, 7)], 10),
    # 粘贴的流地址，预算 1：首连就没声音 → 只重连 1 次
    (DIRECT, [F, F], [(1, 1)], 2),
    # 粘贴的流地址播了一阵后断：还能试 2 次，以前显示「第 2/1 次」
    (DIRECT, [S, F, F], [(1, 2), (2, 2)], 3),
    (DIRECT, [LONG, F, F], [(1, 2), (2, 2)], 3),
    (DIRECT, [S, S, S, F, F], [(1, 2), (1, 2), (1, 2), (2, 2)], 5),
])
def test_attempt_count_follows_the_give_up_rule(monkeypatch, tmp_path, url, rounds, want_shown,
                                                want_rounds):
    events, server = drive(monkeypatch, tmp_path, url, rounds)
    assert shown(events) == want_shown
    assert rounds_run(events) == want_rounds                # 放弃时机与改显示之前一样
    assert server.statuses[-1][0] == "error"
    assert_counts_follow_the_rule(events, budget=1 if url == DIRECT else 5)


def test_a_failed_lookup_counts_as_one_silent_attempt(monkeypatch, tmp_path):
    fail = ResolveError("HTTP Error 500", kind="unknown")
    ok = "http://cdn/s.flv"
    events, server = drive(monkeypatch, tmp_path, ROOM, [LONG] + [F] * 6,
                           resolves=[fail, ok, fail, ok, ok, ok])
    assert shown(events) == [(n, 6) for n in range(1, 7)]
    assert rounds_run(events) == 5                          # 两次解析失败各顶掉一轮拉流
    assert server.statuses[-1][0] == "error"
    assert_counts_follow_the_rule(events, budget=5)


def test_a_room_back_live_does_not_use_up_an_attempt(monkeypatch, tmp_path):
    offline = ResolveError("offline", kind="offline", status=3)
    events, server = drive(monkeypatch, tmp_path, ROOM, [LONG] + [F] * 6,
                           resolves=[offline], back_live=True)
    # 房间复查回到在播：马上重新解析，这一次不算，界面上仍是第 1 次
    assert shown(events) == [(1, 6)] + [(n, 6) for n in range(1, 7)]
    assert rounds_run(events) == 7
    assert server.statuses[-1][0] == "error"
    assert_counts_follow_the_rule(events, budget=5)


@pytest.mark.parametrize("seed", range(24))
def test_attempt_count_on_random_sound_and_silence(monkeypatch, tmp_path, seed):
    """随机的有声/无声/解析失败序列：显示规则与放弃规则处处一致。"""
    rng = random.Random(seed)
    url = DIRECT if seed % 3 == 0 else ROOM
    budget = 1 if url == DIRECT else 5
    head = [rng.choice([F, F, S, LONG]) for _ in range(rng.randint(0, 12))]
    rounds = head + [F] * (budget + 2)        # 最后一串无声保证走到放弃
    resolves = [] if url == DIRECT else [
        ResolveError("HTTP Error 500", kind="unknown") if rng.random() < 0.2 else "http://cdn/s.flv"
        for _ in range(40)]
    events, server = drive(monkeypatch, tmp_path, url, rounds, resolves=resolves)
    assert server.statuses[-1][0] == "error"
    assert_counts_follow_the_rule(events, budget)


def test_resolve_failures_trigger_ytdlp_freshen(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    freshened = []

    class StubUpdater:
        async def freshen_ytdlp(self, reason="periodic"):
            freshened.append(reason)

    p.updater = StubUpdater()

    async def fake_resolve(url, cookies=None, cookies_browser="auto", trace=None):
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

    async def fake_resolve(url, cookies=None, cookies_browser="auto", trace=None):
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


def test_corrupt_denoise_model_deleted_and_disabled(monkeypatch, tmp_path):
    """截断的降噪模型（下载中断留下的）必须被删除并禁用降噪——
    留着会让之后每一轮 ffmpeg 都起不来。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    bad = tmp_path / "bd.rnnn"
    bad.write_bytes(b"rnnoise" + b"x" * 50_000)      # 带正确 magic 的截断文件
    monkeypatch.setattr(pipeline_mod, "DENOISE_MODEL", bad)
    monkeypatch.setattr(pipeline_mod, "_arnndn_probe", lambda path: False)

    p.args.denoise = "auto"
    result = run(p._ensure_denoise_model())
    assert result is None
    assert not bad.exists()                          # 坏文件已清除


def test_valid_denoise_model_passes_probe(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    good = tmp_path / "bd.rnnn"
    good.write_bytes(b"rnnoise" + b"x" * 300_000)
    monkeypatch.setattr(pipeline_mod, "DENOISE_MODEL", good)
    monkeypatch.setattr(pipeline_mod, "_arnndn_probe", lambda path: True)

    p.args.denoise = "auto"
    assert run(p._ensure_denoise_model()) == str(good)
    assert good.exists()


def test_fullsize_model_failing_probe_kept_but_disabled(monkeypatch, tmp_path):
    """完整大小但探针失败（如 ffmpeg 不含 arnndn）：禁用降噪但保留文件——
    换一个带 arnndn 的 ffmpeg 后它还能用。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    good = tmp_path / "bd.rnnn"
    good.write_bytes(b"rnnoise" + b"x" * 300_000)
    monkeypatch.setattr(pipeline_mod, "DENOISE_MODEL", good)
    monkeypatch.setattr(pipeline_mod, "_arnndn_probe", lambda path: False)

    p.args.denoise = "auto"
    assert run(p._ensure_denoise_model()) is None
    assert good.exists()


def test_cancelled_model_load_does_not_poison_key(monkeypatch, tmp_path):
    """模型加载中被取消后，「已就绪 key」不能被在途 key 污染——否则换了识别
    参数再启动会静默复用旧模型，新参数至死不生效。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    p._transcriber = object()                  # 假装上一场已加载好模型 T1
    p._transcriber_key = ("old-key",)

    async def scenario():
        # asyncio.Event() 在 3.9 上构造即绑定当前事件循环，必须在协程里创建
        started = asyncio.Event()

        async def never_finishes():
            started.set()
            await asyncio.sleep(3600)

        def fake_executor(_none, fn):          # 加载永不完成
            return asyncio.ensure_future(never_finishes())

        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "run_in_executor", fake_executor)

        import app.resolver
        async def fake_resolve(url, cookies=None, cookies_browser="auto", trace=None):
            return "http://cdn/s.flv"
        monkeypatch.setattr(app.resolver, "resolve_stream_url", fake_resolve)

        p.args.source = "en"                   # 换参数 → key 变化，进入加载分支
        task = asyncio.ensure_future(p._run_stream_inner("https://www.tiktok.com/@x/live"))
        await started.wait()
        task.cancel()                          # 加载中取消（用户点了停止）
        try:
            await task
        except asyncio.CancelledError:
            pass

    run(scenario())
    # 已就绪 key 仍是旧模型的 key：下次同参数启动会重新走加载分支
    assert p._transcriber_key == ("old-key",)
    assert p._loading_key is not None and p._loading_key != ("old-key",)


def test_demo_runs_as_cancellable_task(monkeypatch, tmp_path):
    """演示模式必须挂到 _stream_task 上：点「停止」要能真的停下来。"""
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def scenario():
        await p.start_demo()
        assert p._stream_task is not None and not p._stream_task.done()
        await asyncio.sleep(0)
        await p.stop_stream()                  # 用户点「停止」
        assert p._stream_task is None

    run(scenario())
    assert server.statuses[-1][0] == "idle"
