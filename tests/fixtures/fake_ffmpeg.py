"""假 ffmpeg：不联网、不解码，只模仿 ffmpeg 在管道两头的行为（tests/test_audio_stop.py 用）。

照 asr-bench/live-stability/repro/fake_ffmpeg/ 写的：10-18 那种永久卡住，在真 ffmpeg 上是「探测阶段把
音频攒在手里、收到 SIGTERM 才一次写出来、之后不退」，这里用几行 Python 造出同样的管道行为。

  python -I fake_ffmpeg.py <mode> [N]

mode:
  probe_flush N  stdout 什么都不写；收到 SIGTERM 后一次写出 N 字节，然后挂住不退（要 kill）
  stream         每 0.1 秒往 stdout 写 3200 字节；不理 SIGTERM（要 kill）
  term_exit      每 0.1 秒写 3200 字节；收到 SIGTERM 立刻退出
  eof N          写 N 字节后正常退出（流自然结束）
  eof_linger N   写 N 字节、关掉 stdout，然后不退（SIGTERM 照默认处理：收到就结束）
  exit_now       什么都不写，立刻以退出码 1 退出
  long_stderr N  先往 stderr 写一行 N 字节、不带换行，然后每 0.01 秒写一行短报错；stdout 不写；
                 不理 SIGTERM（要 kill）
"""
import os
import signal
import sys
import time

CHUNK = b"\x01" * 3200


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        k = os.write(fd, view[:65536])
        view = view[k:]


def main():
    mode = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    if mode == "probe_flush":
        def on_term(_sig, _frame):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            _write_all(1, b"\x02" * n)
            while True:
                time.sleep(3600)
        signal.signal(signal.SIGTERM, on_term)
        while True:
            time.sleep(3600)
    elif mode == "stream":
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True:
            _write_all(1, CHUNK)
            time.sleep(0.1)
    elif mode == "term_exit":
        signal.signal(signal.SIGTERM, lambda *_a: os._exit(0))
        while True:
            _write_all(1, CHUNK)
            time.sleep(0.1)
    elif mode == "eof":
        _write_all(1, b"\x03" * n)
        sys.exit(0)
    elif mode == "eof_linger":
        _write_all(1, b"\x03" * n)
        os.close(1)
        while True:
            time.sleep(3600)
    elif mode == "exit_now":
        sys.exit(1)
    elif mode == "long_stderr":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        _write_all(2, b"x" * n)
        while True:
            _write_all(2, b"[in#0/flv @ 0x0] Packet mismatch 1 2 3\n")
            time.sleep(0.01)
    else:
        sys.exit(2)


if __name__ == "__main__":
    main()
