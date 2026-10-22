"""只监听 127.0.0.1 的假直播 CDN（拉流 ffmpeg 的回归测试用：tests/test_ffmpeg_command.py、tests/test_audio_stop.py）。

照专家审查（2026-10-21）media-asr 实验里的 fake_cdn.py / fake_live.py 写的，只用标准库：

  * make_flv(ffmpeg, path, sec, video=True, claims_video=False)：用 lavfi 生成一段 FLV（AAC 音频，可选一路
    很小的画面）。claims_video=True：只有音频，但把 FLV 文件头的「有视频」位打开——就是「流头声称有视频、
    实际只有音频」的那种流（M3）。
  * FakeCDN：ThreadingHTTPServer 绑 127.0.0.1 的随机端口。url(name, **query) 认这几个参数：
      burst=S   开头 S 秒（媒体时间）一次发出，像 CDN 的 GOP 缓存；之后按 FLV 时间戳实时发。默认 2
      drop=S    发到媒体时间 S 秒就关连接（CDN 掐断，M1）
      stall=S   发到媒体时间 S 秒就不再发，连接一直开着，直到测试结束（半开、读卡住，M2）
      status=N  直接回 HTTP N（如 403），不发数据（M8：确认 4xx 判断不变）
      flv=1     名字不以 .flv 结尾也按直播 FLV 发（拿一个 .m3u8 结尾的地址让 ffmpeg 走 HLS 那组参数）
    直播 CDN 不认 Range：每次请求（包括 ffmpeg 的内部重连）都从一个新的 FLV 头开始发。每次请求记在
    requests 里；drop 的那一刻记在 dropped_at（time.monotonic）。
  * 不是 .flv 的文件（HLS 的 .m3u8、.ts）原样整份发。

不联网、不碰 TikTok；地址里不出现 tiktokcdn（直播闸认这几个字）。生成 FLV 要真 ffmpeg，没有就让调用方 skip。
"""
import http.server
import socketserver
import subprocess
import threading
import time
import urllib.parse

AUDIO_RATE = 44100
AAC_FRAME = 1024                # 一个 AAC 帧的采样数


def make_flv(ffmpeg, path, sec, video=True, claims_video=False):
    """生成一段 sec 秒的 FLV。音频是 440 Hz 正弦（足够响，过得了识别门限）；video=True 时带一路 160x120、
    10 fps 的画面（ffmpeg 自带的 flv1 编码器，哪份 ffmpeg 都有）。"""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-f", "lavfi", "-i", "sine=frequency=440:sample_rate={}:duration={}".format(AUDIO_RATE, sec)]
    if video:
        cmd += ["-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration={}".format(sec),
                "-c:v", "flv1", "-b:v", "200k"]
    cmd += ["-c:a", "aac", "-b:a", "64k", "-ar", str(AUDIO_RATE), "-ac", "1", "-f", "flv", str(path)]
    subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    if claims_video:
        data = bytearray(path.read_bytes())
        data[4] |= 0x01                          # 文件头的 TypeFlagsVideo
        path.write_bytes(bytes(data))
    return path


def flv_tags(data):
    """[(这个 tag 结束的字节偏移, 时间戳毫秒, 类型, 是不是一帧 AAC 音频)]。类型 8 音频、9 视频、18 脚本。"""
    off = int.from_bytes(data[5:9], "big") + 4   # 文件头 + PreviousTagSize0
    out = []
    while off + 11 <= len(data):
        kind = data[off] & 0x1F
        size = int.from_bytes(data[off + 1:off + 4], "big")
        ts = int.from_bytes(data[off + 4:off + 7], "big") | (data[off + 7] << 24)
        end = off + 11 + size + 4
        if end > len(data):
            break
        # AAC 音频 tag 的第二个字节：0 是 AudioSpecificConfig（不出声），1 是一帧音频
        aac_frame = kind == 8 and size >= 2 and data[off + 12] == 1
        out.append((end, ts, kind, aac_frame))
        off = end
    return out


def audio_sec_before(data, sec):
    """媒体时间 sec 秒之前（drop / stall 时已经发出去的）一共多少秒 AAC 音频。"""
    frames = sum(1 for _end, ts, _k, aac in flv_tags(data) if aac and ts < sec * 1000)
    return frames * AAC_FRAME / float(AUDIO_RATE)


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class FakeCDN:
    def __init__(self, files):
        self.files = dict(files)                 # 名字 -> bytes
        self.requests = []
        self.dropped_at = []
        self.closing = threading.Event()
        self._lock = threading.Lock()
        cdn = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                cdn._serve(self)

        self.httpd = _Server(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def url(self, name, **query):
        q = urllib.parse.urlencode(query)
        return "http://127.0.0.1:{}/{}{}".format(self.port, name, "?" + q if q else "")

    def close(self):
        self.closing.set()
        self.httpd.shutdown()
        self.httpd.server_close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _serve(self, h):
        u = urllib.parse.urlsplit(h.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        name = u.path.lstrip("/")
        with self._lock:
            self.requests.append({"name": name, "range": h.headers.get("Range"), "query": q})
        h.close_connection = True
        data = self.files.get(name)
        status = int(q.get("status", "200"))
        if data is None or status != 200:
            h.send_response(404 if data is None else status)
            h.send_header("Content-Length", "0")
            h.send_header("Connection", "close")
            h.end_headers()
            return
        live_flv = name.endswith(".flv") or q.get("flv") == "1"
        h.send_response(200)
        h.send_header("Content-Type", "video/x-flv" if live_flv else "application/octet-stream")
        h.send_header("Connection", "close")
        if not live_flv:
            h.send_header("Content-Length", str(len(data)))
            h.end_headers()
            h.wfile.write(data)
            return
        h.end_headers()                          # 直播 FLV：不给 Content-Length，ffmpeg 当成不可 seek 的流
        try:
            self._pace(h, data, q)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _pace(self, h, data, q):
        burst = float(q.get("burst", "2"))
        drop = float(q["drop"]) if "drop" in q else None
        stall = float(q["stall"]) if "stall" in q else None
        cut = drop if drop is not None else stall
        tags = flv_tags(data)
        first_tag = int.from_bytes(data[5:9], "big") + 4
        h.wfile.write(data[:first_tag])
        sent = first_tag
        t0 = time.monotonic()
        i = 0
        while i < len(tags) and not self.closing.is_set():
            end, ts, _kind, _aac = tags[i]
            if cut is not None and ts >= cut * 1000:
                break
            if ts / 1000.0 > burst + (time.monotonic() - t0):
                h.wfile.flush()
                time.sleep(0.01)
                continue
            h.wfile.write(data[sent:end])
            sent = end
            i += 1
        h.wfile.flush()
        if drop is not None:
            with self._lock:
                self.dropped_at.append(time.monotonic())
            return                               # 关连接：ffmpeg 读到流尾
        if stall is not None:
            self.closing.wait(120)               # 半开：什么都不发，连接一直开着
