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
再开一次（关掉弹出动画，见 SHOT_FLAGS），手机在同一次页面上经 DevTools 截（2 倍像素）。

文件末尾几条用例不开 Chrome，平时照常跑：场景组得起来、插入点还在、默认确实跳过、到点没结果时
重试一次。页面结构或场景一变普通 CI 就会红，不用等 dom-scan job。收紧闸（Z0）起 dom-scan 整组
严格跑：英文页上剩下中文、顶栏放不下，PR 都是红的。
"""
import asyncio
import base64
import contextlib
import copy
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
from tests import socket_net
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
# 桌面、浮窗截图（TLT_DOM_SHOTS）另加的参数。--force-prefers-reduced-motion：浮层弹出有 .16 秒的 popover-in 动画
# （style.css，减少动态效果时关掉），截图可能截在动画半路，整个浮层半透明发灰——闪购「再点一次开始」那张就截成过这样
# （2026-10-28 审查第 8 条）。关掉动画，截到的就是画好的样子；扫描（--dump-dom）不受影响
SHOT_FLAGS = ("--hide-scrollbars", "--force-prefers-reduced-motion")
TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".png": "image/png", ".json": "application/json"}
EN_SCENARIOS = S.DESKTOP_EN + S.PHONE_EN + S.FLOAT_EN


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


# 每种页面：(文件, 资源路径前缀, 第一个脚本, 最后一个脚本)。浮窗页（web/float.html）和桌面页走同一个 /static/
PAGE_FILES = {"desktop": ("index.html", "/static/", "i18n.js", "app.js"),
              "float": ("float.html", "/static/", "i18n.js", "float.js"),
              "phone": ("viewer.html", "/v/", "viewer.js", "viewer.js")}


def page_html(scenario, web_dir=WEB_DIR):
    """服务器在 / 上端的页面：注入语言，再在页面脚本前后插入替身。别的字节一个不动。"""
    name, base, first, last = PAGE_FILES[scenario["page"]]
    html = (Path(web_dir) / name).read_text(encoding="utf-8")
    if html.count('<html lang="zh-CN">') != 1:       # inject_lang 靠它；没有就会静默不注入
        raise AssertionError('{} 里 <html lang="zh-CN"> 应恰好一个'.format(name))
    html = i18n.inject_lang(html, scenario["page_lang"], marker=scenario["page"] == "phone")
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
        # 调试端口是 Chrome 自己挑的、不是本进程绑的：读到以后登记给测试总网（tests/socket_net.py），只在这一次
        # DevTools 会话里放行
        try:
            with socket_net.allowing(urlsplit(ws_url).port):
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
            if shot is not None and sc["page"] in ("desktop", "float"):
                self._launch(name, lambda profile: run_chrome(
                    self.chrome, url, sc["size"], sc["lang"], profile,
                    list(SHOT_FLAGS) + ["--screenshot={}".format(shot)], shot_written))
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


def _hidden(html, element_id):
    """--dump-dom 里这个元素的开标签带 hidden 类。"""
    tag = re.search(r'<[^>]*\bid="{}"[^>]*>'.format(re.escape(element_id)), html)
    return bool(tag) and "hidden" in _classes(tag.group(0))


# 首页输入框下面那一行（#asr-model-hint，2026-10-29 launch-2 A）的文字和按钮；大小来自 app/asr.py MODEL_SIZES_MB
ASR_HINT_TEXT = {"en": ("The speech model isn’t downloaded yet (about 3.1 GB)", "Download Now"),
                 "zh": ("语音模型还没下载（约 3.1 GB）", "现在下载")}


@needs_chrome
@pytest.mark.parametrize("name", S.ASR_FETCH_HOMES)
def test_home_says_the_speech_model_is_not_downloaded(dom, name):
    """新装机、语音模型还没下载：首页输入框下面那一行出现，句子和按钮都画出来了；自检那一行也带着「现在下载」。
    英文页的漏翻由 test_english_page_has_no_chinese_left 扫（home-asr-fetch 在 DESKTOP_EN 里）。"""
    result = dom.scan(name)
    html = result["dom"]
    lang = "zh" if name.endswith("-zh") else "en"
    sentence, label = ASR_HINT_TEXT[lang]
    assert not _hidden(html, "asr-model-hint"), "首页那一行没出现"
    assert _holders(html, sentence), "没画出「{}」".format(sentence)
    assert re.search(r'<button[^>]*\bid="asr-model-hint-btn"[^>]*>{}</button>'.format(re.escape(label)), html), \
        "首页按钮不是「{}」".format(label)
    assert "sc-action" in html, "自检「语音识别」一行没带「现在下载」"


@needs_chrome
@needs_mac
@pytest.mark.parametrize("name", S.ASR_FETCH_HOMES)
def test_home_model_row_fits_on_one_line(dom, name):
    """这一行在 600px 的开始面板里一行放得下：句子不折行（一行 18px），整行不横向溢出，按钮在这一行里面。"""
    got = dom.scan(name)["measure"]
    row, text, btn = (got[sel] for sel in S.ASR_HINT_MEASURE)
    assert row and row["width"] > 0 and text and btn, "量不到：{} {} {}".format(row, text, btn)
    assert row["scrollWidth"] <= row["clientWidth"] + 1, "这一行横向溢出 {}".format(row)
    assert text["height"] < 30, "句子折成了两行 {}".format(text)
    assert btn["width"] > 0 and btn["right"] <= row["right"], "按钮没在这一行里 {} {}".format(btn, row)


@needs_chrome
def test_guard_scenario_clicks_took_effect(dom):
    """弹幕留意的场景：点击没点到的话，展开后才有的文字就没被扫到而扫描照样绿。设置行展开了；
    直播中顶部条弹出、红条和琥珀和关注的人都画到弹幕栏里、抽屉在正确的页签；熔断的场景有重新启用按钮。"""
    home = dom.scan("home-guard")["dom"]
    assert _expanded(home, "guard-head"), "设置行没展开"
    assert not _hidden(home, "guard-card") and not _hidden(home, "guard-key-row"), "选了 Claude 且没有密钥：密钥框该出现"
    live = dom.scan("live-guard")
    html = live["dom"]
    assert not _hidden(html, "guard-bar") and not _hidden(html, "guard-state"), "顶部条或状态胶囊没出现"
    assert not _hidden(html, "guard-drawer") and _expanded(html, "guard-state"), "抽屉没展开"
    assert not _hidden(html, "guard-pane-recent") and _hidden(html, "guard-pane-marked")
    for cls in ("guard-alert", "guard-amber", "guard-marked"):
        assert cls in html, "弹幕栏里没有 {} 的行".format(cls)
    for name in ("Possible call to report or not buy", "Possible redirect (link)", "Short, with no question mark",
                 "Same comment from 5 accounts in 20 seconds", "Model: agrees with rules"):
        assert name in html, "页面上没有「{}」".format(name)
    assert "Comment Review: rules only" in html
    marked = dom.scan("live-guard-marked")["dom"]
    assert _hidden(marked, "guard-pane-recent") and not _hidden(marked, "guard-pane-marked")
    assert "Added automatically: possible call to report or not buy" in marked and "Added by you" in marked
    tripped = dom.scan("live-guard-tripped")["dom"]
    assert not _hidden(tripped, "guard-reenable") and "Turn Back On for This Session" in tripped
    assert "speech recognition fell 14 sec behind" in tripped


@needs_chrome
@needs_mac
@pytest.mark.parametrize("name", ("live-guard", "live-guard-marked", "live-guard-tripped"))
def test_guard_panels_do_not_overflow(dom, name):
    """顶部条永远一行（放不下用省略号，不撑宽）；抽屉和弹幕列不出现横向滚动条。"""
    got = dom.scan(name)["measure"]
    problems = []
    for sel in (".guard-bar", ".guard-drawer", ".comment-list"):
        m = got.get(sel)
        if not m:
            problems.append("{} 量不到：{}".format(sel, m))
        elif m["scrollWidth"] > m["clientWidth"] + 1:
            problems.append("{} 横向溢出 {}".format(sel, m))
    assert not problems, "\n".join(problems)


@needs_chrome
def test_guard_drawer_is_a_third_of_the_window_and_both_tabs_are_equally_tall(dom):
    """抽屉的高度是固定的：最近需要留意、关注名单、熔断的三个场景（状态行长短不一）里一样高，不超过窗口的约三分之一；
    滚的是列表区（.guard-pane），不是整个抽屉。静态的规则在 tests/guard-css.test.mjs，这里量真的排版。"""
    names = ("live-guard", "live-guard-marked", "live-guard-tripped")
    scans = {name: dom.scan(name) for name in names}
    heights = {name: scans[name]["measure"][".guard-drawer"]["height"] for name in names}
    viewport = scans["live-guard"]["viewport"]["height"]
    assert max(heights.values()) - min(heights.values()) < 1, "切页签或状态行变长时抽屉高度变了：{}".format(heights)
    assert max(heights.values()) <= viewport / 3 + 4, "抽屉 {} 超过窗口 {} 的三分之一".format(heights, viewport)
    for name in names:
        got = scans[name]["measure"]
        drawer, pane = got[".guard-drawer"], got[".guard-pane:not(.hidden)"]
        assert drawer["scrollHeight"] <= drawer["clientHeight"] + 1, "{}：整个抽屉在滚，应该是列表区在滚 {}".format(name, drawer)
        assert pane["scrollHeight"] > pane["clientHeight"], "{}：场景里的内容应该多到要滚动 {}".format(name, pane)
        assert pane["bottom"] <= drawer["bottom"] + 1 and pane["top"] >= drawer["top"], "{}：列表区跑出了抽屉".format(name)


@needs_chrome
@pytest.mark.parametrize("name", S.GUARD_ZH)
def test_guard_chinese_pages_are_chinese(dom, name):
    """中文对照页：同一批场景，界面是中文（不扫漏翻，只核对确实是中文的守卫界面），顺便出中文截图。"""
    result = dom.scan(name)
    html = result["dom"]
    assert '<html lang="zh-CN"' in html
    expect = {"home-guard-zh": ("弹幕留意", "本程序只读", "打开规则文件夹"),
              "live-guard-zh": ("疑似号召举报或不买", "句子很短，没有问号", "模型意见：同意规则", "展开账号"),
              "live-guard-marked-zh": ("自动加入：疑似号召举报或不买", "手动加入"),
              "live-guard-tripped-zh": ("本场模型复核已停用：识别落后 14 秒。只用规则。", "本场重新启用")}[name]
    for text in expect:
        assert text in html, "{}：页面上没有「{}」".format(name, text)


def _caption_menu_problems(dom, base, width, got):
    """直播中量顶栏的几条共用：「字幕译成」下拉是不是场景要量的那个样子。
    名字带 S.EXTRA 后缀的场景目标在四种以外：下拉必须真的补了一项、选中它，而且比同一宽度、目标在四种之内的
    那份场景宽（约 144 对 116px，docs/i18n-style.md §4.1）——否则这份场景量的仍是 116px，那 28px 等于没量。
    其余场景目标在四种之内，不该有补出来的那一项。"""
    problems = []
    extra = got[S.TARGET_EXTRA_SELECTED]
    if not base.endswith(S.EXTRA):
        if extra is not None:
            problems.append("目标在四种之内，下拉不该有补出来的那一项 {}".format(extra))
        return problems
    if extra is None:
        problems.append("当前目标 {} 不在四种里，下拉该补一项并选中它".format(S.EXTRA_TARGET))
    menu = got["#target-lang"]
    plain = dom.scan("{}@{}".format(base[:-len(S.EXTRA)], width))["measure"]["#target-lang"]
    if not (menu and plain and menu["width"] > plain["width"]):
        problems.append("补了一项的下拉应比只有四种时宽：{} 对 {}".format(menu, plain))
    return problems


@needs_chrome
@needs_mac
@pytest.mark.parametrize("base", S.LIVE_BASES)
@pytest.mark.parametrize("width", S.LIVE_WIDTHS + S.NARROW_WIDTHS)
def test_live_top_bar_fits(dom, base, width):
    """live-full 在各档窗口宽度下：顶栏整行不溢出；状态字、「换主播」确认按钮不被截；
    品牌标签留得出宽度。860/720 两档只看顶栏（spec §12.3 第 7 条）。
    live-float 是同一个场景加上窗口模式才有的「浮窗」按钮（顶栏多 36px，字号滑块收窄 24px 抵消，
    见 web/style.css）：下限和 live-full 一样，这是窗口模式（所有人的默认模式）真实的顶栏。
    -extra 的两个是同一场景、当前目标换成四种以外的 id：「字幕译成」下拉补了一项，是它最宽的样子（约 144px，
    只有四种时约 116px）。§4.1/§4.2 的余量按 144 算，存着 id / vi / pt 的中控直播中看到的就是这个顶栏。"""
    result = dom.scan("{}@{}".format(base, width))
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
        # 123.015625，只差 3/64 像素，15 个字母的主播名就被省略号吃掉最后三个字母。
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
    problems.extend(_caption_menu_problems(dom, base, width, got))
    assert not problems, "\n".join(problems)


@needs_chrome
@needs_mac
@pytest.mark.parametrize("base", S.HOME_BASES)
@pytest.mark.parametrize("width", S.HOME_WIDTHS)
def test_home_top_bar_says_what_the_caption_menu_is_for(dom, base, width):
    """待机首页的顶栏（用户 2026-10-20 在默认 1000px 的首页把这个下拉当成了界面语言）：
    标签「Captions in / 字幕译成」每一档都露出来（直播中 ≤1080px 才省掉，web/style.css），顶栏不溢出，
    状态字不被截。home-zh 的当前目标是四种以外的 id：浏览器里下拉确实补了一项并选中它（也是它最宽的样子）；
    home-idle 的目标是 en，在四种之内，没有补的那一项。"""
    result = dom.scan("{}@{}".format(base, width))
    got = result["measure"]
    problems = []

    def need(ok, what):
        if not ok:
            problems.append(what)

    need(result["viewport"]["width"] == width, "窗口不是 {}px 宽：{}".format(width, result["viewport"]))
    bar, status, label = got[".topbar"], got[".status-text"], got[".ctl-text"]
    need(bar and bar["scrollWidth"] <= bar["clientWidth"], ".topbar 溢出 {}".format(bar))
    need(status and status["textWidth"] <= status["innerWidth"] + 0.01,
         ".status-text 文字比盒子宽（省略号已出现） {}".format(status))
    need(label and label["width"] > 20, "「字幕译成」标签没露出来 {}".format(label))
    need(label and label["right"] <= result["viewport"]["width"], "「字幕译成」标签出了窗口 {}".format(label))
    extra = got[S.TARGET_EXTRA_SELECTED]
    if base in ("home-zh", "home-flash-zh"):
        need(extra is not None, "当前目标 {} 不在四种里，下拉该补一项并选中它".format(S.HOME_ZH_TARGET))
    else:
        need(extra is None, "目标在四种之内，下拉不该有补出来的那一项 {}".format(extra))
    # home-flash*：装了闪购助手，首页也有「闪购」按钮（用户 10-27 定）；首页没有字号控件，「Aa」也不出现
    flash, font = got["#flash-btn"], got["#font-btn"]
    if "flash" in base:
        need(flash and flash["width"] > 0 and flash["right"] <= result["viewport"]["width"], "首页的闪购按钮没露出 {}".format(flash))
        label, icon = got["#flash-btn .btn-label"], got["#flash-btn .i"]
        zh_label = base == "home-flash-zh" and width >= 1000          # 中文 1000–1399px 只有字，其余只有图标
        need(label and (label["width"] >= 20) == zh_label, "「闪购」的字应{}露出 {}".format("" if zh_label else "不", label))
        need(icon and (icon["width"] > 0) != zh_label, "闪购按钮的图标应{}露出 {}".format("不" if zh_label else "", icon))
    else:
        need(flash and flash["width"] == 0, "没装闪购助手，按钮不该出现 {}".format(flash))
    need(font and font["width"] == 0, "首页不该有「Aa」{}".format(font))
    assert not problems, "\n".join(problems)


@needs_chrome
@needs_mac
@pytest.mark.parametrize("name", sorted(S.HINTS))
def test_the_caption_menu_hint_offers_the_switch_while_live(dom, name):
    """直播中、窗口模式默认 1000px：在「字幕译成」里选了另一种界面语言，下拉下面弹出就地提示，
    里面有切换界面的按钮（用户 2026-10-29：连接中、直播中也给）。提示在顶栏下面、整个在窗口里；按钮露全、
    在提示里面，字是那种语言的自称、lang 跟着它；顶栏照旧不溢出。"""
    result = dom.scan(name)
    got, html, vw = result["measure"], result["dom"], result["viewport"]["width"]
    problems = []

    def need(ok, what):
        if not ok:
            problems.append(what)

    need(vw == S.HINT_WIDTH, "窗口不是 {}px 宽：{}".format(S.HINT_WIDTH, result["viewport"]))
    hint, btn, bar = got["#target-hint"], got["#target-hint-switch"], got[".topbar"]
    need(bar and bar["scrollWidth"] <= bar["clientWidth"], ".topbar 溢出 {}".format(bar))
    need(hint and hint["width"] > 0, "提示没弹出来 {}".format(hint))
    need(btn and btn["width"] > 0, "直播中提示里该有切换按钮 {}".format(btn))
    if hint and hint["width"] > 0 and btn and btn["width"] > 0:
        need(hint["right"] <= vw and hint["right"] - hint["width"] >= 0, "提示出了窗口 {}".format(hint))
        need(bar and hint["top"] >= bar["bottom"] - 0.5, "提示该在顶栏下面 {} / {}".format(hint, bar))
        need(btn["right"] <= hint["right"] and btn["right"] - btn["width"] >= hint["right"] - hint["width"]
             and btn["bottom"] <= hint["bottom"], "按钮出了提示 {} / {}".format(btn, hint))
        need(btn["textWidth"] <= btn["innerWidth"] + 0.01, "按钮的字没露全 {}".format(btn))
    want_name, want_lang = ("中文", "zh-CN") if S.HINTS[name][2].startswith("zh") else ("English", "en")
    tag = re.search(r'<button[^>]*\bid="target-hint-switch"[^>]*>([^<]*)</button>', html)
    need(tag and tag.group(1) == want_name and 'lang="{}"'.format(want_lang) in tag.group(0),
         "按钮该写 {}（lang={}）：{}".format(want_name, want_lang, tag.group(0) if tag else None))
    assert not problems, "\n".join(problems)


def _pressed(html, element_id):
    """--dump-dom 里这个元素的开标签上 aria-pressed="true"（属性先后不论）。"""
    tag = re.search(r'<[^>]*\bid="{}"[^>]*>'.format(re.escape(element_id)), html)
    return bool(tag) and 'aria-pressed="true"' in tag.group(0)


@needs_chrome
@needs_mac
@pytest.mark.parametrize("base", S.HOME_BASES)
@pytest.mark.parametrize("width", S.HOME_WIDTHS)
def test_home_language_switch_sits_in_the_title_area_corner(dom, base, width):
    """待机首页标题区右上角的「中文 / English」两段切换（用户 2026-10-29）：露出来、在窗口里，不压住居中的
    图标（横向在它右边）和标题（整个在标题上面）；两颗都露全，按下的是本页的语言。设置里界面语言那一行
    的名字（英文页「App Language · 界面语言」）和摘要都露全，摘要没被省略号截掉。"""
    result = dom.scan("{}@{}".format(base, width))
    got, html, lang = result["measure"], result["dom"], dom.scenarios["{}@{}".format(base, width)]["lang"]
    problems = []

    def need(ok, what):
        if not ok:
            problems.append(what)

    quick, glyph, title = got["#lang-quick"], got[".sp-glyph"], got[".sp-title"]
    need(quick and quick["width"] > 0, "两段切换没露出来 {}".format(quick))
    if quick and quick["width"] > 0:
        need(quick["right"] <= result["viewport"]["width"], "两段切换出了窗口 {}".format(quick))
        need(glyph and quick["right"] - quick["width"] >= glyph["right"] + 8,
             "两段切换压到了图标 {} / {}".format(quick, glyph))
        need(title and quick["bottom"] <= title["top"], "两段切换压到了标题 {} / {}".format(quick, title))
        for code in ("zh", "en"):
            btn = got["#lang-quick-" + code]
            need(btn and btn["width"] > 0 and btn["textWidth"] <= btn["innerWidth"] + 0.01,
                 "「{}」那颗没露全 {}".format(code, btn))
    need(_pressed(html, "lang-quick-" + lang) and not _pressed(html, "lang-quick-" + ("en" if lang == "zh" else "zh")),
         "按下的应该是本页的语言 {}".format(lang))
    name, summary = got["#lang-head .set-name"], got["#lang-summary"]
    need(name and name["width"] > 0 and name["textWidth"] <= name["innerWidth"] + 0.01, "行名没露全 {}".format(name))
    need(summary and summary["textWidth"] <= summary["innerWidth"] + 0.01, "摘要被省略号截了 {}".format(summary))
    assert not problems, "\n".join(problems)


def _flash_shape_problems(got, width, viewport, lang="en", alert_tag=False):
    """带闪购按钮的顶栏共用的几条（docs/i18n-style.md §4.2 的表）：按钮露出、在窗口里；
    - 「Aa」：窄于 1400px（且宽于 720px）时露出、约 28px，字号控件收起（小浮层没开）；1400px 起「Aa」不出现、
      字号控件回到顶栏；≤720px 两样都不显示（同原来的字号控件）；
    - 按钮上的字：中文页 1000–1399px 只有字（报警标签亮着时除外），1400px 起图标加字；英文页 1600px 起才露字；
      其余只有图标（字视觉隐藏，1px）。"""
    problems = []
    flash, font_btn, font_ctl = got["#flash-btn"], got["#font-btn"], got["#font-ctl"]
    label, icon = got["#flash-btn .btn-label"], got["#flash-btn .i"]
    if not (flash and flash["width"] > 0 and flash["right"] <= viewport["width"]):
        problems.append("闪购按钮没露出或出了窗口 {}".format(flash))
    if width <= 720:
        if not (font_btn and font_btn["width"] == 0 and font_ctl and font_ctl["width"] == 0):
            problems.append("≤720px：「Aa」和字号控件都该收起 {} {}".format(font_btn, font_ctl))
    elif width < S.FLASH_WIDE:
        if not (font_btn and 26 <= font_btn["width"] <= 34):
            problems.append("窄于 1400px：「Aa」该露出、约 28px 宽 {}".format(font_btn))
        if not (font_ctl and font_ctl["width"] == 0):
            problems.append("窄于 1400px：字号控件该收进「Aa」（小浮层没开时不占地方）{}".format(font_ctl))
    else:
        if not (font_btn and font_btn["width"] == 0):
            problems.append("1400px 起不该有「Aa」{}".format(font_btn))
        if not (font_ctl and font_ctl["width"] > 60):
            problems.append("1400px 起字号控件该在顶栏里 {}".format(font_ctl))
    if lang == "zh":
        label_shown = width >= S.FLASH_WIDE or (width >= 1000 and not alert_tag)
        icon_shown = width >= S.FLASH_WIDE or not label_shown
    else:                        # 英文页图标一直在，1600px 起再加上字
        label_shown, icon_shown = width >= 1600, True
    if not (label and (label["width"] >= 20) == label_shown):
        problems.append("按钮上的字应{}露出 {}".format("" if label_shown else "不", label))
    if not (icon and (icon["width"] > 0) == icon_shown):
        problems.append("按钮的图标应{}露出 {}".format("" if icon_shown else "不", icon))
    if lang == "zh" and label_shown and not icon_shown and not (flash and 40 <= flash["width"] <= 52):
        problems.append("只有字的按钮应约 46px 宽 {}".format(flash))
    return problems


@needs_chrome
@needs_mac
@pytest.mark.parametrize("base", S.FLASH_BASES)
@pytest.mark.parametrize("width", S.LIVE_WIDTHS + S.NARROW_WIDTHS + S.FLASH_WIDE_WIDTHS)
def test_flash_button_and_the_aa_button_in_the_live_top_bar(dom, base, width):
    """装了闪购助手的直播顶栏（live-flash：浮窗、闪购都在，英文页）：闪购按钮露出，「Aa」在窄于 1400px 时替下字号控件。
    版面下限在 test_live_top_bar_fits 里（live-flash 在 LIVE_BASES 里，下限一条没放宽）；这里另量 1400、1600px 两档：
    「Aa」退场、字号控件回到顶栏，顶栏照样一行放下、状态字不被截，品牌标签不窄于 1101–1199px 那一档的 120px
    （1400px 时英文仍只有图标，就是为了这一条：露字的话 live-flash-extra@1400 只剩 54px）。"""
    result = dom.scan("{}@{}".format(base, width))
    got = result["measure"]
    problems = _flash_shape_problems(got, width, result["viewport"])
    if width in S.FLASH_WIDE_WIDTHS:
        bar, status, brand = got[".topbar"], got[".status-text"], got["#active-brand-tag"]
        if not (bar and bar["scrollWidth"] <= bar["clientWidth"]):
            problems.append(".topbar 溢出 {}".format(bar))
        if not (status and status["textWidth"] <= status["innerWidth"] + 0.01):
            problems.append(".status-text 文字比盒子宽（省略号已出现） {}".format(status))
        if not (brand and brand["width"] >= 120):
            problems.append("#active-brand-tag 窄于 120px {}".format(brand))
    body = _body_classes(result["dom"])
    if "has-flash-btn" not in body:
        problems.append("body 该有 has-flash-btn：{}".format(body))
    assert not problems, "\n".join(problems)


@needs_chrome
def test_flash_panel_keeps_the_helpers_chinese_text_in_the_data_area(dom):
    """闪购助手写的记录正文是中文（数据）：英文页上确实画出来了（浮层收到状态就画，不等打开），带 class i18n-data、
    不标 translate="no"（正文留给浏览器翻译），扫描不许把它算作漏翻。商品标题是名字，在 translate="no" 里。"""
    for name in ("live-flash", "home-flash"):
        result = dom.scan(name)
        html = result["dom"]
        assert S.FLASH_ZH_ALERT in html, "{}：记录正文没画到页面上".format(name)
        assert not [x for x in result["cjk"] if S.FLASH_ZH_ALERT in x["text"]], "{}：数据区里的中文被计入了".format(name)
        holders = _holders(html, S.FLASH_ZH_ALERT)
        assert holders and all("i18n-data" in _classes(t) and 'translate="no"' not in t for t in holders), holders
        titles = _holders(html, "Calm Gel Mask x4 (Fictional)")
        assert titles and all('translate="no"' in t for t in titles), titles


# 闪购浮层顶部那一排（S.FLASH_CTL_KINDS，docs/flash-sale.md §9）：种类 -> (露出来的按钮, 能点的按钮)
FLASH_CTL_BUTTONS = ("start", "stop", "resume", "open")
FLASH_CTL_EXPECT = {
    "idle": (("start", "open"), ("start", "open")),
    "armed": (("start", "open"), ("start", "open")),
    "running": (("stop", "open"), ("stop", "open")),
    "paused": (("stop", "resume", "open"), ("stop", "resume", "open")),
    "awaiting": (("stop", "open"), ("open",)),
    "old": (("open",), ("open",)),
    "starting": (("start", "open"), ("open",)),
    "skip": (("stop", "open"), ("stop", "open")),
    "rejected": (("start", "open"), ("start", "open")),
    "gated": (("stop", "open"), ("stop", "open")),
}
# 各种类在页面上必须看得到的字（中文、英文），按元素 id 找；None 的元素要藏着。flash.js 的文案，docs/i18n-style.md §3 K
FLASH_CTL_TEXT = {
    "idle": {"flash-start": ("开始", "Start"), "flash-ctl-note": None,
             "flash-desc": ("这里只能开始、停止和继续，其他操作请到闪购助手里。",
                            "Only Start, Stop and Resume work here. Do everything else in Flash Sale Helper.")},
    "armed": {"flash-start": ("再点一次开始", "Click Again to Start")},
    "running": {"flash-stop": ("停止", "Stop")},
    # 顶部有「继续」：主句不再叫人去闪购助手的面板上点
    "paused": {"flash-resume": ("继续", "Resume"),
               "flash-headline": ("已暂停：确认框里点了取消。", "Paused: a confirmation was canceled.")},
    "awaiting": {"flash-ctl-note": ("确认框处理完以后才能从这里停止。", "Stop works here once the confirmation is handled.")},
    "old": {"flash-ctl-note": ("闪购助手 0.8.0 起才能从这里开始、停止。", "Start and Stop need Flash Sale Helper 0.8.0 or later."),
            "flash-desc": ("这里只显示状态。要操作，请到闪购助手里。", "This panel only shows status. Make changes in Flash Sale Helper.")},
    # 胶囊也写「正在开始…」，不写「未开始」
    "starting": {"flash-start": ("正在开始…", "Starting…"), "flash-state": ("正在开始…", "Starting…"), "flash-ctl-note": None,
                 "flash-headline": ("正在开始…（闪购助手在刷新页面）", "Starting… (Flash Sale Helper is reloading the page)")},
    "skip": {"flash-headline": None},
    "rejected": {"flash-note": ("闪购助手没有接受：正忙，稍等再试。", "Flash Sale Helper declined: It’s busy. Try again in a moment.")},
    # 发布闸关着（出厂的样子）：只有停止，最底下那句只说停止
    "gated": {"flash-stop": ("停止", "Stop"), "flash-ctl-note": None,
              "flash-desc": ("这里只能停止，其他操作请到闪购助手里。", "Only Stop works here. Do everything else in Flash Sale Helper.")},
}
FLASH_SKIP_TEXT = (("1 号位，不续开", "Slot 1, not renewed"), ("下一个要置顶，不续开", "Next to pin, not renewed"),
                   ("没有库存，不续开", "Out of stock, not renewed"),
                   ("续开次数到上限", "Renewal limit reached"), ("没开自动开始", "Auto-start off"),
                   ("读不到序号，先不处理", "Position unread, skipped for now"))


def _tag(html, element_id):
    m = re.search(r'<[^>]*\bid="{}"[^>]*>'.format(re.escape(element_id)), html)
    return m.group(0) if m else ""


def _inner(html, element_id):
    """--dump-dom 里这个元素里面的字（去掉标签）。只用在不嵌套同名标签的元素上（按钮、p）。"""
    m = re.search(r'<([a-z]+)\b[^>]*\bid="{}"[^>]*>(.*?)</\1>'.format(re.escape(element_id)), html, re.S)
    return re.sub(r"<[^>]+>", "", m.group(2)) if m else None


def _flash_ctl_name(kind, lang):
    return "flash-ctl-" + kind + ("-zh" if lang == "zh" else "")


@needs_chrome
@pytest.mark.parametrize("lang", ("en", "zh"))
@pytest.mark.parametrize("kind", S.FLASH_CTL_KINDS)
def test_flash_panel_control_row_shows_what_the_state_says(dom, kind, lang):
    """闪购浮层顶部那一排在真浏览器里：浮层打开了（场景点了 #flash-btn），露出来、能点的按钮照 flash.js flashControlView 的规则，
    文案对（武装态「再点一次开始」、过渡「正在开始…」、版本门槛、确认框、409 那一句、最底下那句），开始后的过渡里顶栏按钮
    不变色，skip 的不续开原因和卡片序号画出来了、认不出的原因代码在 translate="no" 里、同一档按序号排。"""
    html = dom.scan(_flash_ctl_name(kind, lang))["dom"]
    problems = []

    def need(ok, what):
        if not ok:
            problems.append(what)

    need(not _hidden(html, "flash-panel"), "浮层没打开")
    want_shown, want_usable = FLASH_CTL_EXPECT[kind]
    for b in FLASH_CTL_BUTTONS:
        tag = _tag(html, "flash-" + b)
        need(tag, "找不到 #flash-{}".format(b))
        shown = "hidden" not in _classes(tag)
        usable = shown and not re.search(r"\sdisabled(=|\s|>)", tag)
        need(shown == (b in want_shown), "#flash-{} 应{}露出：{}".format(b, "" if b in want_shown else "不", tag))
        need(usable == (b in want_usable), "#flash-{} 应{}能点：{}".format(b, "" if b in want_usable else "不", tag))
    for element_id, texts in FLASH_CTL_TEXT[kind].items():
        if texts is None:
            need(_hidden(html, element_id) or not (_inner(html, element_id) or "").strip(),
                 "#{} 应该藏着：{}".format(element_id, _inner(html, element_id)))
            continue
        want = texts[0] if lang == "zh" else texts[1]
        need(not _hidden(html, element_id), "#{} 没露出".format(element_id))
        need(_inner(html, element_id) == want, "#{} 的字不对：{!r}，应为 {!r}".format(element_id, _inner(html, element_id), want))
    if kind == "starting":
        need("attn" not in _classes(_tag(html, "flash-btn")), "开始后的过渡里顶栏按钮不该变色")
        need("attn" not in _classes(_tag(html, "flash-headline")), "开始后的过渡里主句不该是红字")
    if kind == "skip":
        for zh, en in FLASH_SKIP_TEXT:
            need((zh if lang == "zh" else en) in html, "不续开原因没画出来：{}".format(zh if lang == "zh" else en))
        need(re.search(r'<span[^>]*translate="no"[^>]*>brand_new_reason</span>', html), "认不出的原因代码不在 translate=\"no\" 里")
        order = [m.group(1) for m in re.finditer(r'<span class="flash-pos">#(\d+)</span>', html)]
        need(order == ["1", "6", "2", "4", "5", "3", "9"], "卡片序号或排序不对：{}".format(order))
        need("attn" not in _classes(_tag(html, "flash-btn")), "标了不续开的已停止不算卡住，顶栏按钮不该变色")
    assert not problems, "\n".join(problems)


@needs_chrome
@needs_mac
@pytest.mark.parametrize("lang", ("en", "zh"))
@pytest.mark.parametrize("kind", S.FLASH_CTL_KINDS)
def test_flash_panel_control_row_fits_at_the_default_window(dom, kind, lang):
    """默认窗口 1000px：闪购浮层不出窗口、不横向溢出；顶部那一排（最宽的是英文的 Click Again to Start、
    Open Flash Sale Helper）不溢出、按钮都在浮层里、露出来的排在同一行、字不被截；旁边那句、结果那句、主句、商品表不横向溢出。
    顶栏照 test_live_top_bar_fits 的下限（1000px 品牌标签不窄于 100px），一条都不放宽：点开浮层不该挤到顶栏。"""
    result = dom.scan(_flash_ctl_name(kind, lang))
    got, view = result["measure"], result["viewport"]
    problems = []

    def need(ok, what):
        if not ok:
            problems.append(what)

    need(view["width"] == S.FLASH_CTL_SIZE[0], "窗口不是 {}px 宽：{}".format(S.FLASH_CTL_SIZE[0], view))
    panel, row = got["#flash-panel"], got["#flash-ctl"]
    need(panel and panel["width"] > 0, "浮层没打开 {}".format(panel))
    need(panel and panel["right"] <= view["width"] + 0.5, "浮层出了窗口 {}".format(panel))
    need(panel and panel["scrollWidth"] <= panel["clientWidth"] + 1, "浮层横向溢出 {}".format(panel))
    need(row and row["scrollWidth"] <= row["clientWidth"] + 1, "#flash-ctl 横向溢出 {}".format(row))
    buttons = {b: got["#flash-" + b] for b in FLASH_CTL_BUTTONS if got.get("#flash-" + b) and got["#flash-" + b]["width"] > 0}
    need(set(buttons) == set(FLASH_CTL_EXPECT[kind][0]), "露出来的按钮 {} 不对".format(sorted(buttons)))
    for b, m in buttons.items():
        need(row and m["right"] <= row["right"] + 0.5, "#flash-{} 出了这一排 {}".format(b, m))
        need(m["scrollWidth"] <= m["clientWidth"] + 1, "#flash-{} 的字被截 {}".format(b, m))
    need(len({round(m["top"]) for m in buttons.values()}) <= 1, "这一排没排在同一行：{}".format(
        {b: m["top"] for b, m in buttons.items()}))
    for sel in ("#flash-ctl-note", "#flash-note", "#flash-headline", "#flash-products"):
        m = got.get(sel)
        need(not m or m["width"] == 0 or m["scrollWidth"] <= m["clientWidth"] + 1, "{} 横向溢出 {}".format(sel, m))
    bar, brand = got[".topbar"], got["#active-brand-tag"]
    need(bar and bar["scrollWidth"] <= bar["clientWidth"], ".topbar 溢出 {}".format(bar))
    need(brand and brand["width"] >= 100, "#active-brand-tag 窄于 100px {}".format(brand))
    assert not problems, "\n".join(problems)


# 中文页的「浮窗」按钮（web/style.css 的 html[lang^="zh"] 那一段，docs/i18n-style.md §4.2）：
# 宽度 -> (两个字露出, 图标露出)。1000–1100 和 1140–1399 去图标露字；1101 起「手机同看」露出文字，
# 按钮带字会超出余量，仍是只有图标；1400 起图标加文字
def _body_classes(html):
    """--dump-dom 里 <body> 开标签上的类（顺序不论）。"""
    tag = re.search(r"<body\b[^>]*>", html)
    return _classes(tag.group(0)) if tag else []


ZH_FLOAT_SHAPE = {1000: (True, False), 1100: (True, False), 1101: (False, True),
                  1140: (True, False), 1399: (True, False), 1400: (True, True)}


@needs_chrome
@needs_mac
@pytest.mark.parametrize("base", S.ZH_QUIET_BASES)
@pytest.mark.parametrize("width", S.ZH_FLOAT_WIDTHS)
def test_zh_float_button_shows_its_label_where_it_fits(dom, base, width):
    """报警关着的中文直播顶栏（品牌 Allura Belt Nalla，和 §4.2 余量表的基线品牌同宽）：
    顶栏不溢出、状态字不被截（中文页的品牌标签只比状态胶囊先缩一倍，余量为负时主播名先被截）、品牌标签下限同英文页、
    「浮窗」两个字和图标露不露出符合表里的宽度范围，只有字的按钮约 46px 宽。
    -extra 是同一场景、当前目标是四种以外的 id，下拉约 144px：§4.2 的余量表按它算，1101px 只有图标时只剩约 +2px
    （web/style.css 1101–1139px 再收 8px 滑块就是在 144px 的下拉上量出来的）。"""
    result = dom.scan("{}@{}".format(base, width))
    got = result["measure"]
    label_shown, icon_shown = ZH_FLOAT_SHAPE[width]
    problems = []

    def need(ok, what):
        if not ok:
            problems.append(what)

    need(result["viewport"]["width"] == width, "窗口不是 {}px 宽：{}".format(width, result["viewport"]))
    bar, status, button = got[".topbar"], got[".status-text"], got["#float-btn"]
    label, icon = got["#float-btn .btn-label"], got["#float-btn .i"]
    need(bar and bar["scrollWidth"] <= bar["clientWidth"], ".topbar 溢出 {}".format(bar))
    need(status and status["scrollWidth"] <= status["clientWidth"] + 1, ".status-text 被截 {}".format(status))
    need(status and status["textWidth"] <= status["innerWidth"] + 0.01,
         ".status-text 文字比盒子宽（省略号已出现） {}".format(status))
    brand = got["#active-brand-tag"]
    floor = 100 if width == 1000 else 120 if 1101 <= width <= 1199 else 0
    need(brand and brand["width"] >= floor, "#active-brand-tag 窄于 {}px {}".format(floor, brand))
    need(button and button["width"] > 0, "浮窗按钮没露出 {}".format(button))
    # 视觉隐藏的文字是 1px 宽；露出来的两个 13px 汉字是 26px
    need(label and (label["width"] >= 20) == label_shown,
         "「浮窗」两个字应{}露出 {}".format("" if label_shown else "不", label))
    need(icon and (icon["width"] > 0) == icon_shown, "图标应{}露出 {}".format("" if icon_shown else "不", icon))
    if label_shown and not icon_shown:
        need(button and 40 <= button["width"] <= 52, "只有字的按钮应约 46px 宽 {}".format(button))
    body = _body_classes(result["dom"])
    need("has-float-btn" in body and "has-alert-tag" not in body, "body 的类不对（该有 has-float-btn、没有 has-alert-tag）：{}".format(body))
    if "flash" in base:          # 带闪购按钮的那两份：「浮窗」的露法同上，闪购按钮（露「闪购」两个字）和「Aa」另查
        problems.extend(_flash_shape_problems(got, width, result["viewport"], lang="zh"))
    problems.extend(_caption_menu_problems(dom, base, width, got))
    assert not problems, "\n".join(problems)


@needs_chrome
@needs_mac
@pytest.mark.parametrize("base", S.ZH_FULL_BASES)
def test_zh_float_button_gives_way_to_the_alert_tag(dom, base):
    """满编（报警开、长品牌名）的中文 1000px：「报警开」标签亮着，body 带 has-alert-tag，「浮窗」退回只有图标。
    不断言状态字不被截：这一档在没有浮窗按钮时就只有 −4px，只有图标的按钮再多 −12px（§4.2），这一条不是这次引入的。
    -extra 是同一场景、目标在四种以外，下拉约 144px（§4.2 的账按它算）。"""
    result = dom.scan("{}@1000".format(base))
    menu_problems = _caption_menu_problems(dom, base, 1000, result["measure"])
    assert not menu_problems, "\n".join(menu_problems)
    got = result["measure"]
    body = _body_classes(result["dom"])
    assert "has-alert-tag" in body and "has-float-btn" in body, "body 的类不对：{}".format(body)
    bar, label, icon = got[".topbar"], got["#float-btn .btn-label"], got["#float-btn .i"]
    brand = got["#active-brand-tag"]
    assert bar and bar["scrollWidth"] <= bar["clientWidth"], ".topbar 溢出 {}".format(bar)
    assert label and label["width"] < 20, "报警标签亮着，「浮窗」两个字该是视觉隐藏的 {}".format(label)
    assert icon and icon["width"] > 0, "报警标签亮着，图标该露出 {}".format(icon)
    assert brand and brand["width"] >= 100, "#active-brand-tag 窄于 100px {}".format(brand)
    if "flash" in base:          # 带闪购按钮的那两份：「闪购」同样退回只有图标，「Aa」替下字号控件
        flash_problems = _flash_shape_problems(got, 1000, result["viewport"], lang="zh", alert_tag=True)
        assert not flash_problems, "\n".join(flash_problems)


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


@needs_chrome
def test_float_page_keeps_its_chinese_data_in_the_data_areas(dom):
    """浮窗场景故意放了中文昵称（名字，translate="no"）和中文的弹幕译文（正文，class i18n-data）：
    它们确实画到了页面上，英文页的扫描不许把它们算作漏翻。"""
    result = dom.scan("float-live")
    html = result["dom"]
    for text in ("小明", S.CAPTION_ZH):
        assert text in html, "浮窗场景里的中文数据没画到页面上：{}".format(text)
        assert not [x for x in result["cjk"] if text in x["text"]], "数据区里的中文被计入了：{}".format(text)
    for text, named in (("小明", True), (S.CAPTION_ZH, False)):
        for tag in _holders(html, text):
            assert ('translate="no"' in tag) == named and ("i18n-data" in _classes(tag)) != named, tag


def float_kept_comments(messages):
    """浮窗页最后留下的弹幕，照 web/float.js 的 floatReduce：同一个 id 原地换，新的接在后面，只留最后
    FLOAT_COMMENTS_MAX 条；换主播清空（换场之后回放里更早的弹幕要丢掉那一条没有照抄，场景里没有换场）。
    上限从 float.js 里读，改了这里跟着变。"""
    keep = int(re.search(r"var FLOAT_COMMENTS_MAX = (\d+);", (WEB_DIR / "float.js").read_text("utf-8")).group(1))
    kept = []
    for msg in messages:
        if msg["type"] == "session_break":
            kept = []
        elif msg["type"] == "comment":
            ids = [c["id"] for c in kept]
            if msg["id"] in ids:
                kept[ids.index(msg["id"])] = msg
            else:
                kept = (kept + [msg])[-keep:]
    return kept


@needs_chrome
def test_float_page_shows_what_the_scenario_says(dom):
    """核对页面真的画出了红 / 琥珀 / 关注的标记、琥珀行的理由和没译成的角标（扫描绿了也可能只是什么都没画）。

    琥珀的理由（「说的是自己的遭遇」这一行，guard.js 的 guard-note）浮窗照中控页弹幕栏一样显示：琥珀是「拿不准」，
    没有这句，中控只看见一个「疑似指称假货或骗局」，分不出是攻击还是顾客在说自己的遭遇。只有一行小字。
    浮窗只留最近 5 条弹幕：场景里「句子很短，没有问号」那条（g3）比它们都早，不在窗口里，也就不该出现
    （以前这里写的正是它，G10 一跑就红）。"""
    html = dom.scan("float-live")["dom"]
    for cls in ("guard-alert", "guard-amber", "guard-marked", "guard-note"):
        assert cls in html, "弹幕里没有 {} 的行".format(cls)
    for text in ("Possible redirect (link)", "Describes the viewer’s own experience", "Model: no attack found",
                 "Not translated (API refusal)"):
        assert text in html, "页面上没有「{}」".format(text)
    assert "Short, with no question mark" not in html, "比最近 5 条都早的弹幕还留在浮窗里"
    assert "guard-mark-btn" not in html, "浮窗只显示，不该有加入关注名单的按钮"


@needs_chrome
@needs_mac
def test_float_page_fits_its_window(dom):
    """460×330 的默认窗口：不横向溢出，弹幕列表不超出窗口底边（放不下时被裁掉的是最旧的）。"""
    result = dom.scan("float-live")
    got, view = result["measure"], result["viewport"]
    problems = []
    for sel in (".f-root", "#f-caption", "#f-comments"):
        m = got.get(sel)
        if not m:
            problems.append("{} 量不到：{}".format(sel, m))
        elif m["scrollWidth"] > m["clientWidth"] + 1:
            problems.append("{} 横向溢出 {}".format(sel, m))
    comments = got.get("#f-comments")
    if comments and comments["bottom"] > view["height"] + 1:
        problems.append("弹幕列表超出窗口底边 {} > {}".format(comments["bottom"], view["height"]))
    assert not problems, "\n".join(problems)


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
        assert sc["page"] in PAGE_FILES and sc["size"][0] > 0, name
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


def test_bad_session_scenarios_carry_the_stream_notices(tmp_path):
    """不开 Chrome：坏场次的两个场景确实带着生产代码写的提示（覆盖率、无声、时钟跳变）、V2 到了上限那句
    重连状态，和让底部角标出现的 stats 数字。"""
    built = S.build(tmp_path)
    bad = built["live-bad"]["messages"]
    incidents = bad[0]["config"]["incidents"]
    assert incidents["session:audio_rate"]["text"].startswith("Only 30% of stream audio arrived in the "
                                                              "last 5 min.")
    assert incidents["session:clock_gap"]["text"]
    stats = [m for m in bad if m["type"] == "stats"]
    assert stats and stats[-1]["coverage_low"] is True and stats[-1]["coverage_pct"] == 31
    quiet = built["reconnect-no-audio"]["messages"][0]["config"]
    assert quiet["incidents"]["session:no_audio"]["text"].startswith("No stream audio for 5 min.")
    assert quiet["status"]["detail"].startswith("The same stream URL isn’t returning data.")


def test_guard_scenarios_carry_the_data_the_page_needs(tmp_path):
    """不开 Chrome：弹幕留意的场景确实带着守卫的状态、事件、名单、带标记的弹幕，和实时的那两条（时刻在页面真实
    时钟之后，顶部条才会弹出）；英文和中文对照页是同一批数据。"""
    built = S.build(tmp_path)
    assert set(S.GUARD_NAMES + S.GUARD_ZH) <= set(built)
    live = built["live-guard"]["messages"]
    config = live[0]["config"]
    assert config["guard_state"]["status"] == "rules" and config["guard_state"]["streamer"] == S.STREAMER
    assert len(config["guard_events"]) == 5 and len(config["guard_marked"]) == 3
    assert all(len(m["uid"]) == 19 for m in live if m["type"] == "comment" and m.get("uid"))
    replayed = [m for m in live if m["type"] == "comment" and m.get("replay")]
    assert {m["guard"]["lvl"] for m in replayed if "guard" in m} == {"alert", "amber"}
    fresh = [m for m in live if m["type"] in ("guard_event", "comment") and not m.get("replay")]
    assert fresh and all((m.get("ev") or m)["ts"] > S.NOW + 5 * 365 * 86400 for m in fresh)
    assert built["home-guard"]["messages"][0]["config"]["guard_state"]["status"] == "no_sdk"
    assert built["live-guard-tripped"]["messages"][0]["config"]["guard_state"]["status"] == "tripped"
    for name in S.GUARD_NAMES:
        assert built[name + "-zh"]["lang"] == "zh" and built[name]["lang"] == "en"
        assert built[name + "-zh"]["clicks"] == built[name]["clicks"]
        assert built[name + "-zh"]["messages"][0]["config"].get("guard_events") == \
            built[name]["messages"][0]["config"].get("guard_events")


def test_the_float_button_scenario_differs_from_live_full_only_by_the_config_key(tmp_path):
    built = S.build(tmp_path)
    full, float_ = built["live-full"], built["live-float"]
    assert full["messages"][0]["config"].get("float_window") is None
    assert float_["messages"][0]["config"]["float_window"] == {"available": True, "open": False}
    stripped = json.loads(json.dumps(float_))
    del stripped["messages"][0]["config"]["float_window"]
    stripped["name"] = full["name"]
    assert stripped == full
    for width in S.LIVE_WIDTHS + S.NARROW_WIDTHS:       # 每档宽度两个基础场景都有
        assert built["live-float@{}".format(width)]["size"][0] == width
        assert built["live-full@{}".format(width)]["size"][0] == width


def test_the_flash_scenarios_differ_from_their_base_only_by_the_config_key_and_the_first_click(tmp_path):
    """不开 Chrome：每个带闪购按钮的场景和它的底本只差 hello 里的 config.flash_sale 和打头的那一下 #flash-btn。
    flash_sale 是生产代码（flash_sale.StatusTracker）从契约样例 tests/fixtures/flash_sale/running.json 规整出来、
    计满 60 秒后的样子：有卡住的商品（后端排在最前）、有没看过的 ❌ 记录，按钮是变色的样子；倒计时的时刻在「现在」之后。
    每档宽度都有。"""
    built = S.build(tmp_path)
    pairs = {"live-flash": "live-float", "live-flash-zh": "live-float-zh", "live-flash-zh-quiet": "live-float-zh-quiet",
             "home-flash": "home-idle", "home-flash-zh": "home-zh"}
    now_ms = time.time() * 1000
    for name, base in pairs.items():
        sc = built[name]
        state = sc["messages"][0]["config"]["flash_sale"]
        assert state["conn"] == "ok" and state["attention"] == ["stuck"] and state["stuck_count"] == 1
        assert state["products"][0]["stuck"] is True and state["products"][0]["state"] == "stopped"
        assert any(a["level"] == "warn" and a["text"].startswith("❌") for a in state["alerts"])
        assert any(a["text"] == S.FLASH_ZH_ALERT for a in state["alerts"])
        live = [p for p in state["products"] if p["state"] == "live"]
        assert live and all(p["ends_ms"] > now_ms for p in live)
        assert sc["clicks"][0] == "#flash-btn"
        stripped = json.loads(json.dumps(sc))
        del stripped["messages"][0]["config"]["flash_sale"]
        stripped["name"] = base
        stripped["clicks"] = stripped["clicks"][1:]
        assert stripped == built[base], name
        assert "flash_sale" not in built[base]["messages"][0]["config"]
    assert set(S.DESKTOP_MEASURE) >= {"#flash-btn", "#flash-btn .btn-label", "#font-btn", "#font-ctl"}
    for base in S.FLASH_BASES:
        for width in S.LIVE_WIDTHS + S.NARROW_WIDTHS + S.FLASH_WIDE_WIDTHS:
            assert built["{}@{}".format(base, width)]["size"][0] == width
    for base in ("home-flash", "home-flash-zh"):
        for width in S.HOME_WIDTHS:
            assert built["{}@{}".format(base, width)]["size"][0] == width
    assert "live-flash" in S.DESKTOP_EN and "home-flash" in S.DESKTOP_EN


def test_the_flash_control_scenarios_differ_from_live_float_only_where_they_say(tmp_path):
    """不开 Chrome：flash-ctl-* 和它的底本（英文 live-float、中文满编 live-float-zh）只差 hello 里的 config.flash_sale、
    点击（只点开闪购浮层，armed 再点一下「开始」）、窗口（默认 1000px）、要量的元素，rejected 另在最后多一条
    flash_control_result。config.flash_sale 是 StatusTracker 规整的、attention 是 flash_sale.attention 算的；各种类的
    状态确实是它说的样子；每个种类在 FLASH_CTL_EXPECT / FLASH_CTL_TEXT 里都有预期；英文的几份都进了 G10 的扫描。
    gated 以外 hello 里都带开闸的 flash_start_enabled: true，gated 没有（出厂的样子）。"""
    from app import flash_sale

    built = S.build(tmp_path)
    assert set(FLASH_CTL_EXPECT) == set(FLASH_CTL_TEXT) == set(S.FLASH_CTL_KINDS)
    assert set(S.FLASH_CTL_NAMES) <= set(S.DESKTOP_EN) and set(S.FLASH_CTL_NAMES) <= set(EN_SCENARIOS)
    states = {}
    for kind in S.FLASH_CTL_KINDS:
        for lang, base in (("en", "live-float"), ("zh", "live-float-zh")):
            sc = built[_flash_ctl_name(kind, lang)]
            assert sc["lang"] == lang and sc["page"] == "desktop"
            assert sc["size"] == list(S.FLASH_CTL_SIZE) and sc["size"][0] == 1000
            assert sc["clicks"] == ["#flash-btn"] + (["#flash-start"] if kind == "armed" else [])
            assert set(S.FLASH_CTL_MEASURE) <= set(sc["measure"]) and set(S.DESKTOP_MEASURE) <= set(sc["measure"])
            state = sc["messages"][0]["config"]["flash_sale"]
            assert state["conn"] == "ok" and state["attention"] == flash_sale.attention(state), kind
            assert not [a for a in state["alerts"] if a["level"] == "error" or a["text"].startswith("❌")], kind
            assert any(a["text"] == S.FLASH_ZH_ALERT for a in state["alerts"]), kind
            states.setdefault(kind, state)
            assert state == states[kind], "{}：两种语言的状态不一样".format(kind)
            stripped = json.loads(json.dumps(sc))
            del stripped["messages"][0]["config"]["flash_sale"]
            gate = stripped["messages"][0]["config"].pop("flash_start_enabled", None)
            assert gate is (None if kind == "gated" else True), kind
            if kind == "rejected":
                assert stripped["messages"].pop() == S.FLASH_CTL_REJECT
            for key in ("name", "clicks", "size", "measure"):
                stripped[key] = built[base][key]
            assert stripped == built[base], kind
    versions = {kind: states[kind]["app_version"] for kind in S.FLASH_CTL_KINDS}
    assert versions.pop("old") == S.FLASH_CTL_OLD_VERSION and set(versions.values()) == {S.FLASH_CTL_VERSION}
    for kind in ("idle", "armed", "rejected", "old"):
        assert (states[kind]["running"], states[kind]["paused_reason"]) == (False, "not_started"), kind
    assert states["running"]["running"] is True and states["running"]["paused_reason"] is None
    for key in ("running", "paused_reason", "page", "control", "attention", "app_version", "stale"):
        assert states["gated"][key] == states["running"][key], "gated 和 running 只差发布闸：" + key
    assert states["paused"]["paused_reason"] == "user_interaction"
    assert states["awaiting"]["paused_reason"] == "awaiting_approval"
    starting = states["starting"]
    assert starting["control"] == {"action": "start", "phase": "waiting"} and flash_sale.starting(starting)
    assert starting["page"] == "error" and starting["stale"] is True and starting["running"] is False
    assert starting["attention"] == [], "开始后的过渡：页面出错、陈旧都不算要人处理"
    assert flash_sale.attention(dict(starting, control=None)) == ["stale", "page"], "过渡一过就该提醒"
    skip = states["skip"]
    assert skip["attention"] == [] and not any(p["stuck"] for p in skip["products"])
    assert {p["skip"] for p in skip["products"]} == {None, "slot_1", "next_to_pin", "no_stock", "max_renewals",
                                                     "auto_start_off", "no_position", "brand_new_reason"}
    assert [p["position"] for p in skip["products"]] == [1, 6, None, 2, 4, None, 5, 3, 9], "后端排好的顺序"
    for kind in set(S.FLASH_CTL_KINDS) - {"skip"}:
        assert all(p["position"] is None and p["skip"] is None for p in states[kind]["products"]), kind


def test_screenshots_turn_off_the_popover_animation(tmp_path, monkeypatch):
    """不开 Chrome：截图（TLT_DOM_SHOTS）那一次 Chrome 带 --force-prefers-reduced-motion，浮层不会截在弹出动画半路、
    半透明发灰（审查第 8 条：「再点确认」那张截图就是这样）；扫描那一次不带。"""
    site = _Site(WEB_DIR)
    sc = {"page": "desktop", "page_lang": "en", "lang": "en", "size": [1000, 760]}
    calls = []

    def fake_run(chrome, url, size, lang, profile, extra, finished):
        calls.append(list(extra))
        site.served.extend((path, 200) for path in site.assets(page_html(sc)))
        return SCAN_DOM

    monkeypatch.setattr(sys.modules[__name__], "run_chrome", fake_run)
    try:
        runner = _Runner(site, "chrome", {"x": sc}, tmp_path, shots=tmp_path / "shots")
        runner.scan("x")
    finally:
        site.server_close()
    assert len(calls) == 2
    assert calls[0] == ["--dump-dom"]
    assert "--force-prefers-reduced-motion" in calls[1] and "--hide-scrollbars" in calls[1]
    assert calls[1][-1] == "--screenshot={}".format(tmp_path / "shots" / "x.png")


def test_the_home_scenarios_carry_a_target_inside_and_one_outside_the_four(tmp_path):
    """不开 Chrome：首页顶栏那条 G10 的两个基础场景。home-idle 是英文页、目标 en（四种之一）；home-zh 是中文页、
    目标 id（四种以外，web/live-ui.js targetLangExtra 要补的那种）。每档宽度都有，补出来的那一项量得到。"""
    built = S.build(tmp_path)
    four = ("zh-CN", "zh-TW", "en", "es")
    idle, zh = built["home-idle"], built["home-zh"]
    assert (idle["lang"], zh["lang"]) == ("en", "zh")
    assert idle["messages"][0]["config"]["target_lang"] in four
    assert zh["messages"][0]["config"]["target_lang"] == S.HOME_ZH_TARGET
    assert S.HOME_ZH_TARGET not in four
    assert zh["messages"][0]["config"]["status"]["state"] == "idle"
    for base in ("home-idle", "home-zh"):
        for width in S.HOME_WIDTHS:
            sc = built["{}@{}".format(base, width)]
            assert sc["size"][0] == width and S.TARGET_EXTRA_SELECTED in sc["measure"]


def test_the_hint_scenarios_differ_from_their_base_only_by_size_target_and_the_one_pick(tmp_path):
    """不开 Chrome：「字幕译成」就地提示的两个场景就是窗口模式的直播中场景，只改了窗口宽、当前目标，
    点击换成在下拉里选另一种界面语言那一步；英文那个进扫描。"""
    built = S.build(tmp_path)
    assert set(S.DESKTOP_MEASURE) >= set(S.HINT_MEASURE)
    assert "live-hint" in S.DESKTOP_EN and "live-hint-zh" not in S.DESKTOP_EN
    for name, (base, target, pick) in S.HINTS.items():
        sc = copy.deepcopy(built[name])
        assert sc["clicks"] == [{"select": "#target-lang", "value": pick}]
        assert sc["size"] == [S.HINT_WIDTH, built[base]["size"][1]]
        assert sc["messages"][0]["config"]["target_lang"] == target != pick
        assert sc["messages"][0]["config"]["status"]["state"] == "live"
        assert sc["messages"][0]["config"]["ui_lang_available"] is True
        assert sc["messages"][0]["config"]["ui_lang_locked"] is False
        sc["messages"][0]["config"]["target_lang"] = built[base]["messages"][0]["config"]["target_lang"]
        for key in ("name", "size", "clicks"):
            sc[key] = built[base][key]
        assert sc == built[base], name


def test_the_home_scenarios_show_the_language_switch(tmp_path):
    """不开 Chrome：首页的场景都带着界面语言行的几个键（ui_lang_available 为 true、没被 --ui-lang 固定），
    页面才会露出两段切换给 G10 量；按下的语言就是场景页面的语言。"""
    built = S.build(tmp_path)
    assert set(S.DESKTOP_MEASURE) >= set(S.HOME_LANG_MEASURE)
    for base in S.HOME_BASES:
        for width in S.HOME_WIDTHS:
            sc = built["{}@{}".format(base, width)]
            config = sc["messages"][0]["config"]
            assert config["ui_lang_available"] is True and config["ui_lang_locked"] is False, sc["name"]
            assert config["ui_lang"] == sc["lang"], sc["name"]


def test_every_live_top_bar_scenario_has_a_copy_with_the_widest_caption_menu(tmp_path):
    """不开 Chrome：直播中量顶栏的每个基础场景（live-full、live-float、live-float-zh、live-float-zh-quiet）都有一份
    名字带 S.EXTRA 后缀的，只差 hello 里的 target_lang——四种以外的 id，页面给下拉补一项 Bahasa Indonesia，
    是它最宽的样子。2026-10-20 以前 13 种一直都在下拉里，这些场景量的都是这个宽度；只列四种以后，
    基础场景的目标是 en，少量了约 28px。之后的消息里也没有 config 改目标（不然页面会把补的那项撤掉），
    每档宽度两份都有，补出来的那一项和下拉都量得到。"""
    built = S.build(tmp_path)
    four = ("zh-CN", "zh-TW", "en", "es")
    assert S.EXTRA_TARGET not in four and S.HOME_ZH_TARGET == S.EXTRA_TARGET
    groups = ((S.LIVE_BASES, S.LIVE_WIDTHS + S.NARROW_WIDTHS), (S.ZH_FULL_BASES, (1000,)),
              (S.ZH_QUIET_BASES, S.ZH_FLOAT_WIDTHS))
    plain_names = []
    for bases, widths in groups:
        plain = [b for b in bases if not b.endswith(S.EXTRA)]
        assert sorted(bases) == sorted(plain + [b + S.EXTRA for b in plain]), bases
        plain_names += plain
        for name in bases:
            for width in widths:
                sc = built["{}@{}".format(name, width)]
                assert sc["size"][0] == width
                assert {S.TARGET_EXTRA_SELECTED, "#target-lang"} <= set(sc["measure"])
    assert plain_names == ["live-full", "live-float", "live-flash", "live-float-zh", "live-flash-zh",
                           "live-float-zh-quiet", "live-flash-zh-quiet"]
    for name in plain_names:
        plain, extra = built[name], built[name + S.EXTRA]
        assert plain["messages"][0]["type"] == extra["messages"][0]["type"] == "hello"
        assert plain["messages"][0]["config"]["target_lang"] in four
        assert extra["messages"][0]["config"]["target_lang"] == S.EXTRA_TARGET
        same = json.loads(json.dumps(extra))
        same["name"] = plain["name"]
        same["messages"][0]["config"]["target_lang"] = plain["messages"][0]["config"]["target_lang"]
        assert same == plain
        assert not [m for m in extra["messages"][1:] if m.get("type") == "config" and "target_lang" in m]


def test_the_chinese_float_scenarios_differ_from_the_chinese_control_only_where_they_say(tmp_path):
    """中文页的浮窗场景（顶栏「浮窗」两个字的 G10）：live-float-zh 是 live-full-zh 加 config.float_window；
    live-float-zh-quiet 再把报警关掉、品牌换成和 §4.1 基线同宽的 Allura Belt Nalla，别的一样。"""
    built = S.build(tmp_path)
    control, zh, quiet = built[S.CONTROL_ZH], built["live-float-zh"], built["live-float-zh-quiet"]
    assert zh["lang"] == quiet["lang"] == "zh" and zh["page"] == quiet["page"] == "desktop"
    assert control["messages"][0]["config"]["alerts_enabled"] is True        # 满编：报警开着
    assert zh["messages"][0]["config"]["float_window"] == {"available": True, "open": False}
    stripped = json.loads(json.dumps(zh))
    del stripped["messages"][0]["config"]["float_window"]
    stripped["name"] = control["name"]
    assert stripped == control
    assert quiet["messages"][0]["config"]["float_window"] == {"available": True, "open": False}
    assert quiet["messages"][0]["config"]["alerts_enabled"] is False
    assert quiet["messages"][0]["config"]["active_brand"] == {"id": S.BRAND_ID, "name": S.ZH_QUIET_BRAND}
    rest = json.loads(json.dumps(quiet))
    for key, value in (("alerts_enabled", True), ("active_brand", zh["messages"][0]["config"]["active_brand"])):
        rest["messages"][0]["config"][key] = value
    rest["name"] = zh["name"]
    assert rest == zh
    assert built["live-float-zh@1000"]["size"][0] == 1000
    assert built["live-float-zh@1000"]["messages"][0]["config"]["alerts_enabled"] is True
    for width in S.ZH_FLOAT_WIDTHS:
        sc = built["live-float-zh-quiet@{}".format(width)]
        assert sc["size"][0] == width and sc["messages"][0]["config"]["alerts_enabled"] is False
    assert set(S.ZH_FLOAT_WIDTHS) == set(ZH_FLOAT_SHAPE)       # 每一档宽度都有预期的样子
    for selector in ("#float-btn", "#float-btn .btn-label", "#float-btn .i", ".topbar", ".status-text"):
        assert selector in built["live-float-zh@1000"]["measure"]


def test_float_scenario_is_what_the_float_connection_really_receives(tmp_path):
    """不开 Chrome：浮窗场景的消息是 /ws?role=float 真的会收到的那些——hello 只带浮窗用得上的键，回放里没有报警，
    类型都在 FLOAT_TYPES 里；数据里有红 / 琥珀 / 关注的弹幕、中文昵称和没译成的最新字幕。"""
    from app.server import FLOAT_CONFIG_KEYS, FLOAT_TYPES
    sc = S.build(tmp_path)["float-live"]
    assert sc["page"] == "float" and sc["lang"] == "en" and sc["size"] == list(S.FLOAT)
    msgs = sc["messages"]
    assert msgs[0]["type"] == "hello" and set(msgs[0]["config"]) <= set(FLOAT_CONFIG_KEYS)
    assert msgs[0]["config"]["caption_size"] == 40 and msgs[0]["config"]["guard_marked"]
    assert {m["type"] for m in msgs[1:]} <= set(FLOAT_TYPES)
    assert not [m for m in msgs if m["type"].startswith("alert")]
    assert {m["guard"]["lvl"] for m in msgs if m["type"] == "comment" and "guard" in m} == {"alert", "amber"}
    assert any(m.get("user") == "小明" for m in msgs if m["type"] == "comment")
    assert msgs[-1]["translate_state"] == "failed" and msgs[-1]["refused"] is True
    assert [m["restore"] for m in msgs if m["type"] == "caption" and m.get("restore")]   # 回放的最后一条带 restore
    assert sc["measure"] == list(S.FLOAT_MEASURE)
    # test_float_page_shows_what_the_scenario_says 找的字要真的在浮窗留下的那几条里：琥珀行带理由（grievance，
    # 外加模型意见），「句子很短，没有问号」（maybe_question）那条已经被挤出去了
    kept = float_kept_comments(msgs)
    guards = [m["guard"] for m in kept if m.get("guard")]
    assert {(g["lvl"], g["amb"], g["rv"]) for g in guards if g["lvl"] == "amber"} >= {("amber", "grievance", "neutral")}
    assert "maybe_question" not in {g["amb"] for g in guards}
    assert "maybe_question" in {m["guard"]["amb"] for m in msgs if m["type"] == "comment" and m.get("guard")}
    assert {g["cats"][0] for g in guards if g["lvl"] == "alert"} >= {"promo_link"}
    assert any(m.get("user") == "小明" for m in kept)


def test_float_kept_comments_follows_the_page_rules():
    def c(cid, **extra):
        return dict({"type": "comment", "id": cid}, **extra)
    msgs = [c(i) for i in "abcdef"] + [c("c", text="again"), {"type": "comment_update", "id": "d"}]
    assert [m["id"] for m in float_kept_comments(msgs)] == list("bcdef")
    assert float_kept_comments(msgs)[1]["text"] == "again"
    assert [m["id"] for m in float_kept_comments(msgs + [{"type": "session_break"}, c("g")])] == ["g"]


@pytest.mark.parametrize("page, lang, html_tag", [
    ("desktop", "en", '<html lang="en">'),
    ("desktop", "zh", '<html lang="zh-CN">'),
    ("float", "en", '<html lang="en">'),
    ("float", "zh", '<html lang="zh-CN">'),
    ("phone", "en", '<html lang="en" data-i18n="on">'),
    ("phone", "zh", '<html lang="zh-CN" data-i18n="on">'),
])
def test_page_gets_the_fixtures_around_its_own_scripts(page, lang, html_tag):
    name, base, first_js, last_js = PAGE_FILES[page]
    html = page_html({"page": page, "page_lang": lang})
    original = (WEB_DIR / name).read_text(encoding="utf-8")
    assert html.count(html_tag) == 1
    for tag in re.findall(r'<script src="[^"]+"></script>', original):
        assert html.count(tag) == 1, tag                   # 原来的脚本（连 ?v=）一个不少
    first = _only_tag(original, base + first_js)
    last = _only_tag(original, base + last_js)
    fixtures = "".join(_script_tag(base + n) for n in ("probe.js", "scenario.js", "fake_ws.js"))
    assert fixtures + first in html
    assert last + _script_tag(base + "probe.js") + _script_tag(base + "scan.js") in html
    stripped = html
    for n in ("probe.js", "scenario.js", "fake_ws.js", "scan.js"):
        stripped = stripped.replace(_script_tag(base + n), "")
    assert stripped == i18n.inject_lang(original, lang, marker=page == "phone")   # 别的字节不动；只有手机页带 data-i18n


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
