"""认出端口上跑的是不是本程序：双击第二次时复用已有实例，而不是再起一个后端（main.py）。

以前的指纹是页面开头有没有「直播同传」——那是界面上的中文字，界面换成英文、或者标题改了名，
第二次双击就认不出自己，会再起一个后端。现在 web/index.html 在 <meta charset> 后面紧跟一个
与界面语言无关的 <meta name="tlt-app">。旧字样照认：升级时可能还有一个旧版本的实例在跑，
它的页面上没有这个 meta。
"""

PROBE_BYTES = 4096        # main.py 只读页面开头这么多字节；meta 必须落在里面
META = '<meta name="tlt-app" content="tiktok-live-translator">'
MARKER = 'name="tlt-app"'
LEGACY_MARKER = "直播同传"  # i18n: data


def looks_like_us(html):
    """html：页面开头（已解码）。新旧两种指纹认任一个。"""
    text = html or ""
    return MARKER in text or LEGACY_MARKER in text
