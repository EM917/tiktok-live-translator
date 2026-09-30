"""后端的英文场景（spec §12.1 G7 / G8）。

G8：审计和终端与界面语言无关。同一组场景分别在 use("zh") 和 use("en") 下跑，审计 JSONL 写进
临时目录、终端输出由 capsys 抓下，去掉时刻字段后两次必须**逐字节相同**——「终端、日志、审计
永远是中文」（不变量 2）由此从推论变成测试事实。同时断言英文那一次本机页面收到的确实是
英文：不然「两次一样」可能只是因为英文模式根本没生效。

前半是骨架（C4）：场景用的是生产里接受调用方文字的几个出口（_incident、_announce_health、
_publish_selfcheck），句子由测试写成 L()。各迁移提交（M 系列）在自己的测试文件里把改过的产出函数
加成场景，只断言自己拥有的句子，别的模块交进来的文字换成 ASCII 替身。

后半是跨模块组合（收紧闸 Z0）：反过来全用真函数，看几个模块的文字拼到一起之后，交到英文页面上的
仍然没有中文，审计和终端仍然与界面语言无关。
"""
import json
import re
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

from app import i18n
from app.audit import AuditLog
from app.i18n import CJK, L
from app.pipeline import Pipeline
from app.server import CaptionServer
from tests.helpers import run

TIME_KEYS = ("at", "ts", "started_at", "ended_at")
UNFILLED = re.compile(r"\{[^{}]*\}")


class FakeClient:
    def __init__(self):
        self.got = []

    async def send_json(self, msg):
        self.got.append(msg)


def _pipeline(log_dir):
    """半成品 Pipeline：真的 CaptionServer（带一个本机页面替身）和真的 AuditLog。"""
    p = Pipeline.__new__(Pipeline)
    p.server = CaptionServer(port=0)
    client = FakeClient()
    p.server.clients.add(client)
    p.server._transports[client] = None
    p.audit = AuditLog(room_url="https://www.tiktok.com/@demo/live", log_dir=log_dir)
    return p, client


def _audit_lines(path):
    """审计记录去掉时刻字段后重新编码。键序照原样：比较的是写出来的样子。"""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        out.append(json.dumps({k: v for k, v in rec.items() if k not in TIME_KEYS},
                              ensure_ascii=False))
    return out


def run_in_both_languages(scenario, tmp_path, capsys):
    """scenario(pipeline) 返回一个协程。分别在中文、英文下跑一遍，返回
    {lang: (审计行, 终端输出, 本机页面收到的消息)}。"""
    results = {}
    for lang in (i18n.ZH, i18n.EN):
        i18n._bad_once.clear()            # 坏模板的警告每个进程只打一次：两次各打各的
        capsys.readouterr()
        with i18n.use(lang):
            p, client = _pipeline(tmp_path / lang)
            run(scenario(p))
            p.audit.close(reason="user_stop")
        results[lang] = (_audit_lines(p.audit.path), capsys.readouterr().out, client.got)
    return results


def assert_language_independent(results):
    zh_audit, zh_out, _ = results[i18n.ZH]
    en_audit, en_out, _ = results[i18n.EN]
    assert en_audit == zh_audit
    assert en_out == zh_out
    assert not [line for line in en_audit + en_out.splitlines()
                if "Possible" in line or "asleep" in line or "falling behind" in line]


def assert_no_chinese_on_the_page(messages):
    """G7：发到页面上的消息，登记为界面文字的字段（i18n.UI_FIELDS）渲染后没有 CJK，也没有没填上的
    {占位符}、拼接处的双空格。messages 是本机页面替身收到的（已经渲染过的）消息。"""
    for msg in messages:
        for path, value in i18n.ui_values(msg):
            if isinstance(value, str) and value:
                where = (msg.get("type"), path, value)
                assert not CJK.search(value), where
                assert not UNFILLED.search(value), where
                assert "  " not in value.strip(), where


# ---- 场景 ---------------------------------------------------------------------------------

SLEPT = L("电脑休眠过 {} 秒，这段时间没有监听", "The computer was asleep for {} sec. Nothing was "
          "monitored during that time.")
LAGGING = L("⚠️ 识别开始落后（积压 {:.0f} 秒），报警会相应延迟",
            "⚠️ Speech recognition is falling behind ({:.0f} sec backlog). Alerts will be delayed.")
