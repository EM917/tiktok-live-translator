"""找得到闪购助手（直播助手平台轮廓 §7.2 第 2 步）和两个新用户问题 launch-7、launch-8：

- 本机指定的位置：settings.json 的 flash_sale.app_path（§5.2「本机指定位置」：只认完整路径、名字是 闪购助手.app 的）。
  指定的优先；不存在就不用，接着看 /Applications、~/Applications；
- 没找到时终端一行（双击 .app 启动时它进运行日志）；
- launch-7（用户选 B）：没找到 .app，但启动时 9334 上的 /status 像闪购助手的回话，照样有「闪购」按钮，只是没有
  「打开闪购助手」（推给页面的状态里 app_found 是 false）；
- 打开浮层时页面发 flash_find，后端再找一次（只看文件系统）：装完、挪完不用重启；
- launch-8：「打开闪购助手」失败时，把 open 的返回码和系统给的错误号照实写给人，再说能做什么（第八条：不猜原因）。

绝不碰本机真的闪购助手：位置一律在 tmp_path 里造，抓取换成假的，open 换成记账的替身（同 tests/test_pipeline_flash.py，
那里的替身直接拿来用）。settings.json 是 tests/conftest.py 换好的临时文件。
"""
import asyncio
import contextlib
import json
import os

import pytest

from app import flash_sale as fs
from app import i18n, settings
from app.i18n import CJK
from tests.test_pipeline_flash import (FAKE_TOKEN, Done, FakeExec, FakePost, FakeToken, Script, bare_pipeline, click,
                                       control_pipeline, installed_app, opening_pipeline, out_lines, poll, sample)

APP = fs.APP_NAME
ENV_HOME = "USERPROFILE" if os.name == "nt" else "HOME"


def saved(value):
    return {"flash_sale": {"app_path": value}}


def write_settings(data):
    settings.SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def not_found_line(*paths):
    return ("[信息] 闪购助手：没找到 {}（找过 {}）。装在别处的话，可以把完整路径写进 settings.json 的 "
            "flash_sale.app_path").format(APP, "、".join(str(p) for p in paths))


def fake_fetch(result):
    """假的抓取：每次都交出同一个结果，记下被调了几次。"""
    calls = []

    async def fetch(url):
        calls.append(url)
        return result
    fetch.calls = calls
    return fetch


def attach(p, candidates):
    """attach_flash_sale，接上了就马上停掉轮询（不让它真的跑下去），等它收完，_bg_tasks 里不留它。"""
    async def go():
        await p.attach_flash_sale(platform="darwin", candidates=candidates)
        task = p._flash_task
        p.stop_flash_watch()
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await asyncio.sleep(0)
    asyncio.run(go())
    assert not p._bg_tasks


def find(p):
    """控制页打开浮层时发的 flash_find：handle_control 交回协程就等它跑完（server._ws 也是这么做的）。"""
    async def go():
        result = p.handle_control({"type": "flash_find"})
        if asyncio.iscoroutine(result):
            await result
        return result
    return asyncio.run(go())


# ---- 本机指定的位置（纯函数） ------------------------------------------------------------------

@pytest.mark.parametrize("data", [
    {}, {"other": 1}, {"flash_sale": None}, {"flash_sale": {}}, {"flash_sale": {"app_path": None}},
    {"flash_sale": {"app_path": ""}}, {"flash_sale": {"rules": True}}, None, [], "x",
])
def test_nothing_configured(data):
    assert fs.configured_app(data) == (None, None)


def test_a_full_path_to_the_app_is_used_as_is(tmp_path):
    path = tmp_path / "Desktop" / "闪购助手" / APP
    assert fs.configured_app(saved(str(path))) == (path, None)
    assert fs.configured_app(saved(str(path) + os.sep)) == (path, None)        # 结尾带斜杠也认


