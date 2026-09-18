"""手机同看（局域网观众面）：从 app/pipeline.py 纯搬移出来，方法名/签名/方法体
逐字未改动。Pipeline 通过 ViewerShareMixin 继承这些方法，self.* 语义不变。
"""
import asyncio
from collections import deque

from .redact import strip_query
from .settings import load_settings

# 空闲期攒下的手机同看审计事件上限，与 app/viewer.VIEWER_AUDIT_PENDING_MAX 一致
VIEWER_AUDIT_PENDING_MAX = 100
VIEWER_IP_TIMEOUT_SEC = 3.0      # 取本机地址放执行器里跑，最多等这么久
VIEWER_STOP_TIMEOUT_SEC = 2.0    # 退出收尾时关同看的预算：不得拖住退出
# 手机点「重译」：一次只跑一条（见 _viewer_action_semaphore），排在后面的
# 用这个数上限，多出来的安静丢掉——不然一台手机连点十几下能把请求堆成一长队，
# 而这条队伍会一直占着强模型，跟违禁词报警抢显存
VIEWER_RETRANSLATE_QUEUE_MAX = 4


class ViewerShareMixin:
    # ---- 手机同看（见 app/viewer.py）----
    # 这一整块的三条不变式：
    #   1. 开/关/换链接全部在专用锁内串行，任何 await 之后都重新核对想要的终态
    #      ——并发的 on/off/on 不能留下一个谁也关不掉的监听端口；
    #   2. 文案只写观察到的事实和能做的事（CLAUDE.md 八，按 A8 修订）；
    #   3. 这里的任何失败都不许外抛到启动或退出路径上。
    def _viewer_lock(self):
        """专用锁，惰性建（3.9 的 asyncio.Lock 构造时就要绑事件循环）。
        刻意不复用 _stream_lock：开同看和开播互相等没有任何好处。"""
        if getattr(self, "_viewer_lock_obj", None) is None:
            self._viewer_lock_obj = asyncio.Lock()
        return self._viewer_lock_obj

    def _viewer_audit(self, event, **fields):
        """一条同看审计事件。没有 audit（还没开播）就先攒着，开播后由
        _flush_viewer_audit 按原顺序补写。无论有没有 audit 都打一行——
        沿用 on_ui_client_dropped 的做法，终端里也要看得见。"""
        shown = " ".join("{}={}".format(k, fields[k]) for k in sorted(fields)
                         if fields[k] is not None)
        print("[同看] {} {}".format(event, shown).rstrip())
        audit = getattr(self, "audit", None)
        if audit is None:
            pending = getattr(self, "_viewer_audit_pending", None)
            if pending is None:
                pending = deque(maxlen=VIEWER_AUDIT_PENDING_MAX)
                self._viewer_audit_pending = pending
            pending.append((event, dict(fields)))
            return
        self._write_viewer_audit(audit, event, fields)

    @staticmethod
    def _write_viewer_audit(audit, event, fields):
        method = getattr(audit, event, None)
        if method is None:
            return
        try:
            method(**fields)
        except Exception as exc:          # 审计写不进去不能影响同看本身
            print("[警告] 记录手机同看事件失败: {}".format(exc))

    def _flush_viewer_audit(self):
        """_begin_session 建好 self.audit 之后调一次：把空闲期攒下的事件按原顺序补写。"""
        audit = getattr(self, "audit", None)
        pending = getattr(self, "_viewer_audit_pending", None)
        if audit is None or not pending:
            return
        while pending:
            event, fields = pending.popleft()
            self._write_viewer_audit(audit, event, fields)

    def _make_viewer_hub(self, ports, token):
        """建一个观众面。单独一个方法是为了测试能注入替身。"""
        from .viewer import ViewerHub

        return ViewerHub(self.server, ports=ports, token=token,
                         audit_hook=self._viewer_audit,
                         on_action=self._on_viewer_action)

    def _viewer_action_semaphore(self):
        """手机发起的重译一次只跑一条。中控桌面那颗「重译」按钮不受影响——
        它走 self.retranslate() 本身，不经过这把锁。"""
        if getattr(self, "_viewer_action_sem_obj", None) is None:
            self._viewer_action_sem_obj = asyncio.Semaphore(1)
        return self._viewer_action_sem_obj

    def _on_viewer_action(self, action, payload, ip):
        """ViewerHub 的 on_action 回调：同步，不 await。校验和排队计数都在这儿
        做完；真正等强模型的部分是返回的协程，由 hub 用 ensure_future 调度，
        绝不堵住它读 socket 的循环。"""
        if action != "retranslate" or not isinstance(payload, dict):
            return None
        seq = payload.get("id")
        if isinstance(seq, bool) or not isinstance(seq, int):
            return None
        if seq not in self._recent:
            return None           # 程序自己都不认得这条字幕，不必往下传
        if self._viewer_action_pending >= VIEWER_RETRANSLATE_QUEUE_MAX:
            return None           # 排队已经够多了，安静丢掉这次点击
        self._viewer_action_pending += 1
        return self._run_viewer_action(seq, ip)

    async def _run_viewer_action(self, seq, ip):
        try:
            self._viewer_audit("viewer_action", action="retranslate", seq=seq, ip=ip)
            async with self._viewer_action_semaphore():
                await self.retranslate(seq, trigger="viewer")
        finally:
            self._viewer_action_pending -= 1

    async def _viewer_ip_info(self):
        """取本机局域网地址。getaddrinfo 会阻塞（DNS 配错时能卡上几秒），放执行器里
        跑并且限时——界面卡住比没出二维码严重得多。取不到就按「没读到地址」处理。"""
        from .viewer import lan_ipv4

        try:
            loop = asyncio.get_running_loop()
            return await asyncio.wait_for(loop.run_in_executor(None, lan_ipv4),
                                          VIEWER_IP_TIMEOUT_SEC)
        except Exception:
            return {"ip": None, "all": [], "source": None, "ambiguous": False}

    async def _publish_viewer(self, error=None, ports=(), ip_changed=None, ip_info=None):
        """把同看状态推给控制面并存进 config（刷新/晚开的页面从 hello 里取）。

        这条消息带着含 token 的 URL，所以 filter_payload 把 type="viewer" 显式拒了
        ——它只走 127.0.0.1 的控制面，永远到不了手机端。
        """
        from . import viewer as viewer_mod

        hub = getattr(self.server, "viewer", None)
        if hub is None or not hub.running:
            candidates = list(ports)
            if not candidates:
                candidates = viewer_mod.pick_ports(
                    getattr(self.server, "port", 8765),
                    load_settings().get("viewer_port"))
            state = viewer_mod.off_state(port=candidates[0] if candidates else None,
                                         error=error, ports=candidates)
        else:
            info = await self._viewer_ip_info() if ip_info is None else ip_info
            pinned = getattr(self, "_viewer_pinned_ip", None)
            if pinned and pinned in (info.get("all") or []):
                info = dict(info, ip=pinned)
            qr_rows = None
            url = hub.url(info.get("ip")) if info.get("ip") else None
            if url:
                # 整段包住：二维码坏掉绝不影响同看本身，只是要手工输入地址
                try:
                    from . import qr

                    qr_rows = qr.rows(qr.encode(url.encode("utf-8")))
                except Exception as exc:
                    print("[警告] 二维码没能生成（可以手工输入地址）: {}".format(exc))
                    qr_rows = None
            state = hub.state(ip_info=info, qr_rows=qr_rows, ip_changed=ip_changed)
            self._viewer_ip = state.get("ip")
        self.server.config["viewer"] = state
        await self.server.broadcast(dict(state, type="viewer"))
        return state

    async def _set_viewer_share(self, on):
        """中控点了「打开」/「关闭」。锁外先记下想要的终态，锁内复核——
        连着点三下 on/off/on 时，只有最后一条说的算，中间那几条不许留下监听端口。"""
        on = bool(on)
        self._viewer_want = on
        async with self._viewer_lock():
            if self._viewer_want != on:
                return          # 期间又来了新指令：由它落地，别来回开关端口
            if on:
                await self._start_viewer_locked("operator")
                if not self._viewer_want:
                    # start 里有 await，期间中控又点了关闭：别留下一个在监听的端口
                    await self._stop_viewer_locked("operator")
            else:
                await self._stop_viewer_locked("operator")

    async def _start_viewer_locked(self, reason):
        from . import viewer as viewer_mod

        hub = getattr(self.server, "viewer", None)
        if hub is not None and hub.running:
            await self._publish_viewer()
            return
        saved = load_settings()
        token = viewer_mod.sanitize_token(saved.get("viewer_token"))
        if token is None:
            # 手改坏了或首次打开：重新生成并存回。关闭同看**不**清 token——
            # 贴在墙上的二维码还要能用，只有「换一个链接」才换
            token = viewer_mod.new_token()
            self._save_setting("viewer_token", token)
        ports = viewer_mod.pick_ports(getattr(self.server, "port", 8765),
                                      saved.get("viewer_port"))
        hub = self._make_viewer_hub(ports=ports, token=token)
        try:
            await hub.start()
        except OSError as exc:
            # 观众端口刻意不漂到别的端口：已经发出去、贴在墙上的二维码会静默失效。
            # 只报观察（系统真的返回了什么）+ 能做的事，保持关闭
            self.server.viewer = None
            self._save_setting("viewer_share_enabled", False)
            self._viewer_audit("viewer_share", state="off",
                               port=ports[0] if ports else None, viewers=0,
                               reason="port_busy")
            await self._publish_viewer(error=strip_query(str(exc), 200), ports=ports)
            return
        self.server.viewer = hub
        self._viewer_pinned_ip = None
        self._save_setting("viewer_share_enabled", True)
        self._save_setting("viewer_port", hub.port)
        self._viewer_audit("viewer_share", state="on", port=hub.port, viewers=0,
                           reason=reason)
        await self._publish_viewer()
        self._start_viewer_ip_watch()

    async def _stop_viewer_locked(self, reason):
        hub = getattr(self.server, "viewer", None)
        self.server.viewer = None
        self._cancel_viewer_ip_watch()
        if reason != "shutdown":
            # 退出时不动这个开关：下次启动还要照中控最后的意思恢复
            self._save_setting("viewer_share_enabled", False)
        if hub is not None:
            port, viewers = hub.port, hub.count
            try:
                await hub.stop(reason)
            except Exception as exc:
                print("[警告] 关闭手机同看出错: {}".format(exc))
            self._viewer_audit("viewer_share", state="off", port=port,
                               viewers=viewers, reason=reason)
        if reason != "shutdown":
            await self._publish_viewer()

    async def _rotate_viewer_token(self):
        """「换一个链接」：换 token，在看的手机全部断开（要重新扫码）。"""
        from . import viewer as viewer_mod

        async with self._viewer_lock():
            token = viewer_mod.new_token()
            self._save_setting("viewer_token", token)
            hub = getattr(self.server, "viewer", None)
            viewers = hub.count if hub is not None else 0
            port = hub.port if hub is not None else None
            if hub is not None:
                hub.rotate(token)
            self._viewer_audit("viewer_share", state="rotate", port=port,
                               viewers=viewers, reason="operator")
            await self._publish_viewer()

    async def _pick_viewer_ip(self, ip):
        """中控在候选地址里点了一个。只接受确实在候选里的地址——界面传什么都不能
        直接拼进链接里。认不出就清掉，回到自动选的那个。"""
        async with self._viewer_lock():
            info = await self._viewer_ip_info()
            candidates = info.get("all") or []
            self._viewer_pinned_ip = ip if (
                isinstance(ip, str) and ip in candidates) else None
            await self._publish_viewer(ip_info=info)

    # 地址会变（换 Wi-Fi、插网线、VPN 上下线）。开着同看就每 60 秒重取一次：
    # 地址变了而二维码没换，中控只会看到手机「打不开」，而程序这边什么都不知道
    def _start_viewer_ip_watch(self):
        if getattr(self, "_viewer_ip_task", None) is not None:
            return
        self._viewer_ip_task = self._spawn(self._viewer_ip_watch())

    def _cancel_viewer_ip_watch(self):
        task = getattr(self, "_viewer_ip_task", None)
        self._viewer_ip_task = None
        if task is not None:
            task.cancel()

    async def _viewer_ip_watch(self):
        from . import viewer as viewer_mod

        try:
            while True:
                await asyncio.sleep(viewer_mod.IP_REFRESH_SEC)
                hub = getattr(self.server, "viewer", None)
                if hub is None or not hub.running:
                    return
                info = await self._viewer_ip_info()
                old, new = getattr(self, "_viewer_ip", None), info.get("ip")
                if new == old:
                    continue
                await self._publish_viewer(
                    ip_info=info, ip_changed=(old, new) if (old and new) else None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("[警告] 重取本机地址出错: {}".format(exc))

    async def restore_viewer_share(self):
        """启动时恢复上次的开关（main.py 调用）。缺这一项就是关闭——要求 1。
        任何异常都不外抛：启动不能被同看带崩。"""
        try:
            if not bool(load_settings().get("viewer_share_enabled")):
                await self._publish_viewer()     # 卡片要显示「关闭」这个状态
                return
            self._viewer_want = True
            async with self._viewer_lock():
                if not self._viewer_want:
                    return
                await self._start_viewer_locked("boot_restore")
        except Exception as exc:
            print("[警告] 恢复手机同看失败: {}".format(exc))

    async def stop_viewer_share(self, reason="shutdown"):
        """退出收尾：尽力关掉观众监听面。带自己的预算，任何失败都不许拖住退出。"""
        self._viewer_want = False
        try:
            await asyncio.wait_for(self._stop_viewer_share_inner(reason),
                                   VIEWER_STOP_TIMEOUT_SEC)
        except Exception as exc:
            print("[警告] 收尾时关闭手机同看出错: {}".format(exc))

    async def _stop_viewer_share_inner(self, reason):
        async with self._viewer_lock():
            await self._stop_viewer_locked(reason)

