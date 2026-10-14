"""tools/guard_review_eval.py（PR-5，spec §5.13）：虚构弹幕集 × 多遍，打真的 Claude 接口，只测格式和稳定性。

钉住的是安全闸，不是评估结果：
  * 直播转写进行中（pgrep 打桩成命中）：退出码非零，**零 HTTP、连密钥都没读**；
  * 花费硬上限：每次调用前先按最坏情况算，加上已花的会超过上限就不发；每次回来按 usage 记真实花费；
  * 没有密钥：不运行、退出码非零；接口出错：停下来，不循环重试；
  * 密钥不出现在输出、报告里（连尾四位都不写）；请求只发给固定地址，不读 ANTHROPIC_BASE_URL；
  * 指标的算法：格式合格率、几遍一致率、P50 / P95、拒答率、总花费。

全程假会话，不联网、不读真的 settings.json。
"""
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from app import comment_review as cr
from tests.helpers import run
from tests.test_comment_review import KEY, Resp, Session, answer, message

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("guard_review_eval", ROOT / "tools" / "guard_review_eval.py")
E = importlib.util.module_from_spec(SPEC)
sys.modules["guard_review_eval"] = E
SPEC.loader.exec_module(E)

CASES = ROOT / "tests" / "fixtures" / "comment_guard" / "review_cases.jsonl"


def stub_pgrep(monkeypatch, answers):
    """answers: 按 pgrep 的 pattern 给 returncode；返回调用记录。"""
    calls = []

    class Done:
        def __init__(self, code):
            self.returncode = code

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return Done(answers.get(argv[-1], 1))

    monkeypatch.setattr(E.subprocess, "run", fake_run)
    return calls


def fake_reviewer(responder=None):
    reviewer = cr.ClaudeReviewer(KEY)
    session = Session(responder or (lambda body, n: answer(body)))

    async def get_session():
        return session

    reviewer.session = get_session
    return reviewer, session


# ---- 直播闸 --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("pattern", E.LIVE_PATTERNS)
def test_a_live_transcription_refuses_the_run_before_reading_the_key_or_touching_the_network(monkeypatch, tmp_path, capsys, pattern):
    calls = stub_pgrep(monkeypatch, {pattern: 0})
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    touched = []
    monkeypatch.setattr(E, "find_key", lambda paths: touched.append("key") or (KEY, "env"))
    monkeypatch.setattr(E.cr, "ClaudeReviewer", lambda *a, **k: touched.append("reviewer"))
    monkeypatch.setattr(E.asyncio, "run", lambda *a, **k: touched.append("run"))
    code = E.main(["claude", str(CASES), str(tmp_path / "out.json"), "--yes"])
    assert code != 0 and touched == []                         # 一个请求都没发，连密钥都没读
    assert not (tmp_path / "out.json").exists()
    assert all(c[:2] == ["pgrep", "-f"] for c in calls) and calls[-1][-1] == pattern
    assert "拒绝运行" in capsys.readouterr().err


def test_without_pgrep_the_gate_cannot_tell_but_says_so_on_stderr(monkeypatch, capsys):
    def no_pgrep(argv, **kwargs):
        raise FileNotFoundError("pgrep")

    monkeypatch.setattr(E.subprocess, "run", no_pgrep)
    assert E.live_running() is None
    assert "pgrep" in capsys.readouterr().err


def test_the_gate_patterns_are_not_written_out_whole_in_the_file(monkeypatch):
    text = (ROOT / "tools" / "guard_review_eval.py").read_text(encoding="utf-8")
    for pat in E.LIVE_PATTERNS:
        assert text.count(pat) == 0                              # 拆开写，免得本文件自己被这条 pgrep 扫到


def test_no_key_means_no_run(monkeypatch, tmp_path, capsys):
    stub_pgrep(monkeypatch, {})
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    empty = tmp_path / "settings.json"
    empty.write_text("{}", encoding="utf-8")
    ran = []
    monkeypatch.setattr(E.asyncio, "run", lambda *a, **k: ran.append(1))
    assert E.main(["claude", str(CASES), "--settings", str(empty), "--yes"]) == 3 and ran == []
    assert "没有 Claude 密钥" in capsys.readouterr().err


