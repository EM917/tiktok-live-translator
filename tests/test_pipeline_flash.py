"""顶栏「闪购」的后端接线（app/pipeline_flash.py）：启动检测、轮询节奏、去重广播、hello 补发、
「打开闪购助手」、手机和浮窗收不到、终端和审计在两种界面语言下逐字节相同。

绝不碰本机真的闪购助手：程序位置一律用 tmp_path 注入（tests/conftest.py 另把默认位置换成了不存在的路径），
抓取函数换成假的，或指到本机临时端口上的假服务器；open 换成记账的替身，不真的执行。
"""
import ast
import asyncio
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app import audit
from app import flash_sale as fs
from app import i18n, settings, viewer
from app.i18n import CJK
from app.pipeline import Pipeline
from app.server import FLOAT_CONFIG_KEYS, FLOAT_TYPES, CaptionServer
from app.window_close import close_session

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "flash_sale"
REAL_FETCH = fs.fetch_status
# 导入时留下真的那个（导入、收集阶段不在 conftest 的任何夹具里）：用例里拿它算出本机真的密钥位置，和换过的比
REAL_CONTROL_TOKEN_PATH = fs.control_token_path
START_WALL = datetime(2026, 10, 27, 21, 36, 8, tzinfo=timezone.utc).timestamp()


def sample(name):
    return json.loads((FIXTURES / (name + ".json")).read_text(encoding="utf-8"))


def iso(wall):
    return datetime.fromtimestamp(wall, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class RecordingServer(CaptionServer):
    """真的 CaptionServer（G9 运行时网照样查生产代码发出的每一条），另记下发过什么。"""

    def __init__(self):
        super().__init__(port=0)
        self.sent = []

    async def broadcast(self, msg):
        self.sent.append(msg)
        return await super().broadcast(msg)

    def of_type(self, mtype):
        return [m for m in self.sent if m.get("type") == mtype]


def bare_pipeline():
    p = Pipeline.__new__(Pipeline)
    p.server = RecordingServer()
    p._bg_tasks = set()
    p._init_flash_sale()
    return p


def installed_app(tmp_path):
    app = tmp_path / "Applications" / fs.APP_NAME
    app.mkdir(parents=True)
    return app


class Done(Exception):
    """假 sleep 在剧本用完时抛它，结束轮询循环。"""


class Script:
    """假的抓取和睡眠：fetch 按顺序交出剧本里的结果——dict 是一份 /status（updated_at 盖成此刻的墙钟，
    当作闪购助手每 2 秒刷新一次），元组原样交出，异常就抛；sleep 记下间隔、把墙钟和单调时钟一起拨快，剧本用完就停。
    后端只读单调时钟（_flash_mono）；墙钟只用来给 updated_at 盖时刻。"""

    def __init__(self, p, steps, wall=START_WALL, mono=1000.0):
        self.steps = list(steps)
        self.sleeps, self.urls, self.seen_config = [], [], []
        self.wall, self.mono = wall, mono
        self.p = p
        p._flash_fetch = self.fetch
        p._flash_sleep = self.sleep
        p._flash_mono = lambda: self.mono

    async def fetch(self, url):
        self.urls.append(url)
        self.seen_config.append(copy.deepcopy(self.p.server.config.get("flash_sale")))
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, dict):
            doc = copy.deepcopy(step)
            doc["updated_at"] = iso(self.wall)
            return "ok", doc
        return step

    async def sleep(self, sec):
        self.sleeps.append(sec)
        self.wall += sec
        self.mono += sec
        if not self.steps:
            raise Done()


def run_watch(p):
    async def go():
        with pytest.raises(Done):
            await p._flash_watch()
    asyncio.run(go())


def out_lines(capsys):
    return [line for line in capsys.readouterr().out.splitlines() if line.strip()]


# ---- 构造与启动检测 ---------------------------------------------------------------------------

def full_pipeline(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(settings, "_corrupt", {"backup": None, "announced": False})
    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source="es", beam=5, context=False,
        asr_temperature=None, glossary=None, backend="auto", model=None, device="auto",
        compute_type="auto", denoise="off", banned_terms=None, comments=False)
    return Pipeline(args, RecordingServer())


def test_constructing_a_pipeline_never_looks_for_the_helper(monkeypatch, tmp_path):
    """检测只在 main.py 调 attach_flash_sale 时做：就算这台机器上真装着，构造 Pipeline 也不写键、不起任务。"""
    app = installed_app(tmp_path)
    monkeypatch.setattr(fs, "app_candidates", lambda home=None: (app,))
    looked = []
    monkeypatch.setattr(fs, "find_app", lambda *a, **k: looked.append(1))
    p = full_pipeline(monkeypatch, tmp_path)
    assert "flash_sale" not in p.server.config
    assert p._flash_app is None and p._flash_task is None and looked == []
    assert p._flash_url != "http://127.0.0.1:9334/status"     # tests/conftest.py 的网


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_off_macos_the_feature_does_not_exist(platform, tmp_path, capsys):
    p = bare_pipeline()
    asyncio.run(p.attach_flash_sale(platform=platform, candidates=[installed_app(tmp_path)]))
    assert "flash_sale" not in p.server.config and p._flash_task is None and p._flash_app is None
    assert out_lines(capsys) == []


def test_on_macos_without_the_app_the_feature_does_not_exist(tmp_path, capsys):
    p = bare_pipeline()
    asyncio.run(p.attach_flash_sale(platform="darwin", candidates=[tmp_path / fs.APP_NAME]))
    assert "flash_sale" not in p.server.config and p._flash_task is None
    assert p.handle_control({"type": "open_flash_helper"}) is None and not p._bg_tasks
    assert out_lines(capsys) == []


def test_installed_the_first_hello_already_says_checking(tmp_path, capsys):
    app = installed_app(tmp_path)
    p = bare_pipeline()
    script = Script(p, [sample("running")])

    async def go():
        await p.attach_flash_sale(platform="darwin", candidates=[tmp_path / "nope" / fs.APP_NAME, app])
        # 第一次轮询之前，config 里已经有这个键：第一次 hello 就带着，按钮不会晚一拍冒出来
        assert p.server.config["flash_sale"] == fs.empty_state("checking")
        assert p._flash_app == app and p._flash_task is not None
        with pytest.raises(Done):
            await p._flash_task

    asyncio.run(go())
    assert script.seen_config[0]["conn"] == "checking"
    assert script.urls == ["http://127.0.0.1:9/status"]
    assert p.server.config["flash_sale"]["conn"] == "ok"
    lines = out_lines(capsys)
    assert lines[0] == "[信息] 闪购助手：已装，开始查看 127.0.0.1:9 的状态"
    assert lines[1] == "[信息] 闪购助手：已连上 127.0.0.1:9（版本 0.7.2）"


def test_an_error_while_attaching_never_blocks_startup(monkeypatch, tmp_path, capsys):
    def boom(**_kw):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(fs, "find_app", boom)
    p = bare_pipeline()
    asyncio.run(p.attach_flash_sale(platform="darwin", candidates=[installed_app(tmp_path)]))
    assert "flash_sale" not in p.server.config and p._flash_task is None
    assert out_lines(capsys) == ["[警告] 闪购助手：启动时出错（RuntimeError），这一次不显示闪购按钮"]


def test_stop_flash_watch_cancels_the_poll_and_is_reentrant(tmp_path):
    p = bare_pipeline()
    started = []

    async def blocking_fetch(url):
        started.append(url)
        await asyncio.Event().wait()

    p._flash_fetch = blocking_fetch

    async def go():
        await p.attach_flash_sale(platform="darwin", candidates=[installed_app(tmp_path)])
        task = p._flash_task
        await asyncio.sleep(0.01)
        assert started and not task.done()
        p.stop_flash_watch()
        p.stop_flash_watch()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert p._flash_task is None

    asyncio.run(go())


def test_a_bare_pipeline_stop_is_harmless():
    bare_pipeline().stop_flash_watch()


# ---- 轮询：节奏、去重、终端 ------------------------------------------------------------------

def test_only_changes_are_broadcast_and_the_rhythm_is_three_or_fifteen_seconds(capsys):
    p = bare_pipeline()
    changed = sample("running")
    changed["products"][0]["stock_left"] = 29
    script = Script(p, [sample("running"), sample("running"), changed, ("unreachable", None),
                        ("unreachable", None), ("unreachable", None), sample("running")])
    p._flash_conn_prev = "checking"
    run_watch(p)
    sent = p.server.of_type("flash_sale")
    assert [m["conn"] for m in sent] == ["ok", "ok", "unreachable", "ok"]
    # 连上以后第一次没查到先照旧、3 秒后再查；连续第二次才发布「连不上」，之后每 15 秒
    assert script.sleeps == [3, 3, 3, 3, 15, 15, 3]
    assert p.server.config["flash_sale"] == {k: v for k, v in sent[-1].items() if k != "type"}
    assert sent[2]["products"] == [] and sent[2]["alerts"] == []        # 连不上：不留旧数据
    assert all("updated_at" not in m and "_schema" not in m and "_updated_s" not in m for m in sent)
    assert out_lines(capsys) == [
        "[信息] 闪购助手：已连上 127.0.0.1:9（版本 0.7.2）",
        "[信息] 闪购助手：连不上 127.0.0.1:9，之后每 15 秒查一次",
        "[信息] 闪购助手：已连上 127.0.0.1:9（版本 0.7.2）",
    ]


