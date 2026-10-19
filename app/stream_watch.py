"""拉流这一侧「卡没卡住、多久没听到」的观察：阶段计时、无声计时、跨轮覆盖率、场末汇总的计数。

只记时刻和计数，不做决定、不出界面文字、不写审计：Pipeline 的统计循环每 10 秒问一次，自己决定写什么、
说什么（文字都在 pipeline.py，成对写）。它不依赖拉流任务——10-18 那 91 分钟里拉流任务卡死在
audio.py 的 wait() 上，统计循环和弹幕一直活着，所以检查放在统计循环里。

时钟：
  * 触发判断全用单调时钟。macOS / Linux 睡眠时单调时钟不走，睡着的那段不会被算成「卡住」；
  * Windows 睡眠、或者事件循环被卡住时单调时钟照走：时钟对账发现跳变时调 shift()，把各个起点往后挪
    单调时钟多走的那段，醒来时阶段计时和无声计时不会一起误报；
  * 给中控看的时长用墙钟：合盖 30 分钟就说 30 分钟，跟 clock_gap 那条提示对得上。
"""
import time
from collections import deque
from contextlib import contextmanager

OTHER = "other"


class StageClock:
    """这一场此刻在哪个阶段、进来多久了、预算多少。进出是同步的一次赋值，不 await。

    整个重连循环的每一刻都落在某个阶段里：离开一个具名阶段时回到兜底的 other（它也有预算）——
    下一个没想到的卡点就在那里被抓到。每进一次阶段最多报一次超时。"""

    def __init__(self, other_budget, mono=time.monotonic):
        self._mono = mono
        self.other_budget = other_budget
        self.overruns = 0
        self.enter(OTHER, other_budget)

    def enter(self, name, budget, **extra):
        self.name = name
        self.entered = self._mono()
        self.budget = budget
        self.extra = extra
        self.reported = False

    def leave(self, name):
        """离开 name：还在 name 里才回到 other（中途已经进了别的阶段就不动）。"""
        if self.name == name:
            self.enter(OTHER, self.other_budget)

    @contextmanager
    def during(self, name, budget, **extra):
        self.enter(name, budget, **extra)
        try:
            yield
        finally:
            self.leave(name)

    def elapsed(self, now=None):
        return (self._mono() if now is None else now) - self.entered

    def overrun(self, now=None):
        """超了预算、这一次进来还没报过：返回 {stage, sec, budget_sec, …}（只报这一次）；否则 None。"""
        sec = self.elapsed(now)
        if self.budget is None or self.reported or sec <= self.budget:
            return None
        self.reported = True
        self.overruns += 1
        return dict(self.extra, stage=self.name, sec=round(sec, 1),
                    budget_sec=round(float(self.budget), 1))

    def shift(self, sec):
        self.entered += sec


