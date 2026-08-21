# TikTok 直播同传 · TikTok Live Translator

<p align="center"><img src="assets/icon-1024.png" width="128" alt="icon"></p>

**语言 / Language：[中文](#chinese) | [English](#english)**

---

<a name="chinese"></a>

监听 TikTok 直播间，把**主播说的话**实时转写并翻译成字幕，在本地浏览器 UI 中双语显示。附带一个 Chrome 插件，可以把字幕直接叠加在 TikTok 直播页面上。

**全程本地运行**：拉流、语音识别、（可选）翻译都在你自己的电脑上完成，无需任何 API Key，零费用。

## 特性

- 🎙️ **实时语音识别** —— OpenAI Whisper（faster-whisper / MLX 双后端），自动检测主播语言，支持 90+ 语言
- 🌐 **三档翻译引擎** —— 本地 TranslateGemma（推荐，离线免费）/ Google 免费接口（默认兜底）/ Claude·OpenAI API
- 🎵 **人声降噪** —— RNNoise 神经降噪抑制背景音乐，专为带 BGM 的直播间优化
- ⚡ **硬件自动配置** —— 检测你的芯片（Apple Silicon GPU / NVIDIA CUDA / 普通 CPU）自动选择能实时跑的最优模型，零配置开箱即用
- 📺 **双显示端** —— 本地网页 UI（历史双语字幕 + 底部大字幕），或 Chrome 插件叠加在 TikTok 页面上
- 🔄 **抗故障** —— yt-dlp 解析失效时自动从直播页面直接挖流地址；识别跟不上时自动丢段保实时

## 快速开始

只需要装好 [Python 3.9+](https://www.python.org/downloads/)，然后两条命令：

```bash
git clone https://github.com/EM917/tiktok-live-translator.git
cd tiktok-live-translator && python3 main.py
```

（Windows 用 `python main.py`）

**其余全自动**：首次运行会自动创建虚拟环境、安装全部依赖（含内置 ffmpeg 和降噪模型），然后自动打开浏览器 `http://127.0.0.1:8765`——**在页面里粘贴直播间地址、选主播语言、点「开始翻译」即可**，随时可停止/换房间。首次安装依赖需要几分钟，之后秒开。

命令行党也可以直接传参：

```bash
python3 main.py "https://www.tiktok.com/@主播用户名/live" --source es
python3 main.py --demo      # 不连直播，先看看界面效果
python3 main.py --doctor    # 看看硬件体检和推荐配置
```

> 想手动控制安装过程？`bash setup.sh`（macOS/Linux）或 `powershell -ExecutionPolicy Bypass -File setup.ps1`（Windows）做的是同样的事。

> **之前克隆过旧版本？** 不要重新 clone（会报 `destination path already exists`）——进目录执行 `git pull` 再启动即可；v0.2.0 起页面里就能一键更新，不再需要命令行。

### 日常启动（关机重启 / 关闭之后）

- **macOS**：双击项目文件夹里的 **`TikTok Live Translator.app`**（首次启动如被系统拦截，右键 → 打开；可拖到 Dock 常驻）；也可以双击 `Start.command`；
- **Windows**：**双击 `Start.bat`**；
- 命令行党：`cd ~/tiktok-live-translator && python3 main.py`。

界面在**独立应用窗口**中打开（不占浏览器标签页），上次填过的直播间地址还在输入框里，点「开始翻译」即可；关掉窗口就是退出。想改回浏览器界面加 `--browser`。

## 硬件自动配置（它是怎么根据你的电脑选配置的）

运行 `python main.py --doctor` 随时查看体检结果。启动时未显式指定的参数按下表自动补齐：

| 你的电脑 | 自动选择 | 效果 |
|---|---|---|
| Apple Silicon（M1 及以上） | MLX GPU 后端 + `large-v3` | 最准模型，比实时快约 5 倍 |
| NVIDIA 显卡 | CUDA + `large-v3` (float16) | 最准模型，速度富余 |
| 普通 CPU（≥8 核 & ≥8GB） | CPU + `small` (int8) | 保实时，精度中等 |
| 低配 CPU | CPU + `base` (int8) | 优先保证字幕不掉队 |

实测锚点（M3 Pro，90 秒真实直播素材，RTF=识别耗时/音频时长，<1 才不丢字幕）：

| 配置 | RTF |
|---|---|
| MLX GPU + large-v3 | **0.21** ✅ |
| CPU + large-v3-turbo | 0.99 ⚠️ 临界 |
| CPU + large-v3 | 1.22 ❌ 丢段 |

任何自动选择都可以用命令行参数覆盖（见下表）。

## 命令行参数

| 参数 | 说明 | 默认 |
|------|------|------|
| `--target` | 目标语言（`zh-CN`/`en`/`ja`/`ko`/…，UI 里也能随时切换） | `zh-CN` |
| `--source` | 主播语言，不填自动检测（确定时指定更准，如 `es`/`en`/`ja`） | 自动 |
| `--backend` | 识别后端：`mlx`（Apple GPU）/`ct2`（faster-whisper）/`auto` | `auto` |
| `--model` | whisper 模型：`tiny`/`base`/`small`/`medium`/`large-v3`/`large-v3-turbo` | 按硬件自动 |
| `--device` | `auto`/`cpu`/`cuda` | 按硬件自动 |
| `--compute-type` | ct2 精度（`int8`/`float16`/…） | 按硬件自动 |
| `--beam` | beam search 宽度（越大越准越慢，`1`=贪心；仅 ct2 后端） | `5` |
| `--no-context` | 关闭滚动上下文（默认开启，提升断句连贯性） | 关 |
| `--translator` | 翻译引擎：`auto`/`gemma`/`google`/`claude`/`openai`/`none` | `auto` |
| `--denoise` | RNNoise 人声降噪：`auto`/`on`/`off` | `auto`（开） |
| `--port` | 本地 UI 端口 | `8765` |
| `--cookies` | yt-dlp cookies.txt 路径（地区受限的直播间可能需要） | 无 |
| `--demo` | 演示模式，仅驱动 UI | 关 |
| `--doctor` | 打印硬件体检和推荐配置后退出 | 关 |
| `--no-open` | 启动后不自动打开浏览器 | 关 |

## 翻译引擎

- `auto`（默认）—— 本地 Ollama 里装了 TranslateGemma 就用 `gemma`，否则退回 `google`。
- `gemma` —— **推荐**：Google 开源的翻译专用模型 TranslateGemma，跑在本地 GPU 上，完全离线免费，口语/俚语翻译质量远好于谷歌网页接口。安装：

  ```bash
  brew install ollama          # Windows/Linux 见 https://ollama.com/download
  ollama pull translategemma:4b
  brew services start ollama   # 或手动运行 ollama serve
  ```

  模型可用环境变量 `OLLAMA_TRANSLATE_MODEL` 换成 `translategemma:12b`/`27b`（更准更慢）。
- `google` —— Google 翻译网页版免费接口，无需密钥。**注意：字幕文本会被发送给 Google**，且口语直译错误较多。
- `claude` —— 需要环境变量 `ANTHROPIC_API_KEY`（默认 Claude Haiku，可用 `CLAUDE_TRANSLATE_MODEL` 覆盖）。
- `openai` —— 需要 `OPENAI_API_KEY`（可选 `OPENAI_BASE_URL`、`OPENAI_MODEL`，兼容各类 OpenAI 风格接口，包括本地 LM Studio / vLLM）。
- `none` —— 只显示识别原文，不翻译，完全离线。

实测对比（西语口语 → 中文）：

| 原文 | Google | TranslateGemma |
|---|---|---|
| *Se mueren lo rico*（好吃到不行） | ❌ 有钱人死 | ✅ 味道非常好 |
| *tengo un sueño*（我好困） | ❌ 我做了一个梦 | ✅ 现在感觉很困 |
| *Es vegano*（纯素产品） | ❌ 它是素食主义者 | ✅ 纯素的 |

## Chrome 插件（把字幕叠加到 TikTok 页面上）

1. 打开 `chrome://extensions`，右上角开启「开发者模式」；
2. 点「加载已解压的扩展程序」，选择本项目的 `extension/` 文件夹；
3. 保持 `main.py` 在运行，打开任意 `tiktok.com` 页面，字幕到达时出现悬浮字幕条。

字幕条支持**拖动**移动位置、**双击**折叠/展开。改过 `--port` 的话在扩展「选项」页同步修改。插件只是显示端——音频抓取和识别由本地 `main.py` 完成。

## 架构

```
TikTok 直播间 URL
      │  yt-dlp 解析直播流（失效时自动从页面 HTML 挖流地址，优先纯音频流）
      ▼
   ffmpeg ──► RNNoise 人声降噪 ──► 16 kHz 单声道 PCM
      │  能量 VAD 切段（2.5–9 秒）
      ▼
 Whisper 语音识别（MLX GPU / faster-whisper，滚动上下文 + 置信度过滤）
      │
      ▼
   翻译引擎（TranslateGemma 本地 / Google / Claude / OpenAI / 关闭）
      │
      ▼
WebSocket 广播 ──► 浏览器字幕 UI（http://127.0.0.1:8765）
              └──► Chrome 插件叠加字幕（可选）
```

## 自动更新

每次启动时会静默检查 GitHub 上的最新版本（网络失败一律无声跳过）。有新版本时页面顶部会出现横幅：

- **git 安装**（`git clone` 的）→ 点「一键更新」自动 `git pull --ff-only` 并重启程序。本地有未提交修改时会拒绝更新以免覆盖你的改动；
- **ZIP 安装** → 横幅提供下载页链接，手动替换即可。

界面底部会显示当前版本号。

## 常见问题

- **`yt-dlp 未能解析直播流`** —— 若主播确实在播，工具会自动改从页面 HTML 解析（日志有提示）；仍失败说明主播没播，或该地区需要登录，用浏览器插件导出 cookies.txt 后加 `--cookies cookies.txt`。
- **首次启动卡在「正在加载语音识别模型」** —— 正在从 Hugging Face 下载模型（large-v3 约 3GB），只需一次。
- **识别追不上直播 / 终端提示丢弃音频** —— 换小一档模型（`--model small`），或 `--beam 1`；Apple Silicon 用户确认 `pip install mlx-whisper` 后走 GPU。
- **背景音乐里的歌声被当成主播的话** —— RNNoise 对器乐抑制好，但对歌曲里的**演唱人声**只能部分抑制；置信度过滤会兜住大部分，个别漏网属正常。
- **插件没显示字幕** —— 确认 `main.py` 在运行，刷新 TikTok 页面；改过端口的在扩展选项页同步。
- **翻译显示「翻译失败」** —— 当前引擎不可达（断网/Ollama 没启动），会自动显示原文；`auto` 模式下重启工具会自动回退到可用引擎。

## 隐私与使用边界

- 识别永远在本地；翻译用 `gemma`/`none` 时全程离线，用 `google`/`claude`/`openai` 时字幕文本会发给对应服务商。
- 本工具仅供个人学习、语言辅助用途；请遵守 TikTok 服务条款与当地法律，不要用于转播、录制分发他人内容。

## 致谢

- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) / [mlx-whisper](https://github.com/ml-explore/mlx-examples) —— 语音识别
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) —— 直播流解析
- [FFmpeg](https://ffmpeg.org/) —— 音频处理（内置 RNNoise `arnndn` 滤镜）
- [rnnoise-models](https://github.com/GregorR/rnnoise-models) —— 降噪模型（beguiling-drafter）
- [Ollama](https://github.com/ollama/ollama) + [TranslateGemma](https://ollama.com/library/translategemma) —— 本地翻译

## 作者与许可

Copyright © 2026 [Elon Mei (EM917)](https://github.com/EM917)，以 [MIT 许可](LICENSE) 发布。

---

<a name="english"></a>

# TikTok Live Translator (English)

Listens to a TikTok livestream, transcribes what the **streamer says** in real time, translates it, and shows bilingual subtitles in a local browser UI. Comes with a Chrome extension that overlays the subtitles directly on the TikTok live page.

**Runs entirely locally**: stream capture, speech recognition, and (optionally) translation all happen on your own machine — no API key needed, zero cost.

## Features

- 🎙️ **Real-time speech recognition** — OpenAI Whisper (dual backend: faster-whisper / MLX), auto-detects the streamer's language, supports 90+ languages
- 🌐 **Three-tier translation engine** — local TranslateGemma (recommended, offline and free) / Google's free API (default fallback) / Claude · OpenAI API
- 🎵 **Voice-focused denoising** — RNNoise neural denoising suppresses background music, tuned for streams with BGM
- ⚡ **Automatic hardware tuning** — detects your chip (Apple Silicon GPU / NVIDIA CUDA / plain CPU) and automatically picks the best model that can still run in real time — zero-config out of the box
- 📺 **Two display modes** — a local web UI (scrolling bilingual subtitle history + a large caption at the bottom), or a Chrome extension overlay on the TikTok page
- 🔄 **Fault-tolerant** — falls back to scraping the stream URL straight from the live page when yt-dlp resolution fails; drops segments automatically to stay real-time when recognition falls behind

## Quick Start

All you need installed is [Python 3.9+](https://www.python.org/downloads/). Then:

```bash
git clone https://github.com/EM917/tiktok-live-translator.git
cd tiktok-live-translator && python3 main.py
```

(On Windows use `python main.py`.)

**Everything else is automatic**: the first run creates a virtual environment, installs all dependencies (including a bundled static ffmpeg and the denoising model), then opens `http://127.0.0.1:8765` in your browser — **paste the live-room URL into the page, pick the streamer's language, and hit Start**. You can stop or switch rooms anytime from the page. The first run takes a few minutes to install; every run after that starts instantly.

CLI flags still work if you prefer:

```bash
python3 main.py "https://www.tiktok.com/@streamer_username/live" --source es
python3 main.py --demo      # preview the UI without connecting to a stream
python3 main.py --doctor    # print the hardware check and recommended config
```

> Prefer to control the install yourself? `bash setup.sh` (macOS/Linux) or `powershell -ExecutionPolicy Bypass -File setup.ps1` (Windows) does the same steps explicitly.

> **Cloned an older version before?** Don't re-clone (it fails with `destination path already exists`) — just `cd` into the folder, run `git pull`, and start it; from v0.2.0 on you can update with one click from the page itself.

### Everyday startup (after reboot / after closing it)

- **macOS**: double-click **`TikTok Live Translator.app`** in the project folder (right-click → Open on first launch if Gatekeeper complains; drag it to the Dock to keep it handy). `Start.command` works too;
- **Windows**: **double-click `Start.bat`**;
- CLI: `cd ~/tiktok-live-translator && python3 main.py`.

The UI opens in its **own app window** (no browser tab), with the room URL you used last time still in the input box — just hit Start. Closing the window quits the app. Pass `--browser` if you prefer the browser UI.

## Automatic Hardware Tuning (how it picks a config for your machine)

Run `python main.py --doctor` anytime to see the hardware-check results. Any flag you don't explicitly set at startup is auto-filled per the table below:

| Your machine | Auto-selected | Result |
|---|---|---|
| Apple Silicon (M1 or later) | MLX GPU backend + `large-v3` | Most accurate model, runs ~5x faster than real time |
| NVIDIA GPU | CUDA + `large-v3` (float16) | Most accurate model, plenty of speed headroom |
| Plain CPU (≥8 cores & ≥8GB RAM) | CPU + `small` (int8) | Keeps up in real time, moderate accuracy |
| Low-end CPU | CPU + `base` (int8) | Prioritizes keeping subtitles from falling behind |

Benchmark reference (M3 Pro, 90 seconds of real livestream footage; RTF = recognition time / audio duration — needs to be < 1 to avoid dropping subtitles):

| Config | RTF |
|---|---|
| MLX GPU + large-v3 | **0.21** ✅ |
| CPU + large-v3-turbo | 0.99 ⚠️ borderline |
| CPU + large-v3 | 1.22 ❌ drops segments |

Any auto-selected value can be overridden with a command-line flag (see below).

## Command-Line Flags

| Flag | Description | Default |
|------|------|------|
| `--target` | Target language (`zh-CN`/`en`/`ja`/`ko`/…; can also be switched anytime in the UI) | `zh-CN` |
| `--source` | Streamer's language; auto-detected if omitted (specify it for better accuracy when known, e.g. `es`/`en`/`ja`) | auto |
| `--backend` | Recognition backend: `mlx` (Apple GPU) / `ct2` (faster-whisper) / `auto` | `auto` |
| `--model` | Whisper model: `tiny`/`base`/`small`/`medium`/`large-v3`/`large-v3-turbo` | auto by hardware |
| `--device` | `auto`/`cpu`/`cuda` | auto by hardware |
| `--compute-type` | ct2 precision (`int8`/`float16`/…) | auto by hardware |
| `--beam` | Beam search width (larger = more accurate but slower; `1` = greedy; ct2 backend only) | `5` |
| `--no-context` | Disable rolling context (on by default; improves sentence-boundary coherence) | off |
| `--translator` | Translation engine: `auto`/`gemma`/`google`/`claude`/`openai`/`none` | `auto` |
| `--denoise` | RNNoise voice denoising: `auto`/`on`/`off` | `auto` (on) |
| `--port` | Local UI port | `8765` |
| `--cookies` | Path to a yt-dlp cookies.txt file (may be needed for region-restricted streams) | none |
| `--demo` | Demo mode — drives only the UI | off |
| `--doctor` | Print the hardware check and recommended config, then exit | off |
| `--no-open` | Don't auto-open the browser on startup | off |

## Translation Engines

- `auto` (default) — uses `gemma` if TranslateGemma is installed in local Ollama, otherwise falls back to `google`.
- `gemma` — **Recommended**: TranslateGemma, Google's open-source translation-specialized model. Runs on your local GPU, fully offline and free, and handles colloquial speech/slang far better than Google's web API. Install:

  ```bash
  brew install ollama          # See https://ollama.com/download for Windows/Linux
  ollama pull translategemma:4b
  brew services start ollama   # or run ollama serve manually
  ```

  You can switch models via the `OLLAMA_TRANSLATE_MODEL` environment variable to `translategemma:12b`/`27b` (more accurate, slower).
- `google` — Google Translate's free web API, no key required. **Note: subtitle text is sent to Google**, and it tends to mistranslate colloquial speech more often.
- `claude` — requires the `ANTHROPIC_API_KEY` environment variable (defaults to Claude Haiku; override with `CLAUDE_TRANSLATE_MODEL`).
- `openai` — requires `OPENAI_API_KEY` (optionally `OPENAI_BASE_URL`, `OPENAI_MODEL`); compatible with any OpenAI-style API, including local LM Studio / vLLM.
- `none` — shows only the raw transcription, no translation, fully offline.

Real-world comparison (colloquial Spanish → Chinese):

| Original | Google | TranslateGemma |
|---|---|---|
| *Se mueren lo rico* (so good it's unreal) | ❌ 有钱人死 (the rich people die) | ✅ 味道非常好 (tastes amazing) |
| *tengo un sueño* (I'm sleepy) | ❌ 我做了一个梦 (I had a dream) | ✅ 现在感觉很困 (feeling very sleepy right now) |
| *Es vegano* (it's a vegan product) | ❌ 它是素食主义者 (he/she is a vegetarian) | ✅ 纯素的 (vegan) |

## Chrome Extension (overlay subtitles on the TikTok page)

1. Open `chrome://extensions` and enable "Developer mode" in the top right.
2. Click "Load unpacked" and select this project's `extension/` folder.
3. Keep `main.py` running, open any `tiktok.com` page, and a floating subtitle bar appears as subtitles arrive.

The subtitle bar supports **dragging** to reposition it and **double-clicking** to collapse/expand. If you changed `--port`, update it to match on the extension's "Options" page too. The extension is just a display layer — audio capture and recognition are handled by the local `main.py`.

## Architecture

```
TikTok live room URL
      │  yt-dlp resolves the live stream (falls back to scraping the stream URL from page HTML if it fails; prefers audio-only streams)
      ▼
   ffmpeg ──► RNNoise voice denoising ──► 16 kHz mono PCM
      │  Energy-based VAD segmentation (2.5–9 sec)
      ▼
 Whisper speech recognition (MLX GPU / faster-whisper, rolling context + confidence filtering)
      │
      ▼
   Translation engine (local TranslateGemma / Google / Claude / OpenAI / off)
      │
      ▼
WebSocket broadcast ──► Browser subtitle UI (http://127.0.0.1:8765)
              └──► Chrome extension subtitle overlay (optional)
```

## Auto-update

On every launch the app silently checks GitHub for the latest release (network failures are silently ignored). When a new version exists, a banner appears at the top of the page:

- **git install** (cloned via `git clone`) → click "Update" to run `git pull --ff-only` and restart automatically. If you have uncommitted local changes, the update is refused to avoid overwriting them;
- **ZIP install** → the banner links to the download page instead.

The current version is shown in the page footer.

## FAQ

- **`yt-dlp failed to resolve the live stream`** — if the streamer is genuinely live, the tool automatically falls back to parsing the page HTML (you'll see this noted in the logs). If it still fails, either the streamer isn't actually live, or your region requires login — export `cookies.txt` with a browser extension and add `--cookies cookies.txt`.
- **First launch stuck on "Loading speech recognition model"** — it's downloading the model from Hugging Face (large-v3 is about 3GB); this only happens once.
- **Recognition can't keep up with the stream / terminal shows dropped audio** — switch to a smaller model (`--model small`), or use `--beam 1`; Apple Silicon users should confirm `pip install mlx-whisper` succeeded so it runs on the GPU.
- **Singing in background music gets picked up as the streamer talking** — RNNoise suppresses instrumental music well, but can only partially suppress **sung vocals** in a song; confidence filtering catches most of these, and the occasional slip-through is normal.
- **Extension isn't showing subtitles** — confirm `main.py` is running and refresh the TikTok page; if you changed the port, update it on the extension's Options page too.
- **Translation shows "translation failed"** — the current engine is unreachable (no network / Ollama not running); it automatically falls back to showing the raw text. In `auto` mode, restarting the tool falls back to whichever engine is available.

## Privacy and Usage Boundaries

- Recognition always runs locally. Translation is fully offline when using `gemma`/`none`; with `google`/`claude`/`openai`, subtitle text is sent to the corresponding provider.
- This tool is for personal learning and language-assistance use only. Please comply with TikTok's Terms of Service and local laws — don't use it to rebroadcast or redistribute recordings of other people's content.

## Acknowledgments

- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) / [mlx-whisper](https://github.com/ml-explore/mlx-examples) — speech recognition
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — livestream resolution
- [FFmpeg](https://ffmpeg.org/) — audio processing (built-in RNNoise `arnndn` filter)
- [rnnoise-models](https://github.com/GregorR/rnnoise-models) — denoising model (beguiling-drafter)
- [Ollama](https://github.com/ollama/ollama) + [TranslateGemma](https://ollama.com/library/translategemma) — local translation

## Author & License

Copyright © 2026 [Elon Mei (EM917)](https://github.com/EM917). Released under the [MIT License](LICENSE).
