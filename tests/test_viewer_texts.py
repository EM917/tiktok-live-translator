"""规则八的文案闸，按 A8 修订。

拦的是「给不透明失败贴原因」和因果断言句式——这些词每一个都曾被真的写进过代码
或对话：4003110 被先后解释成年龄限制、IP 限流、被我们打坏，三个全错。手机同看
这边的不透明失败是「手机打不开」，程序这边能观察到的只有「没有连接进来」。

**不拦**「防火墙」「Wi-Fi」「路由器」这类名词：说清系统会弹什么框、要连哪个
Wi-Fi，是可观察事实加可做的事，是要写出来的（A8）。

英文一样（spec §12.1 G11）：界面上的句子是 L("中文", "English") 对，英文臂同样不许贴标签
（tests/i18n_rules.py 的 EN_LABELS：age、rate limit、ban、block、restrict……），也不许有猜原因
的句式（EN_CAUSAL：because、probably、likely、must be……）。手机同看这一路整个在第八条家族里。
"""
import ast
import asyncio
from types import SimpleNamespace

import pytest

from app import i18n
from app import pipeline as pipeline_mod
from app import settings as settings_mod
from app import viewer as viewer_mod
from app.i18n import CJK, Bi, bimap, of
from app.redact import strip_query
from tests.i18n_rules import EN_CAUSAL, EN_LABELS
from tools import i18n_pairs as P

ROOT = viewer_mod.WEB_DIR.parent

BANNED = ("年龄", "限流", "封禁", "被墙", "拦截", "AP 隔离",
          "是因为", "应该是", "多半", "可能是", "导致")


def assert_clean_en(text, where):
    assert not CJK.search(text), (where, text[:200])
    hit = EN_LABELS.search(text) or EN_CAUSAL.search(text)
    assert hit is None, (where, hit and hit.group(), text[:200])


def assert_clean(text, where):
    for word in BANNED:
        assert word not in text, (where, word, text[:200])
    if isinstance(text, Bi):
        assert_clean_en(text.en, where)


def operator_strings(source):
    """源码里会被人读到的中文串：字符串字面量，**不含**注释和文档字符串。

    只扫字符串是刻意的。注释和 docstring 是写给改代码的人的，里面正该出现
    「控制端口漂移是因为中控没有别的入口」「限流在 ViewerHub 里做」这种话；
    闸要拦的是发到界面上的句子。
    """
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docstrings:
            out.append(node.value)
    return out


def assert_source_clean(source, where):
    found = operator_strings(source)
    assert found, where
    for text in found:
        assert_clean(text, where)


def section(text, start_marker, end_marker):
    head = text.index(start_marker)
    tail = text.index(end_marker, head)
    return text[head:tail]


def share_card_region(html):
    """index.html 里 #share-card 那一块：从它那一行起，到下一个同缩进的兄弟
    `<div id=` 为止。只扫这一块——别的卡片的文案不是这次改动的责任。"""
    lines = html.splitlines()
    for i, line in enumerate(lines):
        if 'id="share-card"' in line:
            indent = len(line) - len(line.lstrip())
            for j in range(i + 1, len(lines)):
                nxt = lines[j]
                if "<div id=" in nxt and (len(nxt) - len(nxt.lstrip())) <= indent:
                    return "\n".join(lines[i:j])
            return "\n".join(lines[i:])
    return None


def test_the_viewer_module_texts_are_clean():
    assert_source_clean((ROOT / "app" / "viewer.py").read_text(encoding="utf-8"),
                        "app/viewer.py")


def test_the_new_pipeline_block_is_clean():
    """手机同看那一块已经从 app/pipeline.py 纯搬移到 app/viewer_share.py
    （ViewerShareMixin，方法体逐字未改动）——扫这个新文件，不是靠旧的注释
    标记在 pipeline.py 里挖一段：那两行标记搬走后就不在那儿了，继续按老办法
    找只会读到不相关的文本，或者干脆读不到（ValueError），从而假装通过。"""
    text = (ROOT / "app" / "viewer_share.py").read_text(encoding="utf-8")
    assert "手机同看" in text
    assert_source_clean(text, "app/viewer_share.py")


