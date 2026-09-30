"""G2 成对检查与 G3 同中文→同英文（spec §12.1），以及抽对用的 JS 词法器。

全仓检查从这个提交起就是严格的：界面上的每一句英文都要过这几条，漏一条就红。
下面的夹具用例证明每条规则真的会拦——检查器自己坏了（比如正则写错、词法器把正则
字面量当成除号）时，全仓那条会静默变成「0 个对、没有违例」，所以两头都要钉。"""
import ast
import json
import re

import pytest

from tests import i18n_rules as rules
from tools import i18n_pairs as P

ROOT = P.REPO_ROOT


def _rules(pairs):
    return sorted(v[2] for v in P.check_pairs(pairs))


def _py(src, path="app/example.py"):
    return P.python_pairs(path, src)


def _js(src, path="web/example.js"):
    return P.js_pairs(path, src)


def _html(src, path="web/example.html"):
    return P.html_pairs(path, src)


# ---- 全仓 ---------------------------------------------------------------------------------

def _all_pairs():
    pairs = []
    for path in P.source_files():
        pairs.extend(P.pairs_of(path, P.read(path)))
    return pairs


def test_every_pair_in_the_repo_passes_g2_and_g3():
    pairs = _all_pairs()
    violations = P.check_pairs(pairs) + P.check_consistency(pairs)
    assert not violations, "\n".join(map(str, violations))


# 句子里指路到设置分组里的某一行：一律从 Settings 走起（docs/i18n-style.md §2.1），
# 光写 “in Translation Engine” 读着像个地名，用户不知道去哪找
_BARE_SETTINGS_POINTER = re.compile(
    r"\b(?:in|of|See) (?:the )?(?:Translation Engine|Startup Check|Storage|Banned-Term Alerts)\b"
    r"(?! row)")


def test_english_pointers_to_a_settings_row_start_from_settings():
    bad = [(p.path, p.line, en) for p in _all_pairs() for en in p.ens
           if _BARE_SETTINGS_POINTER.search(en)]
    assert not bad, bad
    assert _BARE_SETTINGS_POINTER.search("You can choose another engine in Translation Engine.")
    assert not _BARE_SETTINGS_POINTER.search("Choose another engine in Settings > Translation Engine.")
    assert not _BARE_SETTINGS_POINTER.search("See the Audit Log row in Settings > Startup Check.")


# 括号里以命令或文件名收尾时右括号前不加句号（docs/i18n-style.md §2.2）：照着复制会把
# “requirements.txt.” 带进 pip
_COMMAND_THEN_PERIOD = re.compile(
    r"(?:requirements\.txt|setup\.sh|setup\.ps1|Start\.command|Start\.bat|\.app)\.\)")


def test_a_command_in_parentheses_has_no_full_stop_after_it():
    bad = [(p.path, p.line, en) for p in _all_pairs() for en in p.ens
           if _COMMAND_THEN_PERIOD.search(en)]
    assert not bad, bad
    assert _COMMAND_THEN_PERIOD.search("(Advanced: pip install -r requirements.txt.)")


def test_the_reinstall_hint_reads_the_same_everywhere():
    """「关闭程序（后）重新打开，会自动补装」只有一种英文说法。"""
    got = [(p.path, p.line, en) for p in _all_pairs() for en in p.ens
           if re.search("关闭程序后?重新打开，会自动补装", p.zh)]
    assert len(got) >= 5, got
    assert not [g for g in got if not re.search(r"Quit and reopen the app to install \w+( \w+)? "
                                                r"automatically\.", g[2])], got


def test_the_scan_covers_the_ui_sources_and_nothing_else():
    """范围读错了（比如 glob 写坏）时上面那条会假装通过：这里钉住它真的在看界面源文件。"""
    files = P.source_files()
    for must in ("main.py", "app/pipeline.py", "app/i18n.py", "web/app.js", "web/viewer.js",
                 "web/index.html", "web/viewer.html"):
        assert must in files
    assert len(files) > 40
    assert not [f for f in files if f.startswith(("tests/", "tools/"))]


