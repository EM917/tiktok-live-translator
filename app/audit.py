# i18n: done
"""审计日志：每一段音频的识别结果逐条落盘（JSONL）。

合规监听场景里，事后要能回答「昨天主播明明说了某个违禁词，为什么没报警」。
这有三种完全不同的原因，只有把被过滤掉的候选也记下来才能区分：
  A. Whisper 根本没听出来       —— raw 里也没有
  B. 听出来了但被质量过滤丢掉   —— rejected 里有，附原因
  C. ASR 正确但检测器没匹配上   —— text 里有、hits 为空
写入失败不抛给实时链路（日志不能拖累报警），但也不再静默：见 AuditLog._write。
"""
import json
import math
import os
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

from .i18n import L, bimap, of
from .redact import strip_query

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

# 写不进去时先留在内存里、能写了再补写的记录类型：会话头、报警、会话尾。
# 逐段字幕和译文只计数不留——几个小时的磁盘满不能把内存吃光。
RETAIN_TYPES = ("session_start", "alert", "session_end")
RETAIN_MAX = 500


def _open_new(directory, stamp):
    """独占创建审计文件。同一秒起两场（双击「开始」、一秒内换主播）会得到同一个
    stamp：以前用追加模式，第二场写进第一场的文件，provenance 把两场混成一场归到
    第一个主播名下，按会话切语料的工具全部错归属。冲突就加 -2/-3 后缀。

    无缓冲的二进制句柄：每条记录要么整行落盘、要么一个字节都不留（见
    AuditLog._append_locked）。文本句柄在磁盘满时会把一部分记录留在 Python 的
    缓冲区里、丢掉另一部分，事后既数不清丢了几条，补写报警时还可能写出重复的。"""
    for n in range(1, 100):
        name = "session-{}{}.jsonl".format(stamp, "" if n == 1 else "-{}".format(n))
        path = directory / name
        try:
            return path, path.open("xb", buffering=0)
        except FileExistsError:
            continue
    raise OSError(L("同一秒内已有 99 个审计文件", "99 audit files already exist for this second"))


def _iso_from_epoch(ts):
    try:
        return datetime.fromtimestamp(float(ts)).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def strip_url_queries(text, limit=300):
    """写进审计的错误文本：去掉网址里的 query。签名流地址的 sign/expire、HF 的
    token 都在 query 里——审计文件不能变成两周有效的拉流凭证。规则在 redact.strip_query。"""
    return strip_query(text, limit)


def clean_error(exc, limit=200):
    """错误文字写进审计或界面之前去掉 URL 的查询串（签名地址、token 常在里面）。"""
    # 我们自己抛的 OSError(L(...)) 带着英文：中英两句各清洗一次，界面按语言取，审计仍是中文。
    # 别的异常 of(exc) 就是 str(exc)，而 strip_query 本来也是先 str() 再清洗：中文结果与改造前
    # 的 strip_query(exc, limit) 逐字节相同（tests/test_i18n_backend_modules_en.py 钉着）
    return bimap(lambda s: strip_query(s, limit), of(exc))


def _now_ms():
    return datetime.now().isoformat(timespec="milliseconds")


def _finite_json(value):
    """把记录里的 NaN / inf 换成 None（写出来是 null），dict / list / tuple 逐层换，别的原样返回。
    json.dumps 默认把 NaN 写成裸的 NaN，那不是合法 JSON：严格的解析器（jq、浏览器的 JSON.parse、
    别的语言的库）读到这一行就报错，整份审计跟着读不了。10-14 录音里就有一段 avg_logprob 是 NaN。"""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _finite_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite_json(v) for v in value]
    return value


def _json_default(obj):
    """json.dumps 认不出的类型怎么写。以前写入只接 ValueError：np.float32、np.int64、set、bytes、自定义对象
    都抛 TypeError，从 _write 一路冒到拉流那一轮，一段坏记录就让整场以 internal_error 结束（专家审查 B2）。
      - numpy 标量（0 维）：.item() 换成 Python 自己的数；非有限的浮点换成 None（同 _finite_json）；
      - set / frozenset：按 repr 排好序的 list——元素类型混杂时排序也不抛；
      - bytes / bytearray：只记 <bytes N>，不写内容；
      - 其余认不出的：只记 <类型名>。不用 str(obj)：异常原文、带签名的流地址会借这条路进审计。
    json 认得的类型不会走到这里：正常记录的输出和以前逐字节相同。"""
    if type(obj).__module__ == "numpy" and getattr(obj, "ndim", None) == 0:
        try:
            value = obj.item()
        except Exception:
            value = obj
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        if value is None or isinstance(value, (bool, int, str)):
            return value
    if isinstance(obj, (set, frozenset)):
        return _finite_json(sorted(obj, key=repr))
    if isinstance(obj, (bytes, bytearray)):
        return "<bytes {}>".format(len(obj))
    return "<{}>".format(type(obj).__name__)


def _encode(record):
    """一条记录编成整行（UTF-8，带换行）；怎么都编不出来返回 None。
    先照原样编：正常记录走这一步，和以前逐字节相同。有 NaN / inf 再换成 null 编一次（见 _finite_json）。
    两次都带 default=_json_default：NaN 加 set 这种组合在第二次也不再抛 TypeError。"""
    try:
        return (json.dumps(record, ensure_ascii=False, allow_nan=False, default=_json_default)
                + "\n").encode("utf-8")
    except ValueError:
        pass
    except (TypeError, RecursionError):
        return None
    try:
        return (json.dumps(_finite_json(record), ensure_ascii=False, allow_nan=False,
                           default=_json_default) + "\n").encode("utf-8")
    except (ValueError, TypeError, RecursionError):
        return None


# 编不出来时不能整条换成占位记录的骨架记录：会话头和会话尾。按会话切审计的工具认它们，
# 只换掉编不出来的那几个字段（见 _sanitized），记录本身照写
SKELETON_TYPES = ("session_start", "session_end")


def _sanitized(record):
    """骨架记录编不出来时用：编不出来的顶层字段换成 <类型名>，键不是字符串的整个丢掉。"""
    out = {}
    for key, value in record.items():
        if not isinstance(key, str):
            continue
        if _encode(value) is None:
            value = "<{}>".format(type(value).__name__)
        out[key] = value
    return out


