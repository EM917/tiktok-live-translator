"""启动时给上一次没写结尾的审计补一条 session_end（专家审查 B5，2026-10-21）。

为什么：本机 132 场审计里有 38 场既没有 session_end、也没有 window_closed（packaging-verify/v6），其中
29 场的最后一条停在 translation 上。退出路径各有各的漏法：关掉 Start.command 的终端窗口（SIGHUP）、
kill（SIGTERM）、窗口模式下按 Ctrl-C、⌘Q（2026-10-23 用真 pywebview 6.2.1 实测：NSApp terminate: 在
applicationShouldTerminate 同意之后直接 exit()，webview.start() 不返回，收尾一步都没走）。信号和 ⌘Q 现在
会走收尾（app/exit_signals.py、app/window_close.py），可崩溃、强杀、断电还是一条都留不下——下一次启动时
补一条，对哪种原因都管用。补的这一条只说「上一场没正常收尾」，不猜是哪一种原因。

规则：
  * 只看日志目录下的 session-*.jsonl。最后一条完整记录既不是 session_end 也不是 window_closed 的，在末尾
    追加一行 {"type": "session_end", "ended_at": <文件 mtime>, "reason": "unclean_exit", "recovered": true,
    "recovered_at": <补写的时刻>}。只追加，原有的字节一个不动。文件末尾不是换行（崩溃留下的半行）时先补
    一个换行，新记录不和半行粘成一行坏 JSON。空文件、一条完整记录都没有的不碰；
  * 只补 mtime 早于 QUIET_SEC（15 分钟）以前的文件：一场活着的直播，统计循环每 5 分钟至少写一条
    （stream_summary、asr_memory），不会这么旧。--no-open 和 --url 模式不做单实例检查，另一个实例可能正在
    写某一份——这一条加上调用方的端口探测（others_running：有本程序在跑就整个不补）两道保险；
  * 范围（D16，用户 2026-10-22 定）：只补这个功能上线之后产生的文件，之前的历史文件一个字节不动。
    「上线时刻」是这台电脑第一次跑到这一步的时刻，记在 settings.json 的 SINCE_KEY 里（recover_since）；
    mtime 不晚于它的文件不碰。记不下来（settings.json 写不进去）就这一次整个不补；
  * 两个进程同时补同一份：POSIX 上先 flock 排他锁，拿不到就跳过；拿到之后重读末尾再判断一次。
    补过的文件最后一条就是 session_end，再启动不会重复补。

这个模块不含中文字面量、自己不打印：结果（Report）交给调用方（main.py），由它用中文写终端
（CLAUDE.md 第九条：终端永远是中文）。
"""
import json
import math
import os
from datetime import datetime
from pathlib import Path

try:
    import fcntl
except ImportError:          # Windows：没有 flock，靠 15 分钟和端口探测两道保险
    fcntl = None

SINCE_KEY = "audit_recover_since"
QUIET_SEC = 900.0
PATTERN = "session-*.jsonl"
CLOSING_TYPES = ("session_end", "window_closed")
REASON = "unclean_exit"
TAIL_FIRST = 8 * 1024         # 先读末尾这么多：平常的一条记录远小于它
TAIL_BYTES = 256 * 1024       # 放大到这么多还没有一条完整记录，就整份读


class Report:
    """一次补写的结果。recovered 是补了结尾的文件名；其余是各种没补的个数（只有数字和文件名）。"""

    def __init__(self):
        self.scanned = 0
        self.recovered = []
        self.skipped_recent = 0      # mtime 在 QUIET_SEC 之内：可能还有人在写
        self.skipped_before = 0      # mtime 不晚于 since：D16 范围外的历史文件
        self.skipped_locked = 0      # 另一个进程正拿着锁
        self.errors = []             # (文件名, 异常类型名)
        self.blocked = False         # 端口上有本程序在跑：整个没补
        self.disabled = False        # since 没有（第一次记不下来）：整个没补

    def as_dict(self):
        return {"scanned": self.scanned, "recovered": list(self.recovered),
                "skipped_recent": self.skipped_recent, "skipped_before": self.skipped_before,
                "skipped_locked": self.skipped_locked, "errors": list(self.errors),
                "blocked": self.blocked, "disabled": self.disabled}


