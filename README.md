<a id="en"></a>

# TikTok Live Translator

<p align="center"><img src="assets/icon-1024.png" width="128" alt="icon"></p>

<p align="center">
  <a href="https://github.com/EM917/tiktok-live-translator/releases/latest"><img src="https://img.shields.io/github/v/release/EM917/tiktok-live-translator" alt="release"></a>
  <a href="https://github.com/EM917/tiktok-live-translator/actions/workflows/ci.yml"><img src="https://github.com/EM917/tiktok-live-translator/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/python-3.9%2B-blue" alt="python 3.9+">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Windows-lightgrey" alt="platform">
  <img src="https://img.shields.io/badge/privacy-local--first,_no_API_key_needed-success" alt="local-first, no API key needed">
  <a href="LICENSE"><img src="https://img.shields.io/github/license/EM917/tiktok-live-translator" alt="license"></a>
</p>

<p align="center"><img src="assets/demo.en.gif" width="760" alt="Demo: each Spanish caption appears first and its English translation fills in a moment later, while viewer comments are translated in the panel on the right"></p>

**Language / 语言：English | [中文](#zh)**

---

Real-time bilingual captions for TikTok live streams, built for Spanish-language
live selling. Paste a live link and the app transcribes what the streamer says,
shows each line as soon as it is recognised, and fills in the translation
beneath it a moment later. Viewer comments are translated alongside.

It is for teams who follow Spanish-language live selling without speaking
Spanish: the operator watches on the computer, and colleagues follow on their
phones. To get started, see [Quick Start](#quick-start).

A caption never waits for its translation: the original goes on screen first.
When recognition falls behind, audio waits in a queue and the window says so.
Speech recognition always runs on your computer, and with a local model so does
translation — no API key and no fees.

The interface is in English or Chinese. A new install follows the system
language, and Settings → App Language switches it; see
[Interface Language](#interface-language).

<a id="en-features"></a>

## Features

<sub>[中文](#zh-features)</sub>

- 🎙️ **Live captions, original first** — OpenAI Whisper with two backends (faster-whisper / MLX), 90+ languages, limited to Spanish + English by default. Each line appears as soon as it is recognised and its translation fills in beneath it; the latest line is also shown large at the bottom of the window
- 🌐 **Local-first translation** — Hy-MT2 (Apache 2.0, offline, free) in two sizes through Ollama, with TranslateGemma, DeepL (with a native glossary), Google's free endpoint, the Claude API and any OpenAI-compatible API as alternatives. By default Hy-MT2 1.8B is used, then TranslateGemma, and Google's free endpoint when neither is installed; 7B is never picked automatically. See [Translation Engines](#translation-engines)
- 🏷️ **Glossaries per brand and streamer** — a global glossary, one per streamer and one per brand (picked in the Brand menu for the session) steer how product names are recognised and translated, with every engine. See [Glossaries](docs/translation-engines.md#glossaries)
- 💬 **Viewer comment translation** — comments are fetched from the live stream through TikTokLive and translated while a local engine (Hy-MT2 1.8B or TranslateGemma) or Claude is active; with Google, DeepL, OpenAI or 7B the Comments panel shows the original text. Disable with `--no-comments`; see [the FAQ](docs/faq.md#viewer-comments)
- 🔁 **Switch Streamer during a session** — Switch Streamer in the top bar moves to another streamer without clicking Stop first: pick from Recent Streams or paste a live link, choose the brand, then click again to confirm. The spoken language carries over, and a divider in the caption history marks where the new streamer begins
- 📱 **Phone Viewing** — colleagues on the same Wi-Fi scan a QR code and follow captions and alerts on their phones, each page in its phone's own language; view only, no control; off by default. See [the FAQ](docs/faq.md#phone-viewing)
- 🌍 **English and Chinese interface** — follows the system language on a new install, switchable in Settings. See [Interface Language](#interface-language)
- ✅ **Startup Check** — each capability is executed rather than inspected: noise reduction processes a sample through RNNoise, translation queries the engine, the audit log performs a write. A failing check opens its row on the home page with a remediation step, and stays in a banner at the top of the window, idle or live, until a later check passes
- 🔄 **Fault tolerance** — up to seven ways to find the stream URL, reconnects that tell a network drop from the end of the stream, and audio queued rather than lost when recognition falls behind. See [Fault Tolerance](#fault-tolerance)
- 🎵 **Voice-focused noise reduction** — RNNoise suppresses background music, tuned for streams with continuous BGM
- ⚡ **Automatic hardware configuration** — detects the available accelerator (Apple Silicon GPU / NVIDIA CUDA / CPU) and selects the largest model that still runs in real time
- 📊 **Observable latency** — a live readout at the bottom of the window: the longest a spoken word waits before it is recognised and checked (median and P95), split into segmentation and recognition, plus the time the translation adds. An audit log records each segment: accepted text, candidates rejected by the quality filter, banned-term matches, and the translation that followed
- 🚨 **Banned-term alerts, off by default** — for compliance monitoring of live selling: three-tier matching (exact / variant / similar) against the recognised source text, independent of translation and across caption boundaries. The switch is in Settings → Banned-Term Alerts. See [Banned-Term Alerts](#banned-term-alerts)
- 👀 **Comment Review, on by default with rules only** — spots insults and threats, claims of fakes or calls to report, flooding and redirects among viewer comments: a clear rule match is marked red, an unsure one amber, a line at the top of the window sums it up, and marked viewers are highlighted on every comment. Read-only — it cannot delete comments or mute anyone — and absent from Phone Viewing. Optional Claude Haiku 5.5 review only ever adds. Switch it off in Settings → Comment Review. See [Comment Review](#comment-review)

<a id="en-screenshots"></a>

## Screenshots

<sub>[中文](#zh-screenshots)</sub>

<table>
  <tr>
    <td width="50%"><img src="assets/screenshots/en/home.png" width="100%" alt="Home page: live link and Start, the Spoken language and Brand menus, Recent Streams, and the Settings group with Startup Check, Translation Engine, Banned-Term Alerts, Storage and App Language"><br><sub>Home: paste a live link and click Start. Settings sits below, one line per setting until opened.</sub></td>
    <td width="50%"><img src="assets/screenshots/en/live.png" width="100%" alt="During a session: each Spanish line above its English translation, translated comments on the right, the latest line in large type at the bottom, and the latency readout under it"><br><sub>Live: the original first, the translation beneath it, comments alongside.</sub></td>
  </tr>
  <tr>
    <td width="50%"><img src="assets/screenshots/en/switch-streamer.png" width="100%" alt="The Switch Streamer panel during a session: a new live link, Recent Streams with the current streamer marked, a Brand menu, the carried-over spoken language, and the button asking for a second click to switch"><br><sub>Switch Streamer: pick the next streamer, then click again to confirm.</sub></td>
    <td width="50%"><img src="assets/screenshots/en/share-panel.png" width="100%" alt="The Phone Viewing panel: a QR code, the local network link, Copy Link, New Link and Stop Sharing, and 1 watching (limit 12)"><br><sub>Phone Viewing: a QR code and link for phones on the same Wi-Fi.</sub></td>
  </tr>
  <tr>
    <td width="50%" align="center"><img src="assets/screenshots/en/phone.png" width="240" alt="The phone page: Connected and Live, Spanish lines with English translations and a Retranslate button on each, the Comments bar, View only, a language toggle and the Sound button"><br><sub>The phone page: view only, in the phone's own language.</sub></td>
    <td width="50%"><img src="assets/screenshots/en/settings-language.png" width="100%" alt="The Settings group with the App Language row open, set to System (English), and the note that Phone Viewing pages follow each phone's language"><br><sub>Settings → App Language: System, 中文 or English.</sub></td>
  </tr>
</table>

<sub>The screenshots and the demo above show made-up streams and are generated by <code>tools/readme_shots.py</code>.</sub>

<a id="en-disk-space"></a>

## Disk Space

<sub>[中文](#zh-disk-space)</sub>

Moved to [docs/hardware.md](docs/hardware.md#disk-space).

<a id="en-quick-start"></a>

## Quick Start

<sub>[中文](#zh-quick-start)</sub>

### Installation without a terminal (recommended)

1. **Download**: grab **TikTok-Live-Translator-vX.Y.Z.zip** from the [latest release](https://github.com/EM917/tiktok-live-translator/releases/latest)'s Assets and unzip it anywhere (e.g. your Desktop) — "Source code (zip)" works too. The green **Code** button → **Download ZIP** also works (latest dev snapshot).
2. **Install Python** (free, one time only): grab the installer from [python.org/downloads](https://www.python.org/downloads/) and install with the default options. Forgot? No problem — the launcher will detect it and open the download page for you.
3. **Launch**:
   - **macOS**: double-click **`TikTok Live Translator.app`** in the folder. If the first launch is blocked ("cannot be opened"): on older systems right-click → Open; on **macOS 15 and later** go to **System Settings → Privacy & Security**, scroll to the bottom and click **"Open Anyway"** (one time only). Feel free to drag it to the Dock — but **don't move it out of this folder**.
   - **Windows**: double-click **`Start.bat`**. If a "publisher unknown" security warning pops up, click "Run" (this tool is fully open source — the code is right there in the folder). A black text window stays open while running — **that window is the app itself; keep it open**. Captions appear in the separate app window.

System requirements and the window engine: [docs/hardware.md](docs/hardware.md#minimum-system-and-the-window-engine).

The first launch installs everything automatically (a few minutes, with on-screen progress; the first recognition also downloads the speech model, with progress shown on the page). Every launch after that is instant. Once the window opens: **paste the live link (or just the streamer's username) → check Spoken language (Spanish + English by default) and, in the top bar, the language to translate into → click Start**. During a session, **Switch Streamer** moves to another streamer, and **Stop** takes the window back to the home page, with that session's captions kept below under Previous Session.

### Command line

With [Python 3.9+](https://www.python.org/downloads/) installed, two commands:

```bash
git clone https://github.com/EM917/tiktok-live-translator.git
cd tiktok-live-translator && python3 main.py
```

(On Windows use `python main.py`.) Everything else is automatic: the first run creates a virtual environment and installs all dependencies (including a bundled static ffmpeg and the denoising model). CLI flags work too:

```bash
python3 main.py "https://www.tiktok.com/@streamer_username/live" --source es
python3 main.py --demo      # preview the UI without connecting to a stream
python3 main.py --doctor    # print the hardware check and recommended config
python3 main.py --ui-lang en   # English interface for this launch only
```

Manual install and updating a clone: [docs/command-line.md](docs/command-line.md#installing-and-updating-a-clone).

### Starting the app

- **macOS**: double-click **`TikTok Live Translator.app`** (or `Start.command`);
- **Windows**: double-click **`Start.bat`**;
- CLI: `cd ~/tiktok-live-translator && python3 main.py`.

The UI opens in its **own app window** (no browser tab), remembering the last live link, spoken language and target language — just click Start. **Recent Streams** under the input box lists the streamers you opened recently; one click starts one. Closing the window quits the app, and asks first during a session. Pass `--browser` if you prefer the browser UI.

<a id="en-interface-language"></a>

## Interface Language

<sub>[中文](#zh-interface-language)</sub>

Moved to [docs/interface-language.md](docs/interface-language.md).

<a id="en-automatic-hardware-configuration"></a>

## Automatic Hardware Configuration

<sub>[中文](#zh-automatic-hardware-configuration)</sub>

Moved to [docs/hardware.md](docs/hardware.md#automatic-hardware-configuration).

<a id="en-command-line-flags"></a>

## Command-Line Flags

<sub>[中文](#zh-command-line-flags)</sub>

Moved to [docs/command-line.md](docs/command-line.md#command-line-flags).

<a id="en-translation-engines"></a>

## Translation Engines

<sub>[中文](#zh-translation-engines)</sub>

Moved to [docs/translation-engines.md](docs/translation-engines.md).

<a id="en-fault-tolerance"></a>

## Fault Tolerance

<sub>[中文](#zh-fault-tolerance)</sub>

Moved to [docs/fault-tolerance.md](docs/fault-tolerance.md).

<a id="en-banned-term-alerts"></a>

## Banned-Term Alerts

<sub>[中文](#zh-banned-term-alerts)</sub>

Moved to [docs/banned-terms.md](docs/banned-terms.md).

<a id="en-comment-review"></a>

## Comment Review

<sub>[中文](#zh-comment-review)</sub>

Moved to [docs/comment-review.md](docs/comment-review.md).

<a id="en-architecture"></a>

## Architecture

<sub>[中文](#zh-architecture)</sub>

<p align="center">
  <img src="assets/audio-chain.en.svg" width="1000" alt="Audio pipeline: TikTok live room → stream resolver → ffmpeg denoise → energy VAD → audio queue → Whisper ASR (the glossary supplies hotwords), branching into a translation queue → translation engine (the same glossary supplies term hints and fix-ups) and a banned-term scan, both converging on CaptionServer and the caption surface">
</p>

**[Open the explorable version ↗](https://em917.github.io/tiktok-live-translator/architecture/audio-chain.en.html)** — search nodes, focus a component to see its authored upstream and downstream, trace a directed route, play the guided chapters.

The same topology as Mermaid and where the diagram was drawn from: [docs/architecture/mermaid.md](docs/architecture/mermaid.md).

<a id="en-auto-update"></a>

## Auto-update

<sub>[中文](#zh-auto-update)</sub>

On every launch the app silently checks GitHub for the latest release (network failures are silently ignored). When a new version exists, a banner appears at the top of the page:

- **git install** (cloned via `git clone`) → click **Update Now** to run `git pull --ff-only` and restart automatically. If you have uncommitted local changes, the update is refused to avoid overwriting them;
- **ZIP install** → the banner links to the download page instead.

The current version is shown in the page footer.

<a id="en-faq"></a>

## FAQ

<sub>[中文](#zh-faq)</sub>

Moved to [docs/faq.md](docs/faq.md).

<a id="en-privacy-and-usage-boundaries"></a>

## Privacy and Usage Boundaries

<sub>[中文](#zh-privacy-and-usage-boundaries)</sub>

- Recognition always runs locally. Translation stays on your computer with `hymt2`, `hymt2-7b`, `gemma` or `none`; with `deepl`, `google`, `claude` or `openai`, caption text is sent to that provider (for `openai`, unless `OPENAI_BASE_URL` points at a local server such as LM Studio). With `claude`, translated viewer comments and Retranslate requests are sent to Anthropic as well. Viewer comments come from TikTok through TikTokLive, whose connection signing goes through the third-party Euler Stream service.
- Comment Review uses rules only by default, entirely on your computer. If you choose Claude Haiku 5.5 review, the one comment to review (plus the same viewer's previous comment from the last 2 minutes) is sent to Anthropic, with phone numbers, digit strings, links, emails and `@handles` masked first; nicknames, account ids, the streamer's name, timestamps and captions are not sent. See [Comment Review](#comment-review).
- This tool is for personal learning and language-assistance use only. Please comply with TikTok's Terms of Service and local laws — don't use it to rebroadcast or redistribute recordings of other people's content.
- Phone Viewing binds `0.0.0.0` only while it's turned on, and stops as soon as it's turned off; the control page always stays on `127.0.0.1` only. What a phone receives is filtered through an allowlist — it never sees settings, file paths, cookies, or the stream address. The share link's key lives in the local `settings.json` (already gitignored); no data leaves the local network.

<a id="en-acknowledgments"></a>

## Acknowledgments

<sub>[中文](#zh-acknowledgments)</sub>

- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) / [mlx-whisper](https://github.com/ml-explore/mlx-examples) — speech recognition
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — livestream resolution
- [FFmpeg](https://ffmpeg.org/) — audio processing (built-in RNNoise `arnndn` filter)
- [rnnoise-models](https://github.com/GregorR/rnnoise-models) — denoising model (beguiling-drafter)
- [Ollama](https://github.com/ollama/ollama) + [Hy-MT2](https://github.com/Tencent-Hunyuan/Hy-MT2) / [TranslateGemma](https://ollama.com/library/translategemma) — local translation

<a id="en-author-license"></a>

## Author & License

<sub>[中文](#zh-author-license)</sub>

Copyright © 2026 [Elon Mei (EM917)](https://github.com/EM917). Released under the [MIT License](LICENSE).

---

<a id="zh"></a>

# TikTok 直播同传

<p align="center"><img src="assets/icon-1024.png" width="128" alt="icon"></p>

<p align="center"><img src="assets/demo.zh.gif" width="760" alt="演示：每句西语原文先上屏，中文译文稍后在下方补上；右侧弹幕面板同时翻译观众评论"></p>

**Language / 语言：[English](#en) | 中文**

---

TikTok 直播的实时双语字幕工具，为西语带货直播而做。粘贴直播间地址，程序就把主播说的话
实时转写：每句识别出来立刻上屏，译文稍后在它下方补上。观众弹幕同时翻译。

给不懂西语、又要跟西语带货直播的团队用：中控在电脑上看，同事用手机跟着看。上手见
[快速开始](#快速开始)。

字幕从不等译文：原文先上屏。识别跟不上时音频在队列里排着，窗口上会说明。语音识别
永远在本机完成，用本地模型时翻译也在本机——不需要 API Key，没有使用费用。

界面有中文和英文两种。新装的程序跟随系统语言，「设置 → 界面语言 · Language」可以切换，
见[界面语言](#界面语言)。

<a id="zh-features"></a>

## 特性

<sub>[English](#en-features)</sub>

- 🎙️ **实时字幕，原文先出** —— OpenAI Whisper 双后端（faster-whisper / MLX），支持 90+ 语言，默认只在西语和英语之间识别。每句识别出来立刻上屏，译文随后补在它下方；最新一句还会以大字显示在窗口底部
- 🌐 **本地优先的翻译** —— Hy-MT2（Apache 2.0，离线免费）经 Ollama 提供两档，另可选 TranslateGemma、DeepL（带原生术语表）、Google 免费接口、Claude API 和任意 OpenAI 兼容接口。默认用 Hy-MT2 1.8B，其次 TranslateGemma，两个都没装时用 Google 免费接口；7B 不会被自动选用。见[翻译引擎](#翻译引擎)
- 🏷️ **按品牌、按主播的词表** —— 全局词表、主播词表、品牌词表（在「本场品牌」下拉里按场选择）共同决定商品名怎么识别、怎么翻译，对所有引擎都生效。见[词表](docs/translation-engines.zh-CN.md#词表)
- 💬 **观众弹幕翻译** —— 评论通过 TikTokLive 从直播间获取，当前引擎是本地模型（Hy-MT2 1.8B 或 TranslateGemma）或 Claude 时翻译；用 Google、DeepL、OpenAI 或 7B 时「观众弹幕」面板只显示原文。可用 `--no-comments` 关闭，见[常见问题](docs/faq.zh-CN.md#zh-viewer-comments)
- 🔁 **直播中换主播** —— 点顶栏「换主播」，不用先点「停止」就能改听另一个主播：从「最近的直播间」里选或粘贴直播间地址，选好本场品牌，再点一次确认。主播语言沿用当前设置，字幕历史里会插一条分隔线标出新主播从哪里开始
- 📱 **手机同看** —— 同一 Wi-Fi 下的同事扫二维码，就能在手机上看字幕和报警，页面按各自手机的语言显示；只看，不能操作；默认关闭。见[常见问题](docs/faq.zh-CN.md#zh-phone-viewing)
- 🌍 **中英文界面** —— 新装跟随系统语言，也可以在「设置」里切换。见[界面语言](#界面语言)
- ✅ **启动自检** —— 每项能力实际执行而非检查配置：降噪实跑 RNNoise，翻译实际请求引擎，审计日志实际写入。有项目未生效时，首页上那一行自动展开并附处理步骤，页面顶部还会出现一条提示，待机和直播中都在，直到之后的自检通过
- 🔄 **抗故障** —— 最多七种办法找流地址；重连时分得清断网和下播；识别跟不上时音频排队而不是丢掉。见[抗故障](#zh-fault-tolerance)
- 🎵 **人声降噪** —— RNNoise 抑制背景音乐，针对持续 BGM 的直播间调校
- ⚡ **硬件自动配置** —— 检测可用加速器（Apple Silicon GPU / NVIDIA CUDA / CPU），选择仍能实时运行的最大模型
- 📊 **延迟可观测** —— 窗口底部实时显示：一个词说出口后最多要等多久才被识别并检查完（中位数与 P95），拆成切段与识别两部分，另加译文再等多久。审计日志逐段记录：采纳的文本、被质量过滤丢弃的候选、命中的违禁词，以及随后到达的译文
- 🚨 **违禁词报警（默认关闭）** —— 用于带货直播的合规监听：在识别出的原文上做三级匹配（命中 / 变体 / 疑似），不依赖翻译，短语跨字幕边界同样可命中。开关在「设置 → 违禁词报警」。见[违禁词报警](#违禁词报警)
- 👀 **弹幕留意（默认开，只用规则）** —— 在观众弹幕里发现辱骂和威胁、说假货或号召举报、刷屏、引流：规则明确命中标红，拿不准标琥珀，窗口顶部有一行提示，关注名单里的人每条弹幕都高亮。只读，不能删弹幕或禁言；手机同看上没有。可选 Claude Haiku 5.5 复核，只加不减。「设置 → 弹幕留意」里可以关。见[弹幕留意](#弹幕留意)

<a id="zh-screenshots"></a>

## 界面截图

<sub>[English](#en-screenshots)</sub>

<table>
  <tr>
    <td width="50%"><img src="assets/screenshots/zh/home.png" width="100%" alt="首页：直播间地址与「开始翻译」、主播语言与本场品牌下拉、最近的直播间，以及含启动自检、翻译引擎、违禁词报警、磁盘空间、界面语言的「设置」分组"><br><sub>首页：粘贴直播间地址，点「开始翻译」。「设置」在下方，每项一行，点开才展开。</sub></td>
    <td width="50%"><img src="assets/screenshots/zh/live.png" width="100%" alt="直播中：每句西语原文下面是中文译文，右侧是翻译后的观众弹幕，底部大字显示最新一句，下方是延迟统计"><br><sub>直播中：原文先出，译文在下，弹幕在旁。</sub></td>
  </tr>
  <tr>
    <td width="50%"><img src="assets/screenshots/zh/switch-streamer.png" width="100%" alt="直播中的「换主播」面板：新主播地址、标出当前主播的最近直播间、本场品牌下拉、沿用的主播语言，以及要求再点一次才切换的按钮"><br><sub>换主播：选好下一个主播，再点一次确认。</sub></td>
    <td width="50%"><img src="assets/screenshots/zh/share-panel.png" width="100%" alt="「手机同看」面板：二维码、局域网链接、复制链接、换一个链接、停止同看，以及 1 人在看（上限 12）"><br><sub>手机同看：同一 Wi-Fi 下的手机扫码或打开链接观看。</sub></td>
  </tr>
  <tr>
    <td width="50%" align="center"><img src="assets/screenshots/zh/phone.png" width="240" alt="手机页：已连接、直播中，每句西语原文带中文译文和「重译」按钮，观众弹幕栏，只看模式提示，语言切换与提示音按钮"><br><sub>手机页：只看，按手机自己的语言显示。</sub></td>
    <td width="50%"><img src="assets/screenshots/zh/settings-language.png" width="100%" alt="「设置」分组里展开的「界面语言 · Language」一行，选的是跟随系统（中文），并说明手机同看页按各自手机的语言显示"><br><sub>设置 → 界面语言 · Language：跟随系统、中文或 English。</sub></td>
  </tr>
</table>

<sub>上面的截图和演示动图都是虚构的直播，由 <code>tools/readme_shots.py</code> 生成。</sub>

<a id="zh-disk-space"></a>

## 磁盘空间

<sub>[English](#en-disk-space)</sub>

已移到 [docs/hardware.zh-CN.md](docs/hardware.zh-CN.md#磁盘空间)。

<a id="zh-quick-start"></a>

## 快速开始

<sub>[English](#en-quick-start)</sub>

### 无需终端的安装方式（推荐）

1. **下载**：到[最新 Release](https://github.com/EM917/tiktok-live-translator/releases/latest) 的 Assets 里下载 **TikTok-Live-Translator-vX.Y.Z.zip**，解压到任意位置（例如桌面）——下载「Source code (zip)」也一样能用。也可以点仓库页绿色 **Code** 按钮 → **Download ZIP**（取的是最新开发版）。
2. **装 Python**（免费，只装一次）：到 [python.org/downloads](https://www.python.org/downloads/) 下载安装包，按默认选项装完即可。忘了装也没关系——启动器发现没有 Python 时会自动打开下载页提示你。
3. **启动**：
   - **macOS**：双击文件夹里的 **`TikTok Live Translator.app`**。首次打开如提示「无法打开」：旧系统右键 → 打开；**macOS 15 及以后**要到 **系统设置 → 隐私与安全性**，拉到页面底部点 **「仍要打开」**（只需一次）。可以把它拖到 Dock 常驻，但**不要把它拖出这个文件夹**。
   - **Windows**：双击 **`Start.bat`**。若弹出「发布者未知」的安全警告，点「运行」即可（本工具完全开源，代码就在这个文件夹里）。运行期间会有一个黑色文字窗口——**它就是程序本身，请保持打开**，字幕显示在另外弹出的应用窗口里。

系统要求与窗口内核：[docs/hardware.zh-CN.md](docs/hardware.zh-CN.md#最低系统要求与窗口内核)。

首次启动会自动安装全部组件（需要几分钟，屏幕上有提示；首次识别还会自动下载语音模型，页面上能看到进度），之后每次秒开。窗口打开后：**粘贴直播间地址（或只输入主播用户名）→ 确认「主播语言」（默认西班牙语 + 英语）和顶栏里要译成的语言 → 点「开始翻译」**。直播中点「**换主播**」可以改听另一个主播；点「**停止**」回到首页，这一场的字幕留在下方「上一场字幕」里。

### 命令行方式

装好 [Python 3.9+](https://www.python.org/downloads/) 后两条命令：

```bash
git clone https://github.com/EM917/tiktok-live-translator.git
cd tiktok-live-translator && python3 main.py
```

（Windows 用 `python main.py`）其余全自动：首次运行自动创建虚拟环境、安装全部依赖（含内置 ffmpeg 和降噪模型）。也可以直接传参：

```bash
python3 main.py "https://www.tiktok.com/@主播用户名/live" --source es
python3 main.py --demo      # 不连直播，预览界面
python3 main.py --doctor    # 打印硬件检测结果和推荐配置
python3 main.py --ui-lang zh   # 只这一次用中文界面
```

手动安装与更新克隆：[docs/command-line.zh-CN.md](docs/command-line.zh-CN.md#克隆安装与更新)。

### 启动方式

- **macOS**：双击 **`TikTok Live Translator.app`**（或 `Start.command`）；
- **Windows**：双击 **`Start.bat`**；
- 命令行：`cd ~/tiktok-live-translator && python3 main.py`。

界面在**独立应用窗口**中打开（不占浏览器标签页），上次填过的直播间地址、选过的主播语言和目标语言都会记住，点「开始翻译」即可；输入框下方的「**最近的直播间**」列着最近打开过的主播，点一下就直接开始。关掉窗口就是退出，直播中会先问一句。想改回浏览器界面加 `--browser`。

<a id="zh-interface-language"></a>

## 界面语言

<sub>[English](#en-interface-language)</sub>

已移到 [docs/interface-language.zh-CN.md](docs/interface-language.zh-CN.md)。

<a id="zh-automatic-hardware-configuration"></a>

## 硬件自动配置

<sub>[English](#en-automatic-hardware-configuration)</sub>

已移到 [docs/hardware.zh-CN.md](docs/hardware.zh-CN.md#硬件自动配置)。

<a id="zh-command-line-flags"></a>

## 命令行参数

<sub>[English](#en-command-line-flags)</sub>

已移到 [docs/command-line.zh-CN.md](docs/command-line.zh-CN.md#命令行参数)。

<a id="zh-translation-engines"></a>

## 翻译引擎

<sub>[English](#en-translation-engines)</sub>

已移到 [docs/translation-engines.zh-CN.md](docs/translation-engines.zh-CN.md)。

<a id="zh-fault-tolerance"></a>

## 抗故障

<sub>[English](#en-fault-tolerance)</sub>

已移到 [docs/fault-tolerance.zh-CN.md](docs/fault-tolerance.zh-CN.md)。

<a id="zh-banned-term-alerts"></a>

## 违禁词报警

<sub>[English](#en-banned-term-alerts)</sub>

已移到 [docs/banned-terms.zh-CN.md](docs/banned-terms.zh-CN.md)。

<a id="zh-comment-review"></a>

## 弹幕留意

<sub>[English](#en-comment-review)</sub>

已移到 [docs/comment-review.zh-CN.md](docs/comment-review.zh-CN.md)。

<a id="zh-architecture"></a>

## 架构

<sub>[English](#en-architecture)</sub>

<p align="center">
  <img src="assets/audio-chain.zh.svg" width="1000" alt="音频链路：TikTok 直播间 → 流地址解析 → ffmpeg 抽音降噪 → 能量 VAD 切段 → 音频队列 → Whisper 识别（领域词表供识别热词），再分出翻译队列 → 翻译引擎（同一份词表供术语提示与译文兜底）与违禁词检测两条支线，汇入 CaptionServer 与字幕界面">
</p>

**[打开可探索版本 ↗](https://em917.github.io/tiktok-live-translator/architecture/audio-chain.zh.html)** —— 搜索节点、聚焦某个组件看它的上下游、追踪有向路径、播放引导章节。

同一份拓扑的 Mermaid 版本和图的出处：[docs/architecture/mermaid.zh-CN.md](docs/architecture/mermaid.zh-CN.md)。

<a id="zh-auto-update"></a>

## 自动更新

<sub>[English](#en-auto-update)</sub>

每次启动时会静默检查 GitHub 上的最新版本（网络失败一律无声跳过）。有新版本时页面顶部会出现横幅：

- **git 安装**（`git clone` 的）→ 点「一键更新」自动 `git pull --ff-only` 并重启程序。本地有未提交修改时会拒绝更新以免覆盖你的改动；
- **ZIP 安装** → 横幅提供下载页链接，手动替换即可。

界面底部会显示当前版本号。

<a id="zh-faq"></a>

## 常见问题

<sub>[English](#en-faq)</sub>

已移到 [docs/faq.zh-CN.md](docs/faq.zh-CN.md)。

<a id="zh-privacy-and-usage-boundaries"></a>

## 隐私与使用边界

<sub>[English](#en-privacy-and-usage-boundaries)</sub>

- 识别永远在本地；翻译用 `hymt2`、`hymt2-7b`、`gemma` 或 `none` 时不出本机，用 `deepl`、`google`、`claude` 或 `openai` 时字幕文本会发给对应服务商（`openai` 的 `OPENAI_BASE_URL` 指向 LM Studio 这类本机服务时除外）。用 `claude` 时，翻译后的观众弹幕和「重译」的请求也会发给 Anthropic。观众弹幕经 TikTokLive 从 TikTok 获取，连接签名经第三方 Euler Stream 服务。
- 弹幕留意默认只用规则，全部在本机完成。选了 Claude Haiku 5.5 复核之后，需要复核的那一条弹幕（加上同一人 2 分钟内的前一条）会发给 Anthropic：电话、数字串、链接、邮箱和 `@账号` 先打码，昵称、账号 id、主播名、时间和字幕不发。见[弹幕留意](#弹幕留意)。
- 本工具仅供个人学习、语言辅助用途；请遵守 TikTok 服务条款与当地法律，不要用于转播、录制分发他人内容。
- 手机同看只在打开期间才监听 `0.0.0.0`，关闭即停；控制面始终只在 `127.0.0.1`。手机端拿到的是白名单过滤后的字段，看不到设置、文件路径、Cookie、直播流地址；链接里的钥匙存在本机 `settings.json`（已在 `.gitignore` 里）；数据不出局域网。

<a id="zh-acknowledgments"></a>

## 致谢

<sub>[English](#en-acknowledgments)</sub>

- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) / [mlx-whisper](https://github.com/ml-explore/mlx-examples) —— 语音识别
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) —— 直播流解析
- [FFmpeg](https://ffmpeg.org/) —— 音频处理（内置 RNNoise `arnndn` 滤镜）
- [rnnoise-models](https://github.com/GregorR/rnnoise-models) —— 降噪模型（beguiling-drafter）
- [Ollama](https://github.com/ollama/ollama) + [Hy-MT2](https://github.com/Tencent-Hunyuan/Hy-MT2) / [TranslateGemma](https://ollama.com/library/translategemma) —— 本地翻译

<a id="zh-author-license"></a>

## 作者与许可

<sub>[English](#en-author-license)</sub>

Copyright © 2026 [Elon Mei (EM917)](https://github.com/EM917)，以 [MIT 许可](LICENSE) 发布。
