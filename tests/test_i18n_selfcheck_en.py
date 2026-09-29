"""启动自检的英文（spec §12.1 G7 / G8，迁移提交 M6）。

自检每一行的 name / detail / fix 都写成 L()：终端、审计拿中文，桌面页在发出去的那一刻换成英文。
这里把 app/selfcheck.py 能产出的每一种行都真的跑出来（环境用替身造，数据全是 ASCII），断言：

- G7：英文界面上这些行没有中文，也没有没填上的 {占位符}；英文模板写坏会当场抛（NET=strict），
  不会悄悄退回中文；
- 拼接出来的长句（安装命令接在说明后面、违禁词表的几条提示接成一段、单复数）英文语序对，整句钉住；
- G8：同一批行经 Pipeline.run_selfcheck 发出，审计 JSONL 与终端输出在中英两种界面下逐字节相同。

只断言本模块自己的句子（spec §12.1 G7「每个 M 只断言自己拥有的句子」）。别的模块拼进来的文字——
bootstrap.mlx_giveup_note、localmodel.install_hint、ffmpeg_bin.ffmpeg_source、browser_login 的
LOGIN_STEPS / observed_text / steps_text、detector 的 load_warnings——这里都换成 ASCII 替身：
它们的英文归各自的迁移提交，跨模块组合留给收紧闸的提交（Z0）。
"""
import re
import sys
from types import SimpleNamespace

import pytest

from app import i18n, selfcheck
from app.i18n import CJK
from tests.helpers import run
from tests.test_i18n_backend_en import assert_language_independent, run_in_both_languages

PY = "/opt/py/bin/python3"
# 别的模块交进来的文字：替身，全 ASCII，结尾不带句号（与它们的中文一样）
NOTE = "GPU acceleration didn't install on 2026-09-01"            # bootstrap.mlx_giveup_note
HINT = "Install Ollama from ollama.com, then reopen the app."      # localmodel.install_hint
WARNING = "Line 7 'foo # x' can't match: put the comment on its own line"   # detector.load_warnings
OBSERVED = "Chrome: no TikTok login"                               # browser_login.observed_text

ENGLISH_NAMES = {"Audio (ffmpeg)", "Noise Reduction", "Speech Recognition", "Translation Engine",
                 "Banned-Term List", "Glossary", "Audit Log", "Stream Lookup", "Browser Login",
                 "Comments", "Storage"}
UNFILLED = re.compile(r"\{[^{}]*\}")


def _now(coro):
    """跑一个在第一次 await 之前就 return 的协程（不开事件循环）。用在要临时改 sys.platform /
    sys.version_info 的分支上：只在这一步里改，不让事件循环在改过的环境里跑。"""
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    coro.close()
    raise AssertionError("协程在返回之前 await 了")


class Engine:
    """管线建好的翻译引擎替身：自检只看 name 和 inner 上的几个属性。"""

    def __init__(self, name, **inner):
        self.name = name
        self.inner = SimpleNamespace(**inner)


class Detector:
    """违禁词表检测器替身：check_watchlist 只看这几个属性。"""

    def __init__(self, count=3, effective=None, warnings=(), decode_error=None, skipped=(),
                 read_error=None, enabled=True):
        self.count, self.enabled, self.read_error = count, enabled, read_error
        self.effective_count = count if effective is None else effective
        self.load_warnings = [{"text": w} for w in warnings]
        self.decode_error, self.skipped_lines = decode_error, list(skipped)
        self.source_path = "/data/banned_terms.txt"


class Unwritable:
    """logs/ 建不出来：mkdir 抛 OSError（不靠真实文件系统，报错文字在任何系统上都是 ASCII）。"""
    parent = "/data/app"

    def mkdir(self, **kwargs):
        raise OSError(30, "Read-only file system")

    def exists(self):
        return False

    def __truediv__(self, other):
        return self


