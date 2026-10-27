"""让 `import app.xxx` 在任何工作目录下都成立。

测试只依赖 pytest + numpy + aiohttp——被测模块的顶层导入都不含
faster-whisper / yt-dlp 等重型依赖（重型导入全部在函数内延迟进行）。
"""
import itertools
import os
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
def _keep_transcripts_out_of_the_real_folder(tmp_path, monkeypatch):
    """字幕记录（app/transcript.py）同理：测试写的记录必须落在临时目录，不能在开发者的
    transcripts/ 里留下夹具字幕和虚构弹幕，更不能让「导出最近一场」导出到别人的真实记录。
    autouse：靠每个测试自己记得传 directory，已经被审计那一份证明会漏。"""
    from app import transcript

    monkeypatch.setattr(transcript, "TRANSCRIPT_DIR", tmp_path / "transcripts")


@pytest.fixture(autouse=True)
def _comment_guard_emergency_switch_is_not_inherited(monkeypatch):
    """TLT_COMMENT_GUARD=off 是弹幕留意的应急总开关（app/pipeline_guard.py）：开发机上设过它，
    不该让依赖「守卫开着」的用例变红。要测应急开关的用例自己 setenv。"""
    monkeypatch.delenv("TLT_COMMENT_GUARD", raising=False)


@pytest.fixture
def v1_reconnect(monkeypatch):
    """用例测的是 V1 的重连规则（每次都重新解析、连续无声几轮就放弃、「第 N/M 次」）：显式钉在 V1 上，
    不靠 RECONNECT_V2 的默认值（CLAUDE.md 第九条）。V2 的规则在 tests/test_reconnect_v2.py。"""
    from app import reconnect_policy
    monkeypatch.setattr(reconnect_policy, "RECONNECT_V2", False)


@pytest.fixture(autouse=True)
def _reconnect_rules_switch_is_not_inherited(monkeypatch):
    """TLT_RECONNECT_V2=0 是重连新规则的应急开关（app/reconnect_policy.py）：开发机上设过它，不该让
    依赖默认规则的用例换一套规则跑。要测哪一边的用例自己 monkeypatch reconnect_policy.RECONNECT_V2。"""
    monkeypatch.delenv("TLT_RECONNECT_V2", raising=False)


@pytest.fixture(scope="session")
def _fresh_dir(tmp_path_factory):
    """给下面几个 autouse fixture 发目录：每次调用返回一个全新、空、只给这一个用例的目录。会话开始时用
    tmp_path_factory 建一个父目录，之后每次在里面按自增编号 mkdir 一个子目录。

    为什么不在每个用例里直接 tmp_path_factory.mktemp()：带编号的 mktemp（_pytest/pathlib.py 的
    make_numbered_dir）每次都把 basetemp 根目录整个扫一遍找最大编号，而每个用例又往根目录里添条目
    （它自己的 tmp_path 也是这么建的），六千个用例下来是 O(N²)。#106 给每个用例多加了一次，CI 的 Windows
    跑器（扫目录慢）全量测试就从 11 分 17 秒涨过了 15 分钟的上限，连续四次跑到九成多就到点被取消（#106 自己那次
    PR 的 CI 碰上一台快约三成的跑器，699 秒过了）；拿那几次的日志拟合，多出来的时间和用例序号的平方成正比。在一个父目录里 mkdir 什么都不扫，每个用例 O(1)。

    编号来自进程内计数器，同一个会话里不会重复；mkdir 不带 exist_ok，真撞上已有的目录就当场报错，
    不会悄悄发出一个别人用过的目录。父目录在 basetemp 里、不在任何用例的 tmp_path 里。"""
    root = tmp_path_factory.mktemp("per-test")
    numbers = itertools.count()

    def fresh(name):
        path = root / "{}-{}".format(name, next(numbers))
        path.mkdir()
        return path

    return fresh


@pytest.fixture(autouse=True)
def _no_claude_key_is_inherited(monkeypatch, _fresh_dir):
    """开发机上设过 ANTHROPIC_API_KEY、或者在界面里存过 Claude 密钥（settings.json 的 api_keys）的话，选了 Claude
    的用例会造出真的复核后端，探针还会带着真密钥去联网。api_key() 先看环境变量、再看 settings.json，所以两处都要
    隔开：环境变量删掉，SETTINGS_FILE 指到一个空的临时目录。要密钥的用例自己 setenv，并换掉发请求的那一层
    （tests/test_comment_review.py 的假 HTTP），测试从不联网；要读写设置文件的用例自己再 monkeypatch SETTINGS_FILE
    （它们本来就这么做）。"""
    from app import settings

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("TLT_GUARD_CLAUDE_MODEL", raising=False)
    monkeypatch.setattr(settings, "SETTINGS_FILE", _fresh_dir("no-saved-keys") / "settings.json")