def test_the_new_audit_methods_are_clean():
    text = (ROOT / "app" / "audit.py").read_text(encoding="utf-8")
    block = section(text, "    def viewer_share(", "    def window_closed(")
    assert_source_clean("class _Scan:\n" + block, "app/audit.py 的 viewer_* 方法")


def test_every_operator_note_is_clean():
    """模块里那几段整句文案逐条过闸（share_note 组出来的每一种都在这里）。中英两臂都查。"""
    notes = [getattr(viewer_mod, name) for name in dir(viewer_mod)
             if name.startswith("NOTE_")]
    assert len(notes) >= 10
    for note in notes:
        assert isinstance(note, Bi), note          # 每一句都有英文
        assert_clean(note, "NOTE 常量")


_SHARE_CASES = {
    "off": dict(on=False),
    "open": dict(on=True, port=8766, url="http://192.168.1.23:8766/#k=abc", ip="192.168.1.23",
                 ips=["192.168.1.23"], viewers=2),
    "several addresses": dict(on=True, port=8766, url="http://192.168.1.23:8766/#k=abc",
                              ip="192.168.1.23", ips=["192.168.1.23", "10.0.0.5"], viewers=1),
    "no LAN address": dict(on=True, port=8766, url=None, viewers=0),
    "no QR code": dict(on=True, port=8766, url="http://192.168.1.23:8766/#k=abc",
                       ip="192.168.1.23", ips=["192.168.1.23"], qr_ok=False),
    "address changed": dict(on=True, port=8766, url="http://192.168.1.24:8766/#k=abc",
                            ip="192.168.1.24", ips=["192.168.1.24"],
                            ip_changed=("192.168.1.23", "192.168.1.24")),
    "port busy": dict(on=False, port=8766, ports=[8766, 8767, 8770],
                      error="[Errno 48] Address already in use"),
}


@pytest.mark.parametrize("case", sorted(_SHARE_CASES))
def test_every_share_note_reads_as_plain_english(case):
    """share_note 组出来的每一种卡片文案，英文逐行对应中文，没有中文、没有贴标签的词。"""
    note = viewer_mod.share_note(**_SHARE_CASES[case])
    assert isinstance(note, Bi)
    assert_clean(note, case)
    en = i18n.render(note, "en")
    assert en.count("\n") == str(note).count("\n"), (case, en)
    assert "  " not in en and not en.startswith(" "), (case, en)


def test_the_share_note_english_wording():
    note = i18n.render(viewer_mod.share_note(**_SHARE_CASES["several addresses"]), "en")
    lines = note.split("\n")
    assert lines[0] == "On. On a phone connected to the same Wi-Fi, scan the QR code or open: " \
                       "http://192.168.1.23:8766/#k=abc"
    assert lines[1] == "1 watching (limit {})".format(viewer_mod.MAX_VIEWERS)
    assert lines[3] == ("This computer has several network addresses: 192.168.1.23, 10.0.0.5. "
                        "The QR code uses 192.168.1.23. If a phone can’t open it, type another "
                        "address from the list.")
    busy = i18n.render(viewer_mod.share_note(**_SHARE_CASES["port busy"]), "en")
    assert busy == ("Couldn’t open a port for Phone Viewing (tried 8766–8770). Details: [Errno 48] "
                    "Address already in use. Phone Viewing is still off. Quit apps that use those "
                    "ports, then try again.")
    # 换链接确认框：{n} 原样下发，前端点按钮时才填
    assert "{n}" in viewer_mod.NOTE_ROTATE_CONFIRM.en


def test_no_free_viewer_port_is_an_oserror_that_keeps_its_english():
    """端口候选一个都没有时抛的是 OSError（不能是 ViewerError：那是 RuntimeError，会逃出
    viewer_share 的 except OSError，同看开关收不到回执），文字带着英文。viewer_share 取用处
    用 of + bimap 清洗截断，两种语言都保住。"""
    hub = viewer_mod.ViewerHub(SimpleNamespace(), ports=[])
    with pytest.raises(OSError) as exc:
        asyncio.run(hub.start())
    assert str(exc.value) == "没有可用的观众端口"
    err = bimap(lambda s: strip_query(s, 200), of(exc.value))
    assert err == "没有可用的观众端口" and err.en == "No viewer port is available"
    note = viewer_mod.share_note(False, port=8766, ports=[8766], error=err)
    assert str(note) == ("端口 8766 都没能打开监听，系统返回：没有可用的观众端口。已保持关闭。"
                         "可以关掉占用这些端口的程序后再打开一次。")
    assert i18n.render(note, "en") == ("Couldn’t open a port for Phone Viewing (tried 8766). "
                                       "Details: No viewer port is available. Phone Viewing is "
                                       "still off. Quit apps that use those ports, then try again.")


