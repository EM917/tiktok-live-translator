"""拉流 ffmpeg 的参数和 stderr（app/audio.py，2026-10-22，专家审查 M1、M3、M8）。

  M1  FLV 关掉 ffmpeg 的内部重连（-reconnect_streamed 0）：CDN 一断就退出，交给程序自己的重连。以前内部重连用
      同一个地址连上，拿到从新 FLV 头开始的一段流，跳读约 5 MB 才出声，聋 15–27 秒。HLS 不动。
  M3  FLV 设 -analyzeduration 3000000：流头声称有视频、实际只有音频时，以前最多攒 90 秒流时间才出声。
  M8  先探测 ffmpeg 认不认 -loglevel +level+warning，认才用；stderr 尾巴只收 error 级、和以前逐字节相同，
      「Will reconnect」只计数。
  TLT_FFMPEG_LEGACY=1 时三处一起回到改动前，逐字节相同。TLT_RECONNECT_V2=0（重连退回 V1）时只有 M1 退回：
  V1 每次重连都查流地址、没有上限，FLV 每断一次就退出的话每断一次就查一次（tests/test_m1_lookup_budget.py）。

前半部分是纯参数和纯文本，不起进程；后半部分用本机真 ffmpeg 对只监听 127.0.0.1 的假 CDN（tests/fake_flv_cdn.py），
没有 ffmpeg 就跳过。不联网、不碰 TikTok。
"""
import asyncio
import os
import re
import subprocess
import sys
import time
from collections import deque

import pytest

from app import audio, reconnect_policy
from app.audio import BYTES_PER_SEC, FRAME_BYTES, FFmpegAudioSource, StderrLines
from app.ffmpeg_bin import find_ffmpeg
from tests import fake_flv_cdn

FLV = "https://pull-f5-va01.example.invalid/stage/stream-7712345678901234567_or4.flv?expire=1761200000&sign=abc"
# 不以 .flv 结尾的渐进流地址（CDN 也会给这种），query 里碰巧有 .m3u8 也不算 HLS
FLV_NO_SUFFIX = "https://pull-q5-va01.example.invalid/game/stream-7712345678901234567_or4?vhost=x&f=a.m3u8"
HLS = "https://pull-hls-va01.example.invalid/stage/stream-7712345678901234567_or4/index.m3u8?expire=1"
MODEL = "/opt/models/bd.rnnn"


def old_command(ffmpeg, url, denoise=None):
    """2026-10-22 之前 FFmpegAudioSource.command() 的原样（照 9fa26b6 抄下来，不 import）。"""
    cmd = [ffmpeg, "-nostdin", "-loglevel", "error"]
    if url.startswith("http"):
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10",
                "-rw_timeout", "15000000"]
    cmd += ["-i", url, "-vn", "-ac", "1"]
    if denoise:
        cmd += ["-af", "highpass=f=70,arnndn=m={}".format(denoise)]
    cmd += ["-ar", "16000", "-f", "s16le", "pipe:1"]
    return cmd


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """每个用例从「没探测过、没开应急开关、重连用 V2」开始：开发机上设过 TLT_FFMPEG_LEGACY、TLT_RECONNECT_V2
    也不影响；V2 显式打开，不靠 RECONNECT_V2 的默认值（CLAUDE.md 第九条）。"""
    monkeypatch.delenv("TLT_FFMPEG_LEGACY", raising=False)
    monkeypatch.delenv("TLT_RECONNECT_V2", raising=False)
    monkeypatch.setattr(audio, "FFMPEG_LEGACY", False)
    monkeypatch.setattr(reconnect_policy, "RECONNECT_V2", True)
    monkeypatch.setattr(audio, "_probed", {})


def argv(url, ffmpeg="ffmpeg", denoise=None):
    return FFmpegAudioSource(url, denoise_model=denoise).command(ffmpeg)


def value(cmd, flag):
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


# ---- 参数 ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("url", [FLV, FLV_NO_SUFFIX])
def test_progressive_streams_exit_on_a_drop_and_cap_the_probe(url):
    cmd = argv(url)
    assert value(cmd, "-reconnect_streamed") == "0"
    assert value(cmd, "-reconnect") == "1"                  # 保留：连上之前的错误照样重试
    assert value(cmd, "-rw_timeout") == "15000000"
    assert value(cmd, "-analyzeduration") == "3000000"
    assert cmd.index("-analyzeduration") < cmd.index("-i")  # 是输入选项，必须在 -i 之前