CHECKS = [
    {"name": L("语音识别", "Speech Recognition"), "level": "fail",
     "detail": L("模型没加载上", "The model didn’t load."),
     "fix": L("关掉程序再打开", "Quit and reopen the app.")},
    {"name": L("人声降噪", "Noise Reduction"), "level": "ok",
     "detail": L("已开启", "On"), "fix": ""},
]


async def _lifecycle(p):
    await p._incident("session:sleep", "warn", SLEPT.format(30))
    await p._announce_health("lagging", 12.0, text=LAGGING.format(12.0))
    await p._publish_selfcheck(CHECKS)
    await p._incident("session:sleep", "clear")


def test_audit_and_terminal_do_not_depend_on_the_ui_language(tmp_path, capsys):
    results = run_in_both_languages(_lifecycle, tmp_path, capsys)
    assert_language_independent(results)
    zh_audit, zh_out, zh_got = results[i18n.ZH]
    # 审计与终端确实写了这些句子（中文），不是两边都空
    assert "[提示] 电脑休眠过 30 秒，这段时间没有监听" in zh_out
    assert "[健康] ⚠️ 识别开始落后（积压 12 秒），报警会相应延迟" in zh_out
    types = [json.loads(line)["type"] for line in zh_audit]
    assert types == ["session_start", "health", "selfcheck", "session_end"]
    health = json.loads(zh_audit[1])
    assert health["text"] == "⚠️ 识别开始落后（积压 12 秒），报警会相应延迟"
    selfcheck = json.loads(zh_audit[2])
    assert [c["name"] for c in selfcheck["checks"]] == ["语音识别", "人声降噪"]
    # 本机页面：中文那次是中文，英文那次是英文
    _, _, en_got = results[i18n.EN]
    by_type = {m["type"]: m for m in en_got if m["type"] != "incident"}
    assert en_got[0]["text"] == "The computer was asleep for 30 sec. Nothing was monitored " \
                                "during that time."
    assert by_type["health"]["text"].startswith("⚠️ Speech recognition is falling behind (12 sec")
    assert [c["name"] for c in by_type["selfcheck"]["checks"]] == \
        ["Speech Recognition", "Noise Reduction"]
    assert zh_got[0]["text"] == "电脑休眠过 30 秒，这段时间没有监听"


def test_english_desktop_text_from_these_producers_has_no_chinese(tmp_path, capsys):
    """G7 的形状：渲染后登记为界面文字的字段里没有 CJK。自检那条持续提示
    （pipeline._selfcheck_incident_text 拼的，M5a 迁移）也在内。"""
    _, _, en_got = run_in_both_languages(_lifecycle, tmp_path, capsys)[i18n.EN]
    assert any(m.get("id") == Pipeline.SELFCHECK_INCIDENT for m in en_got)
    assert_no_chinese_on_the_page(en_got)


# ---- 跨模块组合（收紧闸 Z0） -----------------------------------------------------------------
# 各迁移提交只断言自己拥有的句子（spec §12.1 G7），别的模块交进来的文字在那些测试里都是 ASCII 替身。
# 这里全用真函数：自检行接上 bootstrap.mlx_giveup_note、localmodel.install_hint、
# ffmpeg_bin.ffmpeg_source、真检测器的 load_warnings、browser_login 的观察与步骤；顶栏的自检提示
# 接上真的自检名字与 fix；4003110 收场接上每一种浏览器观察；引擎提示接上 translator.engine_label。

PY = "/opt/py/bin/python3"
GIVEUP_AT = datetime(2026, 9, 20, 10, 0, 0).timestamp()
FDA_PATH = "/opt/anaconda3/bin/python3.13"


def _install_hint_on(monkeypatch, platform):
    """localmodel.install_hint 按 sys.platform 给三种说法：只在调它的那一下换平台，事件循环和别的
    代码都不在改过的平台下跑。"""
    from app import localmodel
    real = localmodel.install_hint

    def hint():
        with monkeypatch.context() as mp:
            mp.setattr(sys, "platform", platform)
            return real()

    monkeypatch.setattr(localmodel, "install_hint", hint)


