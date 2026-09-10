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


# ffmpeg 滤镜串解析分两级：先按 `[ ] , ;` 拆滤镜图，再按 `:` 拆每个滤镜的
# 选项——两级都用 av_get_token，各吃掉一层反斜杠。所以 `:` 要写成 `\\:`
# （一级留下 `\:`，二级还原成 `:`），只写一层 `\:` 会在二级被当分隔符，
# 实测报 "No option name near 'b/x.rnnn'"（2026-09-09 用 -f lavfi 复现）。
_OPTION_SPECIAL = re.compile(r"([:'\\])")        # 选项级：av_opt_set_from_string
_GRAPH_SPECIAL = re.compile(r"([\[\],;'\\])")    # 滤镜图级：avfilter_graph_parse


def filter_path(path):
    """把文件路径写进 ffmpeg 滤镜参数（如 arnndn=m=…）需要的转义形态。

    Windows 路径 `C:\\Users\\x\\bd.rnnn` 里的冒号会被 ffmpeg 当成选项分隔符；
    反斜杠先统一成正斜杠（ffmpeg 在 Windows 上照样认），再做两级转义：
    选项级先把 `:` `'` `\\` 各加一个反斜杠，滤镜图级再把结果里的
    `[ ] , ; ' \\` 各加一个。`C:/x` 最终写成 `C\\\\:/x`。"""
    p = str(path).replace("\\", "/")
    p = _OPTION_SPECIAL.sub(r"\\\1", p)
    return _GRAPH_SPECIAL.sub(r"\\\1", p)
