# Fault tolerance

*Moved from the README (Fault Tolerance). The text is unchanged except for relative links and heading levels.*

A live session can fail quietly in three places:
finding the stream URL, keeping the stream, and keeping up with it. Each is
handled explicitly.

## Finding the stream URL

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
- Reading Safari's cookies needs Full Disk Access for the Python interpreter
  the app runs with, not for the app itself (see the
  [Browser Login FAQ](faq.md#browser-login)). Without a readable sign-in, monitoring
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
[code 4003110](faq.md#code-4003110) in the FAQ.

## Keeping the stream

- A dropped stream reconnects, telling a network interruption from the end of
  the stream. After a stretch with audio it first reconnects to the same
  stream URL (only the video server is contacted); when that brings no audio,
  or the URL clearly stops working, it looks up the stream URL again. Before
  looking up, the app checks that it can reach TikTok; while it cannot, it
  waits without looking anything up, for up to 30 minutes.
- Waits between attempts grow while no audio arrives (2 s up to 30 s; 60 s
  after 30 minutes without audio) and reset with any audio.
- Lookups are capped: at most 26 in any hour, counting checks of the stream
  status while waiting for it to resume, and one every 15 minutes after 30
  minutes without audio. At the cap the app keeps reconnecting to the same
  stream URL and says when the next lookup is.
- If TikTok hasn't reported the stream as ended, the app waits about 10
  minutes for it to resume.
- A live link is never given up just because no audio arrives. Monitoring
  stops when TikTok reports the stream as ended, when it hasn't reported it as
  live for about 10 minutes, when TikTok can't be reached for 30 minutes, or
  when looking up the stream URL fails six times in a row. A pasted stream URL
  stops after two connection attempts in a row with no audio; one that stops
  serving data after playing for 30 s or more ends the session instead of
  reconnecting.
- As soon as the stream drops, the status line says "The stream was
  interrupted. Reconnecting…" instead of showing Live until the queued audio
  has been transcribed. Audio that had already arrived is still transcribed
  and its captions keep appearing; translations still in progress get up to
  5 s more in the background and no longer hold up the reconnect. Measured
  with a test audio source: with 8 segments queued at 1 s of recognition each,
  the reconnect status appears within 0.1 s of the drop, and with a
  translation stuck in progress, reconnecting starts 8.1 s after the drop
  instead of 13.1 s (the rest is the wait for the queued audio).
- When the video server closes an FLV stream, ffmpeg exits and the app
  reconnects by itself. Measured against a local test server: about 2 s
  without audio per drop, where ffmpeg's own reconnect left 15–27 s without
  audio. HLS streams keep ffmpeg's own reconnect.
- A stream whose header announces video but which carries only audio starts
  producing audio within about 2 s (measured 1.2 s); before, ffmpeg could wait
  for up to 90 s of stream before producing any.
- Every step of stopping a stalled stream has a time limit, and a stall in
  any step is recorded. On macOS and Linux a second stop signal follows 0.5 s
  after the first: measured stops of a stalled ffmpeg take under 1 s instead
  of 5 s, and the audio ffmpeg still had is kept. "No stream audio for N min"
  appears after 2 minutes without stream audio, and a notice plus a badge
  under the large caption appear when less than 70% of the last 5 minutes
  arrived. Audio that arrives late, when a stalled connection is closed, is
  still checked for banned terms.
- `TLT_RECONNECT_V2=0` at startup brings back the previous reconnect rules
  (look up on every reconnect, stop after six attempts in a row with no
  audio) for that run. FLV streams then go back to ffmpeg's own reconnect as
  well: the previous rules look up the stream URL on every reconnect with no
  limit, so if ffmpeg exited on every drop, every drop would mean one more
  lookup.
- `TLT_FFMPEG_LEGACY=1` at startup brings back the previous ffmpeg options for
  that run: ffmpeg's own reconnect for FLV, ffmpeg's default limits for stream
  analysis (5 s overall, up to 90 s per stream for FLV), and an errors-only
  ffmpeg log. Otherwise, when the installed ffmpeg supports tagged log levels,
  its warnings are read as well, and the number of times it reconnected on its
  own is recorded with each stream interruption in the session audit.
- How the two switches combine: `TLT_RECONNECT_V2=0` changes only how FLV
  reconnects among the ffmpeg options and keeps the other two new options.
  `TLT_FFMPEG_LEGACY=1` brings back all three, whichever reconnect rules are
  in use, and does not change the reconnect rules. While either one is set,
  `TLT_LOW_BITRATE_STREAM=1` ([below](#switches-that-stay-off-by-default))
  has no effect.
- The comment connection can go half-open: the Comments panel shows no
  warning, but no comments arrive. After 15 minutes of silence while
  connected, the app reconnects it and records that in the session audit.
- yt-dlp is updated in the background: the app checks at most once a day,
  and again when looking up the stream URL has failed twice in a row, once
  that session's stream has stopped, unless a check finished in the last 6
  hours. A failure where yt-dlp reports that the stream is not live or needs
  a sign-in starts the count over, and a connection error is not counted. It
  installs only a version released at least 7 days earlier, and never while
  monitoring or during Update Now; the next Start uses it, with no restart.
  TikTokLive is looked at only when TikTok refuses the comment connection,
  under the same rules (a 7.x version at least 7 days old, not while
  monitoring or during Update Now). Refusals come while monitoring, so in
  practice this check installs nothing, and the Comments panel says the
  component isn't updated during monitoring. A newer TikTokLive comes with
  the app's own component list instead: Update Now installs one when the new
  release requires it.
- While monitoring, the computer is kept from going to sleep on its own, and a
  gap where the app did not run at all is recorded and shown on screen. The
  notice has a close button (×) and also goes away by itself after 10 minutes
  without another gap; a new gap shows it again, with the count carried on.
  The session audit keeps every gap either way.

## Keeping up

- When recognition falls behind, audio waits in a queue: a notice appears once
  it is 10 s behind, detection is marked degraded at 30 s, and only audio more
  than 60 s behind is dropped, with the count shown on screen and written to
  the audit log.
- Audio is cut into segments of up to 9 s, and a segment in which no frame
  reaches the speech recognition threshold is not sent to recognition. Two
  notices say when that covers much of the stream; the threshold itself does
  not change. After 120 s of stream audio in a row with no segment sent to
  recognition, a notice says that the audio arrived but none of it was
  transcribed; it goes away with the next segment. Every 5 minutes the app
  also compares the seconds sent to recognition with the seconds that
  arrived: below 60%, a notice gives both numbers, it is updated every 5
  minutes while the share stays below 75%, and it goes away at 75% or more.
  A 5-minute window with less than 60 s of audio is not judged, and while
  the first notice is up the second one is not shown. When the share drops
  below 60% and when it is back at 75% or more, a `gated_audio` record with
  codes and numbers only (its `state` is `low` or `ok`) goes into the session
  audit. Both notices go away when
  monitoring stops.
- Stopping, switching streamers or pausing for an update leaves audio that had
  arrived but wasn't transcribed yet: the queued segments, the one being
  transcribed and the part not yet cut into a segment. Each interruption's
  `stream_break` records how many segments and seconds that was
  (`discarded_segments`, `discarded_sec`), and `session_end` records the
  session total; without any, neither key appears. When the app closes a
  stalled connection by itself, or the audio connection ends with an error,
  the part not yet cut into a segment is still transcribed.
- Translation never slows recognition. Its queue holds four lines and drops
  the oldest when full; a line whose translation was skipped keeps its
  original text.

## Switches that stay off by default

Three changes are in the code but stay off unless the app is started with an
environment variable. Each can change what is recognized, or how soon, so each
needs an evaluation before it is turned on. Read that evaluation first; none
has been run yet, so leave them off for now. The session audit does not record
yet whether they are on. Only `on`, `1`, `true` or `yes` turns one on; anything
else leaves it off.

- `TLT_ASR_TOKEN_CAP=on` stops decoding a segment after 112 tokens. A segment
  that repeats itself otherwise decodes up to the model's limit (224 tokens
  with mlx-whisper). In 4,800 segments from earlier sessions, all 112 that
  reached 112 tokens or more had been dropped as repeats. The evaluation has to show
  that real speech is not cut short, since banned-term matching reads the same
  text, and that a shortened repeat does not start showing up as a caption.
- `TLT_ASR_MASK_LANGS=on` changes what happens to a segment detected in a
  language outside the Spoken language list. Today it is decoded a second time
  in the first language of the list. With the switch on, the most likely
  language from the list is picked during language detection and the segment
  is decoded once. It applies only when the list has more than one language.
  The evaluation has to measure the extra detection time and compare the text.
- `TLT_LOW_BITRATE_STREAM=1` changes which stream is taken when TikTok offers
  no audio-only track: the lowest quality instead of the first one listed,
  which so far has always been the original or the HD quality. The WebKit
  layer still picks as before, and so does a live page whose stream data
  cannot be read, when addresses are taken from the page text. It has no
  effect while `TLT_FFMPEG_LEGACY=1` or `TLT_RECONNECT_V2=0` is set. The audio of the lower
  quality has not been compared with the original yet (format, sample rate,
  channels and delay, measured in the same minute on a stream that is live).
