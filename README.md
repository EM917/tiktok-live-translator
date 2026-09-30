# TikTok 直播同传 · TikTok Live Translator

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

**Language / 语言：English | [中文](README.zh-CN.md)**

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

## Features

- 🎙️ **Live captions, original first** — OpenAI Whisper with two backends (faster-whisper / MLX), 90+ languages, limited to Spanish + English by default. Each line appears as soon as it is recognised and its translation fills in beneath it; the latest line is also shown large at the bottom of the window
- 🌐 **Local-first translation** — Hy-MT2 (Apache 2.0, offline, free) in two sizes through Ollama, with TranslateGemma, DeepL (with a native glossary), Google's free endpoint, the Claude API and any OpenAI-compatible API as alternatives. By default Hy-MT2 1.8B is used, then TranslateGemma, and Google's free endpoint when neither is installed; 7B is never picked automatically. See [Translation Engines](#translation-engines)
- 🏷️ **Glossaries per brand and streamer** — a global glossary, one per streamer and one per brand (picked in the Brand menu for the session) steer how product names are recognised and translated, with every engine. See [Glossaries](#glossaries)
- 💬 **Viewer comment translation** — comments are fetched from the live stream through TikTokLive and translated while a local engine (Hy-MT2 1.8B or TranslateGemma) is active; with a remote engine or 7B the Comments panel shows the original text. Disable with `--no-comments`; see [the FAQ](#viewer-comments)
- 🔁 **Switch Streamer during a session** — Switch Streamer in the top bar moves to another streamer without clicking Stop first: pick from Recent Streams or paste a live link, choose the brand, then click again to confirm. The spoken language carries over, and a divider in the caption history marks where the new streamer begins
- 📱 **Phone Viewing** — colleagues on the same Wi-Fi scan a QR code and follow captions and alerts on their phones, each page in its phone's own language; view only, no control; off by default. See [the FAQ](#phone-viewing)
- 🌍 **English and Chinese interface** — follows the system language on a new install, switchable in Settings. See [Interface Language](#interface-language)
- ✅ **Startup Check** — each capability is executed rather than inspected: noise reduction processes a sample through RNNoise, translation queries the engine, the audit log performs a write. A failing check opens its row on the home page with a remediation step, and stays in a banner at the top of the window, idle or live, until a later check passes
- 🔄 **Fault tolerance** — up to seven ways to find the stream URL, reconnects that tell a network drop from the end of the stream, and audio queued rather than lost when recognition falls behind. See [Fault Tolerance](#fault-tolerance)
- 🎵 **Voice-focused noise reduction** — RNNoise suppresses background music, tuned for streams with continuous BGM
- ⚡ **Automatic hardware configuration** — detects the available accelerator (Apple Silicon GPU / NVIDIA CUDA / CPU) and selects the largest model that still runs in real time
- 📊 **Observable latency** — a live readout at the bottom of the window: the longest a spoken word waits before it is recognised and checked (median and P95), split into segmentation and recognition, plus the time the translation adds. An audit log records each segment: accepted text, candidates rejected by the quality filter, banned-term matches, and the translation that followed
- 🚨 **Banned-term alerts, off by default** — for compliance monitoring of live selling: three-tier matching (exact / variant / similar) against the recognised source text, independent of translation and across caption boundaries. The switch is in Settings → Banned-Term Alerts. See [Banned-Term Alerts](#banned-term-alerts)

## Screenshots

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

## Disk Space

Everything runs locally, so the models live on your machine. The figures below come
from a reference installation; exact sizes vary with platform and package
versions.

| Component | Size | When |
|---|---|---|
| Runtime environment (`.venv`) | 1.4 GB | First launch |
| Speech model — `large-v3` (Apple Silicon GPU / CUDA) | 2.9 GB | First recognition |
| Speech model — `large-v3-turbo` (CPU path) | 1.5 GB | First recognition |
| Ollama application | 0.2 GB | Manual, once |
| Translation model — Hy-MT2 1.8B | 1.1 GB | Automatic on first launch after Ollama |
| Translation model — Hy-MT2 7B (optional) | 4.6 GB | Only if you pull it |
| Denoising model | 0.3 MB | First stream |

**Reserve about 6 GB** for a typical installation: runtime, `large-v3` and the
1.8B translation model. Add 4.6 GB if you also want the 7B tier. Machines on
the CPU path need roughly 4.5 GB, since `large-v3-turbo` is smaller.

Downloads are one-time. Models are shared with any other tool using the same
caches, and are not duplicated per project.

Where they are stored, if you need to reclaim space:

```
~/.cache/huggingface/hub     speech models
~/.ollama/models             translation models
<project>/.venv              runtime environment
<project>/logs               audit logs
```

Audit logs grow by roughly **0.3 MB per hour** of monitoring — about 2 MB for
an eight-hour day, or 0.1 GB a month. They are plain JSONL and safe to delete
once a session has been reviewed.

## Quick Start

### Installation without a terminal (recommended)

1. **Download**: grab **TikTok-Live-Translator-vX.Y.Z.zip** from the [latest release](https://github.com/EM917/tiktok-live-translator/releases/latest)'s Assets and unzip it anywhere (e.g. your Desktop) — "Source code (zip)" works too. The green **Code** button → **Download ZIP** also works (latest dev snapshot).
2. **Install Python** (free, one time only): grab the installer from [python.org/downloads](https://www.python.org/downloads/) and install with the default options. Forgot? No problem — the launcher will detect it and open the download page for you.
3. **Launch**:
   - **macOS**: double-click **`TikTok Live Translator.app`** in the folder. If the first launch is blocked ("cannot be opened"): on older systems right-click → Open; on **macOS 15 and later** go to **System Settings → Privacy & Security**, scroll to the bottom and click **"Open Anyway"** (one time only). Feel free to drag it to the Dock — but **don't move it out of this folder**.
   - **Windows**: double-click **`Start.bat`**. If a "publisher unknown" security warning pops up, click "Run" (this tool is fully open source — the code is right there in the folder). A black text window stays open while running — **that window is the app itself; keep it open**. Captions appear in the separate app window.

The interface window itself is rendered by the system's built-in browser engine
(WKWebView on macOS, WebView2 on Windows), not a bundled one. On macOS the
highest-requirement CSS feature currently used is `:focus-visible`, which needs
**Safari 15.4 / macOS 12.3 (Monterey) or later**, based on reading the CSS this
project ships rather than testing on that exact OS build — Apple has
historically also shipped point releases of Safari to the two prior major
macOS versions, so some older systems may already have it too. Below that, the
app still runs and shows captions; only the keyboard-focus outline on a few
controls falls back to the system default. WebView2 on Windows auto-updates independently of Windows
itself, so there is no practical minimum version there — see the "publisher
unknown" note above if Windows Defender SmartScreen has not seen WebView2 on
this machine before.

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

> Prefer to control the install yourself? `bash setup.sh` (macOS/Linux) or `powershell -ExecutionPolicy Bypass -File setup.ps1` (Windows) does the same steps explicitly.

> **Cloned an older version before?** Don't re-clone (it fails with `destination path already exists`) — just `cd` into the folder, run `git pull`, and start it; from v0.2.0 on you can update with one click from the page itself.

### Starting the app

- **macOS**: double-click **`TikTok Live Translator.app`** (or `Start.command`);
- **Windows**: double-click **`Start.bat`**;
- CLI: `cd ~/tiktok-live-translator && python3 main.py`.

The UI opens in its **own app window** (no browser tab), remembering the last live link, spoken language and target language — just click Start. **Recent Streams** under the input box lists the streamers you opened recently; one click starts one. Closing the window quits the app, and asks first during a session. Pass `--browser` if you prefer the browser UI.

## Interface Language

The interface — the app window, its dialogs and notifications, and the phone
page — is in English or Chinese.

- A new install follows the system language: on macOS, the language set for
  this app in System Settings, otherwise the preferred language; on Windows,
  the display language. A Chinese system language (Simplified or Traditional)
  gives the Chinese interface, anything else English, and Chinese if it
  cannot be detected.
- An install that was already in use before the English interface existed
  stays in Chinese after updating.
- To switch, open **Settings → App Language** on the home page and choose
  System, 中文 or English. The window reloads once. Settings is hidden during
  a session, so switch between sessions.
- `python3 main.py --ui-lang en` (or `zh`) sets the language for one launch
  and saves nothing.
- On a new install with an English interface, captions are translated into
  English until you choose another language in the top bar.
- Phone Viewing pages follow each phone's own language, with a toggle at the
  bottom of the page.
- The app name in the Dock, the menu bar and ⌘Tab follows the macOS system
  language (TikTok 直播同传 on a Chinese system). The file is always
  `TikTok Live Translator.app`.
- Terminal output, logs and the audit log are always in Chinese, as is the
  text `Start.command` and `Start.bat` print in their terminal window. The
  launchers' error dialogs, which appear before the app can read the
  language setting, show Chinese and English together. Streamer names,
  glossaries, captions and comments are data; the interface language never
  changes them.

Notes for maintainers: [docs/i18n.md](docs/i18n.md) (in Chinese).

## Automatic Hardware Configuration

`python main.py --doctor` reports the hardware detection results. Any flag not set explicitly at startup is filled in according to the table below:

| Hardware | Selected configuration | Result |
|---|---|---|
| Apple Silicon (M1 or later) | MLX GPU backend + `large-v3` | Most accurate model, runs ~5x faster than real time |
| NVIDIA GPU | CUDA + `large-v3` (float16) | Most accurate model, plenty of speed headroom |
| CPU (≥8 cores, ≥8 GB RAM) | CPU + `small` (int8) | Real-time capable, moderate accuracy |
| Lower-specification CPU | CPU + `base` (int8) | Prioritises keeping pace over accuracy |

Benchmark reference (M3 Pro, 90 seconds of recorded livestream audio; RTF = recognition time / audio duration, which must remain below 1 to avoid dropping segments):

| Config | RTF |
|---|---|
| MLX GPU + large-v3 | **0.21** ✅ |
| CPU + large-v3-turbo | 0.99 ⚠️ borderline |
| CPU + large-v3 | 1.22 ❌ drops segments |

Every selected value can be overridden with a command-line flag.

## Command-Line Flags

| Flag | Description | Default |
|------|------|------|
| `--target` | Target language (`zh-CN`/`en`/`ja`/`ko`/…; can also be switched anytime in the top bar) | last choice in the UI; otherwise `zh-CN` (`en` on a new install with an English interface) |
| `--source` | Streamer's language(s): a single code (`es`/`en`/`ja`/…), or a comma list to auto-detect within just those (e.g. `es,en`, max 4, first = primary; a detected language outside the list triggers one forced re-run) | last choice in the UI; otherwise `es,en` |
| `--backend` | Recognition backend: `mlx` (Apple GPU) / `ct2` (faster-whisper) / `auto` | `auto` |
| `--model` | Whisper model: `tiny`/`base`/`small`/`medium`/`large-v3`/`large-v3-turbo` | auto by hardware |
| `--device` | `auto`/`cpu`/`cuda` | auto by hardware |
| `--compute-type` | ct2 precision (`int8`/`float16`/…) | auto by hardware |
| `--beam` | Beam search width (larger = more accurate but slower; `1` = greedy; ct2 backend only) | `5` |
| `--context` | Enable rolling context. **Off by default**: measured to trigger repetition loops that badly hurt recall | off |
| `--asr-temperature` | Decoding temperature. **Defaults to 0 (single pass)**: Whisper otherwise re-decodes a segment at up to six temperatures when quality checks fail, which measured 25s on music-heavy audio | `0` |
| `--translator` | Translation engine: `auto`/`hymt2`/`hymt2-7b`/`gemma`/`deepl`/`google`/`claude`/`openai`/`none`; see [Translation Engines](#translation-engines) | last choice in the UI; otherwise `auto` |
| `--denoise` | RNNoise voice denoising: `auto`/`on`/`off` | `auto` (on) |
| `--port` | Local UI port | `8765` |
| `--cookies` | Path to a yt-dlp cookies.txt file (use it when yt-dlp reports that the stream needs a login) | none |
| `--glossary` | Glossary file; see [Glossaries](#glossaries) | `glossary.txt` in the project folder, created from `glossary.example.txt` on first run |
| `--banned-terms` | Banned-term list; see [Banned-Term Alerts](#banned-term-alerts) | `banned_terms.txt` in the project folder, created from `banned_terms.example.txt` on first run |
| `--demo` | Demo mode — drives only the UI | off |
| `--doctor` | Print the hardware check and recommended config, then exit | off |
| `--no-open` | Don't open the app window or browser on startup | off |
| `--ui-lang` | Interface language for this launch only: `zh`/`en`. Not saved; see [Interface Language](#interface-language) | the App Language setting |

## Translation Engines

Installing [Ollama](https://ollama.com/download) completes the setup. On the
next launch the app starts it if required, downloads the 1.1 GB Hy-MT2
1.8B model through Ollama's API with on-screen progress, and switches to it. No
terminal commands are involved; the Whisper model is provisioned the same way.

Pulling the larger tier makes it available for strong re-translation and for an
explicit `--translator hymt2-7b`. It is never selected automatically; see
[Choosing an engine](#choosing-an-engine).

```bash
ollama pull hf.co/tencent/Hy-MT2-7B-GGUF:Q4_K_M     # 4.6 GB, ~16 GB RAM
```

| Value | Engine |
|---|---|
| `auto` (default) | Selects the best engine present: `hymt2` → `gemma` → `google`. 7B is never selected automatically |
| `hymt2` | Hy-MT2 1.8B (Tencent, Apache 2.0). Recommended for most machines |
| `hymt2-7b` | Hy-MT2 7B, the larger local tier. Opt-in only; see [Choosing an engine](#choosing-an-engine) |
| `gemma` | TranslateGemma 4B. `OLLAMA_TRANSLATE_MODEL` selects `translategemma:12b`/`27b`. Not in the Translation Engine menu; pass `--translator gemma` |
| `deepl` | Key entered in Settings → Translation Engine (or `DEEPL_API_KEY`). A key ending in `:fx` is routed to the free endpoint automatically. Builds and maintains a native DeepL glossary from your [glossaries](#glossaries); see below. Caption text is sent to DeepL |
| `google` | Google Translate's free endpoint, no key required. Caption text is sent to Google; rate-limited per IP |
| `claude` | Key entered in Settings → Translation Engine (or `ANTHROPIC_API_KEY`); model overridable via `CLAUDE_TRANSLATE_MODEL` |
| `openai` | Key entered in Settings → Translation Engine (or `OPENAI_API_KEY`); optional `OPENAI_BASE_URL`, `OPENAI_MODEL`. Compatible with any OpenAI-style API including local LM Studio / vLLM |
| `none` | Transcription only, no translation |

The engine is chosen in Settings → Translation Engine on the home page, where API
keys are entered too — no terminal and no environment variables. Keys are stored
in `settings.json`, which is git-ignored, and are never sent back to the page;
only the last four characters are shown so you can tell which key is in place.

When the chosen engine cannot be used and the app falls back to another, the
Translation Engine row says Fallback and opens by itself, so a switch to a
network engine is visible rather than silent.

Three lines from real streams where Google's free endpoint took colloquial
Spanish literally and the local model did not (Spanish → Chinese):

| Source | Google | Local model |
|---|---|---|
| *Se mueren lo rico* (extremely good) | ❌ 有钱人死 (the rich die) | ✅ 味道非常好 |
| *tengo un sueño* (I am sleepy) | ❌ 我做了一个梦 (I had a dream) | ✅ 现在感觉很困 |
| *Es vegano* (it is vegan) | ❌ 它是素食主义者 (he is a vegetarian) | ✅ 纯素的 |

### Choosing an engine

Hy-MT2 1.8B is the default: it keeps pace with the stream without slowing
recognition, which matters because every caption waits for recognition before
it appears.

Four engines — Hy-MT2 1.8B and 7B, TranslateGemma 12B and DeepL — were graded
blind on 259 captions from one live session. After correcting for the twelve
pairwise comparisons, the differences that held were all in readability: 7B
read more easily than both 12B and 1.8B, and DeepL more easily than 1.8B. No
engine, DeepL included, made measurably fewer meaning-changing errors than
another. That means no difference was measured, not that they are equivalent.

7B is never selected automatically. In a 92-caption live run with 7B resident,
recognition took about 3.2 s per segment against 1.4 s with the smaller models.
It is still used for [strong re-translation](#re-translating-with-the-strongest-model),
which loads it for one line and unloads it straight after.

The measurements, tables and caveats are in
[docs/engine-benchmarks.md](docs/engine-benchmarks.md).

### Glossaries

Product names are where a general-purpose model goes wrong most often, and a
short list of them is the most direct fix. The glossary is one set of entries
used in three places: the first entries are given to speech recognition as a
hint; entries that occur in a line are passed with that line to a local
translation model (DeepL uses a native glossary instead, see below, while
Google's free endpoint, Claude and OpenAI-compatible APIs take no hints, so
only the fix-up applies to them); and a rule-based fix-up replaces any
glossary entry the translation left untranslated.

It is merged from three files, most specific first:

- `profiles/<streamer-username>.txt` — one streamer's own wording and
  translations, loaded automatically from the live link
- `brands/<brand-id>.txt` — one brand's products, loaded only when the Brand
  menu on the home page (or in Switch Streamer) selects it; for a stream that
  sells a single brand. The menu remembers each streamer's last choice, the
  folder button next to it opens `brands/`, and the menu rescans that folder
  when clicked, with no restart. The file format is in
  [`brands/README.md`](brands/README.md) (in Chinese)
- `glossary.txt` — the global list (`--glossary` points elsewhere)

Where the same wording appears in more than one file, the streamer's profile
wins over the brand list, which wins over the global list. Edits take effect
after Stop → Start. These files hold your own product data and stay on your
computer: git ignores `glossary.txt`, `profiles/*.txt` and `brands/*.txt`, so
editing them never blocks an update.

### DeepL: the native glossary decides everything

DeepL accepts no prompt, so per-sentence glossary injection does nothing for it —
only a **native glossary** held in the account applies. The app builds one
from the glossary in effect for the session (the global, brand and streamer lists
merged). The glossary name carries a fingerprint of its contents, so an edit, or
a different streamer or brand, rebuilds it with no manual step.

Measured on the same 60 lines of real captions:

| | Glossary compliance | Median latency |
|---|---|---|
| DeepL, no glossary | 26.5% | 388 ms |
| DeepL + native glossary | 91.8% | 542 ms |

Nearly all of those 65 points are product names. With no glossary in place DeepL
still returns fluent Chinese, so nothing looks wrong on screen — which is why the
Startup Check actually builds the glossary and reports its entry count instead of
merely checking that a key is present.

The following constraints are measured, not assumed:

- **The free tier permits exactly one glossary** (creating a second returns 456).
  Only glossaries the app created are deleted — their names start with
  `tlt-`. Glossaries you created in the DeepL dashboard are left alone.
- **A glossary is a hint, not a substitution.** `las gotas → 维生素滴剂` applies,
  while `la limpieza → 排毒粉` does not fire inside `me paso a la limpieza`. The
  post-translation rule-based replacement is therefore retained.
- **Traditional Chinese gets no native glossary.** DeepL offers a single `zh`
  glossary target and `glossary.txt` is written in Simplified Chinese; attaching
  it pushes Simplified terms into Traditional output.
- **The source language must be explicit.** When transcription reports no
  language, no glossary is attached rather than guessing — a wrong guess forces
  the whole line through the wrong language.

Quota is billed on source length, and the burn rate depends heavily on how
densely the streamer talks — two measured sessions differ by 2.3×:

| Session, every caption through DeepL | Burn rate | 1,000,000 characters covers |
|---|---|---|
| Dense (38.4 min, 567 lines) | 95,824 chars/hour | ~10 hours |
| Sparser (4.4 h, 2,041 lines) | 41,363 chars/hour | ~24 hours |

Read your own remaining budget in Settings → Translation Engine, which shows how
much of the free quota your key has used (from the `used / limit` DeepL returns),
rather than from this table; DeepL's allowance size and renewal terms are theirs
to change, so check their current pricing before planning around a number here.

**Caption text is sent to DeepL.** Local engines never leave the machine; this
one does. The stream being monitored belongs to someone else, and whether that is
acceptable is a business decision.

### Re-translating with the strongest model

The strongest local model installed (Hy-MT2 7B if you pulled it, otherwise
1.8B) is applied per sentence rather than for a period of time. What justifies
it is specific content — prices, promotional conditions, health claims — which
is episodic, and only the operator or the detector knows which sentence that
is. Measured cost: loading the model takes 1.9 s and a complete one-shot call
2.3 s, after which it is unloaded, leaving recognition unaffected. Keeping it
resident instead would raise recognition from 1.4 s to 3.2 s and alert latency
from 6.8 s to 10.6 s.

Three ways to invoke it:

- **Per caption** — hover a caption and click Retranslate. The line is
  re-translated and marked in the margin. Phones on
  [Phone Viewing](#phone-viewing) have the same button
- **On a banned-term match** — with [alerts](#banned-term-alerts) on, the
  matched sentence is re-translated automatically. The fast translation appears
  first so nothing is delayed; the accurate one replaces it about two seconds
  later
- **After the session** — `python3 tools/retranslate_audit.py` re-translates a
  session's audit log, appending `translation_strong` records without altering
  any existing line, and prints the segments whose translation changed rather
  than replacing anything silently

Measured on the same 259 captions, re-translation with 7B reads more easily
than 1.8B — that difference survived the correction — but it was not shown to
be more correct. Details are in
[docs/engine-benchmarks.md](docs/engine-benchmarks.md#re-translating-with-the-strongest-model).

### Finding translation errors without reading everything

Scanning a session's captions by eye to find mistranslations is slow and will
miss things. `tools/retranslate_audit.py` re-translates a session with the
strongest model and ranks the segments by how much the two models disagree —
the greater the disagreement, the more likely one of them is wrong. It needs no
judge and makes no semantic call of its own.

```bash
python3 tools/retranslate_audit.py
```

On its first run this surfaced `es una orden de 30` being rendered as "30
units" rather than a $30 threshold — a glossary gap nobody had noticed.

Two approaches that were measured and rejected, recorded so they are not
retried: comparing word overlap against a back-translation scores correct and
incorrect translations identically, because Spanish paraphrases legitimately
change words; and asking a local model to judge agreement fails outright —
the 1.8B model called every pair inconsistent, and the 7B model was right four
times in ten.

## Fault Tolerance

A live session can fail quietly in three places:
finding the stream URL, keeping the stream, and keeping up with it. Each is
handled explicitly.

### Finding the stream URL

Up to seven layers are tried in order, each isolated so a bug in one cannot
take down the rest. Resolution stops at the first layer that returns an
address, and each address is checked before the session starts. Every layer
prefers TikTok's audio-only track when one is offered.

1. **Signed-in live page** (macOS) — uses a TikTok sign-in already in the browser
2. **Official live API** — independent of yt-dlp
3. **Signed-in retry** — only when the first signed-in fetch read a usable
   sign-in but the page carried no address, and the live API did not report
   the stream as ended
4. **System WebKit engine loading the live page** (macOS) — measured at 2 s;
   it has returned the stream URL for rooms where the layers before it got none
5. **yt-dlp, anonymous**
6. **yt-dlp with a browser's sign-in**
7. **Live-page fallback** — also tries the sign-in first, anonymous last

yt-dlp can answer "not currently live" for a stream that plays in a browser, so
its answer alone never ends a session: the app reports a stream as ended only
when TikTok says so.

How the sign-in is used:

- Unless a browser is named (`--cookies-browser`, or `cookies_browser_only` in
  `settings.json`), the signed-in step reads Safari only. Other browsers are
  read only by the later sign-in layers, after the anonymous ones have failed,
  and once Safari shows a sign-in no other browser's data is read.
- The signed-in retry waits 3 s, or the rest of the 8 s gap below when an
  anonymous request went out in between, then makes the identical fetch once
  more, before the slower layers. Measured: some rooms serve their stream URL
  only to signed-in viewers, and a signed-in request made within 3 s of an
  anonymous one came back without it, so the app leaves 8 s between them.
  One reconnect on 2026-09-17, for a room whose every earlier resolution that
  day had taken 0.6–1.4 s: the signed-in page carried no address at 0.5 s,
  WebKit timed out after 25 s, both yt-dlp calls reported the stream as not
  live, and the same signed-in fetch returned the address at 31 s, 37.6 s in
  total. Why the first fetch carried no address is not known.
- An `http://` TikTok link is resolved as `https://`, so the sign-in is never
  sent in clear text.
- The comment connection starts only after the first resolution returns:
  it opens with an anonymous request for the same page.
- Reading Safari's cookies needs Full Disk Access for the app (see the
  [Browser Login FAQ](#browser-login)). Without a readable sign-in, monitoring
  still starts, resolution stays anonymous, and a persistent notice says what
  to do.

When TikTok's live API withholds the stream URL (code 4003110) and no later
layer finds one, the app tries three times, 20 s apart, and then says so on
screen; other failures are reported right away. When the stream plays in your
browser but the app cannot find its address, you can paste **the live link and
a .flv address from your browser together**, separated by a space: the link
drives comments and the glossary, and the .flv address is used as given for
the audio, skipping every layer above. Measured: these signed addresses stay
valid for about two weeks, so one capture covers a whole stream. See also
[code 4003110](#code-4003110) in the FAQ.

### Keeping the stream

- A dropped stream reconnects with a freshly resolved URL, telling a network
  interruption from the end of the stream. Before resolving again the app
  checks that it can reach TikTok; while it cannot, it waits without spending
  reconnect attempts, for up to 30 minutes.
- If TikTok hasn't reported the stream as ended, the app waits up to 10
  minutes for it to resume.
- The app gives up once six connection attempts in a row bring no audio at
  all (two for a pasted stream URL); an attempt that played any audio resets
  the count. A pasted stream URL that stops serving data after playing for
  30 s or more ends the session instead of reconnecting.
- The comment connection can go half-open: the Comments panel still shows
  Connected, but no comments arrive. After 15 minutes of silence while
  connected, the app reconnects it and records that in the session audit.
- yt-dlp is kept current in the background.
- While monitoring, the computer is kept from going to sleep on its own, and a
  gap where the app did not run at all is recorded and shown on screen.

### Keeping up

- When recognition falls behind, audio waits in a queue: a notice appears once
  it is 10 s behind, detection is marked degraded at 30 s, and only audio more
  than 60 s behind is dropped, with the count shown on screen and written to
  the audit log.
- Translation never slows recognition. Its queue holds four lines and drops
  the oldest when full; a line whose translation was skipped keeps its
  original text.

## Banned-Term Alerts

For compliance monitoring of live selling, the app can raise an alert when the
streamer says something on a banned-term list. Matching runs on the recognised
source text, never on the translation, so an alert does not wait for a
translation engine. It has three tiers — exact; variant (plural, gender,
diminutive); similar (a letter or two misheard, in words of five letters or
more) — ignores case and accents, and catches a phrase split across two
captions.

**Alerts are off by default.** The switch is under **Settings → Banned-Term
Alerts**, a collapsed row on the home page whose summary shows whether alerts
are on and how many terms are active. Open it and turn on **Show alert on
match**; the choice is remembered for the next session.

- With the switch off, detection still runs and every match is still written
  to the audit log (tagged `suppressed: "alerts_off"`); nothing pops up and no
  strong-model re-translation is spent.
- With it on, a match opens a red Possible Banned Terms panel at the top of the
  window, which stays until you clear it. During a session the top bar shows
  Alerts on, the window title counts unseen alerts while the window is in the
  background, the matched sentence is
  [re-translated with the strongest model](#re-translating-with-the-strongest-model),
  and phones on [Phone Viewing](#phone-viewing) see the alert too.
- A system notification, without the term and without sound, is sent only if
  `"alert_os_notify": true` is set in `settings.json`.
- A problem with the list itself — empty, unreadable, or no entry able to
  match — fails the Banned-Term List row of the Startup Check. That row opens
  by itself and a banner stays at the top of the window until it passes.

The list is `banned_terms.txt` in the app folder, one word or phrase per line;
entries starting with `re:` are regular expressions. Save, then click Stop and
Start. It is created from `banned_terms.example.txt` on first run, and updates
never change your copy: compare the two after an update to see what the shipped
list gained.

### Validating your banned-term list against a recorded session

A banned-term list that never fires looks identical to a clean stream. It is worth
proving which of the two you have, because the failure is silent — and it is a
failure of the list, not of the matcher.

The shipped list is a starting point derived from one company's category guide
for supplement and weight-management streams. It misses when a streamer
phrases a claim differently from the list: `eliminar grasa` is listed, but
"eliminar **toda la** grasa" puts words between the two anchors and does not
match. The same gap let "eliminando el exceso de grasa" through in the case
below, which is why `exceso de grasa` has been an entry of its own since
2026-09-02.

Replay a recorded session against your list before trusting it:

```bash
python3 tools/replay_alerts.py                   # re-run a session's audit log through the current list
python3 tools/collision_audit.py --term <word>   # check a new term for false-positive collisions
```

**A historical case, from late August 2026.** The list then shipped with 40
active entries and a *pending business review* section of 7 real phrases
deliberately left commented out. Replayed over a 4.4-hour supplement stream
(2,041 segments):

| Term list at the time | Segments alerted |
|---|---|
| As shipped, 40 active entries | **0** |
| With the 7 commented-out *pending review* entries enabled | 62 |

The stream contained `derretir toda la manteca` ("melt away all the fat"),
`acelerar el metabolismo`, and `desinflamarse y quitar la barriga` — the exact
family the guide banned "all variants" of. The matcher was working the whole
time (its fuzzy tier caught the ASR misspelling `derritir`); the entries that
would have fired were switched off.

A separate pass over the same transcript flagged 126 utterances as worth
alerting on, of which 99 matched neither the active nor the pending entries —
appetite suppression, body shape, organ fat, fatty liver, cholesterol, and one
cancer claim.

On 2026-09-02 the 7 pending entries were enabled, together with 6 from that
audit whose evidence was unambiguous (fatty liver, a cancer claim, excess fat,
a flat stomach, appetite, cravings), each checked with the collision audit and
a replay across 36 recorded sessions. The shipped list has had no pending
section since; a `banned_terms.txt` created before that date still has those
entries commented out.

Treat output like that audit as **candidate terms for human review**, never as
an automatic list update: what counts as a violation is a business judgement,
and a list padded with false positives buries the operator in noise.

## Architecture

<p align="center">
  <img src="assets/audio-chain.en.svg" width="1000" alt="Audio pipeline: TikTok live room → stream resolver → ffmpeg denoise → energy VAD → audio queue → Whisper ASR (the glossary supplies hotwords), branching into a translation queue → translation engine (the same glossary supplies term hints and fix-ups) and a banned-term scan, both converging on CaptionServer and the caption surface">
</p>

**[Open the explorable version ↗](https://em917.github.io/tiktok-live-translator/architecture/audio-chain.en.html)** — search nodes, focus a component to see its authored upstream and downstream, trace a directed route, play the guided chapters.

Drawn from the source at [`430fe06`](https://github.com/EM917/tiktok-live-translator/tree/430fe06c139bb3cadc423221b6f4091702a4d51b), with every component checked against where it lives in the code. The typed source and regeneration steps are in [`docs/architecture/`](docs/architecture/).

<details>
<summary>The same topology as Mermaid — editable without any tooling</summary>

```mermaid
flowchart TD
    URL["TikTok live link"] --> RESOLVE["stream resolver<br/>signed-in page (macOS) → live API → retry → WebKit (macOS) → yt-dlp → +sign-in → page<br/>(audio-only preferred; see Fault Tolerance)"]
    RESOLVE -->|"checked media URL"| FF["ffmpeg → RNNoise noise reduction → 16 kHz PCM"]
    FF --> VAD["energy-VAD segmenter (2.5–9 s)"]
    VAD --> AQ["audio queue<br/>60 s cap"]
    AQ --> ASR["Whisper ASR<br/>MLX GPU / faster-whisper<br/>confidence + hallucination filter"]
    GL["glossary<br/>global · brand · streamer"] -.->|"hotwords"| ASR
    ASR -->|"source caption, no wait"| SRV["CaptionServer"]
    ASR --> TQ["translation queue<br/>4 lines, drop oldest"]
    TQ --> TR["translation engine<br/>Hy-MT2 1.8B / 7B (local) / TranslateGemma / DeepL / Google / Claude / OpenAI / off"]
    GL -.->|"term hints · fix-ups"| TR
    TR -->|"translation filled in"| SRV
    ASR -->|"raw_text"| DET["banned-term scan<br/>exact · variant · similar"]
    DET -->|"alert (when on)"| SRV
    SRV --> WS(("WebSocket"))
    WS --> UI["caption surface<br/>app window · Phone Viewing"]
    FF -.->|"stream drops: re-resolve + reconnect"| RESOLVE
```

</details>

## Auto-update

On every launch the app silently checks GitHub for the latest release (network failures are silently ignored). When a new version exists, a banner appears at the top of the page:

- **git install** (cloned via `git clone`) → click **Update Now** to run `git pull --ff-only` and restart automatically. If you have uncommitted local changes, the update is refused to avoid overwriting them;
- **ZIP install** → the banner links to the download page instead.

The current version is shown in the page footer.

## FAQ

**A Startup Check row is failing.** Each row carries its remediation step
directly beneath it, and a failing check also stays in a banner at the top of
the window, idle or live, until a later check passes. The most frequent causes
are an empty `banned_terms.txt` (no alerts can be raised at all) and an
incompletely downloaded noise-reduction model (delete `models/bd.rnnn` and
start again; it re-downloads). The checks run again on every start, so a
resolved issue clears on the next run. A passing row indicates the capability
was executed, not merely configured.

**The update button reports that something is blocking it.** The message names
the affected files and provides a complete command with your project path and a
Copy button. On builds older than v0.10.4 the machine cannot repair itself, as
the fix is delivered by the update that is being blocked — run
`git pull --ff-only` once in the project directory.

**Performance degrades and audio begins backing up.** Check `ollama ps`. Ollama
retains a model in VRAM for 30 minutes after last use, so switching translation
tiers previously left the earlier model resident; combined with Whisper
large-v3 this can exhaust a 16–18 GB machine and cause paging, presenting as
slower recognition and a growing backlog. Unused tiers are now unloaded at
startup. On older builds, `ollama stop <model>` releases it immediately.

**The stream URL isn't found, although the stream plays in a browser.** yt-dlp
can answer "not currently live" for such a stream, so the app does not stop
there: it tries the layers listed under [Fault Tolerance](#fault-tolerance), in
order, and reports a stream as ended only when TikTok says so. Being signed in
to TikTok in Safari or Chrome helps for some rooms but is usually not required,
and no file needs exporting; cookies stay between your computer and TikTok. A
browser can be pinned with `--cookies-browser safari`, or cookies supplied via
`--cookies cookies.txt`. To have the app read only one browser from now on, set
`"cookies_browser_only": "safari"` in `settings.json`; an explicit
`--cookies-browser` takes precedence over it.

<a id="browser-login"></a>**The Browser Login row of the Startup Check says macOS didn't allow access.** macOS does
not let other apps read a browser's data directory unless the reading app has
Full Disk Access. The entry to add is **not** this .app: the app is a launcher
script that hands over to a Python interpreter, and macOS records the permission
against the interpreter file's path (measured 2026-09-17 on macOS 27: the TCC
log shows `identifier_type=Path` with a `python3.x` path and never mentions the
bundle). The Browser Login row and the error message print the exact path(s) for
your machine. Open System Settings → Privacy & Security → Full Disk Access,
press "+", press ⌘⇧G in the file picker, paste the path, press Return, click
Open, and switch the new entry on; repeat for each path shown. The list shows
the entry as `python3.x`, not under the app's name. Add Terminal too if you
launch with Start.command. Then quit the app completely and reopen it. The path
changes when Python is upgraded or the environment is rebuilt; the row then
shows the new one. This only matters for rooms whose stream address TikTok
serves to signed-in viewers. The check looks only at whether the cookie
store can be read and at login cookie names, across every browser profile; it
never decrypts and never raises a Keychain prompt. Failed resolutions record one
of these codes in the audit log: `blocked_by_system`, `no_browser_data`,
`no_tiktok_cookie`, `not_logged_in`, `cannot_decrypt`, `keychain_wait`.
`cannot_decrypt` means the store holds TikTok cookie names but their values did
not come out of decryption; start again and, if a Keychain dialog asks for
"Chrome Safe Storage", enter the Mac login password and choose Always Allow.

<a id="code-4003110"></a>**"TikTok didn't provide a stream URL for this live stream (code 4003110)."**
That code is TikTok's generic refusal; the response carries no reason, and the
app does not invent one. Before concluding anything, run the built-in check:

```bash
python3 tools/diagnose_room.py @streamer --history-only   # your own logs only, no requests
python3 tools/diagnose_room.py @streamer                  # then the same-minute paired check
```

It first aggregates your own `logs/session-*.jsonl` by streamer with zero
requests — whether this room has ever produced captions on this machine, and
which rooms did — then probes the target and a recently-working control room
through the same API in the same minute (four requests in total, no browser).
The verdict distinguishes "this room is refused while others answer" from "this
machine gets nothing" and never labels a cause. In the meantime, paste the room
link together with the `.flv` address from your browser (see
[Fault Tolerance](#fault-tolerance) above); one capture stays valid for about
two weeks.

**The first start appears stuck downloading the recognition model.** The model
is being retrieved from Hugging Face (large-v3 is approximately 3 GB). Progress
is displayed on the page and this occurs only once.

**All translations fail and captions show only the source language.** Google's
free endpoint, which `auto` uses when no local model is installed,
rate-limits per IP and returns 429 under sustained use. The app pauses
requests for two minutes and recovers automatically, with a banner on the page.
Speech recognition is unaffected. For extended sessions, install a local Hy-MT2
model or choose another engine in Settings → Translation Engine. No network
connection, or a stopped Ollama, produces the same symptom.

**The status reads Live but no captions appear for some time.** This is normally
expected: while the streamer plays music or is not speaking, silent and
low-confidence segments are discarded deliberately. If the streamer is clearly
speaking and nothing appears, try `--denoise off` or a different model size.

**Captions stop after closing the laptop lid or switching networks.** When the
stream drops, the app re-resolves the stream URL and reconnects by itself,
waiting longer between attempts each time (2 s, then 4, 8, 16 and at most
30 s). While the computer cannot reach TikTok it waits without spending
attempts, for up to 30 minutes. It gives up only after six reconnects in a row
bring no audio at all (two for a pasted stream URL); a reconnect that played
any audio resets that count, so a patchy connection keeps reconnecting. If the
window says the stream was interrupted several times and couldn't reconnect,
click Start again. Details: [Keeping the stream](#keeping-the-stream).

**Switching to another streamer during a session.** Click **Switch Streamer**
in the top bar, paste the new live link or @username, or pick one from Recent
Streams (the current streamer is marked and can't be picked), and choose the
brand. The button then reads **Click Again to Switch to @name**; click it
within 6 seconds to confirm. The current session stops and a new one starts
with the same spoken language, re-reading the glossaries and the banned-term
list. Neither streamer has captions until the new one starts speaking, and a
divider in the caption history marks the change. **Stop**, by contrast, ends
monitoring and returns to the home page.

**Recognition cannot keep pace with the stream.** Use a smaller model
(`--model small`) or `--beam 1`. On Apple Silicon, confirm the Startup Check
reports the `mlx` backend rather than `ct2` — the CPU path runs at
approximately real time and accumulates backlog.

**Performance is poor on a Mac with 8 GB of RAM.** Apple Silicon defaults to
`large-v3` (~3 GB). Where memory is constrained, use `--model large-v3-turbo`
or `--model small`.

**Limiting the memory MLX keeps for buffer reuse (Apple Silicon).** Measured on
2026-09-17 on an 18 GB M3 Pro (macOS 27, mlx 0.32.1, Whisper large-v3, fp16,
process up 28 minutes): `footprint` reported the monitor at 7.5 GB, of which
6948 MB was "IOAccelerator (graphics)", that is, Metal buffers, and everything
else was under 0.5 GB; the model weights are about 3.1 GB. These buffers are
wired unified memory and can be neither compressed nor swapped. The machine was
10 GB into swap at the time, which coincided with the on-demand 7B translation
model taking 42–60 s to load. MLX keeps freed buffers in a cache for reuse, and without a limit that cache
grows with the number of distinct segment lengths. The app sets the
limit to 256 MB by default. The value comes from a measurement made the same
day with monitoring stopped (`tools/bench_mlx_cache.py`, the same audio paired
segment by segment): against no limit, recognition time changed by −0.5% with
a 95% confidence interval of −2.2% to +1.0%, the same size as the difference
between two no-limit runs, while graphics memory fell from 4305 MB to 3220 MB.
Keeping no cache at all made recognition about 6% slower. Change it with the
environment variable `TLT_MLX_CACHE_MB`, or with `"mlx_cache_limit_mb"` in
`settings.json` (the environment variable takes precedence); the value is a
non-negative integer in MB, `0` keeps no cache, and `off` sets no limit. The
setting applies to the `mlx` backend only and is read when the recognition
model loads, so restart the app after changing it. The value in effect
is written to the session log's `asr_config` record, and `asr_memory` records
(one when the model is ready, then one every 5 minutes) carry MLX's active,
cache and peak memory figures.

**Sung vocals in background music are transcribed as speech.** RNNoise
suppresses instrumental music effectively but can only partially suppress sung
vocals. Confidence filtering removes most such segments; occasional
false positives are expected.

**The interface is not on port 8765.** If 8765 is occupied, the app
moves to the next free port in 8766–8774 and says so in the terminal. That's
the control page only; Phone Viewing uses a port of its own and reuses the
same one across restarts when it is free, so a printed QR code keeps working —
see the next entry.

<a id="phone-viewing"></a>**Can colleagues watch from their phones?** Yes. Click the phone icon in
the top bar (labelled **Phone Viewing** once the window is at least 1,200 px
wide; there whether idle or live) and click **Start Sharing** in the panel that
opens. The first time, macOS may ask to allow incoming network connections, or
Windows Firewall may ask for access; choose Allow. A phone on the same Wi-Fi as this computer can
scan the QR code or type in the address shown in the panel, and can only view
captions and alerts — it cannot control the app in any way (no start/stop,
engine switch, or settings changes). Each phone page is in that phone's own
language, with a toggle at the bottom. Up to 12 phones can watch at once. The
link carries a key and should be treated like a password: whoever has it can
see captions and alerts; clicking **New Link** disconnects every phone
currently watching, and they need to rescan. When a phone can't connect, the
only thing the app knows is that no connection came in; try, in order:
confirming the phone is on the same Wi-Fi; typing in another address from the
panel's list; opening the same link in a browser on this computer to confirm
the service itself is running. The address is plain `http://` on the LAN, not
a secure context, so there are no system notifications and no screen
wake-lock, and the alert sound may stop once the page is backgrounded or the
phone is locked. v1 only works within the same local network — not across
networks. Each caption also has a **Retranslate** button that asks the
operator's computer's strongest local model to redo that one line; it's
rate-limited per phone (at least 3 seconds between taps, at most 10 a minute)
so one phone tapping repeatedly can't queue up work or slow down the
operator's computer. Each alert has a ✕ to hide it, plus a **Clear Seen**
button to hide everything currently shown — both act only on that one phone:
the operator's alert panel and the audit log are unaffected, and nothing is
sent to the server.

<a id="viewer-comments"></a>**Viewer comments.** The app fetches comments itself through TikTokLive,
which needs Python 3.10 or later; the component installs itself on the first
start. It usually works signed out, and retries once with the browser's TikTok
sign-in when TikTok asks for one. Comments are translated only while the active
engine is a local model (Hy-MT2 1.8B or TranslateGemma); with a remote engine
or 7B the panel shows the original text. Comment translation is for display
only and never part of the alert path. The connection is signed through a
third-party service; see [Privacy](#privacy-and-usage-boundaries).

**Exporting captions.** There is no export function; select and copy the text
from the page. The page retains the most recent 300 lines, and the server
replays the last 100 after a reload or reconnection.

**Closing the console window.** On Windows that console window is the app
itself, so closing it quits the app. On macOS, closing the app window quits it
(during a session it asks first); if launched via `Start.command`, close the
Terminal window or press Ctrl-C.

**Installation fails on a managed computer, or space is insufficient.** The
initial installation requires about 6 GB and access to PyPI, Hugging Face and
ollama.com; see "Disk Space" for the breakdown. Corporate security policies
frequently block these.

## Privacy and Usage Boundaries

- Recognition always runs locally. Translation stays on your computer with `hymt2`, `hymt2-7b`, `gemma` or `none`; with `deepl`, `google`, `claude` or `openai`, caption text is sent to that provider (for `openai`, unless `OPENAI_BASE_URL` points at a local server such as LM Studio). Viewer comments come from TikTok through TikTokLive, whose connection signing goes through the third-party Euler Stream service.
- This tool is for personal learning and language-assistance use only. Please comply with TikTok's Terms of Service and local laws — don't use it to rebroadcast or redistribute recordings of other people's content.
- Phone Viewing binds `0.0.0.0` only while it's turned on, and stops as soon as it's turned off; the control page always stays on `127.0.0.1` only. What a phone receives is filtered through an allowlist — it never sees settings, file paths, cookies, or the stream address. The share link's key lives in the local `settings.json` (already gitignored); no data leaves the local network.

## Acknowledgments

- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) / [mlx-whisper](https://github.com/ml-explore/mlx-examples) — speech recognition
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — livestream resolution
- [FFmpeg](https://ffmpeg.org/) — audio processing (built-in RNNoise `arnndn` filter)
- [rnnoise-models](https://github.com/GregorR/rnnoise-models) — denoising model (beguiling-drafter)
- [Ollama](https://github.com/ollama/ollama) + [Hy-MT2](https://github.com/Tencent-Hunyuan/Hy-MT2) / [TranslateGemma](https://ollama.com/library/translategemma) — local translation

## Author & License

Copyright © 2026 [Elon Mei (EM917)](https://github.com/EM917). Released under the [MIT License](LICENSE).