@pytest.fixture(autouse=True)
def _reset_active_glossary(monkeypatch):
    """会话词表的模块级全局态不能在测试之间泄漏。

    _begin_session 会 set_active()，很多管线测试都会走到它；DeepL 的术语表
    测试又会读 active()。现在测试恰好按字母序读在写之前，但那是运气不是
    设计——并行或乱序执行时就是间歇性失败。"""
    from app import glossary

    monkeypatch.setattr(glossary, "_ACTIVE", None)


# ---- 仓库里的真实词表（brands/、profiles/、glossary.txt、banned_terms.txt）测试一律不碰 ----------

# brands/<品牌>.txt、profiles/<主播>.txt、glossary.txt 是开发者本机的私人词表（不入库，见 .gitignore 与
# brands/README.md），CI 上不存在。很多管线用例曾拿开发者日常监听的那个直播间开场：Pipeline() 扫 brands/
# 并读里面每一份词表，_begin_session 读这个主播的 profile 和全局表，审计头记全局表的指纹——本机读到的是
# 开发者自己的词表，CI 上读不到，两边跑的不是同一份内容，本机改一行词表还可能改变本机的测试结果；文件
# 不在时，load() 与 _copy_template_once 的首启复制又会在仓库里凭空建出 glossary.txt、
# profiles/<那个主播>.txt（它在 profiles/ 下有入库的 .example.txt 模板）。改之前插桩数过：54 个测试文件、两千多次。
# 现在测试里的主播名一律是虚构的（真实主播名属于本机数据，不进仓库）；只有 tests/test_glossary_isolation.py
# 有意从入库模板的文件名里取那个主播，名字不写进测试。
#
# 违禁词表 banned_terms.txt 同理（不入库，决定什么会报警）：Pipeline() 的 load_detector() 读它，不在时从
# 入库模板复制一份建在仓库根上。改之前插桩数过：4 个测试文件、121 次读、1 次建。
#
# 下面的审计钩子在整个测试进程里看着这几个位置：读私人词表、往这几处写东西（建文件、建目录、删、改名）
# 都记下来。入库的 README.md 和 *.example.txt 模板是公开文件、本机和 CI 一样，只读不算
# （tests/test_readme_one_page.py 要扫 brands/README.md，tests/test_resilience_asr.py 和
# tests/test_fuzzy_recall.py 要读 banned_terms.example.txt）。
_REPO = Path(__file__).resolve().parent.parent
_REPO_GLOSSARY_DIRS = tuple(os.path.normcase(str(_REPO / name)) for name in ("brands", "profiles"))
# 前缀匹配：也罩住 migrate_legacy_entries 的备份 glossary.txt.bak-<时间>，以及 banned_terms.txt 旁边的
# 备份、临时文件。banned_terms.example.txt 不是 banned_terms.txt 开头，罩不住它
_REPO_PRIVATE_FILES = tuple(os.path.normcase(str(_REPO / name))
                            for name in ("glossary.txt", "banned_terms.txt"))
_GLOSSARY_EVENTS = frozenset(("open", "os.mkdir", "os.remove", "os.rmdir", "os.rename"))
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
# (PYTEST_CURRENT_TEST, 事件, 仓库内相对路径)。专测这张网本身的用例用 glossary_net 换成自己的列表
_GLOSSARY_NET = {"hits": []}


def _repo_glossary_path(event, args):
    """这次审计事件碰的是不是仓库里的真实词表：是就返回仓库内相对路径（正斜杠），不是返回 None。"""
    if event == "open":                  # io.open 的参数是 (路径, mode 字符串, 0)，os.open 的是 (路径, None, flags)
        mode, flags = args[1], args[2]
        writes = any(c in mode for c in "wax+") if isinstance(mode, str) else bool(flags & _WRITE_FLAGS)
        raws = args[:1]
    else:                                # 建目录、删、改名（os.replace 也报 os.rename）：都是写
        writes = True
        raws = args[:2] if event == "os.rename" else args[:1]
    for raw in raws:
        if raw is None or isinstance(raw, int):          # 文件描述符
            continue
        path = os.path.normcase(os.path.abspath(os.fsdecode(raw)))
        hit = path.startswith(_REPO_PRIVATE_FILES)
        for folder in _REPO_GLOSSARY_DIRS:
            if path == folder:
                hit = hit or writes
            elif path.startswith(folder + os.sep):
                name = os.path.basename(path)
                hit = hit or writes or not (name == os.path.normcase("README.md")
                                            or name.endswith(".example.txt"))
        if hit:
            return Path(os.path.relpath(path, os.path.normcase(str(_REPO)))).as_posix()
    return None


