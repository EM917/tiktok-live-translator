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
from tests.test_resilience_stream import (CAUSE_LABELS, DIRECT, ROOM, T0, FakeGuard, audit_rows,
                                          make_pipeline, of_type, scripted_resolve)
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


def test_room_rechecks_wait_for_a_token_and_leave_one_for_the_lookup():
    """复查房间状态扣一个令牌，但桶里要给回到在播之后那次查询留一个：桶空时要等到有两个。"""
    clock = Clock()
    policy = V2Policy(False, clock=clock)
    for _ in range(6):
        policy.take_lookup()
    assert policy.room_token_eta() == pytest.approx(360.0)
    assert policy.room_token_wait() == pytest.approx(360.0)
    clock.t += 180
    assert policy.room_token_wait() == pytest.approx(180.0)     # 只有一个：留给查询
    clock.t += 180
    assert policy.room_token_wait() == 0.0
    assert policy.bucket.available() == 1


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


def _drained(p, server, tmp_path, refill=180.0):
    """一场里桶已经扣空（等房间恢复之前查过好几次）；sleep 只把令牌桶的钟往前拨，不真等。"""
    from app.audit import AuditLog
    p.audit = AuditLog(room_url=ROOM, log_dir=tmp_path / "logs")
    sess = p._session_state = p._new_session_state(p.audit)
    clock = Clock()
    policy = sess["policy"] = V2Policy(False, clock=clock)
    if refill != V2Policy.LOOKUP_BUCKET[1]:
        policy.bucket.set_refill(refill)
    for _ in range(6):
        policy.take_lookup()
    return sess, policy, clock


def _clock_sleep(monkeypatch, clock):
    """asyncio.sleep 不真等，只把令牌桶的钟往前拨（统计循环那种 sleep(0) 照样让出一次）。"""
    real_sleep = asyncio.tasks.sleep

    async def sleep(sec, *a, **k):
        clock.t += sec
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", sleep)


def test_a_long_room_wait_still_leaves_a_token_to_look_up_once_the_room_is_live(monkeypatch, tmp_path):
    """等房间恢复等得比桶撑得久：复查要给回到在播之后那次查询留一个令牌。以前复查把桶扣空，看到在播的
    那一刻反而查不了，只能先用原来的流地址，状态行还说「同一个流地址拉不到数据…3 分钟后再查」。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    sess, policy, clock = _drained(p, server, tmp_path)
    policy.round_ended(False, 0.0, mode="reuse")     # 断流前那一轮用的是原地址、没声音
    answers = iter([3, 2])

    async def probe(url):
        return next(answers), None

    monkeypatch.setattr(p, "_probe_room_status", probe)
    _clock_sleep(monkeypatch, clock)
    outcome, waited = run(p._host_wait(ROOM, 3, sess, 0.0))
    assert outcome == "live" and waited <= p.HOST_WAIT_MAX_SEC
    policy.room_back_live()
    plan = policy.plan()
    assert (plan.mode, plan.delay) == ("lookup", 0)
    assert policy.take_lookup() is True


def test_room_rechecks_from_a_full_bucket_also_stop_one_short(monkeypatch, tmp_path):
    """满桶开始等、等了快 10 分钟才回到在播（审核意见里那个 8 次复查的剧本）：最后照样留得下一个令牌。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    from app.audit import AuditLog
    p.audit = AuditLog(room_url=ROOM, log_dir=tmp_path / "logs")
    sess = p._session_state = p._new_session_state(p.audit)
    clock = Clock()
    policy = sess["policy"] = V2Policy(False, clock=clock)
    policy.take_lookup()                             # 开场
    policy.round_ended(True, 600.0, mode="lookup")
    assert policy.plan(silent_sec=0).mode == "reuse"
    clock.t += 25
    policy.round_ended(False, 0.0, mode="reuse", dead=True)
    assert policy.plan(silent_sec=25).mode == "lookup"
    assert policy.take_lookup()                      # 这次查到的是「不是在播」
    start = clock.t
    probes = []

    async def probe(url):
        probes.append(clock.t)
        return (2 if clock.t - start >= 480 else 3), None   # 等了 8 分钟多才回到在播

    monkeypatch.setattr(p, "_probe_room_status", probe)
    _clock_sleep(monkeypatch, clock)
    outcome, waited = run(p._host_wait(ROOM, 3, sess, 0.0))
    assert outcome == "live" and len(probes) >= 5
    policy.room_back_live()
    plan = policy.plan(silent_sec=clock.t - 1000.0)
    assert (plan.mode, plan.delay) == ("lookup", 0)


