"""服务端历史回放的标记契约。

跨客户端约定：网页端要用最后一条恢复底部大字幕，Chrome 插件则必须整段跳过
回放（否则翻译过一场直播后，打开任意直播页都会浮出上一场的最后一句）。
两个用途各用各的字段——replay 表示「这是历史」，restore 表示「用它恢复大字幕」。
"""
from app.server import replay_payloads


def test_every_payload_marked_replay():
    history = [{"type": "caption", "id": i, "original": "x"} for i in range(3)]
    out = replay_payloads(history)
    assert [p["replay"] for p in out] == [True, True, True]


def test_only_last_marked_restore():
    history = [{"type": "caption", "id": i} for i in range(3)]
    out = replay_payloads(history)
    assert [p.get("restore", False) for p in out] == [False, False, True]


def test_single_item_history():
    out = replay_payloads([{"type": "caption", "id": 1}])
    assert out[0]["replay"] is True and out[0]["restore"] is True


def test_empty_history():
    assert replay_payloads([]) == []


def test_originals_not_mutated():
    history = [{"type": "caption", "id": 1}]
    replay_payloads(history)
    assert "replay" not in history[0]      # 内存里的历史不能被回放标记污染


def test_alerts_survive_reconnect():
    """警报要跨刷新/重连留存——中控没看到就等于漏报。"""
    import asyncio
    from app.server import CaptionServer

    server = CaptionServer(port=8765)
    asyncio.run(server.broadcast({"type": "alert", "term": "cura el cancer",
                                  "tier": "exact", "ts": 100}))
    assert len(server.alerts) == 1
    assert server.alerts[0]["term"] == "cura el cancer"


def test_caption_update_patches_history():
    """译文是后补的：历史里那条也要补上，否则重连回放只剩原文。"""
    import asyncio
    from app.server import CaptionServer

    server = CaptionServer(port=8765)
    asyncio.run(server.broadcast({"type": "caption", "id": 7, "original": "hola",
                                  "translated": None, "translate_state": "pending"}))
    asyncio.run(server.broadcast({"type": "caption_update", "id": 7,
                                  "translated": "你好", "translate_state": "ok"}))
    stored = list(server.history)[-1]
    assert stored["translated"] == "你好"
    assert stored["translate_state"] == "ok"
