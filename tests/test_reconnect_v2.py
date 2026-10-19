"""重连新规则 V2（2026-10-19，改进清单第 3 条；app/reconnect_policy.py V2Policy）。

  * 收到过任何直播音频就清零退避；连续无声 2、4、8、16、30 秒；无声 30 分钟后 60 秒、令牌补得更慢；
  * 有声之后的第一次重连用原来的流地址，不查；又是一帧都没有，下一次才查；原地址明确拉不动时只有第一次
    免退避；连续几轮都只有开头几秒有声，照样去查；
  * 房间地址无声轮次不放弃，查询连续失败才放弃；粘贴的流地址照旧 1 次；
  * 查流地址的令牌桶：到了上限改用原来的流地址，界面说的是程序自己的规矩；等房间恢复时的复查也扣令牌；
  * 用户自带流地址的 2KB 探活不扣令牌；
  * 文字中英逐字，不出现第八条的禁用词。

这个文件显式把 RECONNECT_V2 打开（不靠默认值）；V1 的规则在 tests/test_pipeline_reconnect.py，钉在 V1 上。
全部用桩件：不联网、不起 ffmpeg、不加载模型。
"""
import asyncio

import pytest

from app import i18n, reconnect_policy, resolver
from app.reconnect_policy import TokenBucket, V1Policy, V2Policy, make_policy, v2_enabled
from app.resolver import ResolveError
from tests import i18n_rules as rules
from tests.test_resilience_stream import (CAUSE_LABELS, DIRECT, ROOM, audit_rows, make_pipeline,
                                          of_type)
from tests.helpers import run

EN = i18n.EN


@pytest.fixture(autouse=True)
def _v2(monkeypatch):
    monkeypatch.setattr(reconnect_policy, "RECONNECT_V2", True)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


# ---- 开关 ----------------------------------------------------------------------------------

def test_the_switch_and_its_emergency_override(monkeypatch):
    assert isinstance(make_policy(False), V2Policy)
    assert v2_enabled({"TLT_RECONNECT_V2": "0"}) is False
    assert v2_enabled({"TLT_RECONNECT_V2": "off"}) is False
    monkeypatch.setattr(reconnect_policy, "RECONNECT_V2", False)
    assert isinstance(make_policy(False), V1Policy)
    assert v2_enabled({"TLT_RECONNECT_V2": "1"}) is True
    assert reconnect_policy.session_start_fields() == {"reconnect_policy": "v1"}


# ---- 令牌桶 ---------------------------------------------------------------------------------

def test_the_bucket_holds_six_and_refills_one_every_three_minutes():
    clock = Clock()
    bucket = TokenBucket(6, 180.0, clock=clock)
    assert [bucket.take() for _ in range(7)] == [True] * 6 + [False]
    assert bucket.next_in() == pytest.approx(180.0)
    clock.t += 90
    assert bucket.available() == 0 and bucket.next_in() == pytest.approx(90.0)
    clock.t += 90
    assert bucket.take() is True and bucket.take() is False
    assert bucket.per_hour_max() == 26          # 任意 60 分钟最多 6 + 20 次，不是 20 次


def test_time_the_app_was_not_running_does_not_refill_the_bucket():
    clock = Clock()
    bucket = TokenBucket(6, 180.0, clock=clock)
    for _ in range(6):
        bucket.take()
    clock.t += 600                               # Windows 睡了 10 分钟：单调时钟照走
    bucket.shift(600)
    assert bucket.available() == 0


# ---- 规则本身 -------------------------------------------------------------------------------

def silent(policy, n=1, mode="lookup", dead=False):
    for _ in range(n):
        policy.round_ended(False, 0.0, mode=mode, dead=dead)


def test_any_audio_resets_the_backoff_and_silence_climbs_to_thirty():
    policy = V2Policy(False, clock=Clock())
    delays = []
    policy.round_ended(True, 8.0, mode="lookup")
    delays.append(policy.plan().delay)
    for _ in range(6):
        policy.round_ended(False, 0.0, mode="lookup")
        delays.append(policy.plan().delay)
    assert delays == [2, 2, 4, 8, 16, 30, 30]
    policy.round_ended(True, 3.0, mode="lookup")
    assert policy.plan().delay == 2


