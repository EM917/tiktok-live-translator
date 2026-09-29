"""让 `import app.xxx` 在任何工作目录下都成立。

测试只依赖 pytest + numpy + aiohttp——被测模块的顶层导入都不含
faster-whisper / yt-dlp 等重型依赖（重型导入全部在函数内延迟进行）。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _keep_audit_out_of_the_real_log_dir(tmp_path, monkeypatch):
    """测试写的审计日志必须落在临时目录。

    一个漏传 log_dir 的测试曾把 371 段夹具字幕写进 logs/，而那个目录正是语料
    分析的输入——「hola」和两句癌症宣称被当成真实直播字幕统计了进去，占了
    两成。分母错了，用它算出来的结论也就跟着虚。

    autouse 是刻意的：靠每个测试自己记得传 log_dir，已经被证明会漏。
    """
    from app import audit

    monkeypatch.setattr(audit, "LOG_DIR", tmp_path / "logs")


@pytest.fixture(autouse=True)
def _reset_active_glossary(monkeypatch):
    """会话词表的模块级全局态不能在测试之间泄漏。

    _begin_session 会 set_active()，很多管线测试都会走到它；DeepL 的术语表
    测试又会读 active()。现在测试恰好按字母序读在写之前，但那是运气不是
    设计——并行或乱序执行时就是间歇性失败。"""
    from app import glossary

    monkeypatch.setattr(glossary, "_ACTIVE", None)


@pytest.fixture(autouse=True)
def _reset_resolver_process_state(monkeypatch):
    """resolver 里有两样进程级的状态，不能在测试之间泄漏：

    * 上一次匿名请求的时刻（_ANON）——借登录抓直播页之前要和它隔开 8 秒。前一个用例留下的
      时刻会让后一个用例**真的**去等；这里清掉，并把那段等待换成立刻返回（要验证等了多久的
      用例自己再换成记录用的假 sleep）。等完之后 resolver 会再看一眼时刻（等的时候别的任务
      可能又发过匿名请求），所以这个假 sleep 要让「时间过去了」：把记下的时刻往回拨同样多。
    * 在途的浏览器登录读取（_LOGIN_READS）——生产里同一个浏览器同一时刻只读一次，后来的
      调用接在在途的那一次上；测试里前一个用例故意读得很慢的假读取，不能被后一个用例接上。
    """
    from app import resolver

    async def no_wait(seconds):
        if resolver._ANON["last"] is not None:
            resolver._ANON["last"] -= seconds

    monkeypatch.setitem(resolver._ANON, "last", None)
    monkeypatch.setattr(resolver, "_gap_sleep", no_wait)
    resolver._LOGIN_READS.clear()
    yield
    resolver._LOGIN_READS.clear()


# ---- 英文界面（app/i18n.py） -------------------------------------------------------------

# G9 运行时网的模式（spec §12.1）。迁移期是 report：违例只汇总进测试结束时的报告；收紧闸（Z0）
# 时改成 strict，未标记用例里的违例直接失败。
# 这张网只罩住经过真 CaptionServer.broadcast / ViewerHub.fanout 的消息。很多用例用自己的替身
# server（如 tests/test_resilience_asr.py 的 StubServer），生产函数发出的消息根本到不了
# check_outbound——比如 pipeline._announce_health 的三档提示。所以报告是抽样，不是剩余工作量的
# 完整清单；完整的以 G4 的逐文件 `i18n: done` 标记与 tools/i18n_pairs.py 为准。
I18N_NET_MODE = "report"
# 带 @pytest.mark.i18n_fixture 的用例：它把中文夹具交给生产函数、再由生产函数广播出去
# （比如 _check("语音识别", …) 造的自检行经 _publish_selfcheck 发出），运行时网不算它的违例
_I18N_FIXTURE_TESTS = set()


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "i18n_fixture: 用例把中文夹具交给生产函数广播，G9 运行时网不算这个用例的违例")


def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.get_closest_marker("i18n_fixture") is not None:
            _I18N_FIXTURE_TESTS.add(item.nodeid)


@pytest.fixture(autouse=True)
def _chinese_ui_on_any_machine(monkeypatch):
    """每个用例都从「闸关着时生产里的样子」开始：界面语言中文、没有一次性覆盖。

    与跑测试的那台机器无关（spec §3.2）：系统语言检测换成「检测不了」，三个 TLT_* 环境变量
    删掉——CI 的 Windows / macOS 跑器、开发机的系统语言都不该让结果不同。模块级状态
    （生效语言、已经警告过的坏模板）不许在用例之间泄漏。G9 运行时网全程开着。"""
    from app import i18n

    monkeypatch.setattr(i18n, "system_lang", lambda: None)
    for name in (i18n.ENV_OVERRIDE, i18n.ENV_SYSTEM, i18n.ENV_INSTALL):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(i18n, "_state", {"choice": "system", "lang": i18n.ZH, "system": None,
                                         "override": None})
    monkeypatch.setattr(i18n, "_bad_once", set())
    monkeypatch.setattr(i18n, "NET", I18N_NET_MODE)


def _net_test_id(current):
    """PYTEST_CURRENT_TEST 形如 "tests/test_x.py::test_y[a] (call)"：去掉阶段，剩下用例 id。"""
    return current.rsplit(" (", 1)[0] if current.endswith(")") else current


def pytest_terminal_summary(terminalreporter):
    """G9 report：把运行时网记下的违例汇总成一份清单（spec §12.1）。-v 时逐句列出用例名。
    只是抽样：替身 server 发的消息不经过这张网（见 I18N_NET_MODE 上面的注释）。"""
    from collections import Counter, defaultdict

    from app import i18n

    hits = sorted(set(i18n.NET_HITS))           # broadcast 和 fanout 各查同一条消息一次：去重
    counted = [h for h in hits if _net_test_id(h[4]) not in _I18N_FIXTURE_TESTS]
    skipped = len(hits) - len(counted)
    if not hits:
        return
    tr = terminalreporter
    tr.write_sep("-", "i18n 运行时网（G9，{} 模式）".format(I18N_NET_MODE))
    tr.write_line("只含经过真 CaptionServer / ViewerHub 的消息；用替身 server 的用例不在内。"
                  "这是抽样，完整清单以 G4 标记与 tools/i18n_pairs.py 为准")
    plain = [h for h in counted if h[0] == "plain"]
    unregistered = [h for h in counted if h[0] == "unregistered"]
    sentences = {(h[1], h[2], h[3]) for h in plain}
    tr.write_line("界面字段上的普通中文 str（漏写了 L()）：{} 句，涉及 {} 个用例".format(
        len(sentences), len({_net_test_id(h[4]) for h in plain})))
    by_field = Counter("{}.{}".format(mtype, path) for mtype, path, _ in sentences)
    for field, n in sorted(by_field.items(), key=lambda kv: (-kv[1], kv[0])):
        tr.write_line("  {:<36} {} 句".format(field, n))
    if unregistered:
        tr.write_line("没登记在 i18n.UI_FIELDS / DATA_ONLY 里的消息类型：{}".format(
            "、".join(sorted({h[1] for h in unregistered}))))
    if skipped:
        tr.write_line("标了 i18n_fixture 的用例另有 {} 处，不计".format(skipped))
    if tr.config.option.verbose <= 0:
        tr.write_line("加 -v 逐句列出（含用例名）")
        return
    tests = defaultdict(set)
    for kind, mtype, path, text, test in counted:
        tests[(kind, mtype, path, text)].add(_net_test_id(test))
    for (kind, mtype, path, text), names in sorted(tests.items()):
        where = "{}.{}".format(mtype, path) if kind == "plain" else "类型 {}".format(mtype)
        tr.write_line("{}  「{}」".format(where, text) if text else where)
        for name in sorted(names):
            tr.write_line("    " + (name or "（不在用例里）"))
