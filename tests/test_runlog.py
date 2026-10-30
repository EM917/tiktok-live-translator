"""运行日志（app/runlog.py）和它落盘前的去密钥（app/redact.scrub_secrets）。

双击 .app 启动时终端输出接在 /dev/null 上，现在另抄一份到 ~/Library/Logs/TikTokLiveTranslator/run.log。
这里钉住四件事：
  * 运行日志里搜不到假密钥、假 cookie、假口令——不管是按名字写出来的、在请求头里的、在 URL 里的，还是一个
    裸值；原来的流一个字节不变；
  * 按大小轮转生效，几个进程写同一个文件也不丢行；
  * 只在 macOS、没有终端时装，Windows、Linux 照旧；
  * main.py 在护栏之后、第一次可能打印之前装它（读语法树，不 import main）。

假密钥都在运行时拼出来：源码里不出现任何一把「长得像真的」的密钥，免得推送时被密钥扫描当成泄露拦下。
全部在 tmp_path 里，不碰真实的 ~/Library/Logs。
"""
import ast
import io
import os
import random
import string
import subprocess
import sys
import textwrap
import threading
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import runlog
from app.redact import MASK, scrub_secrets, strip_query

ROOT = Path(__file__).resolve().parent.parent
HEX = "0123456789abcdef"
URLSAFE = string.ascii_letters + string.digits + "-_"
ALNUM = string.ascii_letters + string.digits


def _rand(rng, n, alphabet):
    return "".join(rng.choice(alphabet) for _ in range(n))


def _mixed(rng, n, alphabet=URLSAFE):
    """大写、小写、数字都有的随机串（真实的 token_urlsafe 几乎总是这样）。"""
    while True:
        s = _rand(rng, n, alphabet)
        if any(c.isupper() for c in s) and any(c.islower() for c in s) and any(c.isdigit() for c in s):
            return s


def _uuid(rng):
    return "-".join(_rand(rng, n, HEX) for n in (8, 4, 4, 4, 12))


def _fakes():
    rng = random.Random(20261029)
    return {
        "anthropic": "sk-" + "ant-" + "api03-" + _mixed(rng, 80),
        "openai": "sk-" + "proj-" + _mixed(rng, 48, ALNUM),
        "deepl_free": _uuid(rng) + ":fx",
        "deepl_pro": _uuid(rng),
        "google": "AI" + "za" + _mixed(rng, 35),
        "github": "gh" + "p_" + _mixed(rng, 36, ALNUM),
        "viewer": _mixed(rng, 43),
        "viewer2": _mixed(rng, 43),
        "control": _rand(rng, 64, HEX),
        "sessionid": _rand(rng, 32, HEX),
        "sid_tt": _rand(rng, 32, HEX),
        "mstoken": _mixed(rng, 124),
        "ttwid_tail": _mixed(rng, 43),
        "jwt": "ey" + "J" + _mixed(rng, 20) + "." + _mixed(rng, 30) + "." + _mixed(rng, 40),
        "password": "fake-pass-" + _mixed(rng, 12, ALNUM),
        "csrf": _mixed(rng, 40),
        "workspace": "wrk" + "spc_" + _mixed(rng, 24, ALNUM),
    }


F = _fakes()