def test_after_audio_the_old_url_is_reused_and_a_silent_reuse_leads_to_a_lookup():
    policy = V2Policy(False, clock=Clock())
    policy.round_ended(True, 60.0, mode="lookup")
    assert policy.plan().mode == "reuse"
    policy.round_ended(False, 0.0, mode="reuse")
    assert policy.plan().mode == "lookup"
    policy.lookup_ok()
    policy.round_ended(False, 0.0, mode="lookup")    # 刚查回来的地址也没声音：接着查（受令牌限制）
    assert policy.plan().mode == "lookup"


def test_a_dead_old_url_is_looked_up_at_once_but_only_the_first_time():
    """原地址明确拉不动（HTTP 4xx、起来几秒就退出）：马上去查。只有这一串无声里的第一次免退避——
    查回来的多半还是同一个地址，不免的话几秒内就把令牌扣光。"""
    policy = V2Policy(False, clock=Clock())
    policy.round_ended(True, 60.0, mode="lookup")
    assert policy.plan().mode == "reuse"
    policy.round_ended(False, 0.0, mode="reuse", dead=True)
    first = policy.plan()
    assert (first.mode, first.delay) == ("lookup", 0)
    policy.lookup_ok()
    policy.round_ended(True, 40.0, mode="lookup")
    assert policy.plan().mode == "reuse"
    policy.round_ended(False, 0.0, mode="reuse", dead=True)
    assert policy.plan().delay == 0                  # 有声之后是新的一串：又有一次
    policy.lookup_ok()
    policy.round_ended(False, 0.0, mode="lookup", dead=True)
    assert policy.plan().delay > 0


def test_rounds_with_only_a_few_seconds_of_audio_do_not_keep_the_old_url_forever():
    """10-18 有声的轮次只有 5.7–21.5 秒，像是连上时 CDN 先灌的一段缓存：连续三轮这样，下一次照样去查。"""
    policy = V2Policy(False, clock=Clock())
    modes = []
    for _ in range(4):
        policy.round_ended(True, 8.0, mode=modes[-1] if modes else "lookup")
        plan = policy.plan()
        modes.append(plan.mode)
        if plan.mode == "lookup":
            policy.lookup_ok()
    assert modes == ["reuse", "reuse", "lookup", "reuse"]


def test_silence_alone_never_gives_up_on_a_live_link():
    policy = V2Policy(False, clock=Clock())
    for _ in range(100):
        policy.round_ended(False, 0.0, mode="lookup")
        plan = policy.plan()
        assert plan is not None
        if plan.mode == "lookup":
            policy.take_lookup()
            policy.lookup_ok()


def test_consecutive_lookup_failures_still_give_up():
    policy = V2Policy(False, clock=Clock())
    silent(policy)
    for _ in range(6):
        assert policy.plan() is not None
        policy.lookup_failed()
    assert policy.plan() is None
    assert policy.exhausted() == {"reason": "reconnect_exhausted", "lookup_failures": 6,
                                  "budget": 5, "policy": "v2"}


def test_a_pasted_stream_url_keeps_its_single_retry_and_takes_no_tokens():
    policy = V2Policy(True, clock=Clock())
    silent(policy)
    assert policy.plan().mode == "lookup"
    assert all(policy.take_lookup() for _ in range(20))
    assert policy.bucket.available() == 6
    silent(policy)
    assert policy.plan() is None


def test_an_empty_bucket_reuses_the_old_url_and_says_when_the_next_lookup_is():
    clock = Clock()
    policy = V2Policy(False, clock=clock)
    for _ in range(6):
        assert policy.take_lookup()
    silent(policy)
    plan = policy.plan()
    assert plan.mode == "reuse_capped" and plan.first_capped is True
    assert plan.next_lookup_sec == pytest.approx(180.0)
    silent(policy, mode="reuse_capped")
    again = policy.plan()
    assert again.mode == "reuse_failed_capped" and again.first_capped is False
    clock.t += 180
    assert policy.plan().mode == "lookup"
    assert policy.summary()["capped_sec"] == pytest.approx(180.0)


def test_half_an_hour_of_silence_slows_the_retries_and_the_lookups():
    clock = Clock()
    policy = V2Policy(False, clock=clock)
    silent(policy, 8)
    assert policy.plan(silent_sec=1799).delay == 30
    plan = policy.plan(silent_sec=1800)
    assert plan.delay == 60
    assert policy.bucket.refill_sec == 900.0 and policy.bucket.per_hour_max() == 10
    policy.round_ended(True, 50.0)
    assert policy.plan(silent_sec=0).delay == 2 and policy.bucket.refill_sec == 180.0


