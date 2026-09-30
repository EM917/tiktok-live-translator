"""CLAUDE.md 第八条的英文版（spec §12.1 G11）：拿不到流地址这一族的英文只写观察到的事实和能做的事。

中文原句由 test_browser_login.py、test_resolve_media.py、test_selfcheck_incident.py 钉着，这里钉英文：
- 固定事实一句不少：code 4003110、重试了几次、TikTok doesn’t say why、现在就能照做的办法；
- 不出现贴标签的词（age、rate limit、ban、blocked、restricted……，tests/i18n_rules.EN_LABELS）和
  猜原因的词（because、due to、likely……，EN_CAUSAL）；
- 没有中文字符：英文界面上这一句不会露中文。

本文件管 app/pipeline.py 的部分：browser_only_message 不带浏览器观察时（advice 为空，不经过
browser_login 的文字）、_resolve_media 的重试横幅与放弃、_confirm_offline / _host_wait 的等待与超时、
自检提示条。resolver / browser_login / comment_source 各自的句子在 tests/test_rule8_en_lookup.py。
最后一节是跨模块组合（收紧闸 Z0）：browser_only_message 的固定话术接上 browser_login.browser_only_advice
的每一个分支，整句中英文都照第八条查。
"""
import ast
import re

import pytest

from app import browser_login as bl
from app import i18n
from app import resolver as resolver_mod
from app.i18n import CJK, L
from app.pipeline import Pipeline, browser_only_message
from app.resolver import ResolveError
from tests import i18n_rules as rules
from tests.helpers import run
from tests.test_resilience_stream import ROOM, make_pipeline, scripted_resolve
from tools import i18n_pairs as P

EN = i18n.EN


def assert_rule8_clean(text):
    """一句英文：没有中文、没有贴标签的词、没有猜原因的词。"""
    assert isinstance(text, str) and text.strip(), text
    assert not CJK.search(text), text
    label = rules.EN_LABELS.search(text)
    assert label is None, (label.group(), text)
    causal = rules.EN_CAUSAL.search(text)
    assert causal is None, (causal.group(), text)


# ---- browser_only_message（4003110 收场时的那段话）------------------------------------------

BROWSER_ONLY_3_EN = (
    "TikTok didn’t provide a stream URL for this live stream (code 4003110). Retried 3 times. "
    "TikTok doesn’t say why. Sometimes it works if you click Start again a little later. "
    "Sometimes it doesn’t work for the whole stream. "
    "To watch now, paste the live link and the .flv URL from your browser together, separated "
    "by a space. A .flv URL usually works for about two weeks.")


def test_the_4003110_message_in_english_is_exactly_the_observed_facts_and_the_way_out():
    message = browser_only_message(3, None)
    assert i18n.render(message, EN) == BROWSER_ONLY_3_EN
    assert_rule8_clean(i18n.render(message, EN))


@pytest.mark.parametrize("retries,said", [(1, "Retried once."), (3, "Retried 3 times."),
                                          (5, "Retried 5 times.")])
def test_the_retry_count_reads_right_in_english(retries, said):
    text = i18n.render(browser_only_message(retries, None), EN)
    for fact in ("code 4003110", said, "TikTok doesn’t say why", "click Start",
                 "paste the live link and the .flv URL"):
        assert fact in text, fact
    assert_rule8_clean(text)


def test_neither_language_claims_a_control_room_or_names_a_cause():
    """以前中文有一句「不是 X 问题」的否定句，里面点了原因标签的名；英文从一开始就没照译，
    换成「同一时刻其它直播间正常」。复审（09-29）：那是 09-05 一次同分钟配对的结果，程序运行时
    并不解析对照房间——本机这边出了问题、所有房间都拿不到时也会这么说，把中控引向「只是这个
    房间」。两种语言现在都只写接口不给、重试了几次、TikTok 不说原因（CLAUDE.md 第八条）。
    中文的固定事实由 test_browser_login.py 的 _FIXED_FACTS 钉着。"""
    message = browser_only_message(3, None)
    assert "其它直播间" not in message and "同一时刻" not in message
    assert not rules.ZH_LABELS.search(message) and not rules.ZH_CAUSAL.search(message), message
    text = i18n.render(message, EN).lower()
    assert "other live streams" not in text and "same time" not in text
    assert "limit" not in text and "network" not in text


def test_sentences_around_the_browser_advice_are_spaced_for_any_advice():
    """advice 夹在两段固定话术之间（browser_login.browser_only_advice，英文由它自己的迁移提交写）：
    前一段英文以句号加空格收尾，后一段以大写开头，advice 为空时正好一个空格。"""
    text = i18n.render(browser_only_message(3, None), EN)
    assert "whole stream. To watch now" in text
    first = i18n.render(browser_only_message(3, None), EN).split("To watch now")[0]
    assert first.endswith(". ")


