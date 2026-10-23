"""hwdetect.recommend：硬件→配置矩阵。核心不变量（见 recommend 的 docstring）：
backend/model/device/compute 是联动整体，用户锁定其中一个时其余必须围绕它
重新推导——绝不能拼出「CPU 跑 large-v3」这类会丢字幕的组合。

I10：rosetta() 读 sysctl.proc_translated（sysctl 打桩，不依赖跑测试的这台机器）。"""
import subprocess
from types import SimpleNamespace

import pytest

from app import hwdetect
from app.hwdetect import recommend


def _info(apple_silicon=False, has_mlx=False, has_cuda=False, cores=8, ram_gb=16.0):
    return {"os": "test", "machine": "test", "apple_silicon": apple_silicon,
            "has_mlx": has_mlx, "has_cuda": has_cuda, "cores": cores,
            "ram_gb": ram_gb, "has_ffmpeg": True}


def test_mlx_gpu_gets_large_v3():
    rec = recommend(_info(apple_silicon=True, has_mlx=True))
    assert rec["backend"] == "mlx"
    assert rec["model"] == "large-v3"


def test_forced_ct2_on_apple_silicon_downgrades_model():
    # 有 mlx 但用户强制 ct2：不能沿用 GPU 才跑得动的 large-v3
    rec = recommend(_info(apple_silicon=True, has_mlx=True), backend="ct2")
    assert rec["backend"] == "ct2"
    assert rec["model"] == "large-v3-turbo"


def test_cuda_gets_large_v3_float16():
    rec = recommend(_info(has_cuda=True))
    assert (rec["backend"], rec["model"], rec["device"]) == ("ct2", "large-v3", "cuda")
    assert rec["compute_type"] == "float16"


def test_forced_cpu_ignores_cuda():
    # 有 CUDA 但用户强制 device=cpu：必须按 CPU 能力选小模型
    rec = recommend(_info(has_cuda=True, cores=16, ram_gb=32.0), device="cpu")
    assert rec["device"] == "cpu"
    assert rec["model"] in ("small", "base", "large-v3-turbo")
    assert rec["model"] != "large-v3"


def test_decent_cpu_gets_small_int8():
    rec = recommend(_info(cores=8, ram_gb=16.0))
    assert (rec["model"], rec["compute_type"]) == ("small", "int8")


def test_lowend_cpu_gets_base():
    rec = recommend(_info(cores=4, ram_gb=4.0))
    assert (rec["model"], rec["compute_type"]) == ("base", "int8")


def test_auto_backend_without_mlx_falls_to_ct2():
    rec = recommend(_info(apple_silicon=True, has_mlx=False), backend="auto")
    assert rec["backend"] == "ct2"
    assert rec["model"] == "large-v3-turbo"   # Apple Silicon CPU 的临界实时档


# ---- I10：Apple 芯片上跑着 Intel 版 Python（Rosetta 转译） ---------------------------------------

@pytest.fixture(autouse=True)
def _fresh_sysctl_probe():
    """sysctl 的结果在进程里缓存着（_proc_translated）：每条测试前后都清掉，打桩的结果不漏到别的测试。"""
    hwdetect._proc_translated.cache_clear()
    yield
    hwdetect._proc_translated.cache_clear()


@pytest.mark.parametrize("returncode, stdout, expected", [
    (0, "1\n", True),          # 转译中
    (0, "0\n", False),         # 原生
    (1, "", False),            # Intel Mac：没有这个键，sysctl 报错
], ids=["translated", "native", "intel-mac"])
def test_rosetta_reads_sysctl_proc_translated(monkeypatch, returncode, stdout, expected):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(hwdetect.sys, "platform", "darwin")
    monkeypatch.setattr(hwdetect.subprocess, "run", fake_run)
    assert hwdetect.rosetta() is expected
    assert calls == [["sysctl", "-n", "sysctl.proc_translated"]]


def test_rosetta_is_false_without_evidence(monkeypatch):
    """sysctl 跑不起来、或者根本不是 macOS：不报（没有证据就不说这台机器有问题）。"""
    def boom(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 5)

    monkeypatch.setattr(hwdetect.subprocess, "run", boom)
    monkeypatch.setattr(hwdetect.sys, "platform", "darwin")
    assert hwdetect.rosetta() is False
    called = []
    monkeypatch.setattr(hwdetect.subprocess, "run", lambda cmd, **kw: called.append(cmd))
    for other in ("win32", "linux"):
        monkeypatch.setattr(hwdetect.sys, "platform", other)
        assert hwdetect.rosetta() is False
    assert called == []


def test_the_sysctl_probe_runs_once_per_process(monkeypatch):
    """进程转不转译启动后就定了：rosetta() 调多少次（detect() 每次都调它，点「开始」和自检都同步
    调用 detect()），sysctl 只跑一次。
    跑不起来那次不记：下次还会再探，不把一次超时记成整个进程的答案。"""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd, 5)
        return SimpleNamespace(returncode=0, stdout="1\n", stderr="")

    monkeypatch.setattr(hwdetect.sys, "platform", "darwin")
    monkeypatch.setattr(hwdetect.subprocess, "run", fake_run)
    assert hwdetect.rosetta() is False            # 第一次超时：不报，也不记
    for _ in range(3):
        assert hwdetect.rosetta() is True
    assert len(calls) == 2


def test_detect_reports_rosetta_so_the_startup_check_can_say_so(monkeypatch):
    """转译中 platform.machine() 报 x86_64，apple_silicon 为假；rosetta 这一项单独给出来，
    自检「语音识别」一行据此给换 Python 的步骤（selfcheck.check_asr）。"""
    monkeypatch.setattr(hwdetect, "rosetta", lambda: True)
    monkeypatch.setattr(hwdetect, "_cuda_usable", lambda: False)
    info = hwdetect.detect()
    assert info["rosetta"] is True
    # 推荐配置照旧按探测到的硬件推（Intel 路径的 CPU 小模型），不因为这一项改变
    rec = recommend(dict(info, apple_silicon=False, has_mlx=False, cores=8, ram_gb=16.0))
    assert (rec["backend"], rec["device"]) == ("ct2", "cpu") and rec["model"] in ("small", "base")