def test_a_room_back_live_is_looked_up_at_once():
    policy = V2Policy(False, clock=Clock())
    silent(policy, 3)
    policy.room_back_live()
    plan = policy.plan()
    assert (plan.mode, plan.delay) == ("lookup", 0)


def test_room_rechecks_wait_for_a_token():
    clock = Clock()
    policy = V2Policy(False, clock=clock)
    for _ in range(6):
        policy.take_lookup()
    assert policy.room_token_wait() == pytest.approx(180.0)
    clock.t += 180
    assert policy.room_token_wait() == 0.0


# ---- 接进重连循环 ---------------------------------------------------------------------------

F = (False, 0.0)
S = (True, 20.0)
LONG = (True, 60.0)


def drive(monkeypatch, tmp_path, url, rounds, resolves=(), on_round=None):
    """按剧本跑一场：rounds 是每轮 (有没有直播音频, 秒数)，演完之后解析一律说「已结束」（状态 4）。
    返回 (事件序列, server, pipeline)。事件：("round", 媒体地址)、("resolve",)。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    events = []
    script = list(rounds)
    extra = iter(resolves)

    async def fake_session(media, *a, sess=None, **k):
        events.append(("round", media))
        n = sum(1 for e in events if e[0] == "round")
        if on_round is not None:
            on_round(n, sess)
        sess["break_no"] += 1
        return script[n - 1] if n <= len(script) else F

    async def fake_resolve(url, cookies=None, cookies_browser="auto", trace=None):
        events.append(("resolve",))
        n = sum(1 for e in events if e[0] == "resolve")
        if n == 1:
            return "http://cdn/s.flv"
        if sum(1 for e in events if e[0] == "round") > len(script):
            raise ResolveError("没开播", kind="offline", status=4)
        item = next(extra, "http://cdn/s.flv")
        if isinstance(item, BaseException):
            raise item
        return item

    async def reachable(*a, **k):
        return True, ""

    monkeypatch.setattr(resolver, "resolve_stream_url", fake_resolve)
    monkeypatch.setattr(resolver, "tiktok_reachable", reachable)
    monkeypatch.setattr(p, "_stream_session", fake_session)
    run(p._run_stream_inner(url))
    return events, server, p


def sequence(events):
    return ["R" if e[0] == "round" else "L" for e in events]


def test_after_audio_the_first_reconnect_does_not_ask_tiktok(monkeypatch, tmp_path):
    events, server, p = drive(monkeypatch, tmp_path, ROOM, [LONG, S, F, F])
    # 开场查一次；有声两轮都用原地址；无声 → 查；又无声 → 查；剧本演完 → 查到已结束
    assert sequence(events) == ["L", "R", "R", "R", "L", "R", "L", "R", "L"]
    assert server.statuses[-1][0] == "ended"
    rows = audit_rows(tmp_path)
    plans = [(r["mode"], r["delay_sec"]) for r in of_type(rows, "reconnect_plan")]
    assert plans == [("reuse", 2), ("reuse", 2), ("lookup", 2), ("lookup", 4), ("lookup", 8)]
    end = of_type(rows, "session_end")[0]
    assert end["reason"] == "offline" and end["reused"] == 2 and end["lookups"] == 4


def test_twelve_silent_rounds_do_not_end_monitoring(monkeypatch, tmp_path):
    monkeypatch.setattr(V2Policy, "LOOKUP_BUCKET", (100, 180.0))     # 这条只看「无声不放弃」
    events, server, p = drive(monkeypatch, tmp_path, ROOM, [F] * 12)
    assert sum(1 for e in events if e[0] == "round") == 13
    assert "error" not in [state for state, _ in server.statuses]
    assert of_type(audit_rows(tmp_path), "session_end")[0]["reason"] == "offline"


def test_a_dead_old_url_is_looked_up_without_waiting(monkeypatch, tmp_path):
    def dead_second_round(n, sess):
        if n == 2:
            sess["last_round"] = {"dead": True}

    events, server, p = drive(monkeypatch, tmp_path, ROOM, [LONG, F, F], on_round=dead_second_round)
    texts = [d for _, d in server.statuses if "中断" in d]
    assert texts[1] == "直播流中断，正在重新查流地址（本场第 2 次中断）…"
    assert i18n.render(texts[1], EN) == ("The stream was interrupted. Looking up the stream URL "
                                         "again (interruption 2 this session)…")


def test_a_pasted_stream_url_still_gets_one_retry(monkeypatch, tmp_path):
    events, server, p = drive(monkeypatch, tmp_path, DIRECT, [F, F, F])
    assert sum(1 for e in events if e[0] == "round") == 2
    assert server.statuses[-1][0] == "error"
    end = of_type(audit_rows(tmp_path), "session_end")[0]
    assert end["reason"] == "reconnect_exhausted" and end["budget"] == 1


def test_failed_lookups_still_end_monitoring(monkeypatch, tmp_path):
    fail = ResolveError("HTTP Error 500", kind="unknown")
    monkeypatch.setattr(V2Policy, "LOOKUP_BUCKET", (100, 180.0))     # 这条只看「连续失败才放弃」
    events, server, p = drive(monkeypatch, tmp_path, ROOM, [F] * 3, resolves=[fail] * 10)
    assert server.statuses[-1][0] == "error"
    end = of_type(audit_rows(tmp_path), "session_end")[0]
    assert end["reason"] == "reconnect_exhausted" and end["lookup_failures"] == 6


def test_an_empty_bucket_reconnects_to_the_same_url_and_says_so(monkeypatch, tmp_path):
    monkeypatch.setattr(V2Policy, "LOOKUP_BUCKET", (2, 180.0))

    def refill_late(n, sess):
        if n == 6:
            sess["policy"].bucket.tokens = 1.0     # 过了一个补充间隔：又能查了

    events, server, p = drive(monkeypatch, tmp_path, ROOM, [F] * 5, on_round=refill_late)
    # 开场 1 次、第一次断流查 1 次，桶空了：之后用原地址，直到又有令牌（剧本演完，查到已结束）
    assert sequence(events)[:4] == ["L", "R", "L", "R"]
    assert sequence(events).count("L") == 3
    capped = [d for _, d in server.statuses if "每小时最多" in d]
    assert capped[0] == ("直播流中断，4 秒后用同一个流地址重连（本场第 2 次中断）。为少向 TikTok 发请求，"
                         "程序每小时最多查 22 次流地址，3 分钟后再查。")
    assert any(d.startswith("同一个流地址拉不到数据。为少向 TikTok 发请求，程序每小时最多查 22 次流地址")
               for d in capped)
    rows = audit_rows(tmp_path)
    assert len(of_type(rows, "lookup_capped")) == 1
    assert of_type(rows, "lookup_capped")[0]["per_hour"] == 22
    assert of_type(rows, "session_end")[0]["reason"] == "offline"


def test_room_rechecks_take_tokens_and_wait_when_the_bucket_is_empty(monkeypatch, tmp_path):
    """等房间恢复在播：每次复查房间状态扣一个令牌，桶空了复查间隔拉长到下一个令牌（等的时间算进
    HOST_WAIT_MAX_SEC），V1 不变。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    from app.audit import AuditLog
    p.audit = AuditLog(room_url=ROOM, log_dir=tmp_path / "logs")
    sess = p._session_state = p._new_session_state(p.audit)
    sess["policy"] = V2Policy(False, clock=Clock())
    for _ in range(6):
        sess["policy"].take_lookup()
    probes = []

    async def probe(url):
        probes.append(url)
        return (2 if len(probes) == 2 else 3), None

    clock = sess["policy"]._clock

    async def sleep(sec, *a, **k):        # 退避和等令牌都不真等：只把令牌桶的钟往前拨
        clock.t += sec

    monkeypatch.setattr(p, "_probe_room_status", probe)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    outcome, waited = run(p._host_wait(ROOM, 3, sess, 0.0))
    assert outcome == "live" and len(probes) == 2
    assert waited >= 2 * p.HOST_WAIT_POLL_SEC + 180.0
    assert sess["requests"]["room_rechecks"] == 2