def test_a_product_stuck_for_sixty_seconds_is_broadcast_once_when_it_flips():
    p = bare_pipeline()
    script = Script(p, [sample("running")] * 22)
    run_watch(p)
    sent = p.server.of_type("flash_sale")
    assert len(sent) == 2                         # 第一次，和 60 秒后 stuck 翻成 true 的那一次
    assert sent[0]["attention"] == [] and sent[1]["attention"] == ["stuck"]
    assert sent[1]["stuck_count"] == 1 and sent[1]["products"][0]["stuck"] is True
    assert script.mono - 1000.0 >= 60


def test_bad_responses_are_logged_when_the_reason_changes_and_schema_once(capsys):
    p = bare_pipeline()
    p._flash_conn_prev = "checking"
    Script(p, [("bad_response", "http_404"), ("bad_response", "http_404"), ("bad_response", "json"),
               ("ok", [1]), dict(sample("running"), schema=2), dict(sample("running"), schema=2)])
    run_watch(p)
    sent = p.server.of_type("flash_sale")
    assert [(m["conn"], m["why"]) for m in sent] == [("bad_response", "http_404"), ("bad_response", "json"),
                                                     ("bad_response", "not_object"), ("ok", None)]
    assert sent[0]["attention"] == ["bad_response"]
    assert out_lines(capsys) == [
        "[警告] 闪购助手：http://127.0.0.1:9/status 返回的内容读不出（http_404）",
        "[警告] 闪购助手：http://127.0.0.1:9/status 返回的内容读不出（json）",
        "[警告] 闪购助手：http://127.0.0.1:9/status 返回的内容读不出（not_object）",
        "[信息] 闪购助手：已连上 127.0.0.1:9（版本 0.7.2）",
        "[信息] 闪购助手：/status 的 schema 是 2，按 1 尽量读",
    ]


def test_a_helper_that_connects_but_does_not_answer_turns_the_button_orange(capsys):
    """卡住的闪购助手：端口连得上、不回话。连续两次才发布（一次可能只是抖动），发布的是 bad_response / no_answer，
    attention 非空（按钮变色），终端只写看到的（没有回话），不说它没在运行。恢复了照常连上。"""
    p = bare_pipeline()
    p._flash_conn_prev = "checking"
    script = Script(p, [sample("running"), ("bad_response", "no_answer"), ("bad_response", "no_answer"),
                        ("bad_response", "no_answer"), sample("running")])
    run_watch(p)
    sent = p.server.of_type("flash_sale")
    assert [(m["conn"], m["why"]) for m in sent] == [("ok", None), ("bad_response", "no_answer"), ("ok", None)]
    assert sent[1]["attention"] == ["bad_response"] and sent[1]["products"] == []
    assert script.sleeps == [3, 3, 15, 15, 3]
    assert out_lines(capsys) == [
        "[信息] 闪购助手：已连上 127.0.0.1:9（版本 0.7.2）",
        "[警告] 闪购助手：http://127.0.0.1:9/status 没有回话（no_answer）",
        "[信息] 闪购助手：已连上 127.0.0.1:9（版本 0.7.2）",
    ]


def test_one_missed_poll_broadcasts_nothing_and_keeps_the_stuck_clock():
    """一次没查到：不广播、照 3 秒再查，卡住的计时照走。running 样例里停着的那个商品还是满 60 秒就报，
    不会因为中间一次抖动清零、再推迟一分多钟。"""
    p = bare_pipeline()
    steps = [sample("running")] * 19 + [("bad_response", "no_answer")] + [sample("running")] * 2
    script = Script(p, steps)
    run_watch(p)
    sent = p.server.of_type("flash_sale")
    assert len(sent) == 2 and sent[1]["attention"] == ["stuck"]
    assert set(script.sleeps) == {3}
    # 第 21 次（60 秒那一跳）就报出来：第 21 次查之前 config 里还没卡住，第 22 次查之前已经是了
    assert script.seen_config[20]["attention"] == [] and script.seen_config[21]["attention"] == ["stuck"]


def test_an_exception_in_one_poll_does_not_stop_the_loop(capsys):
    p = bare_pipeline()
    p._flash_conn_prev = "checking"
    script = Script(p, [RuntimeError("boom 1 secret"), RuntimeError("boom 2"), KeyError("k"),
                        sample("running")])
    run_watch(p)
    assert [m["conn"] for m in p.server.of_type("flash_sale")] == ["ok"]
    assert script.sleeps == [15, 15, 15, 3]
    lines = out_lines(capsys)
    assert lines == ["[警告] 闪购助手：查状态出错（RuntimeError），下一次照常",
                     "[警告] 闪购助手：查状态出错（KeyError），下一次照常",
                     "[信息] 闪购助手：已连上 127.0.0.1:9（版本 0.7.2）"]


def test_a_broadcast_that_fails_is_retried_on_the_next_poll():
    p = bare_pipeline()
    calls = []
    real = p.server.broadcast

    async def flaky(msg):
        calls.append(msg["type"])
        if len(calls) == 1:
            raise RuntimeError("page hung")
        return await real(msg)

    p.server.broadcast = flaky
    Script(p, [sample("running"), sample("running")])
    run_watch(p)
    assert calls == ["flash_sale", "flash_sale"]


def test_the_terminal_never_prints_titles_or_alert_texts(capsys):
    p = bare_pipeline()
    p._flash_conn_prev = "checking"
    Script(p, [sample("running"), sample("awaiting"), ("unreachable", None), sample("idle")])
    run_watch(p)
    out = capsys.readouterr().out
    for needle in ("Fictional", "example_shop", "1000000000000000", "Couldn", "专用 Chrome"):
        assert needle not in out, needle
    assert all(line.startswith(("[信息]", "[警告]")) for line in out.splitlines() if line.strip())


def test_the_real_fetch_against_a_local_fake_server(tmp_path):
    """整条链：真的 fetch_status → 本机临时端口上的假 /status（照契约查 Host）→ 规整 → config 和广播。"""
    body = (FIXTURES / "awaiting.json").read_bytes()

    async def handler(request):
        port = request.transport.get_extra_info("sockname")[1]
        if request.headers.get("Host") != "127.0.0.1:%d" % port:
            return web.Response(status=403)
        return web.Response(body=body, content_type="application/json")

    async def go():
        app = web.Application()
        app.router.add_get("/status", handler)
        server = TestServer(app, host="127.0.0.1")
        await server.start_server()
        try:
            p = bare_pipeline()
            p._flash_fetch = REAL_FETCH
            p._flash_url = "http://127.0.0.1:%d/status" % server.port
            assert server.port != 9334
            await p._flash_tick()
            return p
        finally:
            await server.close()

    p = asyncio.run(go())
    state = p.server.config["flash_sale"]
    assert state["conn"] == "ok" and state["paused_reason"] == "awaiting_approval"
    assert state["attention"][-1] == "paused"
    assert len(state["products"]) == 9 and len(state["alerts"]) == 4


# ---- 「打开闪购助手」 ----------------------------------------------------------------------

