"""字幕引擎是 Claude 时，界面上几处说法要跟着实际行为走（2026-10-13）：
  * 手机页「重译」下面那行提示不再说「中控电脑上的大模型」——Claude 引擎下重译是 Sonnet 5.5，
    不在中控电脑上；
  * 选 Claude 引擎时的说明要把「弹幕和重译也发给 Anthropic」说清楚；
  * 重译按钮的悬停提示不写「最准」：Sonnet 5.5 这一档没有测过准确率（CLAUDE.md 第四条），
    docs/engine-benchmarks.zh-CN.md 也只允许说「更好读」。

G2 只查「每句中文有没有挂着英文」，不查文案对不对，改回旧文案不会红；node 的英文页测试只
钉英文那一臂。这里把中英两臂都从源码里取出来钉死。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def source(name):
    return (ROOT / "web" / name).read_text(encoding="utf-8")


def pair_after(js, anchor):
    """anchor 之后第一个 L("中文", "English") 的两臂。"""
    m = re.search(re.escape(anchor) + r'\s*L\(\s*"([^"]*)"\s*,\s*"([^"]*)"\s*\)', js)
    assert m, "app.js 里找不到 " + anchor
    return m.group(1), m.group(2)


def test_the_phone_retranslate_hint_no_longer_points_at_the_operators_computer():
    html = source("viewer.html")
    m = re.search(r'<div class="retranslate-hint"\s+data-en="([^"]*)">([^<]*)</div>', html)
    assert m, "viewer.html 里找不到重译提示"
    assert (m.group(2), m.group(1)) == (
        "重译会用更强的模型重新翻这一句，稍等几秒",
        "Retranslation uses a stronger model and takes a few seconds.")
    for old in ("中控电脑上的大模型", "operator’s computer", "larger model"):
        assert old not in m.group(0)         # 只查这一行：别处（隐藏警报那句）合理地提到中控电脑


def test_the_claude_engine_note_says_comments_and_retranslation_go_to_anthropic():
    zh, en = pair_after(source("app.js"), "claude:")
    assert zh == "字幕文本、观众弹幕和重译的请求会发送给 Anthropic，按用量计费。"
    assert en == ("Captions, viewer comments and retranslation requests are sent to Anthropic "
                  "and billed by usage.")


def test_the_retranslate_tooltip_does_not_claim_the_most_accurate_model():
    zh, en = pair_after(source("app.js"), "redo.title =")
    assert (zh, en) == ("用更强的模型重新翻译这一条", "Retranslate with the stronger model")
    for claim in ("最强模型", "最准", "most accurate"):
        assert claim not in zh + en
    # 术语表是改词的源头（CLAUDE.md 第九条：改词先改它）
    style = (ROOT / "docs" / "i18n-style.md").read_text(encoding="utf-8")
    assert "Retranslate with the stronger model" in style and "most accurate model" not in style
