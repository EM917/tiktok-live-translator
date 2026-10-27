"""页面文件的版本 config["build"]（专家审查 F8 (c)）。

浏览器模式下一键更新重启后，旧标签页重连上新进程，跑的却还是旧 JS：hello 里的 version 只用来改页脚文字。
现在服务端启动时按 web/ 的内容算出一个短哈希放进 config["build"]，页面记下第一次连上时的值，之后哪一次
hello 里的不一样就重新载入（web/app.js reloadForBuild，tests/stale-tab.test.mjs）。不 import main。
"""
import asyncio
import re
import shutil

import aiohttp

from app import server as server_mod
from app.server import CaptionServer, web_build
from tests.helpers import run


def test_the_build_is_a_short_stable_hash_of_the_page_files():
    build = web_build()
    assert re.fullmatch(r"[0-9a-f]{12}", build)
    assert web_build() == build
    assert web_build(server_mod.WEB_DIR) == build


def test_any_page_file_change_changes_the_build_and_dot_files_do_not(tmp_path):
    web = tmp_path / "web"
    shutil.copytree(str(server_mod.WEB_DIR), str(web))
    first = web_build(web)
    assert first == web_build(), "同样的内容，同样的 build：和放在哪儿无关"
    (web / ".DS_Store").write_bytes(b"finder")
    assert web_build(web) == first, "Finder 留下的点开头文件不算"
    app_js = web / "app.js"
    app_js.write_text(app_js.read_text(encoding="utf-8") + "\n// 改了一个字\n", encoding="utf-8")
    second = web_build(web)
    assert second != first
    app_js.rename(web / "app2.js")
    assert web_build(web) != second, "改名也算换了一套页面文件"


def test_no_page_files_means_no_build(tmp_path):
    assert web_build(tmp_path / "missing") == ""
    (tmp_path / "empty").mkdir()
    assert web_build(tmp_path / "empty") == ""


def test_a_started_server_hands_the_build_to_every_page_in_hello():
    async def go():
        server = CaptionServer(port=0)
        assert "build" not in server.config, "只在真起服务时算（测试里大量直接建 CaptionServer）"
        await server.start()
        try:
            port = server._runner.addresses[0][1]
            hellos = []
            async with aiohttp.ClientSession() as session:
                for _ in range(2):            # 刷新、重连拿到的是同一个值
                    async with session.ws_connect("http://127.0.0.1:{}/ws".format(port)) as ws:
                        hellos.append(await asyncio.wait_for(ws.receive_json(), 5.0))
            return hellos
        finally:
            await server._runner.cleanup()

    hellos = run(go())
    assert [h["type"] for h in hellos] == ["hello", "hello"]
    assert hellos[0]["config"]["build"] == hellos[1]["config"]["build"] == web_build()


def test_the_floating_window_does_not_get_the_build():
    """浮窗页（role=float）只收 FLOAT_CONFIG_KEYS 那几个键：它不按 build 重新载入，别多给它东西。"""
    assert "build" not in server_mod.FLOAT_CONFIG_KEYS
