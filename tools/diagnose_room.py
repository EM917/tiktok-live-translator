"""直播间拿不到流地址时的诊断——先看自己的日志，再做同分钟配对，**不猜原因**。

2026-09-05/06 的教训：同一个 4003110 一天之内被先后解释成「年龄限制」「IP 限流」
「被我们的探测打坏」，三个都错。每次都是拿单个观察往外推，没有对照组，也没先看
程序自己的日志——而日志五分钟就能证明那个房间从来没成功过。这个工具把正确的
顺序写死：

  第 0 步（零请求）  按主播聚合 logs/session-*.jsonl：这个房间在本机成功过吗？
                      「之前能用」指的是哪个房间？最近一场的解析记录说了什么？
  第 1 步（配对）    目标房间 + 一个当时能用的对照房间，**同一分钟、同一接口**
                      （和程序解析用的完全同一条链路），各 2 次请求，共 4 次。
  结论               按写死的规则给出，只描述观察到什么、能做什么。

绝不开浏览器；每次运行最多 4 次请求；pytest 里禁止联网。

用法：
    python3 tools/diagnose_room.py @itzesantana11
    python3 tools/diagnose_room.py @itzesantana11 --control bellaallnatural
    python3 tools/diagnose_room.py @itzesantana11 --history-only      # 零请求
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, ".")

from app.provenance import streamer_of                        # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
WITHHELD_CODE = 4003110
LIVE_STATUS = 2
MAX_REQUESTS = 4


# ---------------------------------------------------------------- 第 0 步：日志
def history_by_streamer(log_dir=None):
    """按主播聚合会话：场次、总段数、成功场次、最近一场（时间/段数/解析记录）。

    不用 provenance.corpus()——它会把 0 段的会话过滤掉，而 0 段正是这里
    最要看的东西。"""
    rows = {}
    for f in sorted(Path(log_dir or LOG_DIR).glob("session-*.jsonl")):
        streamer, segs, started, resolves = None, 0, "", []
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t = d.get("type")
            if t == "session_start":
                streamer = d.get("streamer") or streamer_of(d.get("room_url", ""))
                started = d.get("started_at", "")
            elif t == "segment" and (d.get("text") or "").strip():
                segs += 1
            elif t == "resolve":
                resolves.append(d)
        if not streamer:
            continue
        r = rows.setdefault(streamer, {"sessions": 0, "segments": 0, "ok_sessions": 0,
                                       "last_started": "", "last_segments": 0,
                                       "last_resolve": []})
        r["sessions"] += 1
        r["segments"] += segs
        r["ok_sessions"] += 1 if segs else 0
        if started >= r["last_started"]:
            r["last_started"], r["last_segments"], r["last_resolve"] = started, segs, resolves
    return rows


def pick_control(history, exclude):
    """对照房间：历史上出过字幕、且**最近一场也成功**的主播里，最近的那个。"""
    cands = [(v["last_started"], k) for k, v in history.items()
             if k != exclude and v["ok_sessions"] > 0 and v["last_segments"] > 0]
    return max(cands)[1] if cands else None


# ---------------------------------------------------------------- 第 1 步：配对
def classify(room_status, info):
    """(房间状态接口的 status, 房间信息接口的返回) → (观察类别, 说明)。

    类别：withheld（接口不给流地址）/ ok / offline / error。只描述看到的。"""
    if room_status is not None and room_status != LIVE_STATUS:
        return "offline", "房间状态接口 status={}".format(room_status)
    if not isinstance(info, dict):
        return "error", "房间信息接口没有返回 JSON"
    code = info.get("status_code")
    data = info.get("data") if isinstance(info.get("data"), dict) else {}
    if code == WITHHELD_CODE or (data and set(data) == {"prompts"}):
        return "withheld", "status_code {}，data 只有 {} 个字段".format(code, len(data))
    if code not in (0, None):
        return "error", "status_code {}".format(code)
    status = data.get("status")
    if status is not None and status != LIVE_STATUS:
        return "offline", "房间信息 status={}".format(status)
    su = data.get("stream_url") or {}
    has = bool(su.get("flv_pull_url") or su.get("hls_pull_url")
               or ((su.get("live_core_sdk_data") or {}).get("pull_data") or {}).get("stream_data"))
    if has:
        return "ok", "{} 个字段，含流地址".format(len(data))
    return "error", "{} 个字段，无流地址".format(len(data))


def verdict(target, control):
    """配对结论。规则写死，不给人（或 agent）发挥的余地。"""
    t = target[0]
    c = control[0] if control else None
    if c is None:
        return ("只有目标房间一个观察，**不能下任何结论**。"
                "找一个当时能用的房间做对照（--control）再说。")
    if c == "error":
        return "对照房间的观察本身失败了（{}），这次配对无效，换一个对照重来。".format(control[1])
    if t == "withheld" and c == "ok":
        return ("房间维度的拒绝：同一分钟对照房间正常，只有目标房间被拒。"
                "原因 TikTok 不说明——**不要贴年龄/限流/封禁之类的标签**。"
                "能做的：过一会儿再点「开始翻译」；或把直播间链接和浏览器里的 .flv 地址"
                "并排粘进程序（约两周有效）。")
    if t == "withheld" and c in ("withheld", "offline"):
        return ("对照房间也没拿到（{}）。先怀疑本机这边：网络、接口变更、本机被挡——"
                "**别怪目标房间**。换一个确定在播的对照再验一次。").format(control[1])
    if t == "ok":
        return ("目标房间现在能拿到流地址。如果程序仍然失败，问题在解析之后"
                "（探活、ffmpeg、模型加载）——看会话日志里的 resolve 记录。")
    if t == "offline":
        return "接口明确说目标房间没在播（{}）。这是唯一可以断言「没开播」的证据。".format(target[1])
    return "目标观察失败（{}），无法判断；先看网络。".format(target[1])


async def probe(user):
    """走和程序解析**完全同一条**接口链路：用户名 → 房间状态 → 房间信息。2 次请求。"""
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("TLT_NO_NETWORK"):
        raise RuntimeError("测试环境禁止联网")
    import aiohttp

    from app.resolver import _WEBCAST_API, _get_json, _room_status

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        room, status = await _room_status(session, user)
        if room is None:
            return None, None
        info = await _get_json(session, _WEBCAST_API.format(room=room))
        return status, info


# ---------------------------------------------------------------- 输出
def _print_history(history, target, log_dir=None):
    print("== 第 0 步：本机日志按主播聚合（零请求）==")
    # 读的是哪个目录要说出来：从 worktree 里跑这个脚本，默认读到的是 worktree
    # 自己的 logs/（只有测试残留），会得出完全错误的历史——2026-09-07 实录
    print("  目录：{}".format(Path(log_dir or LOG_DIR).resolve()))
    if not history:
        print("  logs/ 里没有会话记录。")
        return
    print("  {:<24}{:>5}{:>7}{:>8}  {}".format("主播", "场次", "成功", "总段数", "最近一场"))
    for k, v in sorted(history.items(), key=lambda kv: -kv[1]["segments"]):
        mark = " ◀ 目标" if k == target else ""
        print("  {:<24}{:>5}{:>7}{:>8}  {} ({} 段){}".format(
            k[:22], v["sessions"], v["ok_sessions"], v["segments"],
            v["last_started"] or "-", v["last_segments"], mark))
    t = history.get(target)
    if t is None:
        print("  目标房间 @{} 在本机日志里从未出现过。".format(target))
    elif t["ok_sessions"] == 0:
        print("  目标房间 @{} 在本机 {} 场里**一次字幕都没出过**——「之前能用」说的不是它。".format(
            target, t["sessions"]))
    else:
        print("  目标房间 @{} 成功过 {}/{} 场，最近一场 {}。".format(
            target, t["ok_sessions"], t["sessions"], t["last_started"]))
    if t and t["last_resolve"]:
        print("  最近一场的解析记录：")
        for r in t["last_resolve"]:
            walked = " · ".join("{}→{}".format(x.get("layer"), x.get("outcome"))
                                for x in r.get("layers", []))
            print("    第{}/{}次 {} {:.1f}s {}{}".format(
                r.get("attempt"), r.get("of"), "成功" if r.get("ok") else "失败",
                (r.get("ms") or 0) / 1000, ("kind=" + r["kind"] + " ") if r.get("kind") else "",
                walked))


async def _paired(target, control):
    print("\n== 第 1 步：同一分钟配对（目标 + 对照，各 2 次请求，共 {} 次）==".format(
        MAX_REQUESTS if control else MAX_REQUESTS // 2))
    tasks = [probe(target)] + ([probe(control)] if control else [])
    results = await asyncio.gather(*tasks, return_exceptions=True)

    def obs(name, res):
        if isinstance(res, Exception):
            return ("error", "请求失败：{}".format(res))
        return classify(*res)

    t_obs = obs(target, results[0])
    c_obs = obs(control, results[1]) if control else None
    print("  目标 @{:<20} {:<9} {}".format(target, t_obs[0], t_obs[1]))
    if control:
        print("  对照 @{:<20} {:<9} {}".format(control, c_obs[0], c_obs[1]))
    else:
        print("  对照：无（日志里找不到最近成功过的房间，可用 --control 指定）")
    print("\n== 结论 ==\n  " + verdict(t_obs, c_obs))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("room", help="@主播名 或直播间链接")
    ap.add_argument("--control", help="对照房间（默认从日志里挑最近成功过的）")
    ap.add_argument("--history-only", action="store_true", help="只看日志，不发请求")
    ap.add_argument("--logs", default=None, help="会话日志目录（默认 logs/）")
    a = ap.parse_args(argv)
    target = streamer_of(a.room) or a.room.lstrip("@").strip()
    history = history_by_streamer(a.logs)
    _print_history(history, target, a.logs)
    if a.history_only:
        return 0
    control = (a.control or "").lstrip("@").strip() or pick_control(history, target)
    asyncio.run(_paired(target, control))
    return 0


if __name__ == "__main__":
    sys.exit(main())