# 每一行都带至少一把假密钥：形状各不相同，也覆盖它们在这个程序里真会出现的样子（请求头、字典、异常原文、
# 设置文件的整份 repr、手机同看的链接、命令行、JSON 里转义过的引号、裸值）
SECRET_LINES = [
    "Authorization: Bearer {control}",
    "{{'Authorization': 'Bearer {control}', 'Content-Type': 'application/json'}}",
    'headers={{"x-api-key": "{anthropic}", "anthropic-version": "2023-06-01"}}',
    "Authorization: DeepL-Auth-Key {deepl_free}",
    "[警告] DeepL 翻译失败: 403, message='Forbidden', url=URL('https://api.deepl.com/v2/translate'), "
    "headers=<CIMultiDictProxy('Authorization': 'DeepL-Auth-Key {deepl_pro}')>",
    "settings = {{'api_keys': {{'DEEPL_API_KEY': '{deepl_pro}', 'OPENAI_API_KEY': '{openai}', "
    "'ANTHROPIC_API_KEY': '{anthropic}'}}, 'viewer_token': '{viewer}', 'target_lang': 'zh-CN'}}",
    "ANTHROPIC_API_KEY={anthropic} OPENAI_API_KEY={openai}",
    "Cookie: sessionid={sessionid}; sid_tt={sid_tt}; msToken={mstoken}; ttwid=1%7C{ttwid_tail}%7C1730000000",
    "Set-Cookie: sessionid_ss={sessionid}; Path=/; Domain=.tiktok.com; HttpOnly",
    "cookie 头里有 sessionid={sessionid};sid_guard={sid_tt}%7C1730000000",
    "[同看] 已打开：http://192.168.1.23:8766/#k={viewer}",
    "[警告] 解析失败：https://www.tiktok.com/api/live/detail/?aid=1988&key={google}&msToken={mstoken}",
    "控制密钥读出来是 {control}",
    "手机同看口令 {viewer2}",
    "上次的 sessionid 是 {sessionid}",
    '{{\\"sessionid\\": \\"{sessionid}\\", \\"tt_csrf_token\\": \\"{csrf}\\"}}',
    "代理 https://someone:{password}@proxy.example.com:8080 连不上",
    "GITHUB_TOKEN={github} 以及裸的 {github}",
    "id_token {jwt}",
    "password={password} passwd: '{password}'",
    "Google 密钥 {google} 被拒",
    "OpenAI 回 401：Incorrect API key provided: {openai}",
    "x-tt-params: csrf_token={csrf}&session_id={sessionid}",
    "Traceback 里的局部变量 token = '{control}'",
    "headers={{'x-api-key': '{anthropic}', 'anthropic-workspace-id': '{workspace}'}}",
    "settings = {{'api_keys': {{'ANTHROPIC_WORKSPACE_ID': '{workspace}'}}}}",
    "工作区 ID {workspace} 填进去了",
]


def _filled():
    return [line.format(**F) for line in SECRET_LINES]


def _leaks(text, secret, window=10):
    """secret 里任何连续 window 个字符出现在 text 里，就算漏了（不只查整把，半截也算）。"""
    return [secret[i:i + window] for i in range(0, len(secret) - window + 1)
            if secret[i:i + window] in text]


def _assert_no_secret(text):
    for name, secret in F.items():
        assert not _leaks(text, secret), (name, _leaks(text, secret)[:3], text[:400])


# ---- scrub_secrets ---------------------------------------------------------------------------

@pytest.mark.parametrize("line", _filled(), ids=range(len(SECRET_LINES)))
def test_each_shape_of_secret_is_scrubbed(line):
    out = scrub_secrets(line)
    _assert_no_secret(out)
    # 按名字、形状抹的换成 MASK；URL 里 query、fragment 带的整段去掉（strip_query），不留记号
    assert MASK in out or out == strip_query(line)


def test_scrubbing_twice_is_the_same_as_once():
    for line in _filled():
        once = scrub_secrets(line)
        assert scrub_secrets(once) == once