def test_hls_keeps_the_old_reconnect_flags():
    cmd = argv(HLS)
    assert value(cmd, "-reconnect_streamed") == "1"
    assert "-analyzeduration" not in cmd
    assert cmd == old_command("ffmpeg", HLS)                # 没探测过：连 -loglevel 都和以前一样


@pytest.mark.parametrize("url", [FLV, FLV_NO_SUFFIX, HLS, "file:///tmp/x.flv"])
@pytest.mark.parametrize("denoise", [None, MODEL])
@pytest.mark.parametrize("v2", [True, False])
def test_legacy_switch_restores_the_old_argv_byte_for_byte(monkeypatch, url, denoise, v2):
    """TLT_FFMPEG_LEGACY=1：不管重连用哪套规则，命令都和改动前逐字节相同。"""
    audio._probed["ffmpeg"] = {"tagged": True, "version": "ffmpeg version 7.1"}
    monkeypatch.setattr(reconnect_policy, "RECONNECT_V2", v2)
    monkeypatch.setenv("TLT_FFMPEG_LEGACY", "1")
    assert argv(url, denoise=denoise) == old_command("ffmpeg", url, denoise)


@pytest.mark.parametrize("how", ["env", "module"])
@pytest.mark.parametrize("url", [FLV, FLV_NO_SUFFIX])
def test_v1_reconnect_rules_keep_ffmpegs_own_reconnect_for_flv(monkeypatch, how, url):
    """TLT_RECONNECT_V2=0（V1）：FLV 回到 ffmpeg 在进程里自己重连（-reconnect_streamed 1），只这一处；
    -analyzeduration 和带级别的日志照新的。V1 每次重连都查流地址、没有上限，ffmpeg 每断一次就退出的话，FLV 每断
    一次就查一次，第 5 次起每次先等 30 秒（tests/test_m1_lookup_budget.py 的 V1 用例）。"""
    audio._probed["ffmpeg"] = {"tagged": True, "version": "ffmpeg version 7.1"}
    v2 = argv(url)
    if how == "env":
        monkeypatch.setenv("TLT_RECONNECT_V2", "0")
    else:
        monkeypatch.setattr(reconnect_policy, "RECONNECT_V2", False)
    cmd = argv(url)
    assert value(cmd, "-reconnect_streamed") == "1"
    assert value(cmd, "-reconnect") == "1" and value(cmd, "-rw_timeout") == "15000000"
    assert value(cmd, "-analyzeduration") == audio.ANALYZE_US
    assert value(cmd, "-loglevel") == audio.LOGLEVEL_TAGGED
    # 和 V2 下的命令只差 -reconnect_streamed 的值
    assert len(cmd) == len(v2)
    assert [i for i, (a, b) in enumerate(zip(cmd, v2)) if a != b] == [cmd.index("-reconnect_streamed") + 1]


def test_v1_reconnect_rules_leave_hls_and_local_files_alone(monkeypatch):
    monkeypatch.setenv("TLT_RECONNECT_V2", "0")
    assert argv(HLS) == old_command("ffmpeg", HLS)
    assert "-reconnect" not in argv("file:///tmp/x.flv")


@pytest.mark.parametrize("env,expected", [
    ({}, True),
    ({"TLT_RECONNECT_V2": "0"}, False),
    ({"TLT_FFMPEG_LEGACY": "1"}, False),
    ({"TLT_RECONNECT_V2": "0", "TLT_FFMPEG_LEGACY": "1"}, False),
    ({"TLT_RECONNECT_V2": "1", "TLT_FFMPEG_LEGACY": "0"}, True)])
def test_either_switch_turns_ffmpegs_own_flv_reconnect_back_on(env, expected):
    assert audio.flv_exits_on_drop(env) is expected


def test_a_local_file_gets_no_network_flags():
    cmd = argv("file:///tmp/x.flv")
    assert "-reconnect" not in cmd and "-analyzeduration" not in cmd


@pytest.mark.parametrize("flag,expected", [
    ("1", True), ("on", True), ("TRUE", True), (" yes ", True),
    ("0", False), ("off", False), ("", False), ("whatever", False)])
def test_legacy_env_values(flag, expected):
    assert audio.ffmpeg_legacy({"TLT_FFMPEG_LEGACY": flag}) is expected


