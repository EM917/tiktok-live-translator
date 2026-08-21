# TikTok 直播同传（tiktok-live-translator）

[English](README.en.md) | 中文

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

**macOS / Linux：**

```bash
git clone https://github.com/EM917/tiktok-live-translator.git
cd tiktok-live-translator
bash setup.sh
```

**Windows（PowerShell）：**

```powershell
git clone https://github.com/EM917/tiktok-live-translator.git
cd tiktok-live-translator
powershell -ExecutionPolicy Bypass -File setup.ps1
```

安装脚本会自动：检查 Python/ffmpeg（缺了帮你装）→ 建虚拟环境装依赖 → 下载降噪模型 → **体检你的硬件并打印推荐配置**。

然后启动（**不用记任何参数**）：

```bash
.venv/bin/python main.py
```

浏览器自动打开 `http://127.0.0.1:8765`——**在页面里粘贴直播间地址、选主播语言、点「开始翻译」即可**，随时可停止/换房间。Windows 下把 `.venv/bin/python` 换成 `.venv\Scripts\python.exe`。

命令行党也可以直接传参：

```bash
.venv/bin/python main.py "https://www.tiktok.com/@主播用户名/live" --source es
.venv/bin/python main.py --demo    # 不连直播，先看看界面效果
```

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

Copyright © 2026 [Elon Mei](https://github.com/EM917)，以 [MIT 许可](LICENSE) 发布。
