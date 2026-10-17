"""识别证据音频：把「事后要回头听」的那几段直播音频留在本机 logs/asr-evidence/ 里。

为什么要留（2026-10-16 的设计复核）：
  * 没有音频，「这段到底识别对没对」永远没法回答。20:47 那次「ponen la oficina」是通顺的错句，
    风险标记抓不到（app/asr_risk.py），线上唯一的人工信号是中控点了「重译」——所以点重译的段也存；
  * 只存被标记的段，以后只能算「标出来的里有几成真有问题」，漏了多少算不出来，重调阈值也会带着
    选择偏差。所以每 50 段再抽 1 段没被标记的当对照（按 seq 取模，不用随机数：测试好钉，重放也能复现）。

存什么：
  * 被风险规则标记的段（不管字幕有没有上屏）、违禁词命中的段（含警示关闭时被抑制的）、中控在桌面上
    点了「重译」的段（手机点的不算）、对照段；
  * 每段一个 16 kHz 单声道 WAV（本段加前 2 秒、后 1 秒），外加一行 index.jsonl（识别原始输出、
    风险子对象、原因）。内容是直播的混音：主播、同场的人、连麦的人、背景音乐都在里面。
  * 只在本机，不上传、不送任何接口、不进导出。logs/ 在 .gitignore 里；控制端口只把 web/ 挂成
    静态目录，手机同看不用静态目录，所以也不会被局域网端出去。

怎么存，才不挡识别和检测：
  * 内存里只留最近 ring_sec 秒（默认 600 秒，约 19 MB）的降噪后 PCM：线上 212 次手动重译，
    点击时距那段音频结束 ≤ 600 秒的占 99%；
  * 拷 PCM 在事件循环里做（只是切一段 bytes），写文件和清理交给这一场自己的证据线程（单线程、先交的先做，
    见 EvidenceRecorder._submit），不用识别的线程池，也不用默认线程池；
  * 出错只计数、终端打一行中文 [警告]（一场只打一次），绝不抛给调用方；
  * 磁盘剩不到 1 GB 就不存，空间留给审计；
  * 保留上限三条（300 MB / 2000 个文件 / 14 天），到了任何一条就从最旧的整场目录删起，
    当前这一场永远不删；当前这一场自己就超了上限，就停止新存。

开关（每场开始时读一次）：settings.json 的 asr_evidence（默认 true），环境变量 TLT_ASR_EVIDENCE=0
强制关，总闸 TLT_ASR_SAFEGUARDS=off 也会关掉它。上限和缓冲长度也能在 settings.json 里改
（asr_evidence_max_mb / _max_files / _days / _ring_sec / _control_every），写错了提示一次、当没写。

这个文件不产出界面文字：终端行一律中文、以 [信息] / [警告] 开头；界面上「存储」那一行的文字在
app/diskspace.py。
"""
import asyncio
import json
import os
import shutil
import threading
import time
import wave
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import asr_risk
from .audit import _finite_json

DIR_NAME = "asr-evidence"
INDEX_NAME = "index.jsonl"
EVIDENCE_ENV = "TLT_ASR_EVIDENCE"
SAMPLE_RATE = 16000              # 和 app/audio.py 的 SAMPLE_RATE 同一个数（测试钉着）
BYTES_PER_SEC = SAMPLE_RATE * 2
CTX_BEFORE_SEC = 2.0             # 本段之前多存 2 秒：切在说话中间时前一句的尾巴要听得到
CTX_AFTER_SEC = 1.0
PRUNE_EVERY = 20                 # 每存这么多份清一次上限（开场也清一次）
MAP_KEEP = 200                   # 记住最近这么多段的位置和识别输出（手动重译要回头找）
DISK_LOW_BYTES = 1024 ** 3       # 同 Pipeline.DISK_LOW_BYTES：剩这么多就不存，空间让给审计
# 内存缓冲最长 30 分钟（约 58 MB）。再长也没用：段表只记最近 MAP_KEEP 段（约 25 分钟），更早的段
# 找不回来。不设上限的话，把秒写成毫秒（600000）这种笔误会让缓冲在长直播里涨到十几 GB——
# 换页就是 08-31 那种识别 38–55 秒一段，内存不够则读流的协程直接崩、整场不再收音频
RING_SEC_MAX = 1800
THREAD_NAME = "asr-evidence"     # 证据线程的名字前缀（ThreadPoolExecutor 的 thread_name_prefix）

