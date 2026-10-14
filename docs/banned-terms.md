# Banned-term alerts

*Moved from the README (Banned-Term Alerts). The text is unchanged except for relative links and heading levels.*

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
  [re-translated with the strongest model](translation-engines.md#re-translating-with-the-strongest-model),
  and phones on [Phone Viewing](faq.md#phone-viewing) see the alert too.
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

## Validating your banned-term list against a recorded session

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