class FakeExec:
    def __init__(self, rc=0, raises=None, hang=False):
        self.rc, self.raises, self.hang = rc, raises, hang
        self.calls = []

    async def __call__(self, *argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.raises is not None:
            raise self.raises

        async def wait():
            if self.hang:
                await asyncio.sleep(30)
            return self.rc
        return SimpleNamespace(wait=wait)


def opening_pipeline(tmp_path, exe):
    p = bare_pipeline()
    p._flash_app = installed_app(tmp_path)
    p._flash_exec = exe
    clock = {"mono": 500.0}
    p._flash_mono = lambda: clock["mono"]
    return p, clock


def click(p, *msgs):
    async def go():
        for msg in msgs or ({"type": "open_flash_helper"},):
            assert p.handle_control(msg) is None
        await asyncio.gather(*list(p._bg_tasks))
    asyncio.run(go())


def test_open_runs_open_a_with_the_full_path_and_speeds_up_polling(tmp_path, capsys):
    exe = FakeExec(rc=0)
    p, clock = opening_pipeline(tmp_path, exe)
    p._flash_conn_prev = "unreachable"
    assert p._flash_interval() == fs.POLL_DOWN_SEC
    click(p)
    (argv, kwargs), = exe.calls
    assert list(argv) == ["open", "-a", str(p._flash_app)]
    assert kwargs == {"stdout": asyncio.subprocess.DEVNULL, "stderr": asyncio.subprocess.DEVNULL}
    assert p.server.sent == []                       # 成功不发提示：状态自己会变
    assert p._flash_wake_ev.is_set()                 # 马上查一次
    assert p._flash_interval() == fs.POLL_OK_SEC
    clock["mono"] += fs.BOOST_SEC - 0.1
    assert p._flash_interval() == fs.POLL_OK_SEC
    clock["mono"] += 0.2
    assert p._flash_interval() == fs.POLL_DOWN_SEC
    assert out_lines(capsys) == ["[信息] 闪购助手：执行 open -a {}".format(p._flash_app)]


def test_the_page_cannot_put_anything_into_argv(tmp_path):
    exe = FakeExec(rc=0)
    p, _clock = opening_pipeline(tmp_path, exe)
    click(p, {"type": "open_flash_helper", "argv": ["rm", "-rf", "/"], "path": "/bin/sh", "app": "x"})
    assert [list(a) for a, _k in exe.calls] == [["open", "-a", str(p._flash_app)]]


def test_a_non_zero_return_code_sends_one_flash_notice_and_no_status(tmp_path, capsys):
    exe = FakeExec(rc=1)
    p, _clock = opening_pipeline(tmp_path, exe)
    click(p)
    notices = p.server.of_type("notice")
    assert len(notices) == 1 and notices[0]["where"] == "flash"
    assert p.server.of_type("status") == [] and p.server.of_type("incident") == []
    assert notices[0]["text"] == "没能打开闪购助手（open 返回 1）。"
    with i18n.use("en"):
        en = i18n.render(notices[0])["text"]
    assert en == "Couldn’t open Flash Sale Helper (open returned 1)." and not CJK.search(en)
    assert p._flash_boost_until is None and not p._flash_opening
    assert out_lines(capsys)[-1] == "[警告] 闪购助手：open 返回 1，没有打开"


@pytest.mark.parametrize("exe_kwargs, line", [
    ({"raises": FileNotFoundError(2, "No such file")}, "[警告] 闪购助手：open 没能执行（FileNotFoundError）"),
    ({"hang": True}, "[警告] 闪购助手：open 等了 0.05 秒还没返回"),
])
def test_open_that_fails_or_hangs_sends_the_plain_notice(exe_kwargs, line, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(fs, "OPEN_TIMEOUT_SEC", 0.05)
    p, _clock = opening_pipeline(tmp_path, FakeExec(**exe_kwargs))
    click(p)
    (notice,) = p.server.of_type("notice")
    assert notice["where"] == "flash" and notice["text"] == "没能打开闪购助手。"
    with i18n.use("en"):
        assert i18n.render(notice)["text"] == "Couldn’t open Flash Sale Helper."
    assert out_lines(capsys)[-1] == line
    assert not p._flash_opening


def test_a_double_click_runs_open_only_once(tmp_path):
    gate = {}
    calls = []

    async def slow_exec(*argv, **kwargs):
        calls.append(argv)

        async def wait():
            await gate["ev"].wait()
            return 0
        return SimpleNamespace(wait=wait)

    p, _clock = opening_pipeline(tmp_path, slow_exec)

    async def go():
        gate["ev"] = asyncio.Event()
        assert p.handle_control({"type": "open_flash_helper"}) is None
        await asyncio.sleep(0.01)
        assert p.handle_control({"type": "open_flash_helper"}) is None
        assert len(p._bg_tasks) == 1
        gate["ev"].set()
        await asyncio.gather(*list(p._bg_tasks))
        assert len(calls) == 1 and not p._flash_opening
        assert p.handle_control({"type": "open_flash_helper"}) is None     # 返回之后再点照常
        await asyncio.gather(*list(p._bg_tasks))
        assert len(calls) == 2

    asyncio.run(go())


def test_the_real_sleep_returns_early_when_woken():
    p = bare_pipeline()

    async def go():
        loop = asyncio.get_running_loop()
        started = loop.time()
        sleeper = asyncio.ensure_future(p._flash_sleep(10))
        await asyncio.sleep(0.05)
        p._flash_wake()
        await sleeper
        assert loop.time() - started < 2
        assert not p._flash_wake_ev.is_set()
        await p._flash_sleep(0.01)                    # 没人叫醒：到点返回

    asyncio.run(go())


# ---- 隔离：手机、浮窗、控制页 -----------------------------------------------------------------

def published_state():
    tracker = fs.StatusTracker()
    doc = sample("running")
    doc["updated_at"] = iso(START_WALL)
    return tracker.update("ok", doc, 0.0)


def test_the_phone_never_gets_flash_sale():
    msg = dict(published_state(), type="flash_sale")
    assert "flash_sale" in viewer.DENY and "flash_sale" not in viewer.ALLOW
    assert viewer.filter_payload(msg) is None
    server = SimpleNamespace(config={"status": {"state": "live"}, "flash_sale": published_state()},
                             alerts=[], history=[], comments=[])
    blob = json.dumps(viewer.replay_snapshot(server), ensure_ascii=False)
    for needle in ("flash", "Fictional", "1000000000000000003", "attention"):
        assert needle not in blob, needle


def test_the_float_page_is_not_in_the_flash_whitelists():
    assert "flash_sale" not in FLOAT_TYPES and "flash_sale" not in FLOAT_CONFIG_KEYS


async def with_client(server, body):
    app = web.Application()
    app.router.add_get("/ws", server._ws)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        return await body(client)
    finally:
        await client.close()


async def drain(ws, wait=0.2):
    got = []
    while True:
        try:
            msg = await ws.receive_json(timeout=wait)
        except (asyncio.TimeoutError, TypeError, ValueError):
            return got
        got.append(msg)


def test_hello_carries_the_latest_state_to_the_control_page_only():
    """重连补发靠 hello：config 里放的是最新那份，控制页重连就拿到；浮窗的 hello 里没有这个键。"""
    p = bare_pipeline()
    state = published_state()
    asyncio.run(p._flash_publish(state))

    async def body(client):
        control = await client.ws_connect("/ws")
        float_ws = await client.ws_connect("/ws?role=float")
        hellos = (await control.receive_json(), await float_ws.receive_json())
        await control.close()
        await float_ws.close()
        return hellos

    control_hello, float_hello = asyncio.run(with_client(p.server, body))
    assert control_hello["config"]["flash_sale"] == json.loads(json.dumps(state))
    assert "flash_sale" not in float_hello["config"]


def test_a_flash_sale_broadcast_reaches_the_control_page_but_not_the_float_page():
    p = bare_pipeline()

    async def body(client):
        control = await client.ws_connect("/ws")
        float_ws = await client.ws_connect("/ws?role=float")
        await drain(control)
        await drain(float_ws)
        await p._flash_publish(published_state())
        got = (await drain(control), await drain(float_ws))
        await control.close()
        await float_ws.close()
        return got

    to_control, to_float = asyncio.run(with_client(p.server, body))
    assert [m["type"] for m in to_control] == ["flash_sale"]
    assert to_float == []


def test_only_the_control_page_can_send_open_flash_helper():
    server = CaptionServer(port=0)
    got = []
    server.on_control = lambda msg: got.append(msg)

    async def body(client):
        float_ws = await client.ws_connect("/ws?role=float")
        await drain(float_ws)
        await float_ws.send_json({"type": "open_flash_helper"})
        await asyncio.sleep(0.1)
        assert got == []
        control = await client.ws_connect("/ws")
        await drain(control)
        await control.send_json({"type": "open_flash_helper"})
        await asyncio.sleep(0.1)
        await control.close()
        await float_ws.close()

    asyncio.run(with_client(server, body))
    assert got == [{"type": "open_flash_helper"}]


# ---- G8：终端和审计在两种界面语言下逐字节相同；这个功能一条审计都不写 ---------------------------------

def test_terminal_output_is_identical_in_both_ui_languages_and_nothing_is_audited(tmp_path, monkeypatch, capsys):
    def no_audit(*_a, **_k):
        raise AssertionError("闪购不写审计")
    monkeypatch.setattr(audit.AuditLog, "__init__", no_audit)
    monkeypatch.setattr(audit, "_open_new", no_audit)

    def one_run(lang):
        p = bare_pipeline()
        p._flash_exec = FakeExec(rc=1)

        async def go():
            Script(p, [sample("running"), ("unreachable", None), ("unreachable", None), ("bad_response", "http_500"),
                       ("bad_response", "no_answer"), dict(sample("awaiting"), schema=2)])
            await p.attach_flash_sale(platform="darwin", candidates=[app])
            with pytest.raises(Done):
                await p._flash_task
            assert p.handle_control({"type": "open_flash_helper"}) is None
            await asyncio.gather(*[t for t in list(p._bg_tasks) if t is not p._flash_task])

        with i18n.use(lang):
            asyncio.run(go())
        return capsys.readouterr().out.encode("utf-8"), p.server.sent

    app = installed_app(tmp_path)
    zh_out, zh_sent = one_run("zh")
    en_out, en_sent = one_run("en")
    assert zh_out == en_out and zh_out
    assert [m["type"] for m in zh_sent] == [m["type"] for m in en_sent]
    assert not audit.LOG_DIR.exists() or not any(audit.LOG_DIR.iterdir())


# ---- 接线：main.py（不能 import，读语法树）、关窗口收尾、pipeline.py ----------------------------------

def _main_async():
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    (fn,) = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "main_async"]
    return fn


def _awaited_pipeline_calls(fn, name):
    return [n for n in ast.walk(fn) if isinstance(n, ast.Await) and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Attribute) and n.value.func.attr == name
            and getattr(n.value.func.value, "id", None) == "pipeline"]


