"""M1 之后查流地址的次数仍有上限（专家审查 M1、计划审查 Q5，2026-10-22）。

FLV 关掉 ffmpeg 的内部重连之后，CDN 每断一次，ffmpeg 就退出、程序自己重连，每次都是「一轮结束」。V2 的
「连续 3 轮都不到 30 秒就去查一次」（SHORT_ROUNDS_BEFORE_LOOKUP）因此会更常触发。计数规则不改：它防的正是
「连上时 CDN 先灌一段缓存就断」（10-18 每轮 5.7–21.5 秒），和 M1 之后的断流形状一样；查询次数由令牌桶
封顶（容量 6、每 3 分钟补 1 个）。这里照 Pipeline._run_session 重连循环的调用顺序，在假时钟上驱动真的
V2Policy，把上限钉住；再钉住粘贴的直连地址短断一次不会被判成拉不到。不联网、不起 ffmpeg。
"""
import time

import pytest

from app.pipeline_stream import StreamWatchMixin
from app.reconnect_policy import V1Policy, V2Policy


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def drive(policy, clock, round_sec, total_sec, got_audio=True):
    """每轮收 round_sec 秒音频后 CDN 断开（reason=eof），照 _run_session 的顺序问 policy：
    round_ended → plan →（等 delay）→ 用原地址，或扣令牌去查（查到了 lookup_ok，没令牌 lookup_capped
    再 plan）。返回每次真正去查的时刻。"""
    lookups = []
    mode = "lookup"
    end = clock.t + total_sec
    while clock.t < end:
        clock.t += round_sec
        policy.round_ended(got_audio, round_sec if got_audio else 0.0, mode=mode, dead=False)
        while True:
            plan = policy.plan(silent_sec=0.0 if got_audio else None)
            assert plan is not None, "直播间链接不该因为短断放弃"
            clock.t += plan.delay
            if plan.reuse:
                mode = plan.mode
                break
            if policy.take_lookup():                 # _resolve_media 里扣令牌
                lookups.append(clock.t)
                policy.lookup_ok()
                mode = "lookup"
                break
            policy.lookup_capped()                   # LookupCapped：下一次安排改用原地址
    return lookups


def test_a_drop_every_20s_for_10_minutes_stays_under_the_bucket():
    clock = Clock()
    policy = V2Policy(direct=False, clock=clock)
    lookups = drive(policy, clock, round_sec=18.0, total_sec=600.0)
    capacity, refill = V2Policy.LOOKUP_BUCKET
    elapsed = lookups[-1] - 1000.0
    assert len(lookups) <= capacity + elapsed / refill           # 令牌桶给的上限：6 + 3 = 9
    assert len(lookups) >= capacity                                # 短断确实会去查，不是测了个空
    assert policy.lookups == len(lookups)
    assert policy.summary()["capped_sec"] > 0                      # 到过上限：之后用原地址接着连


def test_an_hour_of_drops_never_exceeds_the_hourly_cap():
    clock = Clock()
    policy = V2Policy(direct=False, clock=clock)
    lookups = drive(policy, clock, round_sec=18.0, total_sec=3600.0)
    assert len(lookups) <= policy.bucket.per_hour_max()            # 26
    # 桶空了之后的稳态：每 3 分钟最多一次（「每小时最多约 20 次」）
    late = [t for t in lookups if t >= lookups[0] + 1800.0]
    assert len(late) <= 1800.0 / V2Policy.LOOKUP_BUCKET[1] + 1


@pytest.mark.parametrize("cls", [V2Policy, V1Policy])
def test_a_pasted_stream_url_survives_short_drops_with_audio(cls):
    """粘贴的直连地址：每轮都收到过音频、只是很快又断（M1 之后每次 CDN 断开都会这样），不会被判成拉不到。
    DIRECT_BUDGET（V1 的 budget）只数连续一帧都没有的轮次：之后连着两轮无声才停。"""
    clock = Clock()
    policy = cls(direct=True, clock=clock) if cls is V2Policy else cls(direct=True)
    for _ in range(50):
        if cls is V2Policy:
            policy.round_ended(True, 6.0, mode="lookup")
        else:
            policy.round_ended(True, 6.0)
        assert policy.plan() is not None
    for expected in (True, False):                      # 第一轮无声还重连，第二轮放弃
        if cls is V2Policy:
            policy.round_ended(False, 0.0, mode="lookup")
        else:
            policy.round_ended(False, 0.0)
        assert (policy.plan() is not None) is expected


def test_a_cdn_drop_after_audio_is_not_read_as_a_dead_url():
    """M1 之后 FLV 断开时 ffmpeg 自己退出，stderr 是「Stream ends prematurely」，不是 4xx；这一轮收到过音频，
    V2 不会当成「原地址明确拉不动」马上去查（pipeline_stream._round_facts）。"""
    class Source:
        tail_bytes = 0

    tail = ("[http @ 0x1] Stream ends prematurely at 306997, should be 18446744073709551615 | "
            "[in#0/flv @ 0x2] Error during demuxing: Input/output error")
    quick = {"t0": time.monotonic() - 1.0}
    assert StreamWatchMixin._round_facts(None, quick, True, Source(), tail)["dead"] is False
    # 对照：一帧都没有、起来 1 秒就退的那种照旧算拉不动（这条规则 M1 不动）
    assert StreamWatchMixin._round_facts(None, quick, False, Source(), tail)["dead"] is True
