"""G10：英文模式下扫整页 DOM，不许剩下中文（spec §12.3）。无头 Chrome，opt-in。

默认跳过：只有设了 TLT_DOM_SCAN=1、而且找得到 Chrome 才跑——CI 的 dom-scan job（macOS 跑器，
自带 Chrome 和 PingFang / SF Pro，版面余量才可信）。**直播转写进行中的机器上不要设 TLT_DOM_SCAN**：
无头 Chrome 是一整个浏览器进程。这里一次只开一个，跑完连同它的辅助进程一起关掉；它只打开
本测试自己起在 127.0.0.1 上的假页面，别的主机名一律解析失败（--host-resolver-rules）。

装置：stdlib 的 ThreadingHTTPServer 端一份 web/ 的副本。桌面页照 inject_lang 注入语言，在
i18n.js 之前插入场景（scenario.js）和 fake_ws.js，在 app.js 之后插入 scan.js；手机页插在
viewer.js 的前后（`?v=` 查询串与 /static/、/v/ 路径照原样）。场景串行：先把「当前场景」交给
服务器，再起 Chrome；--dump-dom 出来的 DOM 里取 #i18n-scan 的 JSON 断言。页面语言由这个服务器
自己注入，不依赖发布闸。防闪探针（i18n_dom/probe.js）另在页面脚本之前和 scan.js 之前各插一次。
手机场景不用 --window-size（macOS 的新无头模式窄不过约 500px），改经 DevTools 协议把视口定成
390 宽，再从页面里取同样的 DOM（run_chrome_cdp）。

TLT_DOM_SHOTS=<目录> 时同一批场景各出一张 PNG，英文 README 截图从这里来：桌面另用 --screenshot
再开一次，手机在同一次页面上经 DevTools 截（2 倍像素）。

文件末尾几条用例不开 Chrome，平时照常跑：场景组得起来、插入点还在、默认确实跳过、到点没结果时
重试一次。页面结构或场景一变普通 CI 就会红，不用等 dom-scan job。收紧闸（Z0）起 dom-scan 整组
严格跑：英文页上剩下中文、顶栏放不下，PR 都是红的。
"""
import asyncio
import base64
import contextlib
import itertools
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
import pytest

from app import i18n
from tests.i18n_dom import scenarios as S

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
FIXTURE_DIR = Path(__file__).resolve().parent / "i18n_dom"
FIXTURES = ("fake_ws.js", "scan.js", "probe.js")
CHROME_NAMES = ("google-chrome", "chromium", "chromium-browser",
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
CHROME_TIMEOUT_SEC = 60
# 一次 Chrome 到点（CHROME_TIMEOUT_SEC）还没给出结果时，换一个新的配置目录再起一次，最多这么多次。
# 只对「到点没结果」重试（ChromeNoResult）：Chrome 自己退出了没给结果、页面上的任何断言都不重试。
# 依据：PR #68 的 CI（macOS 跑器）里 live-full@1100 的 --dump-dom 起来之后 60 秒一个字节都没往
# stdout 写，stderr 只有每次启动都有的那几行，被杀掉判红；那一轮其余三十多次启动都正常出了结果，
# 同一场景、同一宽度在 PR #69 的 CI 里也正常出了结果（只是版面断言没过，L1 修的就是它）——是偶发，
# 与 1100 这个宽度无关。
# Z0 起整个 dom-scan 严格跑，偶发一次就会让 PR 红；真的每次都卡住的话两次都到点，照样红。
# 重试过的在 pytest 的 warnings 摘要里留一条，连同当时已经端出去的资源，下回好对照。
CHROME_RETRIES = 1
TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".png": "image/png", ".json": "application/json"}
EN_SCENARIOS = S.DESKTOP_EN + S.PHONE_EN


def find_chrome(environ=None):
    """依次找 TLT_CHROME、google-chrome、chromium、chromium-browser、macOS 的 Google Chrome。"""
    env = os.environ if environ is None else environ
    names = ([env["TLT_CHROME"]] if env.get("TLT_CHROME") else []) + list(CHROME_NAMES)
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def skip_reason(environ=None):
    env = os.environ if environ is None else environ
    if env.get("TLT_DOM_SCAN") != "1":
        return "G10 默认不跑：设 TLT_DOM_SCAN=1 才开无头 Chrome（直播转写进行中的机器上不要设）"
    if find_chrome(env) is None:
        return "设了 TLT_DOM_SCAN=1，但找不到 Chrome（TLT_CHROME、google-chrome、chromium…）"
    return None


SKIP = skip_reason()
needs_chrome = pytest.mark.skipif(SKIP is not None, reason=SKIP or "")
# 版面余量只在 macOS 的字体下可信：ubuntu 上 Chrome 回退到 DejaVu / Liberation（spec §12.3 第 7 条）
needs_mac = pytest.mark.skipif(sys.platform != "darwin", reason="版面断言只在 macOS 字体下成立")


# ---- 拼页面 ------------------------------------------------------------------------------

def _script_tag(src):
    return '<script src="{}"></script>'.format(src)


def _only_tag(html, src):
    """页面里 src 那个外部脚本的整个 <script> 标签（带不带 ?v= 都认）。找不到或不止一个就报错：
    插入点悄悄失效的话，扫描会在一个没装替身的页面上空转。"""
    tags = re.findall(r'<script src="{}(?:\?[^"]*)?"></script>'.format(re.escape(src)), html)
    if len(tags) != 1:
        raise AssertionError("页面里 {} 的 <script> 应恰好一个，实际 {} 个".format(src, len(tags)))
    return tags[0]


