"""本地翻译引擎的启动/备货（healing + pull）：从 app/pipeline.py 纯搬移出来，
方法名/签名/方法体逐字未改动。Pipeline 通过 EngineProvisionMixin 继承这些方法，
self.* 语义不变。

搬出来的原因：这 9 个方法（加 3 个只在这里用到的类常量 OLLAMA_HEAL_COOLDOWN_SEC /
LOCAL_ENGINES / _NOT_NOTED）从头到尾只读写 self._engine_pending /
self._pull_deferred / self._pull_deferred_session / self._pulling /
self._heal_at / self._provision_task 这一组状态，是一段前后连续、不跟别的
方法交叉的 259 行；与文件其余部分只通过既有的 self.* 通道打交道（self.args、
self.translator、self.server、self._spawn、self._provision_note、
self._provision_then_check、self._publish_engine、self._stream_active、
self._update_in_progress、self.run_selfcheck、self._selfcheck_task），继承
成 mixin 后这些调用原样成立，不用改一个字。

注意 3796 行前后另有一段「引擎切换/配额兜底」的状态（set_engine /
_keep_engine_until_model / _quota_fallback / _rejection_fallback 等），和这
里同属「本地引擎」这个大概念、但离得很远、还跟每段强模型重译的方法交叉夹在
一起，不能整块搬——那是另一步要做的事，这次不碰。
"""
import asyncio
import time


