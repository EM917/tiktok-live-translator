"""服务端历史回放的标记契约。

约定：网页端要用最后一条恢复底部大字幕，而只显示实时字幕的客户端必须能整段
跳过回放（这条约定起于当年的 Chrome 插件：翻译过一场直播后打开任意直播页会
浮出上一场的最后一句；插件已撤，字段语义保留）。
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


def test_session_break_never_marked_restore_even_when_last():
    """session_break 是场次分隔标记，不是字幕：就算它是 history 里最后一条，
    也不该背 restore——网页端会拿一条没有大字幕字段的消息去恢复底部大字幕。"""
    history = [{"type": "caption", "id": 1, "original": "hola"},
               {"type": "session_break", "ts": 5.0, "streamer": "bella"}]
    out = replay_payloads(history)
    assert out[0]["restore"] is True
    assert "restore" not in out[1]


def test_restore_falls_on_last_caption_not_last_item():
    history = [{"type": "caption", "id": 1}, {"type": "session_break", "ts": 1.0},
               {"type": "caption", "id": 2}, {"type": "session_break", "ts": 2.0}]
    out = replay_payloads(history)
    restored = [i for i, p in enumerate(out) if p.get("restore")]
    assert restored == [2]


def test_replay_with_no_caption_never_sets_restore():
    out = replay_payloads([{"type": "session_break", "ts": 1.0}])
    assert "restore" not in out[0]


def test_session_break_stored_in_history_and_replayed():
    """存进 server.history，和 caption 一样参与重连回放，保证刷新/重连后
    分隔线还在。"""
    import asyncio
    from app.server import CaptionServer

    server = CaptionServer(port=8765)
    asyncio.run(server.broadcast({"type": "session_break", "ts": 100.0,
                                  "streamer": "bellaallnatural"}))
    assert len(server.history) == 1
    stored = list(server.history)[0]
    assert stored["type"] == "session_break" and stored["streamer"] == "bellaallnatural"


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


# ---- 弹幕留意（PR-3）：comment_guard / guard_event / guard_mark 的缓存契约 -----------------------------------

def _guard_server():
    import asyncio
    from app.server import CaptionServer

    server = CaptionServer(port=8765)

    def send(msg):
        asyncio.run(server.broadcast(msg))

    return server, send


def _comment(cid, **extra):
    return dict({"type": "comment", "id": cid, "user": "Ana", "text": "hola", "ts": 1.0,
                 "translated": None, "state": "pending"}, **extra)


def test_comment_guard_overwrites_only_the_guard_key_of_the_cached_comment():
    server, send = _guard_server()
    send(_comment("c1", uid="7234567890123456789", guard={"lvl": "note", "cats": ["flood_repeat"]}))
    send(_comment("c2"))
    send({"type": "comment_update", "id": "c1", "translated": "你好", "state": "ok"})
    new_guard = {"lvl": "alert", "cats": ["flood_repeat"], "src": "rule", "rv": None, "ev": "g1"}
    send({"type": "comment_guard", "id": "c1", "guard": new_guard})
    cached = {c["id"]: c for c in server.comments}
    assert cached["c1"]["guard"] == new_guard
    # 译文、状态、账号字段一个都没动（不借 comment_update：它会无条件把 translated 抹成 None）
    assert cached["c1"]["translated"] == "你好" and cached["c1"]["state"] == "ok"
    assert cached["c1"]["uid"] == "7234567890123456789" and cached["c1"]["text"] == "hola"
    assert "guard" not in cached["c2"]
    assert len(server.comments) == 2                               # 不新增一条
    # 缓存里没有 marked 键：关注高亮由页面按 config.guard_marked 现算
    assert all("marked" not in c for c in server.comments)


def test_comment_guard_for_an_unknown_id_or_without_a_guard():
    server, send = _guard_server()
    send(_comment("c1", guard={"lvl": "amber"}))
    send({"type": "comment_guard", "id": "nope", "guard": {"lvl": "alert"}})          # 找不到这条：什么都不改
    assert list(server.comments)[0]["guard"] == {"lvl": "amber"} and len(server.comments) == 1
    send({"type": "comment_guard", "id": "c1", "guard": None})                          # 没有 guard：撤掉标注
    assert "guard" not in list(server.comments)[0]


def test_comment_guard_message_is_not_stored_as_a_comment_and_leaves_no_trace_elsewhere():
    server, send = _guard_server()
    send(_comment("c1"))
    send({"type": "comment_guard", "id": "c1", "guard": {"lvl": "alert"}})
    assert len(server.comments) == 1 and len(server.history) == 0 and len(server.alerts) == 0
    assert "guard_events" not in server.config and "guard_marked" not in server.config


def test_guard_event_overwrites_by_id_caps_at_50_and_dismiss_removes():
    server, send = _guard_server()
    send({"type": "guard_event", "ev": {"id": "g1", "n": 1, "lvl": "amber"}})
    send({"type": "guard_event", "ev": {"id": "g1", "n": 4, "lvl": "alert"}})           # 同一个 id：覆盖
    assert server.config["guard_events"] == {"g1": {"id": "g1", "n": 4, "lvl": "alert"}}
    for i in range(2, 80):
        send({"type": "guard_event", "ev": {"id": "g%d" % i, "n": 1}})
    events = server.config["guard_events"]
    assert len(events) == 50 and "g1" not in events and "g79" in events and "g30" in events
    send({"type": "guard_event", "ev": {"id": "g30", "n": 9}})                           # 更新过的排到最后：最后才淘汰
    send({"type": "guard_event", "ev": {"id": "g80"}})
    assert "g30" in server.config["guard_events"] and len(server.config["guard_events"]) == 50
    send({"type": "guard_event", "ev": {"id": "g30", "dismissed": True}})
    assert "g30" not in server.config["guard_events"]
    for bad in ({"type": "guard_event"}, {"type": "guard_event", "ev": "x"},
                {"type": "guard_event", "ev": {"n": 1}}):                                # 畸形消息不改任何东西
        send(bad)
    assert len(server.config["guard_events"]) == 49


def test_a_new_session_clears_the_events_through_a_config_message():
    server, send = _guard_server()
    send({"type": "guard_event", "ev": {"id": "g1", "n": 1}})
    send({"type": "guard_mark", "key": "100", "entry": {"keys": ["100"], "by": "auto"}})
    send({"type": "config", "guard_events": {}, "guard_marked": {}, "guard_state": {"status": "rules"}})
    assert server.config["guard_events"] == {} and server.config["guard_marked"] == {}
    assert server.config["guard_state"] == {"status": "rules"}


def test_guard_mark_adds_and_removes_and_the_cache_never_carries_marked():
    server, send = _guard_server()
    send(_comment("c1", uid="100"))
    entry = {"keys": ["100", "s:abc"], "nick": "Ana", "by": "manual", "hits": 0}
    send({"type": "guard_mark", "key": "100", "entry": entry})
    assert server.config["guard_marked"] == {"100": entry}
    send({"type": "guard_mark", "key": "100", "entry": dict(entry, nick="Ana B")})       # 同一个键：覆盖
    assert server.config["guard_marked"]["100"]["nick"] == "Ana B"
    send({"type": "guard_mark", "key": "100", "entry": None})
    assert server.config["guard_marked"] == {}
    send({"type": "guard_mark", "key": "", "entry": entry})                              # 空键：忽略
    assert server.config["guard_marked"] == {}
    assert all("marked" not in c for c in server.comments)                                 # 移出后刷新，回放的弹幕不再高亮


def test_hello_replay_carries_guard_and_a_removed_viewer_is_gone_after_refresh():
    """移出关注后刷新：hello 的 config 里名单已经没有他，回放的弹幕也不带 marked——高亮不会又亮回来。"""
    import asyncio
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from app.server import CaptionServer

    async def connect(server):
        app = web.Application()
        app.router.add_get("/ws", server._ws)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            ws = await client.ws_connect("/ws")
            hello = await ws.receive_json(timeout=5)
            replay = await ws.receive_json(timeout=5)
            await ws.close()
            return hello, replay
        finally:
            await client.close()

    async def scenario():
        server = CaptionServer(port=0)
        guard = {"lvl": "alert", "cats": ["threat"], "src": "rule", "rv": None, "ev": "g1",
                 "why": ["insult/threat:x"], "amb": None, "sub": "threat", "tier": "red"}
        await server.broadcast(_comment("c1", uid="100", uid_ok=True, guard={"lvl": "note"}))
        await server.broadcast({"type": "comment_guard", "id": "c1", "guard": guard})
        await server.broadcast({"type": "guard_event", "ev": {"id": "g1", "n": 1, "lvl": "alert"}})
        await server.broadcast({"type": "guard_mark", "key": "100",
                                "entry": {"keys": ["100"], "nick": "Ana", "by": "auto"}})
        first = await connect(server)
        await server.broadcast({"type": "guard_mark", "key": "100", "entry": None})
        second = await connect(server)
        return guard, first, second

    guard, (hello1, replay1), (hello2, replay2) = asyncio.run(scenario())
    assert replay1["type"] == "comment" and replay1["replay"] is True
    assert replay1["guard"] == guard and replay1["uid"] == "100" and "marked" not in replay1   # 回放带着新的标注
    assert hello1["config"]["guard_marked"]["100"]["by"] == "auto"
    assert hello1["config"]["guard_events"]["g1"]["n"] == 1
    assert hello2["config"]["guard_marked"] == {} and "marked" not in replay2                  # 移出后刷新：不再高亮
    assert replay2["guard"] == guard
