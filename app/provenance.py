"""语料来源：让每一份数字都能回答「这是谁、哪个版本、哪份词表产生的」。

起因是一次真实污染：一个测试建审计日志时漏传目录，往生产的 logs/ 写了 106 个
会话文件、371 段。那个目录正是所有语料分析的输入，于是「hola」和两句癌症宣称
占了我口中真实字幕的两成，我还拿那些分母当过证据。

清理和 conftest 的防线都做了，但那只堵住这一次。真正的防线是**别再见文件就
信**：会话开头记下它是谁、跑的哪个版本、用的哪份词表，分析工具按这份信息挑
语料，而不是 glob 整个目录。

这样以后能准确回答：这个 14% 是哪几个主播、哪个 commit、哪份词表、哪个模型
产生的——而不会像这次一样，事后才发现里面混了测试数据，还混了第三个主播。
"""
import hashlib
import json
import re
import subprocess
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
# 测试夹具用的地址：单字母句柄和 example.com。真实主播名不会长这样。
_FIXTURE = re.compile(r"tiktok\.com/@[a-z]/live|example\.com|/room/", re.I)


def code_commit():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=5,
                              cwd=str(LOG_DIR.parent)).stdout.strip() or "?"
    except Exception:
        return "?"


def file_hash(path):
    """词表/规则文件的指纹。同一个数字来自哪份词表，靠它对上。"""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]
    except OSError:
        return "?"


def app_version():
    try:
        return (LOG_DIR.parent / "VERSION").read_text(encoding="utf-8").strip() or "?"
    except OSError:
        return "?"


def streamer_of(url):
    m = re.search(r"@([\w.]+)", url or "")
    return m.group(1) if m else ""


def session_meta(path):
    """读一个会话的来源信息；不是真实直播就返回 None。"""
    url, meta, segs = "", {}, 0
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") == "session_start":
                url = url or d.get("room_url", "")
                meta = {k: v for k, v in d.items()
                        if k in ("code_commit", "glossary_hash", "vocative_hash",
                                 "translator", "started_at", "app_version",
                                 "source_requested", "source_active",
                                 "translator_requested", "translator_active",
                                 "profile", "profile_hash",
                                 "merged_glossary_hash")} or meta
            elif d.get("type") == "segment" and (d.get("text") or "").strip():
                segs += 1
    except OSError:
        return None
    if not url or _FIXTURE.search(url):
        return None
    return dict(meta, path=str(path), room_url=url,
                streamer=streamer_of(url), segments=segs)


HOLDOUT_FILE = LOG_DIR.parent / "eval_holdout.json"


def eval_holdout(path=None):
    """评估 holdout 冻结清单：这些 session / 主播永远不进训练侧管线。

    读不出来时返回空集**并打印警告**——holdout 静默失效比没有 holdout 更糟，
    因为所有人都以为它还在。"""
    p = Path(path) if path else HOLDOUT_FILE
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {"sessions": set(data.get("sessions", {})),
                "streamers": set(data.get("streamers", {}))}
    except (OSError, ValueError) as exc:
        print("[警告] eval_holdout.json 读取失败（{}）——holdout 未生效！".format(exc))
        return {"sessions": set(), "streamers": set()}


def corpus(log_dir=None, streamer=None):
    """真实直播会话的清单。**分析工具应当用它，而不是 glob 整个目录。**"""
    out = []
    for f in sorted(Path(log_dir or LOG_DIR).glob("session-*.jsonl")):
        m = session_meta(f)
        if m and m["segments"] and (streamer is None or m["streamer"] == streamer):
            out.append(m)
    return out