def test_a_home_relative_path_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_HOME, str(tmp_path))
    assert fs.configured_app(saved("~/Desktop/" + APP)) == (tmp_path / "Desktop" / APP, None)


@pytest.mark.parametrize("value, problem", [
    (42, "bad_value"), (True, "bad_value"), (["/Applications/" + APP], "bad_value"), ({"path": APP}, "bad_value"),
    ("Desktop/" + APP, "not_absolute"), (APP, "not_absolute"),
])
def test_values_that_are_not_a_full_path_are_not_used(value, problem):
    assert fs.configured_app(saved(value)) == (None, problem)


def test_a_flash_sale_key_that_is_not_an_object_is_not_used():
    assert fs.configured_app({"flash_sale": "/Applications/" + APP}) == (None, "bad_value")
    assert fs.configured_app({"flash_sale": ["x"]}) == (None, "bad_value")


def test_a_path_with_a_nul_or_far_too_long_is_not_used(tmp_path):
    assert fs.configured_app(saved(str(tmp_path / ("a\x00b")) + os.sep + APP)) == (None, "bad_value")
    long = str(tmp_path / ("x" * (fs.APP_PATH_MAX + 1)) / APP)
    assert fs.configured_app(saved(long)) == (None, "bad_value")


@pytest.mark.parametrize("name", ["闪购助手 2.app", "Other.app", "闪购助手", APP + ".zip", "闪购助手.APP"])
def test_only_the_helper_by_name_is_used(name, tmp_path):
    """§5.2：只认名字对得上的 .app。iCloud 桌面上整文件夹替换留下的「闪购助手 2.app」也不认。"""
    assert fs.configured_app(saved(str(tmp_path / name))) == (None, "wrong_name")


# ---- 找：指定的优先，不存在就不用 ---------------------------------------------------------------

def test_the_configured_location_comes_first(tmp_path):
    configured = tmp_path / "Desktop" / APP
    configured.mkdir(parents=True)
    standard = installed_app(tmp_path)
    assert fs.app_search_paths(candidates=[standard], configured=configured) == (configured, standard)
    assert fs.app_search_paths(candidates=[standard]) == (standard,)
    assert fs.find_app(platform="darwin", candidates=[standard], configured=configured) == configured


def test_a_configured_location_that_is_not_there_is_skipped(tmp_path):
    standard = installed_app(tmp_path)
    gone = tmp_path / "gone" / APP
    assert fs.find_app(platform="darwin", candidates=[standard], configured=gone) == standard
    assert fs.find_app(platform="darwin", candidates=[tmp_path / "nope" / APP], configured=gone) is None
    a_file = tmp_path / "file" / APP
    a_file.parent.mkdir()
    a_file.write_text("not a bundle", encoding="utf-8")
    assert fs.find_app(platform="darwin", candidates=[standard], configured=a_file) == standard


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_off_macos_even_a_configured_app_is_not_found(platform, tmp_path):
    configured = tmp_path / APP
    configured.mkdir()
    assert fs.find_app(platform=platform, candidates=[], configured=configured) is None


# ---- 9334 上的 /status 像不像闪购助手（launch-7 B 用） -------------------------------------------

def test_a_status_from_the_contract_looks_like_the_helper():
    assert fs.status_refusal("ok", sample("running")) is None
    assert fs.status_refusal("ok", sample("idle")) is None


@pytest.mark.parametrize("kind, value, code", [
    ("unreachable", None, "not_ok"),
    ("bad_response", "no_answer", "not_ok"),
    ("bad_response", "json", "not_ok"),
    ("ok", [1], "not_ok"),
    ("ok", "text", "not_ok"),
    ("ok", {}, "schema"),
    ("ok", {"status": "ok"}, "schema"),
    ("ok", dict(sample("running"), schema=2), "schema"),
    ("ok", dict(sample("running"), page="somewhere"), "page"),
    ("ok", dict(sample("running"), page=None), "page"),
])
def test_anything_else_does_not(kind, value, code):
    """同 StatusTracker.control_refusal 的三条：连上并读懂了、schema 是 1、page 是契约里的代码。"""
    assert fs.status_refusal(kind, value) == code


