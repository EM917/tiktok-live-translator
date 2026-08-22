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

    def _classify_origin(self, request):
        """WS 来源分级。浏览器里任意网页都能发起 ws://127.0.0.1 连接（不受同源
        策略限制），必须挡掉。分三级：

          "control" —— 本机页面/应用窗口，或非浏览器客户端（无 Origin 头）：
                       可收字幕，也可下发 start/stop/更新等控制指令；
          "view"    —— TikTok 页面（Chrome 插件的 content script 以页面身份
                       连接）：只收字幕。插件本身是纯显示端，不需要控制权；
                       给它控制权等于让任何 tiktok.com 上的脚本能驱动本机程序；
          None      —— 其余一律拒绝。
        """
        origin = request.headers.get("Origin")
        if not origin:
            return "control"
        from urllib.parse import urlparse

        parsed = urlparse(origin)
        host = (parsed.hostname or "").lower()
        if host in ("127.0.0.1", "localhost", "::1"):
            # 精确到本服务端口：本机上跑的其他网页（别人的开发服务器、
            # 某个本地应用的内置页面）不该能驱动本程序
            if (parsed.port or (443 if parsed.scheme == "https" else 80)) == self.port:
                return "control"
            return "view"
        if parsed.scheme == "https" and (host == "tiktok.com" or host.endswith(".tiktok.com")):
            return "view"
        return None

    async def _ws(self, request):
        access = self._classify_origin(request)
        if access is None:
            raise web.HTTPForbidden(text="origin not allowed")
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
                if not isinstance(data, dict):   # 非对象 JSON 会让下游 .get 崩掉
                    continue
                if access != "control":          # 只看不许动（TikTok 页面）
                    continue
                if self.on_control is not None:
                    try:
                        result = self.on_control(data)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception as exc:
                        # 单条控制消息出错不该断开整个连接
                        print("[警告] 处理控制消息失败: {}".format(exc))
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