def test_main_attaches_before_the_first_hello_and_stops_on_exit():
    fn = _main_async()
    (attach,) = _awaited_pipeline_calls(fn, "attach_flash_sale")
    (restore,) = _awaited_pipeline_calls(fn, "restore_viewer_share")
    ready = [n for n in ast.walk(fn) if isinstance(n, ast.Assign) and any(
        isinstance(t, ast.Subscript) and getattr(t.slice, "value", None) == "ready_port" for t in n.targets)]
    assert len(ready) == 1
    assert restore.lineno < attach.lineno < ready[0].lineno
    assert attach.value.args == [] and attach.value.keywords == []      # 生产里不传平台和位置
    # 演示模式（--demo，拿来截图验界面）不接闪购：9334 上是真实商品，不能进截图
    guards = [n for n in ast.walk(fn) if isinstance(n, ast.If) and any(attach in ast.walk(b) for b in n.body)]
    assert [ast.unparse(g.test) for g in guards] == ["not args.demo"]
    assert not any(attach in ast.walk(b) for g in guards for b in g.orelse)
    stops = [n for n in ast.walk(fn) if isinstance(n, ast.Try) and n.finalbody
             and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                     and c.func.attr == "stop_flash_watch" for s in n.finalbody for c in ast.walk(s))]
    assert stops, "main_async 的 finally 里要停掉闪购轮询"


class ClosingPipeline:
    audit = None

    def __init__(self, flash_raises=False):
        self.calls = []
        self.flash_raises = flash_raises

    def _stream_active(self):
        return False

    async def stop_viewer_share(self, why):
        self.calls.append("share")

    async def stop_stream(self, quiet=False):
        self.calls.append("stream")

    def stop_flash_watch(self):
        self.calls.append("flash")
        if self.flash_raises:
            raise RuntimeError("x")


@pytest.mark.parametrize("raises", [False, True])
def test_closing_the_window_stops_the_flash_poll(raises):
    pipeline = ClosingPipeline(flash_raises=raises)
    assert asyncio.run(close_session(pipeline, audit_budget=1.0, total=2.0)) is True
    assert pipeline.calls[0] == "flash" and "stream" in pipeline.calls


def test_handle_control_routes_open_flash_helper_and_returns_at_once(tmp_path):
    p, _clock = opening_pipeline(tmp_path, FakeExec(rc=0))

    async def go():
        assert p.handle_control({"type": "open_flash_helper"}) is None
        assert p._flash_opening and len(p._bg_tasks) == 1
        await asyncio.gather(*list(p._bg_tasks))

    asyncio.run(go())


def test_pipeline_flash_sends_only_its_own_type_and_notices_and_touches_no_audit_or_settings():
    """第六条：不借 status、incident、报警来提醒；不写审计、不加 settings 键。读语法树，注释和文档串不算。"""
    for name in ("pipeline_flash.py", "flash_sale.py"):
        tree = ast.parse((ROOT / "app" / name).read_text(encoding="utf-8"))
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        for banned in ("audit", "_save_setting", "save_setting", "load_settings", "incident", "_add_incident"):
            assert banned not in names, (name, banned)
        assert not [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "status"
                    and isinstance(n.value, ast.Attribute) and n.value.attr == "server"], name
        types = {v.value for n in ast.walk(tree) if isinstance(n, ast.Dict)
                 for k, v in zip(n.keys, n.values)
                 if isinstance(k, ast.Constant) and k.value == "type" and isinstance(v, ast.Constant)}
        types |= {kw.value.value for n in ast.walk(tree) if isinstance(n, ast.Call) for kw in n.keywords
                  if kw.arg == "type" and isinstance(kw.value, ast.Constant)}
        assert types <= {"flash_sale", "notice", "flash_control_result"}, (name, types)


# ---- 开始、停止、继续（flash_control） -------------------------------------------------------------
# 绝不碰真的 9334、绝不读真的密钥：POST 换成记账的 FakePost，密钥换成 FakeToken（假值）；最后一条整条链走真的
# post_control，对面是本机临时端口上照契约检查的假闪购助手，密钥是 tmp_path 里的假文件。

FAKE_TOKEN = "7e57" * 16
LEAKS = (FAKE_TOKEN, "7e577e57")          # 假密钥和它的一截：终端、广播、config 里都不许出现
NAMES = {"start": "开始", "stop": "停止", "resume": "继续"}


class FakePost:
    """假的 post_control：记下 (动作, 密钥, 地址)，回 result（None = 已接受）；raises 不为 None 时抛它；
    gate 不为 None 时先等它放行（模拟在途的请求）。"""

    def __init__(self, result=None, raises=None, gate=None):
        self.result, self.raises, self.gate = result, raises, gate
        self.calls = []

    async def __call__(self, action, token, base):
        self.calls.append((action, token, base))
        if self.gate is not None:
            await self.gate.wait()
        if self.raises is not None:
            raise self.raises
        return self.result


class FakeToken:
    def __init__(self, result=(FAKE_TOKEN, None)):
        self.result, self.calls = result, 0

    def __call__(self):
        self.calls += 1
        return self.result


def control_pipeline(tmp_path, post=None, token=None):
    """装了闪购助手、密钥读得到、POST 换成假的，单调时钟手拨。还没查过 /status：要发指令先 poll 一跳。"""
    p = bare_pipeline()
    p._flash_app = installed_app(tmp_path)
    clock = {"mono": 500.0}
    p._flash_mono = lambda: clock["mono"]
    p._flash_post = post if post is not None else FakePost()
    p._flash_read_token = token if token is not None else FakeToken()
    return p, clock


async def apoll(p, clock, step):
    """跑一跳真的 _flash_tick。step 是一份 /status（updated_at 盖成随单调时钟走的时刻：闪购助手每 2 秒刷新一次的
    样子，30 秒里不会变陈旧），或者 fetch 的结果元组。"""
    async def fetch(url):
        if isinstance(step, dict):
            doc = copy.deepcopy(step)
            doc["updated_at"] = iso(START_WALL + clock["mono"])
            return "ok", doc
        return step
    p._flash_fetch = fetch
    await p._flash_tick()


def poll(p, clock, step):
    asyncio.run(apoll(p, clock, step))


def results(p):
    return [{k: v for k, v in m.items() if k != "type"} for m in p.server.of_type("flash_control_result")]


def controls(p):
    return [m["control"] for m in p.server.of_type("flash_sale")]


def paused_doc(reason):
    doc = sample("running")
    doc["paused_reason"] = reason
    return doc


ACCEPTED = {"ok": True, "error": None}


def test_a_pipeline_built_in_tests_points_at_no_real_port_and_no_real_token(tmp_path, monkeypatch):
    """tests/conftest.py 的网在 Pipeline 这一层也兜得住：构造时取的控制地址没有 9334，默认的读密钥函数在调用那一刻
    去读换过的那个不存在的路径。open 换成只记路径、tmp_path 以外一概不打开的替身（tests/test_flash_sale_control.py
    的 OnlyHereOs）：哪怕缺省路径被改成导入时就取，这里也读不到、印不出本机真的密钥。
    「换过的」要和真函数拼出来的那条比（同 test_flash_sale_control.py 那两条）：只拿 fs.control_token_path() 比，
    网没了两边就都是真函数、算出同一条真路径，断言恒成立；why == "missing" 也只说明替身没打开 tmp_path 以外的文件。
    路径在打开任何文件之前就比。"""
    from tests.test_flash_sale_control import OnlyHereOs

    p = bare_pipeline()
    assert p._flash_ctl_base == "http://127.0.0.1:9" and ":9334" not in p._flash_url
    assert p._flash_post is fs.post_control and p._flash_read_token is fs.read_control_token
    path = fs.control_token_path()
    assert path != REAL_CONTROL_TOKEN_PATH() and "FlashSaleHelper" not in str(path)
    assert not path.exists()
    spy = OnlyHereOs(tmp_path)
    monkeypatch.setattr(fs, "os", spy)
    token, why = p._flash_read_token()
    assert spy.paths == [path]
    assert why == "missing" and token is None


def test_without_the_helper_flash_control_does_nothing():
    """没装（演示模式也不接）：什么都不做，不读密钥、不发、不回结果。"""
    p = bare_pipeline()
    for action in fs.CONTROL_ACTIONS:
        assert p.handle_control({"type": "flash_control", "action": action}) is None
    assert not p._bg_tasks and p.server.sent == [] and p._flash_ctl is None


def test_handle_control_routes_flash_control_and_returns_at_once(tmp_path):
    p, clock = control_pipeline(tmp_path)

    async def go():
        await apoll(p, clock, sample("idle"))
        assert p.handle_control({"type": "flash_control", "action": "start"}) is None
        assert p._flash_ctl == {"action": "start", "phase": "sending", "until": None} and len(p._bg_tasks) == 1
        await asyncio.gather(*list(p._bg_tasks))

    asyncio.run(go())
    assert results(p) == [dict(ACCEPTED, action="start")]