# ---- open 失败时系统给的错误号（launch-8，纯函数） ------------------------------------------------

LS_10669 = ("The application /Applications/Fictional.app cannot be opened for an unexpected reason, "
            "error=Error Domain=NSOSStatusErrorDomain Code=-10669 \"(null)\" "
            "UserInfo={_LSLine=4087, _LSFunction=_LSOpenStuffCallLocal}\n")
LS_OLD = "LSOpenURLsWithRole() failed with error -10810 for the file /Applications/Fictional.app.\n"
RBS = ("The application cannot be opened for an unexpected reason, error=Error Domain=RBSRequestErrorDomain Code=5 "
       "\"Launch failed.\" UserInfo={NSLocalizedFailureReason=Launch failed., NSUnderlyingError=0x600 "
       "{Error Domain=NSPOSIXErrorDomain Code=153 \"Unknown error: 153\" UserInfo={}}}\n")


@pytest.mark.parametrize("text, codes", [
    (LS_10669, (-10669,)),
    (LS_OLD, (-10810,)),
    (RBS, (5, 153)),
    (LS_10669 + LS_10669, (-10669,)),                         # 同一个号只记一次
    ("Code=1 Code=2 Code=3 Code=4", (1, 2, 3)),               # 最多 OPEN_CODES_MAX 个
    ("Unable to find application named 'Fictional'\n", ()),
    ("The file /Applications/Fictional.app does not exist.\n", ()),
    ("_LSLine=4087 error=Error", ()),                         # 别的数不算
    ("", ()),
    (None, ()),
])
def test_open_error_codes_are_read_as_written(text, codes):
    assert fs.open_error_codes(text) == codes
    assert fs.OPEN_CODES_MAX == 3


def test_open_output_becomes_one_short_line():
    assert fs.open_output_text(b"line one\n\tline two\r\n") == "line one line two"
    assert fs.open_output_text(b"bad \xff byte\x00\x1b[31m") == "bad � byte[31m"
    assert fs.open_output_text(b"") == "" and fs.open_output_text(None) == ""
    long = fs.open_output_text(b"x" * 5000)
    assert len(long) == fs.OPEN_OUTPUT_SHOW + 1 and long.endswith("…")


# ---- 接线：启动时 -----------------------------------------------------------------------------

def test_not_found_and_nothing_on_9334_means_no_button_and_one_line(tmp_path, capsys):
    p = bare_pipeline()
    p._flash_fetch = fetch = fake_fetch(("unreachable", None))
    gone = tmp_path / "Applications" / APP
    attach(p, [gone])
    assert "flash_sale" not in p.server.config and "flash_start_enabled" not in p.server.config
    assert p._flash_task is None and p._flash_app is None and not p._flash_on
    assert fetch.calls == ["http://127.0.0.1:9/status"]          # 看一次，tests/conftest.py 换过的地址
    assert out_lines(capsys) == [not_found_line(gone)]
    assert p.handle_control({"type": "flash_control", "action": "stop"}) is None and p._flash_ctl is None
    assert find(p) is None and p.server.sent == []               # 没有按钮就没有浮层，再找也什么都不做


@pytest.mark.parametrize("result, line", [
    (("ok", dict(sample("running"), schema=2)), "[信息] 闪购助手：127.0.0.1:9 的 /status 不像闪购助手的回话（schema），"
                                                 "不显示闪购按钮"),
    (("ok", {"status": "ok"}), "[信息] 闪购助手：127.0.0.1:9 的 /status 不像闪购助手的回话（schema），不显示闪购按钮"),
    (("bad_response", "no_answer"), None),
])
def test_not_found_and_something_else_on_9334_means_no_button(result, line, tmp_path, capsys):
    p = bare_pipeline()
    p._flash_fetch = fake_fetch(result)
    gone = tmp_path / "Applications" / APP
    attach(p, [gone])
    assert "flash_sale" not in p.server.config and not p._flash_on
    assert out_lines(capsys) == [not_found_line(gone)] + ([line] if line else [])


