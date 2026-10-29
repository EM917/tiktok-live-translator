# i18n: done
"""认出端口上跑的是不是本程序：双击第二次时复用已有实例，而不是再起一个后端（main.py）。

以前的指纹是页面开头有没有「直播同传」——那是界面上的中文字，界面换成英文、或者标题改了名，
第二次双击就认不出自己，会再起一个后端。现在 web/index.html 在 <meta charset> 后面紧跟一个
与界面语言无关的 <meta name="tlt-app">。旧字样照认：升级时可能还有一个旧版本的实例在跑，
它的页面上没有这个 meta。

认出来是本程序，还要分清是不是「这一份」（2026-10-29）：那天双击 0.19.0 两次，窗口都被开到了另一份
副本上（另一个文件夹、跑的是分支代码），那一份一关，同一个程序再开就是 0.19.0 自己的后端，界面换了一种
语言，看起来像「界面自己变了」；两份的 VERSION 都写 0.19.0，页脚也分不出来。新用户下了新版、旧版还开着时，
双击新版同样会落到旧版上。现在首页响应带两个头（app/server.py _index）：X-TLT-Install 是安装位置的
短哈希（不露路径），X-TLT-Version 是 VERSION 文件里的版本号。main.py 照旧打开已有实例，不是同一份时
弹一个说明框（other_install_text）。

只依赖标准库：main.py 在自举期（还在系统 Python 里）就 import 它。
"""
import hashlib
import os
import re
import unicodedata
from collections import namedtuple
from pathlib import Path

from .i18n import L

PROBE_BYTES = 4096        # main.py 只读页面开头这么多字节；meta 必须落在里面
META = '<meta name="tlt-app" content="tiktok-live-translator">'
MARKER = 'name="tlt-app"'
LEGACY_MARKER = "直播同传"  # i18n: data

INSTALL_HEADER = "X-TLT-Install"
VERSION_HEADER = "X-TLT-Version"
# 版本号的样子：VERSION 文件里写的是 0.19.0 这种；预发布、构建号带 - + 也认。别的（带空格、引号、换行、
# 太长）一律当没有：它要进响应头，也要进对话框
_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,31}")

# main.py _existing_instance_url 认出来的已有实例。same：True 是这个文件夹里的这一份，False 是另一份，
# None 是它没说（加这两个头之前的版本，0.19.0 和更早）。version：对方的版本号，没说或不像版本号时为 None。
# 非空元组，永远为真：ensure_env、defer_install 那几处只拿它当「端口上有没有本程序」用
Found = namedtuple("Found", "url same version")


def looks_like_us(html):
    """html：页面开头（已解码）。新旧两种指纹认任一个。"""
    text = html or ""
    return MARKER in text or LEGACY_MARKER in text


def install_id(root):
    """安装位置（程序文件夹）的短哈希，12 位十六进制。不露路径。

    按真实路径算：从软链接、带 .. 的路径进来都是同一份。再统一成小写：macOS 默认的卷不分大小写，
    从终端敲成小写起的那一份和从访达双击起的是同一个文件夹。真有两个只差大小写的文件夹（分大小写的卷上），
    算成同一份的后果只是不弹框、照旧打开已有实例，不会说错话。
    Unicode 的两种写法同理：macOS 的卷也不分组合形式（NFC，é 一个字符）和分解形式（NFD，e 加附加符号），
    从终端敲的路径和访达给的路径可能各是一种，所以先统一成 NFC 再小写。"""
    key = unicodedata.normalize("NFC", os.path.normcase(os.path.realpath(str(root)))).casefold()
    digest = hashlib.sha256(b"tlt-install\0" + key.encode("utf-8", "surrogateescape"))
    return digest.hexdigest()[:12]


def clean_version(value):
    """像版本号就返回它（去掉开头的 v），否则 None。"""
    text = str(value or "").strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    return text if _VERSION.fullmatch(text) else None


def install_headers(root):
    """首页响应要带的头（app/server.py）：安装位置的短哈希，加上 VERSION 里的版本号（读不到、不像版本号就不带）。"""
    headers = {INSTALL_HEADER: install_id(root)}
    try:
        version = clean_version((Path(root) / "VERSION").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        version = None
    if version:
        headers[VERSION_HEADER] = version
    return headers


def same_install(headers, root):
    """端口上那个实例是不是 root 这一份。headers 是它首页的响应头（urllib 的 resp.headers，键不分大小写；
    dict 也行）。True / False；它没带 X-TLT-Install（加这个头之前的版本）时为 None：不知道，不算「不同」。"""
    got = headers.get(INSTALL_HEADER) if headers is not None else None
    if not got:
        return None
    return str(got).strip() == install_id(root)


def peer_version(headers):
    """它报的版本号（X-TLT-Version），不像版本号就当没有。"""
    return clean_version(headers.get(VERSION_HEADER)) if headers is not None else None


_USE_THIS_ONE = L("要用这个文件夹里的这一份，请先退出正在运行的那一份，再重新打开。",
                  "To use the one in this folder, quit the running copy first, then open this one again.")


def other_install_text(found):
    """双击打开、端口上已经有本程序时：不是这一份就返回说明框的文字（成对文案），是这一份或没找到返回 None。

    只写观察到的：它说了自己不是这一份（还有它的版本号），或者它没说自己是哪一份。它为什么开着、是哪个
    版本更新，都不猜。没说是哪一份的（加这个头之前的版本），它报的版本号也不拿来用。"""
    if found is None or found.same is True:
        return None
    if found.same is False and found.version:
        first = L("另一份程序（v{ver}，不是这个文件夹里的这一份）已经在运行，这次打开的是它。",
                  "Another copy of the app (v{ver}, not the one in this folder) is already running, "
                  "so that copy opened instead.").format(ver=found.version)
    elif found.same is False:
        first = L("另一份程序（不是这个文件夹里的这一份）已经在运行，这次打开的是它。",
                  "Another copy of the app (not the one in this folder) is already running, "
                  "so that copy opened instead.")
    else:
        first = L("程序已经在运行，但它没有说自己是哪个文件夹里的那一份，这次打开的是它。",
                  "The app is already running but didn’t report which folder it’s from, "
                  "so that copy opened instead.")
    return first + "\n" + _USE_THIS_ONE
