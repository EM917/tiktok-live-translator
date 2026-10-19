"""断流之后怎么重连：退避多久、什么时候放弃。只有数字和决定，不碰界面、不发请求、不 await。

Pipeline._run_session 的重连循环每轮问它一次；界面文字、审计、解析请求都还在 pipeline 里。
拆出来是为了能单独测：pipeline.py 五千多行，_run_session 两百多行，规则写在里面就没法单独验。

V1Policy 是 2026-10-19 之前写在 _run_session 里的规则，原样搬过来（纯搬移，逐行比对见
asr-bench/live-stability/impl/check_move.py）：
  * 退避 min(30, 2 ** 连续重连次数)，只有一轮收满 30 秒音频、或房间回到在播时才清零；
  * 每次重连都重新解析流地址；
  * 连续「一帧音频都没有」的轮次（重连时解析失败也算一轮）超过预算就放弃：房间地址 5 次，
    粘贴的流地址 1 次。
"""


class V1Policy:
    name = "v1"

    def __init__(self, direct):
        # 直连 .flv/.m3u8 地址无法重新解析出「是否还在播」：播过一阵后结束
        # 就按正常收尾处理，重试预算也压到 1 次，别对着过期地址空耗。
        self.direct = direct
        self.budget = 1 if direct else 5
        self.reconnects = 0        # 连续重连次数：只决定退避间隔（2、4、…、30 秒），不上界面
        self.silent = 0            # 连续「一帧音频都没有」的轮次：决定何时放弃
        self.first_silent = None   # 这一串无声里第一次提示重连时的 silent：界面「第 N/M 次」从它数起
        self.host_waited = 0.0     # 这次中断里等房间恢复在播已经等了多久；收到音频才清零

    def round_ended(self, got_audio, audio_secs):
        """一轮拉流结束（直连地址播满 30 秒后的探活由调用方先做，探不到就不会走到这里）。"""
        if got_audio:
            self.host_waited = 0.0
        if audio_secs >= 30:
            self.reconnects = 0        # 刚才播得好好的：退避从 2 秒重新算
        # 拿到过音频的轮次不算失败：网络劣化时每轮只播二十几秒就断，
        # 五轮之后主播还在播，监听却宣布「重连失败」放弃了。只有连续几轮
        # 一帧都没有（地址过期、真下播）才放弃；退避间隔照常增长，不空转
        self.silent = 0 if got_audio else self.silent + 1
        if got_audio:
            self.first_silent = None

    def next_attempt(self):
        """下一次重连之前调。返回 None：预算用尽，放弃；否则 (退避秒数, 第 N 次, 共 M 次)。"""
        self.reconnects += 1
        if self.silent > self.budget:
            return None
        delay = min(30, 2 ** self.reconnects)
        # 「第 N/M 次」按放弃规则数，不按退避计数：reconnects 只在播满 30 秒或房间
        # 回到在播时清零，每轮只播二十几秒时照它显示会出现「第 6/5 次」。无声轮次每多一轮 silent 加 1，
        # silent 到 budget 的那一次是最后一次（再无声就放弃），所以从这一串无声的
        # 第一条提示数起：N 不超过 M，最后一条正好是 M/M。播过一阵才断时 M 是
        # budget+1，首连就没声音时首连已算掉一轮，M 是 budget。房间回到在播时
        # silent 不变，N 也不变——那一次没耗预算
        if self.first_silent is None:
            self.first_silent = self.silent
        attempt, attempts = self.silent - self.first_silent + 1, self.budget - self.first_silent + 1
        return delay, attempt, attempts

    def exhausted(self):
        """放弃时写进 session_end 的结束原因和观察。"""
        return {"reason": "reconnect_exhausted",
                "silent": self.silent, "budget": self.budget}

    def room_back_live(self):
        self.reconnects = 0    # 房间回到在播：马上重新解析，不再长退避

    def lookup_failed(self):
        self.silent += 1       # 解析不出地址也是一轮没有音频
