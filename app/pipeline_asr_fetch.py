# i18n: done
"""待机时提前下载语音识别模型：自检「语音识别」一行的「现在下载」。Pipeline 通过 AsrPrefetchMixin
继承这些方法（写法同 app/pipeline_engine.py）。

为什么有它：2026-10-29 用户在新 Mac 上第一次真正开播。新装机第一次点「开始翻译」，要先拿到流地址，
然后 _run_session 才去加载识别模型——large-v3 约 3.1 GB 就是在这时候下载的，直播已经开始了，字幕要等它下完。
自检知道模型还没下，可以前只写「首次开播需先下载模型」，没写多大，也没有能点的地方；整个程序没有提前
下载的入口。翻译模型（Ollama，约 1.1 GB）启动时已经会在后台自己下（pipeline_engine.ensure_local_translator），
识别模型这里**不自动下**：只在中控点了「现在下载」时下，不改默认模型。

规则：
- 只在待机时下。连接中、直播中（_stream_active）、一键更新进行中（_update_in_progress）收到指令一律不下：
  下载和拉流抢同一条网络（与 ensure_local_translator「直播中不下载」同一个道理）；
- 已经在下、已经下全时不再起一份；下之前先看磁盘剩余空间；
- 只下文件（app/asr.py fetch_model），不加载进内存或显存；
- 进度复用 _model_download_progress 的轮询，待机时走 _provision_note（status idle）；
- 下载还没完时点了「开始翻译」：_run_session 在加载模型之前先等这一份（_settle_asr_fetch，照
  _settle_fallback_load 的做法），不并行起第二份下载；
- 下完（或失败）刷新自检「语音识别」那一行（_refresh_asr_check）。终端只打中文。

与文件其余部分只通过既有的 self.* 通道打交道：self.args、self.server、self._spawn、self._provision_note、
self._stream_active、self._update_in_progress、self._model_download_progress、self._refresh_asr_check。
状态只有 self._asr_fetch 一个字段，按 getattr(self, name, None) 惰性创建（测试里 Pipeline.__new__
造的半成品实例也认）。
"""
import asyncio

from .i18n import L, bimap, of