def test_legacy_default_follows_the_module_switch(monkeypatch):
    monkeypatch.setattr(audio, "FFMPEG_LEGACY", True)
    assert audio.ffmpeg_legacy({}) is True
    assert audio.ffmpeg_legacy({"TLT_FFMPEG_LEGACY": "0"}) is False


def test_is_hls_reads_the_path_only():
    assert audio.is_hls(HLS) and audio.is_hls("http://127.0.0.1:1/a/B.M3U8")
    assert not audio.is_hls(FLV) and not audio.is_hls(FLV_NO_SUFFIX)
    assert not audio.is_hls("http://[::1/x.m3u8")          # 解析不了的地址不算


@pytest.mark.parametrize("probed,level", [
    (None, "error"),                                          # 没探测过：不冒险
    ({"tagged": False, "version": "ffmpeg version 4.0"}, "error"),
    ({"tagged": True, "version": "ffmpeg version 7.1"}, "+level+warning")])
def test_the_log_level_follows_the_probe(probed, level):
    if probed is not None:
        audio._probed["/usr/bin/ffmpeg"] = probed
    assert value(argv(FLV, ffmpeg="/usr/bin/ffmpeg"), "-loglevel") == level
    assert value(argv(FLV, ffmpeg="/other/ffmpeg"), "-loglevel") == "error"   # 按路径缓存


# ---- 探测 -----------------------------------------------------------------------------------------

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="假 ffmpeg 是 sh 脚本")


def fake_ffmpeg(tmp_path, refuse_levels=False, hang_levels=False):
    """一个 sh 脚本冒充 ffmpeg：refuse_levels 时带级别标志就退出码 1（像不认它的老版本）；hang_levels 时带级别
    标志就一直不返回（exec sleep，超时由探测收掉）。每次被调用都往 calls 文件追加一行参数。"""
    calls = tmp_path / "calls"
    script = tmp_path / "ffmpeg"
    script.write_text(
        "#!/bin/sh\n"
        'echo "$*" >> "{calls}"\n'
        'case "$*" in *level+*) '
        'if [ "{refuse}" = 1 ]; then echo "Invalid loglevel" >&2; exit 1; fi; '
        'if [ "{hang}" = 1 ]; then exec sleep 30; fi;; esac\n'
        'echo "ffmpeg version 4.0-fake Copyright (c) 2000-2018"\n'
        'echo "built with a fake"\n'.format(calls=calls, refuse=1 if refuse_levels else 0,
                                           hang=1 if hang_levels else 0))
    script.chmod(0o755)
    return str(script), calls


@POSIX_ONLY
def test_an_old_ffmpeg_falls_back_to_plain_error_logging(tmp_path, capsys):
    ff, calls = fake_ffmpeg(tmp_path, refuse_levels=True)
    got = audio.probe_ffmpeg(ff)
    assert got == {"tagged": False, "version": "ffmpeg version 4.0-fake Copyright (c) 2000-2018"}
    assert value(argv(FLV, ffmpeg=ff), "-loglevel") == "error"
    out = capsys.readouterr().out                          # 终端说一句（中文），只写看到了什么
    assert "-loglevel +level+warning 时退出码 1" in out and "改用 -loglevel error" in out
    assert "不认" not in out                                # 不下结论（CLAUDE.md 第八条）
    assert audio.probe_ffmpeg(ff) is got                   # 缓存：不再起进程
    assert audio.ffmpeg_version(ff) == got["version"]
    assert calls.read_text().splitlines() == [
        "-hide_banner -loglevel +level+warning -version", "-hide_banner -version"]


@POSIX_ONLY
def test_a_current_ffmpeg_gets_tagged_logging_and_one_probe(tmp_path):
    ff, calls = fake_ffmpeg(tmp_path, refuse_levels=False)
    assert audio.ffmpeg_version(ff) == "ffmpeg version 4.0-fake Copyright (c) 2000-2018"
    assert value(argv(FLV, ffmpeg=ff), "-loglevel") == "+level+warning"
    assert len(calls.read_text().splitlines()) == 1


