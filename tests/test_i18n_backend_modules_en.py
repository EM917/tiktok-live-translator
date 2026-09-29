"""英文界面：其余后端模块的界面句子（spec §13 M9；G7 里这些模块自己的那一份）。

翻译引擎名与回退提示（translator）、本地翻译模型的下载失败说明与安装引导（localmodel）、
磁盘盘点与删除（diskspace）、词表体检（detector）、缺 ffmpeg（audio）、审计写不进去
（audit）、ffmpeg 来源（ffmpeg_bin）、GPU 加速组件没装上（bootstrap.mlx_giveup_note）。

每一组钉三件事：
1. 中文与改造前逐字节相同（下面写死的中文就是改造前的原样，不许跟着代码改）；
2. 英文模式下渲染出来没有 CJK，关键句子照 docs/i18n-style.md 的术语；
3. 终端和审计与界面语言无关（这些句子也会 print、写审计的，照样是中文）。

只断言本模块自己拥有的句子（spec §12.1 G7）：它们被 pipeline / selfcheck 拼进更长的句子时
那一层的英文归各自的迁移提交，跨模块的组合在收紧闸时统一加。
"""
import json
import os
import sys
from pathlib import Path

import pytest

from app import audit, audio, bootstrap, detector, diskspace, ffmpeg_bin, i18n, localmodel
from app import translator
from app.i18n import CJK, Bi, L, of, render
from app.redact import strip_query
from tests.helpers import run


def en(value):
    return render(value, i18n.EN)


def no_cjk(value):
    return not CJK.search(str(value))


# ---- translator：引擎名、回退提示、缺密钥 -------------------------------------------------

LABELS_ZH = {"auto": "自动", "hymt2": "本地 Hy-MT2 1.8B", "hymt2-7b": "本地 Hy-MT2 7B",
             "gemma": "本地 TranslateGemma", "deepl": "DeepL", "google": "Google 免费接口",
             "claude": "Claude", "openai": "OpenAI"}
LABELS_EN = {"auto": "Automatic", "hymt2": "Local Hy-MT2 1.8B", "hymt2-7b": "Local Hy-MT2 7B",
             "gemma": "Local TranslateGemma", "deepl": "DeepL", "google": "Google (free)",
             "claude": "Claude", "openai": "OpenAI"}


def test_engine_labels_are_unchanged_in_chinese_and_named_in_english():
    for name, zh in LABELS_ZH.items():
        label = translator.engine_label(name)
        assert label == zh and str(label) == zh
        assert en(label) == LABELS_EN[name]
    assert translator.engine_label(None) == "不翻译"
    assert en(translator.engine_label(None)) == "No translation"
    assert translator.engine_label("mystery") == "mystery"          # 认不出的代号原样（数据）
    # 发给页面的 JSON（engine.active_label）与改造前逐字节相同
    assert json.dumps({"active_label": translator.engine_label("hymt2")}, ensure_ascii=False) \
        == '{"active_label": "本地 Hy-MT2 1.8B"}'


def test_the_engine_fallback_note_reads_in_both_languages():
    name, note = translator.restore_engine(None, "deepl", key_lookup=lambda env: None)
    assert name == "auto"
    assert note == ("上次选的翻译引擎 DeepL 还没有密钥，本次先用自动——"
                    "在页面的「翻译引擎」里重新填一次即可")
    assert en(note) == ("The last engine you chose, DeepL, doesn’t have an API key yet, so "
                        "Automatic is used for now. Enter the key again in Settings > "
                        "Translation Engine.")
    # 终端照旧是中文（main.py：print("[警告] " + warn)）
    assert str("[警告] " + note) == "[警告] " + str(note)


@pytest.mark.parametrize("cls,who", [(translator.DeepLTranslator, "DeepL"),
                                     (translator.ClaudeTranslator, "Claude"),
                                     (translator.OpenAITranslator, "OpenAI")])
def test_a_missing_api_key_says_where_to_enter_it(monkeypatch, cls, who):
    monkeypatch.setattr(translator, "api_key", lambda name: None)
    with pytest.raises(RuntimeError) as info:
        cls()
    assert str(info.value) == "还没有填 {} 密钥——在页面上的「翻译引擎」里填一次即可".format(who)
    text = of(info.value)
    assert isinstance(text, Bi)
    assert en(text) == "No {} API key yet. Enter it once in Settings > Translation Engine.".format(who)


