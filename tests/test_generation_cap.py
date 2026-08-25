"""生成阶段的长度上限——让捏造在物理上吐不出来。

事后检查是补不完的：第一版看「丢了问句/数字」，漏掉了短句后接一整段虚构内容；
补上长度比之后，谁知道下一种是什么样。所以在生成时就按源文长度限制 token 数，
把「事后判掉」换成「根本产不出来」。

阈值取自实测而非拍脑袋：70 句真实翻译的「输出 token / 源文字符」比值最大
0.364（中位 0.22），上限取 0.8 留 2.2 倍余量，那 70 句零截断；而捏造那条的
比值约 3.6，必然撞上限。于是「撞上限」本身成了近乎确定的信号。
"""
from app.translator import (MIN_PREDICT, TOKENS_PER_SOURCE_CHAR, predict_cap)


def test_the_cap_scales_with_the_source():
    assert predict_cap("x" * 100) == 80
    assert predict_cap("x" * 250) == 200


def test_short_sources_get_a_floor():
    """短句按比例算会小到把正常译文卡掉，所以有地板。"""
    assert predict_cap("Nada.") == MIN_PREDICT
    assert predict_cap("Sigan dando el micrófono.") == MIN_PREDICT
    assert predict_cap("") == MIN_PREDICT


def test_the_cap_is_never_unbounded():
    assert predict_cap("x" * 100000) == 200


def test_the_margin_over_real_translations_is_real():
    """实测最大比值 0.364，上限 0.8 —— 余量要留够，否则会截断正常译文。"""
    assert TOKENS_PER_SOURCE_CHAR / 0.364 > 2.0


def test_a_fabricated_length_would_exceed_the_cap():
    """实盘那条：25 字符源文吐出约 90 token（比值 3.6）——必然撞上限。"""
    src = "Sigan dando el micrófono."
    would_need = len(src) * 3.6
    assert would_need > predict_cap(src)