@POSIX_ONLY
def test_a_probe_that_times_out_is_not_run_a_second_time(tmp_path, monkeypatch, capsys):
    """带级别那次超时：不再跑不带级别的那次（以前最坏 2 × 5 秒，超过「起 ffmpeg」阶段的 10 秒预算）。
    终端只写「几秒内没有返回」，不说这份 ffmpeg 不认什么。"""
    monkeypatch.setattr(audio, "PROBE_TIMEOUT_SEC", 0.5)
    ff, calls = fake_ffmpeg(tmp_path, hang_levels=True)
    assert audio.probe_ffmpeg(ff) == {"tagged": False, "version": None}
    assert calls.read_text().splitlines() == ["-hide_banner -loglevel +level+warning -version"]
    out = capsys.readouterr().out
    assert "0.5 秒内没有返回" in out and "改用 -loglevel error" in out and "不认" not in out
    assert value(argv(FLV, ffmpeg=ff), "-loglevel") == "error"


def test_a_missing_binary_probes_as_plain_and_only_once(tmp_path, monkeypatch, capsys):
    """起不来（文件不存在）：一次就停，不再跑第二次；终端写「没能启动」。"""
    ff = str(tmp_path / "no-such-ffmpeg")
    runs = []
    real_run = subprocess.run

    def counting_run(cmd, **kw):
        runs.append(cmd)
        return real_run(cmd, **kw)
    monkeypatch.setattr(audio.subprocess, "run", counting_run)
    assert audio.probe_ffmpeg(ff) == {"tagged": False, "version": None}
    assert len(runs) == 1
    out = capsys.readouterr().out
    assert "没能启动" in out and "改用 -loglevel error" in out and "不认" not in out
    assert audio.ffmpeg_version(ff) is None
    assert value(argv(FLV, ffmpeg=ff), "-loglevel") == "error"


def test_ffmpeg_version_without_ffmpeg(monkeypatch):
    monkeypatch.setattr(audio, "find_ffmpeg", lambda: None)
    assert audio.ffmpeg_version() is None


def _spawn_recorder(src, seen):
    async def spawn(cmd):
        seen.append(cmd)
        raise RuntimeError("stop-here")
    src._spawn = spawn


@pytest.mark.parametrize("legacy", [False, True])
def test_frames_probes_once_before_the_first_spawn(monkeypatch, legacy):
    """第一次起 ffmpeg 之前探测一次（放进线程），之后读缓存；应急开关开着时不探测。"""
    if legacy:
        monkeypatch.setenv("TLT_FFMPEG_LEGACY", "1")
    monkeypatch.setattr(audio, "find_ffmpeg", lambda: "/opt/ff/ffmpeg")
    probes = []

    def probe(ff):
        probes.append(ff)
        audio._probed[ff] = {"tagged": True, "version": "ffmpeg version 7.1"}
        return audio._probed[ff]
    monkeypatch.setattr(audio, "probe_ffmpeg", probe)
    seen = []

    async def two_rounds():
        for _ in range(2):
            src = FFmpegAudioSource(FLV)
            _spawn_recorder(src, seen)
            with pytest.raises(RuntimeError, match="stop-here"):
                async for _f in src.frames():
                    pass
    asyncio.run(two_rounds())
    assert probes == ([] if legacy else ["/opt/ff/ffmpeg"])
    levels = [value(c, "-loglevel") for c in seen]
    assert levels == (["error"] * 2 if legacy else ["+level+warning"] * 2)


# ---- stderr 分拣 -----------------------------------------------------------------------------------

def tail_of(lines):
    tail = deque()
    s = StderrLines(tail)
    for line in lines:
        s.feed(line.strip())
    s.close()
    return list(tail), s.reconnects


# 同一段输出，-loglevel error 时 ffmpeg 打的（PLAIN）和 +level+warning 时打的（TAGGED）。照两份真 ffmpeg 的格式写：
# 级别标签在上下文前缀之后；「Last message repeated」没有标签
PLAIN = [
    "[http @ 0x1] Stream ends prematurely at 306997, should be 18446744073709551615",
    "[flv @ 0x2] Packet mismatch 1 2 3",
    "    Last message repeated 2 times",
    "[in#0/flv @ 0x3] Error during demuxing: Input/output error",
    "Error opening input files: Server returned 403 Forbidden (access denied)",
]
TAGGED = [
    "[http @ 0x1] [error] Stream ends prematurely at 306997, should be 18446744073709551615",
    "[http @ 0x1] [warning] Will reconnect at 306997 in 0 second(s), error=Input/output error.",
    "[flv @ 0x2] [error] Packet mismatch 1 2 3",
    "    Last message repeated 2 times",
    "[http @ 0x1] [warning] HTTP error 403 Forbidden",
    "[in#0/flv @ 0x3] [error] Error during demuxing: Input/output error",
    "[aac @ 0x4] [warning] Queue input is backward in time",
    "[fatal] Error opening input files: Server returned 403 Forbidden (access denied)",
]