def _bad_field_types(record):
    """一条编不出来的记录里，自己就编不出来的那几个顶层值（和键）的类型名。只有类型名，不写值。"""
    bad = set()
    if isinstance(record, dict):
        for key, value in record.items():
            if not isinstance(key, (str, int, float, bool)) and key is not None:
                bad.add(type(key).__name__)
            elif _encode(value) is None:
                bad.add(type(value).__name__)
    return sorted(bad)


def _placeholder(record):
    """编不出来的记录在原位置换成这一条：原来是哪种记录、哪几种类型的值写不出来。"""
    orig = record.get("type") if isinstance(record, dict) else None
    return {"type": "audit_record_error", "at": _now_ms(),
            "orig_type": orig[:40] if isinstance(orig, str) else type(orig).__name__,
            "bad_fields": _bad_field_types(record)}


class AuditLog:
    def __init__(self, room_url="", log_dir=None, extra=None):
        self._lock = threading.Lock()
        self._fh = None
        self._pos = 0                    # 已整行落盘的字节数（写到一半失败时截回这里）
        self._torn = False               # 截回失败、文件里留着半行
        # 写入失败的状态。以前 _write 把 OSError 吞掉就完了：磁盘满的那段时间报警
        # 照常上屏，审计里一条没有，界面和自检都不知道。
        self.open_error = None           # 文件根本没建起来的原因（此时 path 为 None）
        self.failing = False             # 此刻是否写不进去
        self.failing_since = None
        self.last_error = ""
        self.write_failures = 0          # 本场累计写失败的记录条数
        self.lost_records = 0            # 其中没留在内存里、确定丢了的条数
        # 编不出 JSON 的记录条数（字段类型的问题，不是磁盘的问题）。单独计、不碰 failing：
        # 算进写失败的话，下一跳就挂出红色「审计写不进去」、补一条 audit_gap，把一个字段类型
        # 的问题报成磁盘故障。不为 0 时 session_end 带上它
        self.serialize_failures = 0
        self.last_gap = None             # 最近一次恢复时写下的 audit_gap
        self._lost_in_outage = 0
        self._retained = deque()         # 等补写的会话头/报警/会话尾（编码好的整行）
        # 每次「开始写不进去」调一次，不是每条都调。可能在事件循环之外的线程里被调用，
        # 调用方自己切回循环（call_soon_threadsafe）
        self.on_write_error = None
        directory = Path(log_dir) if log_dir else LOG_DIR
        try:
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            self.path, self._fh = _open_new(directory, stamp)
            # 记下这一场是哪个版本、哪份词表跑的。事后拿数字回来复盘时，
            # 「这个数是哪几个主播、哪个 commit、哪份词表产生的」要答得出来。
            # extra 是调用方掌握、这里拿不到的运行时事实（引擎、语言等）——
            # 2026-08-26 排查时正因为没记这些，只能靠延迟指纹反推那一场
            # 到底是 DeepL 还是本地 1.8B 在翻。
            from .glossary import GLOSSARY_FILE
            from .provenance import code_commit, file_hash
            root = Path(__file__).resolve().parent.parent
            # 核心字段放后面：extra 与其撞名时核心字段赢。审计的骨架字段
            # 不能被调用方一个手滑的键名静默改写
            self._write(dict(extra or {},
                             **{"type": "session_start", "room_url": room_url,
                                "started_at": datetime.now().isoformat(timespec="seconds"),
                                "code_commit": code_commit(),
                                # 全局表路径用的这一刻从 glossary 模块取（就是 root / "glossary.txt"）：
                                # 测试把它指到临时目录，不让审计头去读开发者本机的真实词表
                                "glossary_hash": file_hash(GLOSSARY_FILE),
                                "vocative_hash": file_hash(root / "app" / "vocative.py")}))
        except OSError as exc:
            if self._fh is None:
                self.path = None
                self.open_error = clean_error(exc)

    def _write(self, record):
        """写一条。失败不抛给调用方（实时链路不能被日志拖住），但要记下来：
          - failing / failing_since / last_error / write_failures 供管线每 10 秒查看；
          - 开始写不进去的那一刻调一次 on_write_error；
          - 会话头、报警、会话尾留在内存里（最多 RETAIN_MAX 条），其余只计数；
          - 之后第一次能写时先补一条 audit_gap（起止时间、丢了几条），再按原顺序补写
            留下的记录，最后才是这一条。
        每条都是整行直接落盘（无缓冲），崩溃也不丢最后几条。

        编不出 JSON 的记录（见 _encode：认不出的类型已经由 _json_default 换成能写的值，剩下的是
        键不是字符串、循环引用这一类）不抛、不算写失败：计进 serialize_failures，原位置写一条
        audit_record_error 占位（原来的类型、写不出来的值的类型名）。会话头、会话尾不换成占位，
        只把写不出来的字段换成 <类型名>（见 _sanitized）。以前这里只接 ValueError，TypeError
        冒到调用方，一段坏记录就结束整场（专家审查 B2）。"""
        if self._fh is None:
            return
        data = _encode(record)
        unencodable = data is None
        if unencodable:
            if isinstance(record, dict) and record.get("type") in SKELETON_TYPES:
                data = _encode(_sanitized(record))
            if data is None:
                record = _placeholder(record)
                data = _encode(record)
            if data is None:
                return
        started = None
        with self._lock:
            if self._fh is None:
                return
            if unencodable:
                self.serialize_failures += 1
            try:
                if self.failing or self._retained:
                    self._drain_locked()
                self._append_locked(data)
            except (OSError, ValueError) as exc:
                started = self._note_failure_locked(record, data, exc)
        callback = self.on_write_error
        if started is not None and callback is not None:
            try:
                callback(started)
            except Exception:
                pass

    def _append_locked(self, data):
        """整行写进去，或者一个字节都不留。写到一半失败（磁盘只剩几个字节）就把文件
        截回这一行之前：半行 JSON 会让逐行读日志的工具在这里断掉。"""
        fh = self._fh
        if self._torn:
            data = b"\n" + data          # 上次没截回去的半行，先把它隔成单独一行
        view = memoryview(data)
        done = 0
        try:
            while done < len(data):
                n = fh.write(view[done:])
                if not n:
                    raise OSError(L("审计文件写入返回 0 字节",
                                    "Writing to the audit file returned 0 bytes"))
                done += n
        except (OSError, ValueError):
            if done:
                try:
                    fh.truncate(self._pos)
                    fh.seek(self._pos)
                except (OSError, ValueError):
                    self._torn = True
                    try:
                        self._pos = fh.tell()
                    except (OSError, ValueError):
                        pass
            raise
        self._pos += done
        self._torn = False

    def _drain_locked(self):
        """写不进去之后第一次能写：先记 audit_gap，再补写留在内存里的记录。
        任何一步写不进去就抛出，留到下一条再试。"""
        if self.failing:
            now = _now_ms()
            gap = {"type": "audit_gap", "at": now, "from": self.failing_since, "to": now,
                   "lost_records": self._lost_in_outage,
                   "retained_records": len(self._retained),
                   "error": self.last_error}
            self._append_locked((json.dumps(gap, ensure_ascii=False) + "\n").encode("utf-8"))
            self.failing = False
            self.failing_since = None
            self._lost_in_outage = 0
            self.last_gap = gap
        while self._retained:
            self._append_locked(self._retained[0])
            self._retained.popleft()

    def _note_failure_locked(self, record, data, exc):
        """记一次写失败。只在「这一刻开始写不进去」时返回信息（给回调），否则 None。"""
        self.write_failures += 1
        self.last_error = clean_error(exc)
        started = not self.failing
        if started:
            self.failing = True
            self.failing_since = _now_ms()
        if record.get("type") in RETAIN_TYPES and len(self._retained) < RETAIN_MAX:
            self._retained.append(data)
        else:
            self._lost_in_outage += 1
            self.lost_records += 1
        if started:
            return {"since": self.failing_since, "error": self.last_error}
        return None

    def unwritten(self):
        """此刻写不进去的这一段：从什么时候起、最近的错误、确定丢了几条、还有几条留在内存里
        等补写。没在失败时返回 None。文件关掉之后留在内存里的也就丢了——停止时的提示按这个说。"""
        with self._lock:
            if not self.failing:
                return None
            return {"since": self.failing_since, "error": self.last_error,
                    "lost": self._lost_in_outage, "retained": len(self._retained)}

    def detached(self):
        """审计文件还在不在它的路径上。logs/ 在访达里被移走或删掉时写入并不报错，
        进的是路径上已经没有的那个文件——删掉的话关闭时就全没了。
        Windows 上打开着的文件删不掉也改不了名，只看路径在不在。"""
        fh, path = self._fh, getattr(self, "path", None)
        if fh is None or path is None:
            return False
        try:
            if os.name == "nt":
                return not os.path.exists(str(path))
            opened = os.fstat(fh.fileno())
            try:
                current = os.stat(str(path))
            except FileNotFoundError:
                return True
            return (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        except (OSError, ValueError):
            return False

    def segment(self, seq, result, audio_end_ts, asr_ms, hits, asr=None, tail=False):
        """一段音频的完整记录：接受的文本、被丢弃的候选及原因、命中的违禁词。

        asr 是识别风险的子对象（app/asr_risk.py assess 的 audit：只有数字和 ASCII 代码，不含文字）。
        不传时这一行和以前逐字节相同；传了才多一个 "asr" 键，放在最后。tail=True 是收尾时 ffmpeg
        吐出来的音频切出的段（晚到的段），多一个 "tail": true。"""
        record = {
            "type": "segment",
            "seq": seq,
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "audio_end_ts": round(audio_end_ts, 3),
            "asr_ms": round(asr_ms, 1),
            "language": result.language,
            "text": result.text,
            "raw_text": result.raw_text,
            "rejected": result.rejected,
            "hits": hits,
        }
        # 只在真的发生过「检测语言不在允许列表里、强制重转」时才带这一列——
        # 平时不带 null，省得每条 segment 都多一个恒为空的字段
        relabeled_from = getattr(result, "relabeled_from", None)
        if relabeled_from:
            record["relabeled_from"] = relabeled_from
        if asr is not None:
            record["asr"] = asr
        if tail:
            record["tail"] = True
        self._write(record)

    def translation(self, seq, translated, translate_ms, ok, engine=None):
        """译文是后到的，单独记一条，按 seq 与上面的 segment 对应。

        不合并进 segment 是因为 segment 必须在识别一出来就落盘——报警证据
        不能等翻译。但审计只有西语原文是残的：事后复查一条报警时，中控要看
        的是「这句被翻成了什么」。翻译失败也记，否则日志里会静默缺一条。

        engine 记的是**这一条**实际用的引擎：会话中途可以在界面里换引擎，
        只看 session_start 会把换挡后的译文归到旧引擎头上。强译那边的
        translation_strong 从第一天就带 model 字段，快译缺这个，做引擎
        对比时快译的归属只能靠猜。"""
        self._write({
            "type": "translation",
            "seq": seq,
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "translated": translated,
            "translate_ms": round(translate_ms, 1),
            "ok": bool(ok),
            "engine": engine,
        })

    def translation_strong(self, seq, translated, translate_ms, ok, model,
                           trigger, error=None, queued=False):
        """用最强模型重译的结果，**单独一种记录类型**。

        不复用上面的 translation：同一个 seq 会同时存在快译和强译两条，若类型
        相同就无法从日志判断哪条是哪个模型翻的——而这正是事后复核最需要区分的
        东西。收工后的批量重译工具写的也是这个类型，两条路径保持一致。

        trigger 说明这次重译是谁发起的：banned_term（命中违禁词自动升级）
        或 manual（中控点了「重译」）。

        error 是这一次没出译文时看到了什么：refusal 或 refusal:<接口给的类别>（Claude 拒答）、
        HTTP <状态码>（再接 400/402 的接口原话）、译文不像译文被丢弃——这几种强模型是 Claude 时
        才有可能写：本地模型不会拒答，失败只剩 ok=false 也分得清。还有 cancelled：桌面点的
        「重译」还没做完就被停止、换主播取消了（哪种强模型都可能，英文代码，同 session_end 的
        reason）。没有要说的就不写这个键，老记录和成功的记录形状不变。

        queued=True 只跟 cancelled 一起出现：还没开始调强模型就被取消（排在队里，或者强模型连接还没
        准备好），这时 model 是 null、translate_ms 是 0。已经在等强模型时被取消的那条不带这个键。
        不是 True 就不写这个键：其余记录逐字节不变。"""
        row = {
            "type": "translation_strong",
            "seq": seq,
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "translated": translated,
            "translate_ms": round(translate_ms, 1),
            "ok": bool(ok),
            "model": model,
            "trigger": trigger,
        }
        if error:
            row["error"] = str(error)[:200]
        if queued:
            row["queued"] = True
        self._write(row)

    def translation_engine_error(self, engine, status, error=None, model=None):
        """翻译引擎的 HTTP 回应说明它这会儿用不了：本地 Ollama 连续几次非 200（没有
        这个模型、载入失败……），或远程引擎拒绝了密钥/模型（401/403/404）。translation
        记录里只有 ok=false，没有这一条就答不出「这一场的译文为什么整段是空的」。

        error 是 Ollama 的原话；远程引擎一般不记原话（它们的错误说明里可能带打码后的
        密钥片段），只有 Claude 的 400/402 记（说的是请求体或账务，不带密钥）。同一个引擎
        在同一场里只记一次，中间成功过才会再记。"""
        self._write({
            "type": "translation_engine_error",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "engine": engine,
            "model": model,
            "status": status,
            "error": (error or "")[:200] or None,
        })

    def translation_refusal(self, seq, engine, model, category, trigger=None):
        """引擎拒绝翻译这一句（Claude：HTTP 200，stop_reason 为 refusal），按 seq 对应。
        translation 记录里只有 ok=false，没有这一条就分不清「接口出错」和「模型拒答」。
        category 照抄接口的 stop_details.category，可能为 null。每一次拒答都记。

        trigger 只在「重译」被拒时写（manual / viewer / banned_term，和 translation_strong 的
        同名字段一样）：没有它，这一行和同一个 seq 的字幕快译被拒分不开。字幕那一条不写这个键。"""
        row = {
            "type": "translation_refusal",
            "seq": seq,
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "engine": engine,
            "model": model,
            "category": category,
        }
        if trigger:
            row["trigger"] = trigger
        self._write(row)

    def translation_rejected(self, seq, engine, model, reason, rejected,
                             attempt=None, retry=None, retry_ms=None):
        """引擎回来的不是译文（在回话、说不能翻译、加注释，见 translator.looks_like_reply），这一句
        显示原文。translation 记录里只有 ok=false；没有这一条，分不清「接口出错」和「模型在回话」，
        也看不到它回了什么。rejected 是模型的原话（截 300 字），和 translation 记录里的译文同一类数据。

        Claude 字幕被拦下后会原样再问一次（Pipeline._retry_claude_reply），只有这条路写下面三个键，
        其余引擎的记录保持原来的 7 个键：
          * attempt：这是第几次请求被拦下的（1 或 2）；
          * retry：紧接着那次重试的结局——ok（重试译出来了，这一句不再显示原文）/ rejected（重试
            也不是译文）/ no_output（重试没有返回文字：超时、HTTP 错误、空输出）/ refusal（重试被
            拒答，拒答本身另记一条 translation_refusal）/ skipped（没有重试：引擎在暂停、已被换掉，
            或有字幕在排队）；最后一次被拦下的那条没有下一次，不写这个键；
          * retry_ms：那次重试的耗时（毫秒）；skipped 没有请求，不写。
        三个键里只有代码和数字，不写中文——审计在中英两种界面下逐字节相同。"""
        row = {
            "type": "translation_rejected",
            "seq": seq,
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "engine": engine,
            "model": model,
            "reason": reason,
            "rejected": (rejected or "")[:300],
        }
        if attempt is not None:
            row["attempt"] = attempt
            if retry is not None:
                row["retry"] = retry
            if retry_ms is not None:
                row["retry_ms"] = retry_ms
        self._write(row)

    def translation_cooldown(self, engine, status, seconds):
        """翻译接口回了 429、程序暂停请求的那一刻。暂停期间每条字幕都是 ok=false，
        有这一条才分得清「接口让等一会儿」和「引擎坏了」。中间成功过才会再记。"""
        self._write({
            "type": "translation_cooldown",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "engine": engine,
            "status": status,
            "seconds": seconds,
        })

    def alert_translation(self, alert_ids, model, ok, ms, fallback, why,
                          downgraded_for_backlog=False, downgraded_for_busy=False,
                          error=None):
        """报警上下文的中文翻译，一次扫描记一条。

        **不复用 translation_strong**：离线重译工具（tools/retranslate_audit.py）见到
        某个 seq 有 ok 的 translation_strong 就跳过这段，而报警编号和字幕 seq 是两套
        号——混用的话，第 N 条报警会挡住第 N 段字幕的重译。

        model 是最后给出（或没给出）译文的那个模型；fallback=真表示强模型没译出来或
        译文不像译文，改用了常驻引擎；downgraded_for_backlog / downgraded_for_busy
        表示一开始就没用强模型（识别正在积压 / 另一条报警正占着强模型）。error 是
        强模型那次失败时 Ollama 的回应（状态码和原话），没有就是 None。"""
        self._write({
            "type": "alert_translation",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "alert_ids": list(alert_ids),
            "model": model,
            "ok": bool(ok),
            "ms": round(ms, 1),
            "fallback": bool(fallback),
            "why": why or "",
            "downgraded_for_backlog": bool(downgraded_for_backlog),
            "downgraded_for_busy": bool(downgraded_for_busy),
            "error": (error or "")[:200] or None,
        })

    def model_pull(self, state, model, error=None):
        """本地翻译模型的下载。state：start / done / failed，以及 deferred（直播中不
        下载，停止后再下）和 in_progress（这一场开始时已有下载在跑）。下载和拉流抢
        同一条网络，事后要能把一段音频中断和它对上号——以前会话日志里没有下载的
        任何痕迹。下载本身跨场次，这条记在事件发生时正开着的那场里。"""
        self._write({
            "type": "model_pull",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "state": state,
            "model": model,
            "error": (error or "")[:300] or None,
        })

    def resolve(self, record):
        """流地址解析的一次尝试：第几次、成没成、走了哪几层、各层结果与耗时。

        record 由 Pipeline._log_resolve 组好（type=resolve）。这条记录存在的
        理由：2026-09-06 一场直播前两次解析失败、第三次才成功，事后查不到
        任何证据——程序 stdout 指向 /dev/null，会话日志又只记开始/结束。
        只能靠「只有一个会话文件」+「重试循环只对一种错误生效」+「秒数
        对得上」倒推，换个失败模式就推不出来了。"""
        self._write(dict(record, at=datetime.now().isoformat(timespec="milliseconds")))

    def dropped_audio(self, queue_depth=None):
        """识别跟不上时丢掉的音频段。漏报的第四种成因——这一段压根没进 ASR，
        不记下来事后就无法归因。"""
        self._write({
            "type": "audio_dropped",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "queue_depth": queue_depth,
        })

    def asr_failed(self, segment_ms, error, queue_depth=None):
        """识别本身抛异常、这段音频没有进检测器——漏报的第五种成因。以前只有
        终端一行 print，打包运行时 stdout 指向 /dev/null，事后无从归因。"""
        self._write({
            "type": "asr_failed",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "segment_ms": round(segment_ms, 1),
            "error": error,
            "queue_depth": queue_depth,
        })

    def asr_overrun(self, asr_ms, segment_ms):
        """识别耗时超过音频时长的调用——复读跑飞的痕迹，事后排查丢段用。"""
        self._write({
            "type": "asr_overrun",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "asr_ms": round(asr_ms, 1),
            "segment_ms": round(segment_ms, 1),
        })

    def asr_config(self, config):
        """这一场实际在听的识别配置（backend/model/device/compute_type/note，改用过
        CPU 时带上来龙去脉）。以前只 print 一行，打包运行时 stdout 指向 /dev/null，
        复盘「这场为什么漏报」时答不出当时是 GPU large-v3 还是 CPU turbo 在听。"""
        self._write(dict(config, type="asr_config",
                         at=datetime.now().isoformat(timespec="milliseconds")))

    def asr_memory(self, active_mb, cache_mb, peak_mb):
        """MLX 分配器的读数（MB）：在用、缓冲缓存、峰值；读不到的是 null。模型就绪时一条，
        之后每 5 分钟一条。2026-09-17 监听进程 7.5 GB 里有 6948 MB 是 Metal 缓冲，当时只能
        靠 `footprint` 从外面看；有了这条，缓存随时间怎么涨在会话日志里就能看到。"""
        self._write({
            "type": "asr_memory",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "active_mb": active_mb, "cache_mb": cache_mb, "peak_mb": peak_mb,
        })

    def asr_load_failed(self, backend, model, device, error):
        """识别模型没能加载：这一场一段都不会识别。以前只有界面上一句话。"""
        self._write({
            "type": "asr_load_failed",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "backend": backend, "model": model, "device": device,
            "error": strip_url_queries(error),
        })

    def asr_backend_fallback(self, from_, to, error, **extra):
        """识别出错后改用了另一套配置（to=None 表示没有可以安全改用的，没换）。"""
        self._write(dict(
            {k: (strip_url_queries(v) if k.endswith("error") else v)
             for k, v in extra.items()},
            type="asr_backend_fallback",
            at=datetime.now().isoformat(timespec="milliseconds"),
            **{"from": from_, "to": to, "error": strip_url_queries(error)}))

    def asr_stalled(self, inflight_sec, backlog_sec, dropped):
        """一段音频识别了很久还没返回。asr_overrun 要等调用返回才写、asr_failed 只在
        抛异常时写——调用一直不返回时，会话日志里只剩一条条 audio_dropped。"""
        self._write({
            "type": "asr_stalled",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "inflight_sec": round(inflight_sec, 1),
            "backlog_sec": round(backlog_sec, 1),
            "dropped": dropped,
        })

    def health(self, level, backlog_sec, reason="backlog", text="", dropped=None,
               asr_failed=None):
        """检测健康状态的变化（积压、识别出错、识别卡住、改用 CPU）。只在变化时写。
        dropped 是积压挤掉的段数，asr_failed 是识别出错没检测的段数，两者分开记。"""
        self._write({
            "type": "health",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "level": level, "reason": reason,
            "backlog_sec": round(backlog_sec or 0.0, 1),
            "dropped": dropped,
            "asr_failed": asr_failed,
            "text": (text or "")[:300],
        })

    def selfcheck(self, checks, summary, full=True):
        """自检结论。full=False 表示只记了等级有变化的那几行。自检本来就是为了抓静默
        降级，结论却只上界面、只 print——事后说不出这场跑的时候哪项能力是坏的。"""
        self._write({
            "type": "selfcheck",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "full": bool(full),
            "summary": summary,
            "checks": [{"name": c.get("name"), "level": c.get("level"),
                        "detail": strip_url_queries(c.get("detail"), 500)}
                       for c in checks],
        })

    def terms_changed(self, digest):
        """直播中违禁词表文件被改了（新内容要「停止→开始」后才生效）。"""
        self._write({
            "type": "terms_changed",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "hash": digest,
        })

    def comment_source(self, state, detail="", raw=""):
        """弹幕连接状态的变化（连上、断开、被拒、组件更新）。弹幕不在报警链路上，
        记下来是为了事后答得出「弹幕是哪一刻坏的、之前几场好不好」——2026-09-14
        弹幕连接每次 HTTP 400 时，会话日志里一条弹幕状态都没有，这两个问题都答不上来。"""
        self._write({
            "type": "comment_source",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "state": state,
            "detail": (detail or "")[:300],
            # 服务端给的原始原因（握手被拒时的 Handshake-Msg、状态码）。面板上只有
            # 中文说明，事后要分清「这次 400」和「另一种 400」只能靠这一栏
            "raw": (raw or "")[:300],
        })

    # ---- 弹幕留意（spec §8）：只记计数、模型调用和闸门转折 ----
    # 弹幕原文、昵称、账号 id 一律不进审计（沿用 PR #58 以来「弹幕与审计隔离」的约定）；字段值只用代码
    # 和数字，中英两种界面下逐字节相同。会话头/尾的 comment_guard 键由 Pipeline 并进 session_start /
    # session_end，不在这里。写入只用守卫在 bind 时拿到的本场自己的 audit，晚到的复核结果换场后不写。

    def comment_guard_review(self, backend, model, n, ms, outcome, refusal_category=None,
                             backlog_sec=None, asr_inflight=None, waited_ms=None, in_tokens=None,
                             out_tokens=None, cache_read_tokens=None):
        """一次模型复核调用（PR-5 起写）。outcome：ok / timeout / http_4xx / http_5xx / refusal /
        max_tokens / invalid / cancelled。每分钟至多 10 次由调用方控制。"""
        self._write({"type": "comment_guard_review", "at": _now_ms(), "backend": backend,
                     "model": model, "n": n, "ms": ms, "outcome": outcome,
                     "refusal_category": refusal_category, "backlog_sec": backlog_sec,
                     "asr_inflight": asr_inflight, "waited_ms": waited_ms, "in_tokens": in_tokens,
                     "out_tokens": out_tokens, "cache_read_tokens": cache_read_tokens})

    def comment_translation_failed(self, engine, model, batch, reasons, ms, retried=None,
                                   rescued=None, retried_reasons=None, rescued_reasons=None):
        """一批弹幕里有没译出来的（界面上那几条只显示原文）。打包运行时终端输出指向 /dev/null，
        2026-10-14 直播中一条弹幕没译出来，事后查不到是接口报错、冷却、拒答还是回了一句话。
        reasons：{原因代码: 条数}，代码见 app/comments.py 的 implausible_reason / why_no_output，
        外加 timeout / exception / no_engine。弹幕原文和模型的回话都不进审计（弹幕与审计隔离）。

        retried / rescued：这一批里有弹幕被判成回话（reply_*）后原样重发过一次，重发了几条、
        其中几条译出来了（译出来的不在 reasons 里）。retried_reasons / rescued_reasons 是这两个数
        按**第一次**被判成什么（reply_*）拆开的 {代码: 条数}：被救回的不在 reasons 里，不拆开就算不出
        各代码的救回率。只在重发过（retried > 0）时才写这四个键，没重发的批次保持原来的 5 个键。
        reasons 里记的也是第一次被判成什么。"""
        row = {"type": "comment_translation_failed", "at": _now_ms(), "engine": engine,
               "model": model, "batch": batch, "reasons": dict(reasons), "ms": ms}
        if retried:
            row["retried"], row["rescued"] = retried, rescued or 0
            row["retried_reasons"] = dict(retried_reasons or {})
            row["rescued_reasons"] = dict(rescued_reasons or {})
        self._write(row)

    def comment_guard_gate(self, backend, state, reason=None, backlog_sec=None):
        """复核的闸门或状态转折（PR-5 起写）。state：paused / resumed / tripped / reenabled / locked。"""
        self._write({"type": "comment_guard_gate", "at": _now_ms(), "backend": backend,
                     "state": state, "reason": reason, "backlog_sec": backlog_sec})

    # ---- 拉流这一侧的观察：断在哪、断了多久、程序有没有在跑、为什么结束 ----
    # 以前这些只打在终端上，而打包运行时 stdout 指向 /dev/null：事后对着一段没有字幕的
    # 时间，分不清是主播没说话、流断了、网断了，还是电脑睡着了。所有错误文本里的 URL
    # 先去掉 query（签名地址两周内拿着就能拉流）。

    def stream_break(self, reason, returncode=None, audio_sec=0.0, got_audio=False,
                     reconnect_no=0, stderr_tail="", error=None, **extra):
        """一轮拉流结束。reason：eof（读到流尾）/ stall（20 秒没有任何字节，看门狗断开）/
        clock_gap（时钟对账发现电脑休眠过，程序主动断开重连）/ cancelled（停止或换场）/
        error（这一轮内部出错）。returncode 是 ffmpeg 收尾后的退出码，原样记。

        extra 是附带的数字和代码，放在骨架字段后面（撞名时骨架字段赢）：break_no（本场第几次）、
        收尾的事实 stop_path / stop_ms / tail_bytes / tail_sec（app/audio.py stop_facts）等。"""
        rec = {
            "type": "stream_break",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "reason": reason,
            "returncode": returncode,
            "audio_sec": round(float(audio_sec or 0.0), 1),
            "got_audio": bool(got_audio),
            "reconnect_no": reconnect_no,
            "stderr_tail": strip_query(stderr_tail)[-300:],
        }
        if error:
            rec["error"] = strip_query(error, 200)
        for key, value in extra.items():
            rec.setdefault(key, value)
        self._write(rec)

    def stream_resumed(self, deaf_sec, reconnect_no=None, **extra):
        """断流之后重新收到第一帧音频：中间有多少秒什么都没听到。extra：no_audio_notice（这之前
        「已 N 分钟没收到直播音频」挂着）。"""
        rec = {
            "type": "stream_resumed",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "deaf_sec": round(max(0.0, float(deaf_sec)), 1),
            "reconnect_no": reconnect_no,
        }
        for key, value in extra.items():
            rec.setdefault(key, value)
        self._write(rec)

    def stage_overrun(self, stage, sec, budget_sec, break_no=0, **facts):
        """拉流某个阶段超过了预算（每进一次阶段最多一条）。stage：lookup / spawn / first_frame /
        stopping / drain / backoff / network_wait / offline_check / other ……；facts 只记事实（pid、
        returncode、stdout_paused、pipes_open、self_heal 做了什么），不写原因。"""
        rec = {
            "type": "stage_overrun",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "stage": str(stage),
            "sec": round(float(sec), 1),
            "budget_sec": round(float(budget_sec), 1),
            "break_no": break_no,
        }
        for key, value in facts.items():
            rec.setdefault(key, value)
        self._write(rec)

    def reconnect_plan(self, break_no, delay_sec, mode, silent_streak=None, lookup_tokens=None,
                       policy=None):
        """每次重连之前一条：本场第几次中断之后、等几秒、怎么连（lookup 重新查流地址 / reuse 用原来的
        流地址 / reuse_capped、reuse_failed_capped 想查但到了上限）、连续无声几轮、桶里还剩几个令牌。"""
        self._write({
            "type": "reconnect_plan",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "break_no": break_no,
            "delay_sec": delay_sec,
            "mode": str(mode),
            "silent_streak": silent_streak,
            "lookup_tokens": lookup_tokens,
            "policy": policy,
        })

    def lookup_capped(self, tokens, refill_sec, next_in_sec, per_hour):
        """查流地址的次数到了程序自己定的上限（每一段一条）：桶里剩几个、多久补一个、多久后能再查、
        任意一小时最多几次。这是程序的规矩，不是 TikTok 的回应。"""
        self._write({
            "type": "lookup_capped",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "tokens": tokens,
            "refill_sec": round(float(refill_sec), 1),
            "next_in_sec": round(float(next_in_sec or 0.0), 1),
            "per_hour": per_hour,
        })

    def coverage(self, state, ratio, window_sec, audio_sec, breaks_in_window):
        """跨轮覆盖率跨过阈值（低于报警线连续两次 → low，回到撤销线以上 → ok）时一条：窗口多长、
        里面收到多少秒直播音频、断了几次。"""
        self._write({
            "type": "coverage",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "state": str(state),
            "ratio": round(float(ratio), 3),
            "window_sec": round(float(window_sec), 1),
            "audio_sec": round(float(audio_sec), 1),
            "breaks_in_window": int(breaks_in_window),
        })

    def stream_summary(self, **fields):
        """每 5 分钟一条拉流汇总的快照（和 session_end 里的那些字段一样，只有数字）：⌘Q、关窗、Ctrl-C
        都不写 session_end，场末没写也留得下数。"""
        rec = {"type": "stream_summary", "at": datetime.now().isoformat(timespec="milliseconds")}
        for key, value in fields.items():
            rec.setdefault(key, value)
        self._write(rec)

    def no_audio(self, sec, wall_sec, stage, break_no=0):
        """「已 N 分钟没收到直播音频」这一次跨过了门限（每一段无声一条）。sec 按单调时钟、wall_sec
        按墙钟（电脑睡着的时间只算在后者里），stage 是此刻所在的阶段。"""
        self._write({
            "type": "no_audio",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "sec": round(float(sec), 1),
            "wall_sec": round(float(wall_sec or 0.0), 1),
            "stage": str(stage),
            "break_no": break_no,
        })

    def audio_heartbeat(self, audio_sec, wall_sec, speech_frames, segments):
        """每 60 秒一条：这段时间实际收到多少秒音频（wall_sec 是这个窗口经过的秒数，
        按单调时钟算，电脑睡着的时间不计）、多少帧超过识别门限、切出几段。
        用来分开「主播没说话」「音频没到」「程序没在跑」三种空白。"""
        self._write({
            "type": "audio_heartbeat",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "audio_sec": round(float(audio_sec), 1),
            "wall_sec": round(float(wall_sec), 1),
            "speech_frames": int(speech_frames),
            "segments": int(segments),
        })

    def stream_audio(self, audio_sec, segments_cut, peak_rms, final=False):
        """一轮拉流的累计：收到的音频秒数、切出送去识别的段数、帧 RMS 峰值。
        每 5 分钟一条、每轮结束一条（final=True）。"""
        self._write({
            "type": "stream_audio",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "audio_sec": round(float(audio_sec), 1),
            "segments_cut": int(segments_cut),
            "peak_rms": round(float(peak_rms), 1),
            "final": bool(final),
        })

    def clock_gap(self, from_ts, to_ts, gap_sec, wall_sec, mono_sec, clocks_diverged):
        """统计循环发现墙钟比预期多走了一大截：这段时间程序没有运行。
        clocks_diverged=True 表示墙钟走了、单调时钟没走（macOS/Linux 上电脑休眠或挂起时
        就是这样）；False 表示两个钟一起走多了（Windows 休眠、或事件循环被卡住）。
        时钟被往前拨也会触发，所以只叫 clock_gap，不叫「休眠」。"""
        self._write({
            "type": "clock_gap",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "from": _iso_from_epoch(from_ts),
            "to": _iso_from_epoch(to_ts),
            "gap_sec": round(float(gap_sec), 1),
            "wall_sec": round(float(wall_sec), 1),
            "mono_sec": round(float(mono_sec), 1),
            "clocks_diverged": bool(clocks_diverged),
        })

    def stats_tick_error(self, exc_type, step):
        """统计循环的某一步出了没预料到的异常（这一场每一步的每种异常一条）。那一步跳过，同一跳的
        其余各步和下一跳照常，循环不因此退出。step 是出错那一步的英文代码（clock_gap、stream_watch、
        snapshot、coverage、broadcast、health、detection、audit_health、asr_memory、guard，见
        Pipeline._stats_loop）；只记异常的类型名，不记异常原文（可能带签名的流地址之类）。"""
        self._write({
            "type": "stats_tick_error",
            "at": _now_ms(),
            "step": str(step)[:40],
            "exc_type": str(exc_type)[:80],
        })

    def network_down(self, since_ts, why=""):
        """重连前的连通性探测（DNS + TCP 到 www.tiktok.com:443）失败。why 只有异常类名。"""
        self._write({
            "type": "network_down",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "since": _iso_from_epoch(since_ts),
            "why": strip_query(why, 120),
        })

    def network_up(self, down_sec):
        self._write({
            "type": "network_up",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "down_sec": round(float(down_sec), 1),
        })

    def host_wait(self, status, waited_sec, outcome, trigger="status", why=None):
        """重连时接口说房间不在播，但不是「已结束」（状态 4）：程序在等、每分钟问一次。
        outcome：started / live / ended / timeout；trigger=clock_gap 是电脑休眠过之后
        对「已结束」判定做的那次复查。status 是房间接口原样返回的值。"""
        rec = {
            "type": "host_wait",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "status": status,
            "waited_sec": int(round(float(waited_sec))),
            "outcome": outcome,
            "trigger": trigger,
        }
        if why:
            rec["why"] = strip_query(why, 120)
        self._write(rec)

    def internal_error(self, exc, tb=""):
        """直播任务里没预料到的异常。以前只有终端里的堆栈（打包运行时没人看得到），
        界面上那句「内部错误，已停止」刷新就没了。堆栈只留最后 2000 个字符。"""
        self._write({
            "type": "internal_error",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "exc_type": type(exc).__name__,
            "message": strip_query(str(exc), 300),
            "traceback": strip_query(tb)[-2000:],
        })

    def update_stop(self, from_version, to_version):
        """一键更新暂停了这场监听。session_end 说「为什么停」，这条说「从哪个版本更到
        哪个版本」；下一场 session_start 的 resumed_after 说「什么时候接上的」。以前
        审计里只剩一条光秃秃的 session_end，事后分不清是中控点了停止还是更新停的。"""
        self._write({
            "type": "update_stop",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "from_version": from_version,
            "to_version": to_version,
        })

    def component_updated(self, name, from_version, to_version, reason):
        """会话进行中后台升级了解析组件（yt-dlp / curl_cffi）。解析行为变了的时候，
        事后要答得出「升没升、升之前之后各是哪个版本」。"""
        self._write({
            "type": "component_updated",
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "name": name,
            "from": from_version,
            "to": to_version,
            "reason": reason,
        })

    def alert(self, hit):
        self._write({"type": "alert",
                     "at": datetime.now().isoformat(timespec="milliseconds"),
                     **hit})

    def ui_client_dropped(self, reason, buffered_bytes=None, clients_left=None):
        """一个界面页面收不下消息，被服务端断开（页面会自己重连并补回报警）。
        留痕是为了事后答得出「那段时间屏幕上的报警为什么晚到」。"""
        self._write({"type": "ui_client_dropped", "at": _now_ms(), "reason": reason,
                     "buffered_bytes": buffered_bytes, "clients_left": clients_left})

    def viewer_share(self, state, port=None, viewers=None, reason=None):
        """手机同看被打开/关闭/换了链接。state: "on"|"off"|"rotate"；
        reason: "operator"|"boot_restore"|"port_busy"|"shutdown"。

        为什么要留痕：打开同看意味着这台机器在局域网上多了一个监听面，事后要答得出
        「那段时间字幕和报警都被哪些人看得到」。**token 绝不进审计**——审计文件是
        给人看的证据，不是发链接的地方。"""
        self._write({"type": "viewer_share", "at": _now_ms(), "state": state,
                     "port": port, "viewers": viewers, "reason": reason})

    def viewer_connected(self, ip, count):
        """一台手机接进来了。只记 IP 与人数：不知道也不记录「谁在看」。"""
        self._write({"type": "viewer_connected", "at": _now_ms(), "ip": ip,
                     "count": count})

    def viewer_disconnected(self, ip, count, reason):
        """reason: closed|queue_full|send_timeout|inbound_flood|kicked|cap。
        前几种是服务端主动断的——被断的手机会重连并拿到完整回放，所以这不是丢数据，
        但要能查出「那台手机当时为什么在重连」。"""
        self._write({"type": "viewer_disconnected", "at": _now_ms(), "ip": ip,
                     "count": count, "reason": reason})

    def viewer_auth_failed(self, ip, why, suppressed=0):
        """鉴权失败。why 只取 missing|bad_type|bad_token|not_json|timeout——
        记的是「第一条消息哪里不对」，不是对 token 的任何回显。
        限流在 ViewerHub 里做（同一 IP 每 10 秒最多一条），suppressed 是这条之前
        被压掉的次数：没有它，一次扫端口就能把审计文件刷成几千行同样的记录。"""
        self._write({"type": "viewer_auth_failed", "at": _now_ms(), "ip": ip,
                     "why": why, "suppressed": suppressed})

    def viewer_action(self, action, seq, ip):
        """手机端发起的动作（目前只有 retranslate）。translation_strong 的
        trigger="viewer" 说得出「是手机按的」，答不出「是哪一台」；这一条单独
        记 ip，事后要能答「那次重译是谁点的」。"""
        self._write({"type": "viewer_action", "at": _now_ms(), "action": action,
                     "id": seq, "ip": ip})

    def window_closed(self):
        """中控关掉了程序窗口（正在监听时要先确认），监听随之停止。先于停止流程写下：
        收尾要等识别线程和弹幕子进程，进程可能等不到 session_end 就退出。"""
        self._write({"type": "window_closed", "at": _now_ms()})

    # 这一场的汇总：返回 dict 的可调用对象，由开这一场的管线挂上（Pipeline._run_stream_inner），闭包里只绑
    # 这一场自己的会话状态。close(reason=…) 写 session_end 之前调一次、并进去，所以不管是谁来关这份审计——
    # 这一场自己收尾、_stop_locked 替过了宽限期的旧任务代写、关窗口的兜底（window_close.close_session）——
    # session_end 都带着这一场的识别风险、证据和弹幕计数（10-19：代写的那条只有 reason）。
    # 只用一次、关的时候放掉（闭包里有证据的环形缓冲）；调用方显式传的字段优先；它出错不拦 session_end。
    end_fields = None

    def close(self, reason=None, **fields):
        """写 session_end 并关文件。reason：这一场为什么结束（offline / reconnect_exhausted /
        user_stop / internal_error …），fields 是这个原因附带的观察（如 silent、budget）。
        骨架字段优先：fields 里撞名的键盖不掉 type/ended_at/reason；end_fields 补的盖不掉 fields。"""
        provider, self.end_fields = self.end_fields, None
        if self._fh is not None:
            if reason and provider is not None:
                try:
                    for key, value in (provider() or {}).items():
                        fields.setdefault(key, value)
                except Exception:
                    pass        # 汇总算不出来，session_end 照样写
            # 以前只接 (OSError, ValueError)：session_end 带上编不出 JSON 的字段时 close 抛 TypeError，
            # 文件不关、session_end 也不写（专家审查 B2）。现在 TypeError 也接住，文件一定在 finally 里关
            try:
                rec = {"type": "session_end",
                       "ended_at": datetime.now().isoformat(timespec="seconds")}
                if reason:
                    rec["reason"] = reason
                    for key, value in fields.items():
                        rec.setdefault(key, value)
                if _encode(rec) is None:
                    # 先在这里换掉编不出来的字段：下面带的 serialize_failures 要把 session_end 自己算进去
                    rec = _sanitized(rec)
                    with self._lock:
                        self.serialize_failures += 1
                if self.serialize_failures:
                    # 只在真的有过编不出来的记录时才多这个键：正常的 session_end 和以前逐字节相同
                    rec.setdefault("serialize_failures", self.serialize_failures)
                self._write(rec)
            except (OSError, ValueError, TypeError):
                pass
            finally:
                with self._lock:
                    fh, self._fh = self._fh, None
                if fh is not None:
                    try:
                        fh.close()
                    except (OSError, ValueError):
                        pass