def test_every_web_script_lexes_and_the_tokens_cover_the_whole_file():
    """词法器认不全，JS 里的对就会漏抽。token 之间只许有空白和注释。"""
    for path in sorted((ROOT / "web").glob("*.js")):
        src = path.read_text(encoding="utf-8")
        toks, _ = P.lex_js(src)
        assert len(toks) > 20, path.name
        pos = 0
        for t in toks:
            gap = re.sub(r"//[^\n]*|/\*.*?\*/", "", src[pos:t.start], flags=re.S)
            assert not gap.strip(), "{}:{} 有没认出来的内容 {!r}".format(path.name, t.line, gap)
            assert src[t.start:t.end] == t.value
            pos = t.end


# ---- JS 词法器 ----------------------------------------------------------------------------

def test_a_regex_with_quotes_inside_is_one_token():
    """web/normalize.js:12 的正则里有单双引号：当成除号就会把后半行吃成一个没闭合的字符串。"""
    src = (ROOT / "web" / "normalize.js").read_text(encoding="utf-8")
    toks, _ = P.lex_js(src)
    assert any(t.kind == "regex" and "'\"<>" in t.value for t in toks)


@pytest.mark.parametrize("src,regexes", [
    ("a = b / c / d;", 0),
    ("x = f(a) / 2;", 0),
    ("n = arr[0] / 2;", 0),
    ("ok = /a\"b'/.test(s);", 1),
    ("function f() { return /[/]x/g; }", 1),
    ("if (a) /x/.test(b);", 0),               # 少见的写法；按「右括号后是除号」处理，两边一致即可
    ("var s = '/not a regex/';", 0),
])
def test_regex_and_division_are_told_apart(src, regexes):
    toks, _ = P.lex_js(src)
    assert sum(t.kind == "regex" for t in toks) == regexes


def test_template_literals_keep_their_expressions():
    toks, _ = P.lex_js('var s = `共 ${n} 条，${ {a: "}"}.a } 个`;')
    tmpl = [t for t in toks if t.kind == "tmpl"]
    assert len(tmpl) == 1
    assert P.js_text(tmpl[0]) == "共  条， 个"
    assert tmpl[0].exprs == ["n", ' {a: "}"}.a ']


def test_string_escapes_are_decoded():
    toks, _ = P.lex_js(r'var s = "a\né\x41\u{1F534}\"";')
    assert P.js_text(toks[3]) == 'a\né\x41\U0001F534"'


def test_only_real_calls_count_as_pairs():
    src = ('function L(zh, en) { return zh; }\n'
           'var I = { L: 1 }; obj.L("甲", "A"); var x = L;\n'
           'if (typeof L === "undefined") { var L = I18N_.L; }\n'
           'var t = L("待机", "Ready");\n')
    pairs = _js(src)
    assert [(p.zh, p.ens) for p in pairs] == [("待机", ["Ready"])]
    assert pairs[0].line == 4


# ---- G2：Python ---------------------------------------------------------------------------

@pytest.mark.parametrize("call", [
    'L("正在停止…", "Stopping…")',
    'L("", "")',                                   # 分隔符对
    'L("\\n", "\\n")',
    'L("、", ", ")',
    'L("{}：{}", "{}: {}")',
    'L("从 {old} 变为 {new}", "changed from {old} to {new}")',
    'L("{}（{}）", "{1} ({0})")',                   # 英文可以重排语序：{} 按顺序规范成 {0}{1}
    'L("⚠️ 识别开始落后（积压 {:.0f} 秒）", "⚠️ Speech recognition is falling behind ({:.0f} sec backlog).")',
    'LN(n, "已删除 {n} 项，释放 {size}", "Deleted 1 item and freed {size}.", "Deleted {n} items and freed {size}.")',
    'L("违禁词表已修改，点「停止」再「开始翻译」后生效", "The banned-term list changed. Click Stop, then Start, to apply it.")',
    'L("有新版本 · TikTok 直播同传", "Update Available · TikTok Live Translator")',
    'L("违禁词表", "Banned-Term List")',
    'L("新的疑似违禁词报警", "New banned-term alert.")',
    'i18n.L("待机", "Ready")',
])
def test_good_python_pairs_pass(call):
    pairs = _py("x = " + call)
    assert len(pairs) == 1
    assert _rules(pairs) == []


