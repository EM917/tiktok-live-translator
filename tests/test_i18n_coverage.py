"""G4 覆盖（spec §12.1）：写了 `i18n: done` 的文件，界面中文必须都成对。

标记写在文件自己身上（Python `# i18n: done`、JS `// i18n: done`、HTML `<!-- i18n: done -->`），
没有共享的进度文件，并行的迁移提交不会互相冲突。没标记的文件这里不管——迁移是一个文件
一个文件来的；所有界面文件都必须标记是之后收紧闸时的事。

Python 另查 R11 的四种写法：它们会把 L() 的结果退化成普通中文 str，英文界面上那一句就露中文，
不报错也不崩，所以只能靠静态检查拦。"""
import textwrap

import pytest

from tools import i18n_pairs as P


def _py(src, path="app/example.py"):
    return [(v[1], v[2]) for v in P.check_python_coverage(path, textwrap.dedent(src))]


def _js(src):
    return [(v[1], v[2]) for v in P.check_js_coverage("web/example.js", textwrap.dedent(src))]


def _html(src):
    return [(v[1], v[2]) for v in P.check_html_coverage("web/example.html", textwrap.dedent(src))]


# ---- 全仓 ---------------------------------------------------------------------------------

def test_every_marked_file_is_fully_covered():
    marked, violations = [], []
    for path in P.source_files():
        src = P.read(path)
        if P.is_marked(path, src):
            marked.append(path)
            violations.extend(P.check_coverage(path, src))
    assert not violations, "已标记 {} 个文件：\n{}".format(len(marked), "\n".join(map(str, violations)))


@pytest.mark.parametrize("path,src,marked", [
    ("app/x.py", "# i18n: done\nX = 1\n", True),
    ("app/x.py", "X = 1  # i18n: done\n", True),
    ("app/x.py", 'X = "# i18n: done"\n', False),              # 字符串里的字样不算
    ("app/x.py", "# i18n: data\nX = 1\n", False),
    ("web/x.js", "// i18n: done\nvar x = 1;\n", True),
    ("web/x.js", "/* i18n: done */ var x = 1;\n", True),
    ("web/x.js", 'var x = "// i18n: done";\n', False),
    ("web/x.html", "<!-- i18n: done -->\n<html lang=\"zh-CN\"></html>", True),
    ("web/x.html", '<html lang="zh-CN"><p>i18n: done</p></html>', False),
])
def test_the_done_marker_is_only_read_from_comments(path, src, marked):
    assert P.is_marked(path, src) is marked


# ---- Python：界面中文 ----------------------------------------------------------------------

def test_plain_chinese_in_a_marked_python_file_is_reported():
    assert _py('x = status("正在停止…")\n') == [(1, "G4")]


def test_what_python_is_allowed_to_keep_in_chinese():
    src = '''
        """模块说明可以是中文。"""
        from .i18n import L, LN


        def f(n):
            """函数说明也可以。"""
            print("正在停止…")                               # print 的实参
            print("第 {} 次".format(n))
            status(L("正在停止…", "Stopping…"))
            status(LN(n, "{n} 项", "1 item", "{n} items").format(n=n))
            log = "[警告] 自检执行失败"                       # R8 前缀
            key = "直播页兜底"  # i18n: audit
            table = {"官方接口": 1,
                     "直播页兜底": 2}  # i18n: data
            return key, log, table
    '''
    assert _py(src) == []


def test_a_note_only_covers_its_own_statement():
    src = '''
        a = "直播页兜底"  # i18n: audit
        b = "官方接口"
    '''
    assert _py(src) == [(3, "G4")]


def test_a_terminal_only_file_is_skipped():
    assert _py('# i18n: file=terminal\nx = "二维码生成失败"\nsep = "、".join(y)\n') == []


def test_unknown_note_tags_do_not_exempt():
    assert _py('x = "直播页兜底"  # i18n: later\n') == [(1, "G4")]


# ---- Python：R11 的四种写法 ----------------------------------------------------------------

@pytest.mark.parametrize("src,line", [
    ('names = "、".join(xs)\n', 1),
    ('steps = "".join(xs)\n', 1),
    ('text = "\\n".join(missing)\n', 1),
])
def test_r11_literal_join(src, line):
    assert _py(src) == [(line, "R11")]


def test_r11_join_is_fine_with_a_pair_or_in_print_audit_or_with_a_note():
    src = '''
        from .i18n import L
        a = L("、", ", ").join(xs)
        print("、".join(xs))
        audit.health(text=" / ".join(xs))
        b = " ".join(parts)  # i18n: data
    '''
    assert _py(src) == []


def test_audit_arguments_skip_r11_but_chinese_there_still_needs_a_note():
    """R8 只自动豁免 print 的实参；写进审计的中文字面量要在行尾写 # i18n: audit。"""
    assert _py('audit.health(text="、".join(xs))\n') == [(1, "G4")]
    assert _py('audit.health(text="、".join(xs))  # i18n: audit\n') == []