def test_the_tail_keeps_error_lines_byte_for_byte_and_counts_reconnects():
    plain, n_plain = tail_of(PLAIN)
    tagged, n_tagged = tail_of(TAGGED)
    assert tagged == plain == [x.strip() for x in PLAIN]
    assert (n_plain, n_tagged) == (0, 1)


def test_untagged_output_passes_through_like_before():
    """ffmpeg 不认级别标志、或者 TLT_FFMPEG_LEGACY=1：一行一行原样收，和改动前的 _drain_stderr 一样。"""
    lines = ["[http @ 0x1] Will reconnect at 1 in 0 second(s)", "    Last message repeated 1 times",
             "x", "x"]
    assert tail_of(lines) == ([x.strip() for x in lines], 0)


def test_repeats_split_by_warnings_are_folded_back_like_plain_error_logging():
    """-loglevel error 时中间的 warning 不打，同一条报错前后两次会被 ffmpeg 折成「Last message repeated」；
    现在 warning 也打了、ffmpeg 不折了，分拣时照样折回去，在下一条不同的报错之前、或流结束时写出来。"""
    e = "[flv @ 0x2] [error] Unable to seek to the next packet"
    w = "[http @ 0x1] [warning] Will reconnect at 9 in 0 second(s), error=End of file."
    tagged = [e, w, e, "    Last message repeated 1 times", w, e, "[error] Something else", e, w, e]
    tail, reconnects = tail_of(tagged)
    assert tail == ["[flv @ 0x2] Unable to seek to the next packet",
                    "Last message repeated 3 times",
                    "Something else",
                    "[flv @ 0x2] Unable to seek to the next packet",
                    "Last message repeated 1 times"]
    assert reconnects == 3


def test_a_repeated_reconnect_warning_counts_every_time():
    tail, reconnects = tail_of(["[http @ 0x1] [warning] Will reconnect at 0 in 1 second(s)",
                                "    Last message repeated 4 times",
                                "[error] done"])
    assert reconnects == 5 and tail == ["done"]


def test_a_continuation_line_keeps_its_message_level():
    tail, _ = tail_of(["[x @ 0x1] [warning] first half", "second half of the warning",
                       "[y @ 0x2] [error] first", "second half of the error"])
    assert tail == ["[y @ 0x2] first", "second half of the error"]


# ---- 真 ffmpeg ---------------------------------------------------------------------------------------

FFMPEG = find_ffmpeg()
NEEDS_FFMPEG = pytest.mark.skipif(FFMPEG is None, reason="本机没有 ffmpeg")
_PTR = re.compile(r"@ (?:0x)?[0-9a-fA-F]+\]")


def normalize(tail):
    """指针地址每次运行都不同，比较前换掉。"""
    return _PTR.sub("@ P]", tail)


def run_ffmpeg_log(args, loglevel):
    r = subprocess.run([FFMPEG, "-nostdin", "-loglevel", loglevel] + args, capture_output=True,
                       timeout=60)
    return r.stderr.decode(errors="replace").split("\n")


@NEEDS_FFMPEG
def test_real_ffmpeg_accepts_the_tagged_level_and_reports_its_version():
    got = audio.probe_ffmpeg(FFMPEG)
    assert got["tagged"] is True
    assert got["version"].startswith("ffmpeg version ")


