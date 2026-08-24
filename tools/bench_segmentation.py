#!/usr/bin/env python3
"""切段参数基准：在真实直播音频上找 延迟 × 召回 × 算力 的帕累托最优点。

用法：
    # 先录一段真实直播音频（16kHz 单声道 PCM）
    python tools/bench_segmentation.py --capture "https://www.tiktok.com/@主播/live" \
        --seconds 240 --out sample.pcm
    # 再跑基准
    python tools/bench_segmentation.py --audio sample.pcm --source es

为什么不是「哪个最快」而是帕累托：这个工具的业务是违禁词监听，
**漏词的代价远高于晚几秒**。切短片段能让字幕早出来，但每次 ASR 调用的成本
几乎是固定的（编码器按 30 秒窗口跑），调用次数翻倍 ≈ 算力翻倍；而且文本会
更碎、重复更多，跨片段的短语更容易被切开。所以要同时看四个维度。

关键指标是**检测延迟**而不是字幕延迟：违禁词说在片段开头还是结尾，报警时间
能差出整整一个片段。这里用「词首时刻 → 该词所在片段识别完成」来度量，
参照转写用大窗口 + 词级时间戳跑一次作为基准真值。
"""
import argparse
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SAMPLE_RATE = 16000
BYTES_PER_SEC = SAMPLE_RATE * 2

# 候选配置：(min_seg_sec, max_seg_sec, trailing_silence_sec)
CONFIGS = [
    ("现状 2.5/9.0/0.70", 2.5, 9.0, 0.70),
    ("A 3.0/5.0/0.45", 3.0, 5.0, 0.45),
    ("B 2.5/4.5/0.40", 2.5, 4.5, 0.40),
    ("C 2.0/4.0/0.35", 2.0, 4.0, 0.35),
    ("D 1.5/4.0/0.35", 1.5, 4.0, 0.35),
]


def capture(url, seconds, out_path):
    """用 yt-dlp + ffmpeg 录一段直播音频，格式与运行时管线一致。"""
    from app.ffmpeg_bin import find_ffmpeg

    media = subprocess.run(
        [sys.executable, "-m", "yt_dlp", "-g", "-f",
         "flv-ao/bestaudio/flv-hd/best", "--no-warnings", "--", url],
        capture_output=True, text=True, timeout=90).stdout.strip().splitlines()
    if not media:
        raise SystemExit("解析直播流失败——主播可能没在播")
    ffmpeg = find_ffmpeg()
    subprocess.run(
        [ffmpeg, "-nostdin", "-loglevel", "error", "-reconnect", "1",
         "-i", media[0], "-t", str(seconds), "-vn", "-ac", "1",
         "-ar", str(SAMPLE_RATE), "-f", "s16le", str(out_path), "-y"],
        check=True)
    print("已录制 {:.0f} 秒 → {}".format(
        out_path.stat().st_size / BYTES_PER_SEC, out_path))