def test_r11_str_of_a_caught_exception():
    src = '''
        try:
            go()
        except OSError as exc:
            status("error", str(exc))
            print("[错误] {}".format(str(exc)))
            detail = str(other)
            ok = of(exc)
    '''
    assert _py(src) == [(5, "R11")]


def test_r11_a_caught_exception_formatted_into_a_pair():
    """raise X(L(...)) 带着英文；L(...).format(exc) 把它当普通值 str() 进两臂，英文就丢了。"""
    src = '''
        try:
            go()
        except RuntimeError as exc:
            status("error", L("内部错误：{}", "Internal error: {}").format(exc))
            status("error", L("{a}：{b}", "{a}: {b}").format(a=name, b=exc))
            status("error", L("内部错误：{}", "Internal error: {}").format(of(exc)))
            status("error", NOTE.format(exc))
            print("[错误] {}".format(exc))
            audit.error(L("出错：{}", "Error: {}").format(exc))
    '''
    assert _py(src) == [(5, "R11"), (6, "R11")]


def test_r11_chinese_f_string():
    src = '''
        a = f"积压 {n} 秒"
        b = f"{n} sec"
        print(f"[健康] 积压 {n} 秒")
    '''
    assert _py(src) == [(2, "R11")]


@pytest.mark.parametrize("src", [
    'await self.server.status("error", detail[:80])\n',
    'self._incident(text[:300])\n',
    'await self._publish_viewer(error=msg[:200], ports=ports)\n',
    'await self._check("x", detail=d[:9])\n',
    'await self.server.broadcast({"type": "notice", "text": t[:120]})\n',
])
def test_r11_slice_handed_to_a_ui_sink(src):
    assert _py(src) == [(1, "R11")]


def test_r11_slice_elsewhere_or_through_bimap_is_fine():
    src = '''
        tail = key[-4:]
        await self.server.status("error", bimap(lambda s: s[:80], detail))
        audit.health(text=text[:300])
        foo(text[:10])
    '''
    assert _py(src) == []


# ---- JS ----------------------------------------------------------------------------------

def test_plain_chinese_in_a_marked_js_file_is_reported():
    src = '''
        // i18n: done
        var a = "待机";
        var b = L("待机", "Ready");
        var c = LN(n, n + " 项", "1 item", n + " items");
        var d = `共 ${n} 条`;
        var e = /[，。]/g;              // 正则不管
        var f = "中文";                 // i18n: data
        /* 注释里的中文不管 */
    '''
    assert _js(src) == [(3, "G4"), (6, "G4")]


# ---- HTML --------------------------------------------------------------------------------

def test_html_text_and_attributes_need_their_english():
    src = '''
        <html lang="zh-CN"><body>
        <span data-en="Ready">待机</span>
        <span>出错了</span>
        <button title="清空历史字幕" data-en="Clear">清空字幕</button>
        <input placeholder="粘贴 API 密钥" data-en-placeholder="Paste API key">
        <script>var s = "中文";</script>
        <style>/* 中文 */</style>
        <!-- 注释里的中文 -->
        </body></html>
    '''
    assert _html(src) == [(4, "G4"), (5, "G4")]


def test_translate_no_exempts_the_subtree_but_not_its_own_attributes():
    """R7：数据区里的文字和子孙的属性不翻；元素自己的 title/aria-label 照样要英文。"""
    src = '''
        <select id="target-lang" aria-label="目标语言" translate="no">
          <option value="zh-CN" title="简体">中文</option>
        </select>
        <select aria-label="目标语言" data-en-aria-label="Translate to" translate="no">
          <option value="ja">日本語</option>
        </select>
    '''
    assert _html(src) == [(2, "G4")]


def test_the_body_text_class_exempts_like_translate_no():
    """R7 的另一种标法（用户决定 6）：字幕、弹幕、报警原话这类正文不标 translate="no"，留给浏览器
    翻译，只带 class i18n-data。豁免范围与 translate="no" 相同：子树的文字和子孙的属性豁免，
    自己的属性照查。class 要整词匹配，只是包含这几个字母的 class 不算。"""
    src = '''
        <div id="live-translated" class="live-translated i18n-data">这款面霜<b title="译文">很好</b></div>
        <div class="i18n-data" title="大字幕">这款面霜</div>
        <div class="i18n-database">这款面霜</div>
    '''
    assert _html(src) == [(3, "G4"), (4, "G4")]


def test_data_en_covers_only_the_first_text_node():
    src = '''
        <p data-en="Stop">停止<b>x</b>然后开始</p>
        <p data-en-html="Stop, then <b title='go'>Start</b>">停止<b title="开始">x</b>然后开始</p>
    '''
    assert _html(src) == [(2, "G4")]
