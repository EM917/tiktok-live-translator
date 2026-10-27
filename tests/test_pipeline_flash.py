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
        assert types <= {"flash_sale", "notice"}, (name, types)
