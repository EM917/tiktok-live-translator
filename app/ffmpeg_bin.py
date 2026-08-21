"""ffmpeg 二进制定位：优先系统安装的，其次 pip 包 imageio-ffmpeg 自带的静态版。

这样用户不需要手动装 ffmpeg——`pip install -r requirements.txt` 就把一切备齐；
系统已有 ffmpeg 时仍优先使用（通常更新、启动更快）。
"""
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
