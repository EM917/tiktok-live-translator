"""运行日志：双击 .app 启动时，把终端输出抄一份到 ~/Library/Logs/TikTokLiveTranslator/run.log。

为什么（平台轮廓 §7.3 旁线 B，nfr N13.4）：从访达、启动台、程序坞、聚焦搜索打开的 .app 由 launchd 拉起，
标准输出、标准错误都接在 /dev/null 上（.app 启动器最后一句 `exec "$PY" main.py` 不做重定向）。程序里几百处
print——引擎回退、ffmpeg 的报错、找不到工具、写不进去——双击启动时哪儿都不留；审计只在监听时有，而且只记代码。

做法：
  * 只在 macOS、标准输出和标准错误都不连终端时装（双击 .app 就是这样）。用 Start.command 或命令行启动时终端里
    本来就看得到，不另写。Windows 走 Start.bat，黑窗口里看得到 print；Linux 从终端启动——两边照旧，不写。
  * 在 Python 这一层抄：sys.stdout、sys.stderr 换成「照旧写原来的流、另抄一份进日志」的包装（Tee）。原来的流
    一个字节不变（/dev/null 也照写，写它出错照旧抛给调用方）；子进程（ffmpeg、pip、osascript）继承的 1、2 号
    描述符不动，它们的输出照旧不进来。
  * 凑满一行才落盘，落盘前过 redact.scrub_secrets（API 密钥、cookie、手机同看口令、闪购助手的控制密钥，
    URL 只留到路径）；每行用 O_APPEND 一次 os.write 写出，不留在缓冲里——main.py 的很多出口是 os._exit，
    exec 换进程映像也不替 Python 冲缓冲。回车（\\r）照终端的样子处理：只留回车之后的那一版，进度条不刷屏。
  * 按大小轮转：run.log 写过 MAX_BYTES 就改名 run.log.1，旧的依次往后挪，最多留 BACKUPS 份。同一个文件可能
    有几个进程在写（双击两次、一键更新重启前后、开发副本）：每次写之前看一眼路径上的文件还是不是自己开着的
    那一个，被别的进程轮转走了就重开；轮转本身用一个锁文件串起来。
  * 每一跳（启动器的 Python、换进 .venv、换进 .app 壳、一键更新后重启）都从 main.py 顶上重新装一遍：头一行记
    进程号、Python、程序目录、版本和命令行，之后每行带时间和进程号，同一个文件里分得清是哪个进程写的。
  * 只写中文（终端、日志永远是中文，CLAUDE.md 第九条）。抄日志出任何错都不影响原来的输出：写不进去就记个数，
    下次写得进去时补一行「前面有几行没写进来」。
  * 文件 600、目录 700：日志里有主播名。~/Library/Logs 不在 iCloud 同步的桌面上，不同步、不进备份。
  * TLT_RUN_LOG_DIR：只在 macOS 上看，给开发副本和测试换目录；设成空串就不写。
"""
import atexit
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from .redact import scrub_secrets

DIR_ENV = "TLT_RUN_LOG_DIR"
APP_DIR = "TikTokLiveTranslator"
FILE_NAME = "run.log"
LOCK_NAME = ".run.log.lock"
MAX_BYTES = 5 * 1024 * 1024     # 一份写到这么大就轮转
BACKUPS = 3                     # 最多留 run.log.1 … run.log.3，合计约 20 MB
LINE_MAX = 16 * 1024            # 一行最多抄这么多字（去密钥之后再截，截断不会留下半截密钥）
PENDING_MAX = 64 * 1024         # 一直不换行时，攒到这么多先写出去
RETRY_SEC = 5.0                 # 写失败之后隔这么久再试，期间的行只计数；轮转锁拿不到时也隔这么久再轮转
LOCK_TRIES = 10                 # 轮转锁最多试这么多次（不阻塞地试，每次隔 LOCK_WAIT_SEC），合计约 50 毫秒
LOCK_WAIT_SEC = 0.005

_ACTIVE = {"log": None, "target": None}