@pytest.mark.parametrize("action", ["start", "stop", "resume"])
def test_an_accepted_command_is_reported_and_then_waited_on(action, tmp_path, capsys):
    post = FakePost()
    p, clock = control_pipeline(tmp_path, post=post)
    poll(p, clock, sample("idle"))
    click(p, {"type": "flash_control", "action": action})
    assert post.calls == [(action, FAKE_TOKEN, "http://127.0.0.1:9")]
    assert results(p) == [dict(ACCEPTED, action=action)]
    assert controls(p) == [None, {"action": action, "phase": "sending"}, {"action": action, "phase": "waiting"}]
    assert p._flash_ctl == {"action": action, "phase": "waiting", "until": 500.0 + fs.CONTROL_WAIT_SEC}
    assert p.server.config["flash_sale"]["control"] == {"action": action, "phase": "waiting"}
    # 结果在状态之后：页面收到结果时，状态里已经是「在等生效」
    assert [m["type"] for m in p.server.sent][-2:] == ["flash_sale", "flash_control_result"]
    assert out_lines(capsys)[-1] == "[信息] 闪购助手：中控请求{} → 已接受".format(NAMES[action])


def test_an_accepted_command_polls_every_three_seconds_for_thirty_seconds(tmp_path):
    """指令被接受后 30 秒内每 3 秒查一次，并马上查一次；这期间闪购助手连不上了也照样 3 秒。"""
    p, clock = control_pipeline(tmp_path)
    poll(p, clock, sample("idle"))
    click(p, {"type": "flash_control", "action": "start"})
    assert p._flash_wake_ev.is_set()
    p._flash_conn_prev = "unreachable"
    assert p._flash_interval() == fs.POLL_OK_SEC
    clock["mono"] += fs.BOOST_SEC - 0.1
    assert p._flash_interval() == fs.POLL_OK_SEC
    clock["mono"] += 0.2
    assert p._flash_interval() == fs.POLL_DOWN_SEC


@pytest.mark.parametrize("error, said", [
    ("not_running", "没有接受（not_running）"),
    ("awaiting_approval", "没有接受（awaiting_approval）"),
    ("busy", "没有接受（busy）"),
    ("brand_new_code", "没有接受（brand_new_code）"),
    ("no_control", "没有接受（no_control）"),
    ("http_403", "没有接受（http_403）"),
    ("unreachable", "连不上（unreachable）"),
    ("no_answer", "没有回话（no_answer）"),
    ("bad_reply", "回话读不出（bad_reply）"),
])
def test_a_command_that_was_not_accepted_hands_back_the_code_as_is(error, said, tmp_path, capsys):
    p, clock = control_pipeline(tmp_path, post=FakePost(result=error))
    poll(p, clock, sample("running"))
    click(p, {"type": "flash_control", "action": "stop"})
    assert results(p) == [{"action": "stop", "ok": False, "error": error}]
    assert p._flash_ctl is None and p.server.config["flash_sale"]["control"] is None
    assert controls(p) == [None, {"action": "stop", "phase": "sending"}, None]
    assert p._flash_boost_until is None
    assert out_lines(capsys)[-1] == "[警告] 闪购助手：中控请求停止 → " + said


@pytest.mark.parametrize("why", ["missing", "symlink", "not_file", "format", "too_long", "unreadable", "no_home"])
def test_no_readable_token_means_nothing_is_sent(why, tmp_path, capsys):
    post = FakePost()
    p, clock = control_pipeline(tmp_path, post=post, token=FakeToken((None, why)))
    poll(p, clock, sample("idle"))
    click(p, {"type": "flash_control", "action": "start"})
    assert post.calls == []
    assert results(p) == [{"action": "start", "ok": False, "error": "no_token"}]
    assert p._flash_ctl is None and controls(p) == [None]
    assert out_lines(capsys)[-1] == "[警告] 闪购助手：中控请求开始 → 没有发出（no_token：{}）".format(why)


@pytest.mark.parametrize("steps", [
    [],                                                     # 还没查过
    [("unreachable", None)],
    [("bad_response", "json")],
    [("bad_response", "no_answer")],
    ["idle", ("bad_response", "no_answer")],                # 连上以后第一次没查到：发布的照旧是 ok，但不发
    ["idle", ("ok", [1])],
    ["idle", ("unreachable", None), ("unreachable", None)],
])
def test_without_a_fresh_status_the_token_is_not_read_and_nothing_is_sent(steps, tmp_path, capsys):
    """发之前要求最近一次 /status 连上并读懂了：9334 上回话的不一定是闪购助手，密钥只给刚刚还在好好回
    /status 的那个。不是的话不读密钥、不发，结果 error=no_status。"""
    post, token = FakePost(), FakeToken()
    p, clock = control_pipeline(tmp_path, post=post, token=token)
    for step in steps:
        poll(p, clock, sample(step) if isinstance(step, str) else step)
        clock["mono"] += 3
    if steps[-1:] == [("bad_response", "no_answer")] and len(steps) == 2:
        assert p.server.config["flash_sale"]["conn"] == "ok"     # 页面上还画着按钮的那一种
    click(p, {"type": "flash_control", "action": "stop"})
    assert token.calls == 0 and post.calls == []
    assert results(p) == [{"action": "stop", "ok": False, "error": "no_status"}]
    assert p._flash_ctl is None
    assert out_lines(capsys)[-1] == "[警告] 闪购助手：中控请求停止 → 没有发出（no_status）"


@pytest.mark.parametrize("doc, why", [
    ({"status": "ok"}, "schema"),
    ({}, "schema"),
    (dict(sample("running"), schema=2), "schema"),
    (dict(sample("running"), page="maintenance"), "page"),
])
def test_a_status_that_does_not_look_like_the_helpers_gets_no_token(doc, why, tmp_path, capsys):
    """连上并读懂了，但不像闪购助手的回话（schema 不是 1、page 不是契约里的代码）：9334 上回话的可能是碰巧占着这个端口的
    别的服务。不读密钥、不发，页面收到 no_status，终端那一行带着原因（StatusTracker.control_refusal）。"""
    post, token = FakePost(), FakeToken()
    p, clock = control_pipeline(tmp_path, post=post, token=token)
    poll(p, clock, ("ok", copy.deepcopy(doc)))
    assert p.server.config["flash_sale"]["conn"] == "ok"
    click(p, {"type": "flash_control", "action": "stop"})
    assert token.calls == 0 and post.calls == []
    assert results(p) == [{"action": "stop", "ok": False, "error": "no_status"}]
    assert p._flash_ctl is None
    assert out_lines(capsys)[-1] == "[警告] 闪购助手：中控请求停止 → 没有发出（no_status：{}）".format(why)


def test_losing_the_status_while_start_is_waiting_refuses_stop_and_keeps_the_wait(tmp_path):
    post = FakePost()
    p, clock = control_pipeline(tmp_path, post=post)
    poll(p, clock, sample("idle"))
    click(p, {"type": "flash_control", "action": "start"})
    waiting = dict(p._flash_ctl)
    for _ in range(2):
        clock["mono"] += 3
        poll(p, clock, ("unreachable", None))
    click(p, {"type": "flash_control", "action": "stop"})
    assert [c[0] for c in post.calls] == ["start"]
    assert results(p)[-1] == {"action": "stop", "ok": False, "error": "no_status"}
    assert p._flash_ctl == waiting
    assert p.server.config["flash_sale"]["control"] == {"action": "start", "phase": "waiting"}


def test_a_refused_command_while_another_is_waiting_puts_the_wait_back(tmp_path):
    """开始在等生效，这时点的停止被闪购助手拒了（busy）：开始照旧等着，到点照旧判。"""
    post = FakePost()
    p, clock = control_pipeline(tmp_path, post=post)
    poll(p, clock, sample("idle"))
    click(p, {"type": "flash_control", "action": "start"})
    waiting = dict(p._flash_ctl)
    post.result = "busy"
    clock["mono"] += 1
    click(p, {"type": "flash_control", "action": "stop"})
    assert p._flash_ctl == waiting
    assert results(p)[-1] == {"action": "stop", "ok": False, "error": "busy"}
    assert controls(p)[-2:] == [{"action": "stop", "phase": "sending"}, {"action": "start", "phase": "waiting"}]


@pytest.mark.parametrize("msg", [
    {"type": "flash_control"},
    {"type": "flash_control", "action": "START"},
    {"type": "flash_control", "action": "status"},
    {"type": "flash_control", "action": "../stop"},
    {"type": "flash_control", "action": "stop?force=1"},
    {"type": "flash_control", "action": None},
    {"type": "flash_control", "action": ["stop"]},
    {"type": "flash_control", "action": {"stop": 1}},
    {"type": "flash_control", "action": 1},
])
def test_only_the_three_actions_do_anything(msg, tmp_path):
    post, token = FakePost(), FakeToken()
    p, clock = control_pipeline(tmp_path, post=post, token=token)
    poll(p, clock, sample("idle"))
    before = list(p.server.sent)
    assert p.handle_control(msg) is None
    assert not p._bg_tasks and p._flash_ctl is None
    assert post.calls == [] and token.calls == 0 and p.server.sent == before


def test_the_page_cannot_put_anything_into_the_request(tmp_path):
    post = FakePost()
    p, clock = control_pipeline(tmp_path, post=post)
    poll(p, clock, sample("running"))
    click(p, {"type": "flash_control", "action": "stop", "url": "http://evil.example/stop", "base": "http://x",
              "token": "0" * 64, "headers": {"Origin": "http://evil.example"}, "body": {"a": 1}})
    assert post.calls == [("stop", FAKE_TOKEN, "http://127.0.0.1:9")]


