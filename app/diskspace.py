"""磁盘占用盘点与可选删除——只认自己知道的东西，只删白名单里的条目。

程序往硬盘放的东西散在三处：HuggingFace 缓存里的语音模型、Ollama 里的翻译模型、
项目目录下的审计日志。README 写的是「一次完整安装约 6 GB」，而 2026-09-08 实测一台
用了三周的机器：HF 缓存 27 GB、Ollama 26 GB——里面一半是引擎对比时拉的、本程序
根本不用的模型。用户看不到这些，也没有地方删。

规则：
- 条目由这里盘点并编 id；删除只按 id 回查，**永远不接受路径**。
- 每条标明角色：in_use（本场正在用）/ app（本程序会用，删了下次自动重下）/
  other（不是本程序下载的，可能是别的工具的）。删 other 由用户自己判断。
- 运行环境 .venv 不列：删了程序就没了。
- 当前会话的审计文件不列、不删。
"""
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS_OLD_DAYS = 30

# 本程序会用到的模型：按名字片段识别
APP_HF_MARKERS = ("whisper",)
APP_OLLAMA_MARKERS = ("hy-mt2", "translategemma")


def hf_hub_dir():
    hf_home = os.environ.get("HF_HOME")
    return Path(hf_home) / "hub" if hf_home else Path.home() / ".cache" / "huggingface" / "hub"


def dir_size(path):
    """不跟随符号链接：HF 缓存的 snapshots/ 是指向 blobs/ 的软链，跟着算会记两遍。"""
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                full = os.path.join(root, name)
                if os.path.islink(full):        # 软链本身也有几十字节，不算
                    continue
                try:
                    total += os.lstat(full).st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _hf_label(dirname):
    # models--Systran--faster-whisper-large-v3 -> Systran/faster-whisper-large-v3
    return dirname[len("models--"):].replace("--", "/", 1)


def _role(name, markers, active):
    low = name.lower()
    if active and active.lower() in low:
        return "in_use"
    return "app" if any(m in low for m in markers) else "other"


def _asr_in_use(dirname, active_asr):
    """active_asr = (backend, model)，如 ("mlx", "large-v3")。同一个 large-v3 在缓存里
    可能有 ct2 版、mlx 版、turbo 版三个目录，只有真正加载的那个算「正在用」：
    mlx 后端认带 -mlx 后缀的仓库，ct2 认不带的；模型名按结尾整段匹配，
    large-v3 不会误吃 large-v3-turbo。"""
    if not active_asr:
        return False
    backend, model = active_asr
    repo = _hf_label(dirname).lower()
    if backend == "mlx":
        return repo.endswith(model.lower() + "-mlx")
    return "mlx" not in repo and repo.endswith(model.lower())


def inventory(hf_dir=None, ollama_models=(), log_dir=None, active_asr=None,
              active_ollama=None, current_log=None, now=None):
    """盘点。ollama_models 由调用方从 /api/tags 取好传进来（这里不联网）。
    active_asr 是 (backend, model)，active_ollama 是 Ollama 模型名。

    返回 [{"id", "kind", "label", "size", "role", "note"}...]，size 单位字节。"""
    items = []
    hub = Path(hf_dir) if hf_dir else hf_hub_dir()
    try:
        entries = sorted(p for p in hub.iterdir() if p.is_dir() and p.name.startswith("models--"))
    except OSError:
        entries = []
    for p in entries:
        role = "in_use" if _asr_in_use(p.name, active_asr) else _role(p.name, APP_HF_MARKERS, None)
        items.append({"id": "hf:" + p.name, "kind": "hf", "label": _hf_label(p.name),
                      "size": dir_size(p), "role": role,
                      "note": {"in_use": "本场正在用的语音模型",
                               "app": "语音模型，删了下次识别时自动重新下载",
                               "other": "不是本程序下载的，可能是别的工具在用"}[role]})
    for m in ollama_models:
        name = str(m.get("name") or "")
        if not name:
            continue
        role = _role(name, APP_OLLAMA_MARKERS, active_ollama)
        items.append({"id": "ollama:" + name, "kind": "ollama", "label": name,
                      "size": int(m.get("size") or 0), "role": role,
                      "note": {"in_use": "当前翻译引擎正在用",
                               "app": "翻译模型，删了下次选用时自动重新拉取",
                               "other": "不是本程序使用的模型（引擎对比或别的工具留下的）"}[role]})
    logs = _log_files(log_dir, current_log)
    if logs:
        cutoff = (now or datetime.now()) - timedelta(days=LOGS_OLD_DAYS)
        old = [f for f in logs if _log_time(f) is not None and _log_time(f) < cutoff]
        if old:
            items.append({"id": "logs:old", "kind": "logs",
                          "label": "早于 {} 天的会话审计日志（{} 个文件）".format(LOGS_OLD_DAYS, len(old)),
                          "size": sum(_fsize(f) for f in old), "role": "app",
                          "note": "复核过的会话可以删；违禁词证据在里面，删前确认已经不需要"})
        items.append({"id": "logs:all", "kind": "logs",
                      "label": "全部会话审计日志（{} 个文件，不含本场）".format(len(logs)),
                      "size": sum(_fsize(f) for f in logs), "role": "app",
                      "note": "违禁词证据在里面，删前确认已经不需要"})
    return items


