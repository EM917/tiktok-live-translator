"""updater：版本号解析与比较——排序错了更新提示就会失灵。"""
import pytest

from app.updater import _parse


@pytest.mark.parametrize("raw,expect", [
    ("v0.8.0", (0, 8, 0)),
    ("0.8.0", (0, 8, 0)),
    ("V1.2.3", (1, 2, 3)),
    ("0.8", (0, 8, 0)),            # 缺段补零
    ("1.2.3-beta", (1, 2, 3)),     # 段内非数字字符剔除
    ("1.2.3.4", (1, 2, 3)),        # 超过三段截断
    ("", (0, 0, 0)),
    ("garbage", (0, 0, 0)),
])
def test_parse(raw, expect):
    assert _parse(raw) == expect


def test_numeric_not_lexicographic_ordering():
    # 字典序会把 0.10.0 排在 0.9.9 前面——必须是数值比较
    assert _parse("v0.10.0") > _parse("v0.9.9")
    assert _parse("v1.0.0") > _parse("v0.99.99")


def test_equal_versions_do_not_trigger_update():
    # check_and_notify 的判定是 tag <= local 即不提示
    assert not (_parse("v0.8.0") > _parse("0.8.0"))
