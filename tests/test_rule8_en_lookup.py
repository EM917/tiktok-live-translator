"""CLAUDE.md 第八条的英文闸：拿不到流地址、借浏览器登录、弹幕连接这几路（spec §12.1 G11）。

这几路的失败对程序来说是不透明的：接口不给、握手被拒、读不到登录，原因对方都不说。中文按
第八条只写观察和能做的事，英文同样如此，而且更严：中文里残留的「可能是私密或有观看限制」
「主播可能没在播，也可能是网络问题」这类猜测，英文版改写成要核对的几件事，不跟着猜。

禁用词两级（tests/i18n_rules.py）：EN_LABELS 贴标签的词（age、rate limit、ban、block、
restrict……）、EN_CAUSAL 猜原因的句式（because、probably、likely……）。

三种查法：
- 源码：resolver.py / browser_login.py / comment_source.py / comments.py 里每一个 L() 对的
  英文臂；resolver.py 里每个 ResolveError 的第一个参数都得是 L() 对（或已经是双语的变量）。
- 运行：真的走一遍 resolver 的错误分类、安全校验、整条解析链收尾，以及 comment_source 的
  各个状态，在英文下渲染，看交到界面上的是不是干净的英文。
- 审计：同一条弹幕状态分别在中文、英文界面下发出，审计记录逐字节相同（终端、审计永远是中文）。

pipeline.browser_only_message 的固定话术由 M5a 管，它和这里 advice 的组合在
tests/test_browser_login.py（_FIXED_FACTS_EN）。
"""
import ast
import asyncio
import json
import re
import sys
import types

import pytest

from app import browser_login as bl
from app import i18n
from app import resolver
from app.audit import AuditLog
from app.i18n import CJK, L, Bi
from app.pipeline import Pipeline
from app.server import CaptionServer
from tests.helpers import run
from tests.i18n_rules import EN_CAUSAL, EN_LABELS
from tests.test_comment_safeguard import (BLOCKED_200, REJECTED_400, FakeProc, _run_until,
                                          jline, make_source, make_stale)
from tools import i18n_pairs as P

FILES = ("app/resolver.py", "app/browser_login.py", "app/comment_source.py", "app/comments.py")
# 句号（或右括号）后面紧跟着下一句的大写字母 = 两句英文粘在了一起
_GLUED = re.compile(r"\.(?=[A-Z]|macOS\b)|\)(?=[A-Za-z])")


def assert_clean_english(text, where):
    assert isinstance(text, str) and not isinstance(text, Bi), (where, type(text))
    assert text.strip(), where
    assert not CJK.search(text), (where, text)
    hit = EN_LABELS.search(text) or EN_CAUSAL.search(text)
    assert hit is None, (where, hit and hit.group(), text)
    assert "  " not in text.strip() and not _GLUED.search(text), (where, text)


def en(value):
    return i18n.render(value, "en")


# ---- 源码 -------------------------------------------------------------------------------

@pytest.mark.parametrize("path", FILES)
def test_every_english_arm_in_the_file_only_states_observations(path):
    pairs = P.pairs_of(path, P.read(path))
    assert pairs, path
    for pair in pairs:
        assert pair.problem is None, (path, pair.line, pair.problem)
        for arm in pair.ens:
            if not arm.strip():               # L("", " ") 这类分隔符
                continue
            assert not CJK.search(arm), (path, pair.line, arm)
            hit = EN_LABELS.search(arm) or EN_CAUSAL.search(arm)
            assert hit is None, (path, pair.line, hit and hit.group(), arm)


def test_every_resolve_error_in_the_resolver_is_raised_with_a_pair():
    """新加的 ResolveError 直接写中文字面量，英文界面上那句就露中文：这里拦下来。
    变量（message）和 of(exc) 放行——它们的来路本身也是 L() 对，由上面那条查。"""
    tree = ast.parse(P.read("app/resolver.py"))
    raised = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Name) and node.func.id == "ResolveError"]
    assert len(raised) >= 12
    for call in raised:
        first = call.args[0]
        base = first.func.value if (isinstance(first, ast.Call)
                                   and isinstance(first.func, ast.Attribute)
                                   and first.func.attr == "format") else first
        ok = (isinstance(base, ast.Call) and isinstance(base.func, ast.Name)
              and base.func.id in ("L", "LN", "of")) or isinstance(base, ast.Name)
        assert ok, (call.lineno, ast.unparse(first))