@pytest.mark.parametrize("call,rule", [
    ('L("正在停止…", "")', "R1"),
    ('L("", "Stopping…")', "R1"),
    ('L(text, "Stopping…")', "R1"),                                     # 臂不是字面量：检查器看不见
    ('L("正在停止…")', "R1"),
    ('LN(n, "{n} 项", "1 item")', "R1"),
    ('L("积压 {:.0f} 秒", "{:.1f} sec behind")', "R2"),                  # 格式规格不同
    ('L("从 {old} 变为 {new}", "changed to {new}")', "R2"),              # 少了一个
    ('L("{}：{}", "{0}: {}")', "R2"),                                    # 英文模板本身不合法
    ('LN(n, "已删除 {n} 项", "Deleted 1 item.", "Deleted items.")', "R2"),  # many 臂少了 {n}
    ('LN(n, "已删除 {n} 项", "Deleted {x} item.", "Deleted {n} items.")', "R2"),  # one 臂多了
    ('L("开始", "Start，")', "R3"),                                      # 中文标点
    ('L("停止", "Stop 停止")', "R3"),
    ('L("⚠️ 识别开始落后", "Speech recognition is falling behind")', "R3"),   # emoji 丢了
    ('L("点「开始翻译」重试", "Click Go to try again.")', "R6"),
    ('L("到「设置」里换一个", "Choose another one in the settings.")', "R6"),  # 整词、大小写都要对
    ('L("有新版本 · TikTok 直播同传", "Update Available")', "名称"),
    ('L("这个直播间看不了", "This stream is age-restricted.")', "第八条"),
    ('L("请求太多", "You were rate-limited.")', "第八条"),
    ('L("被拦下了", "The request was blocked.")', "第八条"),
    ('L("违禁词", "a banned word")', "第八条"),                          # 放过的只有 banned term
])
def test_bad_python_pairs_are_caught(call, rule):
    assert rule in _rules(_py("x = " + call))


def test_causal_words_are_only_checked_in_the_rule8_family():
    call = 'L("没拿到流地址", "This is probably a network problem.")'
    assert _rules(_py("x = " + call, "app/translator.py")) == []
    assert _rules(_py("x = " + call, "app/resolver.py")) == ["第八条"]
    src = "def browser_only_message(n):\n    return {}\n\ndef other():\n    return {}\n".format(call, call)
    found = P.check_pairs(_py(src, "app/pipeline.py"))
    assert [v[1] for v in found] == [2]                 # 只有 browser_only_message 里那一句
    method = "class Pipeline:\n    async def _resolve_media(self):\n        return {}\n".format(call)
    assert _rules(_py(method, "app/pipeline.py")) == ["第八条"]


# 09-29 复审：这几种英文以前都能过 G2。前两个是中文 ZH_LABELS「私密」「地区」对应的标签，
# 然后是排除原因的否定句，最后是 maybe / perhaps 式的猜测
@pytest.mark.parametrize("en", [
    "This live stream is private.",
    "This live stream isn’t available in your region.",
    "It’s not available in some regions.",
    "Regional settings can hide it.",
    "It’s not a network problem. TikTok doesn’t say why.",
    "This isn’t a sign-in issue.",
    "This isn't an account problem.",
    "Maybe the streamer isn’t live.",
    "Perhaps the link is wrong.",
])
def test_family_labels_negations_and_guesses_are_caught_in_english(en):
    call = 'x = L("TikTok 不给流地址", "{}")'.format(en)
    assert _rules(_py(call, "app/resolver.py")) == ["第八条"]
    assert _rules(_py(call, "app/translator.py")) == []      # 家族以外不查（private network 一类照常用）


@pytest.mark.parametrize("en", [
    "This isn’t a live link or username.",                  # 否定句本身没问题，拦的是「不是某某问题」
    "The streamer may not be live yet.",                     # may 说将来可能发生的事，照常用
    "Couldn’t reach TikTok. Check your network connection and try again.",
    "If this keeps happening, report the problem to the developer.",
])
def test_plain_statements_still_pass_in_the_english_family(en):
    assert _rules(_py('x = L("TikTok 不给流地址", "{}")'.format(en), "app/resolver.py")) == []