class EngineProvisionMixin:
    OLLAMA_HEAL_COOLDOWN_SEC = 60.0

    async def _heal_local_engine(self):
        """本地引擎在用、Ollama 却不通：后台把它拉起来，不阻塞调用方。

        开播那一刻和每次翻译失败都会来问一次。实录：Ollama 没跑时一场直播
        每句翻译 0.8 毫秒失败，界面只在第四条之后才提示「翻译服务可能连不上」，
        程序自己明明能启动它。节流一分钟一次；备模型任务在跑就不重复起。"""
        from . import localmodel

        if getattr(self.args, "translator", "auto") not in self.LOCAL_ENGINES:
            return
        if getattr(self.translator, "name", None) not in ("hymt2", "hymt2-7b", "gemma"):
            return
        now = time.monotonic()
        if now - getattr(self, "_heal_at", -1e9) < self.OLLAMA_HEAL_COOLDOWN_SEC:
            return
        task = getattr(self, "_provision_task", None)
        if task is not None and not task.done():
            return
        self._heal_at = now
        if await localmodel.is_running():
            return
        # is_installed 可能跑 Spotlight 查询（最坏 8 秒）：直播中在事件循环上跑，
        # 音频读取和报警广播会一起停住
        loop = asyncio.get_running_loop()
        if not await loop.run_in_executor(None, localmodel.is_installed):
            return
        await self.server.broadcast({
            "type": "notice", "text": "翻译引擎用的 Ollama 没在运行，正在自动启动…"})
        self._provision_task = asyncio.ensure_future(self._provision_then_check())

    # 需要 Ollama 的引擎 → 它要的模型、以及「本机有没有这个模型」的探测
    LOCAL_ENGINES = ("auto", "hymt2", "hymt2-7b", "gemma")

    async def ensure_local_translator(self):
        """开工前把本地翻译准备好，让用户不必为此开终端。

        没装 Ollama 的机器会退回 Google 免费接口——按 IP 限流，长时间监听经常
        整段翻译失败。以前的提示是「自己去装 Ollama，再敲一行 ollama pull」，
        对不会用终端的人等于永远用不上本地翻译。

        Ollama 装了没启动就帮他启动；启动了但没有模型就用 HTTP 接口拉下来
        （3GB 的 Whisper 模型我们本来就自动下，这个 1.1GB 是同一件事）。
        压根没装的只能引导——那一步需要管理员权限，代劳不了。

        用户显式选了 hymt2 / hymt2-7b / gemma 时**同样**要做这些：以前这里
        「用户指定了引擎就不自作主张」直接返回，于是选了本地 Hy-MT2 的机器
        Ollama 永远不会被启动，自检一直红着「Ollama 没在运行」、每句翻译
        0.8 毫秒失败——用户明明就是要这个引擎，把它跑起来才是不自作主张。

        **直播中不下载。** 下载和拉流抢同一条网络（7B 有 4.6 GB），而会话日志里以前
        不留下载的痕迹，事后一段音频中断对不上号。直播中发现缺模型只记下来、说一句，
        停止后由 _end_session 再来一遍；已经在跑的下载（比如启动时起的那个）不去动它。
        """
        from . import localmodel
        from . import translator as T

        engine = getattr(self.args, "translator", "auto")
        if engine not in self.LOCAL_ENGINES:
            return                      # deepl/google/claude/openai/none：不碰 Ollama
        loop = asyncio.get_running_loop()
        started = False
        if await localmodel.is_running():
            pass
        # is_installed 可能跑 Spotlight 查询（最坏 8 秒）：放线程池
        elif await loop.run_in_executor(None, localmodel.is_installed):
            print("[信息] Ollama 已安装但没在运行，正在启动…")
            if not await localmodel.start():
                print("[警告] Ollama 没能启动，本地翻译暂不可用")
                return
            started = True
        else:
            return                      # 没装：交给自检那一行去引导

        # 探测是同步 urllib，放线程池（直播中也可能走到这里）
        if engine == "auto":
            active = getattr(self.translator, "name", None)
            if active in ("hymt2", "gemma"):
                # 正在用的那个本地模型本身得在。以前只要本机还有随便哪个本地模型就算
                # 「有」：1.8B 被 ollama rm 掉而 7B 还在时，既不重新下载也不重建引擎，
                # 每句 404 到本场结束
                has = self._local_wanted(active)[1]
            else:
                def has():
                    # 和 _local_wanted 同一个判据：按生成时真正用的名字精确比
                    return T._ollama_has_model(*(T.local_engine_model(e)
                                                 for e in ("hymt2", "hymt2-7b", "gemma")))
            need = None if await loop.run_in_executor(None, has) \
                else T.local_engine_model("hymt2")
        else:
            model, has = self._local_wanted(engine)
            need = None if await loop.run_in_executor(None, has) else model

        pulled = False
        if need is not None:
            if need in self._pulls_running():
                # 已经有一个下载在跑（比如启动时起的那个）：不再起第二个，也不推迟它；
                # 只在本场日志里留个记号，事后能把这段时间的网络占用对上号
                self._audit_pull("in_progress", need)
            elif self._stream_active() or self._update_in_progress():
                await self._defer_pull(need)
            else:
                pulled = await self._pull_model(need)

        if engine == "auto" and (started or pulled):
            # 启动时 Ollama 还没起来，auto 已经落到了 Google；现在本地模型能用了
            self.translator = await loop.run_in_executor(
                None, create_translator, "auto")
            await self._publish_engine()
        elif pulled and getattr(self, "_engine_pending", None) == engine:
            await self._apply_pending_engine(engine)
        if pulled:
            await self._provision_note("本地翻译已就绪，可以开始了。")
        if started or need is not None:
            # 启动时的自检和这里是并行跑的，那一行多半是在 Ollama 起来之前
            # 查的：等它跑完再查一遍，把红条刷掉
            pending = getattr(self, "_selfcheck_task", None)
            if pending is not None and pending is not asyncio.current_task() \
                    and not pending.done():
                try:
                    await pending
                except Exception:
                    pass
            await self.run_selfcheck()

    _NOT_NOTED = object()

    @staticmethod
    def _local_wanted(engine):
        """本地引擎 → (它生成时真正调用的模型, 本机有没有**这一个**模型的同步探测)。

        和自检（selfcheck.check_translator）同一个判据：按名字精确比（model_listed），
        环境变量改过的模型名也照认。以前这里按子串认——Ollama 里只有 translategemma:12b、
        或 1.8B 只有别的量化档时，自检红着说「会自动下载」，这里却当作有、一个字节都不下，
        set_engine 也照换不误，每句 404。探测在调用时才去 translator 模块里取，测试替换
        得到。不是本地引擎时模型是 None。"""
        from . import translator as T

        model = T.local_engine_model(engine)
        if model is None:
            return None, lambda: True
        return model, lambda: T._ollama_has_model(model)

    def _pulls_running(self):
        """正在下载的模型名（进程级集合：一次下载可能跨场次）。"""
        running = getattr(self, "_pulling", None)
        if running is None:
            running = self._pulling = set()
        return running

    def _audit_pull(self, state, model, error=None):
        """下载本身跨场次，记进事件发生这一刻正开着的那场审计；没有开着的就不记。"""
        audit = getattr(self, "audit", None)
        if audit is not None:
            audit.model_pull(state, model, error)

    async def _defer_pull(self, need):
        """直播中（或一键更新进行中）缺模型：不下载，记下来之后再下；同一场只说一次。"""
        from .translator import model_label

        audit = getattr(self, "audit", None)
        if getattr(self, "_pull_deferred", None) == need \
                and getattr(self, "_pull_deferred_session", self._NOT_NOTED) is audit:
            return
        self._pull_deferred = need
        self._pull_deferred_session = audit
        self._audit_pull("deferred", need)
        when = "更新结束后" if self._update_in_progress() else "停止后"
        text = "本地翻译模型 {} 还没下载，{}自动下载".format(model_label(need), when)
        print("[信息] " + text)
        await self._provision_note(text)

    async def _pull_model(self, need):
        """真的去下载（调用方已确认不在直播）。进度和结果都告诉界面；失败时带上
        Ollama 的原话，替换掉停在半截的百分比。返回是否成功。"""
        from . import localmodel
        from .translator import HYMT2_SMALL, engine_label

        size = "约 1.1 GB，" if need == HYMT2_SMALL else "首次需要下载，"
        await self._provision_note(
            "正在准备本地翻译模型（{}只需这一次）…".format(size))
        last = [-10.0]
        finished = [False]

        async def note(text):
            if not finished[0]:         # 下载结束后才轮到的进度，别盖掉结果
                await self._provision_note(text)

        def progress(pct, done_mb, total_mb):
            if pct - last[0] < 5:       # 别把界面刷爆
                return
            last[0] = pct
            self._spawn(note(
                "正在下载本地翻译模型：{:.0f}%（{:.0f} / {:.0f} MB，"
                "只需这一次）…".format(pct, done_mb, total_mb)))

        running = self._pulls_running()
        running.add(need)
        self._pull_deferred = None      # 推迟的那一次就是这一次
        self._audit_pull("start", need)
        try:
            result = await localmodel.pull(need, on_progress=progress)
        finally:
            finished[0] = True
            running.discard(need)
        ok, error = result if isinstance(result, tuple) else (bool(result), None)
        if ok:
            print("[信息] 本地翻译模型已就绪")
            self._audit_pull("done", need)
            return True
        error = error or "Ollama 没有给出说明"
        print("[警告] 本地翻译模型下载失败：{}".format(error))
        self._audit_pull("failed", need, error)
        current = getattr(self.translator, "name", None)
        keep = "继续用" + engine_label(current) if current else "继续不翻译"
        await self._provision_note(
            "本地翻译模型下载失败：{}；{}，下一场停止后会自动再试".format(error[:120], keep))
        return False

    async def _apply_pending_engine(self, engine):
        """界面上选了一个当时本机还没有模型的本地引擎：模型下好之后才换上它。"""
        loop = asyncio.get_running_loop()
        try:
            new = await loop.run_in_executor(None, create_translator, engine)
        except Exception as exc:
            print("[警告] 换用 {} 失败: {}".format(engine, exc))
            return
        if getattr(self.args, "translator", None) != engine \
                or getattr(self, "_engine_pending", None) != engine:
            try:                        # 下载期间用户又换了别的引擎：以后来的选择为准
                await new.close()
            except Exception:
                pass
            return
        old, self.translator = self.translator, new
        self._engine_pending = None
        self.args.translator_note = None
        if old is not None and old is not new:
            try:
                await old.close()
            except Exception:
                pass
        await self._publish_engine()

    async def _pull_after_session(self, session_task):
        """补上直播中推迟的模型下载。_end_session 跑在直播任务自己里面，那一刻
        _stream_active() 还是真：等这个任务真正结束再动手。这期间又开了新的一场，
        ensure_local_translator 会自己再推迟一次。"""
        try:
            if session_task is not None and session_task is not asyncio.current_task():
                await asyncio.wait({session_task})
            if self._update_in_progress():
                return          # 一键更新停掉的这一场：更新收尾时再补（见 _apply_update）
            if getattr(self, "_pull_deferred", None) and not self._stream_active():
                await self.ensure_local_translator()
        except Exception as exc:
            print("[警告] 停止后补下载本地翻译模型失败: {}".format(exc))