@NEEDS_FFMPEG
def test_real_ffmpeg_tagged_and_plain_tails_match(tmp_path):
    """同一段坏音频解码、再编码：解码报错（error）和编码告警（warning）交错。两种 -loglevel 下各跑一遍，
    分拣出来的整条尾巴（不只最后 8 行）去掉指针之后逐字节相同。

    另外钉住 LOGLEVEL_TAGGED 前面的「+」：它让 ffmpeg 自己的重复行折叠照旧开着，原始 stderr 里有
    「Last message repeated」。不带「+」的绝对写法会关掉折叠——尾巴靠 StderrLines 折回去仍然一样，差在原始
    stderr 的行数（这段输入实测 brew 9.0.1：64 行对 180 行；imageio 7.1：47 行对 165 行）。"""
    good = tmp_path / "s.aac"
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "lavfi",
                    "-i", "sine=d=4", "-c:a", "aac", "-b:a", "64k", "-f", "adts", str(good)],
                   check=True, capture_output=True, timeout=60)
    data = bytearray(good.read_bytes())
    for off in range(3000, len(data) - 1000, 2500):       # 固定的「坏」字节，结果可复现
        for i in range(off, off + 300):
            data[i] = (i * 7919 + off) % 251
    bad = tmp_path / "bad.aac"
    bad.write_bytes(bytes(data))
    args = ["-i", str(bad), "-af", "asetpts=0", "-c:a", "aac", "-f", "null", "-"]
    plain, _ = tail_of([x for x in run_ffmpeg_log(args, audio.LOGLEVEL_PLAIN) if x.strip()])
    raw_tagged = [x for x in run_ffmpeg_log(args, audio.LOGLEVEL_TAGGED) if x.strip()]
    tagged, _ = tail_of(raw_tagged)
    assert plain                                           # 确实有报错可比
    assert [normalize(x) for x in tagged] == [normalize(x) for x in plain]
    assert any("Last message repeated" in x for x in raw_tagged)   # ffmpeg 自己的折叠还开着


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    if FFMPEG is None:
        pytest.skip("本机没有 ffmpeg")
    d = tmp_path_factory.mktemp("flv")
    av = fake_flv_cdn.make_flv(FFMPEG, d / "av.flv", 12, video=True).read_bytes()
    aonly = fake_flv_cdn.make_flv(FFMPEG, d / "aonly_claims.flv", 12, video=False,
                                  claims_video=True).read_bytes()
    with fake_flv_cdn.FakeCDN({"live/av.flv": av, "live/aonly_claims.flv": aonly}) as cdn:
        yield cdn


async def pull(src, limit=None):
    """跑 frames()：第一帧用了多久、一共几帧、生成器结束的时刻（monotonic）。"""
    t0 = time.monotonic()
    first = None
    n = 0
    async for _frame in src.frames():
        if first is None:
            first = time.monotonic() - t0
        n += 1
        if limit and n >= limit:
            break
    return {"first": first, "frames": n, "end": time.monotonic()}


@NEEDS_FFMPEG
def test_a_cdn_drop_ends_ffmpeg_within_3s_and_keeps_every_second_before_it(media):
    """M1：CDN 在媒体时间 4 秒处掐断连接。ffmpeg 自己退出（stop_path=exited），不再在进程里重连；
    断开前收到的音频全部从 stdout 吐出来、交出去。改动前 ffmpeg 会在进程内重连、十几秒不出声。"""
    before = len(media.requests)
    src = FFmpegAudioSource(media.url("live/av.flv", burst=2, drop=4))
    got = asyncio.run(asyncio.wait_for(pull(src), 30))
    assert len(media.requests) == before + 1               # 没有内部重连
    assert got["end"] - media.dropped_at[-1] < 3.0
    assert src.stop_facts()["stop_path"] == "exited"
    sent = fake_flv_cdn.audio_sec_before(media.files["live/av.flv"], 4)
    assert abs(got["frames"] * FRAME_BYTES / BYTES_PER_SEC - sent) <= 0.15
    assert "ffmpeg_reconnects" not in src.stop_facts()


# 首帧的上限：实测值的 3 倍以上，CI 高负载下也稳；又远小于改回旧参数时的样子（见下面的 docstring）
FIRST_FRAME_MAX_SEC = 5.0


