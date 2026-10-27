"""原地重启进程、换进 .venv 时的参数处理与换进程的办法。

Windows 上 `os.execv` 只是把 argv 用空格拼成一条命令行交给 CRT，**不加引号**：
项目放在 `C:\\Users\\Zhang San\\…` 时新进程收到的 argv[1] 是 `San\\…\\python.exe`，
报 "can't open file" 退出——首次安装后换进 .venv、一键更新后的重启都会失败。
POSIX 的 execv 按数组传参，不需要也不能加引号。

Windows 上的 execv 还有一处（I11，2026-10-21 审查）：CRT 的 execv 是「起一个新进程、自己立刻
退出」，Start.bat 等的那个进程于是马上以 0 退出，「出错就 pause」永远轮不到——第二跳以后的
致命错误随黑窗口一闪而过。所以 ensure_env 换进 .venv 那一跳在 Windows 上改用 replace_process：
起子进程、等它结束、带着它的退出码退出。POSIX 上 main.py 照旧自己 execv（和 replace_process 的
POSIX 分支一样）：每一处重启都回到 main.py 从头跑、护栏在新进程里再装一遍，这几处 exec 由
tests/test_stdio_encoding.py 在 main.py、updater、macbundle 里数着。一键更新的重启仍然用 execv：
那时要把端口放开，旧进程不能留着。
"""
import os
import subprocess
import sys


def exec_args(argv):
    """给 os.execv 用的 argv：Windows 逐个按 CRT 规则加引号，其它平台原样。"""
    if os.name != "nt":
        return list(argv)
    return [subprocess.list2cmdline([a]) for a in argv]


def _exit_status(code):
    """子进程的退出码换成 os._exit 收得下的数。Windows 的退出码是 32 位无符号数（按 Ctrl-C
    关掉是 0xC000013A），os._exit 只收有符号的 int：按同一个 32 位值换成负数，Start.bat 看到的
    还是原来那个码。拿不到退出码（None）按 1 算：总之不是正常结束。"""
    if not isinstance(code, int) or isinstance(code, bool):
        return 1
    if code > 0x7FFFFFFF:
        code -= 1 << 32
    return code


def flush_std():
    """把 Python 自己缓冲着的标准输出、标准错误写出去。exec 换掉进程映像时不会替它冲：stdout 不是
    终端（`| tee`、重定向到文件）时，exec 之前打的几行会整段丢掉。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.flush()
        except Exception:
            pass


def replace_process(argv, env=None, os_name=None, execv=None, execve=None,
                    popen=subprocess.Popen, exit_=None):
    """换成 argv 这个程序接着跑，不再回来。

    POSIX：os.execv（给了 env 就 os.execve），进程号不变。
    Windows：起子进程、一直等它结束，然后带着它的退出码 os._exit——Start.bat 拿到的是真正跑
    程序的那个进程的退出码，出错时能停在 pause 上。等的时候按了 Ctrl-C：控制台把它同时送给
    父子两个进程，子进程自己收尾（main.py 里存证据再退出），父进程不能像 subprocess.call 那样
    把它杀掉，只接着等。起不来（OSError）照常抛给调用方。

    execv/execve/popen/exit_ 只给测试换成替身用。"""
    argv = [str(a) for a in argv]
    if (os_name or os.name) != "nt":
        if env is None:
            (execv or os.execv)(argv[0], exec_args(argv))
        else:
            (execve or os.execve)(argv[0], argv, env)
        return            # 真的 exec 不会回到这里；给替身用
    flush_std()
    proc = popen(argv, env=env)
    while True:
        try:
            code = proc.wait()
            break
        except KeyboardInterrupt:
            continue
    flush_std()
    (exit_ or os._exit)(_exit_status(code))