def _glossary_net(event, args):
    if event not in _GLOSSARY_EVENTS:
        return
    try:
        rel = _repo_glossary_path(event, args)
    except Exception:
        # 审计钩子里抛出的异常会落到被观察的那次 open 上：这张网绝不能改变被测代码的行为
        return
    if rel is not None:
        _GLOSSARY_NET["hits"].append((os.environ.get("PYTEST_CURRENT_TEST", ""), event, rel))


sys.addaudithook(_glossary_net)


@pytest.fixture(autouse=True)
def _keep_glossaries_out_of_the_repo(_fresh_dir, monkeypatch):
    """app/glossary.py 的落点全部指到每个用例自己的一个临时目录；用例结束时，这个用例里那张网记下了什么
    就让它失败。

    全局表 GLOSSARY_FILE 指到临时目录、模板 GLOSSARY_EXAMPLE 不动：load() 首启照样把入库的公开模板复制一份
    过来，和 CI 上一样（所以目录要先建好）。PROFILE_DIR、BRAND_DIR 指到还没建的目录：要用主播 profile 或品牌表的
    用例自己往里放（照 tests/test_streamer_switch.py 的虚构品牌 acme）。目录由 _fresh_dir 另开，不建在
    用例的 tmp_path 里：有用例断言 tmp_path 里只有它自己写的文件。autouse：靠每个用例自己记得
    monkeypatch，已经被几十个测试文件证明会漏。"""
    from app import glossary

    home = _fresh_dir("glossaries")
    monkeypatch.setattr(glossary, "GLOSSARY_FILE", home / "glossary.txt")
    monkeypatch.setattr(glossary, "PROFILE_DIR", home / "profiles")
    monkeypatch.setattr(glossary, "BRAND_DIR", home / "brands")
    hits = _GLOSSARY_NET["hits"]
    start = len(hits)
    yield
    touched = hits[start:]
    if touched:
        pytest.fail("这个用例碰了仓库里的真实词表 {} 次：\n{}\n测试只能用临时目录里的词表。app.glossary 的 "
                    "GLOSSARY_FILE / PROFILE_DIR / BRAND_DIR、app.pipeline 的 TERMS_FILE 已经指到临时目录；"
                    "自己拼了 ROOT / \"brands\" 这类路径的代码，改成用的那一刻从 app.glossary 取（照 "
                    "app/pipeline.py 的 _open_brands_dir）；tools/collision_audit.py、tools/replay_alerts.py "
                    "不传 --terms 时读仓库里的 banned_terms.txt，测试里要显式传。".format(
                        len(touched), "\n".join(sorted({"  {} {}".format(e, rel) for _t, e, rel in touched}))),
                    pytrace=False)