def log_dir(platform=None, environ=None, home=None):
    """运行日志放在哪个目录。只在 macOS 上有：~/Library/Logs/TikTokLiveTranslator；TLT_RUN_LOG_DIR 可以换，
    设成空串是不写。别的平台返回 None（照旧，见模块说明）。"""
    if (platform or sys.platform) != "darwin":
        return None
    env = os.environ if environ is None else environ
    if DIR_ENV in env:
        raw = (env.get(DIR_ENV) or "").strip()
        return Path(raw).expanduser() if raw else None
    base = Path(home) if home is not None else Path.home()
    return base / "Library" / "Logs" / APP_DIR


def no_console(streams=None):
    """标准输出和标准错误都不连终端（和 main._has_console 相反的判断）。"""
    for stream in (sys.stdout, sys.stderr) if streams is None else streams:
        try:
            if stream is not None and stream.isatty():
                return False
        except Exception:
            pass
    return True


def start(root=None, platform=None, environ=None):
    """main.py 顶上调：macOS、没有终端时装上运行日志，返回 RunLog；不装返回 None。永不抛。"""
    try:
        if (platform or sys.platform) != "darwin" or not no_console():
            return None
        directory = log_dir(platform=platform, environ=environ)
        if directory is None:
            return None
        return install(directory, root=root)
    except Exception:
        return None


def install(directory, root=None, target=None, max_bytes=MAX_BYTES, backups=BACKUPS):
    """把 target（默认 sys）的 stdout、stderr 换成 Tee，写头一行。同一个进程只装一次，再调返回已装的那个。"""
    if _ACTIVE["log"] is not None:
        return _ACTIVE["log"]
    target = sys if target is None else target
    log = RunLog(RotatingFile(Path(directory) / FILE_NAME, max_bytes=max_bytes, backups=backups))
    log.write_line(header_line(root))
    target.stdout = Tee(target.stdout, log)
    target.stderr = Tee(target.stderr, log)
    _ACTIVE["log"], _ACTIVE["target"] = log, target
    atexit.register(_at_exit)
    return log


def uninstall():
    """换回原来的流（测试用；程序里不调）。没写完的半行先写出去。"""
    log, target = _ACTIVE["log"], _ACTIVE["target"]
    _ACTIVE["log"] = _ACTIVE["target"] = None
    if log is None:
        return
    for name in ("stdout", "stderr"):
        stream = getattr(target, name, None)
        if isinstance(stream, Tee) and stream.log is log:
            stream.finish()
            setattr(target, name, stream.inner)
    log.close()


def active():
    """当前进程装上的 RunLog，没装是 None。"""
    return _ACTIVE["log"]


def _at_exit():
    """正常退出（走 atexit 的那几条路）：两条流里没换行的半行写出去。os._exit、exec 走不到这里，
    丢的只是半行——整行早就落盘了。"""
    log, target = _ACTIVE["log"], _ACTIVE["target"]
    if log is None:
        return
    for name in ("stdout", "stderr"):
        stream = getattr(target, name, None)
        if isinstance(stream, Tee) and stream.log is log:
            stream.finish()


def _version(root):
    try:
        text = (Path(root) / "VERSION").read_text(encoding="utf-8", errors="replace")
    except (OSError, TypeError):
        return "?"
    first = text.strip().splitlines()[0].strip() if text.strip() else ""
    return first[:40] or "?"


def header_line(root=None):
    """每一跳进程的头一行：进程号、Python、程序目录、版本、命令行（时间在行首，和别的行一样）。
    命令行里的直播间地址照样过去密钥。"""
    args = " ".join(sys.argv[1:]) or "（无参数）"  # i18n: terminal
    return ("======== 进程 {} 开始 · Python {}.{}.{}（{}）· 程序目录 {} · 版本 {} · 参数 {}"  # i18n: terminal
            .format(os.getpid(), *sys.version_info[:3], sys.executable,
                    root if root is not None else "?", _version(root) if root is not None else "?",
                    args))


