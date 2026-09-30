"""app/i18n.py 的核心语义（spec §12.1 G1）。

整个双语方案押在一件事上：L() 的结果**就是那句中文**。相等、哈希、in、print、json.dumps、
写审计全按中文走，所以终端、审计、约 1000 处钉中文的测试都不用动；英文只在发往界面的
那一刻由 render() 换出来，写错了只会退回中文，绝不抛异常。这里把这些性质逐条钉住。

另一半是语言初始化：boot 只读、settle 才写。损坏的 settings.json 要留到最终进程里才备份，
否则「设置文件损坏，已备份为…」的提示和审计里的备份名会随 exec 静默消失（spec §3.3）。"""
import ast
import copy
import json
import pickle
import sys
import types
from pathlib import Path

import pytest

from app import i18n, settings
from app.i18n import EN, ZH, Bi, L, LN, bimap, of, render

REAL_SYSTEM_LANG = i18n.system_lang


@pytest.fixture(autouse=True)
def _fresh_i18n(monkeypatch):
    """模块级状态与三个环境变量不许在用例之间泄漏，也不许受跑测试那台机器的系统语言影响。"""
    monkeypatch.setattr(i18n, "_state", {"choice": "system", "lang": ZH, "system": None,
                                         "override": None})
    monkeypatch.setattr(i18n, "_bad_once", set())
    monkeypatch.setattr(i18n, "NET", None)
    monkeypatch.setattr(i18n, "I18N_ENABLED", False)
    monkeypatch.setattr(i18n, "system_lang", lambda: None)
    for name in (i18n.ENV_OVERRIDE, i18n.ENV_SYSTEM, i18n.ENV_INSTALL):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    """settle 走真的 load_settings/save_setting，文件落在 tmp 里。"""
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(settings, "_corrupt", {"backup": None, "announced": False})
    return tmp_path / "settings.json"


# ---- Bi：就是那句中文 ------------------------------------------------------------------

def test_bi_is_its_chinese_for_equality_hash_and_membership():
    b = L("语音识别", "Speech Recognition")
    assert b == "语音识别" and "语音识别" == b
    assert b == L("语音识别", "something else")          # 按中文比较：切换语言中途按文字做键不会失配
    assert b != "Speech Recognition"
    assert hash(b) == hash("语音识别")
    assert {"语音识别": 1}[b] == 1 and {b: 1}["语音识别"] == 1
    assert b in {"语音识别"} and b in ["语音识别"]
    assert "识别" in b and L("识别", "x") in "语音识别"
    assert "Speech" not in b


def test_str_print_and_repr_are_the_chinese(capsys):
    b = L("正在停止…", "Stopping…")
    assert type(str(b)) is str and str(b) == "正在停止…"
    assert repr(b) == repr("正在停止…")
    print("[信息] " + b)
    print(b)
    assert capsys.readouterr().out == "[信息] 正在停止…\n正在停止…\n"


@pytest.mark.parametrize("ensure_ascii", [True, False])
@pytest.mark.parametrize("indent", [None, 2])
def test_json_dumps_is_byte_identical_to_plain_chinese(ensure_ascii, indent):
    """WebSocket 和审计都走 json.dumps：带 indent 时走的是纯 Python 编码器，两条路都要钉。"""
    def msg(t):
        return {"type": "incident", "text": t, "items": [t, {"name": t}], t: 1}

    b = L("电脑休眠过", "The computer was asleep")
    kw = {"ensure_ascii": ensure_ascii, "indent": indent, "sort_keys": True}
    assert json.dumps(msg(b), **kw) == json.dumps(msg("电脑休眠过"), **kw)
    assert json.dumps(b, **kw) == json.dumps("电脑休眠过", **kw)


def test_format_fills_both_arms_with_positional_named_and_format_specs():
    b = L("积压 {:.0f} 秒，{:>4}|{!r}|{n:03d}", "{:.0f} sec behind, {:>4}|{!r}|{n:03d}").format(
        12.6, "ab", "x", n=7)
    assert isinstance(b, Bi)
    assert b == "积压 13 秒，  ab|'x'|007"
    assert b.en == "13 sec behind,   ab|'x'|007"
    # 英文可以重排语序；Bi 参数两臂各取各的
    moved = L("从 {0} 切到 {1}", "{1}, switched from {0}").format(L("甲", "A"), "b")
    assert (moved, moved.en) == ("从 甲 切到 b", "b, switched from A")
    named = L("自检：{name} 未生效——{fix}", "Startup Check: {name} isn’t working. {fix}").format(
        name=L("语音识别", "Speech Recognition"), fix="pip install x")
    assert named == "自检：语音识别 未生效——pip install x"
    assert named.en == "Startup Check: Speech Recognition isn’t working. pip install x"