# ---- 每一种行都跑出来 -------------------------------------------------------------------------

def _ffmpeg(mp, tmp_path):
    from app import ffmpeg_bin
    mp.setattr(ffmpeg_bin, "ffmpeg_source", lambda: "bundled")
    mp.setattr(ffmpeg_bin, "find_ffmpeg", lambda: None)
    rows = [run(selfcheck.check_ffmpeg())]
    mp.setattr(ffmpeg_bin, "find_ffmpeg", lambda: "/fake/ffmpeg")
    for runs in (False, True):
        mp.setattr(selfcheck, "_ffmpeg_runs", lambda exe, runs=runs: runs)
        rows.append(run(selfcheck.check_ffmpeg()))
    return rows


def _denoise(mp, tmp_path):
    from app import pipeline
    model = tmp_path / "bd.rnnn"
    mp.setattr(pipeline, "DENOISE_MODEL", model)
    rows = [run(selfcheck.check_denoise(SimpleNamespace(denoise="off"))),
            run(selfcheck.check_denoise(SimpleNamespace(denoise="auto")))]       # 模型还没下载
    model.write_bytes(b"x" * 2048)
    for works in (True, False):
        mp.setattr(pipeline, "_arnndn_probe", lambda path, works=works: works)
        rows.append(run(selfcheck.check_denoise(SimpleNamespace(denoise="auto"))))
    return rows


def _asr(mp, tmp_path):
    from app import bootstrap, hwdetect
    args = SimpleNamespace(backend="auto", model=None, device="auto")
    rows = []
    for error in ("RuntimeError: boom", None):
        rows.append(run(selfcheck.check_asr(args, {"fallback": {
            "from": "mlx", "to": "ct2 (cpu)", "error": error}})))
        rows.append(run(selfcheck.check_asr(args, {"load_error": {
            "backend": "mlx", "model": "large-v3-turbo", "error": error}})))

    def no_hardware(**kwargs):
        raise RuntimeError("sysctl failed")

    mp.setattr(hwdetect, "recommend", no_hardware)
    rows.append(run(selfcheck.check_asr(args)))
    backend = {"name": "mlx"}
    mp.setattr(hwdetect, "recommend",
               lambda **kwargs: {"backend": backend["name"], "model": "large-v3-turbo"})
    mp.setattr(selfcheck, "_importable", lambda name: False)
    for name in ("mlx", "ct2"):                     # 加速组件 / faster-whisper 没装上
        backend["name"] = name
        rows.append(run(selfcheck.check_asr(args)))
    mp.setattr(selfcheck, "_importable", lambda name: True)
    note = {"text": None}
    mp.setattr(bootstrap, "mlx_giveup_note", lambda root: note["text"])
    for apple in (False, True):                     # 苹果芯片却在用 CPU：另一句
        mp.setattr(hwdetect, "detect", lambda apple=apple: {"apple_silicon": apple})
        for cached in (True, False):
            mp.setattr(selfcheck, "_model_cached", lambda m, b, cached=cached: cached)
            rows.append(run(selfcheck.check_asr(args)))
    note["text"] = NOTE                             # 装 GPU 组件放弃过：给手动安装的命令
    rows.append(run(selfcheck.check_asr(args)))
    return rows