def _overwrite(line):
    """一行里有回车时照终端的样子：回车之后的内容盖住前面的，只留最后一版。行尾的 \\r（\\r\\n）不算。"""
    line = line.rstrip("\r")
    if "\r" in line:
        line = line.rsplit("\r", 1)[1]
    return line


def _compact(pending):
    """攒着的半行里有回车：前面那几版已经被盖住了，不用再攒（进度条一直不换行时不至于越攒越长）。
    结尾的回车留着：后面跟换行就是 \\r\\n，跟别的字才是盖住。"""
    tail = "\r" if pending.endswith("\r") else ""
    body = pending[:-1] if tail else pending
    if "\r" in body:
        body = body.rsplit("\r", 1)[1]
    return body + tail


class RunLog:
    """一行一行写进轮转文件：去密钥、截长、加时间和进程号。线程安全，永不抛。"""

    def __init__(self, sink):
        self.sink = sink
        self.lock = threading.RLock()
        self.dropped = 0          # 写不进去、没写成的行数
        self._retry_at = 0.0

    def write_line(self, text, now=None):
        try:
            at = time.time() if now is None else now
            with self.lock:
                if self.dropped and at < self._retry_at:
                    self.dropped += 1
                    return False
                data = self._format(text, at)
                if self.dropped:
                    note = "（前面有 {} 行没写进运行日志）".format(self.dropped)  # i18n: terminal
                    data = self._format(note, at) + data
                self.sink.write(data)
                self.dropped = 0
                return True
        except Exception:
            with self.lock:
                self.dropped += 1
                self._retry_at = (time.time() if now is None else now) + RETRY_SEC
            return False

    @staticmethod
    def _format(text, at):
        clean = scrub_secrets(text)
        if len(clean) > LINE_MAX:
            clean = clean[:LINE_MAX] + "…（这一行太长，后面 {} 个字没抄）".format(len(clean) - LINE_MAX)  # i18n: terminal
        stamp = datetime.fromtimestamp(at).strftime("%Y-%m-%d %H:%M:%S")
        line = "{}.{:03d} [{}] {}\n".format(stamp, int(at * 1000) % 1000, os.getpid(), clean)
        return line.encode("utf-8", errors="replace")

    def close(self):
        with self.lock:
            self.sink.close()


class Tee:
    """照旧写原来的流，另把整行抄进 RunLog。不是 io.TextIOBase 的子类：那样 encoding、isatty、fileno 这些会被
    基类的默认值挡住，这里一律交给原来的流。

    没换行的半行按线程分开攒：print 是先写正文、再写换行两次调用，两个线程同时 print 时，终端上可能拼成
    「甲乙\n\n」，运行日志里各是各的整行。"""

    def __init__(self, inner, log):
        self.inner = inner
        self.log = log
        self._pending = {}        # {线程号: 还没换行的半行}

    def write(self, s):
        if isinstance(s, str) and s:
            try:
                self._feed(s)
            except Exception:
                pass
        if self.inner is None:
            return len(s) if isinstance(s, str) else 0
        return self.inner.write(s)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        if self.inner is not None:
            self.inner.flush()

    def isatty(self):
        try:
            return bool(self.inner is not None and self.inner.isatty())
        except Exception:
            return False

    def __getattr__(self, name):
        inner = self.__dict__.get("inner")
        if inner is None:
            raise AttributeError(name)
        return getattr(inner, name)

    def pending(self, ident=None):
        """这个线程（默认当前线程）还没换行的半行。"""
        return self._pending.get(threading.get_ident() if ident is None else ident, "")

    def _feed(self, s):
        ident = threading.get_ident()
        with self.log.lock:
            parts = (self._pending.pop(ident, "") + s).split("\n")
            rest = parts.pop()
            for part in parts:
                self.log.write_line(_overwrite(part))
            if "\r" in rest:
                rest = _compact(rest)
            if len(rest) > PENDING_MAX:
                self.log.write_line(_overwrite(rest))
                rest = ""
            if rest:
                self._pending[ident] = rest

    def finish(self):
        """各线程没换行的半行写出去（退出、换回原来的流时）。"""
        try:
            with self.log.lock:
                rests, self._pending = list(self._pending.values()), {}
                for rest in rests:
                    rest = _overwrite(rest)
                    if rest:
                        self.log.write_line(rest)
        except Exception:
            pass