def test_layer_labels_keep_the_audit_values_and_say_them_in_english():
    """审计 trace 里的层名一个字不变；拼进界面句子的是同一句中文、另挂英文标签。"""
    for layer, label in resolver.LAYER_LABEL.items():
        assert str(label) == layer and isinstance(label, Bi)
        assert_clean_english(label.en, layer)
    assert resolver._ui_layer("WebKit") == "WebKit"
    assert resolver._ui_layer(resolver.LOGIN_LAYER).en == "live page (signed in)"


# ---- resolver：运行起来交到界面上的英文 ----------------------------------------------------

@pytest.mark.parametrize("stderr,kind", [
    ("ERROR: [tiktok:live] x: The channel is not currently live", "offline"),
    ("ERROR: Unable to find room for @x", "not_found"),
    ("ERROR: This live is private. Log in to watch", "login"),
    ("ERROR: <urlopen error [Errno 8] nodename nor servname provided>", "network"),
    ("ERROR: something nobody has seen before", "unknown"),
])
def test_ytdlp_errors_are_explained_in_english_without_guessing(stderr, kind):
    got_kind, message = resolver._classify_ytdlp_error(stderr)
    assert got_kind == kind
    text = en(message)
    assert_clean_english(text, kind)
    if kind in ("login", "network", "unknown"):                # 这三种把 yt-dlp 的原话放在最后
        assert text.endswith("\nDetails: " + stderr), text
    assert str(message) == resolver._classify_ytdlp_error(stderr)[1]


def test_unknown_ytdlp_error_turns_the_guess_into_checks():
    """中文说「主播可能没在播，也可能是网络问题或地址有误」；英文只列要核对的三件事。"""
    _, message = resolver._classify_ytdlp_error("ERROR: ???")
    assert "可能" in message
    text = en(message)
    assert "Check that the streamer is live, your network is working, and the link is correct" in text
    assert "may" not in text.split("\n")[0]


@pytest.mark.parametrize("url,expect", [
    ("ftp://example.com/a.flv", "Unsupported URL scheme: ftp."),
    ("//example.com/a.flv", "Unsupported URL scheme: (empty)."),
    ("http://127.0.0.1/a.flv", "Refused a stream URL on this computer or the local network"),
])
def test_media_url_checks_fail_in_english(url, expect):
    with pytest.raises(resolver.ResolveError) as exc:
        run(resolver._check_media_url(url))
    text = en(i18n.of(exc.value))
    assert text.startswith(expect), text
    assert_clean_english(text, url)
    assert CJK.search(str(exc.value))                         # 中文（终端、审计）照旧


def _fake_yt_dlp_module(monkeypatch):
    monkeypatch.setitem(sys.modules, "yt_dlp", types.ModuleType("yt_dlp"))


