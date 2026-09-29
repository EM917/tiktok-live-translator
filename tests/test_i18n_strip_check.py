"""无损证明的两个工具（spec §12.4）：tools/i18n_strip_check.py 与 tools/i18n_const_check.py。

M 系列提交靠 strip-check 证明「只加了英文」：剥回中文后与基点逐节点相同，中文界面、终端、
审计就一个字节都没变。C3a 靠 const-check 证明「只是把字面量收成了常量」。这两个工具
说「一致」就会被贴进 PR 当证据，所以这里既钉它认得该认的，也钉它拦得住该拦的。"""
import importlib
import os
import subprocess
import sys
import textwrap

import pytest

from app import i18n
from app.i18n import L
from tests import i18n_rules as rules
from tools import i18n_const_check as C
from tools import i18n_strip_check as S
from tools.i18n_pairs import REPO_ROOT


def _d(src):
    return textwrap.dedent(src).lstrip("\n")


def _py_same(base, new):
    ok, counts, why = S.compare_python(_d(base), _d(new))
    return ok, counts, why


# ---- strip-check：Python -------------------------------------------------------------------

def test_the_spec_example_beta_reduces_to_the_base():
    """bimap(lambda s: strip_query(s, 200), of(exc)) 剥回来就是 viewer_share.py:264 的原样。"""
    base = '''
        try:
            await hub.start()
        except OSError as exc:
            await self._publish_viewer(error=strip_query(str(exc), 200), ports=ports)
    '''
    new = '''
        try:
            await hub.start()
        except OSError as exc:
            await self._publish_viewer(error=bimap(lambda s: strip_query(s, 200), of(exc)), ports=ports)
    '''
    ok, counts, _ = _py_same(base, new)
    assert ok and counts == {"bimap": 1, "of": 1}


def test_every_pair_form_strips_back_to_chinese():
    base = '''
        from . import audit, settings
        NOTE = "这个链接里带着一把钥匙"


        async def f(self, n, names, exc, msg, detail):
            await self.server.status("idle", "正在停止…")
            text = "⚠️ 识别开始落后（积压 {:.0f} 秒）".format(n)
            done = "已删除 {n} 项，释放 {size}".format(n=n, size=1)
            joined = "、".join(names)
            steps = "".join(names)
            await self.server.status("error", str(exc))
            out = msg
            title = "TikTok 直播同传"
            short = detail[:80]
            clean = strip_query(detail)
            return text, done, joined, steps, out, title, short, clean
    '''
    new = '''
        # i18n: done
        from . import audit, i18n, settings
        from .i18n import L, LN, bimap, of
        NOTE = L("这个链接里带着一把钥匙", "This link contains an access key.")


        async def f(self, n, names, exc, msg, detail):
            await self.server.status("idle", L("正在停止…", "Stopping…"))
            text = L("⚠️ 识别开始落后（积压 {:.0f} 秒）", "⚠️ Falling behind ({:.0f} sec)").format(n)
            done = LN(n, "已删除 {n} 项，释放 {size}", "Deleted 1 item.", "Deleted {n} items.").format(n=n, size=1)
            joined = L("、", ", ").join(names)
            steps = i18n.L("", "").join(names)
            await self.server.status("error", i18n.of(exc))
            out = i18n.render(msg)
            title = i18n.text("TikTok 直播同传", "en")
            short = bimap(lambda s: s[:80], detail)
            clean = bimap(strip_query, detail)
            return text, done, joined, steps, out, title, short, clean
    '''
    ok, counts, why = _py_same(base, new)
    assert ok, why
    assert counts["L"] == 5 and counts["LN"] == 1 and counts["of"] == 1 and counts["bimap"] == 2
    assert counts["i18n.render"] == 1 and counts["i18n.text"] == 1 and counts["import"] == 2


@pytest.mark.parametrize("new", [
    'x = L("正在停止", "Stopping…")\n',                    # 中文少了一个字
    'x = L("正在停止…", "Stopping…") + "!"\n',            # 顺手改了别的
    'x = of(exc)\n',                                       # 基点是 exc 不是 str(exc)
    'x = bimap(lambda s: s[:81], "正在停止…")\n',
    'x = L("正在停止…", "Stopping…")\ny = 1\n',            # 多了一条语句
])
def test_any_change_to_the_chinese_side_is_caught(new):
    base = {'x = bimap(lambda s: s[:81], "正在停止…")\n': 'x = "正在停止…"[:80]\n',
            'x = of(exc)\n': 'x = exc\n'}.get(new, 'x = "正在停止…"\n')
    ok, _, why = _py_same(base, new)
    assert not ok and why


def test_the_report_points_at_the_innermost_statement():
    base = '''
        class A:
            def f(self):
                try:
                    go()
                except OSError as exc:
                    status(str(exc))
                    other()
    '''
    new = base.replace("status(str(exc))", 'status(L("出错了", "Error"))')
    ok, _, why = _py_same(base, new)
    assert not ok
    assert "第 6 行" in why and "status(str(exc))" in why and "status('出错了')" in why


