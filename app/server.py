"""本地 Web 服务：托管字幕 UI 页面，并通过 WebSocket 向所有客户端广播字幕。"""
import asyncio
import hashlib
import json
import time
from collections import deque
from pathlib import Path

from aiohttp import WSMsgType, web

from . import i18n

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def replay_payloads(history):
    """把历史字幕（以及场次分隔标记）转成回放消息。

    每一条都带 replay=True——只显示实时字幕的客户端据此整段跳过（这条约定起于
    当年的 Chrome 插件，插件已撤、语义保留）。以前为了让网页端恢复底部大字幕，
    最后一条刻意不加标记，结果插件把它当成新
    字幕：翻译过一场直播后再打开任意直播页，画面上就会浮出上一场的最后一句。
    改由单独的 restore 标记承担「恢复大字幕」这件事，两个用途各归各的字段。

    history 现在还混着 session_break（分隔线用，不是字幕）：restore 只该打在
    「最后一条 caption」上，不能落到它后面可能跟着的 session_break 头上，
    否则网页端会拿一条没有大字幕字段的消息去恢复底部大字幕。
    """
    items = list(history)
    last_caption = None
    for i, item in enumerate(items):
        if item.get("type") == "caption":
            last_caption = i
    payloads = []
    for i, item in enumerate(items):
        payload = dict(item)
        payload["replay"] = True
        if item.get("type") == "caption" and i == last_caption:
            payload["restore"] = True
        payloads.append(payload)
    return payloads


# 回放的深度。中控页回放多少，由页面自己最多留多少决定：
#   HISTORY_MAX  = web/app.js 的 maxHistory（字幕卡片最多留几张）
#   COMMENTS_MAX = web/app.js 的 maxComments（弹幕栏最多留几条）
# 两个数在 tests/test_server_clear.py 里拿正则从 app.js 读出来比对：一边改了另一边没改，测试就红。
# 手机的回放有自己的上限（app/viewer.py 的 PHONE_HISTORY_MAX / PHONE_COMMENTS_MAX），不跟着这里涨：
# 手机的入站队列是固定的，回放压不进去会被判定为落后。
HISTORY_MAX = 300
COMMENTS_MAX = 100
ALERTS_MAX = 50
# config["incidents"]（持续提示）最多留几条，超出时丢最久没更新的那条（见 broadcast）。
# 手机回放的上限 app/viewer.py 的 REPLAY_MAX 按这个数和 ALERTS_MAX 算，改这里不用再去那边改
INCIDENTS_MAX = 20
# 这几种 status 说明这一场已经结束：停止（idle）、下播（ended）、出错（error）。
# 之后不会再有这一场的识别落后提示，config["health"] 在这时删掉、也不再存（见 broadcast）
SESSION_OVER_STATES = ("idle", "ended", "error")
# 浮窗页（web/float.html，app/float_window.py）：它连 /ws?role=float，只读。收什么、回放多深，都在这里定死：
#   FLOAT_TYPES        广播里只转发这几种消息（报警、审计提示、incident、health 一概不发；
#                      tests/test_float_server.py 拿正则从 web/float.js 读出它处理的类型来比对）
#   FLOAT_CONFIG_KEYS  hello 和 config 广播里只留这几个键（不把引擎、磁盘、同看链接这些带给它）
FLOAT_TYPES = frozenset({"caption", "caption_update", "session_break", "comment", "comment_update",
                         "comment_guard", "guard_mark", "history_cleared", "status", "config"})
FLOAT_CONFIG_KEYS = ("caption_size", "target_lang", "status", "guard_state", "guard_marked")
FLOAT_REPLAY_CAPTIONS = 3
FLOAT_REPLAY_COMMENTS = 10
# history 里除了字幕还混着场次分隔（session_break），它们不算字幕、页面也不把它们算进 300 张卡片。
# deque 多留一点余量，回放时再按「最后 HISTORY_MAX 条字幕」切（captions_tail）——换主播换得再勤，
# 回放的字幕条数也是 HISTORY_MAX，而不是少一条分隔线就少一条字幕
HISTORY_BREAK_ROOM = 30
# 控制面所有响应都带的 CSP 片段：谁都不许把这些页面放进 iframe（见 build_app）
FRAME_ANCESTORS = "frame-ancestors 'none'"


def web_build(web_dir=None):
    """web/ 下所有页面文件内容的短哈希（12 位十六进制），启动时算一次放进 config["build"]（专家审查 F8）。

    浏览器模式下一键更新重启后，旧标签页重连上新进程，跑的却还是旧 JS：hello 里的 version 只用来改页脚
    文字，没人比对。现在页面记下第一次连上时的 build，之后哪一次 hello 里的不一样，就是服务端换了一套页面
    文件，重新载入（web/app.js reloadForBuild）。按内容算、不按版本号：没改 web/ 的更新不必打断页面，
    开发时改了页面再重启服务也会刷新。点开头的文件（.DS_Store 之类）不算；读不了的文件按「读不了」记进去，
    不让整个哈希失败。web/ 都读不了时返回空串，页面见到空串不比对"""
    root = Path(web_dir) if web_dir is not None else WEB_DIR
    try:
        files = sorted(p for p in root.rglob("*")
                       if p.is_file() and not any(part.startswith(".") for part in p.relative_to(root).parts))
    except OSError:
        return ""
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        try:
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        except OSError:
            digest.update(b"unreadable")
    return digest.hexdigest()[:12] if files else ""


