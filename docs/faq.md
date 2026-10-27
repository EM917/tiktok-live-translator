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
the fix is delivered by the update that is being blocked. Those builds say to
run `git pull`; don't, because it also brings in changes on the main branch that
are not released yet. Instead, run these two commands once in the project
directory, with the tag of the latest release from the
[releases page](https://github.com/EM917/tiktok-live-translator/releases/latest)
in place of `vX.Y.Z`:

```bash
git fetch --tags --force
git merge --ff-only vX.Y.Z
```

Current versions use the same two steps in the command they show: they install
that release and nothing after it.

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
launch with Start.command. Then quit the app completely and reopen it. Both
launchers start the app with the Python that built its environment (`.venv`)
while that Python is still there at the same version, so installing or
upgrading a different Python (for example Homebrew pulling in a newer
`python@3.x` as a dependency) does not change the path. It changes when that
Python itself is upgraded, moved or removed, or when the environment is
rebuilt; the row then shows the new one. With an unchanged path, the entry can
also be in the list with its switch on while the row still says macOS didn't
allow access. In that case select the entry, click −, and add it again with the
steps above. The permission belongs to the Python
interpreter, not just this app: any script run with that Python can read
protected data on this computer, such as Mail, Messages and Safari data. When
you no longer need the TikTok sign-in from your browser, switch the entry off in
the same list. This only matters for rooms whose
stream address TikTok serves to signed-in viewers. The check looks only at whether the cookie
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
stream drops, the app reconnects by itself. After a stretch that had audio it
first reconnects to the same stream URL, which only contacts the video server
and not the live page or TikTok's API; if that brings no audio (or the URL
clearly stops working), it looks up the stream URL again. While no audio
arrives, the waits grow (2 s, then 4, 8, 16 and at most 30 s; 60 s after 30
minutes without audio). Any audio resets them. To keep requests to TikTok low,
the app looks up the stream URL at most 26 times in any hour, and checks of
the stream status while waiting for it to resume count toward the same limit;
past it, the app keeps reconnecting to the same stream URL and the status line
says when the next lookup is. After 30 minutes without audio it looks up at
most once every 15 minutes. While the computer cannot reach TikTok it waits
without looking anything up, for up to 30 minutes.

A live link is never given up just because no audio arrives. Monitoring stops
only when TikTok reports the stream as ended (status 4, checked again if the
computer was asleep), when TikTok's API hasn't reported it as live for about
10 minutes, when TikTok can't be reached for 30 minutes, or when looking up the
stream URL fails six times in a row. A pasted stream URL stops after two
connection attempts in a row with no audio. The status line counts the
interruptions in this session ("interruption 7 this session").

