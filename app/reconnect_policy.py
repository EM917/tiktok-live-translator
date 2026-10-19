"""断流之后怎么重连：退避多久、什么时候放弃。只有数字和决定，不碰界面、不发请求、不 await。

Pipeline._run_session 的重连循环每轮问它一次；界面文字、审计、解析请求都还在 pipeline 里。
拆出来是为了能单独测：pipeline.py 五千多行，_run_session 两百多行，规则写在里面就没法单独验。

V1Policy 是 2026-10-19 之前写在 _run_session 里的规则，原样搬过来（纯搬移，逐行比对见
asr-bench/live-stability/impl/check_move.py）：
  * 退避 min(30, 2 ** 连续重连次数)，只有一轮收满 30 秒音频、或房间回到在播时才清零；
  * 每次重连都重新解析流地址；
  * 连续「一帧音频都没有」的轮次（重连时解析失败也算一轮）超过预算就放弃：房间地址 5 次，
    粘贴的流地址 1 次。
V2Policy 是 2026-10-19 新加的规则（RECONNECT_V2，默认开；TLT_RECONNECT_V2=0 退回 V1），见类的文档。
"""
import os
import time


class V1Policy:
    name = "v1"

    def __init__(self, direct):
        # 直连 .flv/.m3u8 地址无法重新解析出「是否还在播」：播过一阵后结束
        # 就按正常收尾处理，重试预算也压到 1 次，别对着过期地址空耗。
        self.direct = direct
        self.budget = 1 if direct else 5
        self.reconnects = 0        # 连续重连次数：只决定退避间隔（2、4、…、30 秒），不上界面
        self.silent = 0            # 连续「一帧音频都没有」的轮次：决定何时放弃
        self.first_silent = None   # 这一串无声里第一次提示重连时的 silent：界面「第 N/M 次」从它数起
        self.host_waited = 0.0     # 这次中断里等房间恢复在播已经等了多久；收到音频才清零

    def round_ended(self, got_audio, audio_secs):
        """一轮拉流结束（直连地址播满 30 秒后的探活由调用方先做，探不到就不会走到这里）。"""
        if got_audio:
            self.host_waited = 0.0
        if audio_secs >= 30:
            self.reconnects = 0        # 刚才播得好好的：退避从 2 秒重新算
        # 拿到过音频的轮次不算失败：网络劣化时每轮只播二十几秒就断，
        # 五轮之后主播还在播，监听却宣布「重连失败」放弃了。只有连续几轮
        # 一帧都没有（地址过期、真下播）才放弃；退避间隔照常增长，不空转
        self.silent = 0 if got_audio else self.silent + 1
        if got_audio:
            self.first_silent = None

    def next_attempt(self):
        """下一次重连之前调。返回 None：预算用尽，放弃；否则 (退避秒数, 第 N 次, 共 M 次)。"""
        self.reconnects += 1
        if self.silent > self.budget:
            return None
        delay = min(30, 2 ** self.reconnects)
        # 「第 N/M 次」按放弃规则数，不按退避计数：reconnects 只在播满 30 秒或房间
        # 回到在播时清零，每轮只播二十几秒时照它显示会出现「第 6/5 次」。无声轮次每多一轮 silent 加 1，
        # silent 到 budget 的那一次是最后一次（再无声就放弃），所以从这一串无声的
        # 第一条提示数起：N 不超过 M，最后一条正好是 M/M。播过一阵才断时 M 是
        # budget+1，首连就没声音时首连已算掉一轮，M 是 budget。房间回到在播时
        # silent 不变，N 也不变——那一次没耗预算
        if self.first_silent is None:
            self.first_silent = self.silent
        attempt, attempts = self.silent - self.first_silent + 1, self.budget - self.first_silent + 1
        return delay, attempt, attempts

    def exhausted(self):
        """放弃时写进 session_end 的结束原因和观察。"""
        return {"reason": "reconnect_exhausted",
                "silent": self.silent, "budget": self.budget}

    def room_back_live(self):
        self.reconnects = 0    # 房间回到在播：马上重新解析，不再长退避

    def lookup_failed(self):
        self.silent += 1       # 解析不出地址也是一轮没有音频

    # ---- 和 V2Policy 同一套接口（Pipeline 的重连循环两种规则共用）。V1 的规则本身不变 ----

    def plan(self, silent_sec=None):
        """next_attempt 包成 Plan：每次都重新解析（mode=lookup），界面照旧显示「第 N/M 次」。"""
        got = self.next_attempt()
        if got is None:
            return None
        delay, attempt, attempts = got
        return Plan(delay, "lookup", attempt=attempt, attempts=attempts)

    def take_lookup(self):
        return True                # V1 不限查询次数

    def lookup_ok(self):
        pass

    def room_token_wait(self):
        return 0.0                 # V1 复查房间状态不扣令牌

    def shift(self, sec):
        pass

    def summary(self):
        return {}

    def session_start(self):
        return {"reconnect_policy": self.name}