def test_registered_identity_calls_really_are_identity_in_chinese(monkeypatch):
    """R12：strip-check 把 ZH_IDENTITY_CALLS 里的 f(x, …) 剥成 x，前提是中文模式下它真的原样返回。"""
    monkeypatch.setattr(i18n, "_state", {"choice": "system", "lang": i18n.ZH, "system": None,
                                         "override": None})
    assert rules.ZH_IDENTITY_CALLS
    for name, target in rules.ZH_IDENTITY_CALLS.items():
        module, attr = target.split(":")
        fn = getattr(importlib.import_module(module), attr)
        for value in (L("正在停止…", "Stopping…"), "正在停止…", ""):
            assert str(fn(value)) == str(value), name
        assert fn("正在停止…") == "正在停止…", name


# ---- strip-check：JS 与 HTML ---------------------------------------------------------------

def test_js_pairs_strip_back_to_the_base_tokens():
    base = _d('''
        var STATUS = { idle: "待机", live: "直播中" };
        function f(n, s) {
          var raw = s.replace(/[!?.,;:)\\]'"<>]+$/, "");
          return n + " 项功能未生效" + raw;
        }
    ''')
    new = _d('''
        // i18n: done
        var STATUS = { idle: L("待机", "Ready"), live: L("直播中", "Live") };
        function f(n, s) {
          var raw = s.replace(/[!?.,;:)\\]'"<>]+$/, "");   // 正则原样
          return LN(n, n + " 项功能未生效", "1 feature isn’t working", n + " features aren’t working") + raw;
        }
    ''')
    ok, counts, why = S.compare_js(base, new)
    assert ok, why
    assert counts == {"L": 2, "LN": 1}
    ok, _, why = S.compare_js(base, new.replace('L("待机"', 'L("待命"'))
    assert not ok and "待命" in why
    ok, _, _ = S.compare_js(base, new.replace("return LN", "if (n) n++;\n  return LN"))
    assert not ok


def test_html_data_en_and_the_marker_are_invisible():
    base = _d('''
        <!doctype html>
        <html lang="zh-CN">
        <head><title>TikTok 直播同传</title></head>
        <body><span id="s" class="a">待机</span>
        <pre>  两个空格</pre></body></html>
    ''')
    new = _d('''
        <!doctype html>
        <!-- i18n: done -->
        <html lang="zh-CN">
        <head><title data-en="TikTok Live Translator">TikTok 直播同传</title></head>
        <body><span id="s" data-en="Ready" class="a">待机</span>
        <pre>  两个空格</pre></body></html>
    ''')
    ok, counts, why = S.compare_html(base, new)
    assert ok, why
    assert counts == {"data-en*": 2, "注记": 1}


@pytest.mark.parametrize("old,new", [
    ("待机</span>", "待命</span>"),
    ('class="a"', 'class="b"'),
    ("<pre>  两个空格", "<pre> 两个空格"),                  # pre 里的空白算数
    ("</span>", "</span><b></b>"),
])
def test_html_changes_outside_data_en_are_caught(old, new):
    base = '<html lang="zh-CN"><body><span id="s" class="a">待机</span><pre>  两个空格</pre></body></html>'
    ok, _, _ = S.compare_html(base, base.replace(old, new, 1))
    assert not ok


# ---- const-check -------------------------------------------------------------------------

def _const(base, new, others=None, path="app/pipeline.py"):
    resolver = C.Resolver(REPO_ROOT, {k: _d(v) for k, v in (others or {}).items()})
    return C.compare_python(path, _d(base), _d(new), resolver)


def test_literals_replaced_by_constants_that_resolve_to_them():
    base = '''
        import os


        def run(checks):
            asr = next((c for c in checks if c.get("name") == "语音识别"), None)
            status("ended", "直播已结束。可以往下翻看这一场的字幕，"
                            "或在上方输入新的直播间地址。")
            title("TikTok 直播同传")
            return asr
    '''
    new = '''
        import os
        from .i18n import APP_NAME

        LIVE_ENDED_NOTE = "直播已结束。可以往下翻看这一场的字幕，或在上方输入新的直播间地址。"


        def run(checks):
            from . import selfcheck
            asr = next((c for c in checks if c.get("name") == selfcheck.NAMES["asr"]), None)
            status("ended", LIVE_ENDED_NOTE)
            title(APP_NAME)
            return asr
    '''
    others = {"app/i18n.py": 'APP_NAME = "TikTok 直播同传"\n',
              "app/selfcheck.py": 'NAMES = {"asr": "语音识别", "ffmpeg": "音频组件 ffmpeg"}\n'}
    ok, cmp, why = _const(base, new, others)
    assert ok, why
    assert [expr for _, _, expr in cmp.mapping] == ["selfcheck.NAMES['asr']", "LIVE_ENDED_NOTE",
                                                   "APP_NAME"]
    assert [text.split(" =")[0] for _, text in cmp.added_consts] == ["LIVE_ENDED_NOTE"]
    assert len(cmp.added_imports) == 2


