# i18n: done
"""浮窗（app/float_window.py）和管线之间的接线：只管「开 / 关 / 停止就关、自己停了留下结束卡片 / 把状态告诉页面」。

浮窗只做显示：这里不碰识别、检测、审计、翻译。窗口宿主（FloatWindowHost）只有桌面应用模式才有，
由 main.py 在开窗口之前通过 attach_float_host 挂上来；--browser 模式、命令行模式没有宿主，
config 里连 float_window 这个键都没有，页面据此不画顶栏按钮。

线程：宿主的 on_change 回调来自 GUI / 工作线程，用 run_coroutine_threadsafe 回到事件循环里广播；
server.on_status 钩子在事件循环里同步调用，只做一个判断和（必要时）一次不阻塞的 request_close
或一次不阻塞的 Dock 提醒（交给主线程做，见 app/window_attention.request_attention）。
"""
import asyncio

# 这两个状态以外，顶栏的「换主播」按钮就收起来了（web/follow.js viewTransition）；浮窗跟它同一条规则
ACTIVE_STATES = ("live", "connecting")
# 监听自己停下来的两种状态：下播（ended）、出错（error）。浮窗不关，页面换成结束卡片（web/float.js
# floatEndView），Dock 图标跳一次（专家审查 U3）。ended 不只是「主播下播」：等了一阵一直没开播、直连地址拉不到
# 数据也发 ended；卡片标题和主窗口状态胶囊是同一句「直播已结束」，只说这场监听结束在 ended 这个状态上。
# idle 什么时候关窗、什么时候不关，见 _float_on_status
END_STATES = ("ended", "error")


class FloatWindowMixin:
    def _init_float_window(self):
        self._float_host = None
        self._float_loop = None
        self._float_last_state = None       # 上一条 status 的 state：只在「从监听中停下来」那一下提醒一次
        self._float_card = False            # 结束卡片露着：从 ended / error 起，到下一场 connecting / live 为止

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
        """CaptionServer.broadcast 的 status 分支同步调到这里（在发给页面之前）。只看 state，不读 _stop_reason：

        - live / connecting（换主播、更新暂停、断流重连都是 connecting）：窗口留着；结束卡片收起；
        - ended / error：监听自己停了。以前浮窗跟着直接消失，中控看着浮窗、主窗口被盖着时，什么提醒都没有
          （专家审查 U3）。现在窗口留着，页面收到同一条 status 换成结束卡片（「回到主窗口」按钮走 js_api，
          见 app/float_window.FloatApi）；从监听中停下来的那一下 Dock 图标跳一次；
        - idle：卡片没露着时照旧关窗。中控点停止会先后发两条 idle（「正在停止…」和停完的那条），两条都只是
          关窗，不会翻出结束卡片；一键更新暂停了监听、更新又没成也没地址可恢复时（app/pipeline.py
          _resume_after_update_failure）同样是关窗；
        - idle，但结束卡片露着：窗口留着，卡片也留着（页面 web/float.js 同一条规则）。这时来的 idle 不是中控点
          停止——停止按钮只在连接中 / 直播中才有（web/follow.js viewTransition）——而是别的事顺带把状态写回了
          待机：直播中推迟的本地翻译模型下载在这场结束后开始、每 5% 报一次进度（app/pipeline.py
          _provision_note），连续查不到流地址后 yt-dlp 自动更新完、状态是 error 时提示「请重新点开始」
          （app/updater.py freshen_ytdlp），在主窗口里点了一键更新、没在监听时的那几种没更新成的出口
          （app/updater.py _refuse / _not_updated / _update_failed）。以前这几条会把卡片和窗口一起关掉，
          中控又回到 U3 那样「浮窗没说明就没了」；卡片说的「详情见主窗口」照旧成立；
        - 别的（认不出的状态）：照旧关窗，卡片也不留。

        为什么不读 _stop_reason：_stop_locked 先把它清空、最后才发那条停完的 idle，读它的话第二条 idle 会被
        当成「意外结束」（修复计划审查 P3）；只看 state 也不受停止原因怎么改的影响。"""
        host = getattr(self, "_float_host", None)
        if host is None:
            return
        state = msg.get("state")
        last = getattr(self, "_float_last_state", None)
        self._float_last_state = state
        if state in ACTIVE_STATES:
            self._float_card = False
            return
        if state in END_STATES:
            self._float_card = True
            # 只在从监听中（live / connecting）停下来时提醒：待机时点开始、地址不合格的那条 error 不算，
            # 同一场接连两条结束状态也只跳一次。浮窗开没开都跳：主窗口以外总得有个地方看得出来
            if last in ACTIVE_STATES:
                self._float_attention()
            return
        if state == "idle" and getattr(self, "_float_card", False):
            return
        self._float_card = False
        host.request_close()

    def _float_attention(self):
        """Dock 图标跳一次（macOS；程序在前台时系统什么都不做）。只有窗口模式挂了浮窗宿主才走到这里。
        报警那一路（窗口标题、系统通知）不经过这里，行为不变。"""
        from . import window_attention
        window_attention.request_attention()

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
