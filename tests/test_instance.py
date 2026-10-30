"""app/instance.py：双击第二次时认出端口上已经在跑的本程序。

以前 main.py 只看页面开头有没有「直播同传」：界面文字一换成英文（或标题改名），第二次双击就
认不出自己，会再起一个后端。现在认 web/index.html 里与语言无关的 <meta name="tlt-app">，
旧字样也照认——升级时可能还有旧版本的实例在跑，它的页面上没有这个 meta。

认出来以后还要分清是不是「这一份」（2026-10-29）：那天双击 0.19.0 两次，窗口都开到了 agent 起的另一份
副本上（跑的是分支代码、带 --ui-lang zh），副本一关，同一个程序再开就是 0.19.0 自己的英文界面，看起来像
「界面自己变了」；两份的 VERSION 都写 0.19.0，页脚也分不出来。新用户下了新版、旧版还开着时也一样。
现在首页响应带 X-TLT-Install（安装位置的短哈希，不露路径）和 X-TLT-Version，main.py 照旧打开已有实例，
不是同一份时弹一个说明框。
"""
import ast
import asyncio
import re
import unicodedata
import urllib.request
from email.message import Message
from pathlib import Path

import pytest
from aiohttp import web

from app import i18n, instance
from app.i18n import CJK
from app.instance import Found, install_headers, install_id, looks_like_us, other_install_text, \
    peer_version, same_install

REPO = Path(__file__).resolve().parent.parent


def _index_head():
    """main.py 实际读到的样子：页面前 PROBE_BYTES 字节，解码时坏字节替换掉。"""
    return (REPO / "web" / "index.html").read_bytes()[:instance.PROBE_BYTES].decode(errors="replace")


def test_new_fingerprint_works_without_any_chinese():
    page = ('<!DOCTYPE html>\n<html lang="en">\n<head>\n  <meta charset="UTF-8">\n  '
            + instance.META + '\n  <title>TikTok Live Translator</title>')
    assert looks_like_us(page)


def test_an_instance_from_before_the_meta_is_still_recognised():
    page = '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n  <meta charset="UTF-8">\n  <title>TikTok 直播同传</title>'
    assert instance.MARKER not in page
    assert looks_like_us(page)


@pytest.mark.parametrize("page", [
    "",
    None,
    "<!DOCTYPE html><html><head><title>Some Other App</title>",
    '<meta name="tlt-application" content="x">',
    "Not Found",
])
def test_other_pages_are_not_us(page):
    assert not looks_like_us(page)


def test_index_html_carries_the_meta_right_after_charset():
    head = _index_head()
    # 换行不限 LF：Windows 的 Git 默认 core.autocrlf=true，检出的是 CRLF。PROBE_BYTES 仍按原始
    # 字节截：CRLF 下 meta 也得落在 main.py 读的那一段里
    assert re.search(r'<meta charset="UTF-8">\r?\n  ' + re.escape(instance.META), head)
    assert looks_like_us(head)


def test_index_html_is_recognised_by_the_meta_alone():
    """把中文字样拿掉（界面英文化之后的样子），光靠 meta 也认得出。"""
    head = _index_head().replace(instance.LEGACY_MARKER, "Live Translator")
    assert instance.LEGACY_MARKER not in head
    assert looks_like_us(head)


