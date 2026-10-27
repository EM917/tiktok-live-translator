"""一场自己结束之后（下播、出错），后台的事不能把主窗口的结束状态换成待机（#135 核对、b3-float-end 第 2 轮记）。

直播中推迟的本地翻译模型下载在这一场结束后才开始（_pull_after_session），以前每 5% 一条 status=idle：主窗口
那句「直播已结束」/「出错了」和下面的说明被换成待机加下载进度，中控回来看不到这一场是怎么结束的；浮窗结束卡片上
「详情见主窗口」也落空。现在状态是 ended / error 时下载进度只发 notice（和直播中一样），状态行原样留着；
中控点了停止（idle）时照旧走 status，首页大字显示进度。

同一类的另外两处在 app/updater.py（yt-dlp 自动更新完那条「请重新点开始」、没在监听时一键更新没成的几种出口），
这一波 updater.py 归别的批次，顺延，见 PR 说明。

不联网、不下载、不加载模型。夹具里的中文（结束状态的说明、进度文字）是测试自己交给生产函数的，用例标了
i18n_fixture。
"""
import asyncio

import pytest

from app import localmodel
from app.translator import HYMT2_SMALL
from tests.helpers import run
from tests.test_resilience_stream import make_pipeline

PROGRESS = ("正在准备本地翻译模型（约 1.1 GB，只需这一次）…",
            "正在下载本地翻译模型：40%（440 / 1100 MB，只需这一次）…")


def notices(server):
    return [str(m["text"]) for m in server.messages if m.get("type") == "notice"]


def statuses(server):
    return [(m["state"], str(m.get("detail", ""))) for m in server.messages if m.get("type") == "status"]


@pytest.mark.i18n_fixture
@pytest.mark.parametrize("state, detail", [("ended", "直播已结束。可以输入新的直播间地址继续。"),
                                           ("error", "直播流多次中断且自动重连失败。")])
def test_download_progress_after_the_session_ended_by_itself_leaves_the_end_state(monkeypatch, tmp_path,
                                                                                 state, detail):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def scenario():
        await server.status("live", "")
        await server.status(state, detail)
        for text in PROGRESS:
            await p._provision_note(text)
        await p._provision_note("本地翻译已就绪，可以开始了。")

    run(scenario())
    assert statuses(server) == [("live", ""), (state, detail)]
    assert server.config["status"]["state"] == state and str(server.config["status"]["detail"]) == detail
    assert notices(server) == list(PROGRESS) + ["本地翻译已就绪，可以开始了。"]


@pytest.mark.i18n_fixture
def test_after_the_operator_stopped_the_progress_still_shows_on_the_start_page(monkeypatch, tmp_path):
    """中控点了停止（状态是 idle）：照旧走 status，首页大字显示进度。没开始过（hello 之前的默认 idle）也一样。"""
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def scenario():
        await p._provision_note(PROGRESS[0])
        await server.status("live", "")
        await server.status("idle", "")
        await p._provision_note(PROGRESS[1])

    run(scenario())
    assert statuses(server) == [("idle", PROGRESS[0]), ("live", ""), ("idle", ""), ("idle", PROGRESS[1])]
    assert notices(server) == []


@pytest.mark.i18n_fixture
def test_the_real_deferred_pull_after_an_ended_session_reports_in_notices(monkeypatch, tmp_path):
    """走真的 _pull_model（进度回调每 5% 一条，_spawn 出去）：下播之后补下载，状态行一直是「直播已结束」。"""
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def fake_pull(model, on_progress=None):
        for pct in (5, 10, 40, 100):
            on_progress(pct, pct * 11, 1100)
            await asyncio.sleep(0)
        return True, None

    monkeypatch.setattr(localmodel, "pull", fake_pull)

    async def scenario():
        await server.status("ended", "直播已结束。")
        ok = await p._pull_model(HYMT2_SMALL)
        for _ in range(20):
            await asyncio.sleep(0)
        return ok

    assert run(scenario()) is True
    assert statuses(server) == [("ended", "直播已结束。")]
    said = notices(server)
    assert said[0].startswith("正在准备本地翻译模型") and any("正在下载本地翻译模型" in t for t in said)
