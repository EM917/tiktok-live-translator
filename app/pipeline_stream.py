# i18n: done
"""拉流稳定性的 Pipeline 侧接线：阶段计时、「已 N 分钟没收到直播音频」、跨轮覆盖率、场末汇总。

计数和时刻在 app/stream_watch.py（一场一份，挂在 sess["watch"] 上）；这里只做三件事：
  * 在重连循环的各个等待前后标阶段（同步的一次赋值）；
  * 统计循环每 10 秒调一次 _stream_watch_tick：阶段超了预算写一条 stage_overrun（收尾超时再自愈一次），
    多久没收到直播音频超过 NO_AUDIO_NOTICE_SEC 就挂持续提示；
  * session_end / stop 代写 session_end 时带上这一场的汇总。

规矩：
  * 只认这一场自己的会话状态（sess is self._session_state，且审计是这一场的）：晚到的旧任务碰不到新一场；
  * 每一项检查各自 try/except——统计循环遇到异常会整个退出，而它是唯一能发现「卡住」的地方；
  * 文字只写观察和能做的事，不写为什么（CLAUDE.md 第八条、第九条）。终端、审计是中文（G8）。
"""
import asyncio
import contextlib
import re
import time

from .audio import FFmpegAudioSource as _Source
from .i18n import LN, L
from .stream_watch import StreamWatch


# ffmpeg 报 HTTP 4xx 的说法（stderr 原话）：Server returned 403 Forbidden / HTTP error 404 Not Found
_HTTP_4XX = re.compile(r"(?:Server returned|HTTP error) 4\d\d")