SURVIVORS = [
    "字幕界面已启动: http://127.0.0.1:8765",
    "[信息] 端口 8765 被占用，已自动改用 8766",
    '  File "/Users/someone/Desktop/tiktok-live-translator/app/pipeline.py", line 1645, in _run_stream',
    "[警告] 加载模型 mlx-community/whisper-large-v3-mlx 失败：OSError",
    "large-v3-turbo / hymt2-7b / Qwen2.5-7B-Instruct / gemma3:12b",
    "已更新到 0123456789abcdef0123456789abcdef01234567（v0.20.0）",
    "[弹幕] 字段：累计 120 条里 nickname 120、user_id 0",
    "max_tokens=1024 token_cap=112 cookies_browser_only=safari --cookies-browser auto",
    "HTTP 429 · code 4003110 · 接口不给流地址",
    "¿Cuánto cuesta? ¡Oferta por tiempo limitado!",
    "aiohttp.client_exceptions.ClientConnectorError: Cannot connect to host 127.0.0.1:9334 "
    "ssl:default [Connect call failed ('127.0.0.1', 9334)]",
    "test_the_files_that_run_before_the_version_check_still_parse_as_python_39",
    "能读取，没有 TikTok 登录 cookie（sessionid 等）",
    "[信息] ffmpeg 输出: [flv @ 0x7f8a1c00] Packet mismatch 123456 4567 789",
    "/Users/someone/Library/Caches/huggingface/hub/models--mlx-community--whisper-large-v3-mlx",
    "TikTokLiveTranslator · TikTok Live Translator · @主播甲",
    "======== 2026-10-29 13:26:32 进程 4242 开始 · Python 3.13.1",
]


@pytest.mark.parametrize("line", SURVIVORS)
def test_ordinary_lines_pass_through_untouched(line):
    assert scrub_secrets(line) == line


def test_urls_keep_only_scheme_host_and_path_like_strip_query():
    line = "https://pull-flv-l1.tiktokcdn.com/stage/stream-1.flv?expire=1790000000&sign=SECRETSIGN"
    assert scrub_secrets(line) == strip_query(line) == "https://pull-flv-l1.tiktokcdn.com/stage/stream-1.flv"


@pytest.mark.parametrize("prefix", ["拉流失败", "地址", "url_", "8"])
def test_a_url_glued_to_the_word_before_it_still_loses_its_query(prefix):
    """中文、数字、下划线紧贴着协议名时（「拉流失败https://…」）中间没有词边界：以前协议名前要求 \\b，
    这种地址的签名、代理口令原样留下。"""
    signed = prefix + "https://pull-flv-l1.tiktokcdn.com/stage/stream-1.flv?expire=1790000000&sign=SECRETSIGN"
    assert scrub_secrets(signed) == strip_query(signed) == \
        prefix + "https://pull-flv-l1.tiktokcdn.com/stage/stream-1.flv"
    proxy = prefix + "https://someone:" + F["password"] + "@proxy.example.com:8080"
    _assert_no_secret(scrub_secrets(proxy))


# 一长串字母、数字和 . - + 连在一起，中间没有空白：每个字母前都是词边界
LONG_RUNS = {"dash": "x-" * 32000, "dot": "a." * 32000, "mixed": "ab-cd." * 11000, "version": "v1.2-rc+" * 8000}


@pytest.mark.parametrize("name", sorted(LONG_RUNS))
def test_a_long_run_of_short_words_is_scrubbed_in_linear_time(name):
    """每一行都在打印它的线程里、拿着运行日志的锁过 scrub_secrets，别的线程（包括事件循环）的 print 都等着。
    协议名写成不设上限的 \\b[a-z][a-z0-9+.-]*:// 时，这种串里每个词边界都往后扫到串尾再退回来找 ://，耗时是
    行长的平方：64 KB 的一行要十秒左右（2026-10-29 本机实测）。设了上限以后是几十毫秒。"""
    text = LONG_RUNS[name]
    started = time.perf_counter()
    assert scrub_secrets(text) == text
    assert strip_query(text) == text
    assert time.perf_counter() - started < 1.0


def test_none_and_non_strings():
    assert scrub_secrets(None) == ""
    assert scrub_secrets(42) == "42"


# ---- 运行日志：装上、抄进去、搜不到 --------------------------------------------------------------

@pytest.fixture
def clean_runlog():
    yield
    runlog.uninstall()


def _target():
    return SimpleNamespace(stdout=io.StringIO(), stderr=io.StringIO())


def _log_text(directory):
    parts = []
    for path in sorted(Path(directory).glob(runlog.FILE_NAME + "*")):
        parts.append(path.read_text(encoding="utf-8"))
    return "".join(parts)


