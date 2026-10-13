"""关注名单的存储（弹幕留意 PR-3，spec §3.3）：comment_guard/marked/<主播小写>.json。

没有界面文字：这里只返回 `marks_corrupt` / `marks_unreadable` 这类代码，提示文字由 app/pipeline_guard.py 用 L() 写。

* **内存为准**：改动只标脏，2 秒合并成一次写盘；写盘在 `loop.run_in_executor` 里做（`.tmp` → fsync →
  `os.replace`），事件循环上没有磁盘 IO。只更新「最近发言时间 / 条数」的改动是轻量脏标记，最多每
  60 秒写一次：一个常发言的被关注者不该让磁盘每 2 秒落一次盘。收尾（`flush_now`）一律强制写。
* 写盘失败**不丢脏标记**：写之前清掉的标记在失败时放回去，下一次改动和收尾的 `flush_now` 都会再试一次；
  `failing` 为真表示最近一次写盘没成功（pipeline_guard 据此提示一次）。
* **载入**放在线程池里。文件内容不对（不是合法 JSON、结构不对、版本不认识）：改名成 `.bak-<时间>`，
  按空名单继续，返回 `marks_corrupt`（这一条才有备份）；读不到（权限）、或内容不对而改名失败，就不改名、
  也不覆盖它，返回 `marks_unreadable`，本场只在内存里记、不写盘。
* **键**：`keys` 里任一命中就算同一个人（uid、`s:<sec_uid>`、`h:<handle>`）。只有昵称的人用
  `n:<昵称>`，只在本场内存里有效，**不落盘**。
* **上限与过期**：每个主播最多 500 人，满了先淘汰 `by=auto` 里 `last_seen` 最旧的，手动加入的不淘汰；
  `by=auto` 的人 30 天没再发言，载入时自动移出；手动加入的永久保留。
* 不存弹幕原文；`aka` 最多 3 个。
"""
import asyncio
import json
import os
import re
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARKED_DIR = ROOT / "comment_guard" / "marked"

VERSION = 1
MAX_VIEWERS = 500
AUTO_EXPIRE_SEC = 30 * 86400
SAVE_DELAY_SEC = 2.0          # 结构性改动（加入、移出）合并这么久再写
LAZY_SAVE_SEC = 60.0          # 只是 last_seen / hits 变了：最多这么久写一次
MAX_KEYS = 4
MAX_AKA = 3
MAX_KEY_LEN = 96
MAX_NAME_LEN = 64

_STREAMER_RE = re.compile(r"^[\w.\-]{1,64}$")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
SESSION_PREFIX = "n:"         # 只有昵称的人：本场内存里有效，不落盘


def streamer_key(streamer):
    """主播名 -> 文件名里用的小写键；不合规（空、带路径符、以点开头……）返回 None，此时只在内存里记。"""
    name = str(streamer or "").strip().lower()
    if not name or name.startswith(".") or not _STREAMER_RE.match(name):
        return None
    return name


def clean_key(key):
    """一个账号键：去空白、非空、不超长、没有控制字符；不合规返回 None。"""
    if not isinstance(key, str):
        return None
    key = key.strip()
    if not key or len(key) > MAX_KEY_LEN or _CTRL_RE.search(key):
        return None
    return key


def _clean_text(value, limit=MAX_NAME_LEN):
    if not isinstance(value, str):
        return ""
    return _CTRL_RE.sub("", value).strip()[:limit]


def _clean_ts(value, default):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if value != value or value in (float("inf"), float("-inf")) or value < 0:
        return default
    return int(value)


def is_session_only(keys):
    """所有键都是 n:<昵称>：键不稳，不落盘。"""
    return bool(keys) and all(k.startswith(SESSION_PREFIX) for k in keys)


