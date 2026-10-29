"""写进审计日志、运行日志或界面之前，把文本里的凭证拿掉。

TikTok 的拉流地址把 sign/expire 放在 query 里，拿着它两周内就能拉流。ffmpeg、yt-dlp
和 aiohttp 的报错会原样带上输入地址，所以任何要落盘的错误文本、stderr 尾巴、
traceback 都先过这里：URL 只留「协议://主机/路径」，query 和 fragment 一律去掉。

运行日志（app/runlog.py）抄的是终端里的每一行，什么都可能带进来，所以另有一道
scrub_secrets：在 strip_query 之外，再按名字和形状抹掉 API 密钥、cookie、手机同看口令、
闪购助手的控制密钥。
"""
import re

# 从协议头一直到空白或引号为止算一个地址；query/fragment 从第一个 ? 或 # 开始丢
# 协议名不限（rtmp、wss、s3 签名地址……）：asr 与 evidence 两组原先在 audit.py 各写了一份
# 规则略有出入，合并时统一到这里，只留这一处
_URL_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s?#\"'<>|]*)(?:[?#][^\s\"'<>|]*)?")


def strip_query(text, limit=None):
    """去掉文本里所有 URL 的 query 和 fragment；limit 给定时截到这么多字符。

    None 返回空串，非字符串先 str()。先去 query 再截断：反过来可能把半截签名留下。"""
    if text is None:
        return ""
    cleaned = _URL_RE.sub(lambda m: m.group(1), str(text))
    if limit is not None and len(cleaned) > limit:
        cleaned = cleaned[:limit]
    return cleaned


# ---- scrub_secrets：运行日志落盘之前 ---------------------------------------------------------
#
# 抹掉的地方换成 MASK，不留尾几位（界面上的 mask_key 留尾四位是给人认是哪一把，日志用不着）。
# 规则分三层，顺序有讲究：先按「整段值都是凭证」的请求头抹到引号或行尾，再按键名抹一个值，最后按形状
# 兜底——键名没写出来的（异常原文、列表里的裸值）靠形状。MASK 本身不会被任何一条再匹配上，所以抹两遍
# 和抹一遍一样。
MASK = "<已抹去>"  # i18n: terminal

# URL 里的「用户名:密码@」（代理地址会带）。strip_query 只去 query，不管这一段
_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/?#@\"'<>|]+@")

# 整个值都是凭证的请求头：Authorization: Bearer …、Cookie: a=b; c=d。值是引号里的一段（字典、JSON 的
# 写法，引号前可以带反斜杠），否则一直抹到行尾——值里本来就有空格和分号，按词切会漏后半截
_HEADER_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])(\\?[\"']?)(proxy-authorization|authorization|set-cookie|cookie)\1"
    r"(\s*[:=]\s*)(?:(\\?[\"'])(?:(?!\4).)*\4|[^\r\n]+)")

# 键名是凭证的「键 = 值」「键: 值」「'键': '值'」。只认整个名字：max_tokens、token_cap、cookies_browser
# 都不算。TikTok 那几个是登录和设备 cookie 的名字（app/browser_login.LOGIN_COOKIE_NAMES 是其中一部分）
_KEY_NAMES = (
    r"[A-Za-z0-9_-]*api[_-]?keys?",                 # api_key、DEEPL_API_KEY、x-api-key、apikey
    r"auth[_-]?key",
    r"(?:access|refresh|id|auth|bearer|csrf|session|viewer|control)[_-]?token",
    r"token", r"secret", r"client[_-]?secret", r"password", r"passwd",
    r"session[_-]?id",
    r"sessionid(?:_ss)?", r"sid_tt", r"sid_guard", r"uid_tt(?:_ss)?", r"s?sid_ucp_v1",
    r"odin_tt", r"mstoken", r"ttwid", r"tt_csrf_token", r"passport_csrf_token(?:_default)?",
    r"tt_chain_token", r"cmpl_token", r"multi_sids", r"s_v_web_id", r"csrf_session_id",
)
_KV_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])(\\?[\"']?)(" + "|".join(_KEY_NAMES) + r")\1(\s*[:=]\s*)"  # i18n: data
    r"(?:(\\?[\"'])(?:(?!\4).)*\4|[^\s;&,\"'<>()\[\]{}]+)")

# 带认证方案名的值：Bearer <闪购助手的控制密钥>、DeepL-Auth-Key <DeepL 密钥>
_SCHEME_RE = re.compile(r"(?i)\b(bearer|deepl-auth-key)(\s+)[A-Za-z0-9._~+/=:-]{6,}")

# 形状兜底。每一条两头都要求「不是同类字符」，免得把长一点的词、路径切一段出来
_SHAPES = (
    # Anthropic（sk-ant-…）、OpenAI（sk-…、sk-proj-…）
    re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{16,}"),
    # Google API 密钥
    re.compile(r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{35}(?![A-Za-z0-9_-])"),
    # GitHub 令牌
    re.compile(r"(?<![A-Za-z0-9_-])(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,})"),
    # JWT
    re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    # DeepL 密钥是 UUID 的样子，免费版带 :fx。本程序自己不产生 UUID，整类抹掉代价很小
    re.compile(r"(?i)(?<![0-9a-z-])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
               r"(?::fx)?(?![0-9a-z-])"),
    # 闪购助手的控制密钥：64 位十六进制（sha256 的十六进制摘要同形，一起抹掉）
    re.compile(r"(?i)(?<![0-9a-z])[0-9a-f]{64}(?![0-9a-z])"),
    # TikTok 的 sessionid、sid_tt 这类：32 位小写十六进制。40 位的 git 提交号不在内
    re.compile(r"(?<![0-9A-Za-z])[0-9a-f]{32}(?![0-9A-Za-z])"),
    # 手机同看口令：secrets.token_urlsafe(32)，正好 43 个 URL 安全字符，大小写字母都有
    re.compile(r"(?<![A-Za-z0-9_-])(?=[A-Za-z0-9_-]{0,42}[A-Z])(?=[A-Za-z0-9_-]{0,42}[a-z])"
               r"[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])"),
    # 其余随机串（msToken 这类）：32 个字符以上、大写小写数字都有、中间没有 / 和 .——路径、模型名、
    # 版本号、snake_case 的函数名都过得去
    re.compile(r"(?<![A-Za-z0-9_+=-])(?=[A-Za-z0-9_+=-]*[A-Z])(?=[A-Za-z0-9_+=-]*[a-z])"
               r"(?=[A-Za-z0-9_+=-]*[0-9])[A-Za-z0-9_+=-]{32,}(?![A-Za-z0-9_+=-])"),
)


def _mask_value(m):
    quote, name, sep, vquote = m.group(1), m.group(2), m.group(3), m.group(4)
    value = vquote + MASK + vquote if vquote else MASK
    return quote + name + quote + sep + value


def scrub_secrets(text):
    """运行日志落盘之前：URL 只留到路径（strip_query），再按请求头、键名、形状抹掉密钥、cookie、口令。

    None 返回空串，非字符串先 str()。永不抛：规则都是写死的正则，出错只会是编程错误。"""
    out = strip_query(text)
    out = _USERINFO_RE.sub(lambda m: m.group(1) + MASK + "@", out)
    out = _HEADER_RE.sub(_mask_value, out)
    out = _KV_RE.sub(_mask_value, out)
    out = _SCHEME_RE.sub(lambda m: m.group(1) + m.group(2) + MASK, out)
    for shape in _SHAPES:
        out = shape.sub(MASK, out)
    return out