DEFAULTS = {"on": True, "max_mb": 300, "max_files": 2000, "days": 14, "ring_sec": 600,
            "control_every": 50}
_INT_KEYS = (("max_mb", "asr_evidence_max_mb", 1, None),
             ("max_files", "asr_evidence_max_files", 1, None),
             ("days", "asr_evidence_days", 1, None),
             ("ring_sec", "asr_evidence_ring_sec", 1, RING_SEC_MAX),
             ("control_every", "asr_evidence_control_every", 0, None))
_OFF_WORDS = ("0", "off", "false", "no")
_said = set()


def _say_once(text):
    if text not in _said:
        _said.add(text)
        print(text)


def load_config(settings=None, environ=None):
    """这一场的证据设置。settings 默认读 settings.json，environ 默认 os.environ。不抛异常。"""
    env = os.environ if environ is None else environ
    if settings is None:
        try:
            from .settings import load_settings
            settings = load_settings()
        except Exception:
            settings = {}
    cfg = dict(DEFAULTS)
    raw_on = settings.get("asr_evidence")
    if isinstance(raw_on, bool):
        cfg["on"] = raw_on
    elif raw_on is not None:
        _say_once("[警告] settings.json 的 asr_evidence 的值 {!r} 不是 true / false，已忽略"
                  .format(raw_on))
    for key, name, low, high in _INT_KEYS:
        raw = settings.get(name)
        if raw is None:
            continue
        if (isinstance(raw, int) and not isinstance(raw, bool) and raw >= low
                and (high is None or raw <= high)):
            cfg[key] = raw
        elif high is None:
            _say_once("[警告] settings.json 的 {} 的值 {!r} 不是不小于 {} 的整数，已忽略"
                      .format(name, raw, low))
        else:
            _say_once("[警告] settings.json 的 {} 的值 {!r} 不是 {} 到 {} 之间的整数，已忽略"
                      .format(name, raw, low, high))
    if str(env.get(EVIDENCE_ENV, "")).strip().lower() in _OFF_WORDS:
        cfg["on"] = False
    if not asr_risk.master_switch_on(env):
        cfg["on"] = False
    return cfg


def evidence_dir(log_dir):
    """证据目录：审计日志所在目录下的 asr-evidence/。跟着审计走，不另起一个根。"""
    return Path(log_dir) / DIR_NAME


class FrameRing:
    """最近 max_bytes 字节的 PCM，按收到的顺序记绝对字节位置（这一轮从 0 数起）。
    存帧而不是存段：切段器会把整段没有人声的缓冲直接扔掉，存段的话前后文就缺了。"""

    def __init__(self, max_bytes):
        self.max_bytes = int(max_bytes)
        self.total = 0                   # 这一轮一共收到的字节数 = 最新一帧末尾的位置
        self._chunks = deque()           # (起始位置, bytes)
        self._held = 0

    def append(self, frame):
        self._chunks.append((self.total, frame))
        self.total += len(frame)
        self._held += len(frame)
        while self._held > self.max_bytes and len(self._chunks) > 1:
            _, old = self._chunks.popleft()
            self._held -= len(old)

    @property
    def start(self):
        return self._chunks[0][0] if self._chunks else self.total

    def slice(self, start, end):
        """[start, end) 这一段，越过两头的部分截掉；start 已经不在缓冲里返回 None。
        从新往旧找：要的几乎总是最近十几秒。"""
        start, end = max(0, int(start)), min(int(end), self.total)
        if start < self.start or end <= start:
            return None
        parts = []
        for pos, chunk in reversed(self._chunks):
            chunk_end = pos + len(chunk)
            if chunk_end <= start:
                break
            if pos >= end:
                continue
            parts.append(chunk[max(0, start - pos):min(len(chunk), end - pos)])
        parts.reverse()
        return b"".join(parts)


