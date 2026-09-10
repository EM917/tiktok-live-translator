"""首次安装的进程间锁（main.py 的 bootstrap 用；只依赖标准库）。

首次安装要几分钟且窗口模式下毫无提示，用户很容易再双击一次。用锁文件把
第二个进程拦住等第一个装完，而不是并发写坏同一个 .venv。

锁文件里写持有者的 PID。持有者不在了（上次安装被强退）就直接接管，不用干等；
它还活着就一直等，**不按 mtime 抢**——真的慢网安装超过 20 分钟时被第二个实例
抢锁、两个 pip 并发写同一个 .venv，正是这把锁要防的事。
"""
import os
import time


def pid_alive(pid):
    """进程还在吗。Windows 上 os.kill(pid, 0) 不是探活——signal.CTRL_C_EVENT 就是 0，
    等于给整个控制台进程组发 Ctrl-C，那边要用 OpenProcess。"""
    if os.name == "nt":
        import ctypes
        SYNCHRONIZE = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True            # 存在但不是我们的，按活着算
    except (OSError, ValueError):
        return False
    return True


def acquire(lock, deps_ok, wait_sec=900, poll_sec=3.0, sleep=time.sleep, clock=time.time):
    """返回 (状态, 锁路径)：
      ("acquired", path) 拿到锁，可以安装，用完要删；
      ("ready", None)    等待期间别的实例已装好，直接用；
      ("busy", None)     等太久还没轮到——绝不能自己再跑一遍 pip；
      ("nolock", None)   建不了锁文件（只读目录等），退化为尽力而为。"""
    deadline = clock() + wait_sec
    while clock() < deadline:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return ("acquired", lock)
        except FileExistsError:
            try:
                holder = lock.read_text(encoding="utf-8").strip()
                alive = holder.isdigit() and pid_alive(int(holder))
            except (OSError, ValueError):
                alive = True          # 读不到就当活着，宁可多等
            if not alive:
                try:
                    lock.unlink()
                except OSError:
                    pass
                continue
            print("[初始化] 另一个实例正在安装依赖，等待它完成…")
            sleep(poll_sec)
            if deps_ok():
                return ("ready", None)
        except OSError:
            return ("nolock", None)
    return ("busy", None)