def page_html(scenario, web_dir=WEB_DIR):
    """服务器在 / 上端的页面：注入语言，再在页面脚本前后插入替身。别的字节一个不动。"""
    desktop = scenario["page"] == "desktop"
    name, base, first, last = (("index.html", "/static/", "i18n.js", "app.js") if desktop
                               else ("viewer.html", "/v/", "viewer.js", "viewer.js"))
    html = (Path(web_dir) / name).read_text(encoding="utf-8")
    if html.count('<html lang="zh-CN">') != 1:       # inject_lang 靠它；没有就会静默不注入
        raise AssertionError('{} 里 <html lang="zh-CN"> 应恰好一个'.format(name))
    html = i18n.inject_lang(html, scenario["page_lang"], marker=not desktop)
    before = "".join(_script_tag(base + n) for n in ("probe.js", "scenario.js", "fake_ws.js"))
    after = "".join(_script_tag(base + n) for n in ("probe.js", "scan.js"))
    head = _only_tag(html, base + first)
    html = html.replace(head, before + head, 1)
    tail = _only_tag(html, base + last)
    return html.replace(tail, tail + after, 1)


def scenario_js(scenario):
    """window.__SCENARIO，外加开页前预置的 localStorage（手机上手动选过的语言）。"""
    lines = ["window.__SCENARIO = {};".format(json.dumps(scenario, ensure_ascii=False))]
    for key, value in sorted(scenario.get("storage", {}).items()):
        lines.append("try {{ localStorage.setItem({}, {}); }} catch (e) {{}}".format(
            json.dumps(key), json.dumps(value)))
    return "\n".join(lines) + "\n"


def dump_json(dom, node_id):
    """--dump-dom 里 <script type="application/json" id=…> 的内容（脚本内容序列化时不转义）。"""
    m = re.search(r'<script[^>]*\bid="{}"[^>]*>(.*?)</script>'.format(re.escape(node_id)), dom, re.S)
    return json.loads(m.group(1)) if m else None


# ---- 假页面服务器 --------------------------------------------------------------------------

class _Site(ThreadingHTTPServer):
    daemon_threads = True
    # Chrome 同时开好几条连接取脚本，socketserver 默认的 backlog 只有 5。连接被拒时页面缺一块
    # 照样跑完、扫描结果就是假的，所以放宽，另外还逐个核对资源真的端出去过（_Runner.scan）
    request_queue_size = 64

    def __init__(self, web_dir):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.web_dir = Path(web_dir)
        self.scenario = None
        self.served = []            # (路径, 状态码)：每个场景开页前清空

    def assets(self, html):
        """页面引用的脚本和样式（连 ?v=），每一个都必须真的端出去过。"""
        return set(re.findall(r'<(?:script src|link rel="stylesheet" href)="(/[^"]+)"', html))

    def resolve(self, raw_path):
        path = urlsplit(raw_path).path
        if path == "/":
            return page_html(self.scenario, self.web_dir).encode("utf-8"), TYPES[".html"]
        base = next((b for b in ("/static/", "/v/") if path.startswith(b)), None)
        if base is None:
            return None
        name = path[len(base):]
        if name == "scenario.js":
            return scenario_js(self.scenario).encode("utf-8"), TYPES[".js"]
        if name in FIXTURES:
            return (FIXTURE_DIR / name).read_bytes(), TYPES[".js"]
        if not name or "/" in name or "\\" in name or name.startswith("."):
            return None
        target = self.web_dir / name
        if not target.is_file():
            return None
        return target.read_bytes(), TYPES.get(target.suffix, "application/octet-stream")


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        hit = self.server.resolve(self.path)
        self.server.served.append((self.path, 404 if hit is None else 200))
        if hit is None:
            self.send_error(404)
            return
        body, ctype = hit
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


# ---- Chrome ------------------------------------------------------------------------------

def _kill_group(proc):
    """连同渲染、GPU、网络这些辅助进程一起关：POSIX 上 Chrome 起在自己的进程组里，
    Windows 上按进程树关。"""
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):     # 整组都已经退出了
        pass
    if proc.poll() is None:
        proc.kill()


def _drain(stream, into):
    for chunk in iter(lambda: os.read(stream.fileno(), 65536), b""):
        into.extend(chunk)


def dumped(out, err):
    """--dump-dom 的结果齐了：整个 <html>…</html> 已经写出来。"""
    return out.rstrip().endswith(b"</html>")


def shot_written(out, err):
    """--screenshot 的结果齐了：Chrome 在 stderr 上报「N bytes written to file …」。"""
    return b"written to file" in err


def _chrome_argv(chrome, size, lang, profile):
    """两种跑法共用的参数。--use-mock-keychain：macOS 上新建的配置目录不去碰钥匙串，免得跑测试时
    弹出系统对话框。"""
    return [chrome, "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", "--disable-extensions",
            "--disable-background-networking", "--disable-sync", "--use-mock-keychain",
            "--user-data-dir={}".format(profile),
            "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
            "--lang={}".format("en-US" if lang == "en" else "zh-CN"),
            "--window-size={},{}".format(*size)]


@contextlib.contextmanager
def _chrome(argv):
    """起一个无头 Chrome，边跑边收它的 stdout / stderr（交出两个一直在长的 bytearray）；
    出了 with 就收掉整个进程组，读线程也收完。"""
    group = {"start_new_session": True} if os.name == "posix" else {}
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **group)
    out, err = bytearray(), bytearray()
    readers = [threading.Thread(target=_drain, args=(proc.stdout, out), daemon=True),
               threading.Thread(target=_drain, args=(proc.stderr, err), daemon=True)]
    for reader in readers:
        reader.start()
    try:
        yield proc, out, err
    finally:
        _kill_group(proc)
        proc.wait()
        for reader in readers:
            reader.join(5)
        proc.stdout.close()
        proc.stderr.close()