def test_the_room_wait_says_when_it_checks_again_and_stops_within_ten_minutes(monkeypatch, tmp_path):
    """桶里令牌不够复查时：状态行照实说多久之后复查，不写「每 60 秒」；拉长之后落在 10 分钟之外就不再等，
    结束那句按实际等了多久写。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    sess, policy, clock = _drained(p, server, tmp_path)
    probes = []

    async def probe(url):
        probes.append(clock.t)
        return 3, None

    monkeypatch.setattr(p, "_probe_room_status", probe)
    _clock_sleep(monkeypatch, clock)
    outcome, waited = run(p._host_wait(ROOM, 3, sess, 0.0))
    assert outcome == "timeout" and waited <= p.HOST_WAIT_MAX_SEC
    assert [t - 1000.0 for t in probes] == [360.0, 540.0]
    waiting = [d for s, d in server.statuses if s == "connecting"]
    assert str(waiting[0]) == ("TikTok 接口返回房间状态 3（不是在播状态），为少向 TikTok 发请求，"
                               "6 分钟后再复查（已等 0 分钟）…")
    assert i18n.render(waiting[0], EN) == ("TikTok’s API returned stream status 3 (not live). To keep "
                                           "requests to TikTok low, the app checks again in 6 min "
                                           "(0 min so far)…")
    assert str(waiting[1]).endswith("3 分钟后再复查（已等 6 分钟）…")
    assert not any("每 60 秒" in str(d) for d in waiting)
    end = server.statuses[-1]
    assert end[0] == "ended" and str(end[1]).startswith("9 分钟内 TikTok 接口一直没有返回在播状态")
    assert sess["end"]["reason"] == "host_wait_timeout" and sess["end"]["waited_sec"] == 540
    for text in waiting:
        zh, en = str(text), i18n.render(text, EN)
        assert not rules.ZH_LABELS.search(zh) and not rules.ZH_CAUSAL.search(zh)
        assert not rules.EN_LABELS.search(en) and not rules.EN_CAUSAL.search(en)


def test_a_slow_bucket_still_checks_the_room_once_before_giving_up(monkeypatch, tmp_path):
    """连续无声半小时后令牌每 15 分钟才补 1 个：10 分钟里一次都复查不了。不能一次都没问就按「等房间恢复
    超时」停止监听——这次中断至少复查一次，状态行说多久之后。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    sess, policy, clock = _drained(p, server, tmp_path, refill=900.0)
    probes = []

    async def probe(url):
        probes.append(clock.t)
        return 3, None

    monkeypatch.setattr(p, "_probe_room_status", probe)
    _clock_sleep(monkeypatch, clock)
    outcome, waited = run(p._host_wait(ROOM, 3, sess, 0.0))
    assert outcome == "timeout" and len(probes) == 1
    first = [d for s, d in server.statuses if s == "connecting"][0]
    assert str(first).endswith("30 分钟后再复查（已等 0 分钟）…")