def test_a_second_command_while_one_is_in_flight_is_dropped(tmp_path):
    """在途时（phase=sending）再点、换个动作点、另一个控制页点：都只发第一个。回来以后照常能发。"""
    async def go():
        gate = asyncio.Event()
        post = FakePost(gate=gate)
        p, clock = control_pipeline(tmp_path, post=post)
        await apoll(p, clock, sample("idle"))
        assert p.handle_control({"type": "flash_control", "action": "start"}) is None
        await asyncio.sleep(0.01)
        assert p.server.config["flash_sale"]["control"] == {"action": "start", "phase": "sending"}
        for action in ("start", "stop", "resume"):
            assert p.handle_control({"type": "flash_control", "action": action}) is None
        assert len(p._bg_tasks) == 1 and len(post.calls) == 1
        gate.set()
        await asyncio.gather(*list(p._bg_tasks))
        post.gate = None
        assert p.handle_control({"type": "flash_control", "action": "stop"}) is None
        await asyncio.gather(*list(p._bg_tasks))
        return p, post

    p, post = asyncio.run(go())
    assert [c[0] for c in post.calls] == ["start", "stop"]
    assert results(p) == [dict(ACCEPTED, action="start"), dict(ACCEPTED, action="stop")]


@pytest.mark.parametrize("doc", ["running", "idle"])
def test_a_poll_while_a_command_is_in_flight_leaves_the_command_alone(doc, tmp_path):
    """指令还在途（phase=sending，还没有「等到哪一刻」）时正好跑了一跳轮询：不拿这一跳去判生效（在途的开始碰上
    running=true 也不提前收掉），也不拿 None 去比时刻（那样会抛 TypeError、这一跳不发布）。回来以后照常进入等生效，
    下一跳再判。running 为真、为假各一遍。"""
    async def go():
        gate = asyncio.Event()
        p, clock = control_pipeline(tmp_path, post=FakePost(gate=gate))
        await apoll(p, clock, sample("idle"))
        p.handle_control({"type": "flash_control", "action": "start"})
        await asyncio.sleep(0.01)
        clock["mono"] += 3
        await apoll(p, clock, sample(doc))
        assert p._flash_ctl == {"action": "start", "phase": "sending", "until": None}
        state = p.server.config["flash_sale"]
        assert state["control"] == {"action": "start", "phase": "sending"}
        assert state["running"] is (doc == "running")          # 这一跳照常发布了
        gate.set()
        await asyncio.gather(*list(p._bg_tasks))
        assert p._flash_ctl["phase"] == "waiting"
        clock["mono"] += 3
        await apoll(p, clock, sample(doc))
        return p

    p = asyncio.run(go())
    assert (p._flash_ctl is None) is (doc == "running")      # running=true：下一跳判出生效；false：接着等
    assert results(p) == [dict(ACCEPTED, action="start")]


# ---- 慢页面：广播真的让出事件循环 ------------------------------------------------------------------
# RecordingServer 没有连着的页面，broadcast 从不让出事件循环：拿掉发布那把锁、或者先发布再发 POST，上面的用例照样全绿。
# 真的页面慢、被卡住时，一次广播最坏拖 2 秒。下面两条用 HeldServer 卡住一条广播，钉住 _flash_control_run 和
# _flash_refresh 里写着的那两件事。（下播后跑一遍「拿掉锁」「先发布再 POST」两种改法，确认这两条各自会红。）

class HeldServer(RecordingServer):
    """hold 不为 None 时，下一条 flash_sale 先记进 held，等 hold 放行才真的发出去（后面的照常）。sent 的先后就是真发到
    页面的先后。"""

    def __init__(self):
        super().__init__()
        self.hold = None
        self.held = []

    async def broadcast(self, msg):
        hold = self.hold
        if hold is not None and msg.get("type") == "flash_sale":
            self.hold = None
            self.held.append(msg)
            await hold.wait()
        return await super().broadcast(msg)


def held_pipeline(tmp_path, post):
    p, clock = control_pipeline(tmp_path, post=post)
    p.server = HeldServer()
    return p, clock


def test_the_post_goes_out_while_a_slow_page_holds_the_sending_broadcast(tmp_path):
    """「停止」不等广播：phase=sending 那一份状态的广播被慢页面卡住时，POST 已经发出去了（先起 POST、再发布）。
    先发布、等发完再发 POST 的话，这时 post.calls 还是空的。结果照旧排在状态之后。"""
    post = FakePost()

    async def go():
        p, clock = held_pipeline(tmp_path, post)
        await apoll(p, clock, sample("running"))
        hold = p.server.hold = asyncio.Event()
        assert p.handle_control({"type": "flash_control", "action": "stop"}) is None
        await asyncio.sleep(0.01)
        seen = ([m["control"] for m in p.server.held], list(post.calls), results(p))
        hold.set()
        await asyncio.gather(*list(p._bg_tasks))
        return p, seen

    p, (held, calls, said) = asyncio.run(go())
    assert held == [{"action": "stop", "phase": "sending"}]          # 卡住的是在途那一份
    assert calls == [("stop", FAKE_TOKEN, "http://127.0.0.1:9")]     # 那时 POST 已经发出去了
    assert said == []                                                # 结果要等状态发完
    assert controls(p) == [None, {"action": "stop", "phase": "sending"}, {"action": "stop", "phase": "waiting"}]
    assert results(p) == [dict(ACCEPTED, action="stop")]


def test_a_poll_and_a_command_publishing_at_once_leave_the_page_on_the_newest_phase(tmp_path):
    """轮询和控制两条路同时发布：轮询那一份（还没有指令）的广播被慢页面卡住时点了停止。两条路在一把锁里排队
    （_flash_refresh），控制那一路等轮询那一份真发完才合成、才发，页面最后收到的、config 里的都是最新的「在等生效」。
    不排队的话，控制那两份先发完，旧的那份最后才到，页面停在没有指令的样子（按钮又能点了），config 却是另一份。
    这把锁只排发布，不挡 POST。"""
    post = FakePost()

    async def go():
        p, clock = held_pipeline(tmp_path, post)
        await apoll(p, clock, sample("idle"))
        hold = p.server.hold = asyncio.Event()
        clock["mono"] += 3
        tick = asyncio.ensure_future(apoll(p, clock, sample("running")))
        await asyncio.sleep(0.01)
        held = [m["control"] for m in p.server.held]
        assert p.handle_control({"type": "flash_control", "action": "stop"}) is None
        await asyncio.sleep(0.01)
        calls = list(post.calls)
        hold.set()
        await tick
        await asyncio.gather(*list(p._bg_tasks))
        return p, held, calls

    p, held, calls = asyncio.run(go())
    assert held == [None]                                            # 卡住的是轮询那一份
    assert calls == [("stop", FAKE_TOKEN, "http://127.0.0.1:9")]     # 轮询那份还卡着时 POST 已经发出去了
    last = p.server.of_type("flash_sale")[-1]
    assert last["control"] == {"action": "stop", "phase": "waiting"}
    assert p.server.config["flash_sale"] == {k: v for k, v in last.items() if k != "type"}
    assert controls(p) == [None, None, {"action": "stop", "phase": "sending"}, {"action": "stop", "phase": "waiting"}]
    assert results(p) == [dict(ACCEPTED, action="stop")]


@pytest.mark.parametrize("action, before, after", [("start", "idle", "running"), ("stop", "running", "idle")])
def test_once_the_status_shows_the_command_took_effect_the_wait_ends(action, before, after, tmp_path, capsys):
    p, clock = control_pipeline(tmp_path)
    poll(p, clock, sample(before))
    click(p, {"type": "flash_control", "action": action})
    clock["mono"] += 3
    poll(p, clock, sample(before))
    assert p._flash_ctl["phase"] == "waiting"
    clock["mono"] += 3
    poll(p, clock, sample(after))
    assert p._flash_ctl is None and p.server.config["flash_sale"]["control"] is None
    assert results(p) == [dict(ACCEPTED, action=action)]          # 生效了不另发结果：状态自己会变
    assert out_lines(capsys)[-1] == "[信息] 闪购助手：中控请求{} → 已生效".format(NAMES[action])


def test_resume_waits_until_the_user_interaction_pause_is_gone(tmp_path):
    p, clock = control_pipeline(tmp_path)
    poll(p, clock, paused_doc("user_interaction"))
    click(p, {"type": "flash_control", "action": "resume"})
    clock["mono"] += 3
    poll(p, clock, paused_doc("user_interaction"))
    assert p._flash_ctl["phase"] == "waiting"
    clock["mono"] += 3
    poll(p, clock, sample("running"))
    assert p._flash_ctl is None