def _translator(mp, tmp_path):
    from app import localmodel
    from app import translator as tr
    mp.setattr(localmodel, "install_hint", lambda: (HINT, "https://ollama.com/download"))
    auto = SimpleNamespace(translator="auto")
    rows = [run(selfcheck.check_translator(SimpleNamespace(translator="none"), None)),
            run(selfcheck.check_translator(auto, None))]                       # 引擎还没建
    for engine, model in (("hymt2", "hy-mt2-1.8b"), ("hymt2-7b", "hy-mt2-7B"),
                          ("gemma", "translategemma")):
        mp.setattr(selfcheck, "_ollama_tags", lambda: None)                  # Ollama 没在跑
        for installed in (True, False):
            mp.setattr(localmodel, "is_installed", lambda installed=installed: installed)
            rows.append(run(selfcheck.check_translator(auto, Engine(engine, model=model))))
        mp.setattr(selfcheck, "_ollama_tags", lambda: ["other-model"])
        for listed in (False, True):                                        # 模型不在 / 在
            mp.setattr(tr, "model_listed", lambda m, names, listed=listed: listed)
            rows.append(run(selfcheck.check_translator(auto, Engine(engine, model=model))))
    rows.append(run(selfcheck.check_translator(auto, Engine("google"))))
    rows.append(run(selfcheck.check_translator(auto, Engine("custom-api"))))

    mp.setattr(tr, "api_key", lambda key: None)
    rows.append(run(selfcheck.check_translator(auto, Engine("claude", model="claude-x"))))
    mp.setattr(tr, "api_key", lambda key: "sk-test")
    rows.append(run(selfcheck.check_translator(auto, Engine("openai", model="gpt-x"))))

    async def unreachable():
        raise TimeoutError()

    rows.append(run(selfcheck.check_translator(
        auto, Engine("claude", model="claude-x", probe_key=unreachable))))
    for status in (401, 404, 500, 200):
        async def probe(status=status):
            return status

        rows.append(run(selfcheck.check_translator(auto, Engine(
            "claude", model="claude-x", probe_key=probe, PROBE_CHECKS_MODEL=True))))
    return rows


def _deepl_engine(usage=200, gid="g1", tsv="a\tb", fails=False):
    async def api(method, path):
        return usage, {}

    async def ensure(source, target):
        if fails:
            raise OSError("connection reset")
        return gid

    return Engine("deepl", _api=api, _ensure_glossary=ensure, _GLOSSARY_TARGET={"zh-CN": "zh"},
                  glossary_tsv=lambda entries: tsv)


def _deepl(mp, tmp_path):
    from app import glossary
    mp.setattr(glossary, "load", lambda *a, **k: SimpleNamespace(entries=[]))
    args = SimpleNamespace(translator="deepl", target="zh-CN", source="es")
    return [run(selfcheck._check_deepl(args, Engine("deepl"))),     # 不建术语表的引擎
            run(selfcheck._check_deepl(args, _deepl_engine(usage=403))),
            run(selfcheck._check_deepl(SimpleNamespace(target="en", source="es"), _deepl_engine())),
            run(selfcheck._check_deepl(args, _deepl_engine(fails=True))),
            run(selfcheck._check_deepl(args, _deepl_engine(gid=None))),
            run(selfcheck._check_deepl(args, _deepl_engine(tsv="a\tb"))),
            run(selfcheck._check_deepl(args, _deepl_engine(tsv="a\tb\nc\td")))]


def _watchlist(mp, tmp_path):
    rows = [run(selfcheck.check_watchlist(None)),
            run(selfcheck.check_watchlist(Detector(enabled=False, read_error="PermissionError"))),
            run(selfcheck.check_watchlist(Detector(enabled=False)))]
    for effective in (0, 1, 5):
        for skipped in ((), (3,), (3, 5), tuple(range(1, 13))):
            for warnings in ((), [WARNING] * 6, [WARNING] * 7):
                rows.append(run(selfcheck.check_watchlist(Detector(
                    count=6, effective=effective, decode_error="bad byte", skipped=skipped,
                    warnings=warnings))))
    rows.append(run(selfcheck.check_watchlist(Detector(count=6, effective=5, warnings=[WARNING]))))
    rows += [run(selfcheck.check_watchlist(Detector(count=n))) for n in (1, 2)]
    return rows


def _glossary(mp, tmp_path):
    return [run(selfcheck.check_glossary(None)),
            run(selfcheck.check_glossary(SimpleNamespace(enabled=False, entries=[]))),
            run(selfcheck.check_glossary(SimpleNamespace(enabled=True, entries=["a"]))),
            run(selfcheck.check_glossary(SimpleNamespace(enabled=True, entries=["a", "b"])))]


