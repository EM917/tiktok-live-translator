# TikTok Live Translator (tiktok-live-translator)

English | [中文](README.md)

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