def test_unlikely_is_not_a_causal_word():
    assert _rules(_py('x = L("没收到", "Nothing arrived, which is unlikely to last.")',
                      "app/resolver.py")) == []


def test_word_exceptions_need_the_exact_chinese_arm(monkeypatch):
    call = 'x = L("电脑被拦截", "The computer blocked it.")'
    assert _rules(_py(call)) == ["第八条"]
    monkeypatch.setattr(rules, "EN_WORD_EXCEPTIONS", {"电脑被拦截": "夹具：系统防火墙的原名"})
    assert _rules(_py(call)) == []


# 第八条家族的中文臂（CLAUDE.md 第八条）：前几条是这一族中文里真实残留过的说法，每一种都要拦得住
@pytest.mark.parametrize("zh", [
    "TikTok 不把流地址给程序。不是网络或限流问题",
    "这个直播间需要登录后才能观看（可能是私密或有观看限制）",
    "无法连接这个直播间：主播可能没在播，也可能是网络问题或地址有误",
    "解析直播流超时（网络不通或该地区无法访问 TikTok）",
    "你给的流地址拉不动（可能已过期），改用自动解析…",
    "直播流多次中断且自动重连失败——可能直播已结束，或网络不稳",
    "评论签名服务繁忙，稍后重试",
    "这个直播间有年龄限制",
    "多半是没登录",
    "因为没登录，所以拿不到",
    # 09-29 复审补的：排除原因的否定句、也许 / 或许 / 恐怕式的猜测
    "不是网络问题，TikTok 不说明原因",
    "这不是登录的问题",
    "主播也许没在播",
    "或许要登录才能看",
    "恐怕要等主播重新开播",
])
def test_chinese_labels_and_guesses_are_caught_in_the_rule8_family(zh):
    call = 'x = L("{}", "TikTok didn’t provide a stream URL.")'.format(zh)
    assert _rules(_py(call, "app/resolver.py")) == ["第八条"]
    assert _rules(_py(call, "app/translator.py")) == []      # 家族以外不查中文（「Google 会按 IP 限流」照常用）


@pytest.mark.parametrize("zh", [
    "TikTok 不把这个直播间的流地址给程序（代码 4003110），已自动重试 3 次，原因 TikTok 不说明；",
    "无法连接这个直播间。请检查主播是否在播、网络是否正常、地址是否正确，然后重试。",
    "拒绝访问内网/本机地址的流媒体地址（安全限制）",                  # 程序自己拒绝的，是观察
    "读到登录之前这类直播间可能解析不出流地址；其余直播间照常监听",      # 将来可能发生的事
    "第一次打开时，系统可能弹出防火墙提示（Windows），请选允许。",
])
def test_chinese_that_states_observations_or_the_future_passes_in_the_rule8_family(zh):
    call = 'x = L("{}", "TikTok didn’t provide a stream URL.")'.format(zh)
    assert _rules(_py(call, "app/resolver.py")) == []


def test_chinese_is_checked_in_the_pipeline_family_functions_only():
    call = 'L("可能已过期", "The stream URL you pasted isn’t returning data.")'
    src = ("class Pipeline:\n    async def _run_session(self):\n        return {}\n\n"
           "    def other(self):\n        return {}\n").format(call, call)
    found = P.check_pairs(_py(src, "app/pipeline.py"))
    assert [(v[1], v[2]) for v in found] == [(3, "第八条")]   # 只有 _run_session 里那一句


# ---- G2：JS 与 HTML -------------------------------------------------------------------------

@pytest.mark.parametrize("call", [
    'L("待机", "Ready")',
    'L("正在连接 @" + streamer + "…", "Connecting to @" + streamer + "…")',
    'LN(sum.fail, sum.fail + " 项功能未生效", "1 feature isn’t working", sum.fail + " features aren’t working")',
    'L("(" + n + ") 疑似违禁词 · ", "(" + n + ") Possible Banned Terms · ")',
    'L(`共 ${n} 条`, `${n} in total`)',
    'L("", "")',
    'L("、", ", ")',
])
def test_good_js_pairs_pass(call):
    pairs = _js("var x = " + call + ";")
    assert len(pairs) == 1
    assert _rules(pairs) == []