def _tail(err):
    return bytes(err).decode("utf-8", "replace")[-800:]


class ChromeNoResult(AssertionError):
    """Chrome 到点（CHROME_TIMEOUT_SEC）还没给出结果，已经杀掉整个进程组。_Runner 只对它重试。"""


def run_chrome(chrome, url, size, lang, profile, extra, finished):
    """起一个无头 Chrome，等到 finished(stdout, stderr) 成立（结果齐了）或它自己退出，然后收掉
    整个进程组。不能用 subprocess.run 等它自己退：2026-09-29 本机实测 macOS 上的 Chrome 154
    --dump-dom 把 DOM 写完之后 25 秒还不退出。60 秒还没齐就杀掉整组并判失败。"""
    argv = _chrome_argv(chrome, size, lang, profile) + ["--virtual-time-budget=4000"]
    argv += list(extra) + [url]
    deadline = time.monotonic() + CHROME_TIMEOUT_SEC
    timed_out = False
    with _chrome(argv) as (proc, out, err):
        while not finished(bytes(out), bytes(err)) and proc.poll() is None:
            if time.monotonic() > deadline:
                timed_out = True
                break
            time.sleep(0.1)
    if not finished(bytes(out), bytes(err)):
        if timed_out:
            raise ChromeNoResult("Chrome {} 秒没有给出结果，已杀掉整个进程组：{}\n{}".format(
                CHROME_TIMEOUT_SEC, url, _tail(err)))
        raise AssertionError("Chrome 没给出结果就退出了（退出码 {}）：{}\n{}".format(
            proc.returncode, url, _tail(err)))
    return bytes(out).decode("utf-8", "replace")


# ---- 手机场景：经 DevTools 协议定视口 ---------------------------------------------------------
# --window-size 在 macOS 的新无头模式里窄不过约 500px：要 390 宽，window.innerWidth 仍是 500
# 上下，页面按 500 排版，--screenshot 却只截左边 390px——右边的按钮、折行前的半句话被裁掉，
# 状态胶囊「不出屏」的断言也是拿 500 在量（PR #69 的 dom-shots/phone-*.png 就是这样）。
# Emulation.setDeviceMetricsOverride 直接定 CSS 视口，不受窗口最小宽度限制。

DEVTOOLS_LINE = re.compile(rb"DevTools listening on (ws://\S+)")
PHONE_SCALE = 2          # 手机截图按 2 倍像素出，README 用得上；量出来的 CSS 像素不受影响
READY_JS = 'document.getElementById("i18n-scan") ? document.documentElement.outerHTML : ""'


def devtools_url(err, profile):
    """--remote-debugging-port=0 时 Chrome 自己挑端口：stderr 上报「DevTools listening on ws://…」，
    同时写进配置目录的 DevToolsActivePort（端口、路径各一行）。哪个先有用哪个；都还没有就 None。"""
    m = DEVTOOLS_LINE.search(bytes(err))
    if m:
        return m.group(1).decode("ascii", "replace")
    try:
        lines = (Path(profile) / "DevToolsActivePort").read_text(encoding="utf-8").split()
    except OSError:
        return None
    if len(lines) >= 2 and lines[0].isdigit():          # 文件可能还只写了一半
        return "ws://127.0.0.1:{}{}".format(lines[0], lines[1])
    return None


class CDPError(Exception):
    """DevTools 回了 error（方法名、错误体）。"""