def test_thirty_seconds_without_effect_is_reported_once_as_not_seen(tmp_path, capsys):
    """契约：开始后超过 30 秒 running 还不是 true，就当这次没成功。只写看到的：30 秒内没看到生效。
    边界：差 0.1 秒还在等，正好 30 秒那一跳收掉、发 not_seen；之后不再发。"""
    p, clock = control_pipeline(tmp_path)
    poll(p, clock, sample("idle"))
    click(p, {"type": "flash_control", "action": "start"})
    accepted_at = clock["mono"]
    for t in range(3, fs.CONTROL_WAIT_SEC, 3):
        clock["mono"] = accepted_at + t
        poll(p, clock, sample("idle"))
        assert p._flash_ctl["phase"] == "waiting"
    clock["mono"] = accepted_at + fs.CONTROL_WAIT_SEC - 0.1
    poll(p, clock, sample("idle"))
    assert p._flash_ctl is not None and results(p) == [dict(ACCEPTED, action="start")]
    clock["mono"] = accepted_at + fs.CONTROL_WAIT_SEC
    poll(p, clock, sample("idle"))
    assert p._flash_ctl is None and p.server.config["flash_sale"]["control"] is None
    assert results(p) == [dict(ACCEPTED, action="start"), {"action": "start", "ok": False, "error": "not_seen"}]
    assert out_lines(capsys)[-1] == "[警告] 闪购助手：中控请求开始 → 30 秒内没看到生效"
    clock["mono"] += 3
    poll(p, clock, sample("idle"))
    assert len(results(p)) == 2
    assert "not_seen" in fs.CONTROL_OWN_ERRORS


def test_exit_stops_the_poll_but_lets_a_command_in_flight_finish(tmp_path):
    """退出（stop_flash_watch）只停轮询，在途的控制请求不取消、让它发完（最多 4 秒）。"""
    async def go():
        gate = asyncio.Event()
        p, clock = control_pipeline(tmp_path, post=FakePost(gate=gate))
        await apoll(p, clock, sample("running"))

        async def blocking_fetch(url):
            await asyncio.Event().wait()

        p._flash_fetch = blocking_fetch
        p._start_flash_watch()
        watch = p._flash_task
        assert p.handle_control({"type": "flash_control", "action": "stop"}) is None
        await asyncio.sleep(0.01)
        (run,) = [t for t in p._bg_tasks if t is not watch]
        p.stop_flash_watch()
        with pytest.raises(asyncio.CancelledError):
            await watch
        assert not run.done()
        gate.set()
        await run
        assert not run.cancelled()
        return p

    p = asyncio.run(go())
    assert results(p) == [dict(ACCEPTED, action="stop")]


def test_the_token_never_reaches_the_terminal_the_pages_or_the_config(tmp_path, capsys):
    """密钥只交给 POST。终端只打异常的类型名：aiohttp 的 ClientResponseError 的 repr 里带着 Authorization
    （先确认这个隐患是真的），异常文字里带着密钥的也一样。"""
    import aiohttp
    from multidict import CIMultiDict, CIMultiDictProxy
    from yarl import URL

    info = aiohttp.RequestInfo(URL("http://127.0.0.1:9/stop"), "POST",
                               CIMultiDictProxy(CIMultiDict({"Authorization": "Bearer " + FAKE_TOKEN})))
    leaky = aiohttp.ClientResponseError(info, (), status=400, message="x")
    assert FAKE_TOKEN in repr(leaky)
    posts = [FakePost(), FakePost(result="not_running"), FakePost(result="http_403"), FakePost(raises=leaky),
             FakePost(raises=RuntimeError("Bearer " + FAKE_TOKEN))]
    for i, post in enumerate(posts):
        p, clock = control_pipeline(tmp_path / str(i), post=post)
        poll(p, clock, sample("running"))
        click(p, {"type": "flash_control", "action": "stop"})
        assert post.calls[0][1] == FAKE_TOKEN                   # 密钥确实交给了 POST，也只交给了它
        blob = json.dumps(p.server.sent, ensure_ascii=False) + json.dumps(p.server.config, default=str)
        assert not any(piece in blob for piece in LEAKS), i
    out = capsys.readouterr()
    assert not any(piece in out.out + out.err for piece in LEAKS)
    lines = [line for line in out.out.splitlines() if "中控请求" in line]
    assert lines == ["[信息] 闪购助手：中控请求停止 → 已接受",
                     "[警告] 闪购助手：中控请求停止 → 没有接受（not_running）",
                     "[警告] 闪购助手：中控请求停止 → 没有接受（http_403）",
                     "[警告] 闪购助手：中控请求停止 → 出错（failed：ClientResponseError）",
                     "[警告] 闪购助手：中控请求停止 → 出错（failed：RuntimeError）"]


def test_the_phone_and_the_float_page_never_get_a_control_result():
    msg = {"type": "flash_control_result", "action": "stop", "ok": True, "error": None}
    assert "flash_control_result" in viewer.DENY and "flash_control_result" not in viewer.ALLOW
    assert viewer.filter_payload(msg) is None
    assert "flash_control_result" not in FLOAT_TYPES and "flash_control_result" in i18n.DATA_ONLY


def test_only_the_control_page_can_send_flash_control_and_only_it_gets_the_result():
    server = CaptionServer(port=0)
    got = []
    server.on_control = lambda msg: got.append(msg)

    async def body(client):
        float_ws = await client.ws_connect("/ws?role=float")
        await drain(float_ws)
        await float_ws.send_json({"type": "flash_control", "action": "stop"})
        await asyncio.sleep(0.1)
        assert got == []
        control = await client.ws_connect("/ws")
        await drain(control)
        await control.send_json({"type": "flash_control", "action": "stop"})
        await asyncio.sleep(0.1)
        await server.broadcast({"type": "flash_control_result", "action": "stop", "ok": True, "error": None})
        out = (await drain(control), await drain(float_ws))
        await control.close()
        await float_ws.close()
        return out

    to_control, to_float = asyncio.run(with_client(server, body))
    assert got == [{"type": "flash_control", "action": "stop"}]
    assert [m["type"] for m in to_control] == ["flash_control_result"] and to_float == []


def test_control_terminal_output_is_identical_in_both_ui_languages(tmp_path, capsys):
    """G8：终端在中英两种界面下逐字节相同；广播的类型也一样（结果只带代码，文案在前端）。"""
    def one_run(lang):
        p, clock = control_pipeline(tmp_path / lang)
        with i18n.use(lang):
            poll(p, clock, sample("idle"))
            click(p, {"type": "flash_control", "action": "start"})
            p._flash_post = FakePost(result="busy")
            click(p, {"type": "flash_control", "action": "stop"})
            clock["mono"] += fs.CONTROL_WAIT_SEC
            poll(p, clock, sample("idle"))
            p._flash_read_token = FakeToken((None, "missing"))
            click(p, {"type": "flash_control", "action": "start"})
            poll(p, clock, ("unreachable", None))
            click(p, {"type": "flash_control", "action": "stop"})
        return capsys.readouterr().out.encode("utf-8"), [m["type"] for m in p.server.sent], results(p)

    zh = one_run("zh")
    en = one_run("en")
    assert zh == en and zh[0]
    assert [r["error"] for r in zh[2]] == [None, "busy", "not_seen", "no_token", "no_status"]


def test_the_whole_chain_against_a_fake_helper_on_a_local_port(tmp_path):
    """整条链：控制页的指令 → 真的 read_control_token（tmp_path 里的假密钥文件）→ 真的 post_control → 本机临时端口上
    照契约逐条检查的假闪购助手（tests/test_flash_sale_control.py）→ 结果广播。"""
    from tests.test_flash_sale_control import fake_helper

    token_path = tmp_path / "control-token"
    token_path.write_text(FAKE_TOKEN + "\n", encoding="ascii")
    seen = []

    async def go():
        server = TestServer(fake_helper(seen, token=FAKE_TOKEN), host="127.0.0.1")
        await server.start_server()
        try:
            assert server.port != 9334
            p, clock = control_pipeline(tmp_path, post=fs.post_control,
                                        token=lambda: fs.read_control_token(token_path))
            p._flash_ctl_base = "http://127.0.0.1:%d" % server.port
            await apoll(p, clock, sample("idle"))
            assert p.handle_control({"type": "flash_control", "action": "start"}) is None
            await asyncio.gather(*list(p._bg_tasks))
            return p
        finally:
            await server.close()

    p = asyncio.run(go())
    assert [r["path"] for r in seen] == ["/start"] and seen[0]["problems"] == []
    assert results(p) == [dict(ACCEPTED, action="start")]


# ---- 开始以后的过渡：30 秒里页面刷新不变色（decisions-step2，用户 10-27 拍板） ---------------------------

WAITING_START = {"action": "start", "phase": "waiting"}


def reloading_doc(page):
    """POST /start 之后闪购助手刷新页面的样子：没在运行、page 是 loading / error / no_list。updated_at 定住不动
    （page=error 时闪购助手不刷新它），15 秒以后就是陈旧。直接当 fetch 的结果交出去，不让 apoll 改时刻。"""
    doc = sample("idle")
    doc.update(page=page, updated_at=iso(START_WALL))
    return "ok", doc


@pytest.mark.parametrize("page, after", [("error", ["stale", "page"]), ("loading", ["stale"]),
                                         ("no_list", ["stale"])])
