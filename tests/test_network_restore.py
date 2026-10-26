"""网络恢复之后，状态行不能还挂着「本机连不上 www.tiktok.com…」（#143 审查 U6，2026-10-26 补）。

断网时 _wait_for_network 每次重探都把状态行写成「本机连不上 www.tiktok.com，已 N 没有监听，网络恢复后自动重连」，
同时挂 session:network。网络回来时只撤了持续提示：状态行（横幅）还是那句，一直挂到解析出结果——解析要走好几层，
可能几十秒，这期间中控看到的是「连不上」。现在撤掉持续提示时一起发一条「正在解析直播流地址…」（复用开场解析时的
那句中英文，不新造）。

不联网（tiktok_reachable 是桩，测试里本来也不真连）、不起 ffmpeg、不加载模型。
"""
import pytest

from app import i18n, resolver
from app.resolver import ResolveError
from tests.helpers import run
from tests.test_resilience_stream import ROOM, make_pipeline, scripted_resolve

EN = i18n.EN
RESOLVING = "正在解析直播流地址…"

pytestmark = pytest.mark.usefixtures("v1_reconnect")


def probes(*answers):
    """tiktok_reachable 的桩：按顺序回答连不连得上，答完之后一直连得上。"""
    seq = list(answers)

    async def reachable():
        return (seq.pop(0), "gaierror") if seq else (True, "")
    return reachable


def test_the_banner_says_it_is_resolving_once_the_network_is_back(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    sess = p._session_state = p._new_session_state(None)
    monkeypatch.setattr(resolver, "tiktok_reachable", probes(False, False))
    assert run(p._wait_for_network(sess)) is True
    kinds = [(m["type"], m.get("id") or m.get("state")) for m in server.messages]
    # 撤掉持续提示的那一下紧跟着换状态行：横幅不再停在「连不上」
    levels = [m.get("level") for m in server.messages if m["type"] == "incident"]
    assert levels == ["error", "error", "clear"]
    clear = len(kinds) - 1 - kinds[::-1].index(("incident", "session:network"))
    assert server.messages[clear]["level"] == "clear"
    assert kinds[clear + 1] == ("status", "connecting")
    detail = server.messages[clear + 1]["detail"]
    assert str(detail) == RESOLVING and i18n.render(detail, EN) == "Getting the stream URL…"
    assert "本机连不上" not in str(server.config["status"]["detail"])
    assert "session:network" not in (server.config.get("incidents") or {})


def test_no_extra_status_when_the_network_was_never_down(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    sess = p._session_state = p._new_session_state(None)
    monkeypatch.setattr(resolver, "tiktok_reachable", probes())
    assert run(p._wait_for_network(sess)) is True
    assert server.messages == []


def test_during_the_resolve_after_an_outage_the_banner_no_longer_says_unreachable(monkeypatch, tmp_path):
    """整场走一遍：断网 → 恢复 → 去解析。解析进行的那一刻，界面上的状态行是「正在解析直播流地址…」。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    seen = []
    calls = scripted_resolve(monkeypatch, ["http://cdn/a.flv",
                                           ResolveError("没开播", kind="offline", status=4)])
    real = resolver.resolve_stream_url

    async def watching(url, **k):
        seen.append(str((server.config.get("status") or {}).get("detail")))
        return await real(url, **k)

    monkeypatch.setattr(resolver, "resolve_stream_url", watching)
    monkeypatch.setattr(resolver, "tiktok_reachable", probes(False, False, False))

    async def fake_session(media, *a, **k):
        return True, 60.0

    monkeypatch.setattr(p, "_stream_session", fake_session)
    run(p._run_stream_inner(ROOM))
    assert len(calls) == 2
    assert seen[1] == RESOLVING                     # 断网之后的那次解析：以前这里还是「本机连不上…」
    assert any("本机连不上 www.tiktok.com" in d for _, d in server.statuses)