class _CDP:
    """最小的 DevTools 协议客户端：一次一条命令，等同 id 的回复；中间来的事件一律略过。"""

    def __init__(self, ws):
        self.ws, self.last = ws, 0

    async def call(self, method, params=None, session=None):
        self.last += 1
        msg = {"id": self.last, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        await self.ws.send_str(json.dumps(msg))
        while True:
            frame = await self.ws.receive()
            if frame.type != aiohttp.WSMsgType.TEXT:
                raise AssertionError("DevTools 连接断了（{}）：{}".format(frame.type, method))
            reply = json.loads(frame.data)
            if reply.get("id") == self.last:
                if "error" in reply:
                    raise CDPError(method, reply["error"])
                return reply.get("result", {})


async def cdp_scan(ws_url, url, size, deadline, shot=False):
    """新开一个标签页，把 CSS 视口定成 size，打开 url，等 scan.js 写出 #i18n-scan，交回
    (整页 DOM, PNG 字节或 None)。DOM 取的是 documentElement.outerHTML，跟 --dump-dom 一样是
    <html>…</html>。mobile 取 False：移动模式会套用 Chrome 安卓的缩放规则，内容溢出时可能把整页
    缩小、视口跟着变宽，量出来的就不是 390 了；桌面模式下版面实打实 390 宽，溢出的元素照样溢出。
    同看页没有按设备分支的样式或脚本（只有深浅色），两种模式排出来一样。"""
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(ws_url, max_msg_size=0) as ws:
            cdp = _CDP(ws)
            target = (await cdp.call("Target.createTarget", {"url": "about:blank"}))["targetId"]
            session = (await cdp.call("Target.attachToTarget",
                                      {"targetId": target, "flatten": True}))["sessionId"]
            await cdp.call("Page.enable", session=session)
            await cdp.call("Emulation.setDeviceMetricsOverride", {
                "width": size[0], "height": size[1], "deviceScaleFactor": PHONE_SCALE,
                "mobile": False}, session=session)
            await cdp.call("Page.navigate", {"url": url}, session=session)
            while True:
                try:
                    got = await cdp.call("Runtime.evaluate", {
                        "expression": READY_JS, "returnByValue": True}, session=session)
                    dom = got.get("result", {}).get("value")
                except CDPError:          # 导航途中旧的执行上下文没了：下一轮再问
                    dom = None
                if dom:
                    break
                if time.monotonic() > deadline:
                    raise ChromeNoResult("{} 秒内没等到 #i18n-scan：{}".format(CHROME_TIMEOUT_SEC, url))
                await asyncio.sleep(0.1)
            png = None
            if shot:
                data = (await cdp.call("Page.captureScreenshot", {"format": "png"},
                                       session=session))["data"]
                png = base64.b64decode(data)
            return dom, png


def run_chrome_cdp(chrome, url, size, lang, profile, shot=None):
    """手机场景的跑法：Chrome 开着调试端口起在 about:blank，经 DevTools 定视口、开页面、等扫描。
    交回整页 DOM；shot 是路径时同一次页面顺手截图写进去。60 秒没完就杀掉整组并判失败。
    --hide-scrollbars：滚动条不占版面宽度，390 就是 390。"""
    argv = _chrome_argv(chrome, size, lang, profile) + [
        "--hide-scrollbars", "--remote-debugging-port=0", "about:blank"]
    deadline = time.monotonic() + CHROME_TIMEOUT_SEC
    with _chrome(argv) as (proc, out, err):
        ws_url = devtools_url(err, profile)
        while ws_url is None:
            if proc.poll() is not None:
                raise AssertionError("Chrome 没报出 DevTools 地址就退出了（退出码 {}）：{}\n{}".format(
                    proc.poll(), url, _tail(err)))
            if time.monotonic() > deadline:
                raise ChromeNoResult("Chrome {} 秒没报出 DevTools 地址，已杀掉整个进程组：{}\n{}".format(
                    CHROME_TIMEOUT_SEC, url, _tail(err)))
            time.sleep(0.1)
            ws_url = devtools_url(err, profile)
        try:
            dom, png = asyncio.run(asyncio.wait_for(
                cdp_scan(ws_url, url, size, deadline, shot is not None),
                timeout=max(1.0, deadline - time.monotonic())))
        except asyncio.TimeoutError:
            raise ChromeNoResult("Chrome {} 秒没有给出结果，已杀掉整个进程组：{}\n{}".format(
                CHROME_TIMEOUT_SEC, url, _tail(err))) from None
    if shot is not None:
        Path(shot).write_bytes(png)
    return dom


class _Runner:
    """每个场景只开一次 Chrome（到点没结果时重试一次，见 CHROME_RETRIES），结果缓存给同一模块里的
    各条断言。"""

    def __init__(self, site, chrome, scenarios, tmp, shots=None):
        self.site, self.chrome, self.scenarios, self.tmp = site, chrome, scenarios, tmp
        self.shots = Path(shots) if shots else None
        self._done = {}
        self._dirs = itertools.count()          # 每次起 Chrome 都用一个新的配置目录

    def _launch(self, name, start):
        """start(配置目录) 起一次 Chrome。到点没给出结果（ChromeNoResult）时换一个新配置目录再起，
        最多 CHROME_RETRIES 次（理由见 CHROME_RETRIES）；别的失败原样抛。"""
        for attempt in range(CHROME_RETRIES + 1):
            self.site.served.clear()
            try:
                return start(self.tmp / "profile-{}".format(next(self._dirs)))
            except ChromeNoResult as exc:
                if attempt == CHROME_RETRIES:
                    raise
                served = sorted(path for path, status in self.site.served if status == 200)
                warnings.warn("G10 {}：{}。当时已端出去 {} 个资源：{}。换一个配置目录重试".format(
                    name, str(exc).split("\n", 1)[0], len(served), served), stacklevel=2)

    def _url(self, scenario):
        url = "http://127.0.0.1:{}/".format(self.site.server_address[1])
        return url + ("#k=" + S.FAKE_TOKEN if scenario["page"] == "phone" else "")

    def _shot(self, name):
        if self.shots is None:
            return None
        self.shots.mkdir(parents=True, exist_ok=True)
        return self.shots / "{}.png".format(name)

    def scan(self, name):
        if name not in self._done:
            sc = self.site.scenario = self.scenarios[name]
            url, shot = self._url(sc), self._shot(name)
            if sc["page"] == "phone":
                dom = self._launch(name, lambda profile: run_chrome_cdp(
                    self.chrome, url, sc["size"], sc["lang"], profile, shot))
            else:
                dom = self._launch(name, lambda profile: run_chrome(
                    self.chrome, url, sc["size"], sc["lang"], profile, ["--dump-dom"], dumped))
            ok = {path for path, status in self.site.served if status == 200}
            missing = sorted(self.site.assets(page_html(sc, self.site.web_dir)) - ok)
            if missing:           # 装置自己的问题，不是界面的：页面缺了脚本，结果不可信
                raise AssertionError("{}：这些脚本/样式没有端出去 {}".format(name, missing))
            result = dump_json(dom, "i18n-scan")
            if result is None:
                raise AssertionError("{}：DOM 里没有 #i18n-scan，scan.js 没跑到。开头：{}".format(
                    name, dom[:300]))
            result["probe"] = dump_json(dom, "i18n-probe")
            result["dom"] = dom
            if shot is not None and sc["page"] == "desktop":
                self._launch(name, lambda profile: run_chrome(
                    self.chrome, url, sc["size"], sc["lang"], profile,
                    ["--hide-scrollbars", "--screenshot={}".format(shot)], shot_written))
            self._done[name] = result
        return self._done[name]


@pytest.fixture(scope="module")
def dom(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("i18n_dom")
    web = tmp / "web"
    shutil.copytree(str(WEB_DIR), str(web))
    site = _Site(web)
    thread = threading.Thread(target=site.serve_forever, daemon=True)
    thread.start()
    try:
        yield _Runner(site, find_chrome(), S.build(tmp / "data"), tmp,
                      os.environ.get("TLT_DOM_SHOTS"))
    finally:
        site.shutdown()
        site.server_close()


def _listing(found):
    return "\n".join("  {}: {}".format(x.get("at"), x.get("text")) for x in found)


# ---- 断言（开 Chrome） ----------------------------------------------------------------------

@needs_chrome
@pytest.mark.parametrize("name", EN_SCENARIOS)
def test_english_page_has_no_chinese_left(dom, name):
    found = dom.scan(name)["cjk"]
    assert not found, "英文页上还有 {} 处中文：\n{}".format(len(found), _listing(found))


@needs_chrome
@pytest.mark.parametrize("name", S.DESKTOP_EN)
def test_desktop_page_tells_the_window_its_language(dom, name):
    bridge = dom.scan(name)["bridge"]
    assert ["set_window_lang", "en"] in bridge, bridge


@needs_chrome
@pytest.mark.parametrize("name", EN_SCENARIOS)
def test_english_page_never_paints_a_chinese_first_frame(dom, name):
    """页面脚本跑之前 body 必须是隐藏的（防闪规则生效），跑完之后第一时间可见、语言已定。
    phone-local-en 最要紧：服务端给的是中文页，英文是这台手机自己选的。"""
    probe = dom.scan(name)["probe"]
    assert probe is not None, "探针没跑到"
    assert probe["before"] == "hidden", probe
    assert (probe["first"], probe["lang"], probe["done"]) == ("visible", "en", True), probe


@needs_chrome
def test_the_scan_is_not_blind(dom):
    """防空转：同一个 live-full 用中文跑一遍，必须扫得出中文。数据区里的中文——中文品牌名
    （名字，translate="no"）、中文译文和报警上下文的中文译文（正文，class i18n-data，不标
    translate="no"，用户决定 6）——确实画到了页面上，但中英两种页面都不许计入。
    手机不收品牌名，只查另外两样。"""
    zh = dom.scan(S.CONTROL_ZH)
    assert zh["cjk"], "中文页上一处中文都没扫到：扫描本身失灵了"
    for name in (S.CONTROL_ZH, "live-full", "phone-live"):
        result = dom.scan(name)
        wanted = [d for d in S.DATA_ZH if name.startswith("live") or d != S.BRAND_ZH]
        missing = [d for d in wanted if d not in result["dom"]]
        assert not missing, "{}：场景里的中文数据没画到页面上 {}".format(name, missing)
        counted = [x for x in result["cjk"] if any(d in x["text"] for d in S.DATA_ZH)]
        assert not counted, "{}：数据区里的中文被计入了：\n{}".format(
            name, _listing(counted))
        # 豁免得对还不够，还要豁免在对的标法上：品牌名是名字，浏览器不许翻；两条译文是正文，
        # 要留给浏览器「翻译此页」，不许标 translate="no"
        for text in wanted:
            holders = _holders(result["dom"], text)
            assert holders, "{}：找不到直接装着「{}」的元素".format(name, text)
            for tag in holders:
                named = 'translate="no"' in tag
                body = "i18n-data" in _classes(tag)
                ok = named and not body if text == S.BRAND_ZH else body and not named
                assert ok, "{}：「{}」的标法不对：{}".format(name, text, tag)


def _holders(html, text):
    """--dump-dom 里直接、只装着这段文字的元素的开标签。"""
    return [m.group(0)[:m.group(0).index(">") + 1] for m in re.finditer(
        r"<([a-z]+)\b[^>]*>{}</\1>".format(re.escape(text)), html)]


def _classes(tag):
    m = re.search(r'\bclass="([^"]*)"', tag)
    return m.group(1).split() if m else []


def _expanded(html, element_id):
    """--dump-dom 里这个元素的开标签上 aria-expanded="true"（属性先后不论）。"""
    tag = re.search(r'<[^>]*\bid="{}"[^>]*>'.format(re.escape(element_id)), html)
    return bool(tag) and 'aria-expanded="true"' in tag.group(0)


@needs_chrome
def test_scenario_clicks_took_effect(dom):
    """点击没点到的话，展开后才有的文字、确认框里的文字就没被扫到，而扫描照样是绿的。
    所以核对：设置各行（含语言行）和输入说明确实展开了；磁盘勾了日志项再点删除、同看点了
    换链接，两处都真的弹出了确认框；换主播已经武装（量得到确认按钮）。"""
    home = dom.scan("home-idle")["dom"]
    closed = [h for h in ("sc-head", "engine-head", "watch-head", "disk-head", "lang-head",
                          "input-help-btn") if not _expanded(home, h)]
    assert not closed, "home-idle 里这些行没展开：{}".format(closed)
    assert len(dom.scan("home-selfcheck-fail")["confirms"]) == 1, "磁盘删除的确认框没弹出"
    live = dom.scan("live-full")
    assert len(live["confirms"]) == 1, "同看换链接的确认框没弹出"
    confirm = live["measure"]["#switch-confirm"]
    assert confirm and confirm["width"] > 0, "换主播没有武装：{}".format(confirm)


@needs_chrome
@needs_mac
@pytest.mark.parametrize("width", S.LIVE_WIDTHS + S.NARROW_WIDTHS)
def test_live_top_bar_fits(dom, width):
    """live-full 在各档窗口宽度下：顶栏整行不溢出；状态字、「换主播」确认按钮不被截；
    品牌标签留得出宽度。860/720 两档只看顶栏（spec §12.3 第 7 条）。"""
    result = dom.scan("live-full@{}".format(width))
    got = result["measure"]
    problems = []

    def need(ok, what):
        if not ok:
            problems.append(what)

    need(result["viewport"]["width"] == width, "窗口不是 {}px 宽：{}".format(width, result["viewport"]))
    bar = got[".topbar"]
    need(bar and bar["scrollWidth"] <= bar["clientWidth"], ".topbar 溢出 {}".format(bar))
    if width in S.LIVE_WIDTHS:
        status, confirm = got[".status-text"], got["#switch-confirm"]
        need(status and status["scrollWidth"] <= status["clientWidth"] + 1,
             ".status-text 被截 {}".format(status))
        # 整数宽度看不出零点几个像素的截断，省略号却已经换掉了最后几个字母（scan.js 的注释）。
        # Chrome 只要文字比盒子宽一点点就出省略号：PR #70 在 1000px 实测文字 123.0625、盒子
        # 123.015625，只差 3/64 像素，"@bellaallnatural" 就显示成 "@bellaallnatu…"。
        # 排版单位是 1/64 像素，容差取 0.01 只挡浮点噪声
        need(status and status["textWidth"] <= status["innerWidth"] + 0.01,
             ".status-text 文字比盒子宽（省略号已出现） {}".format(status))
        need(confirm and confirm["width"] > 0, "换主播面板没打开，#switch-confirm 量不到 {}".format(confirm))
        need(confirm and confirm["scrollWidth"] <= confirm["clientWidth"] + 1,
             "#switch-confirm 被截 {}".format(confirm))
        brand = got["#active-brand-tag"]
        floor = 100 if width == 1000 else 120 if 1101 <= width <= 1199 else 0
        need(brand and brand["width"] >= floor,
             "#active-brand-tag 窄于 {}px {}".format(floor, brand))
    assert not problems, "\n".join(problems)


@needs_chrome
@needs_mac
@pytest.mark.parametrize("name", S.PHONE_EN)
def test_phone_status_pill_stays_on_screen(dom, name):
    """390 宽：状态胶囊不出屏；#conn-text 可以比自己宽（有省略号），不断言。
    先核对视口真是 390：--window-size 在 macOS 的新无头模式里窄不过约 500px，拿 500 量的话
    这条断言永远是绿的（所以手机场景改走 DevTools 定视口）。"""
    result = dom.scan(name)
    assert result["viewport"]["width"] == S.PHONE[0], result["viewport"]
    pill = result["measure"][".status-pill"]
    assert pill and pill["right"] <= result["viewport"]["width"], (pill, result["viewport"])


# ---- 不开 Chrome 的用例（平时照常跑） --------------------------------------------------------

def test_the_scan_is_opt_in():
    """没设 TLT_DOM_SCAN=1 就不开 Chrome：直播转写进行中的机器上跑全套测试也不会起浏览器。"""
    assert skip_reason({}) is not None
    assert skip_reason({"TLT_DOM_SCAN": "0"}) is not None


def _fake_exe(path):
    path.write_text("", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_find_chrome_prefers_tlt_chrome(tmp_path, monkeypatch):
    fake = _fake_exe(tmp_path / "fake-chrome.exe")
    fallback = _fake_exe(tmp_path / "fake-fallback.exe")
    # 候选名单换成 tmp 里的一个真文件：结果与跑测试那台机器装没装 Chrome 无关
    monkeypatch.setattr(sys.modules[__name__], "CHROME_NAMES", (str(fallback),))
    assert Path(find_chrome({"TLT_CHROME": str(fake)})) == fake
    # TLT_CHROME 指向不存在的文件：跳过它，接着找名单里的下一个
    assert Path(find_chrome({"TLT_CHROME": str(tmp_path / "missing")})) == fallback
    assert Path(find_chrome({})) == fallback
    monkeypatch.setattr(sys.modules[__name__], "CHROME_NAMES", ())
    assert find_chrome({"TLT_CHROME": str(tmp_path / "missing")}) is None
    assert find_chrome({}) is None


def test_scenarios_carry_real_backend_text_and_the_chinese_data(tmp_path):
    built = S.build(tmp_path)
    assert set(EN_SCENARIOS + (S.CONTROL_ZH,)) <= set(built)
    for name, sc in built.items():
        assert sc["page"] in ("desktop", "phone") and sc["size"][0] > 0, name
        assert json.loads(json.dumps(sc)) == sc, name        # scenario.js 里原样可用
        if sc["page"] == "desktop":
            hello = sc["messages"][0]
            assert hello["type"] == "hello", name
            # 语言行要露出来（spec §3.4）；本页和服务端语言一致，页面不会自己重载
            assert hello["config"]["ui_lang"] == sc["lang"], name
            assert hello["config"]["ui_lang_available"] is True, name
    live = built["live-full"]["messages"]
    history = [m["type"] for m in live if m["type"] in ("caption", "session_break")]
    # 场次分隔线要画出来：它前面得已经有字幕卡片（web/session-divider.js）
    assert history.index("session_break") > 0 and history[0] == "caption"
    config = live[0]["config"]
    assert config["incidents"]["session:clock_gap"]["text"]            # _check_clock_gap
    assert config["viewer"]["on"] and config["viewer"]["note"]         # share_note
    assert config["active_brand"]["name"] == S.BRAND_ZH
    assert [m["level"] for m in live if m["type"] == "health"] == ["lagging", "ok", "degraded"]
    whys = {m["alert_id"]: m["why"] for m in live if m["type"] == "alert_update"}
    assert sorted(whys) == [2, 3] and whys[2] and whys[3] and whys[2] != whys[3]   # _translate_alert
    assert any(m.get("translated") == S.CAPTION_ZH for m in live if m["type"] == "caption")
    fail = built["home-selfcheck-fail"]["messages"][0]["config"]
    assert fail["selfcheck"]["summary"]["fail"] >= 1 and fail["incidents"]["selfcheck"]["text"]
    assert fail["engine"]["note"] and fail["update_check"]["note"]
    assert {i["kind"] for i in fail["disk"]["items"]} >= {"hf", "ollama", "logs"}
    assert built["error-4003110"]["messages"][0]["config"]["status"]["detail"]
    phone = built["phone-live"]["messages"]
    assert phone[0]["type"] == "viewer_hello" and phone[-1] == {"type": "status", "state": "ended"}
    for msg in phone:                       # 每条后端文字都带派生的英文字段
        if msg["type"] == "incident":
            assert "text_en" in msg
        if msg["type"] == "alert":
            assert "why_en" in msg
    assert built["phone-local-en"]["page_lang"] == "zh"
    assert built["phone-local-en"]["storage"] == {"tlt.viewer.lang": "en"}


@pytest.mark.parametrize("page, lang, html_tag", [
    ("desktop", "en", '<html lang="en">'),
    ("desktop", "zh", '<html lang="zh-CN">'),
    ("phone", "en", '<html lang="en" data-i18n="on">'),
    ("phone", "zh", '<html lang="zh-CN" data-i18n="on">'),
])
def test_page_gets_the_fixtures_around_its_own_scripts(page, lang, html_tag):
    desktop = page == "desktop"
    html = page_html({"page": page, "page_lang": lang})
    base = "/static/" if desktop else "/v/"
    original = (WEB_DIR / ("index.html" if desktop else "viewer.html")).read_text(encoding="utf-8")
    assert html.count(html_tag) == 1
    for tag in re.findall(r'<script src="[^"]+"></script>', original):
        assert html.count(tag) == 1, tag                   # 原来的脚本（连 ?v=）一个不少
    first = _only_tag(original, base + ("i18n.js" if desktop else "viewer.js"))
    last = _only_tag(original, base + ("app.js" if desktop else "viewer.js"))
    fixtures = "".join(_script_tag(base + n) for n in ("probe.js", "scenario.js", "fake_ws.js"))
    assert fixtures + first in html
    assert last + _script_tag(base + "probe.js") + _script_tag(base + "scan.js") in html
    stripped = html
    for n in ("probe.js", "scenario.js", "fake_ws.js", "scan.js"):
        stripped = stripped.replace(_script_tag(base + n), "")
    assert stripped == i18n.inject_lang(original, lang, marker=not desktop)   # 别的字节不动


def test_page_refuses_to_serve_without_its_anchor(tmp_path):
    """插入点没了（脚本改名、合并）要当场报错，不能端出一个没装替身、扫描空转的页面。"""
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    (tmp_path / "index.html").write_text(html.replace("/static/i18n.js", "/static/lang.js"),
                                         encoding="utf-8")
    with pytest.raises(AssertionError, match="i18n.js"):
        page_html({"page": "desktop", "page_lang": "en"}, tmp_path)


def test_site_serves_fixtures_the_scenario_and_web_files_only(tmp_path):
    web = tmp_path / "web"
    (web / "sub").mkdir(parents=True)
    shutil.copy(str(WEB_DIR / "viewer.css"), str(web / "viewer.css"))
    # 这几份文件都真的存在：拦下它们的只能是路径检查本身，而不是「文件不在」
    (tmp_path / "secret.txt").write_text("x", encoding="utf-8")
    (web / "sub" / "x.js").write_text("x", encoding="utf-8")
    (web / ".hidden").write_text("x", encoding="utf-8")
    site = _Site(web)
    try:
        site.scenario = {"page": "phone", "page_lang": "en", "storage": {"tlt.viewer.lang": "en"}}
        body, ctype = site.resolve("/v/scenario.js?v=1")
        assert body.startswith(b"window.__SCENARIO = {") and b'localStorage.setItem("tlt.viewer.lang"' in body
        assert site.resolve("/v/scan.js")[0] == (FIXTURE_DIR / "scan.js").read_bytes()
        assert site.resolve("/v/viewer.css?v=6")[1].startswith("text/css")
        for bad in ("/v/../secret.txt", "/v/sub/x.js", "/v/.hidden", "/v/", "/elsewhere",
                    "/v/missing.js"):
            assert site.resolve(bad) is None, bad
    finally:
        site.server_close()


SCAN_DOM = '<html><body><script type="application/json" id="i18n-scan">{"cjk": []}</script></body></html>'


def _runner_with(monkeypatch, tmp_path, outcomes):
    """_Runner 接一个不开 Chrome 的 run_chrome：按 outcomes 依次抛出或交回 DOM（交回前把页面引用的
    资源都记成端出去过）。交回 (runner, 每次拿到的配置目录, site)。"""
    site = _Site(WEB_DIR)
    sc = {"page": "desktop", "page_lang": "en", "lang": "en", "size": [1000, 760]}
    profiles, script = [], iter(outcomes)

    def fake_run(chrome, url, size, lang, profile, extra, finished):
        profiles.append(profile)
        site.served.append(("/static/app.js", 200))
        outcome = next(script)
        if isinstance(outcome, Exception):
            raise outcome
        site.served.extend((path, 200) for path in site.assets(page_html(sc)))
        return outcome

    monkeypatch.setattr(sys.modules[__name__], "run_chrome", fake_run)
    return _Runner(site, "chrome", {"x": sc}, tmp_path), profiles, site


def test_a_chrome_that_gives_no_result_in_time_is_retried_once(tmp_path, monkeypatch):
    """PR #68 那种偶发：第一次到点没结果，换一个新配置目录再起一次就好了。重试留一条 warning，
    带着当时已经端出去的资源。"""
    runner, profiles, site = _runner_with(monkeypatch, tmp_path, [
        ChromeNoResult("Chrome 60 秒没有给出结果，已杀掉整个进程组：http://x/\nstderr"), SCAN_DOM])
    try:
        with pytest.warns(UserWarning, match=r"G10 x：Chrome 60 秒没有给出结果.*1 个资源.*重试"):
            assert runner.scan("x")["cjk"] == []
        assert len(profiles) == 2 and profiles[0] != profiles[1]
    finally:
        site.server_close()


@pytest.mark.parametrize("outcomes,raised", [
    ([ChromeNoResult("到点 1"), ChromeNoResult("到点 2")], ChromeNoResult),   # 每次都卡：照样红
    ([AssertionError("Chrome 没给出结果就退出了（退出码 1）")], AssertionError),  # 自己退出：不重试
])
def test_only_one_retry_and_only_for_no_result_in_time(tmp_path, monkeypatch, outcomes, raised):
    runner, profiles, site = _runner_with(monkeypatch, tmp_path, outcomes)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with pytest.raises(raised) as caught:
                runner.scan("x")
        assert type(caught.value) is raised and len(profiles) == len(outcomes)
    finally:
        site.server_close()


def test_dump_json_reads_the_scan_node():
    dom = ('<html><body><p>x</p><script type="application/json" id="i18n-scan">'
           '{"cjk": [{"at": "p", "text": "中文"}]}</script></body></html>')
    assert dump_json(dom, "i18n-scan") == {"cjk": [{"at": "p", "text": "中文"}]}
    assert dump_json(dom, "i18n-probe") is None


def test_devtools_url_comes_from_stderr_or_the_port_file(tmp_path):
    line = b"\nDevTools listening on ws://127.0.0.1:53111/devtools/browser/ab-12\nother\n"
    assert devtools_url(bytearray(line), tmp_path) == "ws://127.0.0.1:53111/devtools/browser/ab-12"
    assert devtools_url(b"", tmp_path) is None                         # 两样都还没有
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text("53112\n", encoding="utf-8")                  # 只写了一半
    assert devtools_url(b"", tmp_path) is None
    port_file.write_text("53112\n/devtools/browser/cd-34\n", encoding="utf-8")
    assert devtools_url(b"", tmp_path) == "ws://127.0.0.1:53112/devtools/browser/cd-34"


def test_phone_scan_sets_the_viewport_over_devtools():
    """不开 Chrome：对着一个假的 DevTools 端点跑 cdp_scan，核对对话本身。开标签页、挂上会话，
    页面级命令都带那个 sessionId；先定视口（390×844，桌面模式）再导航；中途插进来的事件跳过；
    导航途中「执行上下文没了」的报错不算失败，接着问，直到 #i18n-scan 出来才交回 DOM；截图解码。"""
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    dom = '<html><body><script type="application/json" id="i18n-scan">{}</script></body></html>'
    calls = []

    async def devtools(request):
        ws = web.WebSocketResponse(max_msg_size=0)
        await ws.prepare(request)
        polls = 0
        async for frame in ws:
            msg = json.loads(frame.data)
            calls.append(msg)
            await ws.send_str(json.dumps({"method": "Page.frameNavigated", "params": {}}))
            reply = {"id": msg["id"], "result": {}}
            method = msg["method"]
            if method == "Target.createTarget":
                reply["result"] = {"targetId": "T1"}
            elif method == "Target.attachToTarget":
                reply["result"] = {"sessionId": "S1"}
            elif method == "Runtime.evaluate":
                polls += 1
                if polls == 1:
                    reply = {"id": msg["id"], "error": {"code": -32000,
                                                        "message": "Execution context was destroyed."}}
                else:
                    reply["result"] = {"result": {"type": "string", "value": dom if polls == 3 else ""}}
            elif method == "Page.captureScreenshot":
                reply["result"] = {"data": base64.b64encode(b"\x89PNG fake").decode("ascii")}
            if "sessionId" in msg:
                reply["sessionId"] = msg["sessionId"]
            await ws.send_str(json.dumps(reply))
        return ws

    async def go():
        app = web.Application()
        app.router.add_get("/devtools/browser/x", devtools)
        server = TestServer(app)
        await server.start_server()
        try:
            ws_url = str(server.make_url("/devtools/browser/x")).replace("http://", "ws://", 1)
            return await cdp_scan(ws_url, "http://127.0.0.1:9/#k=" + S.FAKE_TOKEN, S.PHONE,
                                  time.monotonic() + 20, shot=True)
        finally:
            await server.close()

    got, png = asyncio.run(go())
    assert got == dom and png == b"\x89PNG fake"
    methods = [c["method"] for c in calls]
    assert methods == ["Target.createTarget", "Target.attachToTarget", "Page.enable",
                       "Emulation.setDeviceMetricsOverride", "Page.navigate",
                       "Runtime.evaluate", "Runtime.evaluate", "Runtime.evaluate",
                       "Page.captureScreenshot"], methods
    assert calls[1]["params"] == {"targetId": "T1", "flatten": True}
    assert [c.get("sessionId") for c in calls] == [None, None] + ["S1"] * 7
    metrics = calls[3]["params"]
    assert (metrics["width"], metrics["height"], metrics["mobile"]) == (390, 844, False)
    assert calls[4]["params"] == {"url": "http://127.0.0.1:9/#k=" + S.FAKE_TOKEN}