def _rows_from_other_modules(monkeypatch, tmp_path):
    """真的自检函数，各自拼上别的模块交来的真文字。环境用替身造，文字一句都不替换。
    交回 {场景名: 行}。"""
    from app import bootstrap, ffmpeg_bin, hwdetect, selfcheck
    from app import browser_login as bl
    from app.pipeline import load_detector

    monkeypatch.setattr(sys, "executable", PY)
    rows = {}
    # 识别行 × bootstrap.mlx_giveup_note：苹果芯片却在用 CPU，GPU 加速组件装过、没装上
    bootstrap.write_mlx_giveup(tmp_path / ".venv" / bootstrap.MLX_GIVEUP, 1, "x", now=GIVEUP_AT)
    monkeypatch.setattr(selfcheck, "ROOT", tmp_path)
    monkeypatch.setattr(hwdetect, "recommend",
                        lambda **kw: {"backend": "ct2", "model": "large-v3-turbo"})
    monkeypatch.setattr(hwdetect, "detect", lambda: {"apple_silicon": True})
    monkeypatch.setattr(selfcheck, "_importable", lambda name: True)
    monkeypatch.setattr(selfcheck, "_model_cached", lambda model, backend: True)
    rows["asr"] = run(selfcheck.check_asr(SimpleNamespace(backend="auto", model=None,
                                                          device="auto")))
    # ffmpeg 行 × ffmpeg_bin.ffmpeg_source：内置与系统两种来源
    monkeypatch.setattr(ffmpeg_bin, "find_ffmpeg", lambda: "/fake/ffmpeg")
    monkeypatch.setattr(selfcheck, "_ffmpeg_runs", lambda exe: True)
    for key, system in (("ffmpeg-bundled", None), ("ffmpeg-system", "/usr/bin/ffmpeg")):
        monkeypatch.setattr(ffmpeg_bin.shutil, "which", lambda name, system=system: system)
        rows[key] = run(selfcheck.check_ffmpeg())
    # 违禁词表行 × 真检测器的 load_warnings（临时词表：一条好的，三条各有一种问题）
    terms = tmp_path / "banned_terms.txt"
    terms.write_text("curar\nre:perdí \\d+ kilos\nhola # saludo\n¡¿!\n", encoding="utf-8")
    rows["watchlist"] = run(selfcheck.check_watchlist(load_detector(str(terms))))
    # 浏览器登录态行 × browser_login 的观察、步骤与 LOGIN_STEPS
    monkeypatch.setattr(bl, "fda_targets", lambda *a, **k: [FDA_PATH])
    rows["login-none"] = selfcheck._browser_login_row({})
    rows["login-blocked"] = selfcheck._browser_login_row({"chrome": bl.BLOCKED,
                                                          "safari": bl.NOT_LOGGED_IN})
    rows["login-ok"] = selfcheck._browser_login_row({"chrome": bl.OK})
    # 翻译引擎行 × localmodel.install_hint 的三种平台说法
    google = SimpleNamespace(name="google", inner=SimpleNamespace())
    for platform in ("darwin", "win32", "linux"):
        _install_hint_on(monkeypatch, platform)
        rows["google-" + platform] = run(selfcheck.check_translator(
            SimpleNamespace(translator="auto"), google))
    return rows