def _write_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(str(tmp), "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(tmp), str(path))


def _write_safe(path, text):
    """在线程池里跑：成功返回 None，失败返回一句错误说明（不抛）。"""
    try:
        _write_atomic(path, text)
    except Exception as exc:
        return "{}: {}".format(type(exc).__name__, exc)[:200]
    return None


def _backup_name(path, now):
    stamp = datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M%S")
    for n in range(1, 100):
        cand = path.with_name("{}.bak-{}{}".format(path.name, stamp, "" if n == 1 else "-%d" % n))
        if not cand.exists():
            return cand
    return None


def _clean_entry(raw, primary, now):
    """文件里一条记录 -> 内存里的记录；认不出就返回 None。"""
    if not isinstance(raw, dict):
        return None
    keys = []
    raw_keys = raw.get("keys")
    for k in ([primary] + (raw_keys if isinstance(raw_keys, list) else [])):
        k = clean_key(k)
        if k and not k.startswith(SESSION_PREFIX) and k not in keys and len(keys) < MAX_KEYS:
            keys.append(k)
    if not keys:
        return None
    by = raw.get("by") if raw.get("by") in ("auto", "manual") else "manual"
    aka = [a for a in (_clean_text(x) for x in (raw.get("aka") if isinstance(raw.get("aka"), list) else []))
           if a][:MAX_AKA]
    cat = _clean_text(raw.get("cat"), 24) or None
    first = _clean_ts(raw.get("first"), now)
    return {"keys": keys, "nick": _clean_text(raw.get("nick")),
            "handle": _clean_text(raw.get("handle")) or None, "aka": aka, "by": by, "cat": cat,
            "first": first, "last_seen": _clean_ts(raw.get("last_seen"), first),
            "hits": _clean_ts(raw.get("hits"), 0)}


def _read_file(path, now):
    """线程池里跑。返回 (viewers: OrderedDict, code, persist)。code：None、"marks_corrupt"（内容不对、
    已改名成备份）或 "marks_unreadable"（读不到，或挪不走它：没有备份）；persist 为假表示这场不能往这个
    文件里写（别拿空名单覆盖）。"""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return OrderedDict(), None, True
    except OSError:
        return OrderedDict(), "marks_unreadable", False
    viewers = OrderedDict()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("version") != VERSION \
                or not isinstance(data.get("viewers"), dict):
            raise ValueError("shape")
    except Exception:
        backup = _backup_name(path, now)
        try:
            if backup is None:
                raise OSError("no free backup name")
            os.replace(str(path), str(backup))
        except OSError:
            return viewers, "marks_unreadable", False
        return viewers, "marks_corrupt", True
    for primary, raw_entry in data["viewers"].items():
        entry = _clean_entry(raw_entry, primary, now)
        if entry is not None:
            viewers[entry["keys"][0]] = entry
    return viewers, None, True


class MarkStore:
    """一个主播的关注名单。同一个对象跨场次复用：`load(streamer)` 会先清空再读。"""

    def __init__(self, directory=None, clock=time.time):
        self._directory = directory
        self._clock = clock
        self.write_errors = 0
        self.last_error = ""
        self._timer = None
        self._timer_due = None
        self._task = None
        self._reset()

    def clear(self):
        """清空内存里的名单（不碰文件、不排写盘）。换场、关着守卫的那一场用。"""
        self._cancel_timer()
        self._reset()

    def _reset(self):
        self.viewers = OrderedDict()      # 主键 -> 记录
        self.index = {}                   # 任一键 -> 主键
        self.streamer = None              # 小写的主播键；None = 不落盘
        self.path = None
        self.persist = False
        self._dirty = False
        self._lazy = False
        self.failing = False              # 最近一次写盘没成功（成功一次就复位）

    # ---- 路径 ----
    def _dir(self):
        return Path(self._directory) if self._directory is not None else MARKED_DIR

    def __len__(self):
        return len(self.viewers)

    # ---- 载入 / 写盘 ----
    async def load(self, streamer):
        """换场：先把上一份收尾（调用方已 flush_now），再读这个主播的名单。返回 None、"marks_corrupt"
        或 "marks_unreadable"。
        主播名不合规时名单只在内存里，不读也不写任何文件。"""
        self._cancel_timer()
        self._reset()
        name = streamer_key(streamer)
        if name is None:
            return None
        self.streamer = name
        self.path = self._dir() / (name + ".json")
        loop = asyncio.get_running_loop()
        now = self._clock()
        viewers, code, persist = await loop.run_in_executor(None, _read_file, self.path, now)
        self.persist = persist
        expired = 0
        for primary, entry in viewers.items():
            if entry["by"] == "auto" and entry["last_seen"] < now - AUTO_EXPIRE_SEC:
                expired += 1
                continue
            self._put(primary, entry)
        if expired:
            self._mark_dirty(structural=True)
        return code

    def _render(self):
        persisted = OrderedDict((p, self._public(e, with_session=False)) for p, e in self.viewers.items()
                                if not is_session_only(e["keys"]))
        return json.dumps({"version": VERSION, "streamer": self.streamer, "viewers": persisted},
                          ensure_ascii=False, indent=1)

    def _mark_dirty(self, structural):
        if not self.persist or self.path is None:
            return
        if structural:
            self._dirty = True
            self._schedule(SAVE_DELAY_SEC)
        else:
            self._lazy = True
            self._schedule(LAZY_SAVE_SEC)

    def _schedule(self, delay):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return                        # 没有事件循环（同步用法）：留给 flush_now
        due = loop.time() + delay
        if self._timer is not None:
            if self._timer_due is not None and self._timer_due <= due:
                return
            self._timer.cancel()
        self._timer_due = due
        self._timer = loop.call_later(delay, self._fire)

    def _cancel_timer(self):
        if self._timer is not None:
            self._timer.cancel()
        self._timer = None
        self._timer_due = None

    def _fire(self):
        self._timer = None
        self._timer_due = None
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._write_loop())

    async def _write_loop(self):
        loop = asyncio.get_running_loop()
        while (self._dirty or self._lazy) and self.persist and self.path is not None:
            dirty, lazy = self._dirty, self._lazy
            self._dirty = self._lazy = False
            error = await loop.run_in_executor(None, _write_safe, self.path, self._render())
            if error:
                self.write_errors += 1
                self.last_error = error
                self.failing = True
                # 脏标记放回去：不然收尾的 flush_now 看到「没有待写的」就什么都不做，名单只活在内存里。
                # 这里不自己重试（不至于紧循环）；下一次改动和收尾会再写一次
                self._dirty = self._dirty or dirty
                self._lazy = self._lazy or lazy
                print("[弹幕] 关注名单写不进去：{}".format(error))
                break
            self.failing = False

    async def flush_now(self):
        """收尾：把还没写的立刻写完（含轻量脏标记）。不抛异常。"""
        try:
            self._cancel_timer()
            task = self._task
            if task is not None and not task.done():
                await task
            if self._dirty or self._lazy:
                await self._write_loop()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("[弹幕] 关注名单收尾写盘出错：{}".format(exc))

    def close(self):
        """同步丢弃定时器（不写盘）；测试和退出路径用。"""
        self._cancel_timer()

    # ---- 查询 ----
    def match(self, keys, nick=None):
        """任一键命中就算同一个人，返回主键；没有返回 None。nick 只在调用方确定这个人没有稳定键时才传。"""
        for k in keys or ():
            if k and k in self.index:
                return self.index[k]
        if nick:
            return self.index.get(SESSION_PREFIX + nick)
        return None

    @staticmethod
    def _public(entry, with_session=True):
        out = {"keys": list(entry["keys"]), "nick": entry["nick"], "handle": entry["handle"],
               "aka": list(entry["aka"]), "by": entry["by"], "cat": entry["cat"],
               "first": entry["first"], "last_seen": entry["last_seen"], "hits": entry["hits"]}
        if with_session and is_session_only(entry["keys"]):
            out["session"] = True
        return out

    def snapshot(self):
        """config["guard_marked"] 用：主键 -> 记录（复制）。"""
        return OrderedDict((p, self._public(e)) for p, e in self.viewers.items())

    def entry(self, primary):
        e = self.viewers.get(primary)
        return self._public(e) if e is not None else None

    # ---- 改动 ----
    def _put(self, primary, entry):
        self.viewers[primary] = entry
        for k in entry["keys"]:
            self.index.setdefault(k, primary)

    def add(self, keys, nick="", handle=None, by="manual", cat=None, ts=None):
        """加入一个人。返回 (状态, 主键, 被淘汰的主键列表)：
        状态 added / exists / upgraded（自动加入的人被手动确认，改记成 manual）/ full（满了且没有
        自动加入的可淘汰）/ bad_key。"""
        clean = []
        for k in keys or ():
            k = clean_key(k)
            if k and k not in clean and len(clean) < MAX_KEYS:
                clean.append(k)
        if not clean:
            return "bad_key", None, []
        now = _clean_ts(ts, int(self._clock()))
        existing = self.match(clean)
        if existing is not None:
            entry = self.viewers[existing]
            self._merge_keys(existing, clean)
            if by == "manual" and entry["by"] != "manual":
                entry["by"] = "manual"
                self._mark_dirty(structural=not is_session_only(entry["keys"]))
                return "upgraded", existing, []
            return "exists", existing, []
        evicted = []
        while len(self.viewers) >= MAX_VIEWERS:
            victim = self._oldest_auto()
            if victim is None:
                return "full", None, evicted
            self.remove(victim)
            evicted.append(victim)
        entry = {"keys": clean, "nick": _clean_text(nick), "handle": _clean_text(handle) or None,
                 "aka": [], "by": "auto" if by == "auto" else "manual", "cat": _clean_text(cat, 24) or None,
                 "first": now, "last_seen": now, "hits": 0}
        self._put(clean[0], entry)
        if not is_session_only(clean):
            self._mark_dirty(structural=True)
        return "added", clean[0], evicted

    def _oldest_auto(self):
        best = None
        for primary, e in self.viewers.items():
            if e["by"] == "auto" and (best is None or e["last_seen"] < self.viewers[best]["last_seen"]):
                best = primary
        return best

    def _merge_keys(self, primary, keys):
        entry = self.viewers[primary]
        changed = False
        for k in keys:
            if k.startswith(SESSION_PREFIX) or k in entry["keys"] or len(entry["keys"]) >= MAX_KEYS:
                continue
            if k in self.index and self.index[k] != primary:
                continue                  # 已经属于另一个人：不抢
            entry["keys"].append(k)
            self.index[k] = primary
            changed = True
        return changed

    def remove(self, key):
        """按任一键移出。返回 (主键, 记录) 或 (None, None)。"""
        primary = self.index.get(key)
        if primary is None:
            return None, None
        entry = self.viewers.pop(primary, None)
        for k in (entry["keys"] if entry else ()):
            if self.index.get(k) == primary:
                del self.index[k]
        if entry is not None and not is_session_only(entry["keys"]):
            self._mark_dirty(structural=True)
        return primary, entry

    def touch(self, primary, keys=(), nick="", ts=None):
        """名单里的人又发言了：更新 last_seen、条数、新出现的昵称（最多留 3 个）和多出来的键。
        返回这条记录的键或昵称有没有变化（调用方据此决定要不要广播）。"""
        entry = self.viewers.get(primary)
        if entry is None:
            return False
        now = _clean_ts(ts, int(self._clock()))
        entry["last_seen"] = max(entry["last_seen"], now)
        entry["hits"] += 1
        changed = self._merge_keys(primary, [k for k in (clean_key(x) for x in keys) if k])
        nick = _clean_text(nick)
        if nick and nick != entry["nick"] and nick not in entry["aka"]:
            if not entry["nick"]:
                entry["nick"] = nick
            else:
                entry["aka"].append(nick)
                del entry["aka"][:-MAX_AKA]
            changed = True
        if not is_session_only(entry["keys"]):
            self._mark_dirty(structural=changed)
        return changed