def _audit(mp, tmp_path):
    from app import audit
    mp.setattr(audit, "LOG_DIR", tmp_path / "logs")
    rows = [run(selfcheck.check_audit())]
    mp.setattr(audit, "LOG_DIR", Unwritable())
    rows.append(run(selfcheck.check_audit()))
    for windows in (False, True):                   # 两个平台的修法各一句
        rows.append(selfcheck._check(selfcheck.NAMES["audit"], selfcheck.FAIL, "-",
                                     selfcheck._audit_fix(windows)))
    return rows


def _resolver(mp, tmp_path):
    from app import resolver
    mp.setattr(selfcheck, "_importable", lambda name: False)
    rows = [run(selfcheck.check_resolver())]
    mp.setattr(selfcheck, "_importable", lambda name: True)
    for browsers in (["Chrome", "Safari"], []):
        mp.setattr(resolver, "_installed_browsers", lambda browsers=browsers: browsers)
        rows.append(run(selfcheck.check_resolver()))
    return rows


def _browser_login(mp, tmp_path):
    from app import browser_login as bl
    mp.setattr(bl, "LOGIN_STEPS", "Log in to TikTok in Chrome, then try again.")
    mp.setattr(bl, "observed_text", lambda observed: OBSERVED)
    mp.setattr(bl, "steps_text", lambda observed: "Log in to TikTok in Chrome.")
    rows = [selfcheck._browser_login_row({}),
            selfcheck._browser_login_row({"Chrome": bl.NOT_READ}),
            selfcheck._browser_login_row({"Chrome": bl.OK, "Safari": bl.BLOCKED}),
            selfcheck._browser_login_row({"Chrome": bl.BLOCKED})]
    with mp.context() as other_os:
        coro = selfcheck.check_browser_login()
        other_os.setattr(sys, "platform", "linux")
        rows.append(_now(coro))
    return rows


def _comments_on(mp, python, args):
    """按 python 这个版本跑一次观众弹幕那一行。CI 里有 3.9 的跑器，真版本号会让这一行永远停在
    「需要 3.10」那一句；版本号只在这一步里改，而且不开事件循环（_to_thread 换成直接调用）。"""
    with mp.context() as other_python:
        coro = selfcheck.check_comments(args)
        other_python.setattr(sys, "version_info", python)
        return _now(coro)


def _comments(mp, tmp_path):
    from app import updater

    async def direct(fn, *args):
        return fn(*args)

    mp.setattr(selfcheck, "_to_thread", direct)
    args = SimpleNamespace(comments=True)
    rows = [_comments_on(mp, (3, 13, 0), SimpleNamespace(comments=False)),
            _comments_on(mp, (3, 9, 18), args)]
    mp.setattr(updater, "tiktoklive_version", lambda: None)
    rows.append(_comments_on(mp, (3, 13, 0), args))
    mp.setattr(updater, "tiktoklive_version", lambda: "7.0.0")
    mp.setattr(updater, "tiktoklive_outdated", lambda version: True)
    rows.append(_comments_on(mp, (3, 13, 0), args))
    mp.setattr(updater, "tiktoklive_outdated", lambda version: False)
    rows.append(_comments_on(mp, (3, 13, 0), args))
    for os_name in ("posix", "nt"):                 # 手动安装命令：终端 / PowerShell 各一句
        with mp.context() as other_os:              # 只包住这个纯字符串函数，别的都不在改过的 os.name 下跑
            other_os.setattr(selfcheck.os, "name", os_name)
            fix = selfcheck._pip_command("TikTokLive", upgrade=os_name == "nt")
        rows.append(selfcheck._check(selfcheck.NAMES["comments"], selfcheck.WARN, "-", fix))
    return rows


