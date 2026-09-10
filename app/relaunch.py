"""原地重启进程时的参数处理。

Windows 上 `os.execv` 只是把 argv 用空格拼成一条命令行交给 CRT，**不加引号**：
项目放在 `C:\\Users\\Zhang San\\…` 时新进程收到的 argv[1] 是 `San\\…\\python.exe`，
报 "can't open file" 退出——首次安装后换进 .venv、一键更新后的重启都会失败。
POSIX 的 execv 按数组传参，不需要也不能加引号。
"""
import os
import subprocess


def exec_args(argv):
    """给 os.execv 用的 argv：Windows 逐个按 CRT 规则加引号，其它平台原样。"""
    if os.name != "nt":
        return list(argv)
    return [subprocess.list2cmdline([a]) for a in argv]
