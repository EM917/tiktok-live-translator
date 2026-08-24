"""HTTP 读取小工具。

单独放一个模块是因为同一个 bug 在这个仓库里犯过两次：
`await resp.content.read(n)` **不保证**读满 n 字节，它只返回缓冲区里当前
已有的数据。第一次是降噪模型下载，拿到 8MB 上限里的一小截，校验长度不过，
程序每次启动都静默退回「不降噪」；第二次是直播页兜底解析，213KB 的页面只读到
85KB，流地址正好在被截掉的部分，于是每个直播间都解析失败。

两次的表面症状完全不同，根因是同一个。所以收敛到这里，谁要读整个响应体都用它。
"""


async def read_all(resp, limit):
    """把响应体读到 EOF，最多 limit 字节。超过 limit 返回 None。

    limit 是防止异常/恶意响应撑爆内存的上限，不是「读这么多就够了」。
    """
    data = b""
    while True:
        chunk = await resp.content.read(64 * 1024)
        if not chunk:
            return data
        data += chunk
        if len(data) > limit:
            return None
