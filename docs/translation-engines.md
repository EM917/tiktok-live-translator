# Translation engines

*Moved from the README (Translation Engines). The text is unchanged except for relative links and heading levels.*

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
| `claude` | Key entered in Settings → Translation Engine (or `ANTHROPIC_API_KEY`). Default model Claude Haiku 5.5 (`claude-haiku-5-5`), with thinking turned off to keep each caption fast; `CLAUDE_TRANSLATE_MODEL` selects another model. Caption text is sent to Anthropic. A caption Claude declines to translate shows the original text and is recorded in the audit log; a declined Retranslate is recorded there too, with the trigger. With this engine, viewer comments are translated by Claude Haiku 5.5 on its own connection (a pause there never pauses captions), and Retranslate and the alert-context translation use Claude Sonnet 5.5 instead of a local model, so no local model is needed for them. A viewer comment can be retranslated only with this engine (see below) |
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

## Choosing an engine

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
[docs/engine-benchmarks.md](engine-benchmarks.md).

## Glossaries

Product names are where a general-purpose model goes wrong most often, and a
short list of them is the most direct fix. The glossary is one set of entries
used in three places: the first entries are given to speech recognition as a
hint; entries that occur in a line are passed with that line to a local
translation model (DeepL uses a native glossary instead, see below, while
Google's free endpoint, Claude's caption and comment translation and
OpenAI-compatible APIs take no hints, so only the fix-up applies to them; the
exception is Retranslate and alert-context translation with Claude Sonnet 5.5,
which get the hints in their prompt like the local model does); and a rule-based
fix-up replaces any glossary entry the translation left untranslated.

It is merged from three files, most specific first:

- `profiles/<streamer-username>.txt` — one streamer's own wording and
  translations, loaded automatically from the live link
- `brands/<brand-id>.txt` — one brand's products, loaded only when the Brand
  menu on the home page (or in Switch Streamer) selects it; for a stream that
  sells a single brand. The menu remembers each streamer's last choice, the
  folder button next to it opens `brands/`, and the menu rescans that folder
  when clicked, with no restart. The file format is in
  [`brands/README.md`](../brands/README.md) (in Chinese)
- `glossary.txt` — the global list (`--glossary` points elsewhere)

Where the same wording appears in more than one file, the streamer's profile
wins over the brand list, which wins over the global list. Edits take effect
after Stop → Start. These files hold your own product data and stay on your
computer: git ignores `glossary.txt`, `profiles/*.txt` and `brands/*.txt`, so
editing them never blocks an update.

## DeepL: the native glossary decides everything

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

## Re-translating with the strongest model

The strongest model available is applied per sentence rather than for a period
of time. With any engine other than Claude (a local model, Google, DeepL or an
OpenAI-compatible API) that is the strongest local model installed (Hy-MT2 7B if
you pulled it, otherwise 1.8B), which needs Ollama. With Claude as the engine it
is Claude Sonnet 5.5, so no local model has to be installed for it. What justifies
it is specific content — prices, promotional conditions, health claims — which
is episodic, and only the operator or the detector knows which sentence that
is. Measured cost: loading the model takes 1.9 s and a complete one-shot call
2.3 s, after which it is unloaded, leaving recognition unaffected. Keeping it
resident instead would raise recognition from 1.4 s to 3.2 s and alert latency
from 6.8 s to 10.6 s. Those figures are for the local models; Claude Sonnet 5.5
runs on Anthropic's servers, so it adds no memory load on this computer.

Three ways to invoke it:

- **Per caption** — hover a caption and click Retranslate. The line is
  re-translated and marked in the margin. If the click cannot start a
  retranslation or it does not produce one, a short note appears right under
  that caption for a few seconds and the existing translation stays. Phones on
  [Phone Viewing](faq.md#phone-viewing) have the same button
- **On a banned-term match** — with [alerts](../README.md#banned-term-alerts) on, the
  matched sentence is re-translated automatically. The fast translation appears
  first so nothing is delayed; the accurate one replaces it about two seconds
  later
- **After the session** — `python3 tools/retranslate_audit.py` re-translates a
  session's audit log, appending `translation_strong` records without altering
  any existing line, and prints the segments whose translation changed rather
  than replacing anything silently

A translated viewer comment has a Retranslate button too, in the comment
column on this computer (phones do not have it). It works only when the engine
is Claude: it asks Claude Sonnet 5.5 on its own connection, with the same
instructions the comment translation uses, and the result has to pass the same
check that it reads as a translation and not as a reply. With any other engine
the button is greyed out and its tooltip says so, because comments never load a
local model, the 7B included. A comment that is already being retranslated is
not sent again, at most 20 retranslations per minute are accepted, and the app
keeps the original text of only the latest 200 comments; a click it cannot serve
gets a short note under that comment and the existing translation stays. Comment
text and viewer names are not written to the audit log.

Measured on the same 259 captions, re-translation with 7B reads more easily
than 1.8B — that difference survived the correction — but it was not shown to
be more correct. Details are in
[docs/engine-benchmarks.md](engine-benchmarks.md#re-translating-with-the-strongest-model).

## Finding translation errors without reading everything

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