def _dir_stats(path):
    """(字节数, 文件数, 最新的修改时间)。不跟随符号链接。
    空目录没有文件可看，用目录自己的修改时间：以前记成 0，按天数永远不算过期、也碰不到上限，
    就一直留着，「存储」里还多出一行「0 段」。"""
    size = files = 0
    newest = 0.0
    try:
        for root, _dirs, names in os.walk(str(path)):
            for name in names:
                full = os.path.join(root, name)
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
                size += st.st_size
                files += 1
                newest = max(newest, st.st_mtime)
    except OSError:
        pass
    if files == 0:
        try:
            newest = os.lstat(str(path)).st_mtime
        except OSError:
            pass
    return size, files, newest


def session_dirs(base):
    """证据目录下一场一个的子目录（只认 session- 开头、不是符号链接的），按名字排就是按时间排。"""
    try:
        return sorted(d for d in Path(base).iterdir()
                      if d.name.startswith("session-") and d.is_dir() and not d.is_symlink())
    except OSError:
        return []


def prune(base, keep_name, max_bytes, max_files, days, now=None):
    """清证据目录：先删超过 days 天没动过的整场目录，再从最旧的整场删起，直到总量回到上限以内。
    keep_name 那一场（当前这场）永远不删。返回 (删掉的目录数, 释放的字节数, 剩下的总字节, 剩下的总文件数)。"""
    now = time.time() if now is None else now
    cutoff = now - days * 86400.0
    entries = []
    for d in session_dirs(base):
        size, files, newest = _dir_stats(d)
        entries.append([d, size, files, newest])
    removed = freed = 0

    def drop(entry):
        nonlocal removed, freed
        try:
            shutil.rmtree(str(entry[0]))
        except OSError:
            return False
        removed += 1
        freed += entry[1]
        return True

    kept = []
    for entry in entries:
        if entry[0].name != keep_name and entry[3] and entry[3] < cutoff and drop(entry):
            continue
        kept.append(entry)
    total_b = sum(e[1] for e in kept)
    total_f = sum(e[2] for e in kept)
    for entry in list(kept):
        if total_b <= max_bytes and total_f <= max_files:
            break
        if entry[0].name == keep_name:
            continue
        if drop(entry):
            total_b -= entry[1]
            total_f -= entry[2]
    return removed, freed, total_b, total_f


def prune_for(audit_path, cfg):
    """按上限清一次这份审计旁边的证据目录（在线程池里跑）。证据关着的场次开场时也调：关掉只是
    不再存，已经存下的混音照样过了天数、超了大小就删——文档和「存储」那一行都是这么写的，
    为了隐私关掉证据的人会当真。出错只打一行中文警告。"""
    audit_path = Path(audit_path)
    try:
        return prune(evidence_dir(audit_path.parent), audit_path.stem,
                     cfg["max_mb"] * 1024 * 1024, cfg["max_files"], cfg["days"])
    except Exception as exc:
        _say_once("[警告] 清理识别证据音频出错（{}），识别、检测和字幕不受影响"
                  .format(str(exc)[:160]))
        return None


def _free_bytes(path):
    """path 所在磁盘的剩余字节。path 还没建时往上找已经有的那一级：不为了查空间先把目录建出来。"""
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    return shutil.disk_usage(str(p)).free


def _tmp_of(path):
    return path.with_name(path.name + ".tmp")


def _ms(n_bytes):
    return int(round(n_bytes * 1000.0 / BYTES_PER_SEC))


def _write_wav(path, pcm):
    tmp = _tmp_of(path)
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    os.replace(str(tmp), str(path))


