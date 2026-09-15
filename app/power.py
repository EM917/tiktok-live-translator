"""直播监听期间阻止系统「空闲睡眠」，并让 macOS 不对本程序做 App Nap。

**只管空闲睡眠。** 合上笔记本盖子、手动点「睡眠」、电量耗尽时系统照样会睡——
那些程序阻止不了（2026-09-14 那场就是合盖睡的，当时另一个应用还持有空闲睡眠断言），
事后靠 pipeline 的时钟对账（clock_gap 记录）如实记下「这段时间程序没有运行」。
也不阻止屏幕变暗：屏幕黑了不影响监听，常亮只会白白耗电。

各平台的做法：
  macOS   —— 起 `/usr/bin/caffeinate -i -w <本进程 pid>`：-i 只挡空闲睡眠，-w 让它
             在本程序退出（含崩溃）时自己退出，不会漏成常驻；另外能导入 Foundation
             时开一个 NSProcessInfo activity（UserInitiatedAllowingIdleSystemSleep），
             只为不被 App Nap 节流。
  Windows —— SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)。这个状态
             按线程记，所以获取和释放都必须在同一个线程上调用（pipeline 在事件循环
             线程上做这两件事）。
  其它    —— 什么也不做，held 为空。

一场直播一个 SleepGuard：谁开的场谁拿（owner），晚到的旧任务不许释放新一场的。
"""
import os
import subprocess
import sys

CAFFEINATE = "/usr/bin/caffeinate"
# NSActivityUserInitiated (0x00FFFFFF) 去掉 NSActivityIdleSystemSleepDisabled (1 << 20)
NS_ACTIVITY_USER_INITIATED_ALLOWING_IDLE_SLEEP = 0x00FFFFFF & ~(1 << 20)
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _process_info():
    from Foundation import NSProcessInfo    # pyobjc：pywebview 在 mac 上本来就带
    return NSProcessInfo.processInfo()


def _kernel32():
    import ctypes

    fn = ctypes.windll.kernel32.SetThreadExecutionState
    fn.argtypes = [ctypes.c_uint32]
    fn.restype = ctypes.c_uint32
    return ctypes.windll.kernel32


class SleepGuard:
    def __init__(self, reason="直播合规监听中"):
        self.reason = reason
        self.held = []           # 实际拿到的机制，写进 session_start 当证据
        self.owner = None        # 开这一场的那个任务
        self.released = False
        self._proc = None
        self._activity = None
        self._info = None
        self._k32 = None

    def acquire(self, platform=None, popen=subprocess.Popen, pid=None,
                process_info=_process_info, kernel32=_kernel32):
        """尽力拿；每一种机制失败都只是少一项，绝不抛异常。参数只为测试可注入。"""
        platform = platform or sys.platform
        if platform == "darwin":
            try:
                self._proc = popen(
                    [CAFFEINATE, "-i", "-w", str(pid or os.getpid())],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
                self.held.append("caffeinate -i")
            except Exception:
                self._proc = None
            try:
                info = process_info()
                self._activity = info.beginActivityWithOptions_reason_(
                    NS_ACTIVITY_USER_INITIATED_ALLOWING_IDLE_SLEEP, self.reason)
                self._info = info
                self.held.append("app_nap_opt_out")
            except Exception:
                self._activity = None
        elif platform == "win32":
            try:
                k32 = kernel32()
                if k32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):
                    self._k32 = k32
                    self.held.append("SetThreadExecutionState")
            except Exception:
                self._k32 = None
        return self

    def release(self):
        """可重入。caffeinate 先 terminate，1 秒不退就 kill。"""
        if self.released:
            return
        self.released = True
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=1)
            except Exception:
                pass
        if self._activity is not None and self._info is not None:
            try:
                self._info.endActivity_(self._activity)
            except Exception:
                pass
        self._activity = self._info = None
        if self._k32 is not None:
            try:
                self._k32.SetThreadExecutionState(ES_CONTINUOUS)
            except Exception:
                pass
            self._k32 = None


def hold(reason="直播合规监听中"):
    """开一场直播时调用，返回已经尽力拿好的 SleepGuard。

    测试里（pytest）不起 caffeinate、不改线程执行状态；TLT_NO_SLEEP_GUARD=1 同样关掉。"""
    guard = SleepGuard(reason)
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("TLT_NO_SLEEP_GUARD"):
        return guard
    return guard.acquire()
