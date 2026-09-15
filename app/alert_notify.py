"""报警的系统通知。默认关闭，只有 settings.json 里 "alert_os_notify": true 才发。

中控常把 TikTok LIVE Studio 全屏盖在本程序窗口上：新报警只是被盖住的窗口里多一行，
人什么时候看到没有上限。但通知本身也可能闯祸，所以：
  - 不带词条原文：通知横幅会被 OBS 的屏幕采集拍进直播画面；
  - 不带声音：中控通常就坐在直播麦克风旁边；
  - 一阵报警只发一条：模糊档可能连着响，卖货时不能刷屏。
"""
import base64
import os
import shutil
import subprocess
import sys
import time

SETTING_KEY = "alert_os_notify"
TITLE = "TikTok 直播同传"
TEXT = "有新的疑似违禁词报警，请查看窗口"
BURST_GAP_SEC = 60.0      # 距上一条报警超过这么久，才算新的一阵
REMIND_SEC = 600.0        # 一阵报警持续很久时，最多每 10 分钟再提醒一次

# PowerShell 5.1 自带的 WinRT toast；AppId 借用 PowerShell 自己的（没有注册过的
# AppId 发不出通知）。<audio silent="true"/>：静音。
_WIN_APP_ID = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"
_WIN_TOAST = (
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
    "ContentType = WindowsRuntime] > $null; "
    "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, "
    "ContentType = WindowsRuntime] > $null; "
    "$x = New-Object Windows.Data.Xml.Dom.XmlDocument; "
    "$x.LoadXml('<toast><visual><binding template=\"ToastGeneric\"><text>@TITLE@</text>"
    "<text>@TEXT@</text></binding></visual><audio silent=\"true\"/></toast>'); "
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('@APP@')"
    ".Show([Windows.UI.Notifications.ToastNotification]::new($x))"
)


def enabled():
    from .settings import load_settings
    return load_settings().get(SETTING_KEY) is True


def command(platform=None, which=shutil.which):
    """发一条通知的命令行（不经 shell）。本机没有可用的通知手段时返回 None。"""
    platform = platform or sys.platform
    if platform == "darwin":
        # 不写 sound name 就是静音
        return ["osascript", "-e",
                'display notification "{}" with title "{}"'.format(TEXT, TITLE)]
    if platform.startswith("win"):
        script = (_WIN_TOAST.replace("@TITLE@", TITLE).replace("@TEXT@", TEXT)
                  .replace("@APP@", _WIN_APP_ID))
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                "-EncodedCommand", encoded]
    exe = which("notify-send")
    return [exe, TITLE, TEXT] if exe else None


class AlertNotifier:
    def __init__(self, clock=time.monotonic, is_enabled=enabled, runner=subprocess.run,
                 platform=None):
        self._clock = clock
        self._is_enabled = is_enabled
        self._runner = runner
        self._platform = platform
        self._last_alert = None
        self._last_notice = None

    def note_alert(self):
        """记一条报警，返回这一条该不该发通知。不做任何 I/O，可以在识别循环里调。"""
        now = self._clock()
        fresh = (self._last_alert is None or now - self._last_alert >= BURST_GAP_SEC
                 or self._last_notice is None or now - self._last_notice >= REMIND_SEC)
        self._last_alert = now
        if fresh:
            self._last_notice = now
        return fresh

    def send(self):
        """真的发一条：读设置、跑命令。放线程池里调；任何失败都只返回 False。"""
        try:
            if not self._is_enabled():
                return False
            cmd = command(self._platform)
            if not cmd:
                return False
            kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                      "stderr": subprocess.DEVNULL, "timeout": 15}
            if os.name == "nt":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self._runner(cmd, **kwargs)
            return True
        except Exception:
            return False
