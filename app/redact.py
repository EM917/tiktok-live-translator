"""写进审计日志或界面之前，把文本里的凭证拿掉。

TikTok 的拉流地址把 sign/expire 放在 query 里，拿着它两周内就能拉流。ffmpeg、yt-dlp
和 aiohttp 的报错会原样带上输入地址，所以任何要落盘的错误文本、stderr 尾巴、
traceback 都先过这里：URL 只留「协议://主机/路径」，query 和 fragment 一律去掉。
"""
import re

# 从协议头一直到空白或引号为止算一个地址；query/fragment 从第一个 ? 或 # 开始丢
_URL_RE = re.compile(r"(?i)\b((?:https?|rtmps?|wss?)://[^\s?#\"'<>|]*)(?:[?#][^\s\"'<>|]*)?")


def strip_query(text, limit=None):
    """去掉文本里所有 URL 的 query 和 fragment；limit 给定时截到这么多字符。

    None 返回空串，非字符串先 str()。先去 query 再截断：反过来可能把半截签名留下。"""
    if text is None:
        return ""
    cleaned = _URL_RE.sub(lambda m: m.group(1), str(text))
    if limit is not None and len(cleaned) > limit:
        cleaned = cleaned[:limit]
    return cleaned