class RotatingFile:
    """O_APPEND 写一个文件，写过 max_bytes 就轮转。几个进程同时写同一个文件也对得上（见模块说明）。"""

    def __init__(self, path, max_bytes=MAX_BYTES, backups=BACKUPS):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backups = max(0, int(backups))
        self._fd = None
        self._lock = threading.RLock()
        self._rotate_after = 0.0      # 轮转锁上次没拿到：这个单调时刻以前不再试

    def write(self, data):
        with self._lock:
            try:
                if self._fd is None:
                    self._open()
                elif self._moved():
                    self._reopen()
                size = os.fstat(self._fd).st_size
                if (size > 0 and size + len(data) > self.max_bytes
                        and time.monotonic() >= self._rotate_after):
                    self._rotate()
                view = memoryview(data)
                while view:
                    view = view[os.write(self._fd, view):]
            except BaseException:
                self._close_fd()
                raise

    def close(self):
        with self._lock:
            self._close_fd()

    def backup_path(self, n):
        return self.path.with_name("{}.{}".format(self.path.name, n))

    def _open(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = (os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
                 | getattr(os, "O_NOFOLLOW", 0))
        self._fd = os.open(str(self.path), flags, 0o600)

    def _close_fd(self):
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def _reopen(self):
        self._close_fd()
        self._open()

    def _moved(self):
        """路径上的文件已经不是自己开着的这一个了（被别的进程轮转走、被删掉）。"""
        try:
            on_disk = os.stat(str(self.path))
        except OSError:
            return True
        mine = os.fstat(self._fd)
        return (on_disk.st_dev, on_disk.st_ino) != (mine.st_dev, mine.st_ino)

    def _rotate(self):
        got, lock = self._lock_file()
        if not got:
            # 别的进程拿着锁不放（比如停在调试器里）：这次不轮转，接着往这一份里写，过一会儿再试。
            # 不能一直等：print 跑在事件循环里，卡在这里就是卡住识别
            self._rotate_after = time.monotonic() + RETRY_SEC
            return
        try:
            # 拿到锁再看一眼：等锁的时候别的进程可能已经轮转过了，那就只重开、不再挪一遍
            if not self._moved():
                self._close_fd()      # 先关自己这一个：Windows 上开着的文件改不了名
                if self.backups:
                    for n in range(self.backups - 1, 0, -1):
                        src = self.backup_path(n)
                        if src.exists():
                            os.replace(str(src), str(self.backup_path(n + 1)))
                    os.replace(str(self.path), str(self.backup_path(1)))
                else:
                    os.unlink(str(self.path))
            self._reopen()
        finally:
            self._unlock(lock)

    def _lock_file(self, sleep=time.sleep):
        """(要不要轮转, 锁文件描述符)。拿到锁是 (True, fd)；没有 fcntl、锁文件开不了、文件系统不支持 flock 时
        没法串，照旧轮转 (True, None)；锁被别的进程拿着、试了 LOCK_TRIES 次还没放是 (False, None)。从不阻塞地等。"""
        try:
            import fcntl
        except ImportError:
            return True, None
        try:
            fd = os.open(str(self.path.with_name(LOCK_NAME)),
                         os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)
        except OSError:
            return True, None
        for attempt in range(LOCK_TRIES):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True, fd
            except BlockingIOError:
                if attempt + 1 < LOCK_TRIES:
                    sleep(LOCK_WAIT_SEC)
            except OSError:
                self._unlock(fd)          # 这个文件系统不支持 flock：没法串，照旧轮转
                return True, None
        self._unlock(fd)
        return False, None

    @staticmethod
    def _unlock(fd):
        if fd is None:
            return
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        try:
            os.close(fd)
        except OSError:
            pass
