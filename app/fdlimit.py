"""文件句柄上限（RLIMIT_NOFILE）：进程一启动就把软上限提到 min(硬上限, 8192)。

为什么（专家审查 S1，2026-10-21）：从 Finder 双击启动的 .app 继承 launchd 的 maxfiles，
本机软上限只有 256。文件句柄是打开的文件、网络连接、子进程管道共用的一个名额池——
局域网里一台设备对手机同看端口挂 150 条空连接，之后新开的东西就全部 EMFILE：用程序自己
的解释器实测 open、spawn、socket 全部 ERR24（证据 wav、ffmpeg 断线重连、翻译的新连接都
属于「之后新开的」）。8192 把这道门槛抬高 30 多倍；连接本身的上限另在 app/viewer.py
（每个 IP 16 条、连上 5 秒不发一个字节就断开）。两道一起才算数：只抬上限，攻击者多开
几条就又到顶了；只限连接，launchd 的 256 对程序自己正常的峰值也偏紧。

约定：
  * 只抬不降。软上限已经不低于目标（终端里常见 ulimit -n 很大）就原样不动；
  * 硬上限是天花板，不碰它（普通用户也抬不了硬上限）；硬上限是无穷大时用 8192；
  * 没有 resource 模块（Windows）就跳过——那边没有这个上限；
  * 任何错误都不抛，只放进返回值：启动路径上不许因为这一步失败。

这个模块不含中文字面量、自己不打印：结果交给调用方（main.py），由它用中文写终端警告
（CLAUDE.md 第九条：终端永远是中文，界面文字才成对写）。
"""
import collections

TARGET = 8192

# status 取值：
#   raised       软上限抬上去了（soft_after 是重新读回来的值）
#   unchanged    已经够高、或者硬上限就是这么低，没有可做的
#   unsupported  这个平台没有 resource 模块
#   failed       读或写上限时系统报错，error 是系统给的原话
NofileResult = collections.namedtuple(
    "NofileResult", "status soft_before soft_after hard target error")


def _describe(exc):
    text = str(exc).strip()
    return "{}: {}".format(type(exc).__name__, text) if text else type(exc).__name__


def _finite(value, infinity):
    """RLIM_INFINITY 在 macOS 上是 2**63-1，在 Linux 的 Python 里读出来是 -1：两种都当无穷大。"""
    return isinstance(value, int) and value != infinity and value >= 0


def raise_nofile_limit(target=TARGET, resource_mod=None):
    """把 RLIMIT_NOFILE 的软上限提到 min(硬上限, target)。返回 NofileResult，永不抛。

    resource_mod 只给测试注入替身；生产里不传，按需 import（Windows 上 import 失败即跳过）。
    """
    if resource_mod is None:
        try:
            import resource as resource_mod
        except ImportError:
            return NofileResult("unsupported", None, None, None, target, None)
    try:
        which = resource_mod.RLIMIT_NOFILE
        infinity = getattr(resource_mod, "RLIM_INFINITY", -1)
        soft, hard = resource_mod.getrlimit(which)
    except Exception as exc:
        return NofileResult("failed", None, None, None, target, _describe(exc))
    want = min(hard, target) if _finite(hard, infinity) else target
    if not _finite(soft, infinity) or soft >= want:
        return NofileResult("unchanged", soft, soft, hard, want, None)
    try:
        resource_mod.setrlimit(which, (want, hard))
    except Exception as exc:
        # macOS 上软上限高过 kern.maxfilesperproc 时 setrlimit 报 EINVAL（Python 里是 ValueError）；
        # 8192 低于各版本 macOS 的默认值，真碰上了也只报回去，不重试别的数
        return NofileResult("failed", soft, soft, hard, want, _describe(exc))
    try:
        after = resource_mod.getrlimit(which)[0]
    except Exception:
        after = want
    return NofileResult("raised", soft, after, hard, want, None)