def test_the_end_of_the_lookup_chain_names_the_layers_in_english(monkeypatch):
    """所有层都没拿到：接口层内部出错、直播页兜底给的地址指向本机（没过安全校验）。
    收尾那句话要带上这两件事——中文写审计里的层名，英文写标签，句子之间有空格。"""
    _fake_yt_dlp_module(monkeypatch)

    async def api_crashes(_url, cookies_browser="auto"):
        raise TypeError("boom")

    async def no_webkit(_url, timeout=None):
        return None, False

    async def ytdlp(_url, cookies=None, browser=None, timeout=45):
        return 1, "", "ERROR: [tiktok:live] x: The channel is not currently live"

    async def page_points_home(_url, browser=None):
        return "http://127.0.0.1:9/live.flv", False

    monkeypatch.setattr(resolver, "_resolve_via_api", api_crashes)
    monkeypatch.setattr(resolver, "_resolve_via_webkit", no_webkit)
    monkeypatch.setattr(resolver, "_run_ytdlp", ytdlp)
    monkeypatch.setattr(resolver, "_resolve_from_page", page_points_home)
    trace = []
    with pytest.raises(resolver.ResolveError) as exc:
        run(resolver.resolve_stream_url("https://www.tiktok.com/@x/live",
                                        cookies_browser="none", trace=trace))
    zh = str(exc.value)
    assert "（另有拿到的地址没过安全校验：直播页兜底：拒绝访问内网/本机地址的流媒体地址（安全限制））" in zh
    assert "（另有解析路径内部出错已跳过：官方接口，详见终端）" in zh
    assert [r["layer"] for r in trace] == ["官方接口", "WebKit", "yt-dlp匿名", "直播页兜底"]
    text = en(i18n.of(exc.value))
    assert_clean_english(text, "chain end")
    assert text.startswith("The app tried every method but couldn’t get the audio stream")
    assert ("(Other stream URLs were found but failed the safety check: live page fallback: "
            "Refused a stream URL on this computer or the local network (safety check))") in text
    assert ("(Some lookup methods hit an internal error and were skipped: official API. "
            "See the terminal for details.)") in text


def test_rejected_layer_reasons_drop_their_full_stop_in_the_list():
    """「层名：原因」清单里的原因是完整句子：英文去掉句末句号，免得和「; 」、右括号叠在一起。
    中文就是原来的 str(exc)。"""
    dns = resolver.ResolveError(L("流媒体域名解析失败：{}（检查网络 / DNS）",
                                  "Couldn’t resolve the stream host {}. Check your network and "
                                  "DNS.").format("cdn.example"), kind="network")
    local = resolver.ResolveError(L("拒绝访问内网/本机地址的流媒体地址（安全限制）",
                                    "Refused a stream URL on this computer or the local network "
                                    "(safety check)."))
    for exc in (dns, local, RuntimeError("plain text.")):
        assert str(resolver._ui_reason(exc)) == str(exc)
    listed = L("；", "; ").join(L("{}：{}", "{}: {}").format(resolver._ui_layer(layer),
                                                          resolver._ui_reason(exc))
                               for layer, exc in (("官方接口", local), ("直播页兜底", dns)))
    assert str(listed) == ("官方接口：拒绝访问内网/本机地址的流媒体地址（安全限制）；"
                           "直播页兜底：流媒体域名解析失败：cdn.example（检查网络 / DNS）")
    assert en(listed) == ("official API: Refused a stream URL on this computer or the local "
                          "network (safety check); live page fallback: Couldn’t resolve the "
                          "stream host cdn.example. Check your network and DNS")


def test_the_api_refusal_says_the_code_in_english(monkeypatch):
    """接口一直回 4003110、借来的登录也没用：只说「没给」和代码，不说为什么。"""
    async def gated_status(_session, _user):
        return "123", 2

    async def gated_json(_session, _url, limit=None, headers=None):
        return {"status_code": 4003110, "data": {"prompts": ""}}

    monkeypatch.setattr(resolver, "_room_status", gated_status)
    monkeypatch.setattr(resolver, "_get_json", gated_json)
    monkeypatch.setattr(resolver, "_browser_order", lambda pref: ("chrome",))
    monkeypatch.setattr(resolver, "_read_login", lambda browser: bl.LoginRead(None, bl.NO_TIKTOK))
    with pytest.raises(resolver.ResolveError) as exc:
        run(resolver._resolve_via_api("https://www.tiktok.com/@x/live"))
    assert exc.value.kind == "browser_only"
    assert str(exc.value) == "TikTok 没有把这个直播间的流地址给程序（代码 4003110）"
    text = en(i18n.of(exc.value))
    assert text == "TikTok didn’t provide a stream URL for this live stream (code 4003110)."
    assert_clean_english(text, "4003110")


# ---- browser_login：每一种观察、每一种步骤 -------------------------------------------------

_CODES = (bl.OK, bl.BLOCKED, bl.NO_DATA, bl.NO_TIKTOK, bl.NOT_LOGGED_IN, bl.CANNOT_DECRYPT,
          bl.KEYCHAIN_WAIT, bl.READABLE, bl.NOT_READ, "error:RuntimeError", "something_new")