def test_launch_7_not_found_but_the_helper_answers_shows_the_button_without_open(tmp_path, capsys):
    """用户选 B：不在「应用程序」里、但正开着的闪购助手照样有按钮，浮层里能看状态、能停止，只是没有「打开闪购助手」。"""
    p = bare_pipeline()
    gone = tmp_path / "Applications" / APP
    script = Script(p, [sample("running"), sample("running")])

    async def go():
        await p.attach_flash_sale(platform="darwin", candidates=[gone])
        assert p._flash_on and p._flash_app is None and p._flash_task is not None
        assert p.server.config["flash_sale"] == dict(fs.empty_state("checking"), app_found=False)
        assert p.server.config["flash_start_enabled"] is (fs.START_ENABLED is True)
        with pytest.raises(Done):
            await p._flash_task

    asyncio.run(go())
    assert len(script.urls) == 2                                  # 启动时看的那一次，加上轮询的第一跳
    state = p.server.config["flash_sale"]
    assert state["conn"] == "ok" and state["app_found"] is False
    assert [m["app_found"] for m in p.server.of_type("flash_sale")] == [False]
    assert out_lines(capsys) == [
        not_found_line(gone),
        "[信息] 闪购助手：没找到程序，但 127.0.0.1:9 的 /status 像闪购助手的回话，开始查看它的状态（没有「打开闪购助手」）",
        "[信息] 闪购助手：已连上 127.0.0.1:9（版本 0.7.2）",
    ]
    # 没有「打开」：页面上不画；本机脚本硬发也什么都不做
    exe = FakeExec(rc=0)
    p._flash_exec = exe
    assert p.handle_control({"type": "open_flash_helper"}) is None
    assert not p._flash_opening and exe.calls == []


def test_launch_7_stop_still_goes_out_without_the_app(tmp_path):
    post, token = FakePost(), FakeToken()
    p, clock = control_pipeline(tmp_path, post=post, token=token)
    p._flash_app = None                    # 启动时没找到 .app，靠 9334 上的 /status 接上的
    poll(p, clock, sample("running"))
    click(p, {"type": "flash_control", "action": "stop"})
    assert post.calls == [("stop", FAKE_TOKEN, "http://127.0.0.1:9")]


def test_found_states_say_app_found(tmp_path):
    app = installed_app(tmp_path)
    p = bare_pipeline()
    attach(p, [app])
    assert p.server.config["flash_sale"] == dict(fs.empty_state("checking"), app_found=True)
    assert p._flash_on and p._flash_app == app


def test_the_configured_location_is_used_first(tmp_path, capsys):
    desktop = tmp_path / "Desktop" / "闪购助手" / APP
    desktop.mkdir(parents=True)
    standard = installed_app(tmp_path)
    write_settings({"flash_sale": {"app_path": str(desktop)}, "target_lang": "zh-CN"})
    p = bare_pipeline()
    attach(p, [standard])
    assert p._flash_app == desktop
    assert out_lines(capsys) == ["[信息] 闪购助手：已装，开始查看 127.0.0.1:9 的状态"]