# ---- V2（2026-10-19，改进清单第 3 条；设计 asr-bench/live-stability/design.md §2.3）----

# 默认开。应急退回 V1 不改代码：环境变量 TLT_RECONNECT_V2=0（和 TLT_COMMENT_GUARD=off 一个路数）。
# 测试要测哪一边就显式 monkeypatch 这一项（CLAUDE.md 第九条那次的教训：别靠默认值变绿）
RECONNECT_V2 = True
REUSE_MODES = ("reuse", "reuse_capped", "reuse_failed_capped")


def v2_enabled(env=None):
    env = os.environ if env is None else env
    flag = str(env.get("TLT_RECONNECT_V2", "")).strip().lower()
    if flag in ("0", "off", "false", "no"):
        return False
    if flag in ("1", "on", "true", "yes"):
        return True
    return bool(RECONNECT_V2)


def make_policy(direct, clock=time.monotonic):
    """这一场用哪套重连规则。每场开始时建一个：手动点「开始翻译」是新的一场，查询的令牌桶重新满。"""
    return V2Policy(direct, clock=clock) if v2_enabled() else V1Policy(direct)


def session_start_fields():
    """session_start 里记这一场按哪套规则跑（事后读日志知道）。"""
    if v2_enabled():
        return {"reconnect_policy": V2Policy.name, "lookup_bucket": list(V2Policy.LOOKUP_BUCKET)}
    return {"reconnect_policy": V1Policy.name}


class LookupCapped(Exception):
    """想查流地址，但这一场查询的次数到了上限（令牌桶空了）。不是 ResolveError：不是 TikTok 那边的结果。"""


class Plan:
    """一次重连怎么做。mode：
      lookup               重新查流地址（向 TikTok 发请求）；
      reuse                用原来的流地址（只连 CDN，不碰直播页和房间接口）；
      reuse_capped         想查，但查询次数到了上限：先用原来的流地址；
      reuse_failed_capped  原来的流地址也拉不到数据、查询又到了上限：照样重试原地址，等下一个令牌。
    attempt / attempts 只有 V1 有（界面的「第 N/M 次」）；next_lookup_sec 是到了上限时多久后能再查；
    first_capped 是这一段「到了上限」的第一次（审计每段只写这一次 lookup_capped）。"""
    __slots__ = ("delay", "mode", "attempt", "attempts", "next_lookup_sec", "first_capped")

    def __init__(self, delay, mode="lookup", attempt=None, attempts=None, next_lookup_sec=None,
                 first_capped=False):
        self.delay = delay
        self.mode = mode
        self.attempt = attempt
        self.attempts = attempts
        self.next_lookup_sec = next_lookup_sec
        self.first_capped = first_capped

    @property
    def reuse(self):
        return self.mode in REUSE_MODES

    def __repr__(self):
        return "Plan({!r}, {!r})".format(self.delay, self.mode)


