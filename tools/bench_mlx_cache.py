#!/usr/bin/env python3
"""MLX 缓冲缓存上限基准：同一段音频、逐段配对，量「上限」对识别耗时和显存的影响。

为什么要有它：Apple Silicon 上 MLX 的缓冲是钉住的统一内存，不能压缩也不能换出。2026-09-17
一台 18 GB 的机器上监听进程占到 7.5 GB（其中 6.9 GB 是 Metal 缓冲），把别的程序挤进了
10 GB 的换页。识别耗时在报警的关键路径上，所以上限取多少只能实测。

用法（**一个进程只测一个配置**，测完即退出，保证显存里同一时间只有一个模型）：
    # 16 kHz 单声道 s16le PCM；没有现成音频可以用 say + ffmpeg 合成一段
    python tools/bench_mlx_cache.py unset sample.pcm out-unset.json
    python tools/bench_mlx_cache.py 256   sample.pcm out-256.json
    VARIED=1 python tools/bench_mlx_cache.py 256 sample.pcm out-256-varied.json

第一个参数：unset（不设上限）或 MB 数。VARIED=1 时把音频按 2.5–9 秒的多种长度重新切段——
不设上限时缓存随段长种类增长，这才接近真实直播。比较时用同一份音频、同样的模式，把各次的
ms 数组逐段相除取中位数，并再跑一次 unset 作对照看机器自身的波动。
"""
import json
import os
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.stdio import harden_stdio                     # noqa: E402

harden_stdio()

# 硬闸（CLAUDE.md 第三条）：直播转写进行中再加载一个模型，识别曾从 1.1 秒掉到 38–55 秒、丢 16 段
for _pat in ("tiktok" + "cdn", "main" + ".py --browser", "tiktok-live-translator/main" + ".py"):
    if subprocess.run(["pgrep", "-f", _pat], capture_output=True).returncode == 0:
        raise SystemExit("直播转写仍在进行，拒绝跑模型基准")

VARIED_LENGTHS = [2.5, 9.0, 4.0, 7.5, 3.0, 8.5, 5.0, 6.5, 3.5, 8.0, 4.5, 7.0, 2.8, 9.0, 5.5, 6.0]


def main():
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    limit, pcm_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    os.environ["TLT_MLX_CACHE_MB"] = "off"              # 上限由本脚本自己设，别让默认值掺进来
    from app.asr import create_transcriber
    from app.audio import FRAME_BYTES
    from app.segmenter import SilenceSegmenter

    raw = Path(pcm_path).read_bytes()
    if os.environ.get("VARIED"):
        segments, pos, i, data = [], 0, 0, raw * 2
        while pos < len(data):
            n = int(VARIED_LENGTHS[i % len(VARIED_LENGTHS)] * 16000) * 2
            chunk = data[pos:pos + n]
            if len(chunk) < 2 * 16000 * 2:
                break
            segments.append(chunk)
            pos += n
            i += 1
    else:
        seg, segments = SilenceSegmenter(), []
        for i in range(0, len(raw) - FRAME_BYTES + 1, FRAME_BYTES):
            segments.extend(seg.feed(raw[i:i + FRAME_BYTES]))
        segments.extend(seg.flush() or [])
    if not segments:
        raise SystemExit("音频里没有切出任何一段")

    t0 = time.time()
    tr = create_transcriber(backend="mlx", model_size="large-v3", language="es")
    import mlx.core as mx
    if limit != "unset":
        mx.set_cache_limit(int(limit) * 1024 * 1024)
    tr.transcribe(segments[0])                           # 预热，不计入
    load_s = time.time() - t0
    if hasattr(mx, "reset_peak_memory"):
        mx.reset_peak_memory()
    ms = []
    for s in segments:
        t = time.perf_counter()
        tr.transcribe(s)
        ms.append((time.perf_counter() - t) * 1000.0)
    mem = {k: round(getattr(mx, "get_%s_memory" % k)() / 1048576) for k in ("active", "cache", "peak")}
    fp = subprocess.run(["footprint", str(os.getpid())], capture_output=True, text=True).stdout
    m = re.search(r"([\d.]+) (MB|GB)\s+\S+ \S+\s+\S+ \S+\s+\d+\s+IOAccelerator \(graphics\)", fp)
    gfx = round(float(m.group(1)) * (1024 if m.group(2) == "GB" else 1)) if m else None
    ordered = sorted(ms)
    result = {"limit": limit, "varied": bool(os.environ.get("VARIED")), "segments": len(ms),
              "seg_sec": [round(len(x) / 2 / 16000, 1) for x in segments],
              "ms": [round(x) for x in ms], "median_ms": round(statistics.median(ms)),
              "p95_ms": round(ordered[max(0, int(len(ordered) * 0.95) - 1)]),
              "load_s": round(load_s, 1), "mlx_mb": mem, "graphics_mb": gfx}
    Path(out_path).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    print("{:>6}: {} 段 中位 {} ms P95 {} ms | MLX 在用 {} 缓存 {} 峰值 {} MB | 显卡缓冲 {} MB".format(
        limit, len(ms), result["median_ms"], result["p95_ms"], mem["active"], mem["cache"], mem["peak"], gfx))


if __name__ == "__main__":
    main()
