"""界面用词统一（专家审查 V5，2026-10-26；术语见 docs/i18n-style.md §1.3、§3）。

这几道是 grep 闸，但只查**界面字段**：全仓的中英对（tools/i18n_pairs.py 抽出来的，Python 的 L()/LN()、
JS 的 L()/LN()、HTML 的 data-en*），不做全文替换。终端那行 print("[警报] …")（app/pipeline.py）不是对，
是终端前缀——终端永远中文、不在统一范围内；它和钉着它的测试（tests/i18n_rules.py 的 TERMINAL_PREFIXES、
tests/test_alert_mode.py、tests/test_stdio_encoding.py）照旧，最后一条用例确认它还在。
"""
import re

from app import i18n
from app import viewer
from tests import i18n_rules as rules
from tools import i18n_pairs as P


def _all_pairs():
    pairs = []
    for path in P.source_files():
        pairs.extend(P.pairs_of(path, P.read(path)))
    return pairs


PAIRS = _all_pairs()


def _where(p):
    return "{}:{} {!r}".format(p.path, p.line, p.zh)


def test_the_check_actually_sees_the_ui_pairs():
    # 抽对坏了（0 个对）时下面几条会静默变绿：先确认看得到几处已知的界面字段
    zh = {p.zh for p in PAIRS}
    assert len(PAIRS) > 1000
    assert "违禁词报警" in zh and "清空报警" in zh and "精确" in zh


def test_alert_has_one_word_on_the_ui():
    # 界面上「报警」是唯一的说法：以前桌面有「清空警报」「红色警报」，手机有「违禁词警示已关闭」
    bad = [_where(p) for p in PAIRS if re.search("警示|警报", p.zh)]
    assert not bad, "\n".join(bad)


def test_no_arming_jargon_on_the_ui():
    # 「武装」是代码里两段式确认的叫法（web/switch.js），不是给中控看的词
    bad = [_where(p) for p in PAIRS if "武装" in p.zh]
    assert not bad, "\n".join(bad)


def test_the_translation_engine_row_is_named_one_way():
    # 提示里指「翻译引擎」这一行在哪，一律写「在「设置 > 翻译引擎」里」（英文本来就是 Settings > Translation Engine）。
    # 以前另有「在「翻译引擎」里」「在页面上的「翻译引擎」里」「在页面的「翻译引擎」里」「在「翻译引擎」面板」四种。
    # 唯一的例外是启动自检里的那一项：「见首页自检的「翻译引擎」一项」说的是自检列表的一行，不是设置里那一行
    bad = []
    for p in PAIRS:
        for m in re.finditer("「翻译引擎」", p.zh):
            if not p.zh[:m.start()].endswith("自检的"):
                bad.append(_where(p))
        if re.search("在页面上?的「|「翻译引擎」面板", p.zh):
            bad.append(_where(p))
    assert not bad, "\n".join(bad)
    unified = [p for p in PAIRS if "「设置 > 翻译引擎」" in p.zh]
    assert len(unified) >= 15, len(unified)
    for p in unified:
        assert all("Settings > Translation Engine" in en for en in p.ens), _where(p)


def test_the_exact_tier_is_called_jingque_on_both_pages():
    # 分级：精确 / 变体 / 疑似（docs/i18n-style.md §1.3）。「命中」留给 match，不当分级名
    tiers = [p for p in PAIRS if p.ens == ["Exact"]]
    assert {p.path for p in tiers} >= {"web/live-ui.js", "web/viewer.js"}
    assert {p.zh for p in tiers} == {"精确"}, [_where(p) for p in tiers]
    assert not [_where(p) for p in PAIRS if p.zh == "命中"]


def test_no_lan_address_note_only_says_what_was_seen_and_what_to_do():
    # 以前叫中控在手机上手工输入 http://<本机地址>:{port}/#k=<没显示的钥匙>：这时载荷里没有口令，照着做做不成
    zh, en = i18n.render(viewer.NOTE_NO_IP, i18n.ZH), i18n.render(viewer.NOTE_NO_IP, i18n.EN)
    for text in (zh, en):
        assert "#k=" not in text and "<" not in text and "{" not in text, text
    assert zh == ("已打开，但没读到本机的局域网地址（只读到回环地址），所以没出二维码。"
                  "程序每分钟重读一次地址：把这台电脑连上手机所在的 Wi-Fi，读到地址后二维码就会出来；"
                  "想马上重读，点「停止同看」再点「开始同看」。")
    assert en == ("On, but the app couldn’t find this computer’s local network address (only the "
                  "loopback address), so there’s no QR code. The app checks the address again every "
                  "minute. Connect this computer to the phones’ Wi-Fi, and the QR code appears once "
                  "the address is found. To check right away, click Stop Sharing, then Start Sharing.")
    # 「每分钟」说的是同看开着时重读地址的间隔（app/viewer_share.py _viewer_ip_watch）
    assert viewer.IP_REFRESH_SEC == 60
    assert viewer.share_note(True, port=8766, url=None).startswith(zh)


def test_the_terminal_alert_line_is_left_alone():
    assert "[警报]" in rules.TERMINAL_PREFIXES
    assert 'print("[警报] ' in P.read("app/pipeline.py")