def test_chinese_template_errors_still_raise_like_before():
    """中文一路与原来的 "…".format(...) 完全相同：原来会抛的照旧抛。"""
    with pytest.raises(KeyError):
        L("{missing}", "{missing}").format()
    with pytest.raises(IndexError):
        L("{} {}", "{} {}").format("only one")


def test_broken_english_template_falls_back_to_chinese_and_warns_once(capsys):
    bad = L("已删除 {n} 项", "Deleted {count} items")
    first = bad.format(n=3)
    assert first == "已删除 3 项" and first.en == "已删除 3 项"
    spec_bad = L("积压 {:.0f} 秒", "{:d} sec behind").format(1.5)   # 格式规格不合也一样
    assert spec_bad.en == "积压 2 秒"
    bad.format(n=4)
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("[i18n]")]
    assert len(lines) == 2                                           # 每个模板只打一次
    assert "已删除 {n} 项" in lines[0] and "KeyError" in lines[0]


def test_broken_english_template_raises_in_strict_mode(monkeypatch):
    monkeypatch.setattr(i18n, "NET", "strict")
    with pytest.raises(KeyError):
        L("已删除 {n} 项", "Deleted {count} items").format(n=3)


def test_concatenation_keeps_both_arms():
    b = L("识别落后", "Recognition is behind")
    assert (b + "（3 秒）").en == "Recognition is behind（3 秒）"
    left = "⚠️ " + b                                    # Bi.__radd__ 先于 str 拼接
    assert isinstance(left, Bi) and left == "⚠️ 识别落后" and left.en == "⚠️ Recognition is behind"
    both = L("甲", "A") + L("乙", "B")
    assert (both, both.en) == ("甲乙", "AB")
    acc = ""
    acc += b
    assert isinstance(acc, Bi) and acc.en == "Recognition is behind"
    with pytest.raises(TypeError):
        b + 1


def test_join_uses_each_arm_and_accepts_generators():
    names = L("、", ", ").join(x for x in [L("音频", "Audio"), "ffmpeg", L("翻译", "Translation")])
    assert (names, names.en) == ("音频、ffmpeg、翻译", "Audio, ffmpeg, Translation")
    steps = L("", "").join([L("第一步。", "Step one. "), L("第二步。", "Step two.")])
    assert (steps, steps.en) == ("第一步。第二步。", "Step one. Step two.")
    lines = L("\n", "\n").join([L("甲", "A"), L("乙", "B")])
    assert (lines, lines.en) == ("甲\n乙", "A\nB")


def test_lossy_operations_degrade_to_plain_chinese_without_error():
    """这些写法会丢英文（G2/G4/G9 负责拦），但只会露中文，绝不报错。"""
    b = L("  设置文件损坏  ", "  Settings file damaged  ")
    for lossy in (b[:4], b.strip(), "{}".format(b), f"{b}", str(b), "、".join([b])):
        assert type(lossy) is str
        assert "Settings" not in lossy


def test_missing_english_falls_back_to_chinese():
    assert L("待机", None).en == "待机"
    assert L(L("待机", "Ready"), L("就绪", "Ready")).en == "Ready"


def test_ln_picks_the_english_by_count():
    def deleted(n):
        return LN(n, "已删除 {n} 项，释放 {size}", "Deleted 1 item and freed {size}.",
                  "Deleted {n} items and freed {size}.").format(n=n, size="2 GB")

    assert deleted(0).en == "Deleted 0 items and freed 2 GB."
    assert deleted(1).en == "Deleted 1 item and freed 2 GB."
    assert deleted(2).en == "Deleted 2 items and freed 2 GB."
    assert deleted(1) == "已删除 1 项，释放 2 GB"


def test_of_keeps_bilingual_exception_text_and_matches_str_otherwise():
    bi = L("没有找到这个直播间", "Couldn’t find this live stream.")
    exc = RuntimeError(bi)
    assert of(exc) is bi
    assert str(of(exc)) == str(exc)                      # 中文模式下与原来的 str(exc) 相同
    for plain in (ValueError("x"), OSError(2, "No such file"), Exception(), RuntimeError(bi, 1)):
        assert of(plain) == str(plain) and type(of(plain)) is str


def test_bimap_applies_the_same_function_to_both_arms():
    cut = bimap(lambda s: s[:2] + "…", L("中文句子", "English"))
    assert isinstance(cut, Bi) and (cut, cut.en) == ("中文…", "En…")
    assert bimap(lambda s: s.upper(), "plain") == "PLAIN"


def _pickler(protocol):
    return lambda x: pickle.loads(pickle.dumps(x, protocol=protocol))


_PROTOCOLS = range(pickle.HIGHEST_PROTOCOL + 1)


@pytest.mark.parametrize("clone", [copy.copy, copy.deepcopy] + [_pickler(p) for p in _PROTOCOLS],
                         ids=["copy", "deepcopy"] + ["pickle{}".format(p) for p in _PROTOCOLS])