def test_the_recheck_after_a_clock_gap_says_how_long_it_really_waits(monkeypatch, tmp_path):
    """时钟跳变后的「已结束」复查也扣令牌：桶里不够时等到够了再问，「…秒后再查一次」照实写，问完还留一个
    给回到在播之后的查询。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    sess, policy, clock = _drained(p, server, tmp_path)
    sess["gap"] = {"from": T0, "to": T0 + 1390, "sec": 1380.0, "diverged": True}

    async def probe(url):
        return 2, None

    monkeypatch.setattr(p, "_probe_room_status", probe)
    _clock_sleep(monkeypatch, clock)
    status, waited, why = run(p._recheck_ended_after_gap(ROOM, 4, sess, 0.0))
    assert status == 2 and waited == pytest.approx(360.0)
    text = [d for s, d in server.statuses if s == "connecting"][0]
    assert str(text) == ("电脑刚从休眠或挂起中恢复，TikTok 接口返回房间状态 4（已结束）；"
                         "360 秒后再查一次再下结论…")
    assert policy.bucket.available() == 1


def test_v1_room_wait_lines_are_unchanged(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    from app.audit import AuditLog
    p.audit = AuditLog(room_url=ROOM, log_dir=tmp_path / "logs")
    sess = p._session_state = p._new_session_state(p.audit)
    sess["policy"] = V1Policy(False)

    async def probe(url):
        return 3, None

    monkeypatch.setattr(p, "_probe_room_status", probe)
    outcome, waited = run(p._host_wait(ROOM, 3, sess, 0.0))
    assert outcome == "timeout" and waited == 600
    waiting = [str(d) for s, d in server.statuses if s == "connecting"]
    assert len(waiting) == 10
    assert waiting[0] == ("TikTok 接口返回房间状态 3（不是在播状态），每 60 秒复查一次，最多 10 分钟"
                          "（已等 0 分钟）…")


# ---- V2 下照样收手：状态 4、时钟跳变后复查、等房间超时、自然结束放掉防睡眠 ------------------------
# 这些路径的 V1 用例在 tests/test_resilience_stream.py（钉在 V1 上）。V2 有声之后先用原地址连一轮、不问
# TikTok，所以剧本里有声那一轮后面要跟一轮没声音的，才会去查、才会查到「已结束」。

def _v2_session(monkeypatch, p, rounds, probe=None):
    """rounds 是每一轮的 (有没有直播音频, 秒数)，也可以是函数 (第几轮, sess) -> 结果。返回每轮的媒体地址。"""
    media_seen = []

    async def fake_session(media, *a, sess=None, **k):
        media_seen.append(media)
        n = len(media_seen)
        sess["break_no"] += 1
        return rounds(n, sess) if callable(rounds) else rounds[n - 1]

    async def reachable(*a, **k):
        return True, ""

    monkeypatch.setattr(resolver, "tiktok_reachable", reachable)
    if probe is not None:
        monkeypatch.setattr(resolver, "probe_room_status", probe)
    monkeypatch.setattr(p, "_stream_session", fake_session)
    return media_seen


def test_v2_a_room_that_ends_still_ends_the_session_and_releases_the_guard(monkeypatch, tmp_path):
    from app import power
    p, server = make_pipeline(monkeypatch, tmp_path)
    guard = FakeGuard()
    monkeypatch.setattr(power, "hold", lambda reason="": guard)
    calls = scripted_resolve(monkeypatch, ["http://cdn/a.flv",
                                           ResolveError("没开播", kind="offline", status=4)])
    _v2_session(monkeypatch, p, [LONG, F])

    async def scenario():
        await p.start_stream(ROOM)
        await p._stream_task

    run(scenario())
    assert guard.released == 1 and len(calls) == 2
    rows = audit_rows(tmp_path)
    assert of_type(rows, "session_start")[0]["reconnect_policy"] == "v2"
    assert [r["mode"] for r in of_type(rows, "reconnect_plan")] == ["reuse", "lookup"]
    end = of_type(rows, "session_end")[0]
    assert end["reason"] == "offline" and end["status"] == 4


def test_v2_status_4_without_a_gap_ends_without_asking_the_room_again(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def probe(url):
        raise AssertionError("不该再问房间接口")

    scripted_resolve(monkeypatch, ["http://cdn/a.flv",
                                   ResolveError("没开播", kind="offline", status=4)])
    _v2_session(monkeypatch, p, [LONG, F], probe=probe)
    run(p._run_stream_inner(ROOM))
    assert server.statuses[-1][0] == "ended"
    assert of_type(audit_rows(tmp_path), "session_end")[0]["reason"] == "offline"


def test_v2_an_ended_verdict_right_after_a_clock_gap_is_rechecked_once(monkeypatch, tmp_path):
    """09-14 的教训在 V2 下照样成立：这次中断里睡过，「已结束」先复查；复查说在播就马上查流地址。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    probes = []

    def rounds(n, sess):
        if n == 1:                  # 这一轮里电脑睡过（单调时钟停住了）
            sess["gap"] = {"from": T0, "to": T0 + 1390, "sec": 1380.0, "diverged": True}
        return LONG if n in (1, 3) else F

    async def probe(url):
        probes.append(url)
        return 2, ""

    scripted_resolve(monkeypatch, ["http://cdn/a.flv",
                                   ResolveError("没开播", kind="offline", status=4),
                                   "http://cdn/b.flv",
                                   ResolveError("没开播", kind="offline", status=4)])
    media = _v2_session(monkeypatch, p, rounds, probe=probe)
    run(p._run_stream_inner(ROOM))
    assert media == ["http://cdn/a.flv", "http://cdn/a.flv", "http://cdn/b.flv", "http://cdn/b.flv"]
    assert probes == [ROOM]
    rows = audit_rows(tmp_path)
    assert [(w["trigger"], w["status"], w["outcome"]) for w in of_type(rows, "host_wait")] == [
        ("clock_gap", 2, "live")]
    plans = [(r["mode"], r["delay_sec"]) for r in of_type(rows, "reconnect_plan")]
    assert plans[2] == ("lookup", 0)                 # 复查说在播：马上查，不退避、不用原地址
    assert server.statuses[-1][0] == "ended"
    assert of_type(rows, "session_end")[0]["reason"] == "offline"


