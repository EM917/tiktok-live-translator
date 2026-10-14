"""启动自检里的「弹幕留意」一行（PR-5，spec §5.9 末）：规则条数和体检警告；选了 Claude 就用不花 token 的探针
（GET /v1/models/{模型}）问一次接口。**这一行最高只到 ok（info 级）**：弹幕留意是次要功能（CLAUDE.md 第六条），
不能让「启动自检」因为它变成要自己展开的状态；有问题写在字里。英文臂没有中文、没有没填的占位符，也不贴原因标签。

假会话，不联网。
"""
import asyncio
import json
import re

import pytest

from app import comment_review as cr
from app import i18n, selfcheck
from tests.guard_support import new_runtime, start
from tests.helpers import run
from tests.test_comment_review import KEY, Resp, Session

CJK = re.compile(r"[\u3000-〿぀-ヿ㐀-䶿一-鿿＀-￯]")
UNFILLED = re.compile(r"\{[^{}]*\}")


def both(row):
    return row, i18n.render(row, i18n.EN)


def guard_with(monkeypatch, tmp_path, backend="claude", key=KEY, enabled=True):
    if key:
        monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    rt = new_runtime(monkeypatch, tmp_path, enabled=enabled)
    rt.cfg["backend"] = backend
    rt.configure_reviewer()
    return rt


def probe_with(rt, responder):
    session = Session(responder)

    async def get_session():
        return session

    rt.reviewer.session = get_session
    return session


def check(rt):
    async def go():
        await start(rt)           # 载入词表，规则条数才有
        return await selfcheck.check_comment_guard(rt)

    return run(go())


def test_the_row_is_named_in_both_languages_and_sits_before_storage():
    assert selfcheck.NAMES["comment_guard"] == "弹幕留意"
    assert i18n.text(selfcheck.NAMES["comment_guard"], i18n.EN) == "Comment Review"
    probes_without = run(selfcheck.run_all(type("A", (), {"comments": False, "denoise": "off", "backend": "auto",
                                                         "model": None, "device": "auto", "compute_type": "auto"})()))
    assert "弹幕留意" not in [c["name"] for c in probes_without]               # 没传守卫：行为不变，没有这一行


def test_rules_only_says_how_many_rules_and_that_no_model_review_is_chosen(monkeypatch, tmp_path):
    rt = guard_with(monkeypatch, tmp_path, backend="none")
    zh, en = both(check(rt))
    assert zh["level"] == "ok" and re.search(r"规则 \d+ 条已生效", zh["detail"]) and "不用模型" in zh["detail"]
    assert re.search(r"\d+ rules active", en["detail"]) and "No model review" in en["detail"]


def test_off_and_the_emergency_switch_are_ok_rows_too(monkeypatch, tmp_path):
    rt = guard_with(monkeypatch, tmp_path, backend="none", enabled=False)
    assert check(rt)["detail"] == "已关闭"
    monkeypatch.setenv("TLT_COMMENT_GUARD", "off")
    rt = guard_with(monkeypatch, tmp_path, backend="none")
    row = check(rt)
    assert row["level"] == "ok" and "TLT_COMMENT_GUARD" in row["detail"]


def test_choosing_claude_without_a_key_says_so_and_what_to_do_without_turning_into_a_warning(monkeypatch, tmp_path):
    rt = guard_with(monkeypatch, tmp_path, key=None)
    zh, en = both(check(rt))
    assert zh["level"] == "ok" and "还没有 Claude 密钥" in zh["detail"] and "只用规则" in zh["detail"] and zh["fix"]
    assert "no Claude API key" in en["detail"] and "Rules only" in en["detail"]
    assert "Settings > Comment Review" in en["fix"]


def test_a_working_key_is_verified_without_spending_tokens(monkeypatch, tmp_path):
    rt = guard_with(monkeypatch, tmp_path)
    session = probe_with(rt, lambda body, n: Resp(200, {}))
    zh, en = both(check(rt))
    assert zh["level"] == "ok" and "Claude Haiku 5.5 复核可用（密钥已验证）" in zh["detail"]
    assert "Claude Haiku 5.5 review is available (key verified)" in en["detail"]
    assert session.posts == [] and len(session.gets) == 1               # 只是 GET，不花 token
    assert KEY not in json.dumps([zh, en])


@pytest.mark.parametrize("status,zh_part,en_part", [
    (401, "拒绝了当前密钥（HTTP 401）", "rejected the current API key (HTTP 401)"),
    (403, "拒绝了当前密钥（HTTP 403）", "rejected the current API key (HTTP 403)"),
    (404, "找不到模型 claude-haiku-5-5".replace("claude-haiku-5-5", "Claude Haiku 5.5"), "doesn’t have the model Claude Haiku 5.5 (HTTP 404)"),
    (500, "返回 HTTP 500", "returned HTTP 500"),
])
def test_a_bad_probe_answer_is_an_ok_row_that_says_rules_only(monkeypatch, tmp_path, status, zh_part, en_part):
    rt = guard_with(monkeypatch, tmp_path)
    probe_with(rt, lambda body, n: Resp(status, {}))
    zh, en = both(check(rt))
    assert zh["level"] == "ok" and zh_part in zh["detail"] and "只用规则" in zh["detail"]
    assert en_part in en["detail"] and "Rules only" in en["detail"]


def test_a_probe_that_cannot_connect_or_hangs_is_still_an_ok_row(monkeypatch, tmp_path):
    rt = guard_with(monkeypatch, tmp_path)
    probe_with(rt, lambda body, n: ConnectionError("down"))
    zh, en = both(check(rt))
    assert zh["level"] == "ok" and "这次没连上 Claude（ConnectionError）" in zh["detail"]
    assert "Couldn’t reach Claude this time (ConnectionError)" in en["detail"]


def test_every_state_reads_as_english_with_no_unfilled_placeholders_and_no_cause_labels(monkeypatch, tmp_path):
    from tests.i18n_rules import EN_CAUSAL, EN_LABELS
    rows = []
    for key, backend, status in ((None, "claude", None), (KEY, "claude", 200), (KEY, "claude", 401),
                                 (KEY, "claude", 404), (KEY, "claude", 503), (KEY, "none", None)):
        rt = guard_with(monkeypatch, tmp_path, backend=backend, key=key)
        if status is not None:
            probe_with(rt, lambda body, n, s=status: Resp(s, {}))
        rows.append(check(rt))
    for row in rows:
        en = i18n.render(row, i18n.EN)
        for field in ("detail", "fix"):
            assert not CJK.search(en[field]), en[field]
            assert not UNFILLED.search(en[field]), en[field]
            assert "  " not in en[field].strip()
            assert not EN_LABELS.search(en[field]) and not EN_CAUSAL.search(en[field]), en[field]


def test_run_all_includes_the_row_when_a_guard_is_given(monkeypatch, tmp_path):
    rt = guard_with(monkeypatch, tmp_path, backend="none")

    async def go():
        await start(rt)
        return await selfcheck.run_all(type("A", (), {"comments": False, "denoise": "off", "backend": "auto",
                                                      "model": None, "device": "auto", "compute_type": "auto"})(),
                                       guard=rt)

    names = [c["name"] for c in run(go())]
    assert "弹幕留意" in names and names.index("弹幕留意") == len(names) - 2         # 在「磁盘空间」前面
    assert asyncio.iscoroutinefunction(selfcheck.check_comment_guard)
    assert cr.GUARD_CLAUDE_MODEL == "claude-haiku-5-5"