class AsrPrefetchMixin:
    # 下载时磁盘上除了模型本身还要多留这么多（MB）：HF 先写 .incomplete 再挪进 blobs，审计日志和识别音频
    # 片段也在同一块盘上
    ASR_FETCH_SPARE_MB = 1000

    def _asr_fetching(self):
        """识别模型的提前下载是不是还没完。线程里的下载取消不掉：发起它的任务被取消了，线程还在下也算。"""
        fetch = getattr(self, "_asr_fetch", None)
        if fetch is None:
            return False
        fut = fetch.get("future")
        if fut is not None and not fut.done():
            return True
        if fetch.get("settled"):
            return False
        task = fetch.get("task")
        return task is not None and not task.done()

    def _request_asr_prefetch(self):
        """控制页的 download_asr_model（handle_control）。不等结果：几 GB 的下载放后台跑，不在这个页面的消息循环
        里等（同 apply_update）。只有本机页面发得出来：手机入站只认 retranslate（app/viewer.py
        _handle_inbound），浮窗页的入站一律丢弃（app/server.py）。返回 None。

        没起下载就返回的几条路也重发那一行（_refresh_asr_check）：页面一点就把按钮置灰成「正在下载…」
        （web/app.js asrFetchButton），只有服务端重发这一行才把它放回来。不重发，按钮就一直灰着，点不了第二次。"""
        if self._stream_active() or self._update_in_progress():
            # 按钮在这些时候是灰的；还能收到，多半是页面停在旧状态。不下，只在终端留一句
            print("[信息] 正在连接、监听或更新，这次不下载识别模型；停止后再点「现在下载」")
            self._spawn(self._refresh_asr_check())
            return None
        if self._asr_fetching():
            print("[信息] 识别模型已经在下载了，不再起一份")
            return None
        from .asr import asr_plan
        try:
            plan = asr_plan(self.args)
        except Exception as exc:
            print("[警告] 没能确定要下载哪个识别模型：{}".format(exc))
            self._spawn(self._refresh_asr_check())
            return None
        # 同步记下来再起任务：同一拍里连点两下，第二下就看得到第一下
        fetch = self._asr_fetch = {"backend": plan["backend"], "model": plan["model"],
                                   "task": None, "future": None, "settled": False}
        fetch["task"] = self._spawn(self._prefetch_asr_model(fetch))
        return None

    async def _prefetch_asr_model(self, fetch):
        try:
            outcome = await self._fetch_asr_once(fetch)
        finally:
            fetch["settled"] = True
        if outcome == "done":
            print("[信息] 识别模型 {} 已下载".format(fetch["model"]))
            if not self._asr_session_busy():    # 已经开播了：「可以开始了」这句不对，开播那边自己在说
                await self._provision_note(L("语音识别模型已下载，可以开始了。",
                                             "The speech model is downloaded. You can start now."))
        # 下完：那一行变绿、按钮没了；失败、磁盘不够没下：按钮回来。磁盘不够那条也要重发——那句话叫中控「腾出
        # 空间后再点」，页面上的按钮却还是点击时置灰的「正在下载…」，不重发就再也点不了
        await self._refresh_asr_check()

    async def _fetch_asr_once(self, fetch):
        """真的去下。返回 "cached"（本来就下全了）/ "refused"（磁盘不够，没下）/ "done" / "failed"。"""
        from . import asr, diskspace, selfcheck

        backend, model = fetch["backend"], fetch["model"]
        loop = asyncio.get_running_loop()
        try:
            if await loop.run_in_executor(None, selfcheck._model_cached, model, backend):
                print("[信息] 识别模型 {} 已经下载过，不再下载".format(model))
                return "cached"
            size_mb = asr.MODEL_SIZES_MB.get(model)
            free = await loop.run_in_executor(None, diskspace.free_bytes, diskspace.hf_hub_dir())
            if size_mb and free is not None and free < (size_mb + self.ASR_FETCH_SPARE_MB) * 10 ** 6:
                text = L("磁盘剩余 {:.1f} GB，语音识别模型 {} 约 {:.1f} GB，下载时还要多留 {} GB，这次没有下载。"
                         "腾出空间后再点「现在下载」。",
                         "{:.1f} GB of storage is available. Speech model {} needs about {:.1f} GB, plus "
                         "{} GB to spare while downloading, so it wasn’t downloaded. Free up some space, "
                         "then click Download Now again.").format(
                    free / 10 ** 9, model, size_mb / 1000, self.ASR_FETCH_SPARE_MB // 1000)
                print("[警告] " + text)
                await self._provision_note(text)
                return "refused"
            await self._refresh_asr_check()          # 那一行转成「正在下载」，按钮置灰
            size = selfcheck._size_text(size_mb)
            await self._provision_note(
                (L("正在下载语音识别模型 {}（{}，只需这一次）…",
                   "Downloading speech model {} ({}, only needed once)…").format(model, size) if size
                 else L("正在下载语音识别模型 {}（只需这一次）…",
                        "Downloading speech model {} (only needed once)…").format(model)))
            print("[信息] 开始提前下载识别模型：{}/{}".format(backend, model))
            fetch["future"] = loop.run_in_executor(None, asr.fetch_model, backend, model)
            watcher = asyncio.ensure_future(
                self._model_download_progress(model, report=self._asr_fetch_reporter()))
            try:
                # shield：这个任务被取消（关程序）时不连带取消线程里的下载——反正也取消不掉
                await asyncio.shield(fetch["future"])
            finally:
                watcher.cancel()
            return "done"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("[警告] 识别模型没能提前下载完：{}".format(exc))
            await self._provision_note(
                L("语音识别模型没能下载完（{}）。可以再点一次「现在下载」；不点的话，第一次点「开始翻译」时才下载。",
                  "Couldn’t finish downloading the speech model. Click Download Now to try again. Otherwise "
                  "it downloads the first time you click Start. Details: {}").format(
                    bimap(lambda s: s[:160], of(exc))))
            return "failed"

    def _asr_session_busy(self):
        """已经在连接或监听：直播任务在跑，或者状态还停在 connecting / live。这时提前下载这边不再往待机状态里
        写进度和「可以开始了」——_provision_note 在这里会发 status=idle，把界面从「正在连接」拽回待机。"""
        state = ((getattr(self.server, "config", None) or {}).get("status") or {}).get("state")
        return self._stream_active() or state in ("connecting", "live")

    def _asr_fetch_reporter(self):
        """_model_download_progress 的回调：待机时每涨 5%（大小未知时每 100 MB）走一次 _provision_note。
        开播之后不发：开播那边（_settle_asr_fetch）有自己的进度，这边再发就是每 3 秒一条 notice。"""
        last = {"pct": -100, "mb": -1000}

        async def report(text, pct, mb):
            if self._asr_session_busy():
                return
            if pct is not None:
                if pct - last["pct"] < 5:
                    return
                last["pct"] = pct
            elif mb - last["mb"] < 100:
                return
            else:
                last["mb"] = mb
            await self._provision_note(text)

        return report

    async def _settle_asr_fetch(self):
        """开播时（_run_session 加载模型之前）：提前下载还没完就先等它下完，再按配置加载——加载命中缓存，不再
        下一遍；不并行起第二份下载。下载失败了也照常往下走：create_transcriber 自己会下（以前的路）。
        这一场被停止时不取消下载（asyncio.wait 不取消它等的东西）。"""
        fetch = getattr(self, "_asr_fetch", None)
        if fetch is None or not self._asr_fetching():
            return
        model = fetch.get("model")
        await self.server.status(
            "connecting", L("语音识别模型 {} 还在下载，等它下完再开始识别…",
                            "Speech model {} is still downloading. Recognition starts when it finishes…")
            .format(model))
        watcher = asyncio.ensure_future(self._model_download_progress(model))
        try:
            task = fetch.get("task")
            if task is not None and not task.done():
                await asyncio.wait({task})
            fut = fetch.get("future")
            if fut is not None and not fut.done():
                await asyncio.wait({fut})
        finally:
            watcher.cancel()
