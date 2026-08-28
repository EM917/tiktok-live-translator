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


def eval_holdout(path=None, strict=False):
    """评估冻结清单（dev_eval + sealed_test 两层），返回合并后的排除集。

    dev_eval 是训练排除的开发基准（调参可看）；sealed_test 是密封终审考卷
    （评级前冻结、定稿前不开）。两层对训练侧的效力相同：都绝不进训练。

    strict=True 给训练侧工具（mine / annotate / export / train）用：清单
    缺失、JSON 损坏、schema 不对时**直接退出**——今天生成不了队列，好过
    悄悄污染一次训练集。strict=False 只用于纯分析场景：警告 + 空集。"""
    p = Path(path) if path else HOLDOUT_FILE

    def fail(why):
        msg = "eval_holdout.json {}——训练侧管线拒绝在没有冻结清单的情况下运行".format(why)
        if strict:
            raise SystemExit("[错误] " + msg)
        print("[警告] " + msg + "（非训练侧：按空清单继续）")
        return {"sessions": set(), "streamers": set()}

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return fail("读取失败（{}）".format(exc))
    if not isinstance(data.get("dev_eval"), dict) \
            or not isinstance(data.get("sealed_test"), dict):
        return fail("schema 不合法（缺 dev_eval / sealed_test 两层）")
    sessions, streamers = set(), set()
    for tier in ("dev_eval", "sealed_test"):
        block = data[tier]
        # 第二层也要验：手改成 "sessions": "abc" 时 set("abc") 会悄悄变成
        # {'a','b','c'}——比报错糟糕得多
        for key in ("sessions", "streamers"):
            if not isinstance(block.get(key, {}), dict):
                return fail("schema 不合法（{}.{} 必须是对象）".format(tier, key))
        sessions |= set(block.get("sessions", {}))
        streamers |= set(block.get("streamers", {}))
    return {"sessions": sessions, "streamers": streamers}


def corpus(log_dir=None, streamer=None):
    """真实直播会话的清单。**分析工具应当用它，而不是 glob 整个目录。**"""
    out = []
    for f in sorted(Path(log_dir or LOG_DIR).glob("session-*.jsonl")):
        m = session_meta(f)
        if m and m["segments"] and (streamer is None or m["streamer"] == streamer):
            out.append(m)
    return out
