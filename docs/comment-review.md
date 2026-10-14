# Comment Review

*Moved from the README (Comment Review). The text is unchanged except for relative links and heading levels.*

When a viewer insults the streamer, calls the products fake or a scam, calls for reports, floods the chat, or
pulls people somewhere else, the app marks that comment in the Comments panel and adds a line at the top of the
window, so the operator sees it at a glance. It reads the same comment feed that
[viewer comment translation](faq.md#viewer-comments) uses, and looks at the text only.
**The app is read-only toward TikTok: it cannot delete comments, mute or block anyone.** What to do about a flagged
comment is up to the operator.

**Comment Review is on by default, using rules only.** The rules run on your computer, make no network calls and
use no model. The switch is the **Comment Review** row in the Settings group on the home page: collapsed by default,
never opened by itself, with a summary of whether it is on, how it reviews, and how many viewers are marked. With no
comments there is nothing to review (`--no-comments`, or the comment component not installed).
`python3 main.py --demo` runs without a stream and plays fictional comments, to see what the marks look like.

## What it flags

- **Insults and threats** — insults aimed at the streamer or other viewers, threats of harm, sexual remarks.
- **Calling products fake or a scam, calling for reports or for people not to buy** — claims that something is
  fake or a scam, calls not to buy or to report the shop, threats to report.
- **Flooding** — one viewer posting in quick bursts (by default 6 comments in 10 seconds or 12 in 60 seconds), or the
  same viewer posting the same sentence 5 times in 60 seconds. The thresholds can be changed in
  `comment_guard/local/thresholds.txt` on your machine. **Different accounts posting the same sentence is not flagged:**
  that is almost always a giveaway or raffle chant the streamer asked for. It is only counted in the audit log
  (`chorus` in `session_end.comment_guard`), never shown. A comment in such a chant is still flagged by every other
  rule as usual, for example a confirmed red entry, a link or phone number, or one viewer flooding on their own.
- **Redirects** — links, phone numbers or WhatsApp, email addresses, an `@handle` offered with a "follow me" line,
  pulling people to another stream or shop, and fake official prizes or scam offers.

## Red and amber

- **Red** — a rule matched clearly. A red bar sits on the left of the comment, with the category beside it:
  Possible insult, Possible threat of harm, Possible call to report or not buy, and so on. Categories always say
  "Possible", because the app sees text only and does not conclude anything until a person has confirmed it. Links,
  phone numbers and one viewer's flooding are structural signals and also come out red.
- **Amber** — the rules themselves are unsure. The reason beside it states what was observed and does not guess a
  cause: "Short, with no question mark", "Describes the viewer's own experience", "Reads like a quote",
  "Second time from this viewer in 10 min".
- **Entries you have not confirmed show amber, even on a match.** In the shipped rules only most entries for threats
  and for calls to report or not buy are confirmed. For the rest (insults, sexual remarks, claims of fakes, threats
  to report, pulling people elsewhere, scam offers) a match comes out amber until you confirm the entry one by one: it
  is not marked red and not added to Marked Viewers automatically. What counts as a violation is a business call, so
  the app does not switch an entry on for you. The procedure is in
  [`comment_guard/README.md`](../comment_guard/README.md).
- Under Settings you can tick **Also show weak signals (light)** to show the faint hints that are otherwise only
  counted. Off by default.

A line at the top of the window appears during a session when there is something unseen, for example
"2 items to review · 1 unclear · 3 from marked viewers · Possible threat of harm 1". It counts **events**: one flood
is 1 item however many comments it holds. Click it to open a drawer
under it listing recent events (time, nickname, category, original and translation, reason). From there you can
**Show in Comments** to jump to the comment in the panel, **Dismiss** the event, or **Mark** the viewer; **All Seen** clears the unread count.
With the Comments panel collapsed, the floating button shows the number of unseen red items. There is no system
notification and no sound.

## Marked Viewers

- Hover a comment and click the ☆ in its top-right corner to mark the viewer; click the ★ to unmark.
- A viewer who triggers a red comment is **added automatically**, but only when all three hold: the match is a
  confirmed entry, the account id is stable (the numeric id or sec_uid TikTok provides — a viewer known only by
  nickname or handle does not count), and nothing about the match is flagged uncertain. Links, phone numbers,
  flooding and red items the model promoted never add anyone automatically.
- Every later comment from a marked viewer is highlighted (a ★ and an indigo edge), and the top line counts "from
  marked viewers" separately.
- The list is kept per streamer and is still there next session. An automatically added viewer who does not
  comment for 30 days is removed at the start of the next session; one you added by hand is not. Each streamer holds
  at most 500 viewers; when full, the automatically added viewer who has been silent longest goes first. Under
  Settings → Comment Review you can view the list, **Unmark** a viewer, or **Clear Auto-Added**.

## Optional: Claude Haiku 5.5 review

Under **Settings → Comment Review → Model review**, choose **Claude Haiku 5.5 (cloud)**. The default is **None
(rules only)**. It runs on Anthropic's servers, so it uses none of this computer's GPU memory, and no local model is
loaded during a session.

- **It adds an opinion on the grey zone only.** Clear red items, flooding, links and phone numbers, and the
  streamer and moderators are not sent. A comment that only has a tone hint (all caps, emoji) is not sent either once
  several accounts are posting the same sentence, so a giveaway chant does not use up the review limits.
- **Shadow mode: it only adds, never removes.** It can promote an amber comment to red or a weak hint to amber, with
  "Model: agrees with rules" or "Flagged by the model" beside it. When it finds no attack the amber stays, labelled
  "question", "complaint", "friendly" or "no attack found". **It cannot take a red away or lower a level**, and a
  model saying "fine" never hides what the rules flagged. A red promoted by the model is not added to Marked Viewers
  automatically, and an amber caused by an unconfirmed entry only gets the model's opinion and is never promoted. The
  interface shows the category name and a snippet cut from the original comment; sentences the model wrote itself
  never appear.
- **Only that one comment leaves your computer**, plus the same viewer's previous comment from within 2 minutes.
  Phone numbers, strings of 7 or more digits, links, email addresses and `@handles` are masked first, and the text is
  cut to 280 characters. Nicknames, account ids, the streamer's name, timestamps and captions are never sent. With
  None selected, nothing is sent at all.
- **Cost.** Billed by Anthropic API usage, separate from a Claude subscription. One call (a batch of up to 8 comments)
  cost about $0.0004 in a small smoke test of 4 calls. The app makes at most 10 calls a minute and 600 per session, so
  a session costs about $0.25 at the very most. At the current pace of the stream the estimate is 1 to 20 calls an
  hour, under a cent; a whole session has not been measured yet, so go by the token counts in the audit log.
- **Key.** It shares the translation engine's Claude API key: the `ANTHROPIC_API_KEY` environment variable first,
  otherwise the key entered under Settings → Translation Engine. If you choose Claude and have no key yet, the row
  shows a key field; saving a key there does not switch the translation engine. The key is stored in `settings.json`
  (already gitignored) and never goes into the audit log.
- **It stops by itself.** Review pauses while recognition is 10 seconds or more behind and resumes on its own. With
  no key, a rejected key, no network or a rate limit, only the rules run, and the status beside the Comments panel title
  says which one. The rules always keep running.

## Phones, and where the files live

- **Phone Viewing has none of this.** Red, amber, the top line, Marked Viewers and the model's opinions go only to
  the operator's page. Phones still see the comments, with no marks, and their page does not load this code.
- **Turning it off.** Switch it off in Settings (changed while idle, effective from the next session). The
  environment variable `TLT_COMMENT_GUARD=off` is an emergency master switch: it turns the whole feature off no
  matter what Settings says, and comments keep showing.

| Where | What |
|---|---|
| `comment_guard/defaults/` | The shipped rules and thresholds, updated with the app |
| `comment_guard/local/` | Your own additions and exemptions, competitor shop names and accounts, your own numbers and links, the streamer's and moderators' accounts. Created only when you click **Open Rules Folder**; gitignored |
| `comment_guard/marked/<streamer>.json` | Marked Viewers, one file per streamer, without comment text; gitignored |
| `logs/` | The audit log records counts and codes only; comment text, nicknames and accounts never go in |
| `settings.json` | The switch, the review mode and the Claude key |

How rules are written, the thresholds and the confirmation procedure are in
[`comment_guard/README.md`](../comment_guard/README.md).
