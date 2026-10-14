# Command line

*Moved from the README (Quick Start and Command-Line Flags). The text is unchanged except for relative links.*

## Installing and updating a clone

> Prefer to control the install yourself? `bash setup.sh` (macOS/Linux) or `powershell -ExecutionPolicy Bypass -File setup.ps1` (Windows) does the same steps explicitly.

> **Cloned an older version before?** Don't re-clone (it fails with `destination path already exists`) — just `cd` into the folder, run `git pull`, and start it; from v0.2.0 on you can update with one click from the page itself.

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
| `--translator` | Translation engine: `auto`/`hymt2`/`hymt2-7b`/`gemma`/`deepl`/`google`/`claude`/`openai`/`none`; see [Translation Engines](../README.md#translation-engines) | last choice in the UI; otherwise `auto` |
| `--denoise` | RNNoise voice denoising: `auto`/`on`/`off` | `auto` (on) |
| `--port` | Local UI port | `8765` |
| `--cookies` | Path to a yt-dlp cookies.txt file (use it when yt-dlp reports that the stream needs a login) | none |
| `--glossary` | Glossary file; see [Glossaries](translation-engines.md#glossaries) | `glossary.txt` in the project folder, created from `glossary.example.txt` on first run |
| `--banned-terms` | Banned-term list; see [Banned-Term Alerts](../README.md#banned-term-alerts) | `banned_terms.txt` in the project folder, created from `banned_terms.example.txt` on first run |
| `--demo` | Demo mode — drives only the UI | off |
| `--doctor` | Print the hardware check and recommended config, then exit | off |
| `--no-open` | Don't open the app window or browser on startup | off |
| `--ui-lang` | Interface language for this launch only: `zh`/`en`. Not saved; see [Interface Language](interface-language.md) | the App Language setting |