@pytest.mark.parametrize("code", _CODES)
def test_every_observation_reads_as_plain_english(code):
    text = en(bl.observed_text({"chrome": code}))
    assert text.startswith("Chrome: ") and text[len("Chrome: ")].islower(), text
    assert_clean_english(text, code)


def test_blocked_names_no_browser_in_english():
    """spec §10 覆盖 7：observed_text 按浏览器逐条拼，BLOCKED 的说法里不点名浏览器。"""
    text = en(bl.observed_text({"chrome": bl.BLOCKED, "safari": bl.BLOCKED}))
    assert text == "Chrome: macOS didn’t allow access; Safari: macOS didn’t allow access"
    assert en(bl.FDA_OBSERVED).strip() == "macOS didn’t allow the app to read browser data."


@pytest.mark.parametrize("targets", [["/opt/anaconda3/bin/python3.13"],
                                     ["/opt/homebrew/bin/python3.14", "/opt/anaconda3/bin/python3.13"]])
def test_full_disk_access_steps_in_english(targets):
    text = en(bl.fda_steps(targets))
    assert_clean_english(text, targets)
    assert text.startswith("Go to System Settings > Privacy & Security > Full Disk Access")
    for t in targets:
        assert "“{}”".format(t) in text
    assert ("(do this for each path)" in text) == (len(targets) > 1)


@pytest.mark.parametrize("login", [
    {"chrome": bl.BLOCKED, "safari": bl.BLOCKED},
    {"safari": bl.NOT_LOGGED_IN},
    {"chrome": bl.KEYCHAIN_WAIT, "safari": bl.BLOCKED},
    {"chrome": bl.CANNOT_DECRYPT, "brave": bl.CANNOT_DECRYPT},
    {"chrome": "error:OSError"},
    {"chrome": bl.READABLE},
    {},
])
def test_steps_and_the_no_login_notice_in_english(monkeypatch, login):
    monkeypatch.setattr(bl, "fda_targets", lambda *a, **k: ["/opt/anaconda3/bin/python3.13"])
    steps = en(bl.steps_text(login))
    if steps:
        assert_clean_english(steps, login)
    notice = en(bl.no_login_notice(login))
    assert_clean_english(notice, login)
    assert notice.startswith("The app didn’t find a TikTok sign-in in your browsers. ")
    assert notice.endswith(" This notice disappears once a sign-in is found.")
    if bl.BLOCKED in login.values():
        assert "(These steps are also in the Browser Login row of Settings > Startup Check.)" in notice


# ---- comment_source：面板上每一种状态说明 ---------------------------------------------------

def _english_details(state_log):
    return [(state, en(detail)) for state, detail in state_log if detail]


def _assert_details_clean(state_log):
    got = _english_details(state_log)
    assert got
    for state, text in got:
        assert_clean_english(text, state)
    return [text for _, text in got]


@pytest.mark.parametrize("outcome,expect", [
    ("no-update", "The comment service refused the connection (HTTP 400). The comments "
                  "component is already the latest version. Retrying in 1 min."),
    ("pip-failed", "The comment service refused the connection (HTTP 400). Couldn’t check for "
                   "a comments component update. Retrying in 1 min."),
    ("cooldown", "The comment service refused the connection (HTTP 400). Already checked for a "
                 "comments component update in the last few hours. Retrying in 1 min."),
    ("recently-failed", "The comment service refused the connection (HTTP 400). A comments "
                        "component update check failed recently. Retrying in 1 min."),
])
def test_a_refused_comments_connection_in_english(monkeypatch, outcome, expect):
    stale, _asked = make_stale(outcome, announce_first=outcome != "cooldown")
    cs, state_log, _raw, _calls = make_source(
        monkeypatch, [FakeProc([REJECTED_400], returncode=8)], on_stale=stale)
    assert _run_until(cs, lambda: any("分钟后自动重试" in d for _, d in state_log)) is True
    texts = _assert_details_clean(state_log)
    assert expect in texts, texts