# ---- localmodel：下载失败的说明、安装引导 --------------------------------------------------

class _Content:
    def __init__(self, lines):
        self._it = iter([json.dumps(x).encode() for x in lines])

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None


class _Resp:
    def __init__(self, status, lines=(), body=""):
        self.status, self.content, self._body = status, _Content(lines), body

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _session(resp=None, boom=None):
    class Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, data=None):
            if boom is not None:
                raise boom
            return resp

    return Session


def test_pull_failures_read_in_both_languages(monkeypatch):
    import aiohttp

    async def gone(timeout=2):
        return False

    monkeypatch.setattr(localmodel, "is_running", gone)
    monkeypatch.setattr(aiohttp, "ClientSession", _session(
        _Resp(500, body='{"error": "no space left on device"}')))
    ok, error = run(localmodel.pull("m"))
    assert (ok, error) == (False, "HTTP 500：no space left on device")
    assert en(error) == "HTTP 500: no space left on device"

    monkeypatch.setattr(aiohttp, "ClientSession", _session(_Resp(200, [{"status": "pulling"}])))
    ok, error = run(localmodel.pull("m"))
    assert (ok, error) == (False, "下载没有收到完成信号，Ollama 已经连不上")
    assert en(error) == ("The download ended without a completion signal, and Ollama is no "
                         "longer reachable")

    boom = OSError("Cannot connect to http://127.0.0.1:11434/api/pull?token=SECRET")
    monkeypatch.setattr(aiohttp, "ClientSession", _session(boom=boom))
    ok, error = run(localmodel.pull("m"))
    assert error == "OSError：Cannot connect to http://127.0.0.1:11434/api/pull"
    assert en(error) == "OSError: Cannot connect to http://127.0.0.1:11434/api/pull"
    assert "SECRET" not in error and "SECRET" not in en(error)       # 英文一路同样去掉了 query

    monkeypatch.setattr(aiohttp, "ClientSession", _session(boom=OSError()))
    assert run(localmodel.pull("m")) == (False, "OSError")         # 没有原话：只有类型名


INSTALL_HINTS_ZH = {
    "darwin": "到 ollama.com 下载 Ollama（约 179 MB），拖进「应用程序」打开一次，"
              "然后重开本程序——翻译模型会自动下载，不用敲任何命令。",
    "win32": "到 ollama.com 下载并安装 Ollama（安装器约 1.5 GB，需要管理员权限，"
             "所以本程序无法代劳），装完重开本程序——翻译模型会自动下载。",
    "linux": "按 ollama.com 上的说明装好 Ollama 后重开本程序，翻译模型会自动下载。",
}


@pytest.mark.parametrize("platform", sorted(INSTALL_HINTS_ZH))
def test_install_hints_read_in_both_languages(monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)
    hint, url = localmodel.install_hint()
    assert hint == INSTALL_HINTS_ZH[platform] and url.startswith("https://ollama.com/")
    text = en(hint)
    assert no_cjk(text) and "ollama.com" in text and "Ollama" in text
    assert "ollama pull" not in text                     # 模型由程序自己拉，别让人敲命令
    if platform == "darwin":
        assert "Applications" in text                    # 访达里「应用程序」的英文原名
    if platform == "win32":
        assert "administrator" in text


# ---- diskspace：盘点与删除 ----------------------------------------------------------------

def _make_disk(tmp_path):
    hub = tmp_path / "hub"
    for name in ("models--Systran--faster-whisper-large-v3-turbo",
                 "models--mlx-community--whisper-large-v3-mlx", "models--someone--other-model"):
        (hub / name).mkdir(parents=True)
        (hub / name / "weights.bin").write_bytes(b"x" * 10)
    logs = tmp_path / "logs"
    logs.mkdir(parents=True)
    for stamp in ("20200101-000000", "20200102-000000", "29990101-000000"):
        (logs / "session-{}.jsonl".format(stamp)).write_text("{}\n", encoding="utf-8")
    models = [{"name": "hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M", "size": 1},
              {"name": "hf.co/tencent/Hy-MT2-7B-GGUF:Q4_K_M", "size": 2},
              {"name": "llama3:8b", "size": 3}]
    return hub, logs, models