def _disk(mp, tmp_path):
    def usage(free_gb):
        def fake(path):
            if free_gb is None:
                raise OSError("statvfs failed")
            return SimpleNamespace(total=0, used=0, free=free_gb * 1024 ** 3)
        return fake

    rows = []
    for free in (None, 2.5, 4.2, 120):
        mp.setattr(selfcheck.shutil, "disk_usage", usage(free))
        rows.append(run(selfcheck.check_disk()))
    return rows


def _crashed(mp, tmp_path):
    """每一项探测都崩了：run_all 用 NAMES 里的名字各记一行 FAIL（顺带把 11 个名字都走一遍）。"""
    async def boom(*args, **kwargs):
        raise RuntimeError("probe exploded")

    for name in ("check_ffmpeg", "check_denoise", "check_asr", "check_translator",
                 "check_watchlist", "check_glossary", "check_audit", "check_resolver",
                 "check_browser_login", "check_comments", "check_disk"):
        mp.setattr(selfcheck, name, boom)
    return run(selfcheck.run_all(SimpleNamespace()))


SCENARIOS = {"ffmpeg": _ffmpeg, "denoise": _denoise, "asr": _asr, "translator": _translator,
             "deepl": _deepl, "watchlist": _watchlist, "glossary": _glossary, "audit": _audit,
             "resolver": _resolver, "browser_login": _browser_login, "comments": _comments,
             "disk": _disk, "crashed": _crashed}


def _rows(name, monkeypatch, tmp_path):
    """跑一个场景，交出它产出的自检行。替身只在场景里生效，出来就还原。"""
    tmp = tmp_path / name
    tmp.mkdir(parents=True, exist_ok=True)
    with monkeypatch.context() as mp:
        mp.setattr(sys, "executable", PY)
        return SCENARIOS[name](mp, tmp)


def _english(row):
    return i18n.render(row, i18n.EN)


# ---- G7：英文界面上没有中文 --------------------------------------------------------------------

@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_every_selfcheck_row_reads_as_english(scenario, monkeypatch, tmp_path):
    monkeypatch.setattr(i18n, "NET", "strict")      # 英文模板写坏（占位符对不上）当场抛，不退回中文
    rows = _rows(scenario, monkeypatch, tmp_path)
    assert rows
    for row in rows:
        en = _english(row)
        assert en["name"] in ENGLISH_NAMES, (scenario, en["name"])
        for key in ("detail", "fix"):
            assert not CJK.search(en[key]), (scenario, key, en[key])
            assert not UNFILLED.search(en[key]), (scenario, key, en[key])
            assert "  " not in en[key].strip(), (scenario, key, en[key])    # 拼接处没有多空格
        assert set(en) == {"name", "level", "detail", "fix"} and en["level"] == row["level"]


def test_the_eleven_row_names_follow_the_glossary():
    """11 行的英文名与规范（docs/i18n-style.md §1.3、spec §4.2 ⑧）一字不差。"""
    assert {i18n.text(n, i18n.EN) for n in selfcheck.NAMES.values()} == ENGLISH_NAMES
    assert selfcheck.NAMES["asr"] == "语音识别"        # 中文照旧：pipeline 按它认出识别那一行


# ---- 拼出来的长句：整句钉住 --------------------------------------------------------------------

def _one(scenario, monkeypatch, tmp_path, pick):
    return next(r for r in _rows(scenario, monkeypatch, tmp_path) if pick(r))


def _pip(monkeypatch, spec, upgrade):
    """这台机器上 _pip_command 给的那句（Windows 跑器上是 PowerShell 那句）。"""
    with monkeypatch.context() as mp:
        mp.setattr(sys, "executable", PY)
        return selfcheck._pip_command(spec, upgrade=upgrade)