def test_copy_deepcopy_and_pickle_keep_the_english(clone):
    msg = {"text": L("电脑休眠过", "The computer was asleep"), "items": [L("甲", "A")]}
    out = clone(msg)
    assert isinstance(out["text"], Bi) and out["text"].en == "The computer was asleep"
    assert out["items"][0].en == "A" and out == msg


# ---- render / text：出口 -----------------------------------------------------------------

def test_render_in_chinese_returns_the_very_same_object():
    msg = {"type": "status", "detail": L("待机", "Ready"), "list": [L("甲", "A")]}
    assert render(msg) is msg
    assert render(msg, ZH) is msg
    assert render(msg, "fr") is msg                      # 认不出的语言当中文
    b = L("待机", "Ready")
    assert render(b) is b


def test_render_in_english_swaps_every_nested_bi_and_leaves_the_rest():
    msg = {"type": "selfcheck", "n": 3, "ok": None,
           "checks": [{"name": L("语音识别", "Speech Recognition"), "detail": "plain 中文"}],
           "pair": (L("甲", "A"), 1)}
    out = render(msg, EN)
    assert out == {"type": "selfcheck", "n": 3, "ok": None,
                   "checks": [{"name": "Speech Recognition", "detail": "plain 中文"}],
                   "pair": ["A", 1]}
    assert type(out["checks"][0]["name"]) is str
    assert msg["checks"][0]["name"] == "语音识别"          # 原消息（缓存）不被改动
    with i18n.use(EN):
        assert render(L("待机", "Ready")) == "Ready"      # 不传 lang 就用当前语言


def test_text_always_hands_out_a_plain_str():
    b = L("关闭", "Close")
    for lang, want in ((ZH, "关闭"), (EN, "Close"), (None, "关闭")):
        value = i18n.text(b, lang)
        assert type(value) is str and value == want
    assert type(i18n.text("普通", EN)) is str
    assert i18n.text(None, EN) is None and i18n.text(3, EN) == 3


def test_inject_lang_only_touches_the_first_html_tag():
    page = '<!doctype html>\n<html lang="zh-CN">\n<body><p>\'<html lang="zh-CN">\'</p></body></html>'
    assert i18n.inject_lang(page, ZH) == page
    en = i18n.inject_lang(page, EN)
    assert en.count('<html lang="en">') == 1 and en.count('<html lang="zh-CN">') == 1
    assert '<html lang="en" data-i18n="on">' in i18n.inject_lang(page, EN, marker=True)
    assert '<html lang="zh-CN" data-i18n="on">' in i18n.inject_lang(page, ZH, marker=True)
    assert i18n.inject_lang("<html>", EN) == "<html>"


def test_cjk_pattern_covers_characters_and_cjk_punctuation_only():
    for ch in "中，（）「」、ア々Ａ１：":
        assert i18n.CJK.search(ch), ch
    for ch in "A’é—…⚠️·→":
        assert not i18n.CJK.search(ch), ch


def test_enabled_follows_the_gate_and_one_off_override(monkeypatch):
    assert not i18n.enabled()
    with i18n.use(EN):
        assert i18n.enabled() and i18n.current() == EN
    assert not i18n.enabled() and i18n.current() == ZH
    monkeypatch.setattr(i18n, "I18N_ENABLED", True)
    assert i18n.enabled()
    with pytest.raises(ValueError):
        with i18n.use("fr"):
            pass


# ---- 系统语言与 Accept-Language -----------------------------------------------------------

def _fake_defaults(outputs, calls):
    def run(argv, **kwargs):
        calls.append((argv, kwargs.get("timeout")))
        code, out = outputs[len(calls) - 1]
        if isinstance(code, BaseException):
            raise code
        return types.SimpleNamespace(returncode=code, stdout=out, stderr="")
    return run


@pytest.mark.parametrize("outputs, want, ncalls", [
    ([(1, ""), (0, '(\n    "en-US",\n    "zh-Hans-US"\n)\n')], EN, 2),     # 本机实测的形状
    ([(0, '(\n    "zh-Hans",\n    en\n)\n')], ZH, 1),                       # App 单独选过就不看全局
    ([(1, ""), (0, "(\n    en,\n    \"zh-Hant-TW\"\n)\n")], EN, 2),        # 不带引号的标签
    ([(1, ""), (0, '(\n    "zh-Hant-TW"\n)\n')], ZH, 2),
    ([(0, "()\n"), (0, '(\n    "zh-Hans-CN"\n)\n')], ZH, 2),               # 空列表当没读到
    ([(1, ""), (1, "")], None, 2),
    ([(FileNotFoundError(), ""), (FileNotFoundError(), "")], None, 2),
])
def test_system_lang_on_macos_reads_app_then_global_apple_languages(monkeypatch, outputs, want, ncalls):
    calls = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(i18n.subprocess, "run", _fake_defaults(outputs, calls))
    assert REAL_SYSTEM_LANG() == want
    assert len(calls) == ncalls
    assert calls[0] == (["defaults", "read", i18n.BUNDLE_ID, "AppleLanguages"], 2)
    if ncalls == 2:
        assert calls[1] == (["defaults", "read", "-g", "AppleLanguages"], 2)