def test_v2_an_ended_verdict_that_beats_the_first_stats_tick_after_wake_is_still_rechecked(
        monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def not_ticked_yet(interval=10):
        await asyncio.Event().wait()     # 醒来后统计循环的计时器还没到点，解析先回来了

    monkeypatch.setattr(p, "_stats_loop", not_ticked_yet)
    probes = []

    def rounds(n, sess):
        if n == 1:                       # 这一轮里合盖 23 分钟：墙钟走了，单调时钟没走
            wall, mono = sess["clock"]
            sess["clock"] = (wall - 1380.0, mono)
        return LONG if n in (1, 3) else F

    async def probe(url):
        probes.append(url)
        return 2, ""

    scripted_resolve(monkeypatch, ["http://cdn/a.flv",
                                   ResolveError("没开播", kind="offline", status=4),
                                   "http://cdn/b.flv",
                                   ResolveError("没开播", kind="offline", status=4)])
    media = _v2_session(monkeypatch, p, rounds, probe=probe)
    run(p._run_stream_inner(ROOM))
    assert probes == [ROOM] and media[-1] == "http://cdn/b.flv"
    rows = audit_rows(tmp_path)
    assert len(of_type(rows, "clock_gap")) == 1
    assert [(w["trigger"], w["outcome"]) for w in of_type(rows, "host_wait")] == [("clock_gap", "live")]
    assert of_type(rows, "session_end")[0]["reason"] == "offline"


def test_v2_waiting_for_the_room_ends_monitoring_within_ten_minutes(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    clock = Clock()
    monkeypatch.setattr(reconnect_policy, "make_policy",
                        lambda direct: V2Policy(direct, clock=clock))
    _clock_sleep(monkeypatch, clock)
    probes = []

    async def probe(url):
        probes.append(clock.t)
        return 3, "http=200"

    scripted_resolve(monkeypatch, ["http://cdn/a.flv",
                                   ResolveError("没开播", kind="offline", status=3)])
    _v2_session(monkeypatch, p, [LONG, F], probe=probe)
    run(p._run_stream_inner(ROOM))
    assert server.statuses[-1][0] == "ended" and "监听已停止" in str(server.statuses[-1][1])
    rows = audit_rows(tmp_path)
    end = of_type(rows, "session_end")[0]
    assert end["reason"] == "host_wait_timeout" and end["status"] == 3
    assert end["waited_sec"] <= p.HOST_WAIT_MAX_SEC
    assert probes and end["requests"]["room_rechecks"] == len(probes)
    assert of_type(rows, "host_wait")[-1]["outcome"] == "timeout"


# ---- 4003110 自动重试被程序自己的上限挡下 ------------------------------------------------------

@pytest.mark.parametrize("tokens,tried", [(1, 1), (2, 2)])
def test_a_capped_4003110_retry_says_how_often_it_really_tried(monkeypatch, tmp_path, capsys,
                                                                tokens, tried):
    """重连时桶里只剩一两个令牌：4003110 的自动重试中途停下。那句话的次数按实际试过的写，停下的原因是
    程序自己的上限，另说一句（只进终端），不算到 TikTok 头上。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    sess = p._new_session_state(None)
    policy = sess["policy"] = V2Policy(False, clock=Clock())
    for _ in range(6 - tokens):
        policy.take_lookup()
    calls = []

    async def refuse(url, cookies=None, cookies_browser="auto", trace=None):
        calls.append(url)
        raise ResolveError("TikTok 不给（代码 4003110）", kind="browser_only")

    monkeypatch.setattr(resolver, "resolve_stream_url", refuse)
    with pytest.raises(ResolveError) as err:
        run(p._resolve_media(ROOM, reconnect=3, sess=sess))
    assert len(calls) == tried
    zh = str(err.value)
    assert "已自动重试 {} 次".format(tried) in zh and "已自动重试 3 次" not in zh
    out = capsys.readouterr().out
    assert "程序自己的上限" in out and "试了 {} 次就停下".format(tried) in out


def test_a_full_bucket_4003110_message_is_unchanged(monkeypatch, tmp_path, capsys):
    p, server = make_pipeline(monkeypatch, tmp_path)
    sess = p._new_session_state(None)
    sess["policy"] = V2Policy(False, clock=Clock())

    async def refuse(url, cookies=None, cookies_browser="auto", trace=None):
        raise ResolveError("TikTok 不给（代码 4003110）", kind="browser_only")

    monkeypatch.setattr(resolver, "resolve_stream_url", refuse)
    with pytest.raises(ResolveError) as err:
        run(p._resolve_media(ROOM, sess=sess))
    assert "已自动重试 3 次" in str(err.value)
    assert "程序自己的上限" not in capsys.readouterr().out


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