def _log_files(log_dir, current_log):
    d = Path(log_dir) if log_dir else ROOT / "logs"
    cur = str(Path(current_log).resolve()) if current_log else None
    try:
        return sorted(f for f in d.glob("session-*.jsonl") if str(f.resolve()) != cur)
    except OSError:
        return []


def _log_time(path):
    # session-20260905-171803.jsonl -> 2026-09-05 17:18:03
    try:
        return datetime.strptime(path.stem[len("session-"):], "%Y%m%d-%H%M%S")
    except ValueError:
        return None


def _fsize(path):
    try:
        return path.stat().st_size
    except OSError:
        return 0


async def delete(item_ids, hf_dir=None, log_dir=None, current_log=None,
                 ollama_models=(), ollama_delete=None, now=None):
    """按 id 删除。id 先回查盘点结果，查不到的一律忽略——不存在「按路径删」这条路。

    ollama_delete: async fn(name) -> bool，由调用方提供（这里不联网）。
    返回 (释放的字节数, [删掉的 id], [失败说明])。"""
    known = {it["id"]: it for it in inventory(hf_dir, ollama_models, log_dir,
                                              current_log=current_log, now=now)}
    hub = Path(hf_dir) if hf_dir else hf_hub_dir()
    freed, done, failed = 0, [], []
    for item_id in item_ids:
        it = known.get(str(item_id))
        if it is None:
            failed.append("{}：不在盘点清单里，跳过".format(item_id))
            continue
        try:
            if it["kind"] == "hf":
                target = (hub / it["id"][len("hf:"):]).resolve()
                # 双保险：必须仍在缓存目录之内、且是 models-- 开头的目录
                if hub.resolve() not in target.parents or not target.name.startswith("models--"):
                    failed.append("{}：路径不在缓存目录内，拒绝".format(it["label"]))
                    continue
                shutil.rmtree(target)
            elif it["kind"] == "ollama":
                if ollama_delete is None or not await ollama_delete(it["label"]):
                    failed.append("{}：Ollama 没有删除成功".format(it["label"]))
                    continue
            elif it["kind"] == "logs":
                files = _log_files(log_dir, current_log)
                if it["id"] == "logs:old":
                    cutoff = (now or datetime.now()) - timedelta(days=LOGS_OLD_DAYS)
                    files = [f for f in files if _log_time(f) is not None and _log_time(f) < cutoff]
                for f in files:
                    f.unlink()
            freed += it["size"]
            done.append(it["id"])
        except OSError as exc:
            failed.append("{}：{}".format(it["label"], exc))
    return freed, done, failed


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "{:.1f} {}".format(n, unit) if unit != "B" else "{} B".format(n)
        n /= 1024.0
    return "{:.1f} GB".format(n)
