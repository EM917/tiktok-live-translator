"""tools/check_architecture_sync.py：架构图窄/宽源文件的漂移检查。

2026-09-18 那次改 resolver 节点的文案，四份 JSON（中/英 × 窄/宽）要逐一手改，
漏改一份既不会让 `archify deliver` 失败，也不会在 diff 里显眼——两份产物就此
悄悄不一致。这里钉住三件事：真实仓库里现在这四份文件确实同步；工具真的能
抓到内容漂移（不是摆设）；工具不会把窄/宽版本刻意不同的几何差异（坐标、画布
尺寸、连线走线、宽版独有的 boundaries）误判成漂移。"""
import json

import pytest

from tools.check_architecture_sync import ARCH_DIR, check_language, first_difference


def _write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def _minimal_pair(**overrides):
    """一对最小可用的窄/宽 JSON：默认完全同步，overrides 用来注入分叉。"""
    narrow = {
        "meta": {"title": "T", "locale": "en"},
        "components": [{"id": "a", "label": "A", "pos": [0, 0], "size": [10, 10]}],
        "connections": [{"id": "a-b", "from": "a", "to": "b", "label": "L",
                         "via": [[1, 2]], "labelDy": 5}],
        "cards": [{"dot": "cyan", "title": "Card", "items": ["one", "two"]}],
    }
    wide = json.loads(json.dumps(narrow))          # deep copy
    wide["meta"] = dict(wide["meta"])
    # 几何差异：宽版画布不同，位置、连线走线、标签偏移都该跟着变——这些都不算
    # 内容漂移。
    wide["components"][0]["pos"] = [999, 999]
    wide["components"][0]["size"] = [50, 50]
    wide["connections"][0]["via"] = [[9, 9], [8, 8]]
    wide["connections"][0]["labelDy"] = 40
    wide["boundaries"] = [{"kind": "region", "label": "wide-only", "wraps": ["a"]}]
    for key, value in overrides.items():
        wide[key] = value
    return narrow, wide


def test_real_repository_sources_stay_in_sync():
    """回归测试：仓库里现在的四份 architecture.json 必须保持同步。

    这条测试就是防线本身——它在 CI 里跑，任何一次只改一份源文件的疏漏都会
    在这里挂掉，而不是留到网页版和 README 图不一致时才被人肉眼发现。"""
    for lang in ("en", "zh"):
        in_sync, message = check_language(lang, ARCH_DIR)
        assert in_sync, message


def test_geometry_only_differences_are_not_drift(tmp_path):
    """位置、尺寸、连线走线、标签偏移、宽版独有的 boundaries：允许两边不同。"""
    narrow, wide = _minimal_pair()
    _write(tmp_path / "audio-chain.en.architecture.json", narrow)
    _write(tmp_path / "audio-chain.en.wide.architecture.json", wide)

    in_sync, message = check_language("en", tmp_path)
    assert in_sync, message


def test_a_changed_label_is_flagged_as_drift(tmp_path):
    """内容漂移的典型样本：宽版的组件文案改了，窄版没有跟着改。"""
    narrow, wide = _minimal_pair()
    wide["components"][0]["label"] = "Something else"
    _write(tmp_path / "audio-chain.en.architecture.json", narrow)
    _write(tmp_path / "audio-chain.en.wide.architecture.json", wide)

    in_sync, message = check_language("en", tmp_path)
    assert not in_sync
    assert "components[0].label" in message
    assert "Something else" in message


def test_a_card_item_only_present_in_one_side_is_flagged(tmp_path):
    """卡片文案是内容，不是几何——两边条数不一致必须报出来。"""
    narrow, wide = _minimal_pair()
    wide["cards"][0]["items"].append("three")
    _write(tmp_path / "audio-chain.en.architecture.json", narrow)
    _write(tmp_path / "audio-chain.en.wide.architecture.json", wide)

    in_sync, message = check_language("en", tmp_path)
    assert not in_sync
    assert "cards[0].items" in message


def test_a_missing_source_file_is_reported_not_crashed(tmp_path):
    """源文件缺一份时给出清楚的说明，而不是让 FileNotFoundError 冒出来。"""
    narrow, _wide = _minimal_pair()
    _write(tmp_path / "audio-chain.en.architecture.json", narrow)
    # 故意不写宽版文件。

    in_sync, message = check_language("en", tmp_path)
    assert not in_sync
    assert "missing source file" in message


@pytest.mark.parametrize("a, b, expected", [
    ({"x": 1}, {"x": 1}, None),
    ({"x": 1}, {"x": 2}, ("$.x", 1, 2)),
    ({"x": {"y": 1}}, {"x": {"y": 2}}, ("$.x.y", 1, 2)),
    ([1, 2], [1, 2], None),
    ([1, 2], [1, 3], ("$[1]", 2, 3)),
])
def test_first_difference_reports_the_first_divergent_path(a, b, expected):
    """first_difference 是漂移报告的核心：路径和两边的值都要对得上。"""
    assert first_difference(a, b) == expected
