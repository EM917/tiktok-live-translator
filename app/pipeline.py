"""管线编排：拉流 → (降噪) → 切段 → 语音识别 → 翻译 → 广播给 UI。

支持两种启动方式：命令行传直播间地址，或在网页 UI 里输入地址点「开始」。
同一时间只跑一个直播间；切换房间时旧任务被取消，识别模型跨房间复用不重复加载。
"""
import asyncio
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .settings import load_settings, save_setting
from .translator import create_translator

# whisper 各模型的大致下载体积（MB），用来在 UI 上显示首次下载进度
MODEL_SIZES_MB = {"tiny": 75, "base": 145, "small": 484, "medium": 1530,
                  "large-v3": 3100, "large-v3-turbo": 1620}

DENOISE_MODEL = Path(__file__).resolve().parent.parent / "models" / "bd.rnnn"
DENOISE_MIN_BYTES = 100_000   # 完整模型约 300 KB；明显小于此值 = 下载被截断


def _arnndn_probe(model_path):
    """用 0.1 秒静音实测 arnndn 滤镜 + 模型能否初始化。

    一次探测同时覆盖两种真实故障：模型文件损坏（自动下载被截断——只验文件头
    magic 挡不住），以及 ffmpeg 不支持 arnndn（imageio-ffmpeg 的静态版没编译
    librnnoise）。两种情况下直接把坏参数交给拉流 ffmpeg，会让每一轮会话都
    「Error initializing filters」失败，再被自动重连放大成误导报错。"""
    from .ffmpeg_bin import find_ffmpeg
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        return False
    try:
        r = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=0.1",
             "-af", "arnndn=m={}".format(model_path),
             "-f", "null", "-"],
            capture_output=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False
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
        # 上次的直播间地址由服务端记住（浏览器 localStorage 按端口隔离，
        # 端口自动漂移时会拿不到），启动时回填给 UI
        saved_room = load_settings().get("room_url")
        if saved_room:
            self.server.config["room_url"] = str(saved_room)[:500]
        self._counter = 0
        self._asr_pool = None            # 每条直播一个独立线程池，停止时整个丢弃
        self._stream_task = None
        # Python 3.9 的 asyncio.Lock() 构造时就要绑事件循环，而 Pipeline 可能
        # 在循环外构造（如测试）——惰性初始化，首次使用时必然已在循环内
        self._stream_lock = None
        self._transcriber = None
        self._transcriber_key = None     # 已就绪模型对应的配置 key
        self._loading_key = None         # 在途加载对应的配置 key（可能被取消）
        self._transcriber_future = None  # 正在加载中的模型，避免重复加载
        self._resolve_fail_streak = 0    # 连续解析失败计数（触发 yt-dlp 自动保鲜）

    # ---- 来自 UI 的控制消息 ----
    def handle_control(self, msg):
        mtype = msg.get("type")
        if mtype == "set_target":
            value = str(msg.get("value", ""))[:12]
            if value:
                self.target = value
                self._save_setting("target_lang", value)
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
                return self._start_with_ack(url)
            # 不合规的地址以前是被静默丢弃的——用户点了「开始」却毫无反应
            return self.server.status(
                "error", "地址无效：请填写 http:// 或 https:// 开头的直播间地址")
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

    async def _start_with_ack(self, url):
        """UI 点「开始」后立刻回执——停掉旧管线可能要好几秒（等 ffmpeg 退出），
        期间不给任何反馈的话，用户会以为点了没反应而反复点。"""
        await self.server.status("connecting", "已收到指令，正在连接…")
        await self.start_stream(url)

    def _save_setting(self, key, value):
        """把界面偏好写进 settings.json（重启后 main.py 读回）。"""
        save_setting(key, value)

    # ---- 直播任务管理 ----
    # 加锁的原因：多个页面/标签页可能同时连着服务，两条 start 消息并发进来时，
    # 「读旧任务→取消→建新任务」如果不是原子的，后一条会覆盖 _stream_task，
    # 把前一条的管线（连同它的 ffmpeg 进程）变成谁也停不掉的孤儿。
    def _lock(self):
        if self._stream_lock is None:
            self._stream_lock = asyncio.Lock()
        return self._stream_lock

    async def start_stream(self, url):
        async with self._lock():
            await self._stop_locked(quiet=True)
            self.server.config["room_url"] = url
            self._save_setting("room_url", url)
            await self.server.broadcast({"type": "config", "room_url": url})
            self._stream_task = asyncio.create_task(self._run_stream(url))

    async def stop_stream(self, quiet=False):
        async with self._lock():
            await self._stop_locked(quiet=quiet)

    async def _stop_locked(self, quiet=False):
        """调用方必须已持有 _stream_lock。"""
        task = self._stream_task
        self._stream_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        pool, self._asr_pool = self._asr_pool, None
        if pool is not None:
            # 已排队的段直接丢弃；正在跑的那段让它自己跑完（无法安全打断），
            # 但它跑在旧线程池上，不会占住下一条直播的识别线程
            pool.shutdown(wait=False, cancel_futures=True)
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
        from .resolver import ResolveError, is_direct_url, resolve_stream_url

        await self.server.status("connecting", "正在解析直播流地址…")
        try:
            media = await resolve_stream_url(url, cookies=self.args.cookies)
            self._resolve_fail_streak = 0
        except ResolveError as exc:
            self._note_resolve_failure(exc)
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
            size_mb = MODEL_SIZES_MB.get(model)
            if size_mb and size_mb >= 1000:
                size_note = "约 {:.1f} GB".format(size_mb / 1000)
            elif size_mb:
                size_note = "约 {} MB".format(size_mb)
            else:
                size_note = "可能较大"
            await self.server.status(
                "connecting",
                "正在加载语音识别模型 {}（仅首次使用需下载，{}，进度会显示在这里）…"
                .format(model, size_note))
            # 关键：把「正在加载」这件事本身记下来。模型要几分钟，用户等不及
            # 点停止再开始时，取消只会解绑协程、线程仍在后台加载；若不认这个
            # 在途任务，每次重来都会再起一个数 GB 的模型，几轮就把内存吃光。
            #
            # 在途 key（_loading_key）与已就绪 key（_transcriber_key）必须分开记：
            # 合用一个字段的话，加载中被取消会留下「key 是新的、模型还是旧的」的
            # 错配，之后外层判断永远成立不了，新参数至死不生效（静默用旧模型）。
            if (self._transcriber_future is None or self._transcriber_future.done()
                    or self._loading_key != key):
                self._loading_key = key
                self._transcriber_future = loop.run_in_executor(
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
            watcher = asyncio.ensure_future(self._model_download_progress(model))
            try:
                # shield：本任务被取消时不要连带取消底层加载，
                # 下一次启动可以直接复用同一个在途结果
                transcriber = await asyncio.shield(self._transcriber_future)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._transcriber_future = None
                self._loading_key = None
                watcher.cancel()   # 先停进度播报，别让它把下面的 error 状态盖回去
                await self.server.status(
                    "error", "下载/加载识别模型失败——请检查网络后点「开始翻译」重试。\n"
                             "技术细节：{}".format(str(exc)[:200]))
                print("[错误] 加载模型失败: {}".format(exc))
                return
            finally:
                watcher.cancel()
            # 模型和它的 key 一起提交：中途取消时两者都不动，下次重来还会重新加载
            self._transcriber = transcriber
            self._transcriber_key = key
        transcriber = self._transcriber

        denoise = await self._ensure_denoise_model()
        live_note = ("已连接直播间，开始实时识别"
                     + ("（人声降噪已开启）" if denoise else ""))

        # ---- 断流自动重连 ----
        # TikTok 的流地址会过期、网络会抖动，ffmpeg 一断不等于主播下播了。
        # 中断后重新解析地址重连；只有解析结果明确说「没在播」（kind=offline）
        # 才真正宣布直播结束。连续多次重连都拉不到音频才放弃。
        # 直连 .flv/.m3u8 地址无法重新解析出「是否还在播」：播过一阵后结束
        # 就按正常收尾处理，重试预算也压到 1 次，别对着过期地址空耗。
        direct = is_direct_url(url)
        budget = 1 if direct else 5
        reconnects = 0
        while True:
            await self.server.status("connecting", "正在连接直播音频流…")
            got_audio, audio_secs = await self._stream_session(
                media, transcriber, denoise, live_note, loop)
            if audio_secs >= 30:
                if direct:
                    await self.server.status(
                        "ended", "直播流已结束。可以继续翻看上面的字幕，"
                                 "或输入新的地址。")
                    print("[信息] 直播流已结束。")
                    return
                reconnects = 0        # 刚才播得好好的：重置重连预算

            media = None
            while media is None:
                reconnects += 1
                if reconnects > budget:
                    await self.server.status(
                        "error", "直播流多次中断且自动重连失败——可能直播已结束，"
                                 "或网络不稳。请稍后点「开始翻译」重试。")
                    print("[信息] 自动重连预算用尽，放弃。")
                    return
                delay = min(30, 2 ** reconnects)
                await self.server.status(
                    "connecting",
                    "直播流中断，{} 秒后自动重连（第 {}/{} 次）…".format(
                        delay, reconnects, budget))
                await asyncio.sleep(delay)
                try:
                    media = await resolve_stream_url(url, cookies=self.args.cookies)
                    self._resolve_fail_streak = 0
                except ResolveError as exc:
                    if exc.kind == "offline":
                        await self.server.status(
                            "ended", "直播已结束。可以继续翻看上面的字幕，"
                                     "或输入新的直播间地址。")
                        print("[信息] 直播已结束。可在网页里输入新地址继续。")
                        return
                    self._note_resolve_failure(exc)
                    print("[错误] 重连解析失败: {}".format(exc))

    async def _stream_session(self, media, transcriber, denoise, live_note, loop):
        """跑一轮拉流→识别→翻译，直到流断开。返回 (是否收到过音频, 音频时长秒)。

        音频时长按真实收到的帧数累计，不用墙钟——网络劣化时 ffmpeg 可能连着
        30 秒只吐 2 秒音频，用墙钟会把这种「假连接」当成播得好好的，
        重连预算被错误重置后放弃分支永远走不到。"""
        from .audio import FRAME_SEC, FFmpegAudioSource
        from .segmenter import SilenceSegmenter

        got_audio = False
        audio_secs = 0.0
        queue = asyncio.Queue(maxsize=3)
        asr_pool = ThreadPoolExecutor(max_workers=1)   # 本轮会话专用，停止时随之丢弃
        self._asr_pool = asr_pool
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
            # 「直播中」要等真的收到音频才宣布——ffmpeg 连流失败时不能先报喜再改口
            nonlocal got_audio, audio_secs
            try:
                async for frame in source.frames():
                    if not got_audio:
                        got_audio = True
                        await self.server.status("live", live_note)
                    audio_secs += FRAME_SEC
                    for segment in segmenter.feed(frame):
                        _put(segment)
                for segment in segmenter.flush():   # 别丢掉最后一段话
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
                        asr_pool, transcriber.transcribe, segment
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
        asr_pool.shutdown(wait=False)
        tail = source.stderr_tail()
        if tail:
            print("[信息] ffmpeg 输出: {}".format(tail))   # 英文技术输出只进终端，不上 UI
        return got_audio, audio_secs

    def _note_resolve_failure(self, exc):
        """连续两次「不是主播下播」的解析失败，多半是 yt-dlp 的 TikTok 提取器
        坏了——触发后台自动升级 yt-dlp（见 updater.freshen_ytdlp）。"""
        kind = getattr(exc, "kind", "unknown")
        if kind in ("offline", "login"):
            self._resolve_fail_streak = 0
            return
        if kind == "network":
            return   # 断网与提取器无关：不计数也不清零，更不能拉起注定失败的 pip
        self._resolve_fail_streak += 1
        if self._resolve_fail_streak >= 2 and getattr(self, "updater", None) is not None:
            asyncio.ensure_future(self.updater.freshen_ytdlp(reason="resolve-failures"))

    async def _model_download_progress(self, model):
        """模型加载期间轮询 HuggingFace 缓存目录的增量，把下载进度推到 UI。
        没有真的在下载（缓存已存在）时增量趋近于零，不会打扰用户。"""
        try:
            hf_home = os.environ.get("HF_HOME")
            cache = (Path(hf_home) / "hub" if hf_home
                     else Path.home() / ".cache" / "huggingface" / "hub")

            def _du():
                # lstat 不跟随符号链接：HF 缓存里 snapshots/ 是指向 blobs/ 的
                # 软链，跟着算会把每个文件记两遍，进度条显示 200%
                total = 0
                for root, _dirs, files in os.walk(cache):
                    for name in files:
                        try:
                            total += os.lstat(os.path.join(root, name)).st_size
                        except OSError:
                            pass
                return total

            loop = asyncio.get_running_loop()
            baseline = await loop.run_in_executor(None, _du)
            expected = MODEL_SIZES_MB.get(model)
            while True:
                await asyncio.sleep(3)
                done_mb = (await loop.run_in_executor(None, _du) - baseline) / 1e6
                if done_mb < 20:      # 没在下载（或刚开始），别闪一条 0% 出来
                    continue
                if expected:
                    done_mb = min(done_mb, expected)   # Windows 无软链权限时是真副本，仍可能翻倍
                    pct = min(99, int(done_mb * 100 / expected))
                    text = ("正在下载识别模型 {}：{}%（{:.0f} / {} MB，仅首次需要，"
                            "请保持窗口打开）…".format(model, pct, done_mb, expected))
                else:
                    text = ("正在下载识别模型 {}：已下载 {:.0f} MB（仅首次需要，"
                            "请保持窗口打开）…".format(model, done_mb))
                await self.server.status("connecting", text)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass      # 进度显示是锦上添花，任何失败都不能影响加载本身

    async def _ensure_denoise_model(self):
        if self.args.denoise == "off":
            return None
        loop = asyncio.get_running_loop()
        if DENOISE_MODEL.exists():
            if await loop.run_in_executor(None, _arnndn_probe, str(DENOISE_MODEL)):
                return str(DENOISE_MODEL)
            # 实测起不来：模型损坏（截断下载）或 ffmpeg 不支持 arnndn。
            # 明显截断的坏文件必须删掉——留着会毒害之后的每一次启动
            print("[警告] 降噪不可用（模型损坏或 ffmpeg 不支持 arnndn），本次不降噪")
            try:
                if DENOISE_MODEL.stat().st_size < DENOISE_MIN_BYTES:
                    DENOISE_MODEL.unlink()
            except OSError:
                pass
            return None
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
                        # 只验文件头不够：截断的下载同样带正确 magic，
                        # 长度下限 + 落盘后实测初始化才算数
                        if (data.startswith(b"rnnoise")
                                and len(data) >= DENOISE_MIN_BYTES):
                            # 先写临时文件再原子改名：中途断网不会留下半个模型文件
                            tmp = DENOISE_MODEL.with_suffix(".rnnn.part")
                            tmp.write_bytes(data)
                            tmp.replace(DENOISE_MODEL)
                            if await loop.run_in_executor(
                                    None, _arnndn_probe, str(DENOISE_MODEL)):
                                print("[信息] 已自动下载人声降噪模型")
                                return str(DENOISE_MODEL)
                            DENOISE_MODEL.unlink()   # 实测失败：删掉别留隐患
        except Exception:
            pass
        print("[警告] 降噪模型不可用（下载失败或校验未过），本次不降噪")
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
    async def start_demo(self):
        """演示同样挂到 _stream_task 上：这样「停止」能真的停下来，
        换成真实直播时旧循环也会被先取消（否则真假字幕会交错广播）。"""
        async with self._lock():
            await self._stop_locked(quiet=True)
            self._stream_task = asyncio.create_task(self.run_demo())

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