def segment_audio(pcm, min_sec, max_sec, trailing):
    """用真实的 SilenceSegmenter 切段，返回 [(音频字节, 起始秒, 结束秒)]。"""
    from app.audio import FRAME_BYTES, FRAME_SEC
    from app.segmenter import SilenceSegmenter

    seg = SilenceSegmenter(min_seg_sec=min_sec, max_seg_sec=max_sec,
                           trailing_silence_sec=trailing)
    out = []
    consumed = 0.0        # 已喂入的音频秒数（= 片段结束时刻）
    for i in range(len(pcm) // FRAME_BYTES):
        frame = pcm[i * FRAME_BYTES:(i + 1) * FRAME_BYTES]
        consumed += FRAME_SEC
        for chunk in seg.feed(frame):
            dur = len(chunk) / BYTES_PER_SEC
            out.append((chunk, consumed - dur, consumed))
    for chunk in seg.flush():
        dur = len(chunk) / BYTES_PER_SEC
        out.append((chunk, consumed - dur, consumed))
    return out


def tokens_of(text):
    from app.detector import normalize
    return normalize(text).split()


def duplicate_ratio(tokens):
    """重复率：切太碎时模型会把同一句话反复吐出来，这个比例会飙升。"""
    if len(tokens) < 4:
        return 0.0
    grams = [" ".join(tokens[i:i + 3]) for i in range(len(tokens) - 2)]
    return 1.0 - len(set(grams)) / len(grams)


def run_config(pcm, label, min_sec, max_sec, trailing, make_transcriber, ref_words):
    # 每个配置必须用全新的识别器：滚动上下文（initial_prompt）会跨配置累积，
    # 后跑的配置白蹭前面攒下的上下文，召回率直接失真
    transcriber = make_transcriber()
    segments = segment_audio(pcm, min_sec, max_sec, trailing)
    audio_sec = len(pcm) / BYTES_PER_SEC
    texts, asr_times, seg_lens = [], [], []
    detect_lat = []
    covered = []          # 每段 (起, 止, 识别完成时刻)

    for chunk, start, end in segments:
        t0 = time.time()
        result = transcriber.transcribe(chunk)
        dt = time.time() - t0
        asr_times.append(dt)
        seg_lens.append(end - start)
        texts.append(result.raw_text or result.text)
        covered.append((start, end, end + dt))

    # 检测延迟：每个基准真值词从「说出口」到「所在片段识别完成」要多久
    for _word, wstart in ref_words:
        for start, end, done in covered:
            if start <= wstart < end:
                detect_lat.append(done - wstart)
                break

    all_tokens = tokens_of(" ".join(texts))
    got = set(all_tokens)
    recall = (sum(1 for w, _ in ref_words if w in got) / len(ref_words)
              if ref_words else 0.0)
    rtf = sum(asr_times) / audio_sec
    lat = sorted(detect_lat)
    return {
        "label": label,
        "segments": len(segments),
        "asr_max": max(asr_times) if asr_times else 0,
        "matched": len(detect_lat),
        "seg_p50": statistics.median(seg_lens) if seg_lens else 0,
        "rtf": rtf,
        "detect_p50": statistics.median(lat) if lat else None,
        "detect_p95": lat[int(len(lat) * 0.95) - 1] if len(lat) > 1 else None,
        "recall": recall,
        "dup": duplicate_ratio(all_tokens),
        "words": len(all_tokens),
    }


def main():
    ap = argparse.ArgumentParser(description="切段参数基准")
    ap.add_argument("--capture", help="直播间地址：录一段音频后退出")
    ap.add_argument("--seconds", type=int, default=240)
    ap.add_argument("--out", default="sample.pcm")
    ap.add_argument("--audio", help="已录好的 16kHz 单声道 PCM 文件")
    ap.add_argument("--source", default="es", help="主播语言（锁定可省掉语言检测）")
    ap.add_argument("--model", default=None, help="whisper 模型，默认按硬件推荐")
    ap.add_argument("--backend", default="auto")
    ap.add_argument("--asr-temperature", type=float, default=None,
                    dest="asr_temperature", help="识别解码温度（默认取产品默认值 0）")
    ap.add_argument("--context", action="store_true",
                    help="开启滚动上下文（默认关闭，实测它会诱发复读死循环）")
    args = ap.parse_args()

    if args.capture:
        capture(args.capture, args.seconds, Path(args.out))
        return
    if not args.audio:
        raise SystemExit("需要 --audio 或 --capture，见 --help")

    from app.asr import create_transcriber
    from app.hwdetect import recommend

    pcm = Path(args.audio).read_bytes()
    audio_sec = len(pcm) / BYTES_PER_SEC
    rec = recommend(backend=args.backend)
    model = args.model or rec["model"]
    print("音频 {:.0f} 秒 | 后端 {} | 模型 {} | 语言 {} | 滚动上下文 {}\n".format(
        audio_sec, rec["backend"], model, args.source,
        "开" if args.context else "关"))

    def make_transcriber():
        kw = {}
        if args.asr_temperature is not None:
            kw["temperature"] = args.asr_temperature
        return create_transcriber(rec["backend"], model, language=args.source,
                                  use_context=args.context, **kw)

    # 基准真值：用大窗口跑一遍，拿词级时间戳。大窗口的识别质量最好，
    # 用它当「主播到底说了什么、什么时候说的」的参照
    print("正在生成基准真值转写（大窗口 + 词级时间戳）…")
    ref_words = reference_words(pcm, args.source, model, rec["backend"])
    print("基准真值：{} 个词\n".format(len(ref_words)))

    rows = []
    for label, mn, mx, tr in CONFIGS:
        row = run_config(pcm, label, mn, mx, tr, make_transcriber, ref_words)
        rows.append(row)
        print("{:<18} 段数 {:>3} | 中位 {:>4.1f}s | RTF {:>4.2f} | 最慢ASR {:>4.1f}s | "
              "检测 P50 {:>5.1f}s P95 {:>5.1f}s | 召回 {:>5.1%}({}/{}) | 重复 {:>5.1%}".format(
                  row["label"], row["segments"], row["seg_p50"], row["rtf"],
                  row["asr_max"], row["detect_p50"] or 0, row["detect_p95"] or 0,
                  row["recall"], row["matched"], len(ref_words), row["dup"]))

    print("\n帕累托前沿（在召回不明显下降的前提下，检测延迟最低者胜）：")
    best_recall = max(r["recall"] for r in rows)
    # 排除病态配置：算力吃满、或文本重复过半（切太碎导致模型复读）
    ok = [r for r in rows if r["recall"] >= best_recall - 0.03
          and r["rtf"] < 0.75 and r["dup"] < 0.45]
    for r in sorted(ok, key=lambda r: r["detect_p50"] or 999):
        print("  {:<18} 检测 P50 {:>5.1f}s | RTF {:>4.2f} | 召回 {:>5.1%}".format(
            r["label"], r["detect_p50"] or 0, r["rtf"], r["recall"]))


def reference_words(pcm, language, model, backend):
    """基准真值：整段音频一次跑完 + 词级时间戳，用来定位每个词的实际发声时刻。

    必须与被测配置用**同一个模型和后端**——否则「召回率」测的是跨模型差异，
    而不是切段策略的影响。
    """
    import numpy as np

    from app.detector import normalize

    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    words = []
    if backend == "mlx":
        import mlx_whisper

        from app.asr import _MLX_REPOS
        out = mlx_whisper.transcribe(
            audio, path_or_hf_repo=_MLX_REPOS.get(model, model),
            language=language, word_timestamps=True, fp16=True)
        for seg in out.get("segments", []):
            for w in seg.get("words", []):
                token = normalize(w.get("word", ""))
                if token:
                    words.append((token, w.get("start", 0.0)))
    else:
        from faster_whisper import WhisperModel
        m = WhisperModel(model, device="auto", compute_type="auto")
        segs, _ = m.transcribe(audio, language=language, word_timestamps=True)
        for s in segs:
            for w in (s.words or []):
                token = normalize(w.word)
                if token:
                    words.append((token, w.start))
    return words


if __name__ == "__main__":
    main()
