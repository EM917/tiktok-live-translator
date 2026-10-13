"""弹幕留意规则层的耗时上限（规格 I3：单条 1000 字的弹幕判定 ≤20 ms）按机器快慢折算。

20 ms 是对用户那台 Mac（M 系列，弹幕留意真正跑的地方）定的。GitHub 的 CI 跑器同一段代码要慢
两到三倍（2026-10-13 实测：本机最坏 18.0 ms 的输入，ubuntu 跑器上 51.9 ms），照搬 20 ms 会把
「跑器慢」误判成「代码变慢」。所以先量一段和规则层同类的参考负载（正则 + 字符串处理），
跑得比本机慢多少倍，上限就放宽多少倍；比本机快的机器不收紧，仍按 20 ms。

参考负载不调用 app.comment_guard：规则层自己整体变慢时，上限不会跟着放宽。
"""
import re
import time

# 本机（用户的 Mac）上 _reference_ms() 的 7 次最快值，2026-10-13 量的
REF_MAC_MS = 4.7
# 参考负载和规则层的快慢比例不完全一致（规则层更吃正则回溯），明显更慢（1.5 倍以上）的机器多留两成余量；
# 本机自己的测量抖动（几个百分点）不触发这份余量
SLOW_MACHINE_SLACK = 1.2
SLOW_MACHINE = 1.5

_TEXT = ("hola eres una estafa www.tienda.com 5512345678 @luna_demo jajaja " * 20)[:1000]
_PATTERNS = [re.compile(p) for p in (
    r"[a-z0-9._%+\-]+@[a-z0-9\-]+(?:\.[a-z0-9\-]+)+",
    r"(?<!\d)\+?\d(?:[\s\-.()]{0,2}\d){7,13}(?!\d)",
    r"\b(?:estafa|fraude|ladron\w*|mentir\w*)\b",
    r"([a-z])\1{2,}",
    r"(?:www\.|https?://)?[a-z0-9\-]+\.(?:com|net|org|mx|shop)\b",
)]
_factor = None


def _reference_ms():
    t0 = time.perf_counter()
    for _ in range(40):
        for pat in _PATTERNS:
            pat.findall(_TEXT)
        " ".join("".join(c if c.isalnum() else " " for c in _TEXT.lower()).split())
    return (time.perf_counter() - t0) * 1000.0


def speed_factor():
    """这台机器比用户的 Mac 慢几倍（不小于 1）。整个测试进程只量一次。"""
    global _factor
    if _factor is None:
        ref = min(_reference_ms() for _ in range(7))
        _factor = max(1.0, ref / REF_MAC_MS)
    return _factor


def budget_ms(base=20.0):
    """本机按 base 毫秒；慢机器按慢的倍数放宽，明显慢的再多留 SLOW_MACHINE_SLACK。"""
    factor = speed_factor()
    return base * factor * (SLOW_MACHINE_SLACK if factor >= SLOW_MACHINE else 1.0)
