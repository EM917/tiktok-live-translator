# Translation engine benchmarks

*Moved from the README's [Translation Engines](../README.md#translation-engines) section: the engine comparison and the measurement behind strong re-translation. The text is unchanged except for one sentence in the last section, reworded so that raw percentages are not presented as a win.*

### Engine comparison

The relevant metric is not fluency but glossary adherence, since that determines
whether product names, prices and promotional conditions are rendered correctly.
Measured with `tools/bench_glossary.py` over 280 glossary terms taken from
recorded captions, with latency from a live Spanish selling stream on an 18 GB
M-series Mac:

| | Glossary adherence | Multi-word phrases | Translation latency |
|---|---|---|---|
| Hy-MT2 7B | 83% | 84% | 892 ms median, 1.7 s p95 |
| Hy-MT2 1.8B | 66% | 65% | 358 ms median |
| TranslateGemma 4B | 48% | 26% | 832 ms median |

The multi-word column is the significant one: prices and promotional conditions
are phrases rather than single nouns, and that is where a translation misleads
an operator.

Latency was excluded from the selection criteria. Banned-term alerts are raised
from the recognised source text and never wait for translation; the only
requirement is that translation keep pace with 9-second segments, which both
tiers do.

**Does the paid engine actually win?** Not measurably, on this material. 259
captions from one live session, all four engines graded blind by the same panel:

| | Hy-MT2 1.8B | Hy-MT2 7B | TranslateGemma 12B | DeepL |
|---|---|---|---|---|
| Meaning-changing errors | 17.4% | 12.0% | 11.6% | 10.0% |
| Understood on first pass | 83.4% | **96.1%** | 87.6% | 91.9% |

Twelve pairwise tests were run on these captions, so a single p<0.05 means
little. After correcting for that, exactly three results hold, all on
readability: 7B beats 12B, 7B beats 1.8B, and DeepL beats 1.8B. **No difference
in meaning-changing errors between any two engines survives correction** —
including 7B against DeepL, where the paired difference is +1.9% with a 95%
interval of [-3.1%, +7.0%]. That interval is the honest summary: this test
cannot tell them apart, and it also cannot rule out DeepL being several points
better. "No difference measured" is not "equivalent".

Two cautions before generalising. Grading the same 259 captions with a second
panel moved every absolute percentage and agreed on only about 60% of the
meaning-changing errors, so treat the paired comparisons as the result and the
percentages as decoration. And this is one streamer's material.

**Why 7B is not the default.** Its 17-point accuracy advantage made it the
default in the initial v0.10.0 build, a decision based on a 24-caption sample. A
92-caption live run gave a different result: with 7B resident, recognition held
at approximately 3.2 s against 1.4 s with the smaller models, flat from the
first quartile, indicating steady-state contention for unified memory. Since
recognition is on the alert path and translation is not, the trade ran in the
wrong direction. Where memory is available and alert latency is not the primary
metric, `--translator hymt2-7b` is a genuine improvement in terminology
accuracy.

### Re-translating with the strongest model

How much better it actually is, measured: 259 captions from a live session,
each engine's output graded blind by an independent panel. On both axes 7B's
raw rates are better than the default's — meaning-changing errors 12.0% against
17.4%, first-pass readability 96.1% against 83.4% — but only the readability gap
survives correction for the number of comparisons made (p<0.0001); the accuracy
gap does not. So the honest claim is that re-translation is *easier to read*, not
demonstrably *more correct*. The batch tool still reports what changed rather
than replacing anything silently.