@pytest.mark.parametrize("code, want", [(0x0804, ZH), (0x0404, ZH), (0x0C04, ZH),
                                        (0x0409, EN), (0x0411, EN), (RuntimeError("x"), None)])
def test_system_lang_on_windows_uses_the_display_language(monkeypatch, code, want):
    import ctypes

    def ui_language():
        if isinstance(code, BaseException):
            raise code
        return code

    fake = types.SimpleNamespace(kernel32=types.SimpleNamespace(GetUserDefaultUILanguage=ui_language))
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", fake, raising=False)
    assert REAL_SYSTEM_LANG() == want


@pytest.mark.parametrize("env, want", [
    ({"LANGUAGE": "zh_CN:en", "LANG": "en_US.UTF-8"}, ZH),
    ({"LANG": "en_US.UTF-8"}, EN),
    ({"LC_ALL": "C", "LANG": "zh_TW.UTF-8"}, ZH),
    ({"LC_MESSAGES": "fr_FR.UTF-8"}, EN),
    ({"LANG": "C.UTF-8"}, None),
    ({}, None),
])
def test_system_lang_elsewhere_reads_locale_variables(monkeypatch, env, want):
    monkeypatch.setattr(sys, "platform", "linux")
    for name in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert REAL_SYSTEM_LANG() == want


@pytest.mark.parametrize("header, want", [
    ("", ZH), (None, ZH), ("*", ZH), ("en;q=0", ZH), ("garbage;q=abc", ZH),
    ("en-US,en;q=0.9", EN), ("zh-CN,zh;q=0.9,en;q=0.8", ZH), ("ZH-hant", ZH),
    ("en;q=0.5, zh-TW;q=0.9", ZH), ("zh;q=0.4,fr-FR;q=0.8", EN), ("es-MX", EN),
    ("en, zh", EN),                                                   # 同分取先出现的
])
def test_accept_lang_takes_the_highest_q(header, want):
    assert i18n.accept_lang(header) == want


# ---- boot / settle（spec §3.3） ----------------------------------------------------------