def _inventory(tmp_path):
    hub, logs, models = _make_disk(tmp_path)
    return diskspace.inventory(hf_dir=hub, ollama_models=models, log_dir=logs,
                               active_asr=("mlx", "large-v3"),
                               active_ollama="hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M")


NOTES_ZH = {("hf", "in_use"): "本场正在用的语音模型",
            ("hf", "app"): "语音模型，删了下次识别时自动重新下载",
            ("hf", "other"): "不是本程序下载的，可能是别的工具在用",
            ("ollama", "in_use"): "当前翻译引擎正在用",
            ("ollama", "app"): "翻译模型，删了下次选用时自动重新拉取",
            ("ollama", "other"): "不是本程序使用的模型（引擎对比或别的工具留下的）"}


def test_the_disk_inventory_is_unchanged_in_chinese(tmp_path):
    items = _inventory(tmp_path)
    by_id = {it["id"]: it for it in items}
    for it in items:
        if it["kind"] != "logs":
            assert it["note"] == NOTES_ZH[(it["kind"], it["role"])]
    assert {(it["kind"], it["role"]) for it in items if it["kind"] != "logs"} == set(NOTES_ZH)
    old, every = by_id["logs:old"], by_id["logs:all"]
    assert old["label"] == "早于 30 天的会话审计日志（2 个文件）"
    assert old["note"] == "复核过的会话可以删；违禁词证据在里面，删前确认已经不需要"
    assert every["label"] == "全部会话审计日志（3 个文件，不含本场）"
    assert every["note"] == "违禁词证据在里面，删前确认已经不需要"
    # 发给页面的 JSON（disk.items）与把每个值换成普通 str 之后逐字节相同
    plain = [{k: str(v) if isinstance(v, str) else v for k, v in it.items()} for it in items]
    assert json.dumps(items, ensure_ascii=False) == json.dumps(plain, ensure_ascii=False)


def test_the_disk_inventory_reads_in_english(tmp_path):
    items = en(_inventory(tmp_path))
    for it in items:
        assert no_cjk(it["note"]) and no_cjk(it["label"]), it
    by_id = {it["id"]: it for it in items}
    assert by_id["logs:old"]["label"] == "Session audit logs older than 30 days (2 files)"
    assert by_id["logs:all"]["label"] == "All session audit logs (3 files, not including this session)"
    # hf / ollama 的 label 是模型名（数据），不包 L()
    assert by_id["hf:models--someone--other-model"]["label"] == "someone/other-model"
    assert by_id["ollama:llama3:8b"]["label"] == "llama3:8b"
    assert not isinstance(_inventory(tmp_path / "again")[0]["label"], Bi)


def test_one_log_file_is_singular_in_english(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "session-20200101-000000.jsonl").write_text("{}\n", encoding="utf-8")
    items = diskspace.inventory(hf_dir=tmp_path / "none", log_dir=logs)
    assert [it["label"] for it in items] == ["早于 30 天的会话审计日志（1 个文件）",
                                           "全部会话审计日志（1 个文件，不含本场）"]
    assert [en(it["label"]) for it in items] == [
        "Session audit logs older than 30 days (1 file)",
        "All session audit logs (1 file, not including this session)"]


def test_disk_delete_failures_read_in_both_languages(tmp_path, monkeypatch):
    hub, logs, models = _make_disk(tmp_path)

    async def ollama_refuses(name):
        return False

    def rmtree_fails(path):
        raise OSError("Resource busy")

    monkeypatch.setattr(diskspace.shutil, "rmtree", rmtree_fails)
    ids = ["hf:nope", "ollama:hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M",
           "ollama:llama3:8b", "hf:models--someone--other-model"]
    freed, done, failed = run(diskspace.delete(
        ids, hf_dir=hub, log_dir=logs, ollama_models=models, ollama_delete=ollama_refuses,
        active_ollama="hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M"))
    assert (freed, done) == (0, [])
    assert failed == ["hf:nope：不在盘点清单里，跳过",
                      "hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M：正在使用中，不删",
                      "llama3:8b：Ollama 没有删除成功",
                      "someone/other-model：Resource busy"]
    assert [en(f) for f in failed] == ["hf:nope: not in the list, skipped",
                                       "hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M: in use, not deleted",
                                       "llama3:8b: Ollama didn’t delete it",
                                       "someone/other-model: Resource busy"]


