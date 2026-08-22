"""管线编排：拉流 → (降噪) → 切段 → 语音识别 → 翻译 → 广播给 UI。

支持两种启动方式：命令行传直播间地址，或在网页 UI 里输入地址点「开始」。
同一时间只跑一个直播间；切换房间时旧任务被取消，识别模型跨房间复用不重复加载。
"""
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .translator import create_translator

DENOISE_MODEL = Path(__file__).resolve().parent.parent / "models" / "bd.rnnn"
RNNOISE_URL = ("https://raw.githubusercontent.com/GregorR/rnnoise-models/master/"
               "beguiling-drafter-2018-08-30/bd.rnnn")

# 演示模式的内置台词（英文原文 + 中文译文），用于在没有直播时验证 UI / 浏览器插件
DEMO_SCRIPT = [
    ("Hey everyone, welcome back to my live stream!", "嘿大家好，欢迎回到我的直播间！"),
    ("If you're new here, don't forget to tap the follow button.", "如果你是新来的，别忘了点一下关注按钮。"),
    ("Today I'm going to show you guys something really special.", "今天我要给大家展示一个特别的东西。"),
    ("Thank you so much for the rose, I really appreciate it!", "非常感谢你送的玫瑰，真的很感谢！"),
    ("Let me read the comments real quick... okay, interesting question.", "让我快速看一下评论……好，这个问题很有意思。"),
    ("This product is handmade, and it took me about three days to finish.", "这个产品是纯手工做的，我花了大概三天才完成。"),
    ("We just hit one thousand viewers, that's amazing!", "我们刚刚突破一千名观众，太棒了！"),
    ("I'll do a giveaway when we reach two thousand likes.", "点赞到两千的时候我会做一次抽奖。"),
    ("Alright, let's get started with today's main event.", "好的，让我们开始今天的重头戏吧。"),
    ("Don't forget you can ask me anything in the chat.", "别忘了你可以在聊天区随便问我问题。"),
]