def test_an_upgrade_after_a_blocked_connection_in_english(monkeypatch):
    stale, _asked = make_stale("upgraded", before="7.0.1", after="7.0.2")
    cs, state_log, _raw, calls = make_source(
        monkeypatch, [FakeProc([BLOCKED_200], returncode=7)], on_stale=stale)
    assert _run_until(cs, lambda: len(calls) >= 2, limit=100) is True
    texts = _assert_details_clean(state_log)
    assert "TikTok didn’t accept the comments connection. Checking for a comments component " \
           "update…" in texts
    assert "Comments component updated from 7.0.1 to 7.0.2. Reconnecting…" in texts


@pytest.mark.parametrize("lines,returncode,expect", [
    ([], 3, "The streamer isn’t live"),
    ([], 4, "The comment signing service returned an error. Retrying later."),
    ([], 6, "Couldn’t find this streamer"),
    ([], 5, "TikTok asked for a sign-in to read comments. Sign in to TikTok in your browser, "
            "then start again."),
    ([jline({"event": "status", "state": "error", "detail": "RuntimeError: boom"})], 1,
     "Comments connection error (RuntimeError: boom). Retrying shortly."),
])
def test_other_comment_states_in_english(monkeypatch, lines, returncode, expect):
    cs, state_log, _raw, _calls = make_source(monkeypatch, [FakeProc(lines, returncode=returncode)])
    assert _run_until(cs, lambda: any(en(d) == expect for _, d in state_log), limit=100) is True
    _assert_details_clean(state_log)


def test_a_comment_state_reaches_the_page_in_english_and_the_audit_in_chinese(tmp_path):
    """G8：同一条弹幕状态在中文、英文界面下各发一次，审计记录（去掉时刻）逐字节相同；
    本机页面那边中文就是中文、英文就是英文。"""
    from app import comment_source as cs_mod

    detail = L("{}{}，{} 分钟后自动重试", "{}{}. Retrying in {} min.").format(
        L("评论服务拒绝了连接{}", "The comment service refused the connection{}").format(
            cs_mod._http_note(400)),
        L("；", ". ") + cs_mod._FRESHEN_NOTES["no-update"], 5)

    class FakeClient:
        def __init__(self):
            self.got = []

        async def send_json(self, msg):
            self.got.append(msg)

    audits, pages = {}, {}
    for lang in (i18n.ZH, i18n.EN):
        with i18n.use(lang):
            p = Pipeline.__new__(Pipeline)
            p.server = CaptionServer(port=0)
            client = FakeClient()
            p.server.clients.add(client)
            p.server._transports[client] = None
            p.audit = AuditLog(room_url="https://www.tiktok.com/@demo/live", log_dir=tmp_path / lang)
            asyncio.run(p._publish_comment_source("error", detail, raw="http_status=400"))
            p.audit.close(reason="user_stop")
        rows = [json.loads(line) for line in p.audit.path.read_text(encoding="utf-8").splitlines()]
        audits[lang] = [{k: v for k, v in r.items() if k not in ("at", "ts", "started_at",
                                                                 "ended_at")}
                        for r in rows if r["type"] == "comment_source"]
        pages[lang] = [m["detail"] for m in client.got if m.get("type") == "comment_source"]
    assert audits[i18n.ZH] == audits[i18n.EN]
    assert audits[i18n.ZH][0]["detail"] == ("评论服务拒绝了连接（HTTP 400）；弹幕组件已是可用的"
                                            "最新版本，5 分钟后自动重试")
    assert pages[i18n.ZH] == [audits[i18n.ZH][0]["detail"]]
    assert pages[i18n.EN] == ["The comment service refused the connection (HTTP 400). The "
                              "comments component is already the latest version. Retrying in "
                              "5 min."]


def test_comment_hints_about_the_engine_in_english():
    from app import comments as comments_mod

    for hint in (comments_mod.REMOTE_ENGINE_HINT, comments_mod.LARGE_MODEL_HINT):
        assert_clean_english(en(hint), str(hint))
        assert "original text" in en(hint)