def captions_tail(items, limit):
    """items 里最后 limit 条 caption，连同夹在它们中间和之后的 session_break。

    页面只数 caption（.cap 卡片）的上限，分隔线不占名额；这里按同一个口径切。
    够不到 limit 条就原样返回。"""
    items = list(items)
    if limit <= 0:
        return []
    seen = 0
    for i in range(len(items) - 1, -1, -1):
        if items[i].get("type") == "caption":
            seen += 1
            if seen == limit:
                return items[i:]
    return items


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _ts_of(msg):
    ts = msg.get("ts")
    return ts if _is_number(ts) else 0.0


def _redact_share_state(state):
    """手机同看的状态载荷（ViewerHub.state() 那份，或 type="viewer" 的广播）去掉口令后的副本。

    口令只在链接的片段里（app/viewer.viewer_url：http://地址:端口/#k=口令）：链接截到「#」之前，
    说明文字里出现的同一个片段一并去掉；二维码不发——它编码的就是带口令的那条链接。
    没有链接（同看关着、没读到地址）时原样返回。不改传进来的对象：中文时 i18n.render
    原样返回 server.config 本身。"""
    url = state.get("url")
    if not isinstance(url, str) or "#" not in url:
        return state
    fragment = url[url.index("#"):]
    out = {}
    for key, value in state.items():
        if isinstance(value, i18n.Bi):          # 还没渲染的双语句子（中文时）：两臂都去掉
            value = i18n.Bi(str(value).replace(fragment, ""), value.en.replace(fragment, ""))
        elif isinstance(value, str):
            value = value.replace(fragment, "")
        out[key] = value
    out["qr_rows"] = None
    return out


def without_share_key(msg):
    """发给无 Origin 连接的那一份：凡是带手机同看链接的地方都去掉口令（见 _redact_share_state）。
    hello 里的 config、config 广播、type="viewer" 的广播都认；别的消息原样返回（同一个对象）。

    为什么只对无 Origin 的连接这样：浏览器和应用窗口发起 WebSocket 一定带 Origin，中控页要在
    卡片上显示完整链接和二维码；没有 Origin 的是 curl、本地脚本一类，它们照旧有控制权
    （tests/test_server_origin.py 的 test_no_origin_is_control），但不需要这把钥匙——以前 hello
    把带口令的同看链接整个发给任何一个连上 127.0.0.1 的本地进程。"""
    if not isinstance(msg, dict):
        return msg
    if msg.get("type") == "viewer":
        return _redact_share_state(msg)
    state = msg.get("viewer")
    if isinstance(state, dict):
        redacted = _redact_share_state(state)
        if redacted is not state:
            return dict(msg, viewer=redacted)
    return msg


def _write_buffer_size(transport):
    if transport is None:
        return None
    try:
        return transport.get_write_buffer_size()
    except Exception:
        return None