def test_selfcheck_rows_made_of_other_modules_text_reach_the_page_in_english(
        monkeypatch, tmp_path, capsys):
    rows = _rows_from_other_modules(monkeypatch, tmp_path)

    async def publish(p):
        await p._publish_selfcheck(list(rows.values()))

    results = run_in_both_languages(publish, tmp_path / "run", capsys)
    assert_language_independent(results)
    en_got = results[i18n.EN][2]
    assert_no_chinese_on_the_page(en_got)
    shown = dict(zip(rows, next(m for m in en_got if m["type"] == "selfcheck")["checks"]))
    fix = shown["asr"]["fix"]
    assert fix.startswith("GPU acceleration couldn’t be installed (attempted 2026-09-20, pip "
                          "returned 1). The app won’t retry when it starts. "), fix
    assert ". When you’re not monitoring, you can also run this in " in fix, fix
    assert fix.endswith("mlx-whisper\". Then reopen the app."), fix
    assert shown["ffmpeg-bundled"]["detail"] == "Working (built-in imageio-ffmpeg)"
    assert shown["ffmpeg-system"]["detail"] == "Working (system ffmpeg)"
    watchlist = shown["watchlist"]["detail"]
    assert watchlist.startswith("1 entry active. “re:perdí \\d+ kilos” (line 2) never matches: "
                                "Accents are removed before matching"), watchlist
    assert "“¡¿!” (line 4) isn’t active" in watchlist, watchlist
    assert shown["login-none"]["fix"] == "Sign in to TikTok in Chrome (or Safari), then try again."
    blocked = shown["login-blocked"]
    assert blocked["detail"].startswith("Chrome: macOS didn’t allow access; Safari: readable, but "
                                        "no TikTok sign-in cookie"), blocked
    assert blocked["fix"].startswith("macOS didn’t allow the app to read browser data. Go to "
                                     "System Settings > Privacy & Security > Full Disk Access")
    assert "“{}”".format(FDA_PATH) in blocked["fix"]
    assert blocked["fix"].endswith(" Sign in to TikTok in Chrome (or Safari), then try again.")
    assert shown["login-ok"]["detail"] == "Chrome: found a TikTok sign-in"
    for platform in ("darwin", "win32", "linux"):
        google = shown["google-" + platform]["fix"]
        assert google.startswith("For fully local translation with no request caps: "), google
        assert "ollama.com" in google, google
    # 中文那次拼出来的是各模块自己的中文句子
    zh_checks = next(m for m in results[i18n.ZH][2] if m["type"] == "selfcheck")["checks"]
    assert zh_checks[0]["fix"].startswith("2026-09-20 安装 GPU 加速组件没成功（pip 返回 1）")


def _failing_rows(monkeypatch):
    """三种真的 fail 行：找不到 ffmpeg（有 fix）、探测硬件出错（没有 fix，提示条改用 detail）、
    违禁词表为空。"""
    from app import ffmpeg_bin, hwdetect, selfcheck

    monkeypatch.setattr(ffmpeg_bin, "find_ffmpeg", lambda: None)

    def no_hardware(**kwargs):
        raise RuntimeError("sysctl failed")

    monkeypatch.setattr(hwdetect, "recommend", no_hardware)
    args = SimpleNamespace(backend="auto", model=None, device="auto")
    return (run(selfcheck.check_ffmpeg()), run(selfcheck.check_asr(args)),
            run(selfcheck.check_watchlist(None)))


def test_the_selfcheck_banner_is_built_from_real_check_names_and_fixes(
        monkeypatch, tmp_path, capsys):
    """顶栏的自检提示（pipeline._selfcheck_incident_text）接上 selfcheck.NAMES 的名字和各行真的
    fix / detail：一项、换一项、三项。"""
    ffmpeg, asr, watchlist = _failing_rows(monkeypatch)

    async def publish(p):
        for checks in ([ffmpeg], [asr], [ffmpeg, asr, watchlist]):
            await p._publish_selfcheck(checks)

    results = run_in_both_languages(publish, tmp_path, capsys)
    assert_language_independent(results)
    assert_no_chinese_on_the_page(results[i18n.EN][2])

    def banners(lang):
        return [m["text"] for m in results[lang][2]
                if m.get("id") == Pipeline.SELFCHECK_INCIDENT and m.get("level") != "clear"]

    assert banners(i18n.EN) == [
        "Startup Check: Audio (ffmpeg) isn’t working. Quit and reopen the app to install it "
        "automatically.",
        "Startup Check: Speech Recognition isn’t working. Couldn’t detect the hardware: sysctl "
        "failed",
        "Startup Check: 3 items aren’t working (Audio (ffmpeg), Speech Recognition, Banned-Term "
        "List)"]
    assert banners(i18n.ZH) == ["自检：音频组件 ffmpeg 未生效——关闭程序后重新打开，会自动补装",
                                "自检：语音识别 未生效——硬件探测失败：sysctl failed",
                                "自检：3 项未生效（音频组件 ffmpeg、语音识别、违禁词表）"]


