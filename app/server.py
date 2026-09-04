"""本地 Web 服务：托管字幕 UI 页面，并通过 WebSocket 向所有客户端广播字幕。"""
import asyncio
import json
import time
from collections import deque
from pathlib import Path

from aiohttp import WSMsgType, web

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def replay_payloads(history):
    """把历史字幕转成回放消息。

    每一条都带 replay=True——只显示实时字幕的客户端（Chrome 插件）据此整段跳过。
    以前为了让网页端恢复底部大字幕，最后一条刻意不加标记，结果插件把它当成新
    字幕：翻译过一场直播后再打开任意直播页，画面上就会浮出上一场的最后一句。
    改由单独的 restore 标记承担「恢复大字幕」这件事，两个用途各归各的字段。
    """
    items = list(history)
    payloads = []
    for i, item in enumerate(items):
        payload = dict(item)
        payload["replay"] = True
        if i == len(items) - 1:
            payload["restore"] = True
        payloads.append(payload)
    return payloads


class CaptionServer:
    # 单个连接每秒最多处理这么多条 viewer_comments 消息，超出直接丢弃、不广播。
    # 白名单只按来源（https://*.tiktok.com）放行，不要求走 Chrome 插件，任何
    # 跑在该来源页面上的脚本都能连上来高频灌消息；广播和字幕/警报共用同一个
    # 事件循环，洪水会挤占那两样的时间片——弹幕本身允许丢，这道闸门不算回归。
    CMT_MSG_RATE_LIMIT = 10
    CMT_MSG_RATE_WINDOW_SEC = 1.0

    def __init__(self, port=8765):
        self.port = port
        self.clients = set()
        self.history = deque(maxlen=100)
        # 违禁词警报必须跨刷新/重连留存：中控没看到就等于漏报，
        # 不能因为页面重载而消失
        self.alerts = deque(maxlen=50)
        # 观众弹幕：同样要跨刷新/重连留存，不然中控重连一次就看不到刚才谁说了什么
        self.comments = deque(maxlen=100)
        self.config = {"target_lang": "zh-CN", "status": {"state": "idle", "detail": ""}}
        self.on_control = None  # 由 Pipeline 注入，处理来自 UI 的控制消息
        self.on_comments = None  # 由 Pipeline 注入，处理来自 Chrome 插件的观众评论
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
                       唯一例外是 viewer_comments 这一种数据消息（观众弹幕，
                       见 _ws 里的白名单分支）——它只携带评论文本，不经过
                       on_control，这道控制权的闸门本身不因此松动；
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
        cmt_times = deque(maxlen=self.CMT_MSG_RATE_LIMIT)   # 本连接限频用的滑动窗口
        try:
            await ws.send_json({"type": "hello", "config": self.config})
            # 先补发历史警报（刷新页面不能丢报警），再回放字幕
            for alert in list(self.alerts):
                await ws.send_json(dict(alert, replay=True))
            # 回放完成后才加入广播集合，避免新字幕插进回放序列中间
            for payload in replay_payloads(self.history):
                await ws.send_json(payload)
            # 观众弹幕历史同样要回放，且同样带 replay 标记
            for c in list(self.comments):
                await ws.send_json(dict(c, replay=True))
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
                if data.get("type") == "viewer_comments":
                    # 唯一允许来自 TikTok 页面（view 来源）的入站消息。它只携带
                    # 评论文本，由 on_comments 校验、限量，永远不经过
                    # on_control——下面 access != "control" 这道闸门本身不动。
                    now = time.time()
                    if (len(cmt_times) >= self.CMT_MSG_RATE_LIMIT
                            and now - cmt_times[0] < self.CMT_MSG_RATE_WINDOW_SEC):
                        continue      # 单连接限频：超频消息静默丢弃，不广播
                    cmt_times.append(now)
                    if self.on_comments is not None:
                        try:
                            result = self.on_comments(data)
                            if asyncio.iscoroutine(result):
                                await result
                        except Exception as exc:
                            print("[警告] 处理观众评论失败: {}".format(exc))
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
        elif msg.get("type") == "caption_update":
            # 译文是后补的：历史里那条也要补上，否则重连回放会只剩原文
            for item in reversed(self.history):
                if item.get("id") == msg.get("id"):
                    item["translated"] = msg.get("translated")
                    item["translate_state"] = msg.get("translate_state")
                    break
        elif msg.get("type") == "alert":
            self.alerts.append(msg)
        elif msg.get("type") == "alert_update":
            # 补进留存的那一条：报警面板刷新后要能看到中文，
            # 否则中控一刷新就只剩西语，等于没补
            for a in self.alerts:
                if a.get("alert_id") == msg.get("alert_id"):
                    a["context_zh"] = msg.get("context_zh")
                    break
        elif msg.get("type") == "comment" and not msg.get("replay"):
            self.comments.append(msg)
        elif msg.get("type") == "comment_update":
            # 译文是后补的：历史里那条也要补上，否则重连回放会只剩原文/pending
            for c in reversed(self.comments):
                if c.get("id") == msg.get("id"):
                    c["translated"] = msg.get("translated")
                    c["state"] = msg.get("state")
                    break
        elif msg.get("type") == "status":
            # command 必须一起留存：它常常是用户当下唯一的出路，
            # 刷新一下页面就没了的话，等于没给。
            self.config["status"] = {"state": msg.get("state"),
                                     "detail": msg.get("detail", ""),
                                     "command": msg.get("command")}
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

    async def status(self, state, detail="", command=None):
        """command：给用户一条可以原样照做的命令（界面会渲染成可复制的一行）。

        存在的理由是一次真实的死局：更新器因为几个未跟踪文件拒绝更新，
        提示是「请自行处理后 git pull」——而修好这个判断的代码，只能靠更新
        送达。更新器把自己锁死了。所以任何一条「更新没能完成」的出口，都必须
        带上一条用户能照做的完整命令，哪怕程序本身已经帮不上忙。"""
        msg = {"type": "status", "state": state, "detail": detail, "ts": time.time()}
        if command:
            msg["command"] = command
        await self.broadcast(msg)
