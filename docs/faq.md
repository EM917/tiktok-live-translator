# FAQ

*Moved from the README (FAQ). The text is unchanged except for relative links.*

*An entry that sends you to a README section by name, such as "Disk Space", now means [hardware.md](hardware.md#disk-space).*

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
there: it tries the layers listed under [Fault Tolerance](fault-tolerance.md), in
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
[Fault Tolerance](fault-tolerance.md) above); one capture stays valid for about
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
attempts, for up to 30 minutes. It gives up only after six connection attempts
in a row bring no audio at all (two for a pasted stream URL); a connection that
played any audio resets that count, so a patchy connection keeps reconnecting. If the
window says the stream was interrupted several times and couldn't reconnect,
click Start again. Details: [Keeping the stream](fault-tolerance.md#keeping-the-stream).

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
operator's computer to redo that one line with the strongest model (Claude
Sonnet 5.5 when the engine is Claude, otherwise the strongest local model); it's
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
engine is a local model (Hy-MT2 1.8B or TranslateGemma) or Claude; with Google,
DeepL, OpenAI or 7B the panel shows the original text. With Claude, comments
are translated by Claude Haiku 5.5 through a connection of their own and wait
for caption translation first, so a pause on that connection never delays
captions. Comment translation is for display
only and never part of the alert path. The connection is signed through a
third-party service; see [Privacy](../README.md#privacy).

**Exporting captions.** Every session is saved as it happens in the
`transcripts` folder inside the app folder: each caption with its translation
(a Retranslate result replaces the first one) and each viewer comment with its
translation and display name. The file is written line by line, so a crash loses
at most the line being written. It is separate from the audit log, which never
holds comment text, and the app never uploads or deletes it. The page itself
retains only the most recent 300 lines, and the server replays the last 100
after a reload or reconnection.

To export, stop the session, open Settings → Transcripts and click Export
Latest Session. The app writes three files next to the saved one and opens the
folder: a Markdown file (captions and comments in separate sections, each with
the original and its translation), a CSV file with the columns `time`, `kind`,
`user`, `original` and `translation` (UTF-8 with a byte-order mark, so Excel
shows Chinese correctly, with captions and comments in time order) and an SRT
subtitle file (translation line first, then the original, with times counted from
the start of the session; each caption stays up until the next one, at most
8 seconds). In the CSV, a cell that begins with `=`, `+`, `-` or `@` gets a
leading apostrophe, so Excel never runs a viewer's comment as a formula.
Settings is hidden while a session runs; the export covers the latest finished
session that recorded something. A start that got no stream address, or that
you stopped at once, leaves a file with only a header: it stays in the folder,
but Export Latest Session and the session count in Settings skip it. "Open
Folder" opens the `transcripts` folder without exporting.

**Closing the console window.** On Windows that console window is the app
itself, so closing it quits the app. On macOS, closing the app window quits it
(during a session it asks first); if launched via `Start.command`, close the
Terminal window or press Ctrl-C.

**Installation fails on a managed computer, or space is insufficient.** The
initial installation requires about 6 GB and access to PyPI, Hugging Face,
ollama.com and GitHub; see "Disk Space" for the breakdown. Corporate security policies
frequently block these.
