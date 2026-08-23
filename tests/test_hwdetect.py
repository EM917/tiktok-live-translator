"""hwdetect.recommend：硬件→配置矩阵。核心不变量（见 recommend 的 docstring）：
backend/model/device/compute 是联动整体，用户锁定其中一个时其余必须围绕它
重新推导——绝不能拼出「CPU 跑 large-v3」这类会丢字幕的组合。"""
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