@NEEDS_FFMPEG
def test_an_audio_only_stream_that_claims_video_starts_promptly(media):
    """M3：流头说有视频、其实只有音频。改动前用 ffmpeg 默认的分析上限（总体 5 秒，FLV 单路最多 90 秒）：审查报告
    里这种假流 22 秒内 0 PCM，加了 -analyzeduration 3000000 之后首帧 1.20 秒。本批在本机假 CDN 上实测
    （brew 9.0.1 / imageio 7.1）：首帧 1.13 / 1.14 秒；改前 20.0 秒（文件发完才出）/ 25 秒内没有。
    这里的假流 12 秒（开头 2 秒一次发、之后实时发），改回旧参数时首帧要等文件发完或者根本不出（本批去掉
    -analyzeduration 还原验证，brew 9.0.1 上 10.07 秒），远超 FIRST_FRAME_MAX_SEC。正常音视频流（flv1 10 fps）
    改前 2.07 秒、改后 1.06 / 1.05 秒：只防明显变慢。"""
    src = FFmpegAudioSource(media.url("live/aonly_claims.flv", burst=2))
    got = asyncio.run(asyncio.wait_for(pull(src, limit=1), 30))
    assert got["first"] is not None and got["first"] < FIRST_FRAME_MAX_SEC
    src = FFmpegAudioSource(media.url("live/av.flv", burst=2))
    got = asyncio.run(asyncio.wait_for(pull(src, limit=1), 30))
    assert got["first"] is not None and got["first"] < FIRST_FRAME_MAX_SEC


@NEEDS_FFMPEG
def test_a_403_still_reads_as_dead_and_the_tail_matches_plain_logging(media, monkeypatch):
    """M8：带级别的日志下，CDN 回 403 时 stderr 尾巴（去掉指针）和 -loglevel error 时逐字节相同，
    V2 据此判「原地址明确拉不动」（pipeline_stream._round_facts 找 HTTP 4xx）照旧。"""
    from app.pipeline_stream import StreamWatchMixin

    def round_403():
        src = FFmpegAudioSource(media.url("live/av.flv", status=403))
        asyncio.run(asyncio.wait_for(pull(src), 30))
        return src
    new = round_403()
    assert audio._probed[FFMPEG]["tagged"] is True         # 真的走了带级别的日志
    monkeypatch.setenv("TLT_FFMPEG_LEGACY", "1")
    old = round_403()
    assert "403" in new.stderr_tail()
    assert normalize(new.stderr_tail()) == normalize(old.stderr_tail())
    rnd = {"t0": time.monotonic() - 60.0}                  # 不是「起来没几秒就退」：只靠 4xx 判断
    facts = StreamWatchMixin._round_facts(None, rnd, False, new, new.stderr_tail())
    assert facts["dead"] is True


@NEEDS_FFMPEG
def test_ffmpegs_own_reconnects_are_counted_and_kept_out_of_the_tail(media):
    """M8：内部重连还开着的那组参数（HLS 用的：地址以 .m3u8 结尾），CDN 每次在媒体 3 秒处掐断。ffmpeg 自己重连，
    每次打一行 warning 级的「Will reconnect」——数进 ffmpeg_reconnects、随 stop_facts 进 stream_break，
    不进尾巴。（这里发的其实是 FLV，只是借 .m3u8 的地址让命令走 HLS 那组参数。）"""
    media.files["live/av_as.m3u8"] = media.files["live/av.flv"]
    src = FFmpegAudioSource(media.url("live/av_as.m3u8", flv=1, burst=2, drop=3))
    assert value(src.command(FFMPEG), "-reconnect_streamed") == "1"

    async def go():
        try:
            await asyncio.wait_for(pull(src), 6.0)
        except asyncio.TimeoutError:
            pass                                       # 一直在进程里重连、不会自己结束：到点收掉
    asyncio.run(go())
    assert src.ffmpeg_reconnects >= 1
    assert src.stop_facts()["ffmpeg_reconnects"] == src.ffmpeg_reconnects
    assert "Will reconnect" not in src.stderr_tail()


@NEEDS_FFMPEG
def test_hls_still_plays_with_its_old_flags(tmp_path, monkeypatch):
    """HLS 维持原参数（内部重连开着、不设 -analyzeduration），照常出声。"""
    out = tmp_path / "hls"
    out.mkdir()
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=6", "-c:a", "aac", "-b:a", "64k",
                    "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod",
                    str(out / "index.m3u8")], check=True, capture_output=True, timeout=60)
    files = {"hls/" + p.name: p.read_bytes() for p in out.iterdir()}
    with fake_flv_cdn.FakeCDN(files) as cdn:
        src = FFmpegAudioSource(cdn.url("hls/index.m3u8"))
        assert value(src.command(FFMPEG), "-reconnect_streamed") == "1"
        got = asyncio.run(asyncio.wait_for(pull(src), 30))
    assert abs(got["frames"] / 10.0 - 6.0) <= 0.3
    assert src.stop_facts()["stop_path"] == "exited"
    assert os.path.basename(cdn.requests[0]["name"]) == "index.m3u8"
