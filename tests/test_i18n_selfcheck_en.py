"""启动自检的英文（spec §12.1 G7 / G8，迁移提交 M6）。

自检每一行的 name / detail / fix 都写成 L()：终端、审计拿中文，桌面页在发出去的那一刻换成英文。
这里把 app/selfcheck.py 能产出的每一种行都真的跑出来（环境用替身造，数据全是 ASCII），断言：

- G7：英文界面上这些行没有中文，也没有没填上的 {占位符}；英文模板写坏会当场抛（NET=strict），
  不会悄悄退回中文；
- 拼接出来的长句（安装命令接在说明后面、单复数）英文语序对，整句钉住；
- G8：同一批行经 Pipeline.run_selfcheck 发出，审计 JSONL 与终端输出在中英两种界面下逐字节相同。

M6 分两个提交：这一个先迁 11 个名字和组件、识别、翻译引擎几行（SCENARIOS 里的这几项），
违禁词表、领域词表、审计、解析、登录、弹幕、磁盘几行在下一个提交补上。

只断言本模块自己的句子（spec §12.1 G7「每个 M 只断言自己拥有的句子」）。别的模块拼进来的文字——
bootstrap.mlx_giveup_note、localmodel.install_hint、ffmpeg_bin.ffmpeg_source——这里都换成
ASCII 替身：它们的英文归各自的迁移提交，跨模块组合留给收紧闸的提交（Z0）。
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

ENGLISH_NAMES = {"Audio (ffmpeg)", "Noise Reduction", "Speech Recognition", "Translation Engine",
                 "Banned-Term List", "Glossary", "Audit Log", "Stream Lookup", "Browser Login",
                 "Comments", "Storage"}
UNFILLED = re.compile(r"\{[^{}]*\}")


class Engine:
    """管线建好的翻译引擎替身：自检只看 name 和 inner 上的几个属性。"""

    def __init__(self, name, **inner):
        self.name = name
        self.inner = SimpleNamespace(**inner)


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


SCENARIOS = {"ffmpeg": _ffmpeg, "denoise": _denoise, "asr": _asr, "translator": _translator,
             "deepl": _deepl}


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