def test_the_key_comes_from_the_environment_first_then_the_settings_files(monkeypatch, tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text("{not json", encoding="utf-8")
    b.write_text(json.dumps({"api_keys": {"ANTHROPIC_API_KEY": " key-from-settings "}}), encoding="utf-8")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert E.find_key([a, tmp_path / "missing.json", b]) == ("key-from-settings", "settings")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key-from-env")
    assert E.find_key([b]) == ("key-from-env", "env")
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert E.find_key([a]) == (None, None)


# ---- 花费 ----------------------------------------------------------------------------------------------

def result_with(**kw):
    r = cr.ReviewResult()
    for name, value in kw.items():
        setattr(r, name, value)
    return r


def test_the_cost_is_priced_from_usage_at_haiku_5_5_rates_with_cache_premium_and_discount():
    # 1M 输入 + 1M 输出：$0.10 + $0.50
    assert E.cost_usd(result_with(in_tokens=1_000_000, out_tokens=1_000_000)) == pytest.approx(0.60)
    # 缓存读 0.1 倍、缓存写 1.25 倍
    r = result_with(in_tokens=1_000_000, out_tokens=0, cache_read_tokens=400_000, cache_write_tokens=200_000)
    assert E.cost_usd(r) == pytest.approx(0.4 * 0.10 + 0.2 * 0.125 + 0.4 * 0.01)
    assert E.cost_usd(result_with(in_tokens=None, out_tokens=None)) is None          # 没有 usage：由调用方按最坏情况记


def entries_for(n, hints=True):
    rows = [{"id": "c%03d" % i, "text": "es falso numero %d" % i, "hint": "accuse_claim", "flags": ["maybe_question"],
             "intended": "fraud_claim"} for i in range(n)]
    return E.build_entries(rows, hints=hints)[0]


def test_the_worst_case_bound_is_above_what_a_normal_call_costs():
    reviewer, _ = fake_reviewer()
    chunk = entries_for(8)
    worst = E.worst_case_usd(reviewer, chunk)
    normal = E.cost_usd(result_with(in_tokens=1700, out_tokens=300, cache_read_tokens=0, cache_write_tokens=0))
    assert worst > normal and worst < 0.002


def test_the_run_stops_before_a_call_that_could_push_the_total_over_the_cap():
    def costly(body, n):
        return answer(body, usage={"input_tokens": 500_000, "output_tokens": 200_000})        # 一次 $0.15

    reviewer, session = fake_reviewer(costly)
    report = run(E.run_eval(reviewer, entries_for(16), runs=3, batch=8, cap=0.10))
    assert len(session.posts) == 1                              # 第一次花了 $0.15 已经超了；第二次根本没发
    assert report["stopped_early"] == "cost_cap" and report["cost"]["within_cap"] is False
    # 每次发之前先看最坏情况：上限设得连一次的最坏情况都装不下，一次都不发
    reviewer, session = fake_reviewer()
    report = run(E.run_eval(reviewer, entries_for(16), runs=3, batch=8, cap=0.0001))
    assert session.posts == [] and report["stopped_early"] == "cost_cap" and report["cost"]["usd"] == 0
    assert report["cost"]["within_cap"] is True


def test_a_call_without_usage_is_charged_at_the_worst_case_and_flagged():
    def no_usage(body, n):
        resp = answer(body)
        resp._payload.pop("usage")
        return resp

    reviewer, session = fake_reviewer(no_usage)
    report = run(E.run_eval(reviewer, entries_for(8), runs=1, batch=8, cap=0.10))
    assert report["cost"]["usd"] > 0 and report["cost"]["estimated_calls"] == 1


def test_the_default_run_of_the_fictional_set_fits_the_cap_even_in_the_worst_case(monkeypatch):
    rows = E.load_cases(CASES)
    entries, skipped = E.build_entries(rows)
    assert 140 <= len(entries) + len(skipped) <= 160
    reviewer, _ = fake_reviewer()
    assert E.estimate_usd(reviewer, entries, runs=3, batch=8) < 0.10


# ---- 接口出错：停下来，不循环重试 ---------------------------------------------------------------------------------

@pytest.mark.parametrize("status", [401, 404, 429, 500])
def test_an_api_error_stops_the_run_at_once_without_retrying(status):
    reviewer, session = fake_reviewer(lambda body, n: Resp(status, {}))
    report = run(E.run_eval(reviewer, entries_for(24), runs=3, batch=8, cap=0.10))
    assert len(session.posts) == 1 and report["stopped_early"].startswith("api_error:")
    assert report["runs_completed"] == 0 and report["calls"] == 1


def test_a_refusal_or_bad_json_is_a_result_not_an_error():
    def mixed(body, n):
        if n == 1:
            return Resp(200, dict(message("", stop="refusal"), stop_details={"type": "refusal", "category": "general_harms"}))
        if n == 2:
            return Resp(200, message("not json"))
        return answer(body)

    reviewer, session = fake_reviewer(mixed)
    report = run(E.run_eval(reviewer, entries_for(24), runs=1, batch=8, cap=0.10))
    assert len(session.posts) == 3 and report["stopped_early"] is None
    assert report["refusal"]["calls"] == 1 and report["refusal"]["categories"] == {"general_harms": 1}
    assert report["format"]["outcomes"] == {"refusal": 1, "invalid": 1, "ok": 1}
    assert report["format"]["verdicts_valid"] == 8 and report["format"]["verdicts_asked"] == 24


# ---- 指标 ----------------------------------------------------------------------------------------------

def test_metrics_for_a_stable_model():
    reviewer, session = fake_reviewer()
    report = run(E.run_eval(reviewer, entries_for(16), runs=3, batch=8, cap=0.10))
    assert report["calls"] == 6 and report["runs_completed"] == 3 and report["stopped_early"] is None
    assert report["format"]["valid_rate"] == 1.0 and report["format"]["calls_ok_rate"] == 1.0
    assert report["agreement"]["cases_compared"] == 16 and report["agreement"]["label_rate"] == 1.0
    assert report["agreement"]["label_and_sure_rate"] == 1.0
    assert report["refusal"]["rate"] == 0.0
    assert report["latency_ms"]["n"] == 6 and report["latency_ms"]["p50"] is not None
    assert report["cost"]["usd"] == pytest.approx(6 * E.cost_usd(result_with(
        in_tokens=1500, out_tokens=120, cache_read_tokens=0, cache_write_tokens=0)), abs=1e-6)
    assert report["model"] == "claude-haiku-5-5" and report["prompt_hash"] == cr.load_prompt()[1]


def test_run_to_run_agreement_counts_a_case_only_when_every_run_gave_the_same_label():
    seen = {}

    def flip(body, n):
        # 同一批第二遍对第一条改口
        key = body["messages"][0]["content"]
        seen[key] = seen.get(key, 0) + 1
        plan = {"numero 0": ("friendly" if seen[key] == 2 else "fraud_claim", "none", True, "")}
        return answer(body, plan, default=("fraud_claim", "none", True, ""))

    reviewer, session = fake_reviewer(flip)
    report = run(E.run_eval(reviewer, entries_for(8), runs=3, batch=8, cap=0.10))
    a = report["agreement"]
    assert a["cases_compared"] == 8 and a["label_identical"] == 7 and a["label_rate"] == 0.875
    assert a["attack_or_not_identical"] == 7


def test_percentiles_interpolate_and_handle_empty():
    assert E.percentile([], 0.5) is None
    assert E.percentile([10], 0.95) == 10
    assert E.percentile([10, 20, 30, 40], 0.5) == 25.0
    assert E.percentile(list(range(1, 101)), 0.95) == pytest.approx(95.05, abs=0.06)


def test_the_no_hints_arm_sends_no_hint_or_flags():
    reviewer, session = fake_reviewer()
    run(E.run_eval(reviewer, entries_for(8, hints=False), runs=1, batch=8, cap=0.10))
    content = session.posts[0]["body"]["messages"][0]["content"]
    assert "hint=" not in content and "flags=" not in content


def test_the_report_has_no_comment_text_and_no_key(monkeypatch, tmp_path, capsys):
    stub_pgrep(monkeypatch, {})
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    session = Session(lambda body, n: answer(body))

    async def get_session():
        return session

    monkeypatch.setattr(cr.ClaudeReviewer, "session", lambda self: get_session())
    out = tmp_path / "eval" / "report.json"
    code = E.main(["claude", str(CASES), str(out), "--runs", "1", "--yes"])
    assert code == 0 and out.exists() and out.with_suffix(".md").exists()
    shown = capsys.readouterr().out
    report_text = out.read_text(encoding="utf-8") + out.with_suffix(".md").read_text(encoding="utf-8")
    assert KEY not in shown and KEY not in report_text and KEY[-4:] not in shown     # 连尾四位都不写
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["cases"] > 100 and report["calls"] >= report["cases"] // 8
    for row in E.load_cases(CASES):
        if len(row["text"]) >= 15:                                                    # 「ok」这种短的会碰巧出现在别的字段里
            assert row["text"] not in report_text                                     # 报告里没有弹幕原文
    assert "来自 env" in shown and "不显示" in shown


def test_requests_go_to_the_fixed_address_whatever_the_environment_says(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9/evil")
    reviewer, session = fake_reviewer()
    run(E.run_eval(reviewer, entries_for(8), runs=1, batch=8, cap=0.10))
    assert session.posts[0]["url"] == "https://api.anthropic.com/v1/messages"


def test_the_cancelled_prompt_does_not_run_anything(monkeypatch, tmp_path):
    stub_pgrep(monkeypatch, {})
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    ran = []
    monkeypatch.setattr(E.asyncio, "run", lambda *a, **k: ran.append(1))
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert E.main(["claude", str(CASES)]) == 1 and ran == []
    assert asyncio.iscoroutinefunction(E.run_eval)