def test_an_existing_constant_may_become_an_alias():
    base = 'DEFAULT_TITLE = "TikTok 直播同传"\n'
    new = 'from .i18n import APP_NAME\nDEFAULT_TITLE = APP_NAME\n'
    ok, cmp, why = _const(base, new, {"app/i18n.py": 'APP_NAME = "TikTok 直播同传"\n'},
                          path="app/window_attention.py")
    assert ok, why
    assert [expr for _, _, expr in cmp.mapping] == ["APP_NAME"]


@pytest.mark.parametrize("new,why", [
    ('from .selfcheck import NAMES\nx = NAMES["ffmpeg"]\n', "值与原字面量不同"),     # 换错了常量
    ('x = unknown_name\n', "值与原字面量不同"),                                    # 解析不出来
    ('x = "语音识别" + ""\n', "字面量换成了别的表达式"),
    ('x = "语音识别"\ny = compute()\n', "基点这里没有语句"),                       # 新增的不是常量
    ('X = "语音识别"\nx = X\nY = 2\nY = 3\n', "基点这里没有语句"),                 # 同一个名字赋两次
])
def test_anything_beyond_the_three_allowed_changes_is_caught(new, why):
    others = {"app/selfcheck.py": 'NAMES = {"asr": "语音识别", "ffmpeg": "音频组件 ffmpeg"}\n'}
    ok, _, got = _const('x = "语音识别"\n', new, others)
    assert not ok and why in got


def test_a_lost_import_is_caught():
    ok, _, got = _const("import os\nimport sys\nx = 1\n", "import os\nx = 1\n")
    assert not ok and "import 不见了" in got


def test_js_literal_to_top_level_constant():
    base = 'var t = "TikTok 直播同传";\nfunction f() { return "TikTok 直播同传"; }\n'
    new = ('var APP_NAME = "TikTok 直播同传";\nvar t = APP_NAME;\n'
           'function f() { return APP_NAME; }\n')
    ok, cmp, why = C.compare_js("web/alerts.js", base, new)
    assert ok, why
    assert [expr for _, _, expr in cmp.mapping] == ["APP_NAME", "APP_NAME"]
    ok, _, _ = C.compare_js("web/alerts.js", base, new.replace('"TikTok 直播同传";\nvar t', '"TikTok";\nvar t'))
    assert not ok


# ---- 命令行：在临时仓库里走一遍 git 基点 ------------------------------------------------------

def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com",
                    "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)


def _tool(name, repo, *args):
    # 子进程按 UTF-8 写：Windows 跑器上管道默认是 ANSI 代码页，中文会被换成问号
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, str(REPO_ROOT / "tools" / name), "--repo", str(repo),
                           *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120, env=env)


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "x.py").write_text('def f():\n    return "正在停止…"\n', encoding="utf-8")
    (tmp_path / "notes.txt").write_text("不是界面源文件\n", encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def test_strip_check_cli_exit_codes(repo):
    target = repo / "app" / "x.py"
    target.write_text('from .i18n import L\n\n\ndef f():\n    return L("正在停止…", "Stopping…")\n',
                      encoding="utf-8")
    (repo / "notes.txt").write_text("改了也不查\n", encoding="utf-8")
    ok = _tool("i18n_strip_check.py", repo, "--base", "HEAD")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "`app/x.py`" in ok.stdout and "与基点一致" in ok.stdout and "notes.txt" not in ok.stdout

    target.write_text('def f():\n    return L("正在停止", "Stopping…")\n', encoding="utf-8")
    bad = _tool("i18n_strip_check.py", repo, "--base", "HEAD")
    assert bad.returncode == 1 and "不一致" in bad.stdout, bad.stdout + bad.stderr

    missing = _tool("i18n_strip_check.py", repo, "--base", "no-such-ref")
    assert missing.returncode == 2, missing.stdout + missing.stderr


def test_const_check_cli_prints_the_mapping(repo):
    (repo / "app" / "x.py").write_text('NOTE = "正在停止…"\n\n\ndef f():\n    return NOTE\n',
                                       encoding="utf-8")
    ok = _tool("i18n_const_check.py", repo, "--base", "HEAD")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "| `app/x.py:5` | '正在停止…' | `NOTE` |" in ok.stdout

    (repo / "app" / "x.py").write_text('NOTE = "正在停止"\n\n\ndef f():\n    return NOTE\n',
                                       encoding="utf-8")
    bad = _tool("i18n_const_check.py", repo, "--base", "HEAD")
    assert bad.returncode == 1, bad.stdout + bad.stderr
