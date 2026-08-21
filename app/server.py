"""本地 Web 服务：托管字幕 UI 页面，并通过 WebSocket 向所有客户端广播字幕。"""
import asyncio
import json
import time
from collections import deque
from pathlib import Path

from aiohttp import WSMsgType, web

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class CaptionServer:
    def __init__(self, port=8765):
        self.port = port
        self.clients = set()
        self.history = deque(maxlen=100)
        self.config = {"target_lang": "zh-CN", "status": {"state": "idle", "detail": ""}}
        self.on_control = None  # 由 Pipeline 注入，处理来自 UI 的控制消息
        self._runner = None

    async def start(self):
        @web.middleware
        async def no_cache(request, handler):
            resp = await handler(request)
            # 本地小文件，禁掉强缓存，UI 更新后刷新即生效
            resp.headers["Cache-Control"] = "no-cache"
            return resp

        app = web.Application(middlewares=[no_cache])
        app.router.add_get("/", self._index)
        app.router.add_get("/ws", self._ws)
        app.router.add_static("/static", WEB_DIR)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self.port)
        await site.start()

    async def _index(self, request):
        return web.FileResponse(WEB_DIR / "index.html")

    async def _ws(self, request):
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        try:
            await ws.send_json({"type": "hello", "config": self.config})
            # 最后一条不带 replay 标记，让客户端用它恢复底部大字幕；
            # 回放完成后才加入广播集合，避免新字幕插进回放序列中间
            items = list(self.history)
            for i, item in enumerate(items):
                replay = dict(item)
                if i < len(items) - 1:
                    replay["replay"] = True
                await ws.send_json(replay)
            self.clients.add(ws)
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                if self.on_control is not None:
                    result = self.on_control(data)
                    if asyncio.iscoroutine(result):
                        await result
        finally:
            self.clients.discard(ws)
        return ws

    async def broadcast(self, msg):
        if msg.get("type") == "caption" and not msg.get("replay"):
            self.history.append(msg)
        elif msg.get("type") == "status":
            self.config["status"] = {"state": msg.get("state"), "detail": msg.get("detail", "")}
        elif msg.get("type") == "config":
            self.config.update({k: v for k, v in msg.items() if k != "type"})
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    async def status(self, state, detail=""):
        await self.broadcast({"type": "status", "state": state, "detail": detail, "ts": time.time()})