def test_after_start_is_accepted_a_reloading_page_stays_calm_for_thirty_seconds(page, after, tmp_path):
    """开始被接受后的 30 秒里：页面加载中、出错、不在商品列表，超过 15 秒没更新，都不算要人处理——按钮不变橙、
    浮层不出红字（状态里 control 是「开始、在等生效」，页面据此写「正在开始…（闪购助手在刷新页面）」）。
    边界：差 0.1 秒还算；正好 30 秒那一跳 control 收掉，照原规则提醒，另发一条 not_seen。
    page 和 stale 这两个观察照旧在状态里，只是不进 attention。"""
    p, clock = control_pipeline(tmp_path)
    poll(p, clock, sample("idle"))
    click(p, {"type": "flash_control", "action": "start"})
    accepted_at = clock["mono"]
    for t in (3, 6, 21, 27, fs.CONTROL_WAIT_SEC - 0.1):
        clock["mono"] = accepted_at + t
        poll(p, clock, reloading_doc(page))
        state = p.server.config["flash_sale"]
        assert state["control"] == WAITING_START and state["attention"] == [], t
        assert state["page"] == page and state["stale"] is (t > 3 + fs.STALE_SEC)
    clock["mono"] = accepted_at + fs.CONTROL_WAIT_SEC
    poll(p, clock, reloading_doc(page))
    state = p.server.config["flash_sale"]
    assert state["control"] is None and state["attention"] == after
    assert results(p)[-1] == {"action": "start", "ok": False, "error": "not_seen"}


def test_the_quiet_window_ends_as_soon_as_start_takes_effect(tmp_path):
    """同一个窗口：running 变 true（生效了）就收掉 control，之后页面再出错照原规则提醒，不再安静到 30 秒。"""
    p, clock = control_pipeline(tmp_path)
    poll(p, clock, sample("idle"))
    click(p, {"type": "flash_control", "action": "start"})
    clock["mono"] += 3
    poll(p, clock, reloading_doc("error"))
    assert p.server.config["flash_sale"]["attention"] == []
    clock["mono"] += 3
    poll(p, clock, sample("running"))
    assert p.server.config["flash_sale"]["control"] is None
    clock["mono"] += 3
    running_error = sample("running")
    running_error["page"] = "error"
    poll(p, clock, running_error)
    assert p.server.config["flash_sale"]["attention"] == ["page"]
    assert results(p) == [dict(ACCEPTED, action="start")]


def started_doc(page):
    """刷新以后闪购助手已经接着运行（running=true），页面还没回到 Buy now 列表：刷新期间是 loading（闪购助手的实现在
    正在运行的那个标签页刷新、脚本还没装好时就这样报），列表回来之前是 no_list。没有商品，updated_at 定住不动
    （同 reloading_doc，15 秒以后就是陈旧）。"""
    doc = sample("running")
    doc.update(page=page, updated_at=iso(START_WALL), products=[])
    return "ok", doc


@pytest.mark.parametrize("page, after", [("no_list", ["stale", "page"]), ("loading", ["stale"]),
                                         ("error", ["stale", "page"])])
def test_start_that_takes_effect_before_the_list_is_back_stays_calm_for_thirty_seconds(page, after, tmp_path, capsys):
    """审查 2026-10-28：只要 running 变 true（control_done）就收掉 control 的话，刷新以后列表回来之前的那段 no_list
    （加上 running=true）马上变橙、出红字，START_QUIET_PAGES 里的 no_list 对开始等于没用——正是这段过渡要免的。
    现在：第一次看到生效就在终端说一行「已生效」（只说一次，不另发结果），control 留到页面稳下来，或者等满 30 秒；
    这段时间照样不变色。30 秒那一跳收掉、照原规则提醒；看到过生效，不发 not_seen。"""
    p, clock = control_pipeline(tmp_path)
    poll(p, clock, sample("idle"))
    click(p, {"type": "flash_control", "action": "start"})
    accepted_at = clock["mono"]
    out_lines(capsys)
    for t in (3, 6, 21, 27, fs.CONTROL_WAIT_SEC - 0.1):
        clock["mono"] = accepted_at + t
        poll(p, clock, started_doc(page))
        state = p.server.config["flash_sale"]
        assert state["control"] == WAITING_START and state["attention"] == [], t
        assert state["page"] == page and state["running"] is True and state["stale"] is (t > 3 + fs.STALE_SEC)
    assert out_lines(capsys) == ["[信息] 闪购助手：中控请求开始 → 已生效"]
    clock["mono"] = accepted_at + fs.CONTROL_WAIT_SEC
    poll(p, clock, started_doc(page))
    state = p.server.config["flash_sale"]
    assert state["control"] is None and state["attention"] == after
    assert results(p) == [dict(ACCEPTED, action="start")]
    assert out_lines(capsys) == []


def test_after_start_takes_effect_the_window_closes_once_the_list_is_back(tmp_path, capsys):
    """同一个窗口：生效以后页面稳下来（不陈旧、page 不是 loading / error / no_list）就收掉，不再安静到 30 秒，
    之后页面再出错照原规则提醒。照闪购助手的实现走一遍：刷新中（loading，running 已经是 true）→ 列表还没回来
    （no_list）→ 回到列表（ok）。"""
    p, clock = control_pipeline(tmp_path)
    poll(p, clock, sample("idle"))
    click(p, {"type": "flash_control", "action": "start"})
    out_lines(capsys)
    clock["mono"] += 3
    poll(p, clock, started_doc("loading"))
    assert p.server.config["flash_sale"]["control"] == WAITING_START
    assert out_lines(capsys) == ["[信息] 闪购助手：中控请求开始 → 已生效"]
    clock["mono"] += 3
    no_list = sample("running")
    no_list.update(page="no_list", products=[])
    poll(p, clock, no_list)
    state = p.server.config["flash_sale"]
    assert state["control"] == WAITING_START and state["attention"] == [] and state["stale"] is False
    clock["mono"] += 3
    poll(p, clock, sample("running"))
    state = p.server.config["flash_sale"]
    assert state["control"] is None and state["attention"] == []
    clock["mono"] += 3
    running_error = sample("running")
    running_error["page"] = "error"
    poll(p, clock, running_error)
    assert p.server.config["flash_sale"]["attention"] == ["page"]
    assert results(p) == [dict(ACCEPTED, action="start")]
    assert out_lines(capsys) == []


def test_after_start_takes_effect_a_stale_or_lost_status_does_not_close_the_window(tmp_path):
    """页面稳下来要三样都对（flash_sale.control_settled）：连上了、不陈旧、page 不是 loading / error / no_list。
    - 回到了列表，可 updated_at 从刷新起一直没动（陈旧）：不收，安静到 30 秒，之后照原规则提醒陈旧；
    - 生效以后连着两跳查不到（第二跳发布空状态）：看不到页面，不收；再连上时列表还没回来，照样不变色。"""
    p, clock = control_pipeline(tmp_path)
    poll(p, clock, sample("idle"))
    click(p, {"type": "flash_control", "action": "start"})
    accepted_at = clock["mono"]
    clock["mono"] = accepted_at + 3
    poll(p, clock, started_doc("loading"))
    clock["mono"] = accepted_at + 3 + fs.STALE_SEC + 1
    poll(p, clock, started_doc("ok"))
    state = p.server.config["flash_sale"]
    assert state["page"] == "ok" and state["stale"] is True
    assert state["control"] == WAITING_START and state["attention"] == []
    clock["mono"] = accepted_at + fs.CONTROL_WAIT_SEC
    poll(p, clock, started_doc("ok"))
    state = p.server.config["flash_sale"]
    assert state["control"] is None and state["attention"] == ["stale"]

    p, clock = control_pipeline(tmp_path / "lost")
    poll(p, clock, sample("idle"))
    click(p, {"type": "flash_control", "action": "start"})
    clock["mono"] += 3
    poll(p, clock, started_doc("loading"))
    for _ in range(2):
        clock["mono"] += 3
        poll(p, clock, ("unreachable", None))
    state = p.server.config["flash_sale"]
    assert state["conn"] == "unreachable" and state["control"] == WAITING_START
    clock["mono"] += 3
    no_list = sample("running")
    no_list.update(page="no_list", products=[])
    poll(p, clock, no_list)
    state = p.server.config["flash_sale"]
    assert state["control"] == WAITING_START and state["attention"] == []
    assert results(p) == [dict(ACCEPTED, action="start")]


def test_while_start_is_still_in_flight_a_page_error_is_not_quiet(tmp_path):
    """还没接受（在途）不算：过渡只从「闪购助手接受了」开始。"""
    async def go():
        gate = asyncio.Event()
        p, clock = control_pipeline(tmp_path, post=FakePost(gate=gate))
        await apoll(p, clock, sample("idle"))
        p.handle_control({"type": "flash_control", "action": "start"})
        await asyncio.sleep(0.01)
        clock["mono"] += 3
        await apoll(p, clock, reloading_doc("error"))
        state = p.server.config["flash_sale"]
        gate.set()
        await asyncio.gather(*list(p._bg_tasks))
        return state

    state = asyncio.run(go())
    assert state["control"] == {"action": "start", "phase": "sending"} and state["attention"] == ["page"]


def test_stop_has_no_quiet_window(tmp_path):
    p, clock = control_pipeline(tmp_path)
    poll(p, clock, sample("running"))
    click(p, {"type": "flash_control", "action": "stop"})
    clock["mono"] += 3
    running_error = sample("running")
    running_error["page"] = "error"
    poll(p, clock, running_error)
    state = p.server.config["flash_sale"]
    assert state["control"] == {"action": "stop", "phase": "waiting"} and state["attention"] == ["page"]