def _snapshot(root):
    return {str(p.relative_to(root)): (p.is_dir(), None if p.is_dir() else p.read_bytes(),
                                       p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*"))}


def _boot(root, env, argv=()):
    return i18n.boot(root, argv=list(argv), environ=env)


def _finish_install(root):
    """ensure_env 装完的样子：建出 .venv，pip 返回 0 之后 bootstrap 写下清单指纹。"""
    (root / ".venv").mkdir(exist_ok=True)
    (root / ".venv" / i18n.INSTALL_STAMP).write_text("abc\n", encoding="utf-8")


def test_the_install_stamp_is_the_one_bootstrap_writes():
    """i18n 只依赖标准库，不能 import bootstrap，文件名是抄过来的：两边改名不同步，老装机
    就全被判成新装（界面跟系统语言、字幕改成英文），所以钉住。"""
    from app import bootstrap
    assert i18n.INSTALL_STAMP == bootstrap.REQ_STAMP


def test_boot_records_install_state_only_once(tmp_path):
    env = {}
    _boot(tmp_path, env)
    assert env[i18n.ENV_INSTALL] == "fresh"
    _finish_install(tmp_path)                         # ensure_env 建 .venv、装完写记号，再 execv
    _boot(tmp_path, env)
    assert env[i18n.ENV_INSTALL] == "fresh"           # exec 之后的进程不重判


@pytest.mark.parametrize("make", [
    _finish_install,
    lambda root: (root / "settings.json").write_text("{}", encoding="utf-8"),
    lambda root: (root / "settings.json").write_bytes(b"{ broken"),     # 内容好坏不论
])
def test_boot_treats_settings_or_a_finished_install_as_existing(tmp_path, make):
    make(tmp_path)
    env = {}
    _boot(tmp_path, env)
    assert env[i18n.ENV_INSTALL] == "existing"


def _setup_script_venv(root):
    """setup.sh / setup.ps1 第 3 步：`python -m venv .venv` 再 pip install -r，不写记号。"""
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "pyvenv.cfg").write_text("version = 3.13.5\n", encoding="utf-8")


def _interrupted_first_launch(root):
    """第一次启动 venv.create 之后 pip 失败（断网）或中途退出：.venv 在，记号没写，
    record_requirements_failure 可能留下一份失败记录。"""
    _setup_script_venv(root)
    (root / ".venv" / ".requirements-attempt.json").write_text("{}", encoding="utf-8")


@pytest.mark.parametrize("make", [
    lambda root: (root / ".venv").mkdir(),
    _setup_script_venv,
    _interrupted_first_launch,
])
def test_boot_treats_a_venv_that_never_finished_installing_as_fresh(tmp_path, make):
    """复审（english-e2e、live-path-audit）：光有 .venv 不算老装机。setup.sh 先建 .venv 再用它
    跑 main.py --doctor，那一次就是第一次迁移；第一次启动装到一半失败，重开时 .venv 也在。
    按老装机判，英文系统上的新用户会被永远固定成中文界面、中文字幕。"""
    make(tmp_path)
    env = {}
    _boot(tmp_path, env)
    assert env[i18n.ENV_INSTALL] == "fresh"


def test_the_setup_script_doctor_run_migrates_like_a_fresh_install(tmp_path, settings_file,
                                                                   monkeypatch):
    """setup.sh 的 `.venv/bin/python main.py --doctor`：boot 在 ensure_env 之前（记号那时还没写），
    settle 在同一个进程里。英文系统上写出跟随系统 + 英文字幕，和双击 .app 的新装一样。"""
    monkeypatch.setattr(i18n, "I18N_ENABLED", True)
    monkeypatch.setattr(i18n, "system_lang", lambda: EN)
    _setup_script_venv(tmp_path)
    env = {}
    _boot(tmp_path, env, ("--doctor",))
    assert env[i18n.ENV_INSTALL] == "fresh"
    _finish_install(tmp_path)                         # ensure_env 发现没有指纹，补跑 pip 后写下
    assert i18n.settle(tmp_path, environ=env) == EN
    assert settings.load_settings() == {"ui_lang": "system", "target_lang": "en"}


def test_boot_detects_system_language_once_per_launch(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(i18n, "system_lang", lambda: calls.append(1) or EN)
    env = {}
    _boot(tmp_path, env)
    _boot(tmp_path, env)
    assert calls == [1] and env[i18n.ENV_SYSTEM] == EN and i18n._state["system"] == EN

    def boom():
        raise AssertionError("缓存命中时不该再检测")

    monkeypatch.setattr(i18n, "system_lang", boom)
    _boot(tmp_path, {i18n.ENV_SYSTEM: "zh"})
    assert i18n._state["system"] == ZH
    monkeypatch.setattr(i18n, "system_lang", lambda: None)
    env = {}
    _boot(tmp_path, env)
    assert env[i18n.ENV_SYSTEM] == "none" and i18n._state["system"] is None


@pytest.mark.parametrize("gate, saved, install, system, argv, env_override, want, locked", [
    (False, "en", "existing", EN, (), None, ZH, False),                  # 闸关着恒为中文
    (False, None, "fresh", EN, (), None, ZH, False),
    (False, "zh", "existing", ZH, ("--ui-lang", "en"), None, EN, True),  # 一次性覆盖不看闸
    (False, None, "existing", None, (), "EN", EN, True),
    (True, "en", "existing", ZH, ("--ui-lang=zh",), None, ZH, True),     # 覆盖优先于设置
    (True, "en", "existing", EN, ("--ui-lang", "zh"), "en", ZH, True),   # 命令行优先于环境变量
    (True, "zh", "existing", EN, ("--ui-lang", "fr"), "en", EN, True),   # 命令行值不合法就看环境变量
    (True, "zh", "existing", EN, ("--ui-lang", "fr"), None, ZH, False),
    (True, "en", "existing", ZH, (), None, EN, False),
    (True, "zh", "fresh", EN, (), None, ZH, False),
    (True, "system", "existing", EN, (), None, EN, False),
    (True, "system", "existing", None, (), None, ZH, False),             # 检测失败按中文
    (True, None, "fresh", EN, (), None, EN, False),                      # 键缺失：新装跟随系统
    (True, None, "existing", EN, (), None, ZH, False),                   # 键缺失：老装机固定中文
    (True, "nonsense", "fresh", EN, (), None, EN, False),
])
def test_boot_resolution_order(tmp_path, monkeypatch, gate, saved, install, system, argv,
                               env_override, want, locked):
    monkeypatch.setattr(i18n, "I18N_ENABLED", gate)
    if saved is not None:
        (tmp_path / "settings.json").write_text(json.dumps({"ui_lang": saved}), encoding="utf-8")
    env = {i18n.ENV_INSTALL: install, i18n.ENV_SYSTEM: system or "none"}
    if env_override:
        env[i18n.ENV_OVERRIDE] = env_override
    assert _boot(tmp_path, env, ["main.py"] + list(argv)) == want
    assert i18n.current() == want
    assert i18n.config_info()["ui_lang_locked"] is locked
    assert i18n.enabled() is (gate or locked)


@pytest.mark.parametrize("content", [None, b"", b"{ broken", b'{"ui_lang": "en"}', b"[1, 2]",
                                     '﻿{"ui_lang": "zh"}'.encode("utf-8")])
def test_boot_never_writes_or_touches_settings(tmp_path, monkeypatch, content):
    """只读：不调 load_settings（它会把损坏的文件改名），不写回，对目录做前后快照。
    boot 自己吞掉一切异常，所以替身只记账不抛——抛了会被吞掉，测不出来。"""
    touched = []
    monkeypatch.setattr(settings, "load_settings", lambda *a, **k: touched.append("load") or {})
    monkeypatch.setattr(settings, "save_setting", lambda *a, **k: touched.append("save"))
    monkeypatch.setattr(i18n, "I18N_ENABLED", True)
    if content is not None:
        (tmp_path / "settings.json").write_bytes(content)
    before = _snapshot(tmp_path)
    for env in ({}, {i18n.ENV_INSTALL: "fresh", i18n.ENV_SYSTEM: "en"}):
        _boot(tmp_path, env, ("--ui-lang", "en"))
        _boot(tmp_path, env)
    assert _snapshot(tmp_path) == before
    assert touched == []


def test_boot_never_raises(tmp_path, monkeypatch):
    def boom():
        raise RuntimeError("detector crashed")

    monkeypatch.setattr(i18n, "system_lang", boom)
    (tmp_path / "settings.json").mkdir()                  # 读字节会抛 IsADirectoryError
    assert _boot(tmp_path, {}) == ZH
    assert _boot(tmp_path / "missing", {}) == ZH


def test_corrupt_settings_survive_boot_and_are_backed_up_by_settle(tmp_path, settings_file):
    """损坏的设置文件经 boot（exec 之前的进程）后原地不动，最终进程的 settle 才备份它。"""
    settings_file.write_bytes(b"{ not valid json")
    env = {}
    _boot(tmp_path, env)
    assert settings_file.read_bytes() == b"{ not valid json"
    assert settings.corrupt_backup_name() is None
    i18n.settle(tmp_path, environ=env)
    assert settings.corrupt_backup_name() is not None
    assert (tmp_path / settings.corrupt_backup_name()).read_bytes() == b"{ not valid json"


@pytest.mark.parametrize("content", [None, b'{"target_lang": "ja"}'])
def test_settle_writes_nothing_while_the_gate_is_closed(tmp_path, settings_file, content):
    if content is not None:
        settings_file.write_bytes(content)
    before = _snapshot(tmp_path)
    for install in ("fresh", "existing"):
        assert i18n.settle(tmp_path, environ={i18n.ENV_INSTALL: install, i18n.ENV_SYSTEM: "en"}) == ZH
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("env, want_saved, want_lang", [
    ({i18n.ENV_INSTALL: "fresh", i18n.ENV_SYSTEM: "en"}, "system", EN),
    ({i18n.ENV_INSTALL: "fresh", i18n.ENV_SYSTEM: "none"}, "system", ZH),
    ({i18n.ENV_INSTALL: "existing", i18n.ENV_SYSTEM: "en"}, "zh", ZH),   # 用户决定 1：老装机固定中文
    ({i18n.ENV_SYSTEM: "en"}, "zh", ZH),                                  # 没经过 boot：按老装机
])
def test_settle_migrates_once_when_the_gate_is_open(tmp_path, settings_file, monkeypatch,
                                                    env, want_saved, want_lang):
    monkeypatch.setattr(i18n, "I18N_ENABLED", True)
    settings_file.write_text(json.dumps({"target_lang": "ja"}), encoding="utf-8")
    assert i18n.settle(tmp_path, environ=env) == want_lang
    assert settings.load_settings() == {"target_lang": "ja", "ui_lang": want_saved}
    before = _snapshot(tmp_path)
    i18n.settle(tmp_path, environ=env)
    assert _snapshot(tmp_path) == before                  # 已有 ui_lang：不再写


def test_settle_keeps_an_existing_choice_and_the_boot_override(tmp_path, settings_file, monkeypatch):
    monkeypatch.setattr(i18n, "I18N_ENABLED", True)
    settings_file.write_text(json.dumps({"ui_lang": "en"}), encoding="utf-8")
    env = {i18n.ENV_INSTALL: "existing", i18n.ENV_SYSTEM: "zh"}
    before = _snapshot(tmp_path)
    assert i18n.settle(tmp_path, environ=env) == EN
    assert _snapshot(tmp_path) == before
    _boot(tmp_path, env, ("--ui-lang", "zh"))
    assert i18n.settle(tmp_path, environ=env) == ZH
    assert i18n.config_info()["ui_lang_locked"] is True


# ---- 提交 T：新装、界面是英文时，翻译目标语言默认 English（用户 09-28 决定 3） ---------------

FRESH_EN = {i18n.ENV_INSTALL: "fresh", i18n.ENV_SYSTEM: "en"}


@pytest.mark.parametrize("gate, env, saved, want", [
    (True, FRESH_EN, None, EN),                                          # 新装第一次启动
    (True, FRESH_EN, {}, EN),
    # 已有 ui_lang：不是迁移的那一次 settle，哪怕环境变量还是 fresh（复审：一键更新 execv 继承）
    (True, FRESH_EN, {"ui_lang": "system"}, None),
    (True, FRESH_EN, {"ui_lang": "en"}, None),
    (True, FRESH_EN, {"ui_lang": "zh"}, None),                           # 界面是中文
    (True, {i18n.ENV_INSTALL: "fresh", i18n.ENV_SYSTEM: "zh"}, None, None),
    (True, {i18n.ENV_INSTALL: "fresh", i18n.ENV_SYSTEM: "none"}, None, None),   # 检测失败按中文
    (True, {i18n.ENV_INSTALL: "existing", i18n.ENV_SYSTEM: "en"}, {"ui_lang": "en"}, None),  # 老用户不动
    (True, {i18n.ENV_INSTALL: "existing", i18n.ENV_SYSTEM: "en"}, None, None),
    (True, {i18n.ENV_SYSTEM: "en"}, {"ui_lang": "en"}, None),            # 没经过 boot：按老装机
    (False, FRESH_EN, None, None),                                       # 闸关着
    (False, FRESH_EN, {"ui_lang": "en"}, None),
])
def test_settle_defaults_captions_to_english_only_for_fresh_english_installs(
        tmp_path, settings_file, monkeypatch, gate, env, saved, want):
    monkeypatch.setattr(i18n, "I18N_ENABLED", gate)
    if saved is not None:
        settings_file.write_text(json.dumps(saved), encoding="utf-8")
    i18n.settle(tmp_path, environ=dict(env))
    assert settings.load_settings().get("target_lang") == want


@pytest.mark.parametrize("target", ["zh-CN", "ja", "", None])
def test_settle_never_replaces_a_target_lang_that_is_already_there(tmp_path, settings_file,
                                                                   monkeypatch, target):
    """设置里有过 target_lang（哪怕是空值）就是选过了：一个字节都不写。"""
    monkeypatch.setattr(i18n, "I18N_ENABLED", True)
    settings_file.write_text(json.dumps({"ui_lang": "system", "target_lang": target}),
                             encoding="utf-8")
    before = _snapshot(tmp_path)
    assert i18n.settle(tmp_path, environ=dict(FRESH_EN)) == EN
    assert _snapshot(tmp_path) == before


def test_english_caption_default_is_written_on_the_first_launch_only(tmp_path, settings_file,
                                                                     monkeypatch):
    """真实顺序：第一个进程 boot 判出 fresh，ensure_env 建 .venv、装完写记号再 execv，
    最终进程 settle 写。下一次启动 settings.json 已在，判成老装机，不再写。"""
    monkeypatch.setattr(i18n, "I18N_ENABLED", True)
    monkeypatch.setattr(i18n, "system_lang", lambda: EN)
    env = {}
    _boot(tmp_path, env)
    _finish_install(tmp_path)
    _boot(tmp_path, env)
    assert i18n.settle(tmp_path, environ=env) == EN
    assert settings.load_settings() == {"ui_lang": "system", "target_lang": "en"}
    before = _snapshot(tmp_path)
    env = {}
    _boot(tmp_path, env)
    assert env[i18n.ENV_INSTALL] == "existing"
    assert i18n.settle(tmp_path, environ=env) == EN
    assert _snapshot(tmp_path) == before


def test_an_update_restart_does_not_default_captions_to_english(tmp_path, settings_file,
                                                                monkeypatch):
    """复审（live-path-safety，提交 T）：一键更新用 os.execv 重启（updater.py），环境变量整条继承。
    新装第一次启动时界面解析成中文，之后用户在设置里把界面改成英文、没动过目标语言：更新重启
    （同一个 env 再 boot + settle）不能静默改成英文字幕。"""
    monkeypatch.setattr(i18n, "I18N_ENABLED", True)
    monkeypatch.setattr(i18n, "system_lang", lambda: ZH)
    env = {}
    _boot(tmp_path, env)
    assert env[i18n.ENV_INSTALL] == "fresh"
    assert i18n.settle(tmp_path, environ=env) == ZH
    assert settings.load_settings() == {"ui_lang": "system"}
    assert env[i18n.ENV_INSTALL] == "existing"          # 这之后 exec 出来的进程不再算新装
    settings.save_setting("ui_lang", EN)                  # 设置里的「界面语言」行
    i18n.set_choice(EN)
    _boot(tmp_path, env)                                  # 一键更新：execv，同一个 env
    assert i18n.settle(tmp_path, environ=env) == EN
    assert settings.load_settings() == {"ui_lang": "en"}


@pytest.mark.parametrize("gate", [False, True])
def test_settle_marks_the_install_as_existing_for_later_execs(tmp_path, settings_file,
                                                              monkeypatch, gate):
    """settle 不论闸开关、成败，结束时都把装机状态记成 existing。"""
    monkeypatch.setattr(i18n, "I18N_ENABLED", gate)
    env = dict(FRESH_EN)
    i18n.settle(tmp_path, environ=env)
    assert env[i18n.ENV_INSTALL] == "existing"

    def boom():
        raise RuntimeError("disk gone")

    monkeypatch.setattr(settings, "load_settings", boom)
    env = dict(FRESH_EN)
    assert i18n.settle(tmp_path, environ=env) == i18n.current()
    assert env[i18n.ENV_INSTALL] == "existing"


def test_an_install_from_the_closed_gate_weeks_stays_chinese_after_updating_across_it(
        tmp_path, settings_file, monkeypatch):
    """闸关着时装上、一直没退出，一键更新到开闸的版本：execv 继承环境变量。第一个版本的 settle
    已经把它记成老装机，开闸后的 settle 按老装机迁移成 zh（用户决定 1），不跟随系统变英文，
    也不写英文字幕。"""
    monkeypatch.setattr(i18n, "system_lang", lambda: EN)
    env = {}
    _boot(tmp_path, env)
    assert env[i18n.ENV_INSTALL] == "fresh"
    assert i18n.settle(tmp_path, environ=env) == ZH        # 闸关着：一个字节都不写
    assert not settings_file.exists()
    monkeypatch.setattr(i18n, "I18N_ENABLED", True)        # 更新到开闸的版本，execv
    _boot(tmp_path, env)
    assert i18n.settle(tmp_path, environ=env) == ZH
    assert settings.load_settings() == {"ui_lang": "zh"}


@pytest.mark.parametrize("gate, system, argv, want_lang, want_target", [
    (True, "zh", ("--ui-lang", "en"), EN, None),     # 这次靠覆盖看英文，设置解析出的仍是中文：不写
    (True, "en", ("--ui-lang", "zh"), ZH, EN),       # 这次靠覆盖看中文，下次正常启动是英文：照写
    (False, "en", ("--ui-lang", "en"), EN, None),    # 闸关着用覆盖预览英文：不写
])
def test_english_caption_default_ignores_the_one_off_override(tmp_path, settings_file, monkeypatch,
                                                              gate, system, argv, want_lang,
                                                              want_target):
    """覆盖不落盘（§3.1），不能拿它去定一个会落盘的默认值。"""
    monkeypatch.setattr(i18n, "I18N_ENABLED", gate)
    env = {i18n.ENV_INSTALL: "fresh", i18n.ENV_SYSTEM: system}
    _boot(tmp_path, env, argv)
    assert i18n.settle(tmp_path, environ=env) == want_lang
    assert settings.load_settings().get("target_lang") == want_target


def test_a_failed_caption_default_write_leaves_the_ui_language_resolved(tmp_path, settings_file,
                                                                        monkeypatch):
    monkeypatch.setattr(i18n, "I18N_ENABLED", True)
    real_save = settings.save_setting

    def save(key, value):
        if key == "target_lang":
            raise RuntimeError("disk gone")
        real_save(key, value)

    monkeypatch.setattr(settings, "save_setting", save)
    assert i18n.settle(tmp_path, environ=dict(FRESH_EN)) == EN
    assert i18n.current() == EN
    assert settings.load_settings() == {"ui_lang": "system"}


def test_main_settles_before_it_reads_target_lang():
    """T 的默认值要赶上第一次启动：main() 里 settle 必须早于读 target_lang（静态读，不 import main）。"""
    src = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
    fn = next(node for node in ast.walk(ast.parse(src))
              if isinstance(node, ast.FunctionDef) and node.name == "main")
    body = ast.get_source_segment(src, fn)
    assert body.count("i18n.settle(ROOT)") == 1
    assert body.index("i18n.settle(ROOT)") < body.index('.get("target_lang")')


def test_settle_never_raises(tmp_path, monkeypatch):
    def boom():
        raise RuntimeError("disk gone")

    monkeypatch.setattr(i18n, "I18N_ENABLED", True)
    monkeypatch.setattr(settings, "load_settings", boom)
    assert i18n.settle(tmp_path, environ={}) == ZH


def test_set_choice_and_config_info(monkeypatch):
    monkeypatch.setattr(i18n, "I18N_ENABLED", True)
    i18n._state.update(system=EN)
    assert i18n.set_choice("zh") == ZH
    assert i18n.set_choice("system") == EN
    assert i18n.set_choice("klingon") == EN               # 不认识的值忽略
    assert i18n.config_info() == {"ui_lang": EN, "ui_lang_setting": "system", "ui_lang_system": EN,
                                  "ui_lang_available": True, "ui_lang_locked": False}
    i18n._state.update(system=None)
    assert i18n.set_choice("system") == ZH
    assert i18n.config_info()["ui_lang_system"] == ZH
    monkeypatch.setattr(i18n, "I18N_ENABLED", False)
    assert i18n.set_choice("en") == ZH                    # 闸关着：存了选择也还是中文
    assert i18n.config_info()["ui_lang_available"] is False
