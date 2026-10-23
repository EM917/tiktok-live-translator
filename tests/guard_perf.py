"""弹幕留意规则层的耗时上限（规格 I3：单条 1000 字的弹幕判定 ≤20 ms）。

20 ms 是对用户那台 Mac（M 系列，弹幕留意真正跑的地方）定的，本机（没有 CI 环境变量）照这个数判。

GitHub 的 CI 跑器同一段代码要慢一到三倍，而且慢的倍数随代码路径变：2026-10-13 实测，本机最坏
18.0 ms 的输入，在三种跑器上是 28.5 / 37.2 / 57.7 ms；拿一段和规则层无关的正则 + 字符串参考负载
去折算，只量出 1.2–1.4 倍，判不准。所以 CI 上改用规则层自己判一条普通的 1000 字弹幕作参考：
它和对抗输入走同一套词表匹配（detector 的逐词比对占大头），跑器慢多少倍，上限就放宽多少倍，
再留三成余量。代价是规则层整体一起变慢时 CI 上看不出来——本机的 20 ms 仍然照判，那时会红。

上限只和「最快的那次」比（fastest_ms）。本机负载高的时候（几个会话同时跑测试、开着 Chrome 和 ffmpeg），
同一段代码单次判定会慢到 25–45 ms，而且慢的几次是成段出现的，固定量几次不够。2026-10-22 在 M3 Pro
上实测（另加 10 个空转进程，load 10–46）：load 10 时，低负载下最快 16 ms 的输入连量 7 次最快仍是
27.7 ms；load 26–32 时，同一条连量 3 秒（47、49 次）也没碰到一次低于 20 ms。同时量的线程 CPU 时间和
墙钟一起涨（线程一直在跑，只是跑得慢），换成 thread_time 也躲不开，还会漏掉 sleep 这类阻塞。
所以按时间量：一批输入轮流量，量到每条都有一次低于上限，最多量 PATIENCE_S 秒。别的进程只会让
一次判定变慢、不会让它变快，所以有一次低于上限就说明代码本身够快；代码真变慢了（每一次都超），
量多久都过不了，量满之后报最快的那次。
"""
import os
import time

BASE_MS = 20.0
# 本机（用户的 Mac）上 reference_ms(夹具词表的守卫) 的 7 次最快值，2026-10-13 量的（三次 9.73–9.93 ms）
REF_MAC_MS = 9.8
CI_SLACK = 1.3
# fastest_ms 一批输入最多量几秒（见文件头）。机器不忙时一轮就够；量满只发生在真超上限、或机器长时间
# 满载的时候。2026-10-22 load 43–46 时，37 条对抗输入整批量到每条都低于 20 ms，16 次里用了 0.6–4.7 秒
PATIENCE_S = 20.0
_REF_TEXT = ("hola amiga cuanto cuesta la faja talla m color rosa envio gratis gracias " * 14)[:1000]
_factor = None


def on_ci():
    return bool(os.environ.get("CI"))


def reference_ms(make_guard):
    """规则层判一条普通 1000 字弹幕的 7 次最快值（毫秒）。make_guard() 每次给一个新守卫。"""
    best = None
    for i in range(7):
        g = make_guard()
        t0 = time.perf_counter()
        g.judge(_REF_TEXT, user="ref%d" % i, ts=1000.0 + i)
        ms = (time.perf_counter() - t0) * 1000.0
        best = ms if best is None else min(best, ms)
    return best


def budget_ms(make_guard):
    """本机：20 ms。CI：20 ms × 跑器比本机慢的倍数（不小于 1）× CI_SLACK。整个测试进程只量一次。"""
    global _factor
    if not on_ci():
        return BASE_MS
    if _factor is None:
        _factor = max(1.0, reference_ms(make_guard) / REF_MAC_MS)
    return BASE_MS * _factor * CI_SLACK


def fastest_ms(runs, budget, patience_s=None, clock=time.perf_counter):
    """runs：{输入名: run_ms}，run_ms(序号) 量一次、返回毫秒。返回 {输入名: (最快的毫秒数, 量了几次)}。
    patience_s 不给就用调用时的 PATIENCE_S。

    一轮一轮地量：每轮把还没有一次低于 budget 的输入各量一次，全都低于了就停，否则量到 patience_s 秒
    用完（至少量一轮）。轮流量，同一条输入的几次重试就在时间上隔开了（慢的几次是成段出现的）；时限
    是这一批输入共用的，代码真变慢时整批只多花 patience_s 秒，不是每条各花一份。
    同一条输入每次拿到的序号都不同（0、1、2……），调用方拿它造不重复的弹幕 id。
    判定和「每条都量满再取最小值比 budget」一样：提前停只是省掉了改不了结论的那些次。"""
    deadline = clock() + (PATIENCE_S if patience_s is None else patience_s)
    best, tries = {}, dict.fromkeys(runs, 0)
    pending = list(runs)
    while pending:
        for name in pending:
            ms = runs[name](tries[name])
            tries[name] += 1
            best[name] = min(best.get(name, ms), ms)
        pending = [name for name in pending if best[name] >= budget]
        if clock() >= deadline:
            break
    return {name: (best[name], tries[name]) for name in runs}