def test_main_probes_with_the_shared_fingerprint():
    """main.py 测不了（import 就 execv）：读语法树，确认探测用的是这里的函数和字节数，
    不再自己写中文字样。"""
    tree = ast.parse((REPO / "main.py").read_text(encoding="utf-8"))
    func = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "_existing_instance_url")
    called = {n.func.id for n in ast.walk(func)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "looks_like_us" in called
    reads = [n for n in ast.walk(func) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "read"]
    assert [getattr(r.args[0], "id", None) for r in reads] == ["PROBE_BYTES"]
    strings = [n.value for n in ast.walk(func)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert not [s for s in strings if instance.LEGACY_MARKER in s]


# ---- 是不是「这一份」：首页响应头（2026-10-29） --------------------------------------------------

def _install(tmp_path, name, version="0.19.0"):
    root = tmp_path / name
    root.mkdir()
    if version is not None:
        (root / "VERSION").write_text(version + "\n", encoding="utf-8")
    return root


def test_install_id_is_a_short_hash_that_does_not_show_the_path(tmp_path):
    root = _install(tmp_path, "tiktok-live-translator-0.19.0")
    got = install_id(root)
    assert re.fullmatch(r"[0-9a-f]{12}", got)
    assert "tiktok" not in got and "0.19" not in got
    assert install_id(root) == got, "同一个文件夹每次算出来都一样"
    assert install_id(str(root)) == got


def test_install_id_follows_the_folder_not_how_the_path_was_spelled(tmp_path):
    root = _install(tmp_path, "app")
    other = _install(tmp_path, "app 2")
    assert install_id(other) != install_id(root), "另一个文件夹（解压出来的第二份）是另一份"
    assert install_id(tmp_path / "app" / ".." / "app") == install_id(root)
    # 大小写不敏感：macOS 默认的卷不分大小写，从终端敲成小写起的也是同一份；
    # 万一真是两个只差大小写的文件夹，算成同一份只是不弹框（照旧打开已有实例），不会说错话
    assert install_id(str(root).upper()) == install_id(str(root).lower())
    link = tmp_path / "shortcut"
    try:
        link.symlink_to(root, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("这台机器建不了符号链接")
    assert install_id(link) == install_id(root), "从软链接进来的还是同一份"


def test_install_id_treats_both_unicode_spellings_of_a_folder_as_one(tmp_path):
    """macOS 的卷也不分 Unicode 的两种写法：「Español」可以是组合形式（é 一个字符，NFC），也可以是分解形式
    （e 加一个附加符号，NFD），打开的是同一个文件夹。从终端敲出来的路径和访达给的路径可能各是一种。按原样算就成了
    两份，第二次打开会说「另一份程序……不是这个文件夹里的这一份」——对同一个文件夹说错话。"""
    nfc, nfd = (unicodedata.normalize(form, "Español") for form in ("NFC", "NFD"))
    assert nfc != nfd
    assert install_id(tmp_path / nfc) == install_id(tmp_path / nfd)
    assert install_id(tmp_path / nfc) != install_id(tmp_path / "Espanol"), "去掉附加符号就是另一个名字"


def test_install_headers_carry_the_id_and_the_version(tmp_path):
    root = _install(tmp_path, "app", "0.20.0")
    assert install_headers(root) == {"X-TLT-Install": install_id(root), "X-TLT-Version": "0.20.0"}
    assert install_headers(_install(tmp_path, "bare", None)) == {"X-TLT-Install": install_id(tmp_path / "bare")}
    (root / "VERSION").write_text("0.20.0\nX-Evil: 1", encoding="utf-8")
    assert "X-TLT-Version" not in install_headers(root), "不像版本号的不发：头里不能夹换行"


def _headers(**kv):
    """main.py 拿到的是 urllib 的 resp.headers（http.client.HTTPMessage，email Message 的子类：键不分大小写）。"""
    msg = Message()
    for k, v in kv.items():
        msg[k.replace("_", "-")] = v
    return msg


def test_same_install_tells_this_copy_another_copy_and_an_older_one_apart(tmp_path):
    root = _install(tmp_path, "app")
    other = _install(tmp_path, "app 2")
    assert same_install(_headers(x_tlt_install=install_id(root)), root) is True
    assert same_install(_headers(X_TLT_Install=install_id(root)), root) is True, "头名不分大小写"
    assert same_install(_headers(x_tlt_install=install_id(other)), root) is False
    assert same_install(install_headers(root), root) is True, "服务端那一份 dict 也认"
    # 加这个头之前的版本（0.19.0 和更早）：不知道，不是「不同」
    assert same_install(_headers(), root) is None
    assert same_install(_headers(x_tlt_install=""), root) is None
    assert same_install(None, root) is None


@pytest.mark.parametrize("raw,want", [
    ("0.19.0", "0.19.0"), (" 0.20.1 ", "0.20.1"), ("v0.20.0", "0.20.0"), ("1.0.0-rc.1+b2", "1.0.0-rc.1+b2"),
    (None, None), ("", None), ("0.19.0 beta", None), ('0.19"', None), ("x" * 33, None), ("零点一九", None),
])
def test_peer_version_only_takes_something_shaped_like_a_version(raw, want):
    headers = _headers() if raw is None else _headers(x_tlt_version=raw)
    assert peer_version(headers) == want
    assert peer_version(None) is None


ACTION_ZH = "要用这个文件夹里的这一份，请先退出正在运行的那一份，再重新打开。"
ACTION_EN = "To use the one in this folder, quit the running copy first, then open this one again."


def test_no_note_when_it_is_this_copy_or_nothing_was_found():
    assert other_install_text(None) is None
    assert other_install_text(Found("http://127.0.0.1:8765", True, "0.20.0")) is None


@pytest.mark.parametrize("found,zh,en", [
    (Found("http://127.0.0.1:8766", False, "0.19.0"),
     "另一份程序（v0.19.0，不是这个文件夹里的这一份）已经在运行，这次打开的是它。\n" + ACTION_ZH,
     "Another copy of the app (v0.19.0, not the one in this folder) is already running, "
     "so that copy opened instead.\n" + ACTION_EN),
    (Found("http://127.0.0.1:8766", False, None),
     "另一份程序（不是这个文件夹里的这一份）已经在运行，这次打开的是它。\n" + ACTION_ZH,
     "Another copy of the app (not the one in this folder) is already running, "
     "so that copy opened instead.\n" + ACTION_EN),
    (Found("http://127.0.0.1:8765", None, None),
     "程序已经在运行，但它没有说自己是哪个文件夹里的那一份，这次打开的是它。\n" + ACTION_ZH,
     "The app is already running but didn’t report which folder it’s from, "
     "so that copy opened instead.\n" + ACTION_EN),
])
def test_the_note_says_only_what_was_seen_in_both_languages(found, zh, en):
    note = other_install_text(found)
    assert i18n.text(note, "zh") == zh and type(i18n.text(note, "zh")) is str
    assert i18n.text(note, "en") == en and type(i18n.text(note, "en")) is str
    assert not CJK.search(i18n.text(note, "en"))
    assert str(note) == zh, "print（终端）永远是中文"


def test_an_unknown_peer_does_not_get_a_version_even_if_it_sent_one():
    """没带 X-TLT-Install 的不知道是不是这一份：不拿它的版本号说「另一份 vX」。"""
    got, plain = other_install_text(Found("u", None, "0.19.0")), other_install_text(Found("u", None, None))
    for lang in ("zh", "en"):
        assert i18n.text(got, lang) == i18n.text(plain, lang)


# ---- 真起一个服务端，照 main.py 的写法去探：头真的穿过了 HTTP ----------------------------------------

def _probe(port):
    """main.py _existing_instance_url 里那几行：只读 PROBE_BYTES，返回 (认不认得, 响应头)。"""
    with urllib.request.urlopen("http://127.0.0.1:{}".format(port), timeout=5) as resp:
        return looks_like_us(resp.read(instance.PROBE_BYTES).decode(errors="replace")), resp.headers


async def _serve_and_probe(app):
    from aiohttp.test_utils import TestServer
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    try:
        return await asyncio.get_running_loop().run_in_executor(None, _probe, server.port)
    finally:
        await server.close()


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_the_real_index_page_answers_with_the_install_headers(lang, tmp_path):
    from app.server import CaptionServer
    with i18n.use(lang):
        ours, headers = asyncio.run(_serve_and_probe(CaptionServer(port=0).build_app()))
    assert ours
    assert same_install(headers, REPO) is True, "同一个仓库：就是这一份"
    assert same_install(headers, _install(tmp_path, "other")) is False
    assert peer_version(headers) == (REPO / "VERSION").read_text(encoding="utf-8").strip()


def test_an_instance_from_before_the_headers_is_recognised_but_unknown():
    """0.19.0 和更早的首页没有这两个头：照旧认得出是本程序，是不是这一份是 None。"""
    async def old_index(request):
        return web.FileResponse(REPO / "web" / "index.html")
    app = web.Application()
    app.router.add_get("/", old_index)
    ours, headers = asyncio.run(_serve_and_probe(app))
    assert ours and same_install(headers, REPO) is None and peer_version(headers) is None


def test_main_reports_which_copy_it_found():
    """_existing_instance_url 返回 Found(地址, 是不是这一份, 对方版本)：same_install 拿的是响应头和 ROOT。
    其余几处只当真假用（端口上有没有本程序），Found 是非空元组，照旧为真。"""
    tree = ast.parse((REPO / "main.py").read_text(encoding="utf-8"))
    func = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "_existing_instance_url")
    (found,) = [n for n in ast.walk(func) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name) and n.func.id == "Found"]
    assert ast.unparse(found.args[0]) == "url"
    assert ast.unparse(found.args[1]) == "same_install(resp.headers, ROOT)"
    assert ast.unparse(found.args[2]) == "peer_version(resp.headers)"
    assert Found("u", None, None), "没带头的已有实例也得是真值：ensure_env、defer_install 当真假用"