@pytest.mark.parametrize("call,rule", [
    ('L("正在连接 @" + streamer, "Connecting…")', "R2"),              # 英文丢了变量
    ('L("第 " + i + " 次", "attempt " + n)', "R2"),                     # 变量换了
    ('LN(n, n + " 项", "item " + i, n + " items")', "R2"),              # one 臂用了中文里没有的变量
    ('LN(n, n + " 项", "1 item", "items")', "R2"),                      # many 臂少了变量
    ('L("待机", "Ready，")', "R3"),
    ('L("待机", "")', "R1"),
    ('L("待机")', "R1"),
    ('L(zhText, enText)', "R1"),                                          # 两臂只有变量：检查器看不见
])
def test_bad_js_pairs_are_caught(call, rule):
    assert rule in _rules(_js("var x = " + call + ";"))


def test_a_registered_js_pair_may_leave_out_a_variable(monkeypatch):
    """R2_EN_OMITS：登记了的对，英文可以少引用中文里的变量；多引用、换变量照样报，
    没登记的照样报。"""
    omit = 'L("再点一次：改听 @" + target + "（停止监听 @" + cur + "）", "Click Again to Switch to @" + target)'
    assert "再点一次：改听 @（停止监听 @）" in rules.R2_EN_OMITS
    assert _rules(_js("var x = " + omit + ";")) == []
    monkeypatch.setattr(rules, "R2_EN_OMITS", {})
    assert _rules(_js("var x = " + omit + ";")) == ["R2"]
    monkeypatch.setattr(rules, "R2_EN_OMITS", {"再点一次：改听 @（停止监听 @）": "测试"})
    extra = omit.replace('"Click Again to Switch to @" + target', '"Switch to @" + other')
    assert _rules(_js("var x = " + extra + ";")) == ["R2"]


def test_html_pairs_come_from_every_data_en_attribute():
    src = ('<html lang="zh-CN"><body>\n'
           '<span id="s" data-en="Ready">待机</span>\n'
           '<button title="清空历史字幕" data-en-title="Clear caption history" data-en="Clear">'
           '<svg></svg>清空字幕</button>\n'
           '<p data-en-html="Edit <code>banned_terms.txt</code>.">编辑 <code>banned_terms.txt</code>。</p>\n'
           '</body></html>')
    got = sorted((p.zh, p.ens[0]) for p in _html(src))
    assert got == [("待机", "Ready"), ("清空历史字幕", "Clear caption history"), ("清空字幕", "Clear"),
                   ("编辑 banned_terms.txt。", "Edit banned_terms.txt.")]
    assert _rules(_html(src)) == []


@pytest.mark.parametrize("src", [
    '<span data-en="">待机</span>',
    '<span data-en="Ready"></span>',
    '<span data-en-titel="Ready" title="待机">x</span>',                 # 拼错的属性
    '<span data-en-title="Ready">x</span>',                             # 没有 title 可换
    '<span data-en="A" data-en-html="B">待机</span>',
    '<p data-en-html="编辑 <b>x</b>">编辑 <b>x</b></p>',                 # 英文 HTML 里还有中文
])
def test_bad_html_pairs_are_caught(src):
    assert _rules(_html(src))


def test_the_input_help_box_is_in_the_rule8_family():
    help_box = '<div id="input-help"><p data-en="This is probably blocked.">没拿到</p></div>'
    elsewhere = '<div id="other"><p data-en="This is probably fine.">可能没事</p></div>'
    assert _rules(_html(help_box, "web/index.html")) == ["第八条", "第八条"]   # blocked + probably
    assert _rules(_html(elsewhere, "web/index.html")) == []
    zh_guess = ('<div id="input-help"><p data-en="TikTok didn’t provide a stream URL.">'
                '可能是私密直播间</p></div>')
    assert _rules(_html(zh_guess, "web/index.html")) == ["第八条"]
    assert _rules(_html(zh_guess.replace("input-help", "other"), "web/index.html")) == []


# ---- G3 ---------------------------------------------------------------------------------