class CaptionServer:
    # 一个页面收不下消息时的两道闸（见 broadcast）。都放得很宽：只该拦住真的卡死的
    # 页面，不该拦住跑了个长 JS 任务的慢页面——被断开的页面会重连，而每次 hello
    # 都会清空面板重放。
    SEND_TIMEOUT_SEC = 2.0
    MAX_CLIENT_BUFFER = 2 * 1024 * 1024
    GUARD_EVENTS_MAX = 50       # config["guard_events"] 最多留几个事件（弹幕留意的抽屉）
    # 一个页面在回放期间最多攒多少条广播（见 _ws）。正常回放只要几毫秒，这个数只为拦住「连上了
    # 却一条都不读」的页面：它卡在回放里，期间的每条广播都要替它留着，不设上限就是内存泄漏。
    # 超了就断开它——页面会自己重连，hello 之后补回完整回放
    MAX_REPLAY_BACKLOG = 2000
    # 由 Pipeline 注入：(reason, buffered_bytes) -> None，写审计、打一行日志
    on_client_dropped = None

    def __init__(self, port=8765):
        self.port = port
        self.clients = set()
        self._transports = {}    # ws -> 这个连接的 transport（断开卡死的页面要 abort 它）
        self._replaying = {}     # ws -> 它回放期间发生的广播（list）；回放完、入册的同一步里清掉
        # 浮窗页的连接（也在 clients 里，共用同一条发送路径和卡死保护）：不算「显示报警的界面页面」
        # （ui_client_count），入站一律丢弃，只收 FLOAT_TYPES
        self._float_ws = set()
        # 没带 Origin 头的连接（也在 clients 里）：发给它们的消息去掉手机同看链接里的口令（见 _ws）
        self._keyless_ws = set()
        # 本机页面连上过没有（只用来在第一次时通知一键更新的启动检查，见 _first_page_connected）
        self._page_seen = False
        self.history = deque(maxlen=HISTORY_MAX + HISTORY_BREAK_ROOM)
        # 违禁词警报必须跨刷新/重连留存：中控没看到就等于漏报，
        # 不能因为页面重载而消失
        self.alerts = deque(maxlen=ALERTS_MAX)
        # 观众弹幕：同样要跨刷新/重连留存，不然中控重连一次就看不到刚才谁说了什么
        self.comments = deque(maxlen=COMMENTS_MAX)
        # 最近一次（非回放的）session_break 本身。history 是有限的 deque，一场播得够久它就会被挤出去；
        # 弹幕缓存又不随换场清空——浮窗页的回放靠它认出「哪些弹幕是上一个主播的」（见 replay_messages）
        self._last_break = None
        self.config = {"target_lang": "zh-CN", "status": {"state": "idle", "detail": ""}}
        self.on_control = None  # 由 Pipeline 注入，处理来自 UI 的控制消息
        # 由 Pipeline 注入（桌面应用模式）：每条 status 广播之后同步调一次，参数是那条消息。
        # 必须同步、不阻塞——它在广播的路径上；浮窗靠它在监听结束时自己关掉
        self.on_status = None
        # 弹幕抓取（TikTokLive）的状态：中控要能区分「还没开播」「正在连」
        # 「连上了」「装不上/需要登录」——面板空着时「没人发」和「没连上」
        # 对中控的意义完全不同
        self.config["comment_backend"] = "idle"
        self.config["comment_detail"] = ""
        self._runner = None
        # 手机同看的观众面（app/viewer.ViewerHub），中控打开时由 Pipeline 挂上来。
        # 默认 None：这个功能默认关闭，也就默认不存在第二个监听面
        self.viewer = None

    def build_app(self):
        """本机控制面的 aiohttp 应用（路由 + 响应头中间件）。单独成方法，测试可以不占固定端口直接拿来起。"""
        @web.middleware
        async def no_cache(request, handler):
            resp = await handler(request)
            # 本地小文件，禁掉强缓存，UI 更新后刷新即生效
            resp.headers["Cache-Control"] = "no-cache"
            # 不许被别的网页用 iframe 嵌进去（点击劫持：恶意网页把控制页透明地叠在诱饵按钮上，
            # 骗人点到「停止」——它一下就生效）。两个头都发：老 WebView 只认 X-Frame-Options，
            # 新的以 CSP 为准。本程序自己从不用 iframe：浮窗是独立的原生窗口（app/float_window.py），
            # 不是嵌在控制页里的框。已有的 CSP 不覆盖，只补上 frame-ancestors
            resp.headers["X-Frame-Options"] = "DENY"
            csp = resp.headers.get("Content-Security-Policy")
            if not csp:
                resp.headers["Content-Security-Policy"] = FRAME_ANCESTORS
            elif "frame-ancestors" not in csp:
                resp.headers["Content-Security-Policy"] = csp + "; " + FRAME_ANCESTORS
            return resp

        app = web.Application(middlewares=[no_cache])
        app.router.add_get("/", self._index)
        app.router.add_get("/ws", self._ws)
        app.router.add_get("/float", self._float_page)
        app.router.add_static("/static", WEB_DIR)
        return app

    async def start(self):
        # 这一次进程端出去的页面文件是哪一版（F8，见 web_build）：先于任何页面连上就定好，hello 里一直是它
        self.config["build"] = web_build()
        self._runner = web.AppRunner(self.build_app(), access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self.port)
        await site.start()

    async def _index(self, request):
        """桌面页。中文时照旧端文件本身，字节与改造前相同；英文时只把 <html lang> 换成 en，
        页面里的 web/i18n.js 按它换文字（spec §2.2）。桌面页不加 data-i18n：那是手机页的闸标记。"""
        path = WEB_DIR / "index.html"
        if i18n.current() == i18n.ZH:
            return web.FileResponse(path)
        html = i18n.inject_lang(path.read_text(encoding="utf-8"), i18n.EN)
        return web.Response(text=html, content_type="text/html", charset="utf-8")

    async def _float_page(self, request):
        """浮窗页（app/float_window.py 开的那个置顶小窗）。语言的处理同 _index：中文时端文件本身，
        英文时只把 <html lang> 换成 en，web/i18n.js 按它换文字。它没有 <meta name="tlt-app">：
        双击第二次时 main.py 靠那个指纹认「这是不是本程序的主界面」，浮窗页不是。"""
        path = WEB_DIR / "float.html"
        if i18n.current() == i18n.ZH:
            return web.FileResponse(path)
        html = i18n.inject_lang(path.read_text(encoding="utf-8"), i18n.EN)
        return web.Response(text=html, content_type="text/html", charset="utf-8")

    def _classify_origin(self, request):
        """WS 来源判定。浏览器里任意网页都能发起 ws://127.0.0.1 连接（不受同源
        策略限制），必须挡掉。只有两种结果：

          "control" —— 本机页面/应用窗口，或非浏览器客户端（无 Origin 头）：
                       可收字幕，也可下发 start/stop/更新等控制指令。无 Origin 的
                       连接拿不到手机同看链接里的口令（见 _ws 和 without_share_key）；
          None      —— 其余一律拒绝，包括本机其它端口上的网页和 tiktok.com。

        曾有第三级 "view"（Chrome 插件以 TikTok 页面身份连接，只收字幕、只发
        弹幕数据）。插件整条撤掉之后（2026-09-07），没有任何合法客户端需要
        以别的来源连进来，那一级连同它的数据消息白名单一起删掉——少一个
        入口就少一个要守的门。
        """
        origin = request.headers.get("Origin")
        if not origin:
            return "control"
        from urllib.parse import urlparse

        parsed = urlparse(origin)
        host = (parsed.hostname or "").lower()
        # "::1" 这一支实际走不到：start() 只绑 127.0.0.1，本程序的页面不可能从 http://[::1]:端口
        # 打开，也就不会带这个 Origin。留着只为和 localhost 的写法对称，别当成「IPv6 也在用」
        if host in ("127.0.0.1", "localhost", "::1"):
            # 精确到本服务端口：本机上跑的其他网页（别人的开发服务器、
            # 某个本地应用的内置页面）不该能驱动本程序
            if (parsed.port or (443 if parsed.scheme == "https" else 80)) == self.port:
                return "control"
        return None

    async def _ws(self, request):
        access = self._classify_origin(request)
        if access is None:
            raise web.HTTPForbidden(text="origin not allowed")
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        # 浮窗页自报家门（只能降权：它因此少收东西、不能发指令、不被当成显示报警的页面）。
        # 来源检查照旧：只有本机同端口的页面连得进来
        is_float = request.query.get("role") == "float"
        if is_float:
            self._float_ws.add(ws)
        # 没带 Origin 的（curl、本地脚本）照旧是 control，但同看链接里的口令不给它：hello 和之后的
        # 广播都换成去掉口令的那份（without_share_key）。浏览器和应用窗口一定带 Origin，不受影响
        keyless = not request.headers.get("Origin")
        if keyless:
            self._keyless_ws.add(ws)
        try:
            # 「快照」和「开始替这个页面攒广播」必须在同一个同步步骤里（和 app/viewer.py 的
            # ViewerHub.register 同一个道理）：下面每发一条都要 await，回放期间别处的广播照常
            # 进行，而此时这个页面还不在 self.clients 里——不攒的话，那些广播进了缓存却永远
            # 到不了这个页面（一条违禁词警报就这样漏掉）。反过来，攒的起点就是快照的终点，
            # 所以回放和 backlog 之间既没有缺口也没有重复。
            # 缓存里存的是原消息（界面文字是 Bi），这里按「此刻」的界面语言现渲染：
            # 切换语言后重连的页面拿到的是新语言，不会残留旧的。中文时 render 原样返回
            hello = {"type": "hello", "config": i18n.render(self._float_config(self.config)
                                                            if is_float else self.config)}
            if keyless:
                hello["config"] = without_share_key(hello["config"])
            replay = self.replay_messages("float" if is_float else "control")
            backlog = self._replaying[ws] = []
            self._transports[ws] = request.transport
            await ws.send_json(hello)
            # 先补发历史警报（刷新页面不能丢报警），再回放字幕，最后是观众弹幕。
            for payload in replay:
                await ws.send_json(i18n.render(payload))
            # 回放期间发生的广播（新字幕、新警报、译文补丁、清空通知……）照发生顺序补上。补的时候
            # 又有新的进来就再补一轮，直到队列在检查的那一刻是空的；检查和入册之间没有 await，
            # 所以不会再有漏网的。这些是真正的实时消息，不带 replay 标记，页面照常处理（警报照常提醒）
            while True:
                if ws not in self._replaying:
                    return ws        # 积压超限被 _drop_client 摘掉了：连接已 abort，没有再发的意义
                if not backlog:
                    break
                batch = backlog[:]
                del backlog[:]
                for out in batch:
                    await ws.send_json(out)
            # 以下到 async for 之前一个 await 都不能有：回放完了才加入广播集合，避免新字幕插进回放序列中间
            del self._replaying[ws]
            self.clients.add(ws)
            if not is_float and not keyless and not getattr(self, "_page_seen", False):
                self._first_page_connected()
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                if not isinstance(data, dict):   # 非对象 JSON 会让下游 .get 崩掉
                    continue
                if is_float:
                    continue         # 浮窗页只读：它发什么都不理（连清空缓存也不认）
                if data.get("type") == "clear_history":
                    # 缓存归服务端自己管，不必绕到 Pipeline。只认本机页面（上面的来源检查已拦住其余）；
                    # 手机入站只认 retranslate，发不到这里
                    await self.clear_history(data.get("kind"))
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
            self._float_ws.discard(ws)
            self._keyless_ws.discard(ws)
            self._transports.pop(ws, None)
            self._replaying.pop(ws, None)
        return ws

    def _first_page_connected(self):
        """本机页面第一次连上、拿完 hello 和回放（专家审查 I6）：一键更新之后的新版本算是起来了，清掉
        app/update_guard.py 的记号——不清的话，新版本两次启动页面都没连上，第三次打开就会退回旧版本。
        只认带 Origin 的本机页面（应用窗口、浏览器）：浮窗页不算（它只在主窗口之后才开），没带 Origin
        的本地脚本不是页面。
        同步、不 await（调用处在「入册」和读消息之间）；出什么错都不影响这个连接。"""
        self._page_seen = True
        try:
            from . import update_guard
            update_guard.mark_ready()
        except Exception as exc:
            print("[警告] 记录「页面已连上」失败: {}".format(exc))

    def ui_client_count(self):
        """显示报警的界面页面有几个：不数浮窗页。审计里每条报警记着发出时的页面数
        （Pipeline._alert_stamp），浮窗不显示报警，数进去会让「有页面接收了报警」的证据变假。"""
        return len(self.clients - (getattr(self, "_float_ws", None) or set()))

    @staticmethod
    def _float_config(config):
        """config（或一条 config 广播）里浮窗页用得上的那几个键；没有就是空 dict。"""
        return {k: config[k] for k in FLOAT_CONFIG_KEYS if k in config}

    def replay_messages(self, role="control"):
        """hello 之后补给本机页面的历史，按发送顺序：警报、字幕（含场次分隔）、观众弹幕。
        每条都带 replay=True；最后一条字幕另带 restore（见 replay_payloads）。

        深度是中控页自己的上限（HISTORY_MAX / COMMENTS_MAX），不是手机的：手机拿的是
        app/viewer.py 的 replay_snapshot，它对同一份缓存再切一刀。纯同步，只读缓存。

        role="float"：浮窗页只要最后几条字幕和弹幕，不要报警（它不显示报警）。弹幕缓存不随换主播清空，
        而浮窗只回放最后 3 条字幕，换场的分隔线常常不在里面；所以最近一次换场的分隔线无论如何都放在
        回放最前面（页面据此清空并记下那一刻），比它老的弹幕不回放——不然重连的浮窗会把上一个主播的
        弹幕当成本场最新的。"""
        if role == "float":
            tail = captions_tail(self.history, FLOAT_REPLAY_CAPTIONS)
            comments = list(self.comments)
            brk = self._latest_break()
            if brk is not None:
                if not any(item is brk for item in tail):
                    tail = [brk] + tail
                since = brk.get("ts")
                if _is_number(since):
                    comments = [c for c in comments if not (_is_number(c.get("ts")) and c["ts"] < since)]
            out = replay_payloads(tail)
            out.extend(dict(c, replay=True) for c in comments[-FLOAT_REPLAY_COMMENTS:])
            return out
        out = [dict(alert, replay=True) for alert in list(self.alerts)]
        out.extend(replay_payloads(captions_tail(self.history, HISTORY_MAX)))
        out.extend(dict(c, replay=True) for c in list(self.comments)[-COMMENTS_MAX:])
        return out

    def _latest_break(self):
        """最近一次换场的 session_break 消息，没有就是 None。broadcast 记下的那份优先（history 一场播久了
        会把它挤出去，clear_history 也会清掉它）；个别测试手搭的缓存里只有 history，也认。"""
        best = getattr(self, "_last_break", None)
        for item in reversed(getattr(self, "history", None) or ()):
            if item.get("type") == "session_break":
                if best is None or _ts_of(item) >= _ts_of(best):
                    best = item
                break
        return best

    async def clear_history(self, kind):
        """中控点了「清空字幕」/「清空弹幕」：把服务端缓存里那一类也清掉。

        以前只有页面把自己的节点删了，缓存原样留着，刷新或重连后 hello 又把这些行放了回来——
        「清空」只在没刷新之前成立。警报不在这里：违禁词警报跨刷新留存是合规要求（没看到就等于漏报），
        有自己的「清空警报」，那一个也只清页面，缓存里的警报留着。

        不碰审计：审计日志是另一条独立的记录，清的只是中控页上能回看的那一份。
        手机之后重连也不会再回放被清掉的行（replay_snapshot 读的是同一份缓存）；已经连着的手机页面
        不受影响，history_cleared 不在手机白名单里。
        返回是否认得这个 kind；认得时广播 history_cleared，让同时开着的其他本机页面也跟着清。"""
        if kind == "captions":
            self.history.clear()
        elif kind == "comments":
            self.comments.clear()
        else:
            return False
        await self.broadcast({"type": "history_cleared", "kind": kind})
        return True

    async def broadcast(self, msg):
        if msg.get("type") == "caption" and not msg.get("replay"):
            self.history.append(msg)
        elif msg.get("type") == "session_break" and not msg.get("replay"):
            # 和 caption 混进同一个 deque：顺序就是发生顺序，回放时分隔线
            # 落在它该在的两条字幕之间，不用另开一路时间线去对齐
            self.history.append(msg)
            self._last_break = msg
            # 新的一场开始（_begin_session 第一步就发它，早于这一场的任何 health）：上一场的落后提示作废。
            # 换主播走 quiet 停止、不发 idle，新一场的统计循环又从 ok 数起、等级不变就不广播——只能靠
            # 这一条清（专家审查 F3）。回放的分隔线是历史，不碰（上面的 not replay）
            self.config.pop("health", None)
        elif msg.get("type") == "health":
            # 识别落后提示落进 config：刷新或重连的页面从 hello 里拿到此刻的状态，手机从 replay_snapshot
            # 里拿（专家审查 F3）。以前不存：等级不变 pipeline 就不再广播，断线期间那条「已追上」没收到的
            # 页面，一直挂着旧的「识别开始落后」到这一场停止。level=ok 就是没有提示。
            # stats 里也带着按积压算的等级，但不拿它清：识别连续出错时 pipeline 有意让「检测已降级」
            # 挂着（出错的调用返回得快、积压归零，每跳 stats 都是 ok，见 Pipeline._announce_health），
            # 拿 stats 清会在 10 秒内把它收掉。
            # 这一场已经停了（status 是 idle/ended/error）之后晚到的一条不存，顺手把残留的也删掉：它说的是
            # 已经结束的那一场（比如下播之后、统计循环被取消之前的那一跳）。存下来的话，之后每次重连，
            # 手机都会在待机页上重建这条过时的落后提示。实时广播照发，页面自己挡：桌面的 renderHealth
            # 只在连接中/直播中显示，手机在 idle/ended/error 时不显示（web/viewer.js）
            status = self.config.get("status")
            state = status.get("state") if isinstance(status, dict) else None
            if msg.get("level") == "ok" or state in SESSION_OVER_STATES:
                self.config.pop("health", None)
            else:
                self.config["health"] = {k: msg[k] for k in ("level", "backlog_sec", "text") if k in msg}
        elif msg.get("type") == "caption_update":
            # 译文是后补的：历史里那条也要补上，否则重连回放会只剩原文
            # 只覆盖消息里带的键：重译的「pending/failed」通知不带 translated，
            # 整段照抄会把快译文抹成 None，下次重连回放这条就只剩西语
            for item in reversed(self.history):
                if item.get("id") == msg.get("id"):
                    for k, v in msg.items():
                        if k not in ("type", "id"):
                            item[k] = v
                    break
        elif msg.get("type") == "alert":
            self.alerts.append(msg)
        elif msg.get("type") == "alert_update":
            # 补进留存的那一条：报警面板刷新后要能看到中文，
            # 否则中控一刷新就只剩西语，等于没补
            for a in self.alerts:
                if a.get("alert_id") == msg.get("alert_id"):
                    for k, v in msg.items():   # failed/why 也要留：刷新后不能退回「翻译中…」
                        if k not in ("type", "alert_id"):
                            a[k] = v
                    break
        elif msg.get("type") == "comment" and not msg.get("replay"):
            self.comments.append(msg)
        elif msg.get("type") == "comment_source":
            # 落进 config：晚打开/刷新的页面从 hello 里拿到当前的抓取状态
            if "backend" in msg:
                self.config["comment_backend"] = msg.get("backend")
                self.config["comment_detail"] = msg.get("detail", "")
        elif msg.get("type") == "comment_update":
            # 译文是后补的：历史里那条也要补上，否则重连回放会只剩原文/pending
            for c in reversed(self.comments):
                if c.get("id") == msg.get("id"):
                    c["translated"] = msg.get("translated")
                    c["state"] = msg.get("state")
                    break
        elif msg.get("type") == "comment_guard":
            # 弹幕留意（PR-3）：标注变了才发。只覆盖缓存里那条弹幕的 guard 键，不碰 translated /
            # state（所以不借 comment_update：它会无条件把 translated 抹成 None），刷新后的回放带着新标注
            for c in reversed(self.comments):
                if c.get("id") == msg.get("id"):
                    if isinstance(msg.get("guard"), dict):
                        c["guard"] = msg["guard"]
                    else:
                        c.pop("guard", None)
                    break
        elif msg.get("type") == "guard_event":
            # 聚合后的事件：按 id 覆盖，最近更新的放最后，最多 GUARD_EVENTS_MAX 条；dismissed 是「忽略」
            ev = msg.get("ev")
            if isinstance(ev, dict) and ev.get("id"):
                events = self.config.setdefault("guard_events", {})
                key = str(ev["id"])
                events.pop(key, None)
                if not ev.get("dismissed"):
                    events[key] = ev
                while len(events) > self.GUARD_EVENTS_MAX:
                    events.pop(next(iter(events)))
        elif msg.get("type") == "guard_mark":
            # 关注名单的增删：只放当前主播（开场时 config 里的名单整份换掉）。缓存弹幕里不存 marked，
            # 页面按这里的 keys 现算高亮，移出后刷新才不会又亮回来
            key = str(msg.get("key") or "")
            if key:
                marked = self.config.setdefault("guard_marked", {})
                if isinstance(msg.get("entry"), dict):
                    marked[key] = msg["entry"]
                else:
                    marked.pop(key, None)
        elif msg.get("type") == "incident":
            # 持续提示：按 id 覆盖，level=clear 去掉；落进 config，刷新或重连的页面照样看得到。
            # 最多留 INCIDENTS_MAX 条，超出时丢最久没更新的那条
            key = str(msg.get("id") or "")
            if key:
                incidents = self.config.setdefault("incidents", {})
                if msg.get("level") == "clear":
                    incidents.pop(key, None)
                else:
                    now = msg.get("ts") if msg.get("ts") is not None else time.time()
                    prev = incidents.get(key)
                    incidents[key] = {"id": key, "level": msg.get("level") or "warn",
                                      "text": msg.get("text") or "",
                                      "since": prev["since"] if prev else now, "ts": now}
                    # 「我加好了，再查一次」（Pipeline._incident 的 recheck）：带着才留，刷新的页面照样画出按钮；
                    # 不带时和以前逐字节相同。手机同看的白名单（app/viewer.py）不放它过去
                    if msg.get("recheck"):
                        incidents[key]["recheck"] = True
                    while len(incidents) > INCIDENTS_MAX:
                        oldest = min(incidents.values(), key=lambda x: x["ts"])
                        incidents.pop(oldest["id"], None)
        elif msg.get("type") == "status":
            # command 必须一起留存：它常常是用户当下唯一的出路，
            # 刷新一下页面就没了的话，等于没给。
            self.config["status"] = {"state": msg.get("state"),
                                     "detail": msg.get("detail", ""),
                                     "command": msg.get("command")}
            # 结构化字段同理（见 status()）：带着才留，下一条不带的状态把它们一起换掉——下载完了、
            # 换了状态，旧的进度和路径不会留在 hello 里。没带时 config["status"] 和以前逐字节相同
            for key in self.STATUS_EXTRAS:
                if msg.get(key):
                    self.config["status"][key] = msg[key]
            # 停止、下播、出错：这一场的识别落后提示跟着作废（pipeline 停止时不补发 level=ok）。
            # 下播发的是 ended，不是 idle，漏了它，之后每次重连都会再显示上一场的落后（专家审查 F3）
            if msg.get("state") in SESSION_OVER_STATES:
                self.config.pop("health", None)
            hook = getattr(self, "on_status", None)
            if hook is not None:
                try:
                    hook(msg)
                except Exception as exc:       # 钩子里的任何问题都不许影响状态广播
                    print("[警告] 处理状态钩子失败: {}".format(exc))
        elif msg.get("type") == "config":
            self.config.update({k: v for k, v in msg.items() if k != "type"})
        # 手机同看：同步交棒，绝不 await。fanout 只做「白名单过滤 + put_nowait」，
        # 没有 I/O，耗时与观众数成正比且有上界。识别循环 await 的正是报警广播，
        # 任何观众侧的等待都会变成识别延迟，所以这里一个 await 都不能有；观众
        # 收不下就丢它自己（它会重连并拿到完整回放）。
        # 放在下面逐个控制页面的循环**之前**是刻意的：本机页面每个最多拖 2 秒，
        # 放在后面的话，一个卡死的本机页面会把手机端也一起拖慢。
        hub = getattr(self, "viewer", None)
        if hub is not None:
            try:
                hub.fanout(msg)
            except Exception as exc:      # 观众侧任何问题都不许影响本机界面
                print("[警告] 手机同看分发失败: {}".format(exc))
        # 发往本机页面之前的最后一步：界面文字按当前语言渲染（spec §2.2）。上面的缓存和手机
        # 拿的都是原消息（带 Bi）；中文时 out 就是 msg 本身，字节不变。同步、没有 await。
        # check_outbound 是测试里的运行时网，生产里第一行就返回
        i18n.check_outbound(msg)
        out = i18n.render(msg)
        # 正在回放的页面还不在 self.clients 里，广播够不着它：这里替它攒着，回放完补发（见 _ws）。
        # 同步、没有 await，和上面缓存的写入在同一个步骤里——所以一条广播要么在它的快照里，
        # 要么在它的 backlog 里，不会两头落空。getattr：个别测试用 __new__ 造的服务端没有这个字段
        float_ws = getattr(self, "_float_ws", None) or ()
        float_out = self._float_payload(msg, out) if float_ws else None
        # 没带 Origin 的连接拿去掉同看口令的那份；消息里没有同看链接时就是 out 本身
        keyless_ws = getattr(self, "_keyless_ws", None) or ()
        keyless_out = without_share_key(out) if keyless_ws else out

        def payload_for(ws):
            if ws in float_ws:
                return float_out
            return keyless_out if ws in keyless_ws else out

        replaying = getattr(self, "_replaying", None)
        if replaying:
            for ws, backlog in list(replaying.items()):
                if ws in float_ws and float_out is None:
                    continue         # 浮窗页不收这种消息
                if len(backlog) >= self.MAX_REPLAY_BACKLOG:
                    print("[警告] 本机页面卡在回放里，积压已达 {} 条，已断开（页面会自己重连）".format(len(backlog)))
                    self._drop_client(ws, (getattr(self, "_transports", None) or {}).get(ws),
                                      "replay_backlog", None)
                else:
                    backlog.append(payload_for(ws))
        # 逐个页面发，每个都有上限。以前是不限时的 await：一个不读消息的页面（浏览器
        # 冻结的后台标签、DevTools 断点、弹着 confirm 的 WebView2）把发送缓冲塞满后，
        # send_json 永远不返回——识别循环在等报警广播，于是识别整个停下、音频积压
        # 到上限被丢，而正常的窗口一直显示「直播中」。心跳救不了：它的关闭是优雅
        # 关闭，也在等那个永远发不完的缓冲区。
        transports = getattr(self, "_transports", None) or {}
        dead = []
        for ws in list(self.clients):
            is_float = ws in float_ws
            if is_float and float_out is None:
                continue
            transport = transports.get(ws)
            buffered = _write_buffer_size(transport)
            if buffered is not None and buffered > self.MAX_CLIENT_BUFFER:
                self._drop_client(ws, transport, "buffer_full", buffered)
                continue
            try:
                await asyncio.wait_for(ws.send_json(payload_for(ws)), self.SEND_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                self._drop_client(ws, transport, "send_timeout",
                                  _write_buffer_size(transport))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)
            transports.pop(ws, None)

    def _float_payload(self, msg, out):
        """这条广播发给浮窗页的样子；浮窗页不收这种消息就是 None。out 是已经按界面语言渲染过的那份。"""
        mtype = msg.get("type")
        if mtype not in FLOAT_TYPES:
            return None
        if mtype == "config":
            keys = self._float_config(out)
            return dict(keys, type="config") if keys else None
        return out

    def _drop_client(self, ws, transport, reason, buffered):
        """断开一个收不下消息的页面。用 abort 不用 close：close 要等缓冲区发完，
        对一个不读的页面永远等不完。页面会自己重连，hello 之后补回报警。"""
        registered = ws in self.clients
        is_float = ws in (getattr(self, "_float_ws", None) or ())
        self.clients.discard(ws)
        if is_float:
            self._float_ws.discard(ws)
        keyless = getattr(self, "_keyless_ws", None)
        if keyless is not None:
            keyless.discard(ws)
        replaying = getattr(self, "_replaying", None)
        if replaying is not None:
            replaying.pop(ws, None)
        transports = getattr(self, "_transports", None)
        if transports is not None:
            transports.pop(ws, None)
        if transport is not None:
            try:
                transport.abort()
            except Exception:
                pass
        # 两条并发的广播可能同时卡在同一个页面上：只报一次
        if is_float:
            # 浮窗页不显示报警：不写审计，也不说「会补回报警」；它会自己重连
            print("[警告] 浮窗页收不下消息（{}），已断开（会自动重连）".format(reason))
            return
        hook = self.on_client_dropped
        if registered and hook is not None:
            try:
                hook(reason, buffered)
            except Exception as exc:
                print("[警告] 记录界面断开失败: {}".format(exc))

    async def status(self, state, detail="", command=None, progress=None, paths=None):
        """command：给用户一条可以原样照做的命令（界面会渲染成可复制的一行）。

        存在的理由是一次真实的死局：更新器因为几个未跟踪文件拒绝更新，
        提示是「请自行处理后 git pull」——而修好这个判断的代码，只能靠更新
        送达。更新器把自己锁死了。所以任何一条「更新没能完成」的出口，都必须
        带上一条用户能照做的完整命令，哪怕程序本身已经帮不上忙。

        两个可选的结构化字段（只在给了时才进消息，没给的消息和以前逐字节相同），随 config["status"] 留存，
        hello 时重放：
          * progress：进度，如 {"kind": "model_download", "pct": 37, "mb": 580}（专家审查 U7）。页面中间那张
            「正在连接」卡片只认它，不去解析 detail 的文字；
          * paths：要原样复制的文件路径列表（专家审查 I8，「完全磁盘访问权限」那一步要加的解释器路径）。
            不复用 command：command 是可以原样照做的命令，路径不是命令。手机同看只转发 state 和 ts
            （app/viewer.py 的白名单），本机路径不出这台电脑。"""
        msg = {"type": "status", "state": state, "detail": detail, "ts": time.time()}
        if command:
            msg["command"] = command
        if progress:
            msg["progress"] = dict(progress)
        if paths:
            msg["paths"] = [str(p) for p in paths]
        await self.broadcast(msg)

    # status 消息里可选的结构化字段（见 status()）：config["status"] 留存它们，Pipeline._resend_status 原样再发
    STATUS_EXTRAS = ("progress", "paths")