class StreamWatch:
    """一场直播一份，挂在会话状态 sess["watch"] 上。拉流 reader 每帧调 note_audio()（两次赋值）。"""

    def __init__(self, other_budget, mono=time.monotonic, wall=time.time):
        self._mono, self._wall = mono, wall
        self.stage = StageClock(other_budget, mono=mono)
        self.started = mono()
        self.first_spawn = None       # (单调, 墙钟)：本场第一次起 ffmpeg。无声计时从这里起算——
        #                               模型加载在第一次解析之后、第一次拉流之前，首次要下载几 GB
        self.last_audio = None        # (单调, 墙钟)：最近一帧直播音频（收尾时吐出的不算）
        self.audio_sec = 0.0          # 本场收到的直播音频秒数（不含收尾吐出的）
        self.tail_bytes = 0           # 本场各轮收尾时 ffmpeg 吐出的字节合计
        self.tail_dropped_bytes = 0
        self.max_deaf_sec = 0.0
        self.no_audio_notices = 0
        # 跨轮覆盖率：最近 window 秒里收到多少秒直播音频。按单调时钟的整秒记 [秒, 音频秒]，每帧一次加法；
        # 不随每轮重建（以前的「音频偏少」每轮从零算、一轮要连满约 135 秒才可能响，10-18 一次都没响）
        self.cov_start = None         # 从哪一刻起算：本场第一帧直播音频（时钟跳变时跟着往后挪）
        self._cov = deque()
        self._breaks = deque()        # 每次断流的单调时刻（数窗口里断了几次）
        self.cov_ratio = None         # 最近一次算出来的覆盖率
        self.cov_low = False
        self.cov_below = 0            # 连续几次低于报警线

    def note_spawn(self):
        if self.first_spawn is None:
            self.first_spawn = (self._mono(), self._wall())

    def note_audio(self, sec, mono, wall):
        self.audio_sec += sec
        self.last_audio = (mono, wall)
        if self.cov_start is None:
            self.cov_start = mono
        idx = int(mono)
        cov = self._cov
        if cov and cov[-1][0] == idx:
            cov[-1][1] += sec
        else:
            cov.append([idx, sec])

    def note_break(self, mono=None):
        self._breaks.append(self._mono() if mono is None else mono)

    def coverage(self, mono=None, window=300.0, min_sec=120.0):
        """最近 min(window, 从第一帧起过了多久) 秒里收到的直播音频占比。分母按回放（analysis/coverage.py）
        的口径：开场头两分钟分子最多 120 秒，拿 300 秒当分母必然低于四成，一开场就会误报。不够 min_sec
        返回 None（还不评）。返回 (占比, 窗口秒数, 音频秒数, 窗口里断了几次)。"""
        now = self._mono() if mono is None else mono
        if self.cov_start is None:
            return None
        span = min(float(window), now - self.cov_start)
        if span < min_sec:
            return None
        lo = now - span
        cov = self._cov
        while cov and cov[0][0] < now - window - 1:
            cov.popleft()
        while self._breaks and self._breaks[0] < now - window - 1:
            self._breaks.popleft()
        audio = sum(a for idx, a in cov if idx >= int(lo))
        breaks = sum(1 for t in self._breaks if t >= lo)
        ratio = max(0.0, min(1.0, audio / span))
        return ratio, span, audio, breaks

    def note_deaf(self, sec):
        if sec > self.max_deaf_sec:
            self.max_deaf_sec = sec

    def _since(self):
        """无声从哪一刻算起：最近一帧直播音频，没有就是本场第一次起 ffmpeg；都没有返回 None。"""
        if self.last_audio is not None:
            return self.last_audio
        return self.first_spawn

    def silent_for(self, mono=None):
        """单调时钟上多久没收到直播音频（触发用）。还没起过 ffmpeg 返回 None：不算。"""
        since = self._since()
        if since is None:
            return None
        return (self._mono() if mono is None else mono) - since[0]

    def silent_wall(self, wall=None):
        """墙钟上多久没收到（显示用）。"""
        since = self._since()
        if since is None:
            return None
        return max(0.0, (self._wall() if wall is None else wall) - since[1])

    def shift(self, sec):
        """单调时钟多走了 sec 秒、程序其实没在运行（见文件头）：所有单调时钟的起点往后挪。"""
        if sec <= 0:
            return
        self.stage.shift(sec)
        self.started += sec
        if self.first_spawn is not None:
            self.first_spawn = (self.first_spawn[0] + sec, self.first_spawn[1])
        if self.last_audio is not None:
            self.last_audio = (self.last_audio[0] + sec, self.last_audio[1])
        if self.cov_start is not None:
            self.cov_start += sec
        shift = int(round(sec))
        for item in self._cov:
            item[0] += shift
        self._breaks = deque(t + sec for t in self._breaks)

    def summary(self, breaks, mono=None):
        """场末汇总（session_end 与每 5 分钟的 stream_summary 用）：只有数字。"""
        now = self._mono() if mono is None else mono
        out = {
            "breaks": int(breaks),
            "audio_sec": round(self.audio_sec, 1),
            "monitored_sec": round(max(0.0, now - self.started), 1),
            "max_deaf_sec": round(max(self.max_deaf_sec, self.silent_for(now) or 0.0), 1),
            "stage_overruns": self.stage.overruns,
            "no_audio_notices": self.no_audio_notices,
            "tail_sec_total": round(self.tail_bytes / 32000.0, 1),
            "coverage": None if self.cov_ratio is None else round(self.cov_ratio, 3),
        }
        if self.tail_dropped_bytes:
            out["tail_dropped_sec_total"] = round(self.tail_dropped_bytes / 32000.0, 1)
        return out
