"""模糊层的召回基准：真实违禁词被 ASR 听错一两个字母时仍须命中。

存在的理由：修掉 `bajar los` 那类误报之后，812 段真实语料里模糊层的命中数
变成了 0。那说明语料里没有需要模糊匹配的样本，**不能说明模糊层还活着**。
以后任何一次为降低误报而收紧阈值，都可能悄悄把这一层彻底阉掉而没人发现。

所以这里用确定性的方式制造 ASR 错字（删一个字母、改一个字母、双写一个字母），
逐条要求仍能命中。种子固定，结果可复现。
"""
import random

import pytest

from app.detector import BannedTermDetector, load_terms
from pathlib import Path

REAL_TERMS = [t for t in load_terms(Path("banned_terms.example.txt"))
              if not t.lower().startswith("re:")]
# 只测长词：三四个字母的词本来就要求精确匹配（否则撞车率过高）
LONG_TERMS = [t for t in REAL_TERMS if len(t.replace(" ", "")) >= 8][:20]


def drop_a_letter(word, rnd):
    i = rnd.randrange(1, len(word) - 1)
    return word[:i] + word[i + 1:]


def swap_a_letter(word, rnd):
    i = rnd.randrange(1, len(word) - 1)
    return word[:i] + rnd.choice("aeiousr") + word[i + 1:]


def double_a_letter(word, rnd):
    i = rnd.randrange(1, len(word) - 1)
    return word[:i] + word[i] + word[i:]


CORRUPTIONS = [drop_a_letter, swap_a_letter, double_a_letter]


def corrupt(term, rnd, how):
    """只弄坏最长的那个词——ASR 听错的通常是实词，不是 de / la 这种。"""
    words = term.split()
    target = max(range(len(words)), key=lambda i: len(words[i]))
    if len(words[target]) < 5:
        return None
    words[target] = how(words[target], rnd)
    return " ".join(words)


@pytest.mark.parametrize("how", CORRUPTIONS, ids=lambda f: f.__name__)
def test_asr_letter_errors_are_still_caught(how):
    """整体召回不得跌破 80%——模糊层的存在意义就是接住这些。"""
    rnd = random.Random(20260824)
    caught = total = 0
    missed = []
    for term in LONG_TERMS:
        bad = corrupt(term, rnd, how)
        if bad is None:
            continue
        total += 1
        text = "y esto te ayuda a {} rapido".format(bad)
        if BannedTermDetector(REAL_TERMS).scan(text):
            caught += 1
        else:
            missed.append((term, bad))
    assert total >= 10, "语料太少，这个基准没有意义"
    rate = caught / total
    assert rate >= 0.8, "{}：召回 {:.0%}（{}/{}），漏掉 {}".format(
        how.__name__, rate, caught, total, missed[:5])


def test_the_exact_term_always_matches():
    """错字测试的对照组：没弄坏时必须 100% 命中，否则上面的数字没有意义。"""
    for term in LONG_TERMS:
        text = "y esto te ayuda a {} rapido".format(term)
        assert BannedTermDetector(REAL_TERMS).scan(text), term
