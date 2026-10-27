"""时钟跳变那条持续提示（session:clock_gap）可以关、10 分钟没再跳就自己收起（2026-10-21 用户决定）。

起因：一场直播合盖 35 次，「本场第 35 次：…电脑休眠或挂起了约 29 秒…」那条黄条一直挂到下一场开始，
关不掉。现在：
  - 中控点 × → 页面发 dismiss_incident → 服务端撤掉（所有页面、手机同看一起收起，刷新也不再冒出来）；
  - CLOCK_GAP_CLEAR_SEC（600 秒）没有再跳 → 服务端自己撤；停止之后也照样计时；
  - 又跳一次 → 重新挂出，「本场第 N 次」接着数，重新计时；
  - 审计里的 clock_gap 记录一条不少，也不多写别的记录；
  - 只有这一条能关：别的提示（审计写不进去、网络断了……）发来 dismiss 也不理（CLAUDE.md 第六条）。
全部用桩件：不联网、不起 ffmpeg、不加载模型。
"""
import asyncio
import re
from pathlib import Path

from app import i18n
from app.audit import AuditLog
from app.pipeline import Pipeline
from tests.helpers import run
from tests.test_resilience_stream import ROOM, T0, make_pipeline, of_type, rows_of

KEY = "session:clock_gap"
REAL_SLEEP = asyncio.sleep          # make_pipeline 会把 asyncio.sleep 换成 sleep(0)，这里先留一份真的
ROOT = Path(__file__).resolve().parent.parent