@pytest.mark.parametrize("value, line", [
    ("{gone}", "[警告] 闪购助手：settings.json 的 flash_sale.app_path 指的 {gone} 不存在（或不是文件夹），没用它"),
    ("Desktop/" + APP, "[警告] 闪购助手：settings.json 的 flash_sale.app_path 不是完整路径，没用它"),
    ("{other}", "[警告] 闪购助手：settings.json 的 flash_sale.app_path 指的不是 " + APP + "，没用它"),
    (42, "[警告] 闪购助手：settings.json 的 flash_sale.app_path 不是一个路径，没用它"),
])
def test_an_unusable_configured_location_is_skipped_with_one_line(value, line, tmp_path, capsys):
    standard = installed_app(tmp_path)
    gone, other = tmp_path / "gone" / APP, tmp_path / "Other.app"
    if isinstance(value, str):
        value = value.format(gone=gone, other=other)
    write_settings(saved(value))
    p = bare_pipeline()
    attach(p, [standard])
    assert p._flash_app == standard
    assert out_lines(capsys) == [line.format(gone=gone), "[信息] 闪购助手：已装，开始查看 127.0.0.1:9 的状态"]


def test_a_broken_settings_file_does_not_block_finding(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "_corrupt", {"backup": None, "announced": False})   # 备份的记号不漏给别的用例
    standard = installed_app(tmp_path)
    settings.SETTINGS_FILE.write_text("{not json", encoding="utf-8")
    p = bare_pipeline()
    attach(p, [standard])
    assert p._flash_app == standard


def test_the_settings_are_only_read():
    """第 2 步只读这个键；「选一下它在哪」（写）在第 6 步的首页「工具」行里。"""
    p = bare_pipeline()
    assert p._flash_settings is None                 # None = 调用那一刻取 settings.load_settings（tests/conftest.py 换过文件）


# ---- 接线：打开浮层时再找一次 --------------------------------------------------------------------

def helper_without_app(tmp_path):
    """launch-7 B 的样子：启动时没找到 .app，9334 上的 /status 像闪购助手的回话。"""
    p = bare_pipeline()
    p._flash_fetch = fake_fetch(("ok", sample("running")))
    where = tmp_path / "Applications" / APP
    attach(p, [where])
    assert p._flash_on and p.server.config["flash_sale"]["app_found"] is False
    return p, where


def test_opening_the_panel_finds_an_app_installed_after_launch(tmp_path, capsys):
    p, where = helper_without_app(tmp_path)
    capsys.readouterr()
    where.mkdir(parents=True)                         # 下播后拖进了「应用程序」
    find(p)
    assert p._flash_app == where
    assert p.server.config["flash_sale"]["app_found"] is True
    assert p.server.of_type("flash_sale")[-1]["app_found"] is True
    assert out_lines(capsys) == ["[信息] 闪购助手：找到了 {}".format(where)]
    before = list(p.server.sent)
    find(p)                                           # 没变：不广播、不打印
    assert p.server.sent == before and out_lines(capsys) == []
    exe = FakeExec(rc=0)
    p._flash_exec = exe
    click(p)
    assert [list(a) for a, _k in exe.calls] == [["open", "-a", str(where)]]


def test_opening_the_panel_notices_the_app_is_gone(tmp_path, capsys):
    app = installed_app(tmp_path)
    p = bare_pipeline()
    attach(p, [app])
    capsys.readouterr()
    app.rmdir()
    find(p)
    assert p._flash_app is None and p._flash_on
    assert p.server.config["flash_sale"]["app_found"] is False
    assert p.server.of_type("flash_sale")[-1]["app_found"] is False
    assert out_lines(capsys) == [not_found_line(app)]


def test_opening_the_panel_reads_a_location_configured_after_launch(tmp_path, capsys):
    p, where = helper_without_app(tmp_path)
    desktop = tmp_path / "Desktop" / APP
    desktop.mkdir(parents=True)
    write_settings(saved(str(desktop)))
    find(p)
    assert p._flash_app == desktop and p.server.config["flash_sale"]["app_found"] is True


def test_finding_never_touches_9334(tmp_path):
    p, where = helper_without_app(tmp_path)

    async def boom(url):
        raise AssertionError("再找只看文件系统")
    p._flash_fetch = boom
    where.mkdir(parents=True)
    find(p)
    assert p._flash_app == where


