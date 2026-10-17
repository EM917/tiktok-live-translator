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
  * 拷 PCM 在事件循环里做（只是切一段 bytes），写文件放进默认线程池，不用识别的线程池；
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

DEFAULTS = {"on": True, "max_mb": 300, "max_files": 2000, "days": 14, "ring_sec": 600,
            "control_every": 50}
_INT_KEYS = (("max_mb", "asr_evidence_max_mb", 1), ("max_files", "asr_evidence_max_files", 1),
             ("days", "asr_evidence_days", 1), ("ring_sec", "asr_evidence_ring_sec", 1),
             ("control_every", "asr_evidence_control_every", 0))
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
    for key, name, low in _INT_KEYS:
        raw = settings.get(name)
        if raw is None:
            continue
        if isinstance(raw, int) and not isinstance(raw, bool) and raw >= low:
            cfg[key] = raw
        else:
            _say_once("[警告] settings.json 的 {} 的值 {!r} 不是不小于 {} 的整数，已忽略"
                      .format(name, raw, low))
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
    """(字节数, 文件数, 最新的修改时间)。不跟随符号链接。"""
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


def _write_wav(path, pcm):
    tmp = path.with_name(path.name + ".tmp")
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
        self._saved = {}                 # seq -> 已经写过的 wav 文件名
        self._lock = threading.Lock()
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
        """存这一段（没有原因就不存）。PCM 在这里切好，写盘交给默认线程池。返回是否交出去了。"""
        if not reasons:
            return False
        info = self._seg.get(seq)
        ring = self.ring
        if info is None or ring is None or info["round"] != self.round:
            self._count("skipped_old")
            return False
        if self._full:
            self._count("skipped_cap")
            return False
        wav = self._saved.get(seq)
        pcm = None
        offset = 0
        if wav is None:
            lo = info["start"] - int(CTX_BEFORE_SEC * BYTES_PER_SEC)
            hi = info["end"] + int(CTX_AFTER_SEC * BYTES_PER_SEC)
            if info["start"] < ring.start:
                self._count("skipped_old")
                return False
            lo = max(lo, ring.start)
            pcm = ring.slice(lo, hi)
            if not pcm:
                self._count("skipped_old")
                return False
            offset = info["start"] - lo
            wav = "seq-{:06d}.wav".format(seq)
            self._saved[seq] = wav
        seg_bytes = info["end"] - info["start"]
        record = {
            "seq": seq, "reasons": list(reasons), "audio_end_ts": info["audio_end_ts"],
            "wav": wav, "round": info["round"],
            "seg_offset_ms": int(round(offset * 1000.0 / BYTES_PER_SEC)),
            "seg_ms": int(round(seg_bytes * 1000.0 / BYTES_PER_SEC)),
            "ctx_before_ms": int(round(offset * 1000.0 / BYTES_PER_SEC)),
            "ctx_after_ms": (int(round((len(pcm) - offset - seg_bytes) * 1000.0 / BYTES_PER_SEC))
                             if pcm is not None else None),
            "asr": info["asr"], "language": info["language"],
            "relabeled_from": info["relabeled_from"], "text": info["text"],
            "raw_text": info["raw_text"], "rejected": info["rejected"],
            "windows": info["windows"], "hits": info["hits"],
            "backend": info["backend"], "model": info["model"], "audit": self.audit_name,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        fut = loop.run_in_executor(None, self._write, record, pcm)
        self._pending.add(fut)
        fut.add_done_callback(self._pending.discard)
        return True

    def prune_soon(self, loop):
        """开场清一次上限（在线程池里，不挡开播）。"""
        fut = loop.run_in_executor(None, self._prune_locked_safe)
        self._pending.add(fut)
        fut.add_done_callback(self._pending.discard)

    async def drain(self):
        """等在途的写盘都结束（测试和收尾用）。"""
        while self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

    # ---- 线程池里：写盘与清理 --------------------------------------------------------------
    def _write(self, record, pcm):
        with self._lock:
            try:
                if pcm is not None:
                    self.dir.mkdir(parents=True, exist_ok=True)
                    if shutil.disk_usage(str(self.dir)).free < DISK_LOW_BYTES:
                        self.counts["skipped_disk"] += 1
                        self._saved.pop(record["seq"], None)
                        return
                    size = len(pcm) + 44
                    if (self._own_bytes + size > self.cfg["max_mb"] * 1024 * 1024
                            or self._own_files + 1 > self.cfg["max_files"]):
                        self._full = True
                        self.counts["skipped_cap"] += 1
                        self._saved.pop(record["seq"], None)
                        return
                    _write_wav(self.dir / record["wav"], pcm)
                    self._own_bytes += size
                    self._own_files += 1
                    self.counts["saved"] += 1
                    self.counts["bytes"] += size
                else:
                    self.counts["index_only"] += 1
                line = (json.dumps(_finite_json(record), ensure_ascii=False, allow_nan=False)
                        + "\n").encode("utf-8")
                self.dir.mkdir(parents=True, exist_ok=True)
                index = self.dir / INDEX_NAME
                new_index = not index.exists()
                with open(str(index), "ab") as fh:
                    fh.write(line)
                self._own_bytes += len(line)
                self.counts["bytes"] += len(line)
                if new_index:
                    self._own_files += 1
                for reason in record["reasons"]:
                    self.by_reason[reason] = self.by_reason.get(reason, 0) + 1
                self._since_prune += 1
                if self._since_prune >= PRUNE_EVERY:
                    self._prune_locked()
            except Exception as exc:
                self.counts["failed"] += 1
                if not self._warned:
                    self._warned = True
                    print("[警告] 识别证据音频没存下来（{}），这一场后面再出错不再提示；"
                          "识别、检测和字幕不受影响".format(str(exc)[:160]))

    def _prune_locked(self):
        self._since_prune = 0
        removed, _freed, _b, _f = prune(self.base, self.dir.name,
                                        self.cfg["max_mb"] * 1024 * 1024,
                                        self.cfg["max_files"], self.cfg["days"])
        self.counts["pruned_dirs"] += removed

    def _prune_locked_safe(self):
        with self._lock:
            try:
                self._prune_locked()
            except Exception as exc:
                self.counts["failed"] += 1
                if not self._warned:
                    self._warned = True
                    print("[警告] 清理识别证据音频出错（{}），识别、检测和字幕不受影响"
                          .format(str(exc)[:160]))

    def _count(self, key):
        with self._lock:
            self.counts[key] += 1

    def summary(self):
        with self._lock:
            out = {"on": True}
            out.update(self.counts)
            out["by_reason"] = dict(sorted(self.by_reason.items()))
            out.update({k: self.cfg[k] for k in ("max_mb", "max_files", "days", "ring_sec",
                                                 "control_every")})
            return out

    def close(self):
        """收尾：放掉环形缓冲和段表（约 19 MB），在途的写盘照常写完。"""
        self.ring = None
        self._seg.clear()