def test_the_run_log_has_none_of_the_fake_secrets_and_the_terminal_is_unchanged(tmp_path, clean_runlog):
    target = _target()
    runlog.install(tmp_path, root=ROOT, target=target)
    lines = _filled()
    for line in lines:
        print(line, file=target.stdout)
    # 一把密钥被拆在几次 write 里（print 本来就是先写正文、再写换行）：凑满一行才去密钥
    target.stdout.write("Cookie: sessionid=")
    target.stdout.write(F["sessionid"][:10])
    target.stdout.write(F["sessionid"][10:] + "; sid_tt=" + F["sid_tt"])
    target.stdout.write("\n")
    # 异常的整段 traceback 走标准错误
    try:
        raise RuntimeError("401 Unauthorized, headers={'Authorization': 'Bearer " + F["control"] + "'}")
    except RuntimeError:
        traceback.print_exc(file=target.stderr)
    text = _log_text(tmp_path)
    _assert_no_secret(text)
    assert text.count(MASK) >= len(lines) - 2       # 两行只靠 strip_query 去掉了 query、fragment
    assert "RuntimeError" in text and "Traceback (most recent call last)" in text
    # 原来的流一个字节不变：终端里看到的还是原文（终端在用户自己眼前，不归运行日志管）
    assert target.stdout.inner.getvalue().splitlines()[:len(lines)] == lines
    assert F["control"] in target.stderr.inner.getvalue()


def test_print_through_the_real_sys_streams(tmp_path, clean_runlog, monkeypatch):
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    runlog.install(tmp_path, root=ROOT)
    assert isinstance(sys.stdout, runlog.Tee) and sys.stdout.inner is out
    print("[信息] 一键更新前正在监听，已自动恢复")
    print("[警告] 带着 viewer_token=" + F["viewer"], file=sys.stderr)
    runlog.uninstall()
    assert sys.stdout is out and sys.stderr is err
    text = _log_text(tmp_path)
    assert "[信息] 一键更新前正在监听，已自动恢复" in text
    assert "viewer_token=" + MASK in text
    _assert_no_secret(text)
    assert out.getvalue() == "[信息] 一键更新前正在监听，已自动恢复\n"


def test_each_line_is_on_disk_as_soon_as_it_ends(tmp_path, clean_runlog):
    """main.py 的很多出口是 os._exit，exec 也不冲缓冲：整行写完就要已经在文件里，不等 flush、不等 close。"""
    target = _target()
    runlog.install(tmp_path, root=ROOT, target=target)
    print("[信息] 第一行", file=target.stdout)
    assert "[信息] 第一行" in (tmp_path / runlog.FILE_NAME).read_text(encoding="utf-8")
    target.stdout.write("还没换行")
    assert "还没换行" not in (tmp_path / runlog.FILE_NAME).read_text(encoding="utf-8")
    runlog.uninstall()     # 换回原来的流时，半行写出去
    assert "还没换行" in (tmp_path / runlog.FILE_NAME).read_text(encoding="utf-8")


def test_header_and_line_format(tmp_path, clean_runlog, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["main.py", "https://www.tiktok.com/@demo/live?sessionid=" + F["sessionid"],
                                      "--port", "8790"])
    target = _target()
    runlog.install(tmp_path, root=ROOT, target=target)
    print("你好", file=target.stdout)
    first, second = (tmp_path / runlog.FILE_NAME).read_text(encoding="utf-8").splitlines()[:2]
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip().splitlines()[0]
    pid = "[{}]".format(os.getpid())
    assert pid in first and "进程 {} 开始".format(os.getpid()) in first
    # 程序目录也过去密钥：放在带 UUID 的临时目录里（比如会话的 scratchpad）时那一段会被抹掉
    assert "版本 " + version in first and "程序目录 " + scrub_secrets(str(ROOT)) in first
    assert "https://www.tiktok.com/@demo/live --port 8790" in first
    _assert_no_secret(first)
    # 「2026-10-29 13:26:32.123 [4242] 你好」
    stamp, rest = second[:23], second[24:]
    assert stamp[4] == "-" and stamp[10] == " " and stamp[19] == "."
    assert rest == pid + " 你好"