class TokenBucket:
    """查流地址的上限：容量 capacity 个令牌，每 refill_sec 秒补 1 个，按单调时钟补；时钟对账发现程序
    其实没在运行的那段（shift）不补。任意 60 分钟里最多 capacity + 3600 / refill_sec 次。"""

    def __init__(self, capacity, refill_sec, clock=time.monotonic):
        self.capacity = int(capacity)
        self.refill_sec = float(refill_sec)
        self._clock = clock
        self.tokens = float(capacity)
        self._t = clock()

    def _fill(self, now):
        if now > self._t:
            self.tokens = min(float(self.capacity), self.tokens + (now - self._t) / self.refill_sec)
            self._t = now

    def available(self, now=None):
        self._fill(self._clock() if now is None else now)
        return int(self.tokens + 1e-9)

    def take(self, now=None):
        self._fill(self._clock() if now is None else now)
        if self.tokens + 1e-9 >= 1.0:
            self.tokens -= 1.0
            return True
        return False

    def next_in(self, now=None):
        """多久之后会有 1 个令牌（秒）；现在就有是 0。"""
        self._fill(self._clock() if now is None else now)
        return max(0.0, (1.0 - self.tokens) * self.refill_sec)

    def set_refill(self, refill_sec, now=None):
        self._fill(self._clock() if now is None else now)
        self.refill_sec = float(refill_sec)

    def shift(self, sec):
        if sec > 0:
            self._t += sec

    def per_hour_max(self):
        """任意 60 分钟里最多查几次（满桶 + 一小时补的），界面上那句「每小时最多查 N 次」用它。"""
        return int(self.capacity + 3600 // self.refill_sec)


class V2Policy:
    """V2 的规则（RECONNECT_V2）：

      1. 收到过任何直播音频就清零退避；连续无声时 2、4、8、16、30 秒封顶。收尾时 ffmpeg 吐出的音频不算
         （调用方传进来的 got_audio / audio_secs 已经不含它）。
      2. 收到过音频之后的第一次重连直接用原来的流地址，不查；这一轮又是一帧都没有，下一次才查。原地址明确
         拉不动（ffmpeg 报 HTTP 4xx、或启动后 DEAD_ROUND_SEC 秒内就退出且一帧没有）时马上去查——只有这一串
         无声里的第一次免退避，之后照常退避：查回来的多半还是同一个地址，不免的话几秒内就把令牌扣光。
         防「假有声」：连续 SHORT_ROUNDS_BEFORE_LOOKUP 轮都只有不到 SHORT_AUDIO_SEC 秒（像是连上时 CDN
         先灌的一段缓存，10-18 每轮 5.7–21.5 秒），下一次照样去查。
      3. 无声轮次不计入放弃：查询说在播（拿得到地址）就一直重连。放弃只剩：查询说已结束（状态 4，含
         时钟跳变后的复查）、等房间恢复超时、本机长时间连不上 TikTok（这三条在 pipeline 里）、查询连续
         失败超过 LOOKUP_FAIL_BUDGET 次（不是「没在播」的错误）。粘贴的流地址照旧：拉不到 1 次就停。
      4. 查流地址的上限：令牌桶 LOOKUP_BUCKET（容量 6、每 3 分钟补 1 个）。一次 resolve_stream_url
         扣一个（4003110 自动重试每试一次扣一个；开场最多 3 次，容量放得下）；等房间恢复时每次复查
         房间状态也扣一个，桶空了就等到下一个令牌再查。用户自带的流地址拉 2KB 探活不扣。
      5. 到了上限不停止监听：用原来的流地址重连（只连 CDN），界面说清多久后再查。
      6. 连续无声 SILENT_SLOW_AFTER_SEC 之后：退避放到 SILENT_SLOW_DELAY，令牌改成每 SLOW_REFILL_SEC
         补 1 个——开一夜不会查出一两百次。
    """
    name = "v2"
    LOOKUP_BUCKET = (6, 180.0)
    SILENT_SLOW_AFTER_SEC = 1800.0
    SILENT_SLOW_DELAY = 60
    SLOW_REFILL_SEC = 900.0
    DELAY_CAP = 30
    LOOKUP_FAIL_BUDGET = 5
    DIRECT_BUDGET = 1
    SHORT_AUDIO_SEC = 30.0
    SHORT_ROUNDS_BEFORE_LOOKUP = 3
    DEAD_ROUND_SEC = 5.0

    def __init__(self, direct, clock=time.monotonic):
        self.direct = direct
        self._clock = clock
        capacity, refill = self.LOOKUP_BUCKET
        self.bucket = TokenBucket(capacity, refill, clock=clock)
        self.silent = 0            # 连续「一帧直播音频都没有」的轮次
        self.short = 0             # 连续「有声，但不到 SHORT_AUDIO_SEC 秒」的轮次
        self.failures = 0          # 连续查询失败（不是「没在播」的错误）
        self.fast_used = False     # 这一串无声里「原地址拉不动、马上查」已经用过
        self.want_lookup = False   # 上一次想查没查成（失败、到了上限、房间刚回到在播）：接着查
        self.immediate = False     # 房间刚回到在播：下一次马上查，不退避
        self.last_got = True       # 第一轮之前按「刚有过声音」算：首次断流先用原地址
        self.last_mode = "lookup"
        self.last_dead = False
        self.slow = False
        self.host_waited = 0.0
        self.lookups = 0           # 本场查流地址的次数（每次 resolve_stream_url，含开场）
        self.reused = 0            # 用原地址重连的次数
        self.capped_since = None
        self.capped_sec = 0.0

    # ---- 一轮结束、一次查询的结果 ----

    def round_ended(self, got_audio, audio_secs, mode=None, dead=False):
        if got_audio:
            self.host_waited = 0.0
            self.silent = 0
            self.failures = 0
            self.fast_used = False
            self.short = self.short + 1 if audio_secs < self.SHORT_AUDIO_SEC else 0
        else:
            self.silent += 1
        self.last_got = bool(got_audio)
        if mode:
            self.last_mode = mode
        self.last_dead = bool(dead) and not got_audio

    def take_lookup(self):
        """查之前扣一个令牌；直连地址的「查询」是原样放行、不发请求，不扣。"""
        if self.direct:
            return True
        if self.bucket.take():
            self.lookups += 1
            return True
        return False

    def lookup_ok(self):
        self.failures = 0
        self.want_lookup = False
        self.last_mode = "lookup"
        self.short = 0

    def lookup_failed(self):
        self.failures += 1
        self.want_lookup = True

    def lookup_capped(self):
        self.want_lookup = True

    def room_back_live(self):
        self.want_lookup = True
        self.immediate = True

    def exhausted(self):
        if self.direct:
            return {"reason": "reconnect_exhausted", "silent": self.silent,
                    "budget": self.DIRECT_BUDGET, "policy": self.name}
        return {"reason": "reconnect_exhausted", "lookup_failures": self.failures,
                "budget": self.LOOKUP_FAIL_BUDGET, "policy": self.name}

    # ---- 下一次怎么连 ----

    def _delay(self):
        if self.slow:
            return self.SILENT_SLOW_DELAY
        n = max(self.silent, self.failures)
        return min(self.DELAY_CAP, 2 ** max(1, n))

    def _update_slow(self, silent_sec, now):
        slow = silent_sec is not None and silent_sec >= self.SILENT_SLOW_AFTER_SEC
        if slow != self.slow:
            self.slow = slow
            self.bucket.set_refill(self.SLOW_REFILL_SEC if slow else self.LOOKUP_BUCKET[1], now)

    def _wants_lookup(self):
        if self.want_lookup or not self.last_got:
            return True
        return self.short >= self.SHORT_ROUNDS_BEFORE_LOOKUP

    def _capped(self, now, on):
        """进入 / 离开「到了上限」的一段。返回 True：这是新的一段。"""
        if on:
            if self.capped_since is None:
                self.capped_since = now
                return True
            return False
        if self.capped_since is not None:
            self.capped_sec += now - self.capped_since
            self.capped_since = None
        return False

    def plan(self, silent_sec=None):
        """下一次重连之前调。返回 None：放弃；否则 Plan。silent_sec 是这一场多久没收到直播音频（单调时钟）。"""
        now = self._clock()
        self._update_slow(silent_sec, now)
        if self.direct:
            if self.silent > self.DIRECT_BUDGET:
                return None
            return Plan(self._delay(), "lookup")
        if self.failures > self.LOOKUP_FAIL_BUDGET:
            return None
        delay = self._delay()
        if self.immediate:
            self.immediate = False
            delay = 0
        lookup = self._wants_lookup()
        if (lookup and self.last_dead and not self.fast_used and not self.want_lookup
                and self.last_mode in REUSE_MODES):
            self.fast_used = True     # 原地址明确拉不动：这一串无声里只有这一次免退避
            delay = 0
        if not lookup:
            self._capped(now, False)
            self.reused += 1
            return Plan(delay, "reuse")
        if self.bucket.available(now) >= 1:
            self._capped(now, False)
            return Plan(delay, "lookup")
        first = self._capped(now, True)
        mode = ("reuse_failed_capped" if not self.last_got and self.last_mode in REUSE_MODES
                else "reuse_capped")
        self.reused += 1
        return Plan(delay or self._delay(), mode, next_lookup_sec=self.bucket.next_in(now),
                    first_capped=first)

    def room_token_wait(self):
        """等房间恢复时复查一次房间状态之前：有令牌就扣掉、返回 0；没有就返回还要等几秒。"""
        if self.bucket.take():
            return 0.0
        return max(1.0, self.bucket.next_in())

    def shift(self, sec):
        self.bucket.shift(sec)
        if self.capped_since is not None and sec > 0:
            self.capped_since += sec

    def summary(self):
        now = self._clock()
        capped = self.capped_sec + (now - self.capped_since if self.capped_since is not None else 0.0)
        return {"lookups": self.lookups, "reused": self.reused, "capped_sec": round(capped, 1)}

    def session_start(self):
        return {"reconnect_policy": self.name,
                "lookup_bucket": [self.bucket.capacity, self.LOOKUP_BUCKET[1]]}