class EvidenceRecorder:
    """一场直播的证据。挂在这一场自己的会话状态上（Pipeline._new_session_state 那一份），
    晚到的旧任务写不进下一场的目录。环形缓冲一轮流一份，重连就是新的一轮。"""

    def __init__(self, audit_path, config=None, now=None):
        self.cfg = dict(DEFAULTS if config is None else config)
        audit_path = Path(audit_path)
        self.base = evidence_dir(audit_path.parent)
        self.dir = self.base / audit_path.stem
        self.audit_name = audit_path.name
        self.round = 0
        self.ring = None
        self._seg = OrderedDict()        # seq -> 这一段的位置和识别输出
        # seq -> 已经交出去写的 wav：{"wav": 文件名, "offset": 本段在 wav 里的起点, "after": 本段之后
        # 还有多少字节}（都是字节）。后来再补的索引行照抄这几个数，不写成 0 / null 冒充另一种切法
        self._saved = {}
        # 写盘和清理都在这一场自己的证据线程里（_submit：单线程，先交的先做）。两把锁，事件循环上只拿 _lock：
        #   _io_lock  写盘和清理不交错。先后顺序靠证据线程的队列，不靠这把锁：几个线程抢同一把锁，
        #             谁先拿到不一定是谁先交的。清理要 os.walk 每一场、rmtree 整场目录，可能要好几百
        #             毫秒，所以事件循环上**永远不拿**它——以前计数和写盘共用一把，开场清理那一下
        #             就能把读流、识别、报警广播一起卡住（实测卡 455 ms）；
        #   _lock     只护 counts / by_reason，拿着的时候不做任何 IO，事件循环上拿也只等几微秒
        self._io_lock = threading.Lock()
        self._lock = threading.Lock()
        self._pool = None                # 证据线程，第一次交活时才起（_submit），收尾时放掉（close）
        self._closed = False
        self._pending = set()
        self._own_bytes = 0
        self._own_files = 0
        self._since_prune = 0
        self._full = False
        self._warned = False
        self.counts = {"saved": 0, "index_only": 0, "bytes": 0, "failed": 0, "skipped_cap": 0,
                       "skipped_old": 0, "skipped_disk": 0, "pruned_dirs": 0}
        self.by_reason = {}

    # ---- 拉流这一侧：每帧都进环形缓冲 ------------------------------------------------------
    def start_round(self):
        self.round += 1
        self.ring = FrameRing(self.cfg["ring_sec"] * BYTES_PER_SEC)

    def feed(self, frame):
        if self.ring is not None:
            self.ring.append(frame)

    def cut_meta(self, meta, length):
        """切出一段时（这一帧已经进了缓冲）：在切段器的 last_cut 上补这一段在缓冲里的位置。"""
        if self.ring is None:
            return meta
        out = dict(meta or {})
        out["round"] = self.round
        out["pcm_end"] = self.ring.total
        out["pcm_len"] = int(length)
        return out

    # ---- 识别之后：记住这一段、决定存不存 --------------------------------------------------
    def remember(self, seq, meta, result, risk, hits, audio_end_ts):
        meta = meta if isinstance(meta, dict) else {}
        if "pcm_end" not in meta:
            return
        self._seg[seq] = {
            "round": meta.get("round"), "end": meta["pcm_end"],
            "start": meta["pcm_end"] - meta.get("pcm_len", 0),
            "audio_end_ts": audio_end_ts,
            "asr": risk.audit if risk is not None else None,
            "language": getattr(result, "language", ""),
            "relabeled_from": getattr(result, "relabeled_from", "") or "",
            "text": getattr(result, "text", ""), "raw_text": getattr(result, "raw_text", ""),
            "rejected": list(getattr(result, "rejected", None) or []),
            "windows": list(getattr(result, "windows", None) or []),
            "hits": [h.get("term") for h in hits or () if isinstance(h, dict)],
            "backend": meta.get("backend"), "model": meta.get("model")}
        while len(self._seg) > MAP_KEEP:
            self._seg.popitem(last=False)

    def reasons_for(self, seq, risk, hits):
        reasons = []
        if risk is not None and risk.evidence:
            reasons.append("flagged")
        if hits:
            reasons.append("banned_term")
        every = self.cfg.get("control_every") or 0
        if not reasons and every > 0 and seq % every == 0:
            reasons.append("control")
        return reasons

    def save(self, seq, reasons, loop):
        """存这一段（没有原因就不存）。PCM 在这里切好，写盘交给证据线程（_submit）。返回是否交出去了。

        这一段的 wav 已经交出去过（比如先被标记、后来又点了重译）：只补一行索引，用不着环形缓冲里的
        音频，所以重连换了一轮、这一场到了自己的上限也照补。手动重译是「通顺的错句」唯一的人工信号
        （设计 §6.2、§12.2），在有重连、标记多的场次上丢掉它，「被标记 / 没被标记的字幕被重译的比例」
        就偏了。"""
        if not reasons:
            return False
        info = self._seg.get(seq)
        if info is None:
            self._bump("skipped_old")
            return False
        saved = self._saved.get(seq)
        pcm = None
        if saved is None:
            ring = self.ring
            if ring is None or info["round"] != self.round:
                self._bump("skipped_old")
                return False
            if self._full:
                self._bump("skipped_cap")
                return False
            if info["start"] < ring.start:
                self._bump("skipped_old")
                return False
            lo = max(info["start"] - int(CTX_BEFORE_SEC * BYTES_PER_SEC), ring.start)
            hi = info["end"] + int(CTX_AFTER_SEC * BYTES_PER_SEC)
            pcm = ring.slice(lo, hi)
            if not pcm:
                self._bump("skipped_old")
                return False
            offset = info["start"] - lo
            saved = {"wav": "seq-{:06d}.wav".format(seq), "offset": offset,
                     "after": len(pcm) - offset - (info["end"] - info["start"])}
            self._saved[seq] = saved
        record = {
            "seq": seq, "reasons": list(reasons), "audio_end_ts": info["audio_end_ts"],
            "wav": saved["wav"], "round": info["round"],
            "seg_offset_ms": _ms(saved["offset"]),
            "seg_ms": _ms(info["end"] - info["start"]),
            "ctx_before_ms": _ms(saved["offset"]),
            "ctx_after_ms": _ms(saved["after"]),
            "asr": info["asr"], "language": info["language"],
            "relabeled_from": info["relabeled_from"], "text": info["text"],
            "raw_text": info["raw_text"], "rejected": info["rejected"],
            "windows": info["windows"], "hits": info["hits"],
            "backend": info["backend"], "model": info["model"], "audit": self.audit_name,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        if self._submit(loop, self._write, record, pcm, saved) is None:
            self._drop_saved(seq, saved)
            self._bump("skipped_old")
            return False
        return True

    def prune_soon(self, loop):
        """开场清一次上限（在证据线程里，不挡开播；排在这一场所有写盘前面）。"""
        self._submit(loop, self._prune_locked_safe)

    def _submit(self, loop, fn, *args):
        """交给这一场自己的证据线程，返回 asyncio 的 future（记进 _pending，收尾时等它）。

        单线程、先交的先做：wav 一定先于后来给它补的那行索引落盘，开场清理一定先于第一份写盘。以前交给
        默认线程池、靠 _io_lock 排队，可锁不保证先来先得：wav 还在等锁（后来因为磁盘不够跳过了），排在
        它后面交的重译索引先拿到锁，写出一行指向不存在的 wav（审查复现：线程池里有闲着的线程时，200 次
        里几十次）。不用识别的线程池，也不用默认线程池（别的功能也往里排）。线程第一次交活时才起；收尾
        （close）之后不再收，返回 None。"""
        if self._closed:
            return None
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=THREAD_NAME)
        fut = loop.run_in_executor(self._pool, fn, *args)
        self._pending.add(fut)
        fut.add_done_callback(self._pending.discard)
        return fut

    async def drain(self, timeout=None):
        """等在途的写盘和清理结束（收尾和测试用）。给了 timeout 就最多等这么多秒，返回还没完的份数。
        用 asyncio.wait 不用 wait_for(gather(...))：超时时后者会把线程池里还在排队、没开跑的写盘
        一起取消，等于丢证据；wait 超时什么都不取消，没完的照样写完，只是这里不再等。"""
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while self._pending:
            left = None if deadline is None else deadline - loop.time()
            if left is not None and left <= 0:
                break
            await asyncio.wait(list(self._pending), timeout=left)
        return len(self._pending)

    # ---- 线程池里：写盘与清理 --------------------------------------------------------------
    def _write(self, record, pcm, entry):
        with self._io_lock:
            tmp = None
            try:
                seq = record["seq"]
                if pcm is not None:
                    size = len(pcm) + 44
                    # 先查空间和上限、再建目录：查完不存时不留一个空的场次目录
                    if _free_bytes(self.dir) < DISK_LOW_BYTES:
                        self._bump("skipped_disk")
                        self._drop_saved(seq, entry)
                        return
                    if (self._own_bytes + size > self.cfg["max_mb"] * 1024 * 1024
                            or self._own_files + 1 > self.cfg["max_files"]):
                        self._full = True
                        self._bump("skipped_cap")
                        self._drop_saved(seq, entry)
                        return
                    self.dir.mkdir(parents=True, exist_ok=True)
                    path = self.dir / record["wav"]
                    tmp = _tmp_of(path)
                    _write_wav(path, pcm)
                    tmp = None
                    self._own_bytes += size
                    self._own_files += 1
                    self._bump("saved")
                    self._bump("bytes", size)
                else:
                    if (self._saved.get(seq) is not entry
                            or not (self.dir / entry["wav"]).exists()):
                        # 那份 wav 最后没写成（磁盘满、写失败、到了上限），或者后来不在了（被手动删掉）：
                        # 这一行照记（重译的信号不丢），但不指向一个不存在的文件；也忘掉它，之后再点重译
                        # 还能从缓冲里重新存。证据线程先交的先做，走到这里时那份 wav 的写盘一定已经有了
                        # 结局，查文件在不在只是兜底
                        self._drop_saved(seq, entry)
                        record = dict(record, wav=None, seg_offset_ms=None, ctx_before_ms=None,
                                      ctx_after_ms=None)
                    self._bump("index_only")
                line = (json.dumps(_finite_json(record), ensure_ascii=False, allow_nan=False)
                        + "\n").encode("utf-8")
                self.dir.mkdir(parents=True, exist_ok=True)
                index = self.dir / INDEX_NAME
                new_index = not index.exists()
                with open(str(index), "ab") as fh:
                    fh.write(line)
                self._own_bytes += len(line)
                self._bump("bytes", len(line))
                if new_index:
                    self._own_files += 1
                with self._lock:
                    for reason in record["reasons"]:
                        self.by_reason[reason] = self.by_reason.get(reason, 0) + 1
                self._since_prune += 1
                if self._since_prune >= PRUNE_EVERY:
                    self._prune_io()
            except Exception as exc:
                if tmp is not None:
                    # wav 没写成：从「已存」里拿掉，后面的重译会从缓冲里重新存，而不是补一行指向
                    # 不存在的文件；写了一半的 .tmp 也删掉——它不算进这一场的上限，留着就一直占地方
                    self._drop_saved(record["seq"], entry)
                    try:
                        os.unlink(str(tmp))
                    except OSError:
                        pass
                self._bump("failed")
                if not self._warned:
                    self._warned = True
                    print("[警告] 识别证据音频没存下来（{}），这一场后面再出错不再提示；"
                          "识别、检测和字幕不受影响".format(str(exc)[:160]))

    def _drop_saved(self, seq, entry):
        if self._saved.get(seq) is entry:
            self._saved.pop(seq, None)

    def _prune_io(self):
        """调用方必须已持有 _io_lock（线程池里）。"""
        self._since_prune = 0
        removed, _freed, _b, _f = prune(self.base, self.dir.name,
                                        self.cfg["max_mb"] * 1024 * 1024,
                                        self.cfg["max_files"], self.cfg["days"])
        self._bump("pruned_dirs", removed)

    def _prune_locked_safe(self):
        with self._io_lock:
            try:
                self._prune_io()
            except Exception as exc:
                self._bump("failed")
                if not self._warned:
                    self._warned = True
                    print("[警告] 清理识别证据音频出错（{}），识别、检测和字幕不受影响"
                          .format(str(exc)[:160]))

    def _bump(self, key, n=1):
        """计数加 n。事件循环和线程池两边都调：只拿 _lock，拿着的时候不做 IO。"""
        with self._lock:
            self.counts[key] += n

    def summary(self):
        """这一场的计数（只有数字）。pending 是此刻线程池里还没写完的份数：收尾时先等过一会儿
        （Pipeline._evidence_drain），还不是 0 就说明计数比目录里少这么几份，不是悄悄少算。"""
        with self._lock:
            out = {"on": True}
            out.update(self.counts)
            out["by_reason"] = dict(sorted(self.by_reason.items()))
        out["pending"] = len(self._pending)
        out.update({k: self.cfg[k] for k in ("max_mb", "max_files", "days", "ring_sec",
                                             "control_every")})
        return out

    def close(self):
        """收尾：放掉环形缓冲和段表（约 19 MB）。证据线程里已经排上的写盘照常写完，写完线程自己退出
        （shutdown(wait=False) 不取消排着的活，也不在事件循环上等）；之后不再收新的。"""
        self._closed = True
        self.ring = None
        self._seg.clear()
        if self._pool is not None:
            self._pool.shutdown(wait=False)
