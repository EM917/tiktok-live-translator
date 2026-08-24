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


# ---- 以下每条都对应一次「自检自己不可信」的实测发现 ----

def test_crashed_probe_is_fail_not_warn(monkeypatch):
    """探测崩了 = 这项能力根本没验过，必须是红的。

    曾经记成 WARN，界面上就显示「✅ 自检通过，1 项提醒」——正是本模块
    要消灭的那种「看起来没事」。名字也要保留，否则用户不知道是哪块没验。"""
    async def boom(*a, **kw):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(selfcheck, "check_asr", boom)
    args = SimpleNamespace(denoise="off", backend="auto", model=None,
                           translator="none", device="auto")
    checks = run(selfcheck.run_all(args, None, None))
    crashed = [c for c in checks if "probe exploded" in c["detail"]]
    assert len(crashed) == 1
    assert crashed[0]["level"] == "fail"
    assert crashed[0]["name"] == "语音识别"        # 不能退化成「自检项」


def test_ffmpeg_that_cannot_execute_is_a_failure(monkeypatch):
    """只找到路径不算数：被 Gatekeeper 隔离的二进制文件在、一跑就废。"""
    from app import ffmpeg_bin
    monkeypatch.setattr(ffmpeg_bin, "find_ffmpeg", lambda: "/fake/ffmpeg")
    monkeypatch.setattr(selfcheck, "_ffmpeg_runs", lambda exe: False)
    c = run(selfcheck.check_ffmpeg())
    assert c["level"] == "fail"
    assert c["fix"]


def test_model_cache_does_not_confuse_turbo_with_large_v3(tmp_path, monkeypatch):
    """`"large-v3" in "…large-v3-turbo"` 为真——子串匹配会让 turbo 冒充
    large-v3，界面报绿而第一次开播卡在 3GB 下载上。"""
    hub = tmp_path / "hub"
    turbo = hub / "models--mlx-community--whisper-large-v3-turbo" / "snapshots" / "abc"
    turbo.mkdir(parents=True)
    (turbo / "weights.npz").write_text("x")
    monkeypatch.setattr(selfcheck, "_hub_dirs", lambda: hub)
    assert selfcheck._model_cached("large-v3-turbo", "mlx") is True
    assert selfcheck._model_cached("large-v3", "mlx") is False


def test_model_cache_does_not_accept_another_backends_copy(tmp_path, monkeypatch):
    """ct2 的 Systran 仓库和 MLX 的 mlx-community 仓库是两个不同的 3GB 东西，
    名字里都含 large-v3。"""
    hub = tmp_path / "hub"
    ct2 = hub / "models--Systran--faster-whisper-large-v3" / "snapshots" / "abc"
    ct2.mkdir(parents=True)
    (ct2 / "model.bin").write_text("x")
    monkeypatch.setattr(selfcheck, "_hub_dirs", lambda: hub)
    assert selfcheck._model_cached("large-v3", "ct2") is True
    assert selfcheck._model_cached("large-v3", "mlx") is False


def test_model_cache_rejects_a_half_finished_download(tmp_path, monkeypatch):
    """HF 一开始传就把目录建好，只看目录在不在会把下到一半当成下完了。"""
    hub = tmp_path / "hub"
    repo = hub / "models--mlx-community--whisper-large-v3-mlx"
    (repo / "snapshots" / "abc").mkdir(parents=True)
    ((repo / "snapshots" / "abc") / "weights.npz").write_text("x")
    (repo / "blobs").mkdir()
    (repo / "blobs" / "deadbeef.incomplete").write_text("partial")
    monkeypatch.setattr(selfcheck, "_hub_dirs", lambda: hub)
    assert selfcheck._model_cached("large-v3", "mlx") is False


def test_check_asr_passes_device_through(monkeypatch):
    """recommend() 把 backend/model/device/compute_type 当一组重算。少传
    device，自检算出的就不是管线待会儿真正加载的那套。"""
    seen = {}

    def fake_recommend(backend="auto", device="auto"):
        seen["backend"], seen["device"] = backend, device
        return {"backend": "ct2", "model": "large-v3", "device": device,
                "compute_type": "int8", "reason": ""}

    from app import hwdetect
    monkeypatch.setattr(hwdetect, "recommend", fake_recommend)
    monkeypatch.setattr(selfcheck, "_model_cached", lambda m, b: True)
    run(selfcheck.check_asr(SimpleNamespace(backend="auto", model=None, device="cpu")))
    assert seen == {"backend": "auto", "device": "cpu"}


def test_apple_silicon_on_the_cpu_backend_is_flagged(monkeypatch):
    """一台 M4 因为 mlx-whisper 没装上，一直用 CPU 跑 turbo。

    自检当时只如实报了「ct2 + large-v3-turbo」，没说这台机器本可以快一倍——
    用户看不出有什么不对。能力「在工作」但只发挥了一小部分，同样是静默降级。
    """
    from app import hwdetect

    monkeypatch.setattr(hwdetect, "detect",
                        lambda: {"apple_silicon": True, "has_mlx": False,
                                 "has_cuda": False, "cores": 10, "ram_gb": 16})
    monkeypatch.setattr(hwdetect, "recommend",
                        lambda backend="auto", device="auto": {
                            "backend": "ct2", "model": "large-v3-turbo",
                            "device": "cpu", "compute_type": "auto", "reason": ""})
    monkeypatch.setattr(selfcheck, "_model_cached", lambda m, b: True)
    monkeypatch.setattr(selfcheck, "_importable", lambda name: True)
    c = run(selfcheck.check_asr(SimpleNamespace(backend="auto", model=None,
                                               device="auto")))
    assert c["level"] == "warn"
    assert "CPU" in c["detail"]
    assert c["fix"]


def test_apple_silicon_on_mlx_is_not_flagged(monkeypatch):
    from app import hwdetect

    monkeypatch.setattr(hwdetect, "detect",
                        lambda: {"apple_silicon": True, "has_mlx": True})
    monkeypatch.setattr(hwdetect, "recommend",
                        lambda backend="auto", device="auto": {
                            "backend": "mlx", "model": "large-v3",
                            "device": "auto", "compute_type": "auto", "reason": ""})
    monkeypatch.setattr(selfcheck, "_model_cached", lambda m, b: True)
    monkeypatch.setattr(selfcheck, "_importable", lambda name: True)
    c = run(selfcheck.check_asr(SimpleNamespace(backend="auto", model=None,
                                               device="auto")))
    assert c["level"] == "ok"
