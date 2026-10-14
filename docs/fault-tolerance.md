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
- Reading Safari's cookies needs Full Disk Access for the app (see the
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

## Keeping up

- When recognition falls behind, audio waits in a queue: a notice appears once
  it is 10 s behind, detection is marked degraded at 30 s, and only audio more
  than 60 s behind is dropped, with the count shown on screen and written to
  the audit log.
- Translation never slows recognition. Its queue holds four lines and drops
  the oldest when full; a line whose translation was skipped keeps its
  original text.