def test_a_pasted_audio_source_is_checked_without_tokens(monkeypatch, tmp_path):
    """用户自带流地址（直播间链接 + .flv）：V2 下这种场次从不查询，重连时 2KB 探活不扣令牌；
    探活拉不动才清掉它、回到正常解析（那时才扣）。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    p._media_override = "https://pull.example.com/x.flv"
    looked_up = []

    async def checked(url, trusted=False):
        return url

    async def works(url, timeout=8):
        return True

    async def fake_resolve(url, cookies=None, cookies_browser="auto", trace=None):
        looked_up.append(url)
        raise ResolveError("没开播", kind="offline", status=4)

    rounds = iter([F, F, F])

    async def fake_session(media, *a, sess=None, **k):
        sess["break_no"] += 1
        got = next(rounds, None)
        if got is None:
            p._media_override = None          # 剧本演完：用户的地址失效，回到正常解析
            return F
        return got

    async def reachable(*a, **k):
        return True, ""

    monkeypatch.setattr(resolver, "_check_media_url", checked)
    monkeypatch.setattr(resolver, "_media_url_works", works)
    monkeypatch.setattr(resolver, "resolve_stream_url", fake_resolve)
    monkeypatch.setattr(resolver, "tiktok_reachable", reachable)
    monkeypatch.setattr(p, "_stream_session", fake_session)
    run(p._run_stream_inner(ROOM))
    end = of_type(audit_rows(tmp_path), "session_end")[0]
    assert end["reason"] == "offline" and len(looked_up) == 1
    assert end["lookups"] == 1                         # 只有最后那一次真的查了


# ---- 文字 ----------------------------------------------------------------------------------

def _text(p, sess, plan):
    return p._reconnect_text(sess, sess["policy"], plan)


@pytest.mark.parametrize("mode,delay,zh,en", [
    ("lookup", 4, "直播流中断，4 秒后自动重连（本场第 7 次中断）…",
     "The stream was interrupted. Reconnecting in 4 sec (interruption 7 this session)…"),
    ("lookup", 0, "直播流中断，正在重新查流地址（本场第 7 次中断）…",
     "The stream was interrupted. Looking up the stream URL again (interruption 7 this session)…"),
    ("reuse", 2, "直播流中断，2 秒后用同一个流地址重连（本场第 7 次中断）…",
     "The stream was interrupted. Reconnecting to the same stream URL in 2 sec (interruption 7 "
     "this session)…"),
    ("reuse_capped", 8, "直播流中断，8 秒后用同一个流地址重连（本场第 7 次中断）。为少向 TikTok 发请求，"
     "程序每小时最多查 26 次流地址，2 分钟后再查。",
     "The stream was interrupted. Reconnecting to the same stream URL in 8 sec (interruption 7 "
     "this session). To keep requests to TikTok low, the app looks up the stream URL at most 26 "
     "times an hour. Next lookup in 2 min."),
    ("reuse_failed_capped", 8, "同一个流地址拉不到数据。为少向 TikTok 发请求，程序每小时最多查 26 次流地址，"
     "2 分钟后再查；程序会一直重试。",
     "The same stream URL isn’t returning data. To keep requests to TikTok low, the app looks up "
     "the stream URL at most 26 times an hour. Next lookup in 2 min. The app keeps retrying."),
])
def test_every_reconnect_line_reads_right_in_both_languages_and_names_no_cause(
        monkeypatch, tmp_path, mode, delay, zh, en):
    from app.reconnect_policy import Plan
    p, server = make_pipeline(monkeypatch, tmp_path)
    sess = p._new_session_state(None)
    sess["policy"] = V2Policy(False, clock=Clock())
    sess["break_no"] = 7
    text = _text(p, sess, Plan(delay, mode, next_lookup_sec=125.0))
    assert str(text) == zh
    assert i18n.render(text, EN) == en
    for label in CAUSE_LABELS:
        assert label not in zh
    assert not rules.ZH_LABELS.search(zh) and not rules.ZH_CAUSAL.search(zh)
    assert not rules.EN_LABELS.search(en) and not rules.EN_CAUSAL.search(en)


def test_the_next_lookup_never_reads_zero_minutes(monkeypatch, tmp_path):
    from app.reconnect_policy import Plan
    p, server = make_pipeline(monkeypatch, tmp_path)
    sess = p._new_session_state(None)
    sess["policy"] = V2Policy(False, clock=Clock())
    text = str(_text(p, sess, Plan(2, "reuse_capped", next_lookup_sec=20.0)))
    assert "20 秒后再查" in text and "0 分钟" not in text


def test_v1_keeps_its_words(monkeypatch, tmp_path):
    from app.reconnect_policy import Plan
    p, server = make_pipeline(monkeypatch, tmp_path)
    sess = p._new_session_state(None)
    sess["policy"] = V1Policy(False)
    assert str(_text(p, sess, Plan(4, "lookup", attempt=2, attempts=5))) == \
        "直播流中断，4 秒后自动重连（第 2/5 次）…"