def test_flash_find_takes_no_arguments(tmp_path):
    """页面发什么都进不了找的位置：只认 settings.json 和写死的候选位置。"""
    p, where = helper_without_app(tmp_path)
    elsewhere = tmp_path / "elsewhere" / APP
    elsewhere.mkdir(parents=True)

    async def go():
        result = p.handle_control({"type": "flash_find", "path": str(elsewhere), "app_path": str(elsewhere)})
        if asyncio.iscoroutine(result):
            await result
    asyncio.run(go())
    assert p._flash_app is None


# ---- 「打开闪购助手」失败时（launch-8） ------------------------------------------------------------

def test_open_reads_what_open_wrote_to_stderr(tmp_path):
    exe = FakeExec(rc=0)
    p, _clock = opening_pipeline(tmp_path, exe)
    click(p)
    (_argv, kwargs), = exe.calls
    assert kwargs == {"stdout": asyncio.subprocess.DEVNULL, "stderr": asyncio.subprocess.PIPE}


@pytest.mark.parametrize("stderr, zh, en, term", [
    (LS_10669.encode(),
     "没能打开闪购助手（open 返回 1，系统错误号 -10669）。可以在访达里双击闪购助手，再试一次。",
     "Couldn’t open Flash Sale Helper (open returned 1, system error -10669). "
     "Double-click Flash Sale Helper in Finder to try again.",
     "[警告] 闪购助手：open 返回 1，没有打开（系统错误号 -10669）"),
    (RBS.encode(),
     "没能打开闪购助手（open 返回 1，系统错误号 5、153）。可以在访达里双击闪购助手，再试一次。",
     "Couldn’t open Flash Sale Helper (open returned 1, system errors 5, 153). "
     "Double-click Flash Sale Helper in Finder to try again.",
     "[警告] 闪购助手：open 返回 1，没有打开（系统错误号 5、153）"),
    (b"Unable to find application named 'Fictional'\n",
     "没能打开闪购助手（open 返回 1）。可以在访达里双击闪购助手，再试一次。",
     "Couldn’t open Flash Sale Helper (open returned 1). Double-click Flash Sale Helper in Finder to try again.",
     "[警告] 闪购助手：open 返回 1，没有打开"),
])
def test_open_failure_says_the_return_code_and_the_system_error_numbers(stderr, zh, en, term, tmp_path, capsys):
    exe = FakeExec(rc=1, stderr=stderr)
    p, _clock = opening_pipeline(tmp_path, exe)
    click(p)
    (notice,) = p.server.of_type("notice")
    assert notice["where"] == "flash" and notice["text"] == zh
    with i18n.use("en"):
        rendered = i18n.render(notice)["text"]
    assert rendered == en and not CJK.search(rendered)
    lines = out_lines(capsys)
    assert lines[-2:] == [term, "[警告] 闪购助手：open 的输出：" + fs.open_output_text(stderr)]


def test_open_failure_without_output_writes_no_output_line(tmp_path, capsys):
    p, _clock = opening_pipeline(tmp_path, FakeExec(rc=1))
    click(p)
    assert out_lines(capsys)[-1] == "[警告] 闪购助手：open 返回 1，没有打开"


def test_open_failure_text_guesses_no_cause():
    """第八条：只写返回码和系统给的号，再说能做什么；不解释那个号是什么意思。"""
    from tests.i18n_rules import EN_CAUSAL, EN_LABELS, ZH_CAUSAL, ZH_LABELS
    p = bare_pipeline()
    for rc, codes in ((1, (-10669,)), (1, (5, 153)), (1, ()), (None, ())):
        text = p._flash_open_failure_text(rc, codes)
        with i18n.use("en"):
            en = i18n.text(text)
        assert not (ZH_LABELS.search(text) or ZH_CAUSAL.search(text)), text
        assert not (EN_LABELS.search(en) or EN_CAUSAL.search(en)), en
        assert "访达" in text and "Finder" in en