def test_pip_command_reads_as_the_end_of_a_sentence(monkeypatch, tmp_path):
    fixes = [i18n.text(r["fix"], i18n.EN) for r in _rows("comments", monkeypatch, tmp_path)]
    assert 'run this in Terminal: "/opt/py/bin/python3" -m pip install "TikTokLive"' in fixes
    assert ('run this in PowerShell: & "/opt/py/bin/python3" -m pip install -U "TikTokLive"'
            in fixes)


def test_manual_install_command_follows_the_note_in_english_order(monkeypatch, tmp_path):
    row = _one("asr", monkeypatch, tmp_path, lambda r: NOTE in r["fix"])
    cmd = _pip(monkeypatch, "mlx-whisper", upgrade=False)
    assert i18n.text(row["fix"], i18n.EN) == (
        NOTE + ". When you’re not monitoring, you can also " + i18n.text(cmd, i18n.EN)
        + ". Then reopen the app.")
    assert row["fix"] == NOTE + "。也可以停播后" + cmd + "，装好后重开程序"
    assert i18n.text(row["detail"], i18n.EN) == (
        "This Mac supports GPU acceleration, but recognition is running on the CPU: ct2 + "
        "large-v3-turbo (model downloads when you first click Start). That’s more than twice as "
        "slow, and long sessions tend to build a backlog.")


def test_comments_install_and_update_commands(monkeypatch, tmp_path):
    from app.updater import TIKTOKLIVE_SPEC
    rows = _rows("comments", monkeypatch, tmp_path)
    fixes = [i18n.text(r["fix"], i18n.EN) for r in rows]
    assert ("The app tries to install it at launch and when you click Start, at most once an "
            "hour. You can also quit the app and "
            + i18n.text(_pip(monkeypatch, TIKTOKLIVE_SPEC, upgrade=False), i18n.EN)) in fixes
    assert ("The app tries to update it at each launch, at most once an hour. You can also quit "
            "the app and " + i18n.text(_pip(monkeypatch, TIKTOKLIVE_SPEC, upgrade=True), i18n.EN)
            ) in fixes
    assert "The comments component needs Python 3.10 or later (this is 3.9). Comments aren’t " \
           "available." in [i18n.text(r["detail"], i18n.EN) for r in rows]
    outdated = next(r for r in rows if "7.0.0" in r["detail"])
    assert i18n.text(outdated["detail"], i18n.EN) == (
        "The comments component, TikTokLive 7.0.0, is older than 7.0.1. It can’t connect when "
        "the comment service uses its backup route.")


def test_banned_term_notes_join_into_sentences(monkeypatch):
    detector = Detector(count=12, effective=10, decode_error="bad byte", skipped=(3, 5),
                        warnings=[WARNING] * 6)
    row = run(selfcheck.check_watchlist(detector))
    assert i18n.text(row["detail"], i18n.EN) == (
        "10 entries active. banned_terms.txt isn’t UTF-8 encoded. Lines 3, 5 couldn’t be read and "
        "were skipped. The other 10 entries are active. Save it as UTF-8. " + ". ".join(
            [WARNING] * 5) + ". 1 more similar issue")
    assert row["detail"] == ("10 条已生效；banned_terms.txt 不是 UTF-8 编码，第 3、5 行读不出已跳过，"
                             "其余 10 条照常生效——请用 UTF-8 另存；" + "；".join([WARNING] * 5)
                             + "；另有 1 条同类问题")
    assert i18n.text(row["fix"], i18n.EN) == (
        "Fix banned_terms.txt as described above, then click Stop and Start.")