def test_the_same_chinese_must_have_one_english():
    pairs = _py('a = L("关掉", "Off")\nb = L("关掉", "Close")\nc = L("待机", "Ready")')
    found = P.check_consistency(pairs)
    assert [v[2] for v in found] == ["G3"]
    assert "Off" in found[0][3] and "Close" in found[0][3]


def test_duplicates_across_python_js_and_html_are_one_group():
    pairs = (_py('a = L("不限（默认）", "Any (default)")')
             + _js('var b = L("不限（默认）", "Any (default)");')
             + _html('<option value="" data-en="Any (default)">\n  不限（默认）\n</option>'))
    assert len(pairs) == 3 and P.check_consistency(pairs) == []
    pairs += _js('var c = L("不限（默认）", "Any");')
    assert [v[2] for v in P.check_consistency(pairs)] == ["G3"]


def test_the_allow_list_needs_a_reason_per_entry(monkeypatch):
    pairs = _py('a = L("关掉", "Off")\nb = L("关掉", "Close")')
    monkeypatch.setattr(rules, "SAME_ZH_DIFFERENT_EN", {"关掉": "夹具：状态与按钮"})
    assert P.check_consistency(pairs) == []


# ---- 规则表本身 ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["banned term", "Banned-Term List", "banned-term alerts",
                                  "Banned Terms", "Storage", "message usage", "Image",
                                  "The computer was suspended."])
def test_label_words_leave_product_terms_alone(text):
    assert not rules.EN_LABELS.search(text)


@pytest.mark.parametrize("text", ["age", "Age-restricted", "age restricted", "rate limit",
                                  "rate-limited", "ratelimits", "throttled", "banned", "bans",
                                  "ban", "blocked", "Blocking", "restricted", "restrictions"])
def test_label_words_are_caught(text):
    assert rules.EN_LABELS.search(text)


@pytest.mark.parametrize("text,hit", [("because", True), ("due to", True), ("caused by", True),
                                      ("The reason", True), ("Probably", True), ("likely", True),
                                      ("must be", True), ("seems to", True), ("unlikely", False),
                                      ("reasonable", False)])
def test_causal_words(text, hit):
    assert bool(rules.EN_CAUSAL.search(text)) is hit


def test_rule_tables_point_at_things_that_exist():
    """第八条家族按文件名、函数名、元素 id 认人：改名之后检查会静默失效，所以钉住它们都还在。"""
    for path, spec in rules.RULE8_FAMILY.items():
        assert (ROOT / path).is_file(), path
        if not isinstance(spec, tuple):
            continue
        src = (ROOT / path).read_text(encoding="utf-8")
        if path.endswith(".py"):
            defined = {n.name for n in ast.walk(ast.parse(src))
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            assert set(spec) <= defined, path
        else:
            ids = {el.get("id") for el in P.iter_elements(P.parse_html(src))}
            assert {s.lstrip("#") for s in spec} <= ids, path
    for table in (rules.EN_WORD_EXCEPTIONS, rules.SAME_ZH_DIFFERENT_EN):
        assert all(str(reason).strip() for reason in table.values())
    assert not [v for v in rules.UI_NAMES.values() if P.CJK.search(v)]


# ---- 金句 ---------------------------------------------------------------------------------

def test_golden_sentences_are_valid_english():
    """tests/i18n_golden.json：中文键不同、G3 聚合不到，却必须逐字相同的英文（Python 与 node 各钉一次）。"""
    golden = json.loads((ROOT / "tests" / "i18n_golden.json").read_text(encoding="utf-8"))
    values = {k: v for k, v in golden.items() if not k.startswith("_")}
    assert values["app_name_en"] == rules.PRODUCT_NAME[1]
    assert values["window_title_2_en"] == "(2) Possible Banned Terms · TikTok Live Translator"
    for key, value in values.items():
        assert re.fullmatch(r"[a-z0-9_]+_en", key), key
        assert isinstance(value, str) and value.strip(), key
        assert not P.CJK.search(value), key
        assert not rules.EN_LABELS.search(value), key
        if "TikTok" in value:
            assert rules.PRODUCT_NAME[1] in value, key