def _valid_since(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def recover_since(load, save, now):
    """D16 的范围起点：settings 里记着就用记着的；没有就把 now 记进去并返回 now（这一次因此什么都不补——
    比它早的都是历史文件）。记了读回来对不上（写不进去）返回 None，调用方这一次整个不补。
    load() 返回设置 dict，save(key, value) 写一项（app/settings 的 load_settings / save_setting）。"""
    try:
        value = (load() or {}).get(SINCE_KEY)
    except Exception:
        return None
    if _valid_since(value):
        return float(value)
    try:
        save(SINCE_KEY, now)
        value = (load() or {}).get(SINCE_KEY)
    except Exception:
        return None
    return float(value) if _valid_since(value) and value == now else None


def _last_record(path, size):
    """最后一条完整记录（dict）和文件末尾是不是换行。一条完整记录都没有时第一项是 None。
    先读末尾 TAIL_FIRST；里面一条能解析的都没有就放大 8 倍再读，到 TAIL_BYTES 还没有、文件又比它大时，整份
    从头读一遍。以前每份都直接读末尾 TAIL_BYTES（第 1 轮审查）：since 之后的每一份每次启动都要读，本机两个月
    132 份，一年下来启动时要同步读一两百 MB；现在平常一份只读几 KB。"""
    want = min(TAIL_FIRST, TAIL_BYTES)
    with open(path, "rb") as fh:
        while True:
            start = max(0, size - want)
            fh.seek(start)
            data = fh.read()
            rec = _parse_last(data, partial_head=bool(start))
            if rec is not None or start == 0:
                break
            want = size if want >= TAIL_BYTES else min(want * 8, TAIL_BYTES)
    ends_newline = data.endswith(b"\n") if data else True
    return rec, ends_newline


def _parse_last(data, partial_head=False):
    lines = data.split(b"\n")
    if partial_head and lines:
        lines = lines[1:]            # 从文件中间开始读的第一段可能只是一行的后半截
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue                 # 崩溃留下的半行：跳过，看它前面那一条
        if isinstance(rec, dict):
            return rec
    return None


def _iso(ts):
    try:
        return datetime.fromtimestamp(float(ts)).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _needs_end(path, size):
    rec, ends_newline = _last_record(path, size)
    if rec is None:
        return False, ends_newline
    return rec.get("type") not in CLOSING_TYPES, ends_newline


def _append_end(path, now):
    """拿到锁、重读确认还缺结尾之后追加。返回 "recovered" / "closed" / "locked"。"""
    with open(path, "ab") as fh:
        if fcntl is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return "locked"
        st = os.fstat(fh.fileno())
        needs, ends_newline = _needs_end(path, st.st_size)
        if not needs:
            return "closed"
        rec = {"type": "session_end", "ended_at": _iso(st.st_mtime), "reason": REASON,
               "recovered": True, "recovered_at": _iso(now)}
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        # 一次 write 写完：半行之后先补换行，新记录自成一行
        fh.write(((b"" if ends_newline else b"\n") + line.encode("utf-8")))
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass
    return "recovered"


def recover_unclosed(log_dir, since, now, quiet_sec=QUIET_SEC, others_running=None):
    """给 log_dir 下没写结尾的审计补一条 session_end。规则见模块说明。since 为 None 时什么都不做
    （Report.disabled）；others_running() 为真时什么都不做（Report.blocked）。任何一份出错只记进
    Report.errors，不抛，接着看下一份。"""
    report = Report()
    if since is None:
        report.disabled = True
        return report
    if others_running is not None:
        try:
            if others_running():
                report.blocked = True
                return report
        except Exception as exc:
            report.errors.append(("others_running", type(exc).__name__))
            report.blocked = True
            return report
    try:
        paths = sorted(Path(log_dir).glob(PATTERN))
    except OSError as exc:
        report.errors.append((str(log_dir), type(exc).__name__))
        return report
    for path in paths:
        report.scanned += 1
        try:
            st = path.stat()
            if st.st_mtime <= since:
                report.skipped_before += 1
                continue
            if now - st.st_mtime < quiet_sec:
                report.skipped_recent += 1
                continue
            needs, _ = _needs_end(path, st.st_size)
            if not needs:
                continue
            outcome = _append_end(path, now)
        except (OSError, ValueError) as exc:
            report.errors.append((path.name, type(exc).__name__))
            continue
        if outcome == "recovered":
            report.recovered.append(path.name)
        elif outcome == "locked":
            report.skipped_locked += 1
    return report


def recover_at_startup(log_dir, load, save, now, others_running=None, quiet_sec=QUIET_SEC):
    """main.py 启动时调的那一步：先定范围起点（recover_since），再补写。"""
    return recover_unclosed(log_dir, recover_since(load, save, now), now,
                            quiet_sec=quiet_sec, others_running=others_running)