@pytest.mark.parametrize("login", [
    {"chrome": "blocked_by_system", "safari": "blocked_by_system"},
    {"chrome": "not_logged_in", "safari": "keychain_wait"},
    {"chrome": "cannot_decrypt", "safari": "readable"},
    {"chrome": "ok"},
    {"chrome": "error:RuntimeError"},
])
def test_the_4003110_ending_reaches_the_page_in_english_with_each_login_observation(
        monkeypatch, tmp_path, login):
    """真的管线走完一场「接口一直不给流地址」：重试横幅、没读到登录的持续提示（browser_login 的
    no_login_notice）、收场那段话（pipeline 的固定话术接上 browser_login 的 advice），交到英文
    页面上都没有中文。"""
    from app import browser_login as bl
    from app import resolver
    from app.pipeline import browser_only_message
    from tests.test_resilience_stream import ROOM, make_pipeline

    monkeypatch.setattr(bl, "fda_targets", lambda *a, **k: [FDA_PATH])
    p, server = make_pipeline(monkeypatch, tmp_path)

    borrowed = next((b for b, c in login.items() if c == bl.OK), None)

    async def gated(url, cookies=None, cookies_browser="auto", trace=None):
        if trace is not None:        # 登录直播页那一层的记录：借到了登录就记着是哪个浏览器
            trace.append({"layer": resolver.LOGIN_LAYER, "ok": False, "login": dict(login),
                          "browser": borrowed})
        raise resolver.ResolveError(L("接口没给", "No stream URL"), kind="browser_only",
                                    login=dict(login))

    monkeypatch.setattr(resolver, "resolve_stream_url", gated)
    with i18n.use(i18n.EN):
        run(p._run_stream_inner(ROOM))
    shown = [i18n.render(m, i18n.EN) for m in server.messages]
    assert_no_chinese_on_the_page(shown)
    state, detail = server.statuses[-1]
    assert state == "error"
    assert i18n.render(detail, i18n.EN) == i18n.render(
        browser_only_message(p.BROWSER_ONLY_RETRIES, login), i18n.EN)
    notices = [m["text"] for m in shown if m.get("id") == Pipeline.LOGIN_INCIDENT]
    if borrowed:
        assert notices == []
    else:
        assert notices and notices[-1] == i18n.render(bl.no_login_notice(login), i18n.EN)


def test_engine_notes_name_the_engines_in_english(monkeypatch, tmp_path):
    """引擎的回退与报错提示（pipeline）接上 translator.engine_label 的真名字（M5b 的测试里是 ASCII
    替身）：「本地 Hy-MT2 1.8B」「Google 免费接口」在英文页面上都换成了英文。"""
    from app import pipeline as pipeline_mod
    from tests.test_i18n_pipeline_en import FakeEngine, notices_en
    from tests.test_resilience_stream import make_pipeline

    p, server = make_pipeline(monkeypatch, tmp_path)
    made = []

    def fake_create(name):
        made.append(name)
        return FakeEngine(["hymt2", "google"][len(made) - 1])

    monkeypatch.setattr(pipeline_mod, "create_translator", fake_create)

    async def scenario():
        p.translator = FakeEngine("claude", status=403)
        await p._note_engine_failure(p.translator)          # 本机有本地模型：本场改用它
        p.translator = old = FakeEngine("deepl")
        await p._quota_fallback(old)                        # 额度用完，只有 Google
        p.translator = FakeEngine("hymt2", status=500, said="model not found", streak=3)
        await p._note_engine_failure(p.translator)

    with i18n.use(i18n.EN):
        run(scenario())
    assert_no_chinese_on_the_page([i18n.render(m, i18n.EN) for m in server.messages])
    notes = notices_en(server)
    assert notes[0].startswith("Claude returned HTTP 403. This session switched to Local Hy-MT2 "
                               "1.8B to keep translating."), notes
    assert "the app switched to Google (free) to keep translating" in notes[1], notes
    labels = {i18n.render(m["active_label"], i18n.EN) for m in server.messages
              if m.get("type") == "engine"}
    assert labels and labels <= {"Local Hy-MT2 1.8B", "Google (free)"}, labels
    banner = [i18n.render(m["text"], i18n.EN) for m in server.incidents(Pipeline.ENGINE_INCIDENT)
              if m["level"] != "clear"]
    assert banner and banner[-1].startswith("Local Hy-MT2 1.8B returned no translation 3 times "
                                            "in a row."), banner