@pytest.mark.parametrize("detector, expected", [
    (Detector(count=1), "1 entry active"),
    (Detector(count=2), "2 entries active"),
    (Detector(count=3, effective=1, decode_error="x", skipped=(4,)),
     "1 entry active. banned_terms.txt isn’t UTF-8 encoded. Line 4 couldn’t be read and was "
     "skipped. The other entry is active. Save it as UTF-8"),
    (Detector(count=13, effective=0, decode_error="x", skipped=range(1, 13)),
     "None of the entries in the list can match. The app won’t raise any banned-term alerts. "
     "banned_terms.txt isn’t UTF-8 encoded. Lines 1, 2, 3, 4, 5, 6, 7, 8, 9, 10 and more (12 "
     "lines in all) couldn’t be read and were skipped. The other 0 entries are active. Save it "
     "as UTF-8"),
    (Detector(count=3, effective=2, decode_error="x"),
     "2 entries active. banned_terms.txt isn’t UTF-8 encoded. Only comment lines couldn’t be "
     "read. The other 2 entries are active. Save it as UTF-8"),
])
def test_banned_term_counts_use_english_plurals(detector, expected):
    row = run(selfcheck.check_watchlist(detector))
    assert i18n.text(row["detail"], i18n.EN) == expected


@pytest.mark.parametrize("tsv, expected", [
    ("a\tb", "DeepL + native glossary (1 entry, es→zh-CN)"),
    ("a\tb\nc\td", "DeepL + native glossary (2 entries, es→zh-CN)"),
])
def test_deepl_glossary_size_uses_english_plurals(tsv, expected, monkeypatch):
    from app import glossary
    monkeypatch.setattr(glossary, "load", lambda *a, **k: SimpleNamespace(entries=[]))
    monkeypatch.setattr(i18n, "NET", "strict")
    args = SimpleNamespace(translator="deepl", target="zh-CN", source="es")
    row = run(selfcheck._check_deepl(args, _deepl_engine(tsv=tsv)))
    assert i18n.text(row["detail"], i18n.EN) == expected


def test_browser_login_scope_reads_after_any_observation(monkeypatch, tmp_path):
    rows = _rows("browser_login", monkeypatch, tmp_path)
    scope = (" (Only live streams where TikTok requires a login to provide the stream URL need "
             "this. Other live streams aren’t affected.)")
    details = [i18n.text(r["detail"], i18n.EN) for r in rows]
    assert OBSERVED + scope in details
    assert ("Didn’t find Chrome, Safari, or another browser with a login the app can use"
            + scope) in details


# ---- G8：审计和终端与界面语言无关 ---------------------------------------------------------------

def test_selfcheck_audit_and_terminal_do_not_depend_on_the_ui_language(monkeypatch, tmp_path,
                                                                        capsys):
    rows = [r for name in sorted(SCENARIOS) for r in _rows(name, monkeypatch, tmp_path)]

    async def fake_run_all(*args, **kwargs):
        return rows

    monkeypatch.setattr(selfcheck, "run_all", fake_run_all)

    async def scenario(p):
        p.args, p.detector, p.glossary, p.translator = SimpleNamespace(), None, None, None
        await p.run_selfcheck()

    results = run_in_both_languages(scenario, tmp_path / "g8", capsys)
    assert_language_independent(results)
    zh_audit, zh_out, zh_got = results[i18n.ZH]
    # 终端和审计确实写了这些行（中文），不是两边都空
    assert "[自检] ❌ 音频组件 ffmpeg：找不到 ffmpeg，无法拉取直播音频" in zh_out
    assert '"name": "语音识别"' in "\n".join(zh_audit)
    # 本机页面：中文那次是中文，英文那次是英文
    _, _, en_got = results[i18n.EN]
    zh_rows = next(m for m in zh_got if m["type"] == "selfcheck")["checks"]
    en_rows = next(m for m in en_got if m["type"] == "selfcheck")["checks"]
    assert len(en_rows) == len(zh_rows) == len(rows)
    assert {c["name"] for c in en_rows} <= ENGLISH_NAMES
    assert not [c for c in en_rows for k in ("name", "detail", "fix") if CJK.search(c[k])]
    assert zh_rows[0]["name"] == rows[0]["name"] and CJK.search(zh_rows[0]["name"])