def test_a_busy_port_reaches_the_desktop_in_english(monkeypatch, tmp_path):
    """整条路：中控点打开 → 端口全被占 → 发给桌面的 viewer 载荷。note 是双语的：中文与原来
    逐字相同，英文里系统原话原样放在 Details 后面。"""
    from tests.test_viewer_control import FakeHub, FakeQR, StubServer, install_qr

    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", tmp_path / "settings.json")
    terms = tmp_path / "banned_terms.txt"
    terms.write_text("", encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms)
    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source="es", beam=5, context=False,
        asr_temperature=None, glossary=None, backend="auto", model=None, device="auto",
        compute_type="auto", denoise="off", banned_terms=None)
    server = StubServer()
    p = pipeline_mod.Pipeline(args, server)
    monkeypatch.setattr(p, "_make_viewer_hub", lambda ports, token: FakeHub(
        ports, token, fail=OSError(i18n.L("没有可用的观众端口", "No viewer port is available"))))
    monkeypatch.setattr(p, "_start_viewer_ip_watch", lambda: None)
    install_qr(monkeypatch, FakeQR())
    asyncio.run(p._set_viewer_share(True))
    note = server.of_type("viewer")[-1]["note"]
    assert "系统返回：没有可用的观众端口。" in note
    en = i18n.render(note, "en")
    assert "Details: No viewer port is available. Phone Viewing is still off." in en
    assert_clean(note, "port busy")


@pytest.mark.parametrize("relative", ["web/viewer.js", "web/viewer.html"])
def test_the_phone_page_english_is_clean(relative):
    """手机页自己的英文（L() 对与 data-en*）。还没写英文的时候这里没有对可查。"""
    for pair in P.pairs_of(relative, P.read(relative)):
        for arm in pair.ens:
            if arm.strip():
                assert_clean_en(arm, "{}:{}".format(relative, pair.line))


@pytest.mark.parametrize("relative", ["web/viewer.html", "web/viewer.js"])
def test_the_phone_page_texts_are_clean(relative):
    path = ROOT / relative
    if not path.is_file():
        pytest.skip("{} 还没落地（由另一份改动提供）".format(relative))
    assert_clean(path.read_text(encoding="utf-8"), relative)


def test_the_desktop_share_card_texts_are_clean():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    region = share_card_region(html)
    if region is None:
        pytest.skip("web/index.html 里还没有 #share-card（由另一份改动提供）")
    assert_clean(region, "web/index.html 的 #share-card 段")


def test_viewer_files_are_plain_names_that_really_exist():
    """白名单里的名字既要安全（不含分隔符），也要真的在 web/ 下——
    少一个文件，手机页就是一块空白，而服务端这边看不出任何异常。"""
    for name in viewer_mod.VIEWER_FILES:
        assert "/" not in name and "\\" not in name and ".." not in name, name
        path = viewer_mod.WEB_DIR / name
        if not path.is_file():
            pytest.skip("web/{} 还没落地（由另一份改动提供）".format(name))


def test_the_phone_page_only_uses_the_viewer_asset_prefix():
    path = viewer_mod.WEB_DIR / "viewer.html"
    if not path.is_file():
        pytest.skip("web/viewer.html 还没落地（由另一份改动提供）")
    html = path.read_text(encoding="utf-8")
    assert "/static/" not in html
    for name in ("app.js", "normalize.js", "alerts.js", "follow.js", "style.css"):
        assert name not in html, name


def test_the_help_line_only_states_observations():
    """手机打不开时，程序这边真正知道的只有「没有连接进来」。"""
    assert "只知道没有连接进来" in viewer_mod.NOTE_TROUBLE
    assert "可以依次试" in viewer_mod.NOTE_TROUBLE
    assert_clean(viewer_mod.NOTE_TROUBLE, "NOTE_TROUBLE")