Two notices say how much was heard. "No stream audio for N min" appears after
2 minutes without stream audio and goes away with the first audio. "Only N% of
stream audio arrived in the last 5 min" appears when less than 70% arrived,
and goes away above 85%; a small "Last 5 min: N% received" badge under the
large caption shows the same number. Audio that only arrives late, when the
app closes a stalled connection, is still transcribed and checked for banned
terms; it doesn't count as received in real time, and the notices say how much
of it was checked. Details: [Keeping the stream](fault-tolerance.md#keeping-the-stream).

To go back to the previous reconnect rules for one run (look up the stream URL
on every reconnect, stop after six connection attempts in a row with no audio,
"attempt N of M" in the status line), start the app with `TLT_RECONNECT_V2=0`.
The notices above, the limits on how long stopping a stalled stream can take,
and the audit records stay the same.

<a id="captions-in"></a>**Switching Captions in to English didn't change the interface.** The
**Captions in** menu in the top bar sets only the language captions are
translated into: Simplified Chinese, Traditional Chinese, English or Spanish.
The interface language is a separate setting, **Settings → App Language** on
the home page ([Interface language](interface-language.md)). If an earlier
version saved another caption language, or the app starts with `--target` and
another language code, captions stay in that language and the menu shows it as
an extra item until you choose one of the four. With Spanish selected,
segments recognized as Spanish aren't sent for translation and appear as
said; English segments are translated into Spanish.

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

<a id="recognition-audio"></a>**What are the recognition audio clips in Storage?** The app
saves a few seconds of stream audio around some segments in `logs/asr-evidence/`, so that
someone can listen later and check what was recognized: segments the recognition risk rules
flag, banned-term matches (also when alerts are off), captions you retranslate in this window
(not from a phone), and one segment in 50 that nothing flagged, for comparison. Each clip is a
WAV of the segment plus 2 s before and 1 s after, with a line in `index.jsonl` that holds the
recognition output. It is the stream's mixed audio, so anyone else on the stream and the
background music are in it too. The clips stay on this computer: nothing uploads them, sends
them to an API or puts them in an export, and `logs/` is not in git. Once the folder passes
1 GB (1024 MB), 6000 files or 14 days, the oldest sessions are removed first; the current
session is never removed. This is checked when a session starts, so clips older than 14 days
stay until the next session. Settings → Storage shows the folder and can delete it. Saving runs off the
recognition path, and a failed write only prints a warning in the terminal. Turn it off with
`"asr_evidence": false` in `settings.json` or the environment variable `TLT_ASR_EVIDENCE=0`.
Turning it off stops new clips; clips already saved stay until the limits remove them at a
session start, or until you delete them in Storage.
The limits are `asr_evidence_max_mb` (1024 by default), `asr_evidence_max_files` (6000),
`asr_evidence_days` (14), `asr_evidence_ring_sec` (recent audio kept in memory, 600 by default)
and `asr_evidence_control_every` (50; `0` saves no comparison segments). They are read when a
session starts. To keep less, write a smaller number in `settings.json`, for example
`"asr_evidence_max_mb": 300`.

<a id="recognition-risk"></a>**Recognition risk records, and switching them off.** Each
segment in the audit log has an `asr` object that holds only numbers and short codes: segment
length, how it was cut, the share of voiced frames, words per voiced second, the decoder's
average log-probability, no-speech probability and compression ratio, and the flags of risk
rules v1 (`low_conf`, `nan_lp`, `few_words`, `loop`; `outro_only` is recorded and its audio
clip is saved, but it never gets the badge; `relabel` is only recorded). The thresholds are frozen as version 1 and written to `asr_config`, and
`session_end.asr_risk` counts the session. A flag means the segment looks like a gross
recognition failure; no flag doesn't mean recognition was right. Banned-term matching still
runs first on the raw recognized text and uses none of this. With `"asr_uncertain_chip": true`
in `settings.json` (off by default; read when a session starts), captions whose segment hit one
of the first four rules get a "Recognition unclear · check the original" badge on the caption
card and under the large caption; hover it to see what was observed. The text itself is never
changed, the translation engine is not told anything, and the badge never appears on alerts, the
phone page or the Floating Window. To switch off the risk flags, the audio clips and the badge
together, start the app with `TLT_ASR_SAFEGUARDS=off`; the numbers stay in the audit log. To roll back the code, revert the commits that added `app/asr_risk.py`,
`app/asr_evidence.py` and the badge; no other code reads the new audit keys, and deleting
`logs/asr-evidence/` has no effect on the app.

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
operator's computer. All phones together also share a limit of 20 a minute (up
to 5 in a burst) that reconnecting doesn't reset; taps beyond it do nothing.
While speech recognition is falling behind, retranslations asked for from a
phone aren't run, and the phone's button shows **Couldn't Retranslate · Try
Again**. Each alert has a ✕ to hide it, plus a **Clear Seen**
button to hide everything currently shown — both act only on that one phone:
the operator's alert panel and the audit log are unaffected, and nothing is
sent to the server.

<a id="floating-window"></a>**Can I keep the captions on top of other apps?** Yes, in the app's own
window. While a stream is running, click **Floating Window** in the top bar
(an icon until the window is at least 1,400 px wide). A small dark window opens
and stays above other apps. It shows the
latest caption large, with the original under it and, above it, the same status
labels as the main window ("About N sec ago" once the line is more than 15 seconds
old, "Connecting…" while it reconnects), and the most recent comments with their
translations (up to five; make the window taller to see all five). Comments
that Comment Review marks red or amber keep their marks there. It only displays:
no alerts, and it sends nothing to the app. Drag it or resize it; the
position and size are saved in `settings.json` (`float_window`) and used the
next time, unless that spot is no longer on a screen. The first time, a Mac puts it
near the bottom-right corner of the screen your main window is on; other systems place it themselves. It closes when you click
its close button, when you click **Stop**, or when you close the main window.
When the stream ends or monitoring stops with an error, it stays open and says
**Stream ended** or **Monitoring stopped** (see the main window for details),
with one button, **Show Main Window**, which brings the main window to the
front; on a Mac, if the app is not the active app, its Dock icon also bounces once. The **Floating Window** button exists only when the app runs in its own window, so it is not there
with `--browser`. (A browser tab opened on the address of a running app window does show
it, and clicking it opens the window in the app.)

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
In the audit log, `session_end.comment_fields` counts, in numbers only, how many comments carried each
account field, and for now also candidate account-level and badge fields (`level_has`, `level_hist`,
`level_kinds`); the app shows none of these.

**Exporting captions.** Every session is saved as it happens in the
`transcripts` folder inside the app folder: each caption with its translation
(a Retranslate result replaces the first one) and each viewer comment with its
translation and display name. The file is written line by line, so a crash loses
at most the line being written. It is separate from the audit log, which never
holds comment text, and the app never uploads or deletes it.

The page keeps the most recent 300 captions and 100 comments, and the app
replays the same ones after a reload or reconnection. **Clear** in the top bar
(captions) and **Clear Comments** in the comment column remove them from the
app as well, so a cleared caption or comment does not come back after a reload;
the transcript and the audit log are not touched. Each takes two clicks: the
first turns the button into **Clear?**, and a second click within 6 seconds
clears. A phone that
connects later gets the last 100 captions and 100 comments. While you are
scrolled up, the **Jump to Latest** button shows how many new captions have
arrived since (in the comment column, how many new comments).

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
