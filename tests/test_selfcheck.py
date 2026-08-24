"""启动自检：确认每项能力**实测**可用，而不是看配置写没写。

这个模块存在的原因是一次真实事故：降噪模型下载时被截断，程序每次启动
都静默退回「不降噪」，只在一行日志里说了句话，几周没人发现。所以这里的
测试重点全在「坏掉时会不会变红」，而不是「好的时候是不是绿的」。
"""
import asyncio
from types import SimpleNamespace

import pytest

from app import pipeline, selfcheck


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_denoise_reports_fail_when_model_is_corrupt(tmp_path, monkeypatch):
    """就是当初那个 bug：文件在，但实际跑不起来。"""
    model = tmp_path / "bd.rnnn"
    model.write_bytes(b"truncated garbage")     # 截断的模型，和当初一模一样
    monkeypatch.setattr(pipeline, "DENOISE_MODEL", model)
    monkeypatch.setattr(pipeline, "_arnndn_probe", lambda path: False)
    c = run(selfcheck.check_denoise(SimpleNamespace(denoise="auto")))
    assert c["level"] == "fail"
    assert c["fix"]                      # 必须给出怎么修


def test_denoise_off_by_choice_is_not_a_failure(tmp_path, monkeypatch):
    """用户自己关掉的，和悄悄坏掉的，不能混为一谈。"""
    c = run(selfcheck.check_denoise(SimpleNamespace(denoise="off")))
    assert c["level"] == "ok"


def test_empty_watchlist_is_a_failure_not_a_warning():
    """词表为空 = 一条报警都不会发。这是本工具的主职能，必须是红的。"""
    c = run(selfcheck.check_watchlist(None))
    assert c["level"] == "fail"


def test_summarize_counts_levels():
    checks = [{"level": "ok"}, {"level": "warn"}, {"level": "fail"}, {"level": "fail"}]
    assert selfcheck.summarize(checks) == {"total": 4, "ok": 1, "warn": 1, "fail": 2}


def test_run_all_survives_a_probe_that_raises(monkeypatch):
    """任何一项探测崩了，自检本身不能崩——否则程序启动不了。"""
    async def boom(*a, **kw):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(selfcheck, "check_ffmpeg", boom)
    args = SimpleNamespace(denoise="off", backend="auto", model=None, translator="none")
    checks = run(selfcheck.run_all(args, None, None))
    assert checks                                   # 仍然返回了结果
    assert any(c["level"] == "fail" for c in checks)


@pytest.mark.parametrize("check", ["ffmpeg", "denoise", "asr", "translator",
                                   "watchlist", "glossary", "audit", "resolver", "disk"])
def test_every_check_is_registered(check):
    assert hasattr(selfcheck, "check_" + check)