class Pipeline:
    def __init__(self, args, server):
        self.args = args
        self.server = server
        self.target = args.target
        self.translator = create_translator(args.translator)
        self.server.config["target_lang"] = self.target
        self._counter = 0
        self._asr_pool = ThreadPoolExecutor(max_workers=1)
        self._stream_task = None
        self._transcriber = None
        self._transcriber_key = None

    # ---- 来自 UI 的控制消息 ----
    def handle_control(self, msg):
        mtype = msg.get("type")
        if mtype == "set_target":
            value = str(msg.get("value", ""))[:12]
            if value:
                self.target = value
                return self.server.broadcast({"type": "config", "target_lang": value})
        elif mtype == "start":
            url = str(msg.get("url", "")).strip()
            source = str(msg.get("source", "") or "").strip()
            # UI 只允许网络地址（本地 CLI 不受此限制）
            if url.startswith("http://") or url.startswith("https://"):
                if source and source != "auto":
                    self.args.source = source
                elif source == "auto":
                    self.args.source = None
                return self.start_stream(url)
        elif mtype == "stop":
            return self.stop_stream()
        elif mtype == "apply_update":
            if getattr(self, "updater", None) is not None:
                return self._apply_update()
        elif mtype == "check_update":
            if getattr(self, "updater", None) is not None:
                return self.updater.check_and_notify(delay=0, manual=True)
        return None

    async def _apply_update(self):
        await self.stop_stream(quiet=True)
        await self.updater.apply()

    # ---- 直播任务管理 ----
    async def start_stream(self, url):
        await self.stop_stream(quiet=True)
        self.server.config["room_url"] = url
        await self.server.broadcast({"type": "config", "room_url": url})
        self._stream_task = asyncio.create_task(self._run_stream(url))

    async def stop_stream(self, quiet=False):
        task = self._stream_task
        self._stream_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if not quiet:
            await self.server.status("idle", "已停止。输入直播间地址可重新开始。")

    # ---- 实际的直播管线 ----
    async def _run_stream(self, url):
        """外层兜底：任何未预料的异常都要反映到 UI，绝不无声卡死在「直播中」。"""
        try:
            await self._run_stream_inner(url)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("[错误] 直播管线异常: {}".format(exc))
            try:
                await self.server.status("error", "内部错误，已停止：{}".format(exc))
            except Exception:
                pass

    async def _run_stream_inner(self, url):
        from .asr import create_transcriber
        from .audio import FFmpegAudioSource
        from .resolver import ResolveError, resolve_stream_url
        from .segmenter import SilenceSegmenter

        await self.server.status("connecting", "正在解析直播流地址…")
        try:
            media = await resolve_stream_url(url, cookies=self.args.cookies)
        except ResolveError as exc:
            await self.server.status("error", str(exc))
            print("[错误] {}".format(exc))
            return

        loop = asyncio.get_running_loop()
        # 未显式指定的参数用硬件推荐补齐。注意：backend/model/device/compute 是联动
        # 整体，用户锁定 backend/device 时其余字段围绕它重新推导（见 hwdetect.py）
        from .hwdetect import recommend
        rec = recommend(backend=self.args.backend, device=self.args.device)
        backend = rec["backend"]
        model = self.args.model or rec["model"]
        device = rec["device"]
        compute = (self.args.compute_type if self.args.compute_type != "auto"
                   else rec["compute_type"])
        print("[信息] 识别配置: backend={} model={} device={} ({})".format(
            backend, model, device, rec["note"]))

        key = (backend, model, device, compute, self.args.source,
               self.args.beam, self.args.no_context)
        if self._transcriber is None or self._transcriber_key != key:
            await self.server.status(
                "connecting", "正在加载语音识别模型（首次运行需要下载，可能要几分钟）…")
            try:
                transcriber = await loop.run_in_executor(
                    None,
                    lambda: create_transcriber(
                        backend=backend,
                        model_size=model,
                        device=device,
                        compute_type=compute,
                        language=self.args.source,
                        beam_size=self.args.beam,
                        use_context=not self.args.no_context,
                    ),
                )
            except Exception as exc:
                await self.server.status("error", "加载语音识别模型失败：{}".format(exc))
                print("[错误] 加载模型失败: {}".format(exc))
                return
            self._transcriber = transcriber
            self._transcriber_key = key
        transcriber = self._transcriber

        denoise = await self._ensure_denoise_model()
        await self.server.status(
            "live", "已连接直播间，开始实时识别" + ("（人声降噪已开启）" if denoise else "")
        )
        queue = asyncio.Queue(maxsize=3)
        source = FFmpegAudioSource(media, denoise_model=denoise)
        segmenter = SilenceSegmenter()

        def _put(segment):
            if queue.full():
                try:
                    queue.get_nowait()
                    print("[提示] 识别速度跟不上直播，丢弃一段音频以保持实时")
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(segment)

        async def reader():
            try:
                async for frame in source.frames():
                    for segment in segmenter.feed(frame):
                        _put(segment)
            finally:
                _put(None)

        async def worker():
            while True:
                segment = await queue.get()
                if segment is None:
                    break
                try:
                    text, lang = await loop.run_in_executor(
                        self._asr_pool, transcriber.transcribe, segment
                    )
                except Exception as exc:
                    print("[警告] 识别一段音频失败: {}".format(exc))
                    continue
                if text:
                    await self._emit(text, lang)

        try:
            await asyncio.gather(reader(), worker())
        except asyncio.CancelledError:
            await source.stop()
            raise
        tail = source.stderr_tail()
        await self.server.status("ended", "直播流已结束" + ("（{}）".format(tail) if tail else ""))
        print("[信息] 直播流已结束。可在网页里输入新地址继续。")

    async def _ensure_denoise_model(self):
        if self.args.denoise == "off":
            return None
        if DENOISE_MODEL.exists():
            return str(DENOISE_MODEL)
        # 自动下载（约 300 KB）；失败则本次不降噪，不阻塞直播启动
        try:
            import aiohttp

            DENOISE_MODEL.parent.mkdir(parents=True, exist_ok=True)
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session:
                async with session.get(RNNOISE_URL) as resp:
                    if resp.status == 200:
                        data = await resp.content.read(8 * 1024 * 1024)
                        if data.startswith(b"rnnoise"):
                            # 先写临时文件再原子改名：中途断网不会留下半个模型文件
                            tmp = DENOISE_MODEL.with_suffix(".rnnn.part")
                            tmp.write_bytes(data)
                            tmp.replace(DENOISE_MODEL)
                            print("[信息] 已自动下载人声降噪模型")
                            return str(DENOISE_MODEL)
        except Exception:
            pass
        print("[警告] 降噪模型不可用（下载失败），本次不降噪")
        return None

    async def _emit(self, text, lang):
        translated = None
        state = "skipped"
        # 只有目标语言与检测语言完全一致才跳过翻译；zh-TW 这类带地区的目标
        # 仍要走翻译做简繁转换（whisper 只会返回裸 "zh"）
        same_lang = bool(lang) and self.target.lower() == str(lang).lower()
        if self.translator is not None and not same_lang:
            translated = await self.translator.translate(text, self.target,
                                                         source=lang or "auto")
            state = "ok" if translated else "failed"
        self._counter += 1
        await self.server.broadcast({
            "type": "caption",
            "id": self._counter,
            "ts": time.time(),
            "original": text,
            "translated": translated,
            "translate_state": state,
            "src_lang": lang,
            "target_lang": self.target,
        })

    # ---- 演示模式 ----
    async def run_demo(self):
        await self.server.status("connecting", "演示模式启动中…")
        await asyncio.sleep(1.0)
        await self.server.status("live", "演示模式：内置台词模拟直播字幕（未连接真实直播）")
        while True:
            for original, translated in DEMO_SCRIPT:
                self._counter += 1
                await self.server.broadcast({
                    "type": "caption",
                    "id": self._counter,
                    "ts": time.time(),
                    "original": original,
                    "translated": translated,
                    "translate_state": "ok",
                    "src_lang": "en",
                    "target_lang": self.target,
                    "demo": True,
                })
                await asyncio.sleep(2.8)