@pytest.fixture(autouse=True)
def _keep_banned_terms_out_of_the_repo(_fresh_dir, monkeypatch):
    """app.pipeline 的违禁词表 TERMS_FILE 指到每个用例自己的一份临时副本，内容是入库模板 TERMS_EXAMPLE。

    CI 上 banned_terms.txt 不存在：第一次 load_detector() 从模板复制一份，之后的用例读的都是这份副本。这里
    每个用例开场就按 load_detector 的同一种写法复制好，本机和 CI 读到的就是同一份公开模板，不再是开发者
    本机的真实违禁词表。要别的内容的用例照旧自己 monkeypatch TERMS_FILE（几十个用例本来就这么做）。
    目录由 _fresh_dir 另开，理由同上面的词表（不在 tmp_path 里，也不是每个用例一次带编号的 mktemp）。
    那张网记下的碰仓库 banned_terms.txt 由上面的 _keep_glossaries_out_of_the_repo 判失败。"""
    from app import pipeline

    terms = _fresh_dir("banned-terms") / "banned_terms.txt"
    terms.write_text(pipeline.TERMS_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(pipeline, "TERMS_FILE", terms)


@pytest.fixture
def glossary_net(monkeypatch):
    """换一张空的记录表，给专测这张网本身的用例（tests/test_glossary_isolation.py）：它们故意去碰仓库里的
    路径（用不存在的文件名，既读不到内容也不留下东西），记在这里的不算违例。"""
    hits = []
    monkeypatch.setitem(_GLOSSARY_NET, "hits", hits)
    return hits


def _glossary_net_summary(tr):
    """测试结束时把那张网记下的全部列出来：用例里的已经各自失败，不在用例里的（收集、导入阶段）只在这里。"""
    from collections import Counter

    hits = _GLOSSARY_NET["hits"]
    if not hits:
        return
    tr.write_sep("-", "测试碰了仓库里的真实词表（brands/、profiles/、glossary.txt、banned_terms.txt）")
    counts = Counter((_net_test_id(test) or "（不在用例里）", event, rel) for test, event, rel in hits)
    for (test, event, rel), n in sorted(counts.items()):
        tr.write_line("  {}  {} {}  ×{}".format(test, event, rel, n))


def pytest_sessionfinish(session):
    """碰过真实词表的用例已经各自失败；收集、导入阶段碰的不属于任何用例，也要让这次运行失败。"""
    if _GLOSSARY_NET["hits"] and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


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

# G9 运行时网的模式（spec §12.1）。迁移期是 report（违例只汇总进测试结束时的报告）；收紧闸
# （Z0）起是 strict：未标记 i18n_fixture 的用例里只要记下一条违例，这个用例就失败（见下面的
# pytest_runtest_call），英文模板写坏（占位符对不上）也当场抛，不再退回中文（i18n._bad_pair）。
# 自己把 i18n.NET 换成别的模式的用例（tests/test_i18n_server.py 专测这张网本身）不按 strict 判。
# 这张网只罩住经过真 CaptionServer.broadcast / ViewerHub.fanout 的消息。很多用例用自己的替身
# server（如 tests/test_resilience_asr.py 的 StubServer），生产函数发出的消息根本到不了
# check_outbound——比如 pipeline._announce_health 的三档提示。所以它是抽样，不是全部界面文字的
# 清单；全部的以 G4（tests/test_i18n_coverage.py，UI_FILES 全量）与 tools/i18n_pairs.py 为准。
I18N_NET_MODE = "strict"
# 带 @pytest.mark.i18n_fixture 的用例：它把中文夹具交给生产函数、再由生产函数广播出去
# （比如 _check("语音识别", …) 造的自检行经 _publish_selfcheck 发出），运行时网不算它的违例。
# Z0 按 report 模式列出的名单逐个标上（13 个）：tests/test_selfcheck_incident.py 9 个、
# tests/test_resilience_stream.py 3 个、tests/test_incidents.py 1 个
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
    """每个用例都从中文界面开始：生效语言中文、没有一次性覆盖。发布闸 I18N_ENABLED 不在这里
    定，用生产里的值：守闸关着或开着某一边语义的用例自己 monkeypatch，这样开闸和把闸改回
    False 回退时，测试都不用跟着改。

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


def _net_line(hit):
    kind, mtype, path, text, _test = hit
    where = "{}.{}".format(mtype, path) if kind == "plain" else "没登记的消息类型 {}".format(mtype)
    return "  {}  「{}」".format(where, text) if text else "  " + where


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    """G9 strict：用例本身过了之后，再看运行时网在这个用例里（setup 与 call 阶段）记下的违例，
    有就判这个用例失败。用例自己先失败了就照原样报它自己的错，不叠这一条。

    只在用例结束时 i18n.NET 仍是 strict 才判：tests/test_i18n_server.py 专测这张网本身，
    它把 NET 换成 report、NET_HITS 换成自己的列表，故意造违例再数。"""
    from app import i18n

    result = yield
    if i18n.NET != "strict" or item.nodeid in _I18N_FIXTURE_TESTS:
        return result
    hits = sorted({h for h in i18n.NET_HITS if _net_test_id(h[4]) == item.nodeid})
    if hits:
        pytest.fail("G9 运行时网（strict）：生产代码发往界面的消息里有 {} 处没写成 L() 的中文或"
                    "没登记的消息类型。\n{}\n要么在产出处补 L()（没登记的类型登记进 "
                    "app/i18n.py 的 UI_FIELDS / DATA_ONLY），要么这是测试自己交给生产函数的"
                    "中文夹具——那就给用例加 @pytest.mark.i18n_fixture。".format(
                        len(hits), "\n".join(_net_line(h) for h in hits)), pytrace=False)
    return result


def pytest_terminal_summary(terminalreporter):
    """G9：把运行时网记下的违例汇总成一份清单（spec §12.1）。-v 时逐句列出用例名。strict 模式下
    未标记用例里的违例已经让那个用例失败了，这里照样列出来，另报标了 i18n_fixture 的有几处。
    只是抽样：替身 server 发的消息不经过这张网（见 I18N_NET_MODE 上面的注释）。"""
    from collections import Counter, defaultdict

    from app import i18n

    _glossary_net_summary(terminalreporter)
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
