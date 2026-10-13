"""弹幕留意规则层的耗时上限（规格 I3：单条 1000 字的弹幕判定 ≤20 ms）。

20 ms 是对用户那台 Mac（M 系列，弹幕留意真正跑的地方）定的，本机（没有 CI 环境变量）照这个数判。

GitHub 的 CI 跑器同一段代码要慢一到三倍，而且慢的倍数随代码路径变：2026-10-13 实测，本机最坏
18.0 ms 的输入，在三种跑器上是 28.5 / 37.2 / 57.7 ms；拿一段和规则层无关的正则 + 字符串参考负载
去折算，只量出 1.2–1.4 倍，判不准。所以 CI 上改用规则层自己判一条普通的 1000 字弹幕作参考：
它和对抗输入走同一套词表匹配（detector 的逐词比对占大头），跑器慢多少倍，上限就放宽多少倍，
再留三成余量。代价是规则层整体一起变慢时 CI 上看不出来——本机的 20 ms 仍然照判，那时会红。
"""
import os
import time

BASE_MS = 20.0
# 本机（用户的 Mac）上 reference_ms(夹具词表的守卫) 的 7 次最快值，2026-10-13 量的（三次 9.73–9.93 ms）
REF_MAC_MS = 9.8
CI_SLACK = 1.3
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