class StreamWatchMixin:
    # ---- 阶段预算（秒）。超了只写审计、只报一次；收尾（stopping）超了另外自愈一次 ----
    STAGE_OTHER_SEC = 60.0          # 兜底阶段：重连循环里没有具名的那些瞬间（发状态、写审计……）
    STAGE_SPAWN_SEC = 10.0          # 起 ffmpeg：正常几毫秒
    STAGE_FIRST_FRAME_SLACK_SEC = 10.0   # 起好到第一帧：20 秒看门狗必须先到，+10
    STAGE_STOP_SLACK_SEC = 3.0      # 收尾：FFmpegAudioSource 各步上限之和 +3
    STAGE_DRAIN_SLACK_SEC = 10.0    # 流结束到识别把队列做完：识别看门狗 60 秒 + 10 + 断流时的积压 + 等翻译
    STAGE_BACKOFF_SLACK_SEC = 10.0  # 退避：本次退避 + 10
    STAGE_WAIT_SLACK_SEC = 60.0     # 等网络、等房间恢复、时钟跳变后的复查：自身上限 + 60
    STAGE_MODEL_LOAD_SEC = 1800.0   # 加载识别模型：首次要下载几 GB
    STAGE_DENOISE_SEC = 60.0        # 降噪模型：下载 15 秒 + 两次 ffmpeg 探测各 15 秒
    # 多久没收到直播音频就在页面顶部说（9-17 以来除 10-18 外，断流后重新有声的间隔最长 80 秒；
    # 10-18 有 13 次 ≥ 120 秒，外加最后那 91 分钟）
    NO_AUDIO_NOTICE_SEC = 120.0
    NO_AUDIO_INCIDENT = "session:no_audio"
    # 跨轮覆盖率：最近 5 分钟（开场不足 5 分钟按已过的时间）收到多少直播音频。低于 70% 连续两次（20 秒）
    # 报，回到 85% 以上撤。analysis/coverage.py 回放：9-17 以来 27 场有断流的场次，除 10-18 外最低 0.88
    # （一次正常断流约 31 秒，5 分钟里还剩九成），10-18 开播 2 分钟后起全程低于 50%
    COVERAGE_WINDOW_SEC = 300.0
    COVERAGE_MIN_SEC = 120.0
    COVERAGE_WARN = 0.70
    COVERAGE_CLEAR = 0.85
    COVERAGE_LOW_CHECKS = 2
    # 沿用以前每轮「音频偏少」的 id：同一件事界面上只有一条（那条改由这里驱动）
    COVERAGE_INCIDENT = "session:audio_rate"
    # 每隔这么久往审计写一条 stream_summary（场末汇总的快照）：⌘Q、关窗、Ctrl-C 都不写 session_end，
    # 场末没写也留得下数。不挂在 asr_memory 上——那条只有 mlx 后端才写
    STREAM_SUMMARY_EVERY_SEC = 300.0

    def _new_stream_watch(self):
        return StreamWatch(self.STAGE_OTHER_SEC)

    @staticmethod
    def _watch_of(sess):
        return sess.get("watch") if isinstance(sess, dict) else None

    def _stage(self, sess, name, budget, **extra):
        """这一场进入阶段 name（同步、一次赋值）。没有观察状态（测试里的半成品）就什么都不做。"""
        watch = self._watch_of(sess)
        if watch is not None:
            watch.stage.enter(name, budget, break_no=sess.get("break_no", 0), **extra)

    def _stage_during(self, sess, name, budget, **extra):
        watch = self._watch_of(sess)
        if watch is None:
            return contextlib.nullcontext()
        return watch.stage.during(name, budget, break_no=sess.get("break_no", 0), **extra)

    def _lookup_stage_budget(self):
        """一次解析的预算：各层超时之和（resolver.lookup_worst_case_sec），不是历史最长的那次。"""
        from .resolver import lookup_browser_count, lookup_worst_case_sec

        browsers = lookup_browser_count(getattr(self.args, "cookies_browser", "auto"))
        return lookup_worst_case_sec(browsers) + self.STAGE_BACKOFF_SLACK_SEC

    @staticmethod
    def _stop_limits():
        """收尾各步上限，记进 session_start：事后读日志知道这一场按哪套上限收尾。"""
        S = _Source
        return {"term": S.TERM_GRACE, "kill": S.KILL_GRACE, "close": S.CLOSE_GRACE,
                "urgent": [S.URGENT_TERM_GRACE, S.URGENT_KILL_GRACE, S.URGENT_CLOSE_GRACE],
                "stderr": S.STDERR_GRACE, "stream_limit": S.STREAM_LIMIT}

    def _stop_stage_budget(self):
        S = _Source
        return (S.TERM_GRACE + S.KILL_GRACE + 2 * S.CLOSE_GRACE + S.STDERR_GRACE
                + self.STAGE_STOP_SLACK_SEC)

    def _drain_stage_budget(self, backlog_sec):
        return (self.ASR_STALL_SEC + self.STAGE_DRAIN_SLACK_SEC + max(0.0, float(backlog_sec or 0))
                + self.TRANSLATION_DRAIN_SEC)

    def _direct_probe_budget(self):
        from .resolver import MEDIA_PROBE_SEC

        delays = getattr(self, "DIRECT_RECHECK_DELAYS", ())
        return (sum(delays) + (len(delays) + 1) * MEDIA_PROBE_SEC
                + self.STAGE_BACKOFF_SLACK_SEC)

    def _offline_check_budget(self, sess=None):
        """「没在播」之后：时钟跳变复查（等 OFFLINE_RECHECK_SEC 再问一次）+ 等房间恢复（最多
        HOST_WAIT_MAX_SEC，每 HOST_WAIT_POLL_SEC 问一次，每次一个 HTTP 请求）。V2 复查也扣令牌，
        桶空了要等到下一个令牌：再加两个补充间隔。"""
        from .resolver import HTTP_TIMEOUT_SEC

        polls = int(self.HOST_WAIT_MAX_SEC // max(1.0, self.HOST_WAIT_POLL_SEC)) + 3
        bucket = getattr((sess or {}).get("policy"), "bucket", None)
        token_wait = 2 * float(getattr(bucket, "refill_sec", 0.0) or 0.0)
        return (2 * self.OFFLINE_RECHECK_SEC + self.HOST_WAIT_MAX_SEC + self.HOST_WAIT_POLL_SEC
                + polls * HTTP_TIMEOUT_SEC + token_wait + self.STAGE_WAIT_SLACK_SEC)

    async def _network_wait_staged(self, sess):
        """_wait_for_network 外面套一个 network_wait 阶段：放弃之前最多等 NETWORK_GIVE_UP_SEC。"""
        budget = (self.NETWORK_GIVE_UP_SEC + self.NETWORK_RETRY_SEC
                  + (self.NETWORK_GIVE_UP_SEC / max(1.0, self.NETWORK_RETRY_SEC) + 2) * 4.0
                  + self.STAGE_WAIT_SLACK_SEC)
        with self._stage_during(sess, "network_wait", budget):
            return await self._wait_for_network(sess)

    def _source_stage_hook(self, sess, source):
        """交给 FFmpegAudioSource.on_stage：起好进程进 first_frame，开始收尾进 stopping。"""
        def hook(event):
            if event == "spawned":
                watch = self._watch_of(sess)
                if watch is not None:
                    watch.note_spawn()
                self._stage(sess, "first_frame",
                            getattr(source, "STALL_SEC", 20.0) + self.STAGE_FIRST_FRAME_SLACK_SEC)
            elif event == "stopping":
                self._stage(sess, "stopping", self._stop_stage_budget())
        return hook

    def _shift_session_clocks(self, sess, sec):
        """时钟对账发现单调时钟多走了 sec 秒（Windows 睡眠、事件循环被卡住）：这一场所有按单调时钟
        计的起点往后挪，醒来时阶段计时、无声计时不会一起误报。"""
        if sec <= 0:
            return
        watch = self._watch_of(sess)
        if watch is not None:
            watch.shift(sec)
        policy = sess.get("policy") if isinstance(sess, dict) else None
        if policy is not None:
            policy.shift(sec)          # 令牌桶：程序没在运行的那段不补令牌

    # ---- 重连（app/reconnect_policy.py）：请求计数、令牌、每次重连的安排和文字 ----

    @staticmethod
    def _requests(sess):
        """这一场对 TikTok / CDN 发了多少请求（第八条：对同一房间的请求要省，合起来算）。"""
        if not isinstance(sess, dict):
            return None
        req = sess.get("requests")
        if req is None:
            req = sess["requests"] = {"lookups": 0, "lookup_layers": {}, "room_rechecks": 0,
                                      "cdn_connects": 0}
        return req

    def _count_lookup(self, sess, layers):
        """一次 resolve_stream_url：按层记走过了哪几层（每层至少一个请求，见 resolver 的文档）。"""
        req = self._requests(sess)
        if req is None:
            return
        req["lookups"] += 1
        for rec in layers or ():
            layer = str(rec.get("layer"))
            if rec.get("outcome") in ("skipped",):
                continue
            req["lookup_layers"][layer] = req["lookup_layers"].get(layer, 0) + 1

    @staticmethod
    def _lookup_token_left(policy):
        """4003110 自动重试之前：V2 的桶里还有令牌才值得等那 20 秒；V1 不限。"""
        bucket = getattr(policy, "bucket", None)
        return bucket is None or bucket.available() >= 1

    async def _room_token(self, sess):
        """等房间恢复时复查一次房间状态之前：V2 扣一个令牌，桶空了就等到下一个令牌（返回等了几秒，
        算进 HOST_WAIT_MAX_SEC 的计时）。V1 立刻返回 0。"""
        policy = sess.get("policy") if isinstance(sess, dict) else None
        waited = 0.0
        wait = policy.room_token_wait() if policy is not None else 0.0
        while wait > 0:
            print("[信息] 查询次数到了上限，{:.0f} 秒后再复查房间状态".format(wait))
            await asyncio.sleep(wait)
            waited += wait
            wait = policy.room_token_wait()
        req = self._requests(sess)
        if req is not None:
            req["room_rechecks"] += 1
        return waited

    def _round_facts(self, rnd, got_audio, source, stderr_tail):
        """一轮结束时的事实，给 V2 判断「原地址明确拉不动」：ffmpeg 报 HTTP 4xx，或起来没几秒就退出、
        一帧都没有（收尾吐出的也没有）。"""
        from .reconnect_policy import V2Policy

        t0 = rnd.get("t0") if isinstance(rnd, dict) else None
        sec = time.monotonic() - t0 if t0 is not None else None
        tail = int(getattr(source, "tail_bytes", 0) or 0)
        quick = (not got_audio and not tail and sec is not None
                 and sec < V2Policy.DEAD_ROUND_SEC)
        http4xx = bool(_HTTP_4XX.search(stderr_tail or ""))
        return {"dead": bool(quick or (http4xx and not got_audio)), "sec": sec}

    def _note_reconnect_plan(self, sess, policy, plan):
        """每次重连写一条 reconnect_plan；到了上限的那一段第一次另写一条 lookup_capped。"""
        audit = sess.get("audit")
        if audit is None:
            return
        bucket = getattr(policy, "bucket", None)
        tokens = bucket.available() if bucket is not None else None
        audit.reconnect_plan(break_no=sess.get("break_no", 0), delay_sec=plan.delay,
                             mode=plan.mode, silent_streak=getattr(policy, "silent", None),
                             lookup_tokens=tokens, policy=policy.name)
        if plan.first_capped and bucket is not None:
            audit.lookup_capped(tokens=tokens, refill_sec=bucket.refill_sec,
                                next_in_sec=plan.next_lookup_sec,
                                per_hour=bucket.per_hour_max())
            print("[信息] 查流地址的次数到了上限（每小时最多 {} 次），{:.0f} 秒后再查；先用原来的流地址".format(
                bucket.per_hour_max(), plan.next_lookup_sec or 0))

    def _reconnect_text(self, sess, policy, plan):
        """重连时状态行那一句。V1 一字不动（「第 N/M 次」）；V2 换成本场累计第几次中断——新规则下
        「第 N/M 次」不再对应「还剩几次就放弃」。到了上限时说的是程序自己的规矩，不是 TikTok 那边的事
        （第八条：不能让人读成「TikTok 限了我们」）。"""
        if policy.name == "v1":
            return L("直播流中断，{} 秒后自动重连（第 {}/{} 次）…",
                     "The stream was interrupted. Reconnecting in {} sec (attempt {} of {})…"
                     ).format(plan.delay, plan.attempt, plan.attempts)
        k = sess.get("break_no", 0)
        if plan.mode == "lookup":
            if not plan.delay:
                return L("直播流中断，正在重新查流地址（本场第 {} 次中断）…",
                         "The stream was interrupted. Looking up the stream URL again "
                         "(interruption {} this session)…").format(k)
            return L("直播流中断，{} 秒后自动重连（本场第 {} 次中断）…",
                     "The stream was interrupted. Reconnecting in {} sec (interruption {} this "
                     "session)…").format(plan.delay, k)
        if plan.mode == "reuse":
            return L("直播流中断，{} 秒后用同一个流地址重连（本场第 {} 次中断）…",
                     "The stream was interrupted. Reconnecting to the same stream URL in {} sec "
                     "(interruption {} this session)…").format(plan.delay, k)
        per_hour = policy.bucket.per_hour_max()
        wait = self._duration_text(plan.next_lookup_sec or 0)
        if plan.mode == "reuse_failed_capped":
            return L("同一个流地址拉不到数据。为少向 TikTok 发请求，程序每小时最多查 {} 次流地址，"
                     "{}后再查；程序会一直重试。",
                     "The same stream URL isn’t returning data. To keep requests to TikTok low, "
                     "the app looks up the stream URL at most {} times an hour. Next lookup in {}. "
                     "The app keeps retrying.").format(per_hour, wait)
        return L("直播流中断，{} 秒后用同一个流地址重连（本场第 {} 次中断）。为少向 TikTok 发请求，"
                 "程序每小时最多查 {} 次流地址，{}后再查。",
                 "The stream was interrupted. Reconnecting to the same stream URL in {} sec "
                 "(interruption {} this session). To keep requests to TikTok low, the app looks up "
                 "the stream URL at most {} times an hour. Next lookup in {}.").format(
            plan.delay, k, per_hour, wait)

    # ---- 统计循环每一跳 ----

    def _watched_session(self):
        sess = getattr(self, "_session_state", None)
        audit = getattr(self, "audit", None)
        if sess is None or audit is None or sess.get("audit") is not audit:
            return None
        return sess if self._watch_of(sess) is not None else None

    async def _stream_watch_tick(self, now=None):
        """now=(单调, 墙钟) 只给测试用。每一项检查各自兜住异常，统计循环照常跳。"""
        sess = self._watched_session()
        if sess is None:
            return
        mono, wall = now if now is not None else (time.monotonic(), time.time())
        try:
            await self._check_stage_overrun(sess, mono)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("[警告] 拉流阶段计时检查出错：{}".format(exc))
        try:
            await self._check_no_audio(sess, mono, wall)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("[警告] 无声提示检查出错：{}".format(exc))
        try:
            await self._check_coverage(sess, mono)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("[警告] 覆盖率检查出错：{}".format(exc))
        try:
            self._maybe_stream_summary(sess, mono)
        except Exception as exc:
            print("[警告] 写拉流汇总出错：{}".format(exc))

    async def _check_stage_overrun(self, sess, mono):
        watch = self._watch_of(sess)
        over = watch.stage.overrun(mono)
        if over is None:
            return
        rnd = sess.get("round") or {}
        source = rnd.get("source") if rnd.get("no") == over.get("break_no", 0) + 1 else None
        facts = {}
        if source is not None and over["stage"] in ("spawn", "first_frame", "stopping"):
            facts = self._source_facts(source)
        if over["stage"] == "stopping":
            facts["self_heal"] = self._heal_stopping(sess, over)
        audit = sess.get("audit")
        if audit is not None:
            audit.stage_overrun(**dict(facts, **over))
        print("[警告] 拉流阶段 {} 已 {:.0f} 秒（预算 {:.0f} 秒）{}".format(
            over["stage"], over["sec"], over["budget_sec"],
            "，已自愈：{}".format(facts["self_heal"]) if facts.get("self_heal") else ""))

    @staticmethod
    def _source_facts(source):
        """卡住时音频源的现场：进程号、退出码、stdout 管道还读不读、几根管道还开着。只读，不改。"""
        out = {}
        proc = getattr(source, "proc", None)
        if proc is not None:
            out["pid"] = getattr(proc, "pid", None)
            out["returncode"] = getattr(proc, "returncode", None)
        transport = getattr(source, "_transport", None)
        if transport is not None:
            try:
                pipe = transport.get_pipe_transport(1)
                if pipe is not None:
                    out["stdout_paused"] = not pipe.is_reading()
                out["pipes_open"] = sum(
                    1 for fd in (1, 2)
                    if transport.get_pipe_transport(fd) is not None
                    and not transport.get_pipe_transport(fd).is_closing())
            except Exception:
                pass
        return out

    def _heal_stopping(self, sess, over):
        """收尾超了预算：关掉 ffmpeg 的 transport（可重复调，和收尾任务自己的 close 不冲突），再只取消
        这一轮的 reader 任务。不取消整个直播任务——那和用户点停止分不开，还会把识别任务一起取消，
        队列里的音频就没做违禁词检测。gather 用了 return_exceptions：识别会把队列做完，stream_break 照写。
        动手前核对：还是当前这一场、还是超时的那一轮。"""
        rnd = sess.get("round") or {}
        if sess is not getattr(self, "_session_state", None) \
                or rnd.get("no") != over.get("break_no", 0) + 1:
            return {"done": False, "why": "round_changed"}
        source, reader = rnd.get("source"), rnd.get("reader")
        closed = bool(source is not None and getattr(source, "force_close", None)
                      and source.force_close())
        cancelled = False
        if reader is not None and not reader.done():
            reader.cancel()
            cancelled = True
        return {"done": True, "transport_closed": closed, "reader_cancelled": cancelled}

    def _no_audio_text(self, wall_sec):
        return L("已 {}没收到直播音频，这段时间没有经过检测。程序还在自动重试，"
                 "可以在 TikTok 上看一眼直播是否还在进行。",
                 "No stream audio for {}. Nothing in that time was checked. The app keeps "
                 "retrying. You can check on TikTok whether the stream is still live.").format(
            self._duration_text(wall_sec))

    def _session_incidents(self):
        return (getattr(self.server, "config", {}) or {}).get("incidents") or {}

    async def _check_no_audio(self, sess, mono, wall):
        """「已 N 分钟没收到直播音频」：和阶段无关、面向中控的那一句。从本场第一次起 ffmpeg 起算；
        「本机连不上 TikTok」挂着时不出（那一条已经说了多久没监听）。触发看单调时钟，显示的时长看墙钟。"""
        watch = self._watch_of(sess)
        incidents = self._session_incidents()
        key = self.NO_AUDIO_INCIDENT
        silent = watch.silent_for(mono)
        want = (silent is not None and silent >= self.NO_AUDIO_NOTICE_SEC
                and "session:network" not in incidents)
        if not want:
            if key in incidents and "session:network" in incidents:
                await self._incident(key, "clear")
            return
        wall_sec = watch.silent_wall(wall)
        if not sess.get("no_audio"):
            sess["no_audio"] = True
            watch.no_audio_notices += 1
            audit = sess.get("audit")
            if audit is not None:
                audit.no_audio(sec=silent, wall_sec=wall_sec, stage=watch.stage.name,
                               break_no=sess.get("break_no", 0))
        text = self._no_audio_text(wall_sec)
        if (incidents.get(key) or {}).get("text") != text:
            await self._incident(key, "warn", text)

    def _coverage_text(self, pct, window_sec, breaks):
        return LN(breaks,
                  "近 {}只收到 {}% 的直播音频，缺的部分没有经过检测。本场已中断 {} 次。",
                  "Only {1}% of stream audio arrived in the last {0}. The missing audio wasn’t "
                  "checked. 1 interruption this session.",
                  "Only {1}% of stream audio arrived in the last {0}. The missing audio wasn’t "
                  "checked. {2} interruptions this session.").format(
            self._duration_text(window_sec), pct, breaks)

    async def _check_coverage(self, sess, mono):
        """跨轮覆盖率：低于 COVERAGE_WARN 连续 COVERAGE_LOW_CHECKS 次才报，回到 COVERAGE_CLEAR 以上撤，
        中间留回差免得来回闪。跨阈值时写一条 coverage。同一件事界面上只出一条：「本机连不上 TikTok」或
        「已 N 分钟没收到直播音频」挂着时不出这一条（那两条已经说了更要紧的事）。"""
        watch = self._watch_of(sess)
        cov = watch.coverage(mono, self.COVERAGE_WINDOW_SEC, self.COVERAGE_MIN_SEC)
        if cov is not None:
            ratio, span, audio, breaks = cov
            watch.cov_ratio = ratio
            watch.cov_below = watch.cov_below + 1 if ratio < self.COVERAGE_WARN else 0
            changed = None
            if not watch.cov_low and watch.cov_below >= self.COVERAGE_LOW_CHECKS:
                watch.cov_low, changed = True, "low"
            elif watch.cov_low and ratio >= self.COVERAGE_CLEAR:
                watch.cov_low, changed = False, "ok"
            audit = sess.get("audit")
            if changed and audit is not None:
                audit.coverage(state=changed, ratio=ratio, window_sec=span, audio_sec=audio,
                               breaks_in_window=breaks)
        incidents = self._session_incidents()
        key = self.COVERAGE_INCIDENT
        louder = "session:network" in incidents or self.NO_AUDIO_INCIDENT in incidents
        if watch.cov_low and not louder and cov is not None:
            text = self._coverage_text(int(round(ratio * 100)), span, sess.get("break_no", 0))
            if (incidents.get(key) or {}).get("text") != text:
                await self._incident(key, "warn", text)
        elif key in incidents and (louder or not watch.cov_low):
            await self._incident(key, "clear")

    def _coverage_stats(self):
        """随 stats 推给页面的数字（底部大字幕元信息行的角标用；文字在 web/live-ui.js 里成对写）。
        不是当前这一场、或还没开始评的时候是空的。"""
        sess = self._watched_session()
        if sess is None:
            return {}
        watch = self._watch_of(sess)
        if watch.cov_ratio is None:
            return {"breaks": sess.get("break_no", 0)}
        span = watch.coverage(None, self.COVERAGE_WINDOW_SEC, self.COVERAGE_MIN_SEC)
        window = span[1] if span else self.COVERAGE_WINDOW_SEC
        return {"coverage_pct": int(round(watch.cov_ratio * 100)),
                "coverage_window_min": max(1, int(round(window / 60.0))),
                "coverage_low": bool(watch.cov_low),
                "breaks": sess.get("break_no", 0)}

    def _maybe_stream_summary(self, sess, mono):
        last = sess.get("summary_at")
        if last is None:
            sess["summary_at"] = mono
            return
        if mono - last < self.STREAM_SUMMARY_EVERY_SEC:
            return
        sess["summary_at"] = mono
        audit = sess.get("audit")
        if audit is None:
            return
        fields = self._stream_end_fields(sess)
        comment_source = getattr(self, "comment_source", None)
        connects = getattr(comment_source, "_connect_times", None)
        if isinstance(connects, list):
            # 弹幕那条线最近一小时连了几次（每次一次匿名抓页 + 一次签名握手）：对 TikTok 来说这和查流地址
            # 都是同一个房间的请求，合起来看（第八条）
            fields["comment_connects_hour"] = len(connects)
        audit.stream_summary(**fields)

    def _no_audio_resumed(self, sess):
        """这一轮收到第一帧直播音频：撤掉无声提示。返回这次恢复之前提示是不是挂着。"""
        if not sess.get("no_audio"):
            return False
        sess["no_audio"] = None
        if sess is getattr(self, "_session_state", None):
            self._spawn(self._incident(self.NO_AUDIO_INCIDENT, "clear"))
        return True

    # ---- 场末汇总 ----

    def _stream_end_fields(self, sess):
        """session_end 里这一场拉流的汇总（只有数字）。算不出来就不带。"""
        watch = self._watch_of(sess)
        if watch is None:
            return {}
        try:
            out = watch.summary(sess.get("break_no", 0))
            policy = sess.get("policy")
            if policy is not None:
                out.update(policy.summary())
            req = sess.get("requests")
            if req:
                out["requests"] = {"lookups": req["lookups"],
                                   "lookup_layers": dict(req["lookup_layers"]),
                                   "room_rechecks": req["room_rechecks"],
                                   "cdn_connects": req["cdn_connects"]}
            return out
        except Exception:
            return {}

    def _session_end_fields(self, sess, end, release=True):
        """session_end 的附带字段：结束原因的观察 + 识别风险与证据计数 + 拉流汇总。核心字段优先。
        release=False：只读不放（_stop_locked 替还没收尾的旧任务代写时用，证据缓冲留给旧任务自己放）。"""
        end = self._asr_session_end(sess, end, release=release)
        end = dict(end)
        for key, value in self._stream_end_fields(sess).items():
            end.setdefault(key, value)
        return end