# ---- _resolve_media：等 TikTok 给地址时的横幅、放弃时的那句 ------------------------------------

def test_retry_banners_and_the_final_error_are_english_and_only_say_what_was_observed(
        monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def always(url, cookies=None, cookies_browser="auto", trace=None):
        raise ResolveError("4003110", kind="browser_only")

    monkeypatch.setattr(resolver_mod, "resolve_stream_url", always)
    with i18n.use(EN):
        with pytest.raises(ResolveError) as caught:
            run(p._resolve_media(ROOM))
    banners = [i18n.render(d, EN) for s, d in server.statuses if s == "connecting"]
    assert banners == [
        "TikTok didn’t provide a stream URL for this live stream. Retrying in 20 sec "
        "(attempt 2 of 3)…",
        "TikTok didn’t provide a stream URL for this live stream. Retrying in 20 sec "
        "(attempt 3 of 3)…"]
    for text in banners:
        assert_rule8_clean(text)
    assert i18n.render(i18n.of(caught.value), EN) == BROWSER_ONLY_3_EN
    assert "原因 TikTok 不说明" in str(caught.value)          # 审计与终端拿到的仍是中文


def test_a_dead_pasted_stream_url_says_so_in_english(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    p._media_override = "https://pull-flv-x.tiktokcdn-us.com/a.flv"

    async def dead(url, trusted=False):
        return url

    async def works(url):
        return False

    monkeypatch.setattr(resolver_mod, "_check_media_url", dead)
    monkeypatch.setattr(resolver_mod, "_media_url_works", works)
    scripted_resolve(monkeypatch, ["https://pull-flv-y.tiktokcdn-us.com/b.flv"])
    with i18n.use(EN):
        run(p._resolve_media(ROOM))
    text = i18n.render(server.statuses[0][1], EN)
    assert text == ("The stream URL you pasted isn’t returning data. Looking up the stream URL "
                    "automatically…")
    assert_rule8_clean(text)


# ---- _confirm_offline / _host_wait：房间状态不是在播时的等待与超时 --------------------------------

def test_waiting_for_the_room_and_the_timeout_in_english(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def fake_session(media, *a, **k):
        return True, 60.0

    async def probe(url):
        return 3, "http=200"

    scripted_resolve(monkeypatch, ["http://cdn/a.flv",
                                   ResolveError("没开播", kind="offline", status=3)])
    monkeypatch.setattr(resolver_mod, "probe_room_status", probe)
    monkeypatch.setattr(p, "_stream_session", fake_session)
    with i18n.use(EN):
        run(p._run_stream_inner(ROOM))
    texts = [i18n.render(d, EN) for _, d in server.statuses]
    waiting = [t for t in texts if "Checking every" in t]
    assert waiting[0] == ("TikTok’s API returned stream status 3 (not live). Checking every "
                          "60 sec for up to 10 min (0 min so far)…")
    assert server.statuses[-1][0] == "ended"
    assert texts[-1] == ("TikTok’s API didn’t report the stream as live for 10 min. Last check: "
                         "TikTok’s API returned stream status 3 (not live). Monitoring stopped. "
                         "Click Start to start again.")
    for text in waiting + texts[-1:]:
        assert_rule8_clean(text)


@pytest.mark.parametrize("status,said", [
    (None, "TikTok’s API didn’t return a stream status this time"),
    (2, "TikTok’s API returned stream status 2"),
    (4, "TikTok’s API returned stream status 4 (ended)"),
    (3, "TikTok’s API returned stream status 3 (not live)"),
])
def test_room_status_texts_only_repeat_what_the_api_returned(status, said):
    text = i18n.render(Pipeline._room_status_text(status), EN)
    assert text == said
    assert_rule8_clean(text)


# ---- 自检提示条（_selfcheck_incident_text）--------------------------------------------------
# 自检项的名字和 fix 由 selfcheck.py 的迁移提交写英文；这里用成对的替身，只看这一句本身

def _fail(name, fix):
    return {"name": name, "level": "fail", "detail": L("详情", "Details"), "fix": fix}


def test_one_failed_check_in_english_is_the_name_and_the_fix():
    fails = [_fail(L("人声降噪", "Noise Reduction"), L("重启程序后再试", "Restart the app and try again."))]
    text = Pipeline._selfcheck_incident_text(fails)
    assert text == "自检：人声降噪 未生效——重启程序后再试"
    assert i18n.render(text, EN) == ("Startup Check: Noise Reduction isn’t working. Restart the "
                                     "app and try again.")
    assert_rule8_clean(i18n.render(text, EN))


def test_several_failed_checks_in_english_are_a_count_and_the_names():
    fails = [_fail(L("人声降噪", "Noise Reduction"), ""),
             _fail(L("语音识别", "Speech Recognition"), "")]
    text = Pipeline._selfcheck_incident_text(fails)
    assert text == "自检：2 项未生效（人声降噪、语音识别）"
    assert i18n.render(text, EN) == ("Startup Check: 2 items aren’t working (Noise Reduction, "
                                     "Speech Recognition)")
    assert_rule8_clean(i18n.render(text, EN))


# ---- 静态：pipeline.py 里第八条家族的每一句中文都成了对，英文都干净 ----------------------------------

def _family_functions():
    names = rules.RULE8_FAMILY["app/pipeline.py"]
    src = P.read("app/pipeline.py")
    spans = [(n.lineno, n.end_lineno) for n in ast.walk(ast.parse(src))
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    assert len(spans) == len(names)
    return src, spans


def test_every_chinese_sentence_in_the_pipeline_rule8_family_has_its_english():
    """pipeline.py 整个文件要到引擎那一批迁完才标 `i18n: done`（G4 才开始全文件检查）；第八条家族
    这几个函数先单独钉住：G4 的规则在这几段里一条违例都没有。"""
    src, spans = _family_functions()
    violations = [v for v in P.check_python_coverage("app/pipeline.py", src + "\n# i18n: done\n")
                  if any(a <= v[1] <= b for a, b in spans)]
    assert violations == []


def test_the_english_of_the_pipeline_rule8_family_is_clean():
    _, spans = _family_functions()
    pairs = [p for p in P.python_pairs("app/pipeline.py", P.read("app/pipeline.py"))
             if any(a <= p.line <= b for a, b in spans)]
    assert len(pairs) >= 8
    for pair in pairs:
        for en in pair.ens:
            assert_rule8_clean(en)


# ---- 跨模块组合（Z0）：固定话术 × browser_only_advice 的每一个分支 --------------------------------
# 两段文字分属 pipeline（M5a）和 browser_login（M7），各自的测试只看自己那一段；接起来以后整句
# 还得守第八条，句子之间的空格也得对。每一种观察代码各走一遍，再加几种两个浏览器的组合。

_LOGINS = ([None, {}, {"chrome": bl.NOT_READ}]
           + [{"chrome": code} for code in (bl.OK, bl.BLOCKED, bl.NO_DATA, bl.NO_TIKTOK,
                                            bl.NOT_LOGGED_IN, bl.CANNOT_DECRYPT, bl.KEYCHAIN_WAIT,
                                            bl.READABLE, "error:RuntimeError", "something_new")]
           + [{"chrome": bl.OK, "safari": bl.OK},
              {"chrome": bl.OK, "safari": bl.BLOCKED},
              {"chrome": bl.BLOCKED, "safari": bl.NOT_LOGGED_IN},
              {"chrome": bl.CANNOT_DECRYPT, "edge": bl.CANNOT_DECRYPT, "safari": bl.KEYCHAIN_WAIT},
              {"safari": bl.NOT_READ, "chrome": bl.READABLE}])
# 句号（或右括号）后面紧跟着下一句的大写字母 = 两句英文粘在了一起（中文句子不用空格，英文要）
_GLUED = re.compile(r"\.(?=[A-Z]|macOS\b)|\)(?=[A-Za-z])")
_FACTS_ZH = ("代码 4003110", "原因 TikTok 不说明",
             "把直播间链接和浏览器里的 .flv 地址一起粘进来")


@pytest.mark.parametrize("retries,said", [(1, "Retried once."), (3, "Retried 3 times.")])
@pytest.mark.parametrize("login", _LOGINS, ids=lambda login: ",".join(
    "{}={}".format(b, c) for b, c in (login or {}).items()) or repr(login))
def test_the_4003110_message_with_every_browser_observation(monkeypatch, login, retries, said):
    monkeypatch.setattr(bl, "fda_targets", lambda *a, **k: ["/opt/anaconda3/bin/python3.13"])
    message = browser_only_message(retries, login)
    advice = bl.browser_only_advice(login)
    # 中文：固定事实一句不少，advice 原样夹在中间，整句不贴标签、不猜原因
    for fact in _FACTS_ZH + ("已自动重试 {} 次".format(retries),):
        assert fact in message, fact
    assert str(advice) in message
    assert not rules.ZH_LABELS.search(message) and not rules.ZH_CAUSAL.search(message), message
    # 英文：同样的事实、没有中文和两级禁用词、句子之间恰好一个空格
    text = i18n.render(message, EN)
    assert_rule8_clean(text)
    for fact in ("code 4003110", said, "TikTok doesn’t say why", "click Start",
                 "paste the live link and the .flv URL"):
        assert fact in text, (fact, text)
    assert "  " not in text and not _GLUED.search(text), text
    assert text.endswith("A .flv URL usually works for about two weeks.")
    assert i18n.render(advice, EN) in text
    if not {c for c in (login or {}).values() if c != bl.NOT_READ}:
        assert text == i18n.render(browser_only_message(retries, None), EN)
