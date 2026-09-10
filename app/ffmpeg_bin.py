"""ffmpeg 二进制定位：优先系统安装的，其次 pip 包 imageio-ffmpeg 自带的静态版。

这样用户不需要手动装 ffmpeg——`pip install -r requirements.txt` 就把一切备齐；
系统已有 ffmpeg 时仍优先使用（通常更新、启动更快）。
"""
import re
import shutil

_cached = None
_resolved = False


def find_ffmpeg():
    """返回可用的 ffmpeg 可执行文件路径；找不到返回 None。结果缓存。"""
    global _cached, _resolved
    if _resolved:
        return _cached
    exe = shutil.which("ffmpeg")
    if exe is None:
        try:
            import imageio_ffmpeg

            exe = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            exe = None
    _cached = exe
    _resolved = True
    return exe


def ffmpeg_source():
    """描述当前 ffmpeg 来源，用于 doctor 展示。"""
    exe = find_ffmpeg()
    if exe is None:
        return None
    return "系统" if shutil.which("ffmpeg") else "内置（imageio-ffmpeg）"


_FILTER_SPECIAL = re.compile(r"([:,'\[\];\\])")


def filter_path(path):
    """把文件路径写进 ffmpeg 滤镜参数（如 arnndn=m=…）时的转义。

    滤镜图里 ':' 是选项分隔、',' 是滤镜分隔、'\\' 是转义符。Windows 的
    C:\\Users\\… 原样塞进去，本机 ffmpeg 9 实测报 "No option name near
    'Userselon…'"——反斜杠被吃、冒号处截断——于是降噪探测每场必失败、被当成
    「模型损坏」，Windows 用户的降噪 100% 起不来。先把反斜杠换成 '/'（ffmpeg
    在 Windows 上照样能开），再给特殊字符加反斜杠：实测 'm=/tmp/a\\:b/x.rnnn'
    能正确解析出 /tmp/a:b/x.rnnn。"""
    p = str(path).replace("\\", "/")
    return _FILTER_SPECIAL.sub(r"\\\1", p)