def _gap_pipeline(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    p.audit = AuditLog(room_url=ROOM, log_dir=tmp_path / "logs")
    sess = p._session_state = p._new_session_state(p.audit)
    sess["clock"] = (T0, 500.0)
    return p, server, sess


async def _sleep_gap(p, minutes_later, mono):
    """macOS 合盖：墙钟走了很多，单调时钟只走了一跳。"""
    await p._check_clock_gap(10, now=(T0 + minutes_later * 60.0, mono))


def _shown(server):
    return (server.config.get("incidents") or {}).get(KEY)


def _clears(server):
    return [m for m in server.incidents(KEY) if m["level"] == "clear"]


def test_the_close_button_clears_the_banner_for_every_page_and_leaves_the_audit_alone(monkeypatch, tmp_path):
    p, server, sess = _gap_pipeline(monkeypatch, tmp_path)

    async def scenario():
        await _sleep_gap(p, 23, 510.0)
        before = rows_of(p.audit.path)
        ts = _shown(server)["ts"]
        # 页面发来的就是这样一条（web/app.js dismissIncident）；handle_control 交回协程，由服务端 await
        done = await p.handle_control({"type": "dismiss_incident", "id": KEY, "ts": ts})
        return before, done

    before, done = run(scenario())
    assert done is True
    assert _shown(server) is None                     # 服务端撤了：刷新、重连的页面也看不到
    assert len(_clears(server)) == 1                  # 广播一条 clear：所有本机页面和手机一起收起
    assert rows_of(p.audit.path) == before            # 审计一个字不多
    assert len(of_type(before, "clock_gap")) == 1
    assert p._clock_gap_timer is None                 # 关掉了，10 分钟的计时也作废


def test_a_new_gap_after_closing_shows_the_banner_again_with_the_next_count(monkeypatch, tmp_path):
    p, server, sess = _gap_pipeline(monkeypatch, tmp_path)

    async def scenario():
        await _sleep_gap(p, 23, 510.0)
        await p._dismiss_incident(KEY, _shown(server)["ts"])
        assert _shown(server) is None
        await _sleep_gap(p, 30, 520.0)

    run(scenario())
    text = _shown(server)["text"]
    assert text.startswith("本场第 2 次：") and "电脑休眠或挂起了约 7 分钟" in text
    assert len(of_type(rows_of(p.audit.path), "clock_gap")) == 2
    assert p._clock_gap_timer is not None             # 重新挂出，重新计时


def test_closing_what_the_page_saw_does_not_hide_a_newer_gap(monkeypatch, tmp_path):
    p, server, sess = _gap_pipeline(monkeypatch, tmp_path)

    async def scenario():
        await _sleep_gap(p, 23, 510.0)
        seen = _shown(server)["ts"]
        await REAL_SLEEP(0.05)                         # Windows 的 time.time() 一格约 16 毫秒：两次的 ts 要分得开
        await _sleep_gap(p, 30, 520.0)
        # 中控点 × 的时候，第二次跳变的广播还在路上：页面带的是第一次的 ts
        return seen, await p._dismiss_incident(KEY, seen)

    seen, done = run(scenario())
    assert done is False
    assert _shown(server)["ts"] != seen and _shown(server)["text"].startswith("本场第 2 次：")
    assert _clears(server) == []


def test_only_the_clock_gap_banner_can_be_closed(monkeypatch, tmp_path):
    p, server, sess = _gap_pipeline(monkeypatch, tmp_path)
    others = ("session:audit-write", "session:network", "session:no_audio", "selfcheck",
              "asr-fallback", "settings-corrupt", "engine")

    async def scenario():
        for key in others:
            await p._incident(key, "warn", "test notice")
        results = []
        for key in others:
            shown = server.config["incidents"][key]
            results.append(await p.handle_control({"type": "dismiss_incident", "id": key,
                                                   "ts": shown["ts"]}))
        # 没挂着的时候点（两个页面同时点了 ×，后到的那条）：什么也不做
        results.append(await p._dismiss_incident(KEY, None))
        results.append(await p._dismiss_incident(None, None))
        return results

    results = run(scenario())
    assert results == [False] * (len(others) + 2)
    assert set(others) <= set(server.config["incidents"])
    assert [m for m in server.messages if m.get("type") == "incident" and m["level"] == "clear"] == []
    assert Pipeline.DISMISSABLE_INCIDENTS == (KEY,)


def test_ten_quiet_minutes_clear_the_banner_and_a_new_gap_restarts_the_count_down(monkeypatch, tmp_path):
    p, server, sess = _gap_pipeline(monkeypatch, tmp_path)

    async def scenario():
        loop = asyncio.get_running_loop()
        await _sleep_gap(p, 23, 510.0)
        first, ts1 = p._clock_gap_timer, _shown(server)["ts"]
        assert abs(first.when() - loop.time() - 600.0) < 5.0
        await REAL_SLEEP(0.05)                       # 两次的 ts 要分得开（同上）
        await _sleep_gap(p, 30, 520.0)
        second, ts2 = p._clock_gap_timer, _shown(server)["ts"]
        assert first.cancelled() and not second.cancelled()       # 又跳一次：从这一次重新算 10 分钟
        assert abs(second.when() - loop.time() - 600.0) < 5.0 and ts2 != ts1
        # 第一次的计时器（万一已经排进了队列）到点也只认它自己那一条：收不掉第二次的
        assert await p._expire_clock_gap(ts1) is False
        assert _shown(server)["text"].startswith("本场第 2 次：")
        assert await p._expire_clock_gap(ts2) is True

    run(scenario())
    assert _shown(server) is None and len(_clears(server)) == 1
    assert len(of_type(rows_of(p.audit.path), "clock_gap")) == 2      # 审计照旧两条
    assert not [r for r in rows_of(p.audit.path) if "dismiss" in str(r.get("type"))]


def test_the_count_down_runs_on_the_event_loop_even_with_no_stats_loop(monkeypatch, tmp_path):
    """停止之后统计循环就停了，提示却还留着（它在「留着」那一类）：计时不能挂在统计循环上。"""
    p, server, sess = _gap_pipeline(monkeypatch, tmp_path)
    p.CLOCK_GAP_CLEAR_SEC = 0.01

    async def scenario():
        await _sleep_gap(p, 23, 510.0)
        p._stats_task = None                         # 统计循环没在跑（停止之后）
        await p._clear_ongoing_incidents()           # 停止时撤的那几条不含它
        assert _shown(server) is not None
        for _ in range(300):                         # 最多等 3 秒；正常几十毫秒内就收了
            if _shown(server) is None:
                break
            await REAL_SLEEP(0.01)

    run(scenario())
    assert _shown(server) is None and len(_clears(server)) == 1
    assert KEY not in Pipeline.ONGOING_INCIDENTS


def test_the_next_session_clears_it_and_the_old_count_down_does_nothing(monkeypatch, tmp_path):
    p, server, sess = _gap_pipeline(monkeypatch, tmp_path)

    async def scenario():
        await _sleep_gap(p, 23, 510.0)
        ts = _shown(server)["ts"]
        await p._clear_session_incidents()           # 下一场开始
        n = len(server.messages)
        assert await p._expire_clock_gap(ts) is False
        assert await p._dismiss_incident(KEY, ts) is False
        return n

    n = run(scenario())
    assert _shown(server) is None and len(server.messages) == n


async def _close_then_expire(p, server, capsys):
    await _sleep_gap(p, 23, 510.0)
    capsys.readouterr()
    await p._dismiss_incident(KEY, _shown(server)["ts"])
    await _sleep_gap(p, 30, 520.0)
    await p._expire_clock_gap(_shown(server)["ts"])


def test_closing_and_expiring_print_only_chinese_to_the_terminal_in_both_languages(monkeypatch, tmp_path,
                                                                                capsys):
    """终端永远是中文（CLAUDE.md 第九条）：英文界面下关掉、到点收起，终端打印的和中文界面逐字相同。"""
    outs = []
    for lang in (i18n.ZH, i18n.EN):
        (tmp_path / lang).mkdir()
        p, server, sess = _gap_pipeline(monkeypatch, tmp_path / lang)
        with i18n.use(lang):
            run(_close_then_expire(p, server, capsys))
        outs.append(capsys.readouterr().out)
    assert outs[0] == outs[1]
    assert "中控关掉了提示：session:clock_gap" in outs[0] and "10 分钟没有再发生" in outs[0]


def test_the_english_banner_counts_on_after_it_was_closed(monkeypatch, tmp_path):
    p, server, sess = _gap_pipeline(monkeypatch, tmp_path)

    async def scenario():
        await _sleep_gap(p, 23, 510.0)
        await p._dismiss_incident(KEY, _shown(server)["ts"])
        await _sleep_gap(p, 30, 520.0)

    with i18n.use(i18n.EN):
        run(scenario())
        text = i18n.render(_shown(server)["text"], i18n.EN)
    assert text.endswith(" This computer was asleep or suspended for about 7 min. The app wasn’t "
                         "running, so that part of the stream wasn’t monitored. During streams, "
                         "keep the computer plugged in with the lid open. This has happened 2 "
                         "times this session.")


def test_the_page_offers_the_close_button_on_exactly_the_same_banners():
    """web/app.js 的 DISMISSABLE_INCIDENTS 和服务端是同一份：页面多画一个 ×，点了服务端也不理；
    少画一个，中控就又关不掉了。"""
    src = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    m = re.search(r"var DISMISSABLE_INCIDENTS = \[([^\]]*)\];", src)
    assert m, "web/app.js 里没找到 DISMISSABLE_INCIDENTS"
    page = tuple(re.findall(r'"([^"]+)"', m.group(1)))
    assert page == Pipeline.DISMISSABLE_INCIDENTS