def test_a_carriage_return_keeps_only_the_last_version(tmp_path, clean_runlog):
    target = _target()
    runlog.install(tmp_path, root=ROOT, target=target)
    for pct in range(0, 101):
        target.stderr.write("\r下载 {}%".format(pct))
    assert len(target.stderr.pending()) < 20      # 进度条不换行也不越攒越长
    target.stderr.write("\n")
    target.stdout.write("Windows 换行\r\n")
    lines = (tmp_path / runlog.FILE_NAME).read_text(encoding="utf-8").splitlines()[1:]
    assert [line.split("] ", 1)[1] for line in lines] == ["下载 100%", "Windows 换行"]


def test_a_huge_line_is_cut_after_scrubbing(tmp_path, clean_runlog):
    target = _target()
    runlog.install(tmp_path, root=ROOT, target=target)
    print("x" * (runlog.LINE_MAX + 500) + " Bearer " + F["control"], file=target.stdout)
    text = _log_text(tmp_path)
    assert "后面 " in text and "个字没抄" in text
    _assert_no_secret(text)


def test_threads_printing_at_once_keep_every_line_whole(tmp_path, clean_runlog):
    """print 分两次写（正文、换行）：半行按线程分开攒，别的线程插进来也拼不到一行里。"""
    target = _target()
    runlog.install(tmp_path, root=ROOT, target=target)

    def spam(n):
        for i in range(200):
            print("线程{}-第{}行-".format(n, i) + "字" * 40, file=target.stdout)

    threads = [threading.Thread(target=spam, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    body = [line.split("] ", 1)[1] for line in _log_text(tmp_path).splitlines()[1:]]
    want = {"线程{}-第{}行-".format(n, i) + "字" * 40 for n in range(8) for i in range(200)}
    assert set(body) == want and len(body) == len(want)


def test_a_failing_log_never_touches_the_terminal_and_says_how_many_lines_it_missed(tmp_path):
    class Flaky:
        def __init__(self):
            self.fail = True
            self.data = b""

        def write(self, data):
            if self.fail:
                raise OSError(28, "No space left on device")
            self.data += data

        def close(self):
            pass

    sink = Flaky()
    log = runlog.RunLog(sink)
    inner = io.StringIO()
    tee = runlog.Tee(inner, log)
    tee.write("第一行\n")                           # 写不进去：不抛，终端照写
    assert inner.getvalue() == "第一行\n" and log.dropped == 1
    sink.fail = False
    assert log.write_line("第二行", now=log._retry_at - 1) is False     # 隔一会儿再试，期间只计数
    assert log.dropped == 2
    assert log.write_line("第三行", now=log._retry_at + 1) is True
    text = sink.data.decode("utf-8")
    assert "（前面有 2 行没写进运行日志）" in text and text.rstrip().endswith("第三行")
    assert log.dropped == 0


def test_the_tee_hands_everything_else_to_the_original_stream():
    inner = io.StringIO()
    tee = runlog.Tee(inner, runlog.RunLog(SimpleNamespace(write=lambda d: None, close=lambda: None)))
    assert tee.isatty() is False
    assert tee.getvalue() == ""                 # 没定义的属性交给原来的流
    with pytest.raises(TypeError):
        tee.write(b"bytes")                      # 原来的流怎么报错，这里照样报
    tee.flush()


# ---- 轮转 ------------------------------------------------------------------------------------

@pytest.mark.skipif(os.name == "nt", reason="运行日志只在 macOS 上装；轮转靠 POSIX 的改名和 inode")
def test_rotation_keeps_at_most_the_configured_number_of_backups(tmp_path, clean_runlog):
    target = _target()
    runlog.install(tmp_path, root=ROOT, target=target, max_bytes=4096, backups=3)
    for i in range(400):
        print("第{:04d}行 ".format(i) + "-" * 60, file=target.stdout)
    names = sorted(p.name for p in tmp_path.glob(runlog.FILE_NAME + "*"))
    assert names == ["run.log", "run.log.1", "run.log.2", "run.log.3"]
    for name in names:
        assert (tmp_path / name).stat().st_size <= 4096
    newest = (tmp_path / "run.log").read_text(encoding="utf-8")
    assert "第0399行" in newest
    everything = _log_text(tmp_path)
    assert "第0000行" not in everything          # 最老的一份已经轮转掉了
    oldest_kept = (tmp_path / "run.log.3").read_text(encoding="utf-8")
    assert oldest_kept and "第0399行" not in oldest_kept


@pytest.mark.skipif(os.name == "nt", reason="运行日志只在 macOS 上装；Windows 没有这套权限位")
def test_file_and_directory_are_private(tmp_path, clean_runlog):
    directory = tmp_path / "Logs" / "TikTokLiveTranslator"
    runlog.install(directory, root=ROOT, target=_target())
    assert (directory / runlog.FILE_NAME).stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(os.name == "nt", reason="同一个文件被另一个进程改名：只在 POSIX 上有这种事")
def test_a_writer_follows_the_file_after_another_writer_rotated_it(tmp_path):
    path = tmp_path / runlog.FILE_NAME
    a = runlog.RotatingFile(path, max_bytes=200, backups=5)
    b = runlog.RotatingFile(path, max_bytes=200, backups=5)
    b.write(b"b-before\n")
    for i in range(10):
        a.write("a-{:02d} {}\n".format(i, "-" * 40).encode())
    assert runlog.RotatingFile(path).backup_path(1).exists()
    b.write(b"b-after\n")                        # 不该写进已经被 a 改名成 run.log.1 的旧文件
    assert "b-after" in path.read_text(encoding="utf-8")
    a.close()
    b.close()


@pytest.mark.skipif(os.name == "nt", reason="轮转锁用 fcntl.flock，只在 POSIX 上有")
def test_a_held_rotation_lock_never_blocks_the_print(tmp_path):
    """别的进程拿着轮转锁不放（停在调试器里）：这次不轮转、接着写这一份，不能卡住打印——print 跑在事件循环里。"""
    import fcntl

    path = tmp_path / runlog.FILE_NAME
    f = runlog.RotatingFile(path, max_bytes=100, backups=3)
    f.write(b"x" * 90 + b"\n")
    holder = os.open(str(tmp_path / runlog.LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        started = time.monotonic()
        f.write(b"over the limit\n")
        f.write(b"still no rotation\n")          # 隔 RETRY_SEC 才再试，这一行不再等锁
        assert time.monotonic() - started < 1.0
        assert not f.backup_path(1).exists()
        assert path.read_bytes().endswith(b"over the limit\nstill no rotation\n")
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)
    f._rotate_after = 0.0
    f.write(b"after release\n")
    assert f.backup_path(1).exists() and path.read_bytes() == b"after release\n"
    f.close()


WRITER = textwrap.dedent("""
    import sys
    sys.path.insert(0, {root!r})
    from app.runlog import RotatingFile
    f = RotatingFile({path!r}, max_bytes=2000, backups=200)
    for i in range(300):
        f.write("{{}}-{{:03d}} {{}}\\n".format(sys.argv[1], i, "." * 30).encode())
    f.close()
""")


@pytest.mark.skipif(os.name == "nt", reason="同一个文件被另一个进程改名：只在 POSIX 上有这种事")
def test_two_processes_rotating_the_same_file_lose_no_lines(tmp_path):
    path = tmp_path / runlog.FILE_NAME
    script = tmp_path / "writer.py"
    script.write_text(WRITER.format(root=str(ROOT), path=str(path)), encoding="utf-8")
    procs = [subprocess.Popen([sys.executable, str(script), name]) for name in ("A", "B")]
    for p in procs:
        assert p.wait(60) == 0
    lines = _log_text(tmp_path).splitlines()
    assert sorted(lines) == sorted("{}-{:03d} {}".format(n, i, "." * 30) for n in "AB" for i in range(300))


# ---- 只在 macOS、没有终端时装 -----------------------------------------------------------------

def test_the_directory_is_under_library_logs_on_macos_only(tmp_path):
    assert runlog.log_dir(platform="darwin", environ={}, home=tmp_path) == \
        tmp_path / "Library" / "Logs" / "TikTokLiveTranslator"
    assert runlog.log_dir(platform="darwin", environ={"TLT_RUN_LOG_DIR": "/x/y"}) == Path("/x/y")
    assert runlog.log_dir(platform="darwin", environ={"TLT_RUN_LOG_DIR": " "}) is None
    for other in ("win32", "linux"):            # Windows 走 Start.bat 的黑窗口，Linux 从终端启动：照旧
        assert runlog.log_dir(platform=other, environ={}) is None
        assert runlog.log_dir(platform=other, environ={"TLT_RUN_LOG_DIR": str(tmp_path)}) is None


class _Tty(io.StringIO):
    def isatty(self):
        return True


def test_start_does_nothing_with_a_terminal_or_off_macos(tmp_path, clean_runlog, monkeypatch):
    env = {"TLT_RUN_LOG_DIR": str(tmp_path)}
    monkeypatch.setattr(sys, "stdout", _Tty())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    assert runlog.start(ROOT, platform="darwin", environ=env) is None     # 有终端：终端里看得到
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    assert runlog.start(ROOT, platform="linux", environ=env) is None
    assert runlog.start(ROOT, platform="win32", environ=env) is None
    assert runlog.start(ROOT, platform="darwin", environ={"TLT_RUN_LOG_DIR": ""}) is None
    assert list(tmp_path.iterdir()) == []


def test_start_installs_once_on_macos_without_a_terminal(tmp_path, clean_runlog, monkeypatch):
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    env = {"TLT_RUN_LOG_DIR": str(tmp_path)}
    log = runlog.start(ROOT, platform="darwin", environ=env)
    assert log is not None and runlog.active() is log
    assert runlog.start(ROOT, platform="darwin", environ=env) is log      # 同一个进程只装一次
    assert not isinstance(sys.stdout.inner, runlog.Tee)
    print("[信息] 字幕界面已启动")
    assert "[信息] 字幕界面已启动" in (tmp_path / runlog.FILE_NAME).read_text(encoding="utf-8")


def test_start_never_raises(tmp_path, clean_runlog, monkeypatch):
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())

    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(runlog, "install", boom)
    assert runlog.start(ROOT, platform="darwin", environ={"TLT_RUN_LOG_DIR": str(tmp_path)}) is None


def test_the_suite_never_writes_the_real_run_log():
    """conftest 把 TLT_RUN_LOG_DIR 指到了临时目录：测试里起的子进程也继承它。"""
    assert os.environ.get("TLT_RUN_LOG_DIR")
    home_logs = Path.home() / "Library" / "Logs" / "TikTokLiveTranslator"
    assert Path(os.environ["TLT_RUN_LOG_DIR"]) != home_logs


# ---- main.py 的接线（不能 import main，读语法树）----------------------------------------------------

def _top_level_call_line(tree, name):
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == name:
                return node.lineno
    return None


def test_main_starts_the_run_log_right_after_the_guard_and_before_anything_prints():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert "from app.runlog import start as start_run_log" in source
    harden = _top_level_call_line(tree, "harden_stdio")
    start = _top_level_call_line(tree, "start_run_log")
    raise_ = _top_level_call_line(tree, "raise_nofile_limit")
    ensure = _top_level_call_line(tree, "ensure_env")
    assert None not in (harden, start, raise_, ensure)
    assert harden < start < raise_ < ensure
    between = [n for n in tree.body if harden < n.lineno < start]
    assert between == [], "护栏和运行日志之间不该再有别的语句"
