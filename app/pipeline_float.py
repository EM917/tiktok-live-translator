# i18n: done
"""浮窗（app/float_window.py）和管线之间的接线：只管「开 / 关 / 直播结束就关 / 把状态告诉页面」。

浮窗只做显示：这里不碰识别、检测、审计、翻译。窗口宿主（FloatWindowHost）只有桌面应用模式才有，
由 main.py 在开窗口之前通过 attach_float_host 挂上来；--browser 模式、命令行模式没有宿主，
config 里连 float_window 这个键都没有，页面据此不画顶栏按钮。

线程：宿主的 on_change 回调来自 GUI / 工作线程，用 run_coroutine_threadsafe 回到事件循环里广播；
server.on_status 钩子在事件循环里同步调用，只做一个判断和（必要时）一次不阻塞的 request_close。
"""
import asyncio

# 这两个状态以外，顶栏的「换主播」按钮就收起来了（web/follow.js viewTransition）；浮窗跟它同一条规则
ACTIVE_STATES = ("live", "connecting")


class FloatWindowMixin:
    def _init_float_window(self):
        self._float_host = None
        self._float_loop = None

    def attach_float_host(self, host, loop=None):
        """main.py（窗口模式）在开主窗口之前调：之后 hello 里就带 float_window，页面才会画按钮。
        loop 是事件循环，宿主在别的线程里改状态时靠它回到循环里广播。"""
        self._float_host = host
        self._float_loop = loop
        host.on_change = self._float_changed
        self.server.config["float_window"] = {"available": True, "open": bool(host.is_open())}
        self.server.on_status = self._float_on_status

    def detach_float_host(self):
        """窗口没开成、退回浏览器时调：这个功能不存在了，hello 里也不再带 float_window。"""
        self._float_host = None
        self.server.config.pop("float_window", None)
        self.server.on_status = None

    def _set_float_window(self, on):
        """顶栏「浮窗」按钮的开 / 关。不阻塞：宿主在工作线程里建窗，结果经 on_change 广播。"""
        host = getattr(self, "_float_host", None)
        if host is None:
            return None             # 浏览器 / 命令行模式：没有这个功能，忽略
        if on:
            if not self._stream_active():
                return None         # 按钮只在监听中才有；残留的点击不该在待机时开出一个空窗口
            host.request_open()
        else:
            host.request_close()
        return None

    def _float_on_status(self, msg):
        """CaptionServer.broadcast 的 status 分支同步调到这里：监听停了、结束了、出错了，浮窗跟着关。"""
        host = getattr(self, "_float_host", None)
        if host is None or msg.get("state") in ACTIVE_STATES:
            return
        host.request_close()

    def _float_changed(self, is_open, error=None):
        """宿主的状态变了（在任意线程里调）：回到事件循环里广播。"""
        loop = getattr(self, "_float_loop", None)
        if loop is None:
            return
        coro = self._publish_float(error)
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            coro.close()            # 循环已经关了（正在退出）：丢掉，别留下一个没人 await 的协程

    async def _publish_float(self, error=None):
        host = getattr(self, "_float_host", None)
        if host is None:
            return
        try:
            state = {"available": True, "open": bool(host.is_open())}
            self.server.config["float_window"] = state
            await self.server.broadcast({"type": "config", "float_window": dict(state)})
            if error:
                await self.server.broadcast({"type": "notice", "text": error})
        except Exception as exc:
            print("[警告] 发布浮窗状态失败：{}".format(exc))