def test_a_cache_entry_that_points_outside_is_refused_in_both_languages(tmp_path):
    hub = tmp_path / "hub"
    hub.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    try:
        os.symlink(str(outside), str(hub / "models--evil--escape"), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("这台机器上建不了目录符号链接")
    _, _, failed = run(diskspace.delete(["hf:models--evil--escape"], hf_dir=hub,
                                        log_dir=tmp_path / "logs"))
    assert failed == ["evil/escape：路径不在缓存目录内，拒绝"]
    assert en(failed[0]) == "evil/escape: path is outside the cache folder, refused"
    assert outside.is_dir()


# ---- detector：词表体检的说明 -------------------------------------------------------------

def _warnings(terms):
    det = detector.BannedTermDetector(terms, line_numbers=list(range(1, len(terms) + 1)))
    return {w["entry"]: w["text"] for w in det.load_warnings}


def test_banned_list_warnings_are_unchanged_in_chinese():
    got = _warnings(["re:perdí \\d+ kilos", "re:\\d+% natural", "re:(", "¡¿!", "hola # saludo",
                     "re:perdí \\d+ kilos # nota"])
    assert got["re:perdí \\d+ kilos"] == (
        "第 1 行「re:perdí \\d+ kilos」匹配不上：检测时文本已去掉重音，正则里的「í」不会出现"
        "——请改写成 [ií] 这种形式")
    assert got["re:\\d+% natural"] == (
        "第 2 行「re:\\d+% natural」匹配不上：检测时文本已去掉标点，正则里的「%」不会出现"
        "——删掉这个符号，或在它后面加 ? 让它可有可无")
    assert got["re:("].startswith("第 3 行「re:(」没有生效：正则写法有错，没有生效（")
    assert got["¡¿!"] == "第 4 行「¡¿!」没有生效：去掉标点后什么都不剩，没有生效"
    assert got["hola # saludo"] == ("第 5 行「hola # saludo」匹配不上：行尾的 # 说明不算注释，"
                                    "连同词条一起去匹配——注释要单独写一行")
    assert got["re:perdí \\d+ kilos # nota"] == (
        "第 6 行「re:perdí \\d+ kilos # nota」匹配不上：检测时文本已去掉重音，正则里的「í」"
        "不会出现——请改写成 [ií] 这种形式；行尾的 # 说明也要挪到单独一行")
    det = detector.BannedTermDetector(["¡¿!"])                   # 没有行号
    assert det.load_warnings[0]["text"] == "「¡¿!」没有生效：去掉标点后什么都不剩，没有生效"


def test_banned_list_warnings_read_in_english():
    got = {k: en(v) for k, v in _warnings(["re:perdí \\d+ kilos", "re:\\d+% natural", "re:(",
                                           "¡¿!", "hola # saludo",
                                           "re:perdí \\d+ kilos # nota"]).items()}
    assert all(no_cjk(v) for v in got.values()), got
    assert got["re:perdí \\d+ kilos"] == (
        "“re:perdí \\d+ kilos” (line 1) never matches: Accents are removed before matching, so "
        "“í” in the pattern can never match. Write it as [ií] instead")
    assert got["re:\\d+% natural"] == (
        "“re:\\d+% natural” (line 2) never matches: Punctuation is removed before matching, so "
        "“%” in the pattern can never match. Delete it, or add ? after it to make it optional")
    assert got["re:("].startswith("“re:(” (line 3) isn’t active: The pattern has an error (")
    assert got["¡¿!"] == "“¡¿!” (line 4) isn’t active: Nothing is left once punctuation is removed"
    assert got["hola # saludo"] == (
        "“hola # saludo” (line 5) never matches: A # note at the end of a line isn’t a comment. "
        "It’s matched as part of the entry. Put comments on their own line")
    assert got["re:perdí \\d+ kilos # nota"].endswith(
        "Write it as [ií] instead. Also move the # note at the end of the line to its own line")
    det = detector.BannedTermDetector(["¡¿!"])
    assert en(det.load_warnings[0]["text"]) == \
        "“¡¿!” isn’t active: Nothing is left once punctuation is removed"


def test_the_banned_list_terminal_lines_stay_chinese(tmp_path, capsys):
    from app.pipeline import load_detector

    path = tmp_path / "banned_terms.txt"
    path.write_text("re:perdí \\d+ kilos\nhola\n", encoding="utf-8")
    outs = []
    for lang in (i18n.ZH, i18n.EN):
        with i18n.use(lang):
            load_detector(str(path))
        outs.append(capsys.readouterr().out)
    assert outs[0] == outs[1]
    assert "[警告] 违禁词表：第 1 行「re:perdí \\d+ kilos」匹配不上：检测时文本已去掉重音" in outs[0]


# ---- audio：缺 ffmpeg ---------------------------------------------------------------------

def test_missing_ffmpeg_reads_in_both_languages(monkeypatch):
    monkeypatch.setattr(audio, "find_ffmpeg", lambda: None)

    async def first_frame():
        async for _ in audio.FFmpegAudioSource("http://example.invalid/x.flv").frames():
            break

    with pytest.raises(RuntimeError) as info:
        run(first_frame())
    assert str(info.value) == "缺少音频组件 ffmpeg——请关闭程序后重新打开，会自动补装"
    assert en(of(info.value)) == ("Audio (ffmpeg) is missing. Quit and reopen the app to "
                                  "install it automatically.")


# ---- audit：写不进去时的错误文字 ---------------------------------------------------------

@pytest.mark.parametrize("exc", [
    OSError(28, "No space left on device"),
    OSError("open http://cdn.example/x.flv?sign=SECRET&expire=1 failed"),
    ValueError(),
    RuntimeError("x" * 500),
    OSError(L("同一秒内已有 99 个审计文件", "99 audit files already exist for this second")),
])
def test_clean_error_in_chinese_is_what_it_was(exc):
    """改造前是 strip_query(exc, limit)；现在中文一路是 strip_query(str(exc), limit)。
    strip_query 本来就先 str()，两者对任何异常都逐字节相同——这里把这一处钉住
    （它是 M9 里剥回检查唯一比不上的一行）。"""
    for limit in (200, 30):
        got = audit.clean_error(exc, limit)
        assert str(got) == strip_query(exc, limit)
        assert "SECRET" not in got


def test_our_own_audit_errors_keep_their_english():
    exc = OSError(L("审计文件写入返回 0 字节 http://a/b?token=SECRET",
                    "Writing to the audit file returned 0 bytes http://a/b?token=SECRET"))
    got = audit.clean_error(exc, 60)
    assert got == "审计文件写入返回 0 字节 http://a/b"
    assert en(got) == "Writing to the audit file returned 0 bytes http://a/b"


def test_a_crowded_second_reads_in_both_languages(tmp_path):
    for n in range(1, 100):
        name = "session-X{}.jsonl".format("" if n == 1 else "-{}".format(n))
        (tmp_path / name).write_text("", encoding="utf-8")
    with pytest.raises(OSError) as info:
        audit._open_new(tmp_path, "X")
    assert str(info.value) == "同一秒内已有 99 个审计文件"
    assert en(audit.clean_error(info.value)) == "99 audit files already exist for this second"


class _ZeroWrites:
    """写进去 0 字节的句柄：AuditLog 抛自己的 OSError（那句中文）。"""

    def __init__(self, real):
        self.real = real
        self.zero = True

    def write(self, b):
        return 0 if self.zero else self.real.write(b)

    def __getattr__(self, name):
        return getattr(self.real, name)


def _audit_bytes(path):
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        out.append(json.dumps({k: v for k, v in rec.items()
                               if k not in ("at", "ts", "started_at", "ended_at", "from", "to")},
                              ensure_ascii=False))
    return out


def test_audit_gaps_are_written_in_chinese_whatever_the_ui_language(tmp_path):
    written, errors = {}, {}
    for lang in (i18n.ZH, i18n.EN):
        with i18n.use(lang):
            log = audit.AuditLog(room_url="https://www.tiktok.com/@demo/live",
                                 log_dir=tmp_path / lang)
            fh = _ZeroWrites(log._fh)
            log._fh = fh
            log.alert({"term": "cura", "tier": "exact", "context": "cura"})
            errors[lang] = render(log.last_error)
            fh.zero = False
            log.alert({"term": "cura", "tier": "exact", "context": "cura"})
            log.close()
        written[lang] = _audit_bytes(Path(log.path))
    assert written[i18n.ZH] == written[i18n.EN]
    gap = next(json.loads(x) for x in written[i18n.ZH] if '"audit_gap"' in x)
    assert gap["error"] == "审计文件写入返回 0 字节"
    assert errors == {i18n.ZH: "审计文件写入返回 0 字节",
                      i18n.EN: "Writing to the audit file returned 0 bytes"}


# ---- ffmpeg_bin：ffmpeg 来源 --------------------------------------------------------------

@pytest.mark.parametrize("system,zh,english", [
    ("/usr/bin/ffmpeg", "系统", "system ffmpeg"),
    (None, "内置（imageio-ffmpeg）", "built-in imageio-ffmpeg"),
])
def test_the_ffmpeg_source_reads_in_both_languages(monkeypatch, system, zh, english):
    monkeypatch.setattr(ffmpeg_bin, "find_ffmpeg", lambda: "/somewhere/ffmpeg")
    monkeypatch.setattr(ffmpeg_bin.shutil, "which", lambda name: system)
    assert ffmpeg_bin.ffmpeg_source() == zh
    assert en(ffmpeg_bin.ffmpeg_source()) == english
    assert "✅ {}".format(ffmpeg_bin.ffmpeg_source()) == "✅ " + zh       # doctor 的终端行


# ---- bootstrap：GPU 加速组件没装上 --------------------------------------------------------

def test_the_mlx_giveup_note_reads_in_both_languages(tmp_path):
    from datetime import datetime

    at = datetime(2026, 9, 20, 10, 0, 0).timestamp()
    marker = tmp_path / ".venv" / bootstrap.MLX_GIVEUP
    bootstrap.write_mlx_giveup(marker, 1, "首次安装时单独安装 mlx-whisper 没成功", now=at)
    note = bootstrap.mlx_giveup_note(tmp_path)
    assert note == ("2026-09-20 安装 GPU 加速组件没成功（pip 返回 1），程序启动时不会再自动重试；"
                    "没在监听时每天在后台重试一次，装好后会提示")
    assert en(note) == ("GPU acceleration couldn’t be installed (attempted 2026-09-20, pip "
                        "returned 1). The app won’t retry when it starts. While it isn’t "
                        "monitoring, it retries once a day in the background and lets you know "
                        "once it’s installed")
    # 写进记号文件的说明只写不读，是中文数据
    assert json.loads(marker.read_text(encoding="utf-8"))["note"] == \
        "首次安装时单独安装 mlx-whisper 没成功"


def test_the_mlx_giveup_note_without_a_date_or_exit_code(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, "read_mlx_giveup",
                        lambda path: {"at": None, "pip_exit": None, "note": "", "ts": None})
    note = bootstrap.mlx_giveup_note(tmp_path)
    assert note.startswith("之前 安装 GPU 加速组件没成功，程序启动时不会再自动重试")
    assert en(note).startswith("GPU acceleration couldn’t be installed (attempted earlier). ")
    assert no_cjk(en(note))
    monkeypatch.setattr(bootstrap, "read_mlx_giveup",
                        lambda path: {"at": None, "pip_exit": 2, "note": "", "ts": 1e20})
    note = bootstrap.mlx_giveup_note(tmp_path)          # 时间戳读不出来：同样当作「之前」
    assert note.startswith("之前 安装 GPU 加速组件没成功（pip 返回 2）")
    assert en(note).startswith("GPU acceleration couldn’t be installed (attempted earlier, pip "
                               "returned 2). ")
