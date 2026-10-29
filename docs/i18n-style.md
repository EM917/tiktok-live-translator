# TikTok Live Translator：英文界面文案规范

英文界面（中/英双语）的术语、大小写、标点和定稿文案，以这份文件为准。改词先改 §1 的术语表，再改代码。

- 写法机制：Python 写 `L("中文", "English")`、复数写 `LN(n, …)`（`app/i18n.py`）；HTML 写 `data-en*` 属性。
  中文原文一字不动，英文写在它旁边。
- 机械检查：`tests/test_i18n_pairs.py`（G2 成对、G3 同中文→同英文）、`tests/test_i18n_coverage.py`（G4 覆盖）、
  `tests/test_i18n_static_html.py`（G5 静态页）。按钮名、禁用词、例外清单这些规则表在 `tests/i18n_rules.py`，
  改表要写理由。本地看全仓的对：`python3 tools/i18n_pairs.py --list`。
- 无损证明：`tools/i18n_strip_check.py`（只加英文的提交）、`tools/i18n_const_check.py`（只收常量的提交）。

来源：2026-09-28 按 `web/index.html`、`web/*.js`、`web/style.css`、四份界面文字清单、9 张中文界面截图，
以及 `README.md` 里已经在用的英文术语整理。文中的 `文件:行号` 指那一天的代码（584e1ea），之后会漂移，以文字内容为准。

宽度数字的来源：用本机 SF Pro（`/System/Library/Fonts/SFNS.ttf`，按 CSS 实际字号和字重，
带 Apple 小字号 tracking）和 PingFang 量字形宽度，再拿截图校准过（字幕原文一行：截图 275px，
估算 275px；状态胶囊：截图 167，估算 170；品牌标签：截图 142，估算 143）。误差约 ±5px。
估算脚本和截图没有进仓库。

相对最初的文案稿，这一版按实现规格覆盖了八处：§1.5 不用的词、§2.3 换链接确认、§2.6 BLOCKED 的写法与禁用词两级、
§2.8 设置行、§3 #8 的 file:// 提示、§3 #107–#109 保留 emoji、§4.3 R17 关窗框。另外 §3 #2 与 §4.3 R14 的
`isAlertTitle` 跟着实现改成了与语言无关的写法。

---

## 0. 五条总原则

1. **先说看到了什么，再说能做什么。** 原因只写程序真的观察到的，没观察到就不写。
   这和 CLAUDE.md 第八条是同一条规矩，英文版同样适用。
2. **短。** 按钮 1–3 个词，以动词开头；标签用名词；说明文字一句只讲一件事。
   不写 please、simply、successfully、Oops、Sorry，也不用感叹号。
3. **同一个概念只用一个词。** 第 1 节术语表说了算，改词要先改表。
4. **界面文字会英文化，数据不动。** 这些原样保留：@用户名、品牌名、词表和违禁词内容、
   「字幕译成」下拉（它的选项本来就是各语言的自称）、文件名、HTTP 状态码、模型名、终端输出、审计 JSONL。
5. **不拼句子。** 每一句都是带命名占位符的完整模板（`{streamer}`、`{n}`），
   计数用 ICU plural（one/other），不要写 `"Clear " + noun` 这种拼接。

---

## 1. 术语表

### 1.1 产品与角色

| 中文 | English | 不用 | 理由 |
|---|---|---|---|
| TikTok 直播同传 | **TikTok Live Translator** | TikTok Live Interpreter, Live Translate | `.app` 和 README 已经在用这个名字。「TikTok」永远不改大小写、不翻译 |
| 中控 | **operator** | moderator, admin, controller | README 已经在用。moderator 在 TikTok 语境里是「房管」，意思不一样。界面里主要出现在手机页：*the operator*、*the operator’s computer* |
| 主播 | **streamer** | host, creator, anchor | host 会跟手机同看里的网络主机、IP 地址混在一起；creator 太宽泛；README 已经在用 streamer |
| 观众（TikTok 直播间里的） | **viewers** | audience | 只指 TikTok 直播的观众 |
| 手机同看的在看人数 | **watching**（如 “3 watching”） | viewers, people | 避开和 TikTok 观众撞词。YouTube 式的 “N watching” 单复数同形，不用处理复数 |
| 程序 / 本程序 / 本工具 | **the app** | the program, the tool, the software | 苹果的叫法 |
| 另一份程序 / 这个文件夹里的这一份（同一台电脑上解压了不止一份） | **another copy of the app** / **the one in this folder** | instance, installation, version | 2026-10-29 加（双击打开时窗口开到了另一份上，§3 #10a）。instance 是开发者的词；version 会被读成「另一个版本号」，而两份可以是同一个版本。指已经开着的那一份写 **the running copy** |
| 本机 / 这台电脑 | **this computer** | this Mac, this PC, localhost | 要跨平台，Mac 和 Windows 都能用 |

### 1.2 直播、场次与状态

| 中文 | English | 不用 | 理由 |
|---|---|---|---|
| 直播 / 直播间 | **live stream**（正文），**stream**（短标签） | LIVE room, live room, broadcast | TikTok 品牌写法是全大写的 LIVE，我们不照搬，当普通名词用；room 是中文直译 |
| 直播间地址 / 直播间链接 | **live link** | room URL, live address | 指 `tiktok.com/@x/live` 这种网页链接 |
| 流地址（.flv/.m3u8） | **stream URL** | stream link, stream address | 故意跟 live link 分开：一个是网页，一个是媒体地址。第八条那段文案就靠这两个词讲清楚 |
| 查流地址（重连时再向 TikTok 要一次流地址） | **look up the stream URL**（动词）；名词写 **lookup**（如 Next lookup in 3 min.） | resolve, fetch, request the stream URL | 2026-10-19 加（重连规则 V2）。和「直播流解析 / Stream Lookup」（§1.3，自检行名）是同一件事；正在解析时那句仍是 #111「正在解析直播流地址… / Getting the stream URL…」。「为少向 TikTok 发请求，程序每小时最多查 N 次流地址」说的是程序自己的规矩，英文写 To keep requests to TikTok low, the app looks up the stream URL at most N times an hour，不写 limit reached / rate limit（第八条：不能让人读成 TikTok 限了我们，`EN_LABELS` 也不许 rate limit） |
| 中断（本场第 k 次中断） | **interruption**（如 interruption 7 this session） | disconnect, drop, break | 2026-10-19 加：重连状态行累计这一场断了几次。用 interruption 接住中文「直播流中断」的 interrupted；序数照写阿拉伯数字，不写 7th |
| 本场 / 一场 | **session**（从 Start 到 Stop 的一次监听） | show | stream 指 TikTok 那边的直播，session 指我们这边的一次监听。“Stream ended” 和 “New session” 说的不是同一件事，不能混用 |
| 上一场 | **previous session**；标签写 **Previous** | last stream | |
| 换主播 | **Switch Streamer** | Change Streamer, Switch Room | Switch 更贴近「改听另一个人」，而且短 |
| 最近的直播间 | **Recent Streams** | Recent Rooms, History | |
| 待机 | **Ready** | Idle, Standby | Idle 是工程师用词。首页状态胶囊写 Ready 更像系统的空闲态 |
| 连接中 | **Connecting…** | Loading… | |
| 直播中 | **Live** | Streaming, On Air | |
| 直播已结束 | **Stream ended** | Offline, Finished | |
| 出错了 | **Error** | Something went wrong, Failed | 胶囊里只放一个词，具体内容看横幅 |
| 与本地服务断开 | **lost connection to the local service**；胶囊里写 **Reconnecting…** | backend, server | 用户能理解的层级只到 local service |

### 1.3 功能

| 中文 | English | 不用 | 理由 |
|---|---|---|---|
| 手机同看 | **Phone Viewing** | Phone Viewer, Phone view, Mirror to Phone, Screen Share | README 写的是 “Phone viewer”，但界面里 viewer(s) 还要用来指人；用作功能名会和人数打架。Viewing 跟 Screen Mirroring 的构词一样。建议 README 顺手改成 “Phone viewing” |
| 浮窗 | **Floating Window** | Float, Mini Player, Pop-out, Overlay, Picture in Picture | 顶栏按钮、窗口标题、页面标题都叫这个。开着的是一个始终在最上面的小窗，放最新一条字幕和最近几条弹幕。Overlay 会被读成盖在直播画面上；Picture in Picture 是苹果的视频画中画；Float 单独做名词不成立 |
| 主窗口 | **main window**；浮窗结束卡片上的按钮写 **Show Main Window** | main app, control window | 2026-10-26 补进术语表（b3-float-end）：浮窗以外那个大窗口。按钮名 Title Case，动词用 Show（把已经开着的窗口提到最前，不是新开一个） |
| 闪购 | **Flash Sale** | Flash Deal, Lightning Deal, Flash Sale Status | 2026-10-27 补进术语表：顶栏按钮名（按钮打开的是闪购助手的只读状态浮层）。TikTok Shop 直播里的限时促销就叫 flash sale；Flash Deal 在 TikTok Shop 里指另一种不在直播里的促销，容易混 |
| 闪购助手 | **Flash Sale Helper** | Flash Sale Assistant, Flash Sale Bot, the helper | 2026-10-27 补进术语表：本机另一个程序，不随本程序分发，本程序只读它的状态、能把它打开。它自己只有中文名（程序坞和访达里显示「闪购助手」），英文界面照这个名字写，打开按钮是 **Open Flash Sale Helper**，正文里不加 the。浮层标题、按钮的 tooltip 也用它 |
| 专用 Chrome（闪购助手开的那个 Chrome 窗口） | **the dedicated Chrome** | Chrome profile, automation browser | 闪购助手接口契约的叫法。只在闪购浮层里出现 |
| 商品后台 | **LIVE Manager** | Seller Center, product backend | TikTok 自己的页面名，照抄（大写 LIVE 也照抄）。中文第一次出现时括号里带上英文原名 |
| 验证码 | **CAPTCHA** | captcha, verification code | 全大写 |
| 续开（闪购停了以后闪购助手再开一次） | **renew**；次数写 **Renewed once / Renewed {n} times**；关了自动续开写 **Auto-renew off** | restart, relaunch | 闪购助手面板上的勾选项就叫自动续开 |
| 第 n 轮（循环闪购） | **Round {n}**；两轮之间 **Between rounds** | cycle, loop | |
| 开始同看 / 停止同看 | **Start Sharing / Stop Sharing** | Turn On / Turn Off | 动词加对象，两个按钮成对 |
| 本场品牌 | **Brand**（标签），**this stream’s brand**（正文） | Session Brand | 顶栏标签 “Brand · Bella All Natural” 和中文差不多宽 |
| 不限（默认） | **Any (default)** | None, All brands | None 会被读成「没有品牌」，实际意思是「不限定某一个品牌」 |
| 品牌词表 | **brand glossary** | brand list | |
| 粉丝 / 送过礼 / 订阅（弹幕作者旁的小标签） | **Follower / Gifter / Subscriber** | Fan, Friend, Donor | TikTok 随弹幕发的「和主播的关系」。Fan 太泛（粉丝团另有等级），Friend 不等于互关，Donor 带慈善含义。「互关 / Mutual」2026-10-21 起不显示：中控看到有的标了互关的并不是互关，本机核实不了；带互关标记的只标「粉丝 / Follower」 |
| 词表 / 领域词表（glossary.txt） | **glossary** | dictionary, term list | 帮助翻译用的 |
| 违禁词表（banned_terms.txt） | **banned-term list** | blacklist, banned glossary | 必须和 glossary 分开：list 决定报不报警，glossary 决定怎么翻译 |
| 违禁词 | **banned term** | prohibited word, prohibited term, forbidden word, blacklisted word | README 已经在用。term 也覆盖短语 |
| 违禁词报警 | **Banned-Term Alerts**（设置行名），正文写 **banned-term alerts** | Compliance Alerts, Warnings | |
| 疑似违禁词 | **possible banned term(s)**；面板标题写 **Possible Banned Terms** | suspected violation | 「疑似」是还没人工确认；violation 是法律判断，不该由程序下 |
| 报警 | **alert** | alarm, warning | warning 留给自检的「提醒」这一级。界面上「报警」是唯一的说法，不写「警报」「警示」：2026-10-26（专家审查 V5）以前桌面有「清空警报」「红色警报」、手机有「违禁词警示已关闭」，都改成了「报警」。终端那行 `[警报] …` 是终端前缀（`tests/i18n_rules.py` 的 `TERMINAL_PREFIXES`），终端永远中文、不在统一范围内 |
| 命中 | **match** | hit, trigger | 只说「匹配到了」这件事（「命中时弹出报警 / Show alert on match」），不当分级名（下一行） |
| 分级：精确 / 变体 / 疑似 | **Exact / Variant / Similar** | Fuzzy, Possible；中文不用「命中」 | fuzzy 是算法术语；Possible 会跟面板标题撞。手机和桌面统一成同一套。2026-10-26 以前桌面的 exact 档叫「命中」、手机叫「精确」（专家审查 V5）：「命中」已经留给 match（上一行），所以两边都叫「精确」 |
| 自检 / 启动自检 | **Startup Check** | Self-Test, Self-check, Diagnostics, Health Check | health 已经被「识别落后」那条 health 提示占了。README 写的是 “Startup self-check”，界面用更短的写法 |
| 自检项 | **check** | test, item | |
| 通过 / 提醒 / 未生效 | **passed / warning / isn’t working** | failed（只在读屏的级别说明里用） | 「未生效」强调的是功能没起作用，不是测试没过。isn’t working 是苹果的常用说法 |
| 翻译引擎 | **Translation Engine**；短写 **engine** | translator, provider | 提示里指这一行在哪，一律写「在「设置 > 翻译引擎」里」/ **in Settings > Translation Engine**（英文本来就是这一种）。2026-10-26（专家审查 V5）以前中文有五种：「在「翻译引擎」里」「在「设置 > 翻译引擎」里」「在页面上的「翻译引擎」里」「在页面的「翻译引擎」里」「在「翻译引擎」面板」。设置分组在直播中整块收起，所以**直播中**出现、叫人去换引擎、换模型或填密钥的提示，前面加「点「停止」后」/ **after you click Stop**（英文也可以放进句中：To switch back, click Stop, …；专家审查 U4）。启动自检里那一项（自检的「翻译引擎」一行）是另一个东西，照写「自检的「翻译引擎」一项」。**文档**（README、docs/*.md）里写界面路径照旧用 →（「设置 → 翻译引擎」/ Settings → Translation Engine），不跟着界面改成 >：文档里同一种路径还有 macOS 系统设置的多级路径（System Settings → Privacy & Security → Full Disk Access），一直是 →，读者照着点不会走错。界面文字（成对的 L() 和 data-en）一律用 >（2026-10-26 定，b6-wording 第 1 轮审查） |
| 回退（引擎） | **fallback**；正文写 **switched to {engine}** | downgrade | |
| 本地模型 | **local model** | offline model | |
| 免费额度 | **free quota** | free tier | |
| 密钥 | **API key** | token, secret | |
| 字幕 | **captions** | subtitles | 苹果 Live Captions、Zoom、Meet 在直播场景都叫 captions；subtitles 偏影视。README 宣传语可以继续说 subtitles，界面统一用 captions |
| 字幕字号 | **Caption size** | Font size | |
| 清空字幕 | **Clear**（顶栏按钮），tooltip 写 **Clear caption history** | Clear Captions（放不下，见 §4） | 2026-10-23 专家审查 V3 想把按钮上的字改成 Clear Captions，G10 重量了一次：默认 1000px 窗口里品牌标签被挤到 95px（live-full）、83px（带浮窗按钮）、67 / 55px（下拉补了第五种语言），都低于 100px 的下限，1101px 也有三档低于 120px。所以按钮上仍写 Clear，「字幕」两个字交给 tooltip 说。§4 R1 原来建议的 aria-label “Clear captions” 也没有加：它对应的中文是按钮上的「清空字幕」，这句中文的英文已经是 Clear，同一句中文对应两种英文过不了 G3。换成沿用 title 那一对（清空历史字幕 / Clear caption history）不违反 G3，但和 title 是同一句话，没有多给信息，所以也不加。点第一下之后（两段式确认的武装态）写「再点确认 / Clear?」，见 §3 第 24a 行 |
| 清空弹幕 | **Clear Comments**（弹幕栏按钮），tooltip 写 **Clear comment history** | Clear（和顶栏清空字幕同名） | 2026-10-23 以前英文也只写 Clear、没有 tooltip，和顶栏那颗分不开（专家审查 V3）。弹幕栏里不挤，写全。点第一下之后和顶栏那颗同一句「再点确认 / Clear?」（同一句中文只许一种英文） |
| 重译 | **Retranslate** | Redo, Translate Again | |
| 弹幕 / 观众弹幕 / 评论流 | **comments / Comments**；实在需要时才说 **comment stream** | danmaku, bullet comments, chat | TikTok 英文界面就叫 comments。「弹幕」是中文平台的概念 |
| 开始翻译 | **Start** | Start Translating, Start translating, Go | 和 Stop 成对。十几条后端提示要引用这个按钮，“click Start” 最短（38px，中文是 56px；“Start Translating” 要 123px） |
| 停止 | **Stop** | End, Disconnect | |
| 语音识别 / 识别 | **speech recognition / recognition** | ASR, transcription | 界面上一律不写 ASR（以前只有底部延迟统计行这种技术读数用过，那一行 2026-10-15 删了） |
| 识别门限（切段器判「有没有声音」的音量门槛） | **speech recognition threshold** | silence threshold, gate, VAD | 2026-10-26 补进术语表（#149 之前「已收到 N 秒直播音频，但音量一直低于识别门限」就在用）。gate、VAD 是代码里的词；说 silence 会替人认定低于门限的就是没人说话（背景音乐也可能），只说音量低于门限（第八条） |
| 识别落后 / 积压 | **falling behind / backlog** | lagging, queue | |
| 语音识别不确定（字幕角标） | **Recognition unclear** | Uncertain recognition, Low confidence, Possible error | 「不确定」照「拿不准」译 unclear（§1.6），不用 uncertain；不写 error、wrong——角标只说这一段像是出了大问题，没说识别错了。low confidence 是其中一条规则的读数，不是角标的意思 |
| 检测已降级 | **Detection degraded** | Detection impaired | |
| 人声降噪 / 降噪 | **Noise Reduction** | Voice Isolation, Denoise | Voice Isolation 是苹果麦克风模式的名字，意思是「只留人声」，RNNoise 做不到这么强。合规工具不该夸大 |
| 审计 / 审计日志 | **audit log** | compliance log, record | 和 README 同名。审计文件的内容不翻译 |
| 磁盘空间 | **Storage** | Disk Space, Disk Usage | macOS 系统设置里同一个功能就叫 Storage，而且更短。自检那一行也用 Storage |
| 模型与日志 | **models and logs** | data | |
| 识别音频片段（logs/asr-evidence/，「磁盘空间」里的一行） | **recognition audio clips**；数量写 **{n} clips** | evidence audio, recordings | Recordings 会被读成整场录音，而这里只有零散几秒的片段；evidence 在「违禁词证据」里已经指审计日志，再用会混。按段数（clip）数，不按文件数：一段一个 WAV，索引文件不算 |
| 字幕记录（设置行；一场一个文件，含字幕、译文和观众弹幕） | **Transcripts**（设置行名）；正文说一场的那份文件时写 **transcript** | Caption History, Recordings, Logs | Recordings 会被读成录音录像；Logs 是审计日志的叫法，而审计日志里没有弹幕内容，字幕记录里有，两者不能混着叫。Caption History 撞「清空字幕」的 tooltip（Clear caption history） |
| 导出最近一场 | **Export Latest Session** | Export Last, Download | session 同上；Latest 不会被读成「最后一条字幕」 |
| 打开文件夹 | **Open Folder** | Show in Finder, Reveal | 要跨平台，访达和资源管理器的叫法不通用。与 Open Brands Folder、Open Rules Folder 同一个构词 |
| 正在用 / 本程序 / 非本程序 | **In use / This app / Not this app** | Other | 「非本程序」的意思是「不一定是别人的」，照字面翻最诚实 |
| 更新 / 一键更新 / 更新说明 | **Update / Update Now / Release Notes** | Upgrade | |
| 设置 | **Settings** | Preferences | |
| 字幕译成（顶栏下拉：字幕译成哪种语言；2026-10-20 以前叫「目标语言」） | **Captions in**（标签、aria-label）；tooltip 写 **Captions are translated into this language. To change the app language, go to Settings > App Language.**；正文写 **the language captions are translated into**，参数说明里可以简写 **caption language** | Translate to, Target Language, Language | 用户 2026-10-20 把旧标签「目标语言 / Translate to」当成了界面语言，切成 English 等界面变。标签要一眼读得出管的是字幕：标签连着选项读就是 “Captions in English”「字幕译成 English」。只写 Language 会和界面语言（App Language）分不开。中文 tooltip 是「字幕译成这种语言。界面语言在「设置 > 界面语言」里改」。2026-10-29 同一种误会又来了一次（连接中、1000px 窗口里标签被省掉）：英文界面选了中文、中文界面选了 English 时，下拉下面就地弹一次这句 tooltip，外加一颗切换界面的按钮（§2.8、§3 #21a） |
| 主播语言 | **Spoken language** | Source language, Streamer language | 说的是「识别听哪种语言」，普通用户能懂；source 是翻译行业的行话 |
| 界面语言（新加的设置） | **App Language** | Language, Display Language | 必须和顶栏的 Captions in 一眼就能分开 |
| 跟随系统 | **System ({resolved})**，如 “System (English)” | Auto, Default | |
| 二维码 | **QR code** | QR | |
| 局域网地址 | **local network address** | LAN IP, intranet address | |
| 只看模式 | **View only** | Read-only mode | |
| 提示音 | **Sound** | Ringtone, Beep | |
| 演示模式 | **Demo mode** | Test mode | |
| 浏览器登录态 | **Browser Login** | Cookies, Session | |
| 登录（动作）/ 登录（名词）/ 已登录、未登录 | **sign in** / **sign-in** / **signed in、signed-out** | log in, login, logged in | 苹果的说法；browser_login、comment_source、resolver、selfcheck 统一这么写。设置行名 Browser Login 照上一行保留；钥匙串对话框里的 login password 是 macOS 自己的叫法，照抄 |
| 完全磁盘访问权限 | **Full Disk Access** | | 照抄 macOS 里的原名 |
| 直播流解析 | **Stream Lookup** | Stream Resolution | 「解析」对用户来说就是去找流地址。Resolution 容易被当成分辨率 |
| 音频组件 ffmpeg | **Audio (ffmpeg)** | Audio Component | ffmpeg 全小写，是它的品牌写法 |

### 1.4 不翻译、不改写

`TikTok`、`@username`、品牌名（`Bella All Natural`）、`DeepL`、`Claude`、`OpenAI`、`Google`、
`Ollama`、`Hy-MT2 1.8B/7B`、`TranslateGemma`、`MLX Whisper large-v3-turbo`、`faster-whisper`、
`ffmpeg`、`yt-dlp`、`TikTokLive 7.0.2`、`Safari`、`banned_terms.txt`、`glossary.txt`、
`profiles/`、`brands/`、`logs/`、`.flv`、`.m3u8`、`HTTP 429`、`code 4003110`、
「字幕译成」下拉的选项（四种，加上设置里存着别的语言时补出来的那一项）、违禁词和词表内容、报警里的 `msg.term`。

### 1.5 设计过程中出现过、现在不用的词

三份实现方案里与本表冲突的写法一律不用：**prohibited term**（用 banned term）、**Phone view**（用 Phone Viewing）、
**Self-check**（用 Startup Check）、**报警分级**里的 **Possible**（报警分级一律用 Exact / Variant / Similar）、
**Start translating**（按钮就叫 Start）、**Translate to**（顶栏下拉的旧标签，2026-10-20 起叫 Captions in，见 §1）、**Mutual**（「互关」标签的英文，2026-10-21 起不显示，带互关标记的只标 Follower，见 §1.3）。

**Possible 的两处用法，别混：**

- 违禁词报警：面板标题 “Possible Banned Terms”，分级只用 Exact / Variant / Similar，不再往分级里加 Possible。
- 弹幕留意（§1.6）：每个类别名写成 “Possible …”（Possible insult、Possible threat of harm……）。
  理由同「疑似」：程序只看到文字，没人工确认之前不下结论。这是弹幕留意类别名的固定前缀，
  不用在别的地方（Suspected、Likely 都不用：Likely 撞 `EN_CAUSAL`）。

### 1.6 弹幕留意（Comment Review）

2026-10-13 起。功能说明见 `comment_guard/README.md`。界面文字在 `web/guard.js`、`web/index.html`（`#guard-bar`、
`#guard-drawer`、`#guard-card`）和 `app/pipeline_guard.py`；后端只发代码，文案由前端按代码映射。

| 中文 | English | 不用 | 理由 |
|---|---|---|---|
| 弹幕留意 | **Comment Review**；状态写 **Comment Review: …**（弹幕栏胶囊上只有「只用规则」「规则 + Claude」这两种常态，只画 **Comment Review**，整句放进按钮的 title；关闭和所有 warn 照旧写整句） | Moderation, Comment Moderation, Comment Guard | Moderation 暗示程序能处理，而本程序只读：不能删弹幕、不能禁言。Guard 是代码里的名字 |
| 需要留意的弹幕 | **comments to review**；标题写 **Comments to Review**；空态写 “No comments to review yet.” | flagged comments, alerts, warnings | alert 已经是违禁词报警的名字，warning 留给自检的「提醒」；flagged 带判断 |
| 需要留意 n 件（顶部条、浮钮上的数） | 数的是事件：写 **{n} item to review / {n} items to review**，不写 comments | {n} comments to review, events, incidents, flagged | 用户 2026-10-13 决定：一次刷屏算 1 件，不数其中的弹幕条数（多账号同句曾经也算 1 件，2026-10-14 起不再显示，见下）；写成 “60 comments to review” 会把一个事件说成 60 条；events 是代码里的词；incident 已被别处占用。中文量词用「件」，不用「条」（「条」留给数弹幕本身的地方，如抽屉里的「共 12 条」） |
| 拿不准 | **unclear** | uncertain, maybe | maybe 撞 `EN_CAUSAL` |
| 关注名单 | **Marked Viewers** | Watch List, Follow List, Block List | Watch 已被手机同看的在看人数占用；Follow 撞 TikTok 的关注；Block List 撞 `EN_LABELS` |
| 加入关注名单 / 移出关注名单 | **Mark / Unmark** | Follow / Unfollow, Add to List, Watch | 动词对，短。中文按钮写全，不用单独一个「关注」：在中文 TikTok 语境里它就是 follow |
| 模型复核 | **model review**；设置行的字段标签写 **Model review** | AI review, Auto review | 「AI」不说明做了什么 |
| 模型意见 | **Model: …**（如 “Model: agrees with rules”） | Review: …, AI says | 单独的 “Review: …” 会跟 “Comment Review: …” 挨在一起读不清 |
| 同一人的前一条（随弹幕一起送去复核的上下文） | **the same viewer’s previous comment** | the last message, context, history | 设置行的隐私说明里用；弹幕在界面里一律叫 comment，看直播的人叫 viewer（同 Marked Viewers） |
| 刷屏 | **flooding**；类别名 “Possible flooding” | spam | spam 带判断 |
| 引流 | **redirect**；类别名 “Possible redirect (link)” 等 | promotion, traffic diversion, spam | 说的是「把人引去别处」这个动作 |
| 本场重新启用 | **Turn Back On for This Session** | Re-enable, Restart | |
| 查看 / 收起（顶部条右侧的控件） | **View** / **Hide** | Open, Show, Close, Details | 同一个控件，字写它点了之后会做什么：抽屉收着写「查看 / View」，抽屉开着写「收起 / Hide」（带一个朝上的箭头）。「收起 / Hide」与弹幕栏里收起整列弹幕的按钮同词，那个按钮在另一块区域，不会同时被误会 |
| 弱信号 | **weak signals** | low confidence | 只是线索，还没有定论 |
| 规则 / 规则文件夹 | **rules** / **Open Rules Folder** | lexicon, dictionary | 词表（glossary）是翻译用的，这里是判定规则 |

**类别名**（后端只发代码；中文一律写「疑似……」，英文一律写 “Possible …”，`flood_brigade` 只写观察，不加「疑似」。
`flood_brigade` 和琥珀理由 `brigade` 从 2026-10-14 起后端不再发——多账号同一句几乎都是主播要求的抽奖口令，只在审计里计数；
这两句文案留在表里，只为认得旧数据。账号数或秒数缺失、为 0 时不写这句理由，绝不写「0 个账号，0 秒内同一句」）：

| 代码 | 中文 | English |
|---|---|---|
| `insult` | 疑似辱骂 | Possible insult |
| `threat` | 疑似人身威胁 | Possible threat of harm |
| `sexual` | 疑似涉性言语 | Possible sexual remark |
| `accuse_claim` | 疑似指称假货或骗局 | Possible claim of fakes or scams |
| `accuse_call` | 疑似号召举报或不买 | Possible call to report or not buy |
| `accuse_report` | 疑似扬言举报 | Possible threat to report |
| `flood_user` | 疑似刷屏 | Possible flooding |
| `flood_repeat` | 疑似反复发同一句 | Possible repeated comment |
| `flood_brigade` | 多账号同一句 | Same comment from several accounts |
| `promo_link` | 疑似引流（链接） | Possible redirect (link) |
| `promo_contact` | 疑似引流（联系方式） | Possible redirect (contact info) |
| `promo_pull` | 疑似拉人去别处 | Possible redirect to another stream or shop |
| `promo_scam` | 疑似冒充官方或诈骗信息 | Possible impersonation or scam offer |

**顶部条里的计数写法**（2026-10-13，看了截图后定）：数字紧贴类别名（“Possible flooding 1” / 「疑似刷屏 1」）读不出是计数，
像名字的一部分。类别后面的计数一律写成 **`{类别名} ×{n}`**，两种语言相同：× 前面一个空格、后面紧贴数字，
如 “Possible flooding ×2”、「疑似引流（链接） ×1」。不用括号（类别名自带括号，会变成 “Possible redirect (link) (1)”），
不用冒号（同一行的 “Latest 20:00:00” 是时刻，冒号会混），也不改成复数名词（13 个类别名各配一个复数，术语表跟着膨胀）。
数的仍是事件（用户决定 10）。同一行里另外几段本来就带量词，不动：「需要留意 n 件」/ “n items to review”、「拿不准 n 件」/
“n unclear”、「关注的人发言 n 条」/ “n from marked viewers”（后者数的是发言本身，所以中文用「条」不用「件」）。
同一规矩适用于以后顶部条里任何「名字 + 数」的写法：数字前面要有 × 或量词，不能只靠一个空格。

**琥珀（拿不准）的理由只写观察**（第八条）：「没有问号」「第 2 次」，不写「也可能是提问」。英文同样不用
because / likely / maybe / probably。`pending`（命中的词条还没经业务确认）、`self`（第一人称）两条是规则层后来
加的，同样只写事实。

**英文页的 “Comment Review: …” 状态行**和**模型意见 “Model: …”** 分开写：前者说功能整体处于什么状态，
后者说模型对某一条弹幕的看法。

---

## 2. 风格规则

### 2.1 大小写

要分清两类东西：**给界面元素起的名字**，和**描述性的文字**。

| 元素 | 规则 | 例 |
|---|---|---|
| 窗口标题、产品名 | Title Case | TikTok Live Translator |
| 按钮、顶栏按钮、链接式按钮、二段确认的武装态 | Title Case | Start, Stop, Switch Streamer, Copy Link, Click Again to Clear, Clear?, Show All Steps, How to Update, Show Main Window, Update Now, Release Notes |
| 浮层 / 面板标题、分组标题、设置行名、自检项名 | Title Case | Phone Viewing, Recent Streams, Settings, Startup Check, Noise Reduction |
| 下拉菜单项 | 项名用 Title Case，括号里的补充说明用小写开头 | Spanish + English (default), Local Hy-MT2 7B (more accurate, uses more memory) |
| 控件左边的字段标签 | Sentence case | Spoken language, Brand, Captions in |
| 开关 / 复选框标签 | Sentence case | Show alert on match |
| 状态文字、胶囊、标签、设置行右侧的摘要 | Sentence case | Live, Stream ended, Alerts on, All passed, Off · 53 terms, In use |
| 说明、提示条、横幅、错误、对话框正文、通知正文 | Sentence case，完整句子带句号 | The previous session’s captions are below. |
| 输入框占位文字 | Sentence case，不加句号 | New streamer’s live link or @username |
| tooltip（title）、aria-label | Sentence case；只有一个短语时不加句号，完整句子才加 | Clear caption history |

**Title Case 细则**：实词首字母大写；冠词（a/an/the）、并列连词（and/or/but）、
不超过 4 个字母的介词（to/in/on/of/for/with/from/by/at）小写，除非放在开头或结尾。
带连字符的两部分都大写（Banned-Term Alerts）。专有名词保留原写法（ffmpeg、yt-dlp、iPhone）。
`@username` 保持原样，前面的 to 照规则小写：**Switch to @itzesantana11**。

在正文里提到某个界面元素时，直接写它的 Title Case 名字，不加引号：
**Click Start.**、**Go to Settings > Translation Engine.**
中文原文的「」一律去掉，用大小写区分就够了。

### 2.2 标点

| 规则 | 说明 |
|---|---|
| 省略号只用单字符 `…`（U+2026），前面不加空格 | 进行中的状态：Connecting…、Updating…、Checking… |
| 顶栏按钮不加省略号 | 顶栏相当于 toolbar，苹果的 toolbar 项不带省略号。所以写 **Switch Streamer**，不写 “Switch Streamer…”（省 11px，§4 用得上） |
| 对话框里的推按钮，点了还要再填东西的，加省略号 | 这个程序目前没有这种按钮；以后加了照这条做 |
| 截断统一交给 CSS（`text-overflow: ellipsis`） | 不要在 JS 里按字符数截断再补 “…”（见 §4 R10） |
| 句号 | 完整句子加；按钮、标题、标签、胶囊、占位文字不加。括号里以命令、路径或文件名收尾时，右括号前不加句号：`(Advanced: pip install -r requirements.txt)`，照着复制时句号不会带进命令 |
| 分隔符 ` · `（U+00B7，两边各一个空格） | 沿用中文界面：Live · @bella、Brand · Bella、Off · 53 terms |
| 破折号 | 中文的「——」优先拆成两句。实在要保留，用不带空格的 em dash `—`，一句最多一个 |
| 分号 | 不用，拆成两句。唯一例外：冒号后面逐条列 `name: reason` 时，条目之间用 `; ` 分隔（`Chrome: …; Safari: …`） |
| 冒号 | 标签后接值：`Startup Check: Noise Reduction isn’t working.` 冒号后面除非是专有名词或完整句子，否则小写 |
| 引号 | 引用用户内容（比如报警里的违禁词）用弯双引号 “term”。撇号用弯的 `’`（U+2019），代码和文件名里除外 |
| 括号 | ASCII 括号，前面加空格：`Spanish + English (default)` |
| 感叹号 | 不用 |
| 问号 | 不向用户提问（确认框里的 Continue? 一类除外）。唯一一处当按钮字用的是两段式确认的武装态 **Clear?**（§3 #24a）：问号就是「再点一下」的意思，前面的动词照 Title Case |
| 短状态语和整句同在一处 | 各按各的规矩，不为了整齐硬改：短状态语（胶囊、按钮态的那种，如 **Retranslated**）不加句号，整句加。例：弹幕重译完那条弹幕下面的说明，成了写「已重译 / Retranslated」，结果和原来一样写「重译结果和原来一样 / The retranslation is the same as before.」。前者复用手机重译按钮的同一句（G3：同一句中文只许一种英文，不能为加句号另起一句英文），后者是完整句子（§3 #101a） |
| `&` | 只在照抄苹果系统原名时用（Privacy & Security），其余写 and |
| `/` | 不写 and/or；数量比写成 `2 of 5` |
| 缩略形式 | 用：can’t、isn’t、don’t、won’t。苹果的语气就是这样，而且省宽度 |

中文标点对照：`，`→`,`，`。`→`.`，`；`→拆句，`：`→`:`，`（）`→` ()`，
`「开始翻译」`→`Start`，`——`→拆句或 `—`，`、`→`, `，`…`→`…`。

### 2.3 数字、单位、复数

- 界面上的数字一律写阿拉伯数字。千位用逗号（1,234），小数点用句点，百分号紧贴数字（82%）。
- 数字和单位之间留一个空格：`120.0 GB`、`12 sec`、`3 min`、`1 hr 5 min`。
- 时长有两种写法：
  - 正文空间够的时候写全称：*12 seconds*、*3 minutes*；
  - 摘要、胶囊、进度这类紧凑位置用苹果的缩写：`sec`、`min`、`hr`。
- 范围用 en dash：`2–5 minutes`。
- 第几次：「第 2/5 次」→ `attempt 2 of 5`。
- 模型规格原样写：`1.8B`、`7B`、`large-v3-turbo`。
- **复数**：所有带计数的模板都要有 one 和 other 两种写法，下表列全了。
  中文原文没有单复数变化，直接套一个模板就会出现 “1 items” 这种错。

| 位置 | one | other |
|---|---|---|
| settings-rows.js:17 自检失败 | 1 feature isn’t working | {n} features aren’t working |
| settings-rows.js:18 自检提醒 | Passed · 1 warning | Passed · {n} warnings |
| settings-rows.js:19 全部通过 | （2026-10-15 起不带总数，没有单复数：只写 All passed） | |
| settings-rows.js:49 词表条数 | 1 term | {n} terms |
| pipeline.py:1458 自检提示条 | Startup Check: 1 item isn’t working ({names}) | Startup Check: {n} items aren’t working ({names}) |
| app.js:1719 删除所选 | Delete 1 Item ({size}) | Delete {n} Items ({size}) |
| pipeline_disk.py:90 已删除 | Deleted 1 item and freed {size}. | Deleted {n} items and freed {size}. |
| app.js:1990 / viewer.py:429 换链接确认 | Changing the link disconnects the phones watching now ({n}). They’ll need to scan the new QR code. Continue? | 同左（不变形：`{n}` 由前端在点击时才填，后端选不了单复数） |
| alerts.js:60 报警条数说明 | Showing the latest {n} of {m} alerts this session | （{m} 至少是 2，只需要这一种） |
| app.js:564/589/597 词表迁移 | 1 entry | {n} entries |
| pipeline.py:961 丢段 | 1 audio segment | {n} audio segments |
| pipeline.py:154 已重试 | Retried once. | Retried {n} times. |
| selfcheck.py:434 词表生效 | 1 entry active | {n} entries active |
| guard.js guardAmberText 多账号同一句（2026-10-14 起后端不再发，认旧数据；n 或 s 缺失、小于 1 时整句不写） | Same comment from 1 account in {s} seconds | Same comment from {n} accounts in {s} seconds |
| guard.js guardCountText 事件条数 | 1 comment in {s} seconds / 1 comment | {m} comments in {s} seconds / {m} comments |
| guard.js guardBarSummary、guardFabTitle 顶部条和浮钮（按事件计） | 1 item to review | {n} items to review |
| guard.js guardBarSummary 拿不准、关注的人发言 | {n} unclear / {n} from marked viewers | 同左（不变形；中文是「拿不准 {n} 件」「关注的人发言 {n} 条」） |
| guard.js guardBarSummary 类别计数 | {类别名} ×{n} | 同左（不变形，写法见 §1.6） |
| guard.js guardUnreviewedText 没经过模型复核 | 1 comment wasn’t reviewed by the model this session. | {n} comments weren’t reviewed by the model this session. |
| guard.js guardSummary 关注的人数 | {n} marked | 同左（不变形） |
| guard.js guardRulesText 规则条数和体检警告 | 1 rule active · 1 rule check warning | {n} rules active · {w} rule check warnings |
| guard.js guardLastSeenText 几天前 | 1 day ago | {n} days ago |
| live-ui.js jumpLatestCaptionsText 「回到最新」按钮上的新字幕数 | Jump to Latest · 1 new caption | Jump to Latest · {n} new captions |
| live-ui.js jumpLatestCommentsText 弹幕栏那颗按钮上的新弹幕数 | Jump to Latest · 1 new comment | Jump to Latest · {n} new comments |
| 在看人数（live-ui.js:16、app.js:1869、viewer.py:409） | {n} watching | {n} watching（不变形） |
| 窗口标题（alerts.js:9、window_attention.py:20） | ({n}) Possible Banned Terms · TikTok Live Translator | 同左（标签式写法，不变形） |

### 2.4 时间与日期

- 时刻统一用 **24 小时制 `HH:MM:SS`**，补零，两种语言都一样，用于字幕、弹幕、报警、场次分隔线和审计里的时间段。
  理由：这些时间要拿去跟审计 JSONL 和终端日志逐秒对照；换成 12 小时制要多 3 个字符的 AM/PM，行宽也跟中文版不一样。
- 日期很少出现，出现时写 `Sep 28, 2026`（月份缩写）。不写 `9/28`，美式和欧式会读反。
- 相对时间写 `just now`、`5 min ago`。底部大字幕的滞后（2026-10-15）只在超过 15 秒才出现，按秒数写：`About 18 sec ago`、
  `About 2 min ago`（一分钟以上）；中文「约 18 秒前」「约 2 分钟前」。15 秒以内这一行不在，所以没有 `Just now`。
  「约」译 About，不用 `~`（那一行有地方）。

### 2.5 错误与提示的句式

按这个顺序写，没有的部分就跳过：

1. **发生了什么**：过去时，主语是那个东西，不是用户。
   *TikTok didn’t provide a stream URL for this live stream (code 4003110).*
2. **影响到什么、没影响到什么**：*Captions show the original text. Banned-term alerts aren’t affected.*
3. **程序正在自己做什么**：*Retrying in 20 sec (attempt 2 of 3)…*
4. **你可以做什么**：用祈使句，动词开头。*Click Start to try again.*
5. **技术细节放在最后**：*Details: {err}*

不要这样写：

- 不加 `Error:` 前缀，不写 Oops、Sorry、Please。
- 不怪用户。不写 “You entered an invalid URL”，写 **This isn’t a live link or username.**
- 不把猜测当原因。中文原文里带「可能是 A，或 B」的，英文改写成**让用户去核对的动作**。例如：
  - 原文：「直播流多次中断且自动重连失败——可能直播已结束，或网络不稳。请稍后点「开始翻译」重试。」
  - 译文：**The stream was interrupted several times and couldn’t reconnect. Check that the stream is still live and your network is working, then click Start.**
  - 这一句（和第八条家族里其它几句带猜测的中文）后来中文也照这个改了：「直播流多次中断且自动重连失败。请确认直播还在进行、网络正常，然后点「开始翻译」重试。」
- may 只能用来说「将来可能发生」的事（*Alerts may be delayed*），不能用来解释原因。

### 2.6 第八条（4003110）和「不猜原因」的英文禁用词

4003110 这一族文案包括：`pipeline.browser_only_message`、`resolver.py` 里的各条 `ResolveError`、
`browser_login.py` 的 advice 和 notice、pipeline.py:1683 的重试横幅、index.html:275 的输入帮助、
自检的提示条（`_selfcheck_incident_text`）。

这些文案里只写两件事：「接口没给流地址」，以及用户能做什么。

- 固定说法：**TikTok didn’t provide a stream URL for this live stream (code 4003110).**
- 中文原文有一句「不是网络或限流问题」。英文版**整句删掉**，否定句里出现 rate limit 也不行。
  删掉之后只剩 **Retried {n} times. TikTok doesn’t say why.**，中文是「已自动重试 {} 次，原因 TikTok 不说明」。
- **不写程序没观察到的事。** 这里曾经写过「同一时刻其它直播间正常」/ *Other live streams worked at the
  same time*。那是 2026-09-05 一次人工同分钟配对的结果，程序运行时并不去解析对照房间：本机这边出了
  问题、所有房间都拿不到流地址时，它照样会这么说，把中控引向「只是这个房间的问题」。09-29 复审后
  中英文都删了。要让用户自己做对照，就写成动作（「换一个正在直播的直播间试试」），不写成已经看到的结果。
- `browser_login` 的 BLOCKED 状态（macOS 没给完全磁盘访问权限）**不点名浏览器**。`observed_text` 是按浏览器
  逐条拼的（「Chrome：系统拒绝读取；Safari：系统拒绝读取」），写死 Safari 会让 Chrome 那一条说错：
  - `_OBSERVED[BLOCKED]` 写 **macOS didn’t allow access**，拼出来是 `Chrome: macOS didn’t allow access`；
  - `FDA_OBSERVED` 写 **macOS didn’t allow the app to read browser data.**
  - 不写 blocked。

禁用词分两级，G2 对每一句英文都查，正则在 `tests/i18n_rules.py`：

- **标签词**（`EN_LABELS`）：全仓所有英文都查。它放过产品术语 banned term / Banned-Term List，
  不然自检提示条里的 “Banned-Term List” 会误报：

  ```
  (?i)\b(age|aged|age[- ]restrict\w*|rate[- ]?limit\w*|throttl\w*|ban(s|ned)?\b(?![- ]terms?)|block(ed|ing|s)?|restrict\w*)\b
  ```

- **因果词**（`EN_CAUSAL`）：只查第八条家族，也就是 `resolver.py`、`browser_login.py`、`comment_source.py`、
  `viewer.py`、`viewer_share.py`、`viewer.js`、`viewer.html`、`index.html` 的 `#input-help`，以及 `pipeline.py` 的
  `browser_only_message`、`_resolve_media`、`_confirm_offline`、`_host_wait`、`_run_session`、
  `_selfcheck_incident_text`：

  ```
  (?i)\b(because|due to|caused by|the reason|probably|likely|must be|seems to
  |private|regions?|regional|maybe|perhaps|(?:not|isn[’']t) an? [\w -]*(?:problem|issue))\b
  ```

  第二行是 09-29 复审补的：private / region 对应中文标签词里的「私密」「地区」（只在家族里查，家族以外
  private network 一类照常用）；maybe / perhaps 对应「也许」「或许」；`not a network problem`、
  `isn’t a sign-in issue` 是排除原因的否定句。yt-dlp 自己的原话（放在 Details: 后面的数据，比如
  “This live is private”）不在查的范围里。

- 电脑休眠写 suspended 不在任何一级里，照常用。真有一句必须用到这些词，登记进 `EN_WORD_EXCEPTIONS`，
  写明「中文臂 → 理由」。
- **中文臂**（`ZH_LABELS`、`ZH_CAUSAL`）：同一个第八条家族里，G2 也查中文。标签词是年龄、限流、封禁、私密、
  观看限制、地区、风控、被挡、繁忙这一类；因果词是「可能是」「也可能」「可能 A，或 B」「多半」「因为」「导致」
  「也许」「或许」「恐怕」这一类，还有「不是……问题」这种排除原因的否定句。光「可能」二字不查，说将来可能发生的事（「系统可能弹出确认框」）照常用，跟英文 may 同一个规矩。
  家族以外不查中文：Google 引擎说明里的「会按 IP 限流」是对方写明的规则，不是猜的。

同一族以外，还有两处也顺手避开这些词：

- Google 引擎说明（app.js:2545）原文「会按 IP 限流」→ **Google caps how many requests one IP address can make.**
- HTTP 429 那条提示照实写 **returned HTTP 429**，不加 rate-limited。

### 2.7 可访问性文案

- 图标按钮的 aria-label 就写它的名字：**Close**、**Open Brands Folder**、**Show comments**。
- 读屏专用的级别说明 `（提醒）/（未通过）` → ` (warning)` / ` (failed)`，前面带空格。
- `<html lang>` 要跟着界面语言改成 `en` 或 `zh-CN`。拼写检查、读屏和字体回退都看这个属性（见 §4 R13）。

### 2.8 新加的「界面语言」设置

| 位置 | 中文界面 | 英文界面 |
|---|---|---|
| 设置行名 | 界面语言 · Language（带上英文，困在中文界面里的英文用户也找得到） | App Language |
| 行右侧摘要 | 跟随系统 · 中文 / 中文 / English | System · English / 中文 / English |
| 选项 1 | 跟随系统（中文） | System (English) |
| 选项 2 / 3 | 中文 / English | 中文 / English |
| 行下方的说明 | 切换后窗口会刷新一次。手机同看页按各自手机的语言显示。 | The window reloads once after you switch. Phone Viewing pages follow each phone’s language. |
| 「字幕译成」就地提示里的按钮（§3 #21a） | 字：English；aria-label：界面语言改成 English | 字：中文；aria-label：Set App Language to 中文 |

语言名永远用该语言自己的写法（中文 / English）。这样用户就算落进一个看不懂的界面，也能认出自己的语言，切回去。

不写「关窗确认框重启后才换语言」：切换后页面经 JS 桥通知窗口，确认框当场就换（见 §4.3 R17）。

---

## 3. 最显眼的约 100 条

「宽度」一列是 zh→en 的估算像素，只有在宽度敏感的位置才写。†表示这个 key 在 HTML 和 JS 里各写了一份，两边都要改。

### A. 窗口与系统层

| # | 位置 | 中文 | English | 宽度 / 备注 |
|---|---|---|---|---|
| 1 | index.html:6、alerts.js:6、main.py:611/655、window_attention.py:14、alert_notify.py:17、macbrand.py:35 | TikTok 直播同传 | TikTok Live Translator | 这 6 处要一起改，最好提成一个常量 |
| 2 | alerts.js:9、window_attention.py:20 | ({n}) 疑似违禁词 · TikTok 直播同传 | ({n}) Possible Banned Terms · TikTok Live Translator | 标签式写法，不做单复数。`isAlertTitle` 改成与语言无关：取出开头的 `(n) `，再与当前语言的 `alertTitle(n)` 比较 |
| 3 | app.js:545 | 有新版本 · TikTok 直播同传 | Update Available · TikTok Live Translator | |
| 4 | alert_notify.py:18 | 有新的疑似违禁词报警，请查看窗口 | New banned-term alert. Open the window to review it. | 系统通知的正文 |
| 5 | window_close.py:10 | 正在监听直播，关闭窗口会停止违禁词监听。确定关闭？ | Close the window? This stops monitoring the live stream, including banned-term detection. | pywebview 的退出确认框 |
| 6 | window_close.py:11/12 | 关闭 / 取消 | Close / Cancel | |
| 7 | main.py:146/174 | 知道了 / 好 | OK | 原生对话框的按钮 |
| 8 | index.html:543 | 请不要直接打开这个文件 / 回到解压出来的文件夹，双击…启动程序，字幕界面会自动打开。 | Don’t open this file directly. / Go back to the unzipped folder and double-click TikTok Live Translator.app (Mac) or Start.bat (Windows). The caption window opens automatically. | 这段内联脚本跑得最早，什么都还没加载：中英两段并列，不读 `navigator.language` |
| 9 | viewer.html:6 | 手机同看 · TikTok 直播同传 | Phone Viewing · TikTok Live Translator | |
| 10 | pipeline.py:1799 | 直播合规监听中 | Monitoring a live stream | macOS 电源断言的原因文字，只在 `pmset` 里看得到 |
| 10a | instance.py other_install_text（main.py 双击打开、端口上已有本程序、不是这个文件夹里的这一份时） | 另一份程序（v{ver}，不是这个文件夹里的这一份）已经在运行，这次打开的是它。/ 另一份程序（不是这个文件夹里的这一份）已经在运行，这次打开的是它。/ 程序已经在运行，但它没有说自己是哪个文件夹里的那一份，这次打开的是它。后面都接：要用这个文件夹里的这一份，请先退出正在运行的那一份，再重新打开。 | Another copy of the app (v{ver}, not the one in this folder) is already running, so that copy opened instead. / Another copy of the app (not the one in this folder) is already running, so that copy opened instead. / The app is already running but didn’t report which folder it’s from, so that copy opened instead. + To use the one in this folder, quit the running copy first, then open this one again. | 2026-10-29 加。系统对话框（`_info_dialog`，按钮「知道了 / OK」），照旧打开已有实例，只是说清楚。第三句给加 X-TLT-Install 之前的版本（0.19.0 和更早）：它没说，就照实写没说，不猜它是旧版还是同一份。窗口模式带了 `--url` 时和「这次给的直播间地址没有自动开始」并进同一个框，中间空一行 |

### B. 顶栏

| # | 位置 | 中文 | English | 宽度 / 备注 |
|---|---|---|---|---|
| 11 | index.html:38 | 等待连接… | Starting… | 57 → 55 |
| 12 | app.js:195 idle | 待机 | Ready | 24 → 38 |
| 13 | app.js:195 connecting | 连接中… | Connecting… | |
| 14 | app.js:195 live | 直播中 | Live | |
| 15 | app.js:195 ended、viewer.js STREAM_TEXT.ended | 直播已结束 | Stream ended | 60 → 85 |
| 16 | app.js:195 error | 出错了 | Error | |
| 17 | app.js:195 offline | 与本地服务断开，重连中… | Reconnecting… | 141 → 91。完整说明放在横幅里（见 #75） |
| 18 | app.js:878 | 直播中 · @x / 连接中… · @x | Live · @x / Connecting… · @x | 胶囊整体 170 → 159 / 179 → 211 |
| 19 | app.js:2240 | 报警开 | Alerts on | 52 → 74（含内边距），见 §4 R6 |
| 20 | app.js:2262 | 品牌 · {name} | Brand · {name} | 143 → 154 |
| 21 | index.html:55–62 | 字幕译成（标签 / aria）；title 见 §1 | Captions in | 2026-10-20 由「目标语言 / Translate to」改名。直播中 ≤1080px 时隐藏，首页一直露出；52 → 68（G10 实测，旧的 Translate to 是 74） |
| 21a | index.html #target-hint、app.js showTargetHint | 正文同 #21 的 title（同一对，不新增句子）；按钮字是那种语言的自称（中文 / English，translate="no"）；按钮 aria-label：界面语言改成 {name} | 正文同 #21 的 title；aria-label：Set App Language to {name} | 2026-10-29 加。英文界面选了中文（简体或繁体）、中文界面选了 English 时，下拉下面弹一次（每页一次，8 秒后自己收起，指针或焦点进去就不再按时收）。按钮只在待机首页给：设置分组在连接中、直播中整块藏着，界面语言只在待机时换。{name} 是语言自称，Title Case 里的 App Language 是设置行的名字（§2.1） |
| 22 | index.html:68–70 | 字幕字号（title / aria） | Caption size | |
| 23 | index.html:76 | 清空字幕 | **Clear** | 76 → 56。“Clear Captions” 要 114，放不下，见 §4 |
| 24 | index.html:76 title | 清空历史字幕 | Clear caption history | |
| 24a | app.js confirmTwice（#clear-btn、#clear-comments） | 再点确认 | **Clear?** | 两段式确认的武装态，6 秒内再点才清，到点复原（专家审查 V3，做法和时长同 #49 的「再点一次清除」、#120 的一键更新）。按钮复原后的字是「清空字幕 / Clear」「清空弹幕 / Clear Comments」。**英文为什么不照 #49 写 Click Again to Clear**：顶栏只给这颗按钮留了 Clear（57px）的宽度。2026-10-23 在 G10 的直播场景里把按钮换成各候选再量品牌标签（无头 Chrome，场景照 G10 原样）：Click Again to Clear 145px，1000px 的 live-full / live-float / 两个 -extra 剩 65 / 53 / 48 / 48px，1101px 四个也全低于 120；Confirm 74px，live-float-extra@1000 剩 96.5px，低于 100 的下限；**Clear? 64px，最窄的 live-float-extra@1000 还有 106.5px**（平时 Clear 是 113.5），1101、1199px 两档都不低于 120。Clear? 以动词开头，问号就是「再点一下」的意思，弹幕栏那颗也用同一句（同一句中文只许一种英文）。**中文写「再点确认」**（用户 2026-10-23 定，替换起初的「再点一次清空」）：四个字 76px，和平时的「清空字幕」一样宽，武装时顶栏不动。起初那句 102px，武装那 6 秒在三个场景里截状态胶囊：live-float-zh-quiet-extra@1101 约 4.5px（平时不截，G10 断言的是平时），满编的 live-float-zh@1000 和它的 -extra 从平时的约 2.5 / 13px 加到约 12.7 / 22px。换成「再点确认」后真点一下两颗按钮再量（无头 Chrome，G10 的直播场景原样、浮层照场景点开）：中文各档品牌标签和平时一样宽（报警关着的 142px，满编 160 / 143px），G10 断言状态胶囊的那些场景武装时都不截；满编两档武装时仍截约 2.5 / 13px，和平时一样多（§4.2 记的 −4px 那一档，G10 不断言）。英文不跟着改成 Confirm：74px 过不了 live-float-extra@1000 的下限（见上）；真点下去 Clear? 在那一档剩 106.5px。G10 没有武装态的场景，这些数是另量的 |
| 25 | live-ui.js:13 †index.html:85 | 手机同看 | Phone Viewing | 宽窗口才露出文字（见 §4 R2） |
| 26 | live-ui.js:15 | 手机同看 · 已打开 | Phone Viewing · On | |
| 27 | live-ui.js:16 | 手机同看 · {n} 人 | Phone Viewing · {n} | aria-label 写 “Phone Viewing, {n} watching” |
| 28 | index.html:82 title | 打开/收起手机同看面板 | Show or hide Phone Viewing | |
| 29 | index.html:158 | 换主播 | **Switch Streamer** | 63 → 126（不加省略号） |
| 30 | index.html:157 title | 不停止监听，直接换成另一个主播 | Switch to another streamer without stopping | |
| 31 | index.html:196 | 停止 | Stop | 50 → 55 |
| 32 | index.html:196 title | 停止当前直播间 | Stop monitoring this stream | |

### C. 开始页

| # | 位置 | 中文 | English | 宽度 / 备注 |
|---|---|---|---|---|
| 33 | index.html:298 | 输入 TikTok 直播间地址或 @用户名 | Enter a TikTok Live Link or @username | 232 → 约 330（按字符数估算，未实测），在 600 宽的容器里仍是一行。2026-10-15 并进了原 #34；ⓘ（#35 的按钮）挪到标题右边，同在 `.sp-title` 里居中，不放进 `<h2>`（否则读屏会把「输入说明」念进标题） |
| 34 | （已删）标题下面的说明行 | — | — | 2026-10-15 并进 #33；原来是「粘贴直播间链接，或只输入主播用户名（如 @somebody）」 |
| 35 | index.html:255 title | 输入说明 | About links | |
| 36 | index.html:268 | https://www.tiktok.com/@主播用户名/live | https://www.tiktok.com/@username/live | 占位文字 |
| 37 | index.html:269 | 开始翻译 | **Start** | 96 → 78（含内边距） |
| 38 | index.html:274 | 也可以直接粘贴 .flv / .m3u8 流地址。 | You can also paste a .flv or .m3u8 stream URL. | |
| 39 | index.html:275 | 个别直播间 TikTok 不把流地址给程序。这时把直播间链接和浏览器里的 .flv 地址一起粘进来（中间空格隔开）：前者管弹幕和词表，后者作音频源。 | For some live streams, TikTok doesn’t provide a stream URL to the app. In that case, paste the live link and the .flv URL from your browser together, separated by a space. The link is used for comments and glossaries, and the .flv URL for audio. | 第八条，只写观察和能做的事 |
| 40 | index.html:276 | 首次识别需下载模型，进度会显示在页面上。 | The speech model downloads the first time you start. Progress appears here. | |
| 41 | index.html:280 | 主播语言 | Spoken language | 52 → 104 |
| 42 | index.html:285–299 | 西班牙语 + 英语（默认）/ 西班牙语（只有西语）/ 自动检测 / 英语 / 日语 / 韩语 / 葡萄牙语 / 法语 / 德语 / 俄语 / 阿拉伯语 / 泰语 / 越南语 / 印尼语 / 中文 | Spanish + English (default) / Spanish Only / Detect Automatically / English / Japanese / Korean / Portuguese / French / German / Russian / Arabic / Thai / Vietnamese / Indonesian / Chinese | 这是界面文字，要翻。换主播面板 #switch-source-echo 会回显选中项的文字，自然跟着变 |
| 43 | index.html:303 †184 | 本场品牌 | Brand | 52 → 36 |
| 44 | index.html:311 †186、brand.js:98 | 不限（默认） | Any (default) | 三处都要改 |
| 45 | index.html:310 †185 title | 整场只卖这个牌子时选它：加载该品牌的商品词表。混卖别家货时选「不限」。 | If this stream sells only one brand, choose it to load that brand’s product glossary. For mixed brands, choose Any. | |
| 46 | index.html:315 | 打开词表文件夹（aria）/ 打开 brands 文件夹，把品牌词表文件放进去（title） | Open Brands Folder / Open the brands folder to add brand glossary files | |
| 47 | index.html:326–328 | 整场只卖一个牌子时，放入它的商品词表可提高商品名翻译准确率。[打开 brands 文件夹] · 放好后点一下下拉即可，无需重启 | If a stream sells only one brand, add that brand’s product glossary to translate product names more accurately. [Open Brands Folder] · Then click the menu. No restart needed. | |
| 48 | index.html:336 †180 | 最近的直播间 | Recent Streams | |
| 49 | index.html:337、app.js:1634/1641 | 清除记录 / 再点一次清除 | Clear History / Click Again to Clear | |
| 50 | app.js:1599 | 点击开始翻译 @x | Start with @x | chip 的 title |
| 51 | index.html:346 | 设置 | Settings | |
| 52 | index.html:495 | 上一场字幕 | Previous Session | |

### D. 设置分组的四行

| # | 位置 | 中文 | English | 宽度 / 备注 |
|---|---|---|---|---|
| 53 | index.html:363 / 364 | 启动自检 / 自检中… | Startup Check / Checking… | 56 → 93 |
| 54 | settings-rows.js:17–19 | {n} 项功能未生效 / 通过 · {n} 项提醒 / 全部通过 | {n} features aren’t working / Passed · {n} warnings / All passed | 复数写法见 §2.3；全部通过 2026-10-15 起不带「· {n} 项」（检查项的总数每个版本都在变，对中控没有信息量），失败和提醒照旧 |
| 55 | selfcheck.py:597–607 | 音频组件 ffmpeg / 人声降噪 / 语音识别 / 翻译引擎 / 违禁词表 / 领域词表 / 审计日志 / 直播流解析 / 浏览器登录态 / 观众弹幕 / 磁盘空间 | Audio (ffmpeg) / Noise Reduction / Speech Recognition / Translation Engine / Banned-Term List / Glossary / Audit Log / Stream Lookup / Browser Login / Comments / Storage | 最宽 120px，见 §4 R5。pipeline.py:1513 用中文名做匹配的地方要改成按 id |
| 56 | selfcheck 常见 detail | 实测可用（内置）/ 降噪模型加载失败 / 重启程序后再试 / 已缓存 / 可写入 logs/ / {n} 条 / 剩余 {n} GB | Working (built-in) / Couldn’t load the noise reduction model / Restart the app and try again. / cached / Can write to logs/ / {n} terms / {n} GB available | 截图 3 里那一组 |
| 57 | index.html:384 | 翻译引擎 | Translation Engine | 56 → 119 |
| 58 | index.html:391–398 | 自动（优先本地，离线免费）/ 本地 Hy-MT2 1.8B / 本地 Hy-MT2 7B（更准，更吃内存）/ DeepL API / Claude API / OpenAI 兼容 API / Google 免费接口 / 不翻译，只显示原文 | Automatic (local first, free and offline) / Local Hy-MT2 1.8B / Local Hy-MT2 7B (more accurate, uses more memory) / DeepL API / Claude API / OpenAI-Compatible API / Google (free) / Don’t Translate (show original) | |
| 59 | app.js:2539 NOTES | auto / hymt2 / hymt2-7b / deepl / claude / openai / google / none 的说明 | auto: Uses a local model: fully offline, unlimited, and captions never leave this computer. · hymt2: Local model, offline and free. Good enough for most computers. · hymt2-7b: Local model with more accurate terms. It shares memory with speech recognition and can slow alerts. · deepl: Captions are sent to DeepL. Free usage is what’s shown here; your DeepL plan sets the billing cycle and renewal. · claude: Captions are sent to Anthropic and billed by usage. · openai: Captions are sent to this API’s provider and billed by usage. · google: Captions are sent to Google. Google caps how many requests one IP address can make. · none: Shows the original text without translating. | †index.html:405 也有一份 auto 的说明 |
| 60 | index.html:401、app.js:2606 | 粘贴 API 密钥 / 已填 {tail}（留空则沿用） | Paste API key / Key ending in {tail} saved (leave blank to keep) | 占位文字 |
| 61 | index.html:402、app.js:2620/2626 | 保存 / 切换中… | Save / Switching… | |
| 62 | settings-rows.js:30/34/37 | 不翻译 / · 免费额度已用 {pct}%（按近期速度约剩 {h} 小时）/ 已回退 · | No translation / · {pct}% of free quota used (~{h} hr left) / Fallback · | 用短写法，见 §4 R11 |
| 63 | index.html:422 | 违禁词报警 | Banned-Term Alerts | 70 → 130 |
| 64 | settings-rows.js:48/49 | 开启 / 关闭 · 词表 {n} 条 / 词表为空 · 检查中… | On / Off · {n} terms / List empty · Checking… | 99 → 86 |
| 65 | index.html:430 | 命中时弹出报警 | Show alert on match | 开关标签，Sentence case |
| 66 | settings-rows.js:51/53/54 | 三种说明 | On: Listens to what the streamer says and alerts on a match right away. Translation speed doesn’t affect alerts. · Off: No alerts during the stream. Matches are still written to the audit log. Turn this on to get alerts. · Empty: The banned-term list is empty, so no alerts will be raised. | |
| 67 | index.html:437 | 编辑项目文件夹里的 banned_terms.txt，一行一个词或短语，保存后点「停止」再「开始翻译」即可生效。命中时页面顶部会弹出红色报警并写入审计日志。 | Edit banned_terms.txt in the app folder, one word or phrase per line. Save, then click Stop and Start to apply. Matches show a red alert at the top of the window and are written to the audit log. | 2026-10-26 以前中文写「红色警报」（专家审查 V5，见 §1.3「报警」） |
| 68 | index.html:455 | 磁盘空间 | Storage | 56 → 52 |
| 69 | app.js:1667 | 剩余 {free} · 本机模型与日志 {total} | {free} available · Models and logs {total} | 238 → 约 250 |
| 70 | app.js:1676、index.html:462 | 正在用 / 本程序 / 非本程序 + 说明 | In use / This app / Not this app · Items marked In use can’t be deleted. Models marked This app download again when needed. Items marked Not this app may be used by other tools, so check before deleting. | 标签和说明里的用词必须逐字一致。「自动重下」只说模型（中文「标『本程序』的模型删了……」、英文 Models marked）：审计日志和识别音频片段也标「本程序」，删了就没了，各自的说明写着 |
| 71 | index.html:463、app.js:1719/1740/1738 | 删除所选 / 删除所选（{n} 项，{size}）/ 删除中… / 确定删除以下内容？删了就没有了。 | Delete Selected / Delete {n} Items ({size}) / Deleting… / Delete these items? This can’t be undone. | |
| 144 | diskspace.py inventory（kind=evidence） | 识别音频片段（{n} 段，不含本场）/ 被标记、命中违禁词、点过重译的段和少量对照段的直播混音，只存在这台电脑上，超过 {mb} MB 或 {d} 天从最旧的删起。删了不影响程序 | Recognition audio clips ({n} clips, not including this session) / Mixed stream audio from flagged segments, banned-term matches, retranslated captions and a few comparison segments. It stays on this computer, and the oldest clips are removed past {mb} MB or {d} days. Deleting them doesn’t affect the app. | 「磁盘空间」列表里的一行，和两行审计日志一样是界面句子（app.js 按 kind 不给它标 translate="no"）。单数写 1 clip。上限的两个数取当前设置（默认 1024 MB、14 天）。只写存了什么、在哪、什么时候删，不写为什么会被标记 |

### E. 换主播浮层

| # | 位置 | 中文 | English | 宽度 / 备注 |
|---|---|---|---|---|
| 72 | index.html:166 | 换主播 | Switch Streamer | 标题 |
| 73 | app.js:2129 | 当前 @x，确认前不会中断 / 确认前不会中断当前监听 | Now monitoring @x. Nothing changes until you confirm. / Monitoring continues until you confirm. | 12px 字号，会折成两行，可以接受 |
| 74 | index.html:174 | 新主播的直播间地址或 @用户名 | New streamer’s live link or @username | 199 → 245 |
| 75 | switch.js:94–103 | 确认换主播 / 换到 @B / 再点一次：改听 @B（停止监听 @A）/ 正在监听的就是 @A | Switch Streamer / Switch to @B / **Click Again to Switch to @B** / Already Monitoring @A | 武装态去掉括号那半句（@A 已经写在上面的副标题里）：415 → 283px。中文版这里本身就会溢出，见 §4 R3 |
| 76 | app.js:2100/2103 | @x（当前）/ 填入 @x，再点一次确认才会换 | @x (current) / Select @x. You’ll still need to confirm. | 换主播浮层里最近直播间 chip 的 title。2026-10-26 以前中文写「填入并武装改听 @x（还要再点一次确认才会真的换）」（专家审查 V5）：「武装」是代码里两段式确认的叫法，不是给中控看的词 |
| 77 | index.html:190 | 沿用：主播语言 {x} | Keeps spoken language: {x} | |
| 78 | app.js:2035 †index.html:193 | 切换期间两个主播都没有字幕，直到 @B / 新主播出现第一句。 | No captions from either streamer until @B starts speaking. / …until the new streamer starts speaking. | |
| 79 | app.js:424/2168 | 认不出这个输入：请粘贴直播间链接，或输入主播的英文用户名（到主播主页复制 @ 后面的部分，中文昵称不行）。 | This isn’t a live link or username. Paste the live link, or enter the streamer’s username, which is the part after @ on their profile. Display names won’t work. | 开始面板里写在输入框下面（#room-input-error，输入框标 aria-invalid）。2026-10-23 以前走状态横幅、胶囊同时变红「Error」，专家审查 U9 改成就地提示。换主播浮层里写在 .switch-error |
| 80 | app.js:2172 | 与本地服务断开，正在重连——稍候再试。 | Lost connection to the local service. Reconnecting… Try again in a moment. | |

### F. 手机同看浮层

| # | 位置 | 中文 | English | 宽度 / 备注 |
|---|---|---|---|---|
| 81 | index.html:100 | 手机同看 | Phone Viewing | 标题，56 → 104 |
| 82 | index.html:103 | 手机连不上时 | If a phone can’t connect | ⓘ 按钮的 title |
| 83 | app.js:1831 †index.html:108 | 已打开 / 未打开 | On / Off | |
| 84 | index.html:111 | 收起面板 | Close panel (sharing stays on) | aria-label 写 Close |
| 85 | app.js:1844 †index.html:117、viewer.py:406 | 打开后，连着同一个 Wi-Fi 的手机可以扫码看字幕和报警，只能看，不能操作本程序。 | When this is on, phones on the same Wi-Fi can scan to view captions and alerts. They can only view, not control the app. | |
| 86 | index.html:119 | 开始同看 | Start Sharing | |
| 87 | app.js:1866、viewer.py:408 | 已打开。手机连同一个 Wi-Fi，扫下面的二维码，或直接打开：{url} | On. On a phone connected to the same Wi-Fi, scan the QR code or open: {url} | |
| 88 | index.html:127–131 | 复制链接 / 换一个链接 / 停止同看 | Copy Link / New Link / Stop Sharing | 79 + 74 放一行，Stop Sharing 另起一行，和中文排法一样 |
| 89 | app.js:1869、viewer.py:409 | 当前 {n} 人在看，最多 {m} 人。 | {n} watching (limit {m}) | 152 → 约 120 |
| 90 | app.js:1868、viewer.py:410 | 这个链接里带着一把钥匙，当密码看待；发给谁，谁就能看到字幕和报警。 | This link contains an access key. Treat it like a password. Anyone who has it can see captions and alerts. | |
| 91 | app.js:1902、viewer.py:421 | 第一次打开时，系统可能弹出…请选允许。 | The first time you turn this on, macOS may ask to allow incoming network connections, or Windows Firewall may ask for access. Choose Allow. | |
| 92 | app.js:1891、viewer.py:420 | 本机地址已从 {old} 变为 {new}，之前发出去的链接需要重新扫码 | This computer’s address changed from {old} to {new}. Phones need to scan the new QR code. | |
| 93 | viewer.py:417 | 端口 {ports} 都没能打开监听，系统返回：{err}。已保持关闭。可以关掉占用这些端口的程序后再打开一次。 | Couldn’t open a port for Phone Viewing (tried {ports}). Details: {err}. Phone Viewing is still off. Quit apps that use those ports, then try again. | `{err}` 可能是系统给的任意语言，放在 Details 后面。`{ports}` 可能只有一个（8766）也可能是一段（8766–8770），所以写成 tried {ports}，不写 ports {ports} |
| 93a | viewer.py NOTE_NO_IP | 已打开，但没读到本机的局域网地址（只读到回环地址），所以没出二维码。程序每分钟重读一次地址：把这台电脑连上手机所在的 Wi-Fi，读到地址后二维码就会出来；想马上重读，点「停止同看」再点「开始同看」。 | On, but the app couldn’t find this computer’s local network address (only the loopback address), so there’s no QR code. The app checks the address again every minute. Connect this computer to the phones’ Wi-Fi, and the QR code appears once the address is found. To check right away, click Stop Sharing, then Start Sharing. | 2026-10-26（专家审查 V5）以前叫中控在手机上手工输入 `http://<本机地址>:{port}/#k=<没显示的钥匙>`：这时载荷里没有口令、页面上也没有地方显示它，照着做做不成。「每分钟」是同看开着时重读地址的间隔（`IP_REFRESH_SEC`，`tests/test_ui_wording.py` 钉着两边一致）；只写看到了什么和能做的事，不猜为什么读不到 |

### G. 直播中的字幕区和弹幕列

| # | 位置 | 中文 | English | 宽度 / 备注 |
|---|---|---|---|---|
| 94 | live-ui.js:30 †index.html:489 | 正在连接 @x… / 正在连接… | Connecting to @x… / Connecting… | 161 → 194 |
| 94a | live-ui.js connectHint / modelDownloadText（字幕区中间的卡片） | 正在下载识别模型… {n}% / 正在下载识别模型… 已下载 {n} MB / 正在下载识别模型… | Downloading speech model… {n}% / Downloading speech model… {n} MB so far / Downloading speech model… | 2026-10-23（专家审查 U7）：首次下载识别模型时，status 带结构化的 progress（kind 为 model_download）才写这一句，不带时照旧是 #94；不看 status 的 detail，所以卡片不重复顶部横幅那整句（横幅里还有模型名、MB 和「仅首次需要」）。没有百分比（模型大小未知，后端只给已下载的 MB）时写「正在下载识别模型… 已下载 {n} MB / Downloading speech model… {n} MB so far」，和横幅那句大小未知时的说法一致；MB 也没有时只写前半句。百分号紧贴数字，数字和 MB 之间留空格（§2.3） |
| 94b | live-ui.js connectHint（字幕区中间的卡片） | 等待第一句字幕… | Waiting for the first caption… | 2026-10-23（专家审查 U8）：直播中、这一场还没出过字幕时写，第一句一到就收起；和浮窗的占位（#137）同一对中英文。直播中点过清空字幕不算「还没出过」 |
| 95 | index.html:501 | 观众弹幕 | Comments | 52 → 70（13px/600）。不用 “Viewer Comments”（119）。2026-10-15 起标题后面不再有条数（列表封顶 100 条，数字一直停在 100） |
| 96 | app.js:1336–1358 | 评论流已连接 / 正在连接评论流… / 评论流断开，重连中… / 未连接 | Connected / Connecting… / Disconnected. Reconnecting… / Not connected | 放在 Comments 标题旁边，上下文已经够了。2026-10-15 起「评论流已连接 / Connected」不再画在标题旁边：`#comment-source` 整个收起，这句话写进 Comments 标题的 title；其余状态照旧 |
| 97 | app.js:1338/1358 | 已连接，等待观众发评论… / 开播后自动连接评论流 | Connected. Waiting for comments… / Comments connect when the stream starts. | 空状态文字 |
| 98 | index.html:504/505 | 收起 / 清空弹幕（title：清空历史弹幕） | Hide / Clear Comments（title：Clear comment history） | 收起的 title 写 “Hide comments to widen captions”。清空弹幕 2026-10-23 前英文只写 Clear、没有 title（专家审查 V3），见 §1.3 |
| 99 | index.html:514 | 弹幕（悬浮入口）/ 展开观众弹幕 | Comments / Show comments | 2026-10-15 起入口上不带条数；未读红点（`#comment-fab-guard`）照旧 |
| 100 | index.html:521 | 回到最新 | Jump to Latest | |
| 101 | app.js:1016/1017/1109 | 重译 / 重译中… / 重译失败 / 用更强的模型重新翻译这一条 | Retranslate / Retranslating… / Couldn’t retranslate / Retranslate with the stronger model | 悬停时才出现的按钮，68+16px |
| 101a | app.js receiveCommentRedo（那条弹幕下面的 .redo-note.done，「弹幕留意」抽屉里那一行也有）；comment_redo.py note_done | 已重译 / 重译结果和原来一样 | Retranslated / The retranslation is the same as before. | 2026-10-26（用户要的）：弹幕重译成了，在那条弹幕下面停几秒。「已重译」和手机重译按钮同一句（G3）；标点见 §2.2「短状态语和整句同在一处」。成了不是提醒，样子是次要文字、没有提醒底色（style.css .redo-note.done）；没重译成的照旧用提醒底色 |
| 102 | app.js:1138/1146 | 翻译中… / 翻译失败 / 翻译已跳过（积压） | Translating… / Translation failed / Skipped (backlog) | |
| 103 | session-divider.js:27 | ── 14:01:29 以下为 @x ── / 以下为新的一场 | ── 14:01:29 Now monitoring @x ── / New session | |
| 104 | index.html:225/227、app.js:1222、live-ui.js:62 | 疑似违禁词 / 清空报警 / 上一场 / 精确·变体·疑似 | Possible Banned Terms / Clear Alerts / Previous / Exact · Variant · Similar | 2026-10-26 以前按钮写「清空警报」、exact 档写「命中」（专家审查 V5，见 §1.3） |
| 105a | live-ui.js captionStateChip / liveBarChip，app.js paintLiveState | （大字幕下面的角标，与历史卡片同一句）翻译失败 / 未翻译（接口拒答） / 未翻译（结果不像译文） / 翻译已跳过（积压） | Translation failed / Not translated (API refusal) / Not translated (the result didn’t look like a translation) / Skipped (backlog) | 2026-10-15 起大字幕也画角标，字取自同一个函数，卡片和大字幕不会分叉。只写看到的结局，不写原因（第八条）。还在等译文（翻译中…）的不画角标：每句都要走几秒，会闪 |
| 105b | live-ui.js captionAgeText，app.js paintLiveLag | 约 {n} 秒前 / 约 {n} 分钟前（悬停：从这一句的声音结束算起，超过 15 秒才显示） | About {n} sec ago / About {n} min ago（hover: Counted from when the audio for this line ended. Shown after 15 sec） | 按 ts − e2e_ms（这一段音频结束的时刻）算，每秒涨一次。**超过 15 秒才出现**：健康的直播里一句上屏时是 2～3 秒，下一句 8～10 秒后来，换句前最老 12 秒上下（2026-10-13～15 的 3216 次换句：p50 10.4、p99 13.0 秒），所以平时这一行不在，它出现就是这一句停得比平常久（主播停了，或者积压了——只写数字，不猜是哪一种）。不到 15 秒、负数、缺 e2e_ms、超过 6 小时不写。不从最早一个词算：那样上屏就是 10 秒上下、一直在 10～18 秒之间转圈，中控没有基线。数字和单位不变形，不用分单复数 |
| 105 | app.js:1297、viewer.js:724/819 | 译文失败（{why}）——请看上面的原话 | Couldn’t translate ({why}). See the original above. | 报警译到本场的目标语言，不一定是中文，中文也只说「译文」。手机报警的「译文正在补…」写 Translating…，不提具体语言 |
| 134 | live-ui.js jumpLatestCaptionsText / jumpLatestCommentsText | 回到最新 · {n} 条新字幕 / 回到最新 · {n} 条新弹幕（没有新的就只写「回到最新」） | Jump to Latest · {n} new captions / Jump to Latest · {n} new comments (nothing new: Jump to Latest) | 按钮名 Title Case，条数用 ` · ` 接在后面、小写（同 Passed · 1 warning）；字幕叫 captions，弹幕叫 comments（§1.3）。弹幕栏有自己的一颗，字幕区那颗不数弹幕 |
| 145 | live-ui.js captionAsrChip，app.js renderCaption / paintLiveAsr（卡片 .meta 行、#live-asr） | 语音识别不确定，建议查看原文（悬停：识别出的字数和有声时长对不上。/ 识别置信度偏低。/ 这一段没有拿到识别置信度。/ 识别出的文字有重复。/ 这一段的识别被标记为不确定。+ 原文和译文都照识别结果显示，没有删改。） | Recognition unclear · check the original（hover: The word count didn’t match how long there was sound. / Recognition confidence was low. / No recognition confidence was reported for this segment. / The recognized text repeats. / This segment’s recognition was marked unclear. + The original and translation are shown as recognized, with nothing removed.） | **默认关**（settings.json 的 asr_uncertain_chip）。胶囊 sentence case、不加句号，分隔用 ` · `，后半是祈使句、不写 please（§0、§2.2）。图标是线性三角 #i-warn，不用 ⚠️（桌面界面禁 emoji）。悬停说明按命中的规则逐句写观察，每句都是整句（nan_lp 是没拿到置信度，不写成「偏低」），最后接同一句「照原样显示」；认不出的规则代码用笼统那一句。「有声时长」不写成「说话时长」：切段器量的是能量，背景音乐也算（第八条）。手机页、浮窗、报警面板都不显示 |
| 146 | live-ui.js coverageChip，app.js paintLiveCoverage（#live-cov） | 近 {m} 分钟收到 {p}%（悬停：近 {m} 分钟里实际收到的直播音频占比；本场已中断 {n} 次） | Last {m} min: {p}% received（悬停：Share of stream audio received in the last {m} min. 1 interruption / {n} interruptions this session.） | 2026-10-19：底部大字幕元信息行的角标，只在跨轮覆盖率偏低（#121d 挂着的同一个条件）时出现；数字随 stats 来（coverage_pct、coverage_window_min、coverage_low、breaks），字在页面上成对写。手机页、浮窗不画 |
| 135 | app.js forgetOnServer | 与本地服务断开，只清空了本页——重新连上后这些字幕会回来。（弹幕同） | Lost connection to the local service. Only this page was cleared, and the captions come back once it reconnects. (comments: same) | 点「清空」时服务端收不到的说明，显示在页脚版本号那一行；第八条：只写观察到的（连接断了）和结果，不猜原因 |

### H. 常见的提示条和横幅

| # | 位置 | 中文 | English |
|---|---|---|---|
| 106 | pipeline.py:1456 | 自检：{name} 未生效——{fix} | Startup Check: {name} isn’t working. {fix} ——截图 8：“Startup Check: Noise Reduction isn’t working. Restart the app and try again.”（13px/600 下约 450px，1000 宽的窗口里一行放得下） |
| 107 | pipeline.py:968 | ⚠️ 识别开始落后（积压 {n} 秒），报警会相应延迟 | ⚠️ Speech recognition is falling behind ({n} sec backlog). Alerts will be delayed. |
| 108 | pipeline.py:961/965 | 🔴 检测已降级：识别落后 {n} 秒；… | 🔴 Detection degraded: recognition is {n} sec behind. {k} audio segments older than {m} sec were dropped and weren’t checked for banned terms. |
| 109 | pipeline.py:970 | ✅ 识别已追上，检测恢复正常 | ✅ Speech recognition caught up. Detection is back to normal. |
| 110 | pipeline.py:667/673/784/828 | 已收到指令，正在连接… / 正在切换到 @B：先停止 @A 的监听… / 正在停止… | Connecting… / Switching to @B: stopping @A, then connecting… / Stopping… | 2026-10-15 起停止完成后不再发「已停止。输入直播间地址可重新开始。」（Stopped. Enter a live link to start again.）：状态是空 detail，横幅收起 |
| 111 | pipeline.py:2241/2356/2357 | 正在解析直播流地址… | Getting the stream URL… | 2026-10-15 起收到首帧音频后不再发「已连接直播间，开始实时识别（人声降噪已开启）」（Connected. Transcribing live (noise reduction on).）：状态是空 detail，横幅收起，状态胶囊的「直播中」已经说了。演示模式那句「演示模式：内置台词模拟直播字幕（未连接真实直播）」照旧显示，它要提醒这不是真的 |
| 112 | pipeline.py:2131/2219 | 上一场字幕在下方。 | The previous session’s captions are below. | 2026-10-15 由「直播已结束。可以往下翻看这一场的字幕，或在上方输入新的直播间地址。」缩短；「直播已结束」胶囊已经说了，输入框本来就在上面。「上一场」按 §1.2 写 previous session，跟标题 #52 一致 |
| 113 | pipeline.py:154–158 | 4003110 整段 | TikTok didn’t provide a stream URL for this live stream (code 4003110). Retried {n} times. TikTok doesn’t say why. Sometimes it works if you click Start again a little later. Sometimes it doesn’t work for the whole stream. {browser advice} To watch now, paste the live link and the .flv URL from your browser together, separated by a space. A .flv URL usually works for about two weeks. |
| 114 | pipeline.py:1683 | TikTok 暂时没有把这个直播间的流地址给程序，{s} 秒后自动重试（第 {i}/{n} 次）… | TikTok didn’t provide a stream URL for this live stream. Retrying in {s} sec (attempt {i} of {n})… |
| 115 | pipeline.py:2400 | 直播流多次中断且自动重连失败——可能直播已结束，或网络不稳。… | The stream was interrupted several times and couldn’t reconnect. Check that the stream is still live and your network is working, then click Start. |
| 116 | app.js startStream / connect（ws.onclose，断开超过 20 秒） | 与本地服务断开，正在重连——稍候再点「开始翻译」 / 超过 20 秒没有连上本地服务。如果程序已经关闭，请重新打开：macOS 双击「TikTok Live Translator.app」，Windows 双击「Start.bat」。 | Lost connection to the local service. Reconnecting… Click Start again in a moment. / No connection to the app’s local service for over 20 seconds. If the app has quit, reopen it. On Mac, double-click TikTok Live Translator.app. On Windows, double-click Start.bat. |
| 117 | app.js trackTranslateHealth | 连续多条字幕没有译出来，字幕先显示原文（语音识别不受影响）。一直这样的话，点「停止」后可以在「设置 > 翻译引擎」里换一个引擎。 | The last several captions couldn’t be translated, so captions show the original text. Speech recognition isn’t affected. If this continues, you can choose another engine in Settings > Translation Engine after you click Stop. |
| 118 | pipeline.py:3826 | {engine} 返回 HTTP 429，程序暂停请求 {s} 秒后自动重试… | {engine} returned HTTP 429. Requests are paused for {s} sec, then retried automatically. Captions show the original text meanwhile. Banned-term alerts aren’t affected. |
| 119 | pipeline.py:1946 | {t} 电脑休眠或挂起了约 {d}… | {t} This computer was asleep for about {d}. The app wasn’t running, so that part of the stream wasn’t monitored. During streams, keep the computer plugged in with the lid open. |
| 120 | app.js:530–538、516/524 | 发现新版本 / 一键更新 / 更新说明 / 前往下载新版本 / 再点一次确认更新：更新期间监听暂停，更新完自动恢复 / 更新中… | A new version is available / Update Now / Release Notes / Download New Version / Click Again to Update. Monitoring pauses and resumes afterward. / Updating… |
| 120a | app.js showUpdate | 有新版本 {v}，停止监听后在首页查看 | Version {v} is available. Check the home screen after you stop monitoring. | 2026-10-15 起直播中「有新版本」条不摆；中控在直播中点页脚版本号手动检查、找到新版本时，页脚写这一句作答（其余检查结果「已是最新版本」「检查更新失败」本来就在页脚）。自动检查到的不写，静默提示不写。{v} 沿用 #120 的写法，带 v 前缀；英文不重复 “Update”，因为直播中没有更新入口 |
| 120b | app.js showZipKeepNote（ZIP 安装的更新条，多出来的一行） | 更新时不要直接替换旧文件夹：先把旧文件夹里的 settings.json、banned_terms.txt、glossary.txt，logs/、transcripts/、comment_guard/local/、comment_guard/marked/，以及 profiles/、brands/ 里你自己的 .txt 文件复制到新文件夹。 + 链接「怎么更新」 | Don’t replace the old folder when you update. First copy settings.json, banned_terms.txt, glossary.txt, logs/, transcripts/, comment_guard/local/, comment_guard/marked/ and your own .txt files in profiles/ and brands/ from the old folder into the new one. + link **How to Update** | 2026-10-26 补进术语表（b4-banners，专家审查 I2）。文件名、文件夹名照抄不翻（§1.4）；清单和 README「更新（ZIP 安装）」一节是同一份。链接名 Title Case，指向 README 里那一节 |
| 121 | （已删）底部延迟统计行 | — | — | 2026-10-15 用户觉得是噪音，整行删掉。积压期间的落后和丢段走 health 提示，识别连续出错走 incident；单次识别出错、识别超时次数、积压追上之后的丢段总数只进审计和终端 |
| 121a | pipeline_stream.py `_no_audio_text` | 已 {d}没收到直播音频，这段时间没有经过检测。程序还在自动重试，可以在 TikTok 上看一眼直播是否还在进行。 / 这段时间里有收尾时晚到的音频：已 {d}没有实时收到直播音频，期间只有晚到的 {t}经过了检测。程序还在自动重试，可以在 TikTok 上看一眼直播是否还在进行。 | No stream audio for {d}. Nothing in that time was checked. The app keeps retrying. You can check on TikTok whether the stream is still live. / No stream audio in real time for {d}. Only {t} arrived in that time, late, and it was checked. The app keeps retrying. You can check on TikTok whether the stream is still live. | 2026-10-19：持续提示（incident `session:no_audio`），从本场第一次起 ffmpeg 起算、2 分钟出现、第一帧到了就撤；「本机连不上 www.tiktok.com」那条挂着时不出。{d} 用 `_duration_text`，按墙钟算（合盖多久就说多久）。不写为什么没有音频（第八条）；「重试」不写「重连」，等房间恢复在播时也成立。收尾时 ffmpeg 吐出来的音频（晚到的，送去识别、做了违禁词检测，但不算实时收到）在这段时间里有 1 秒以上时换第二句：不能说「这段时间没有经过检测」；{t} 也用 `_duration_text`。「实时」写 in real time，「晚到」写 late |
| 121b | pipeline_stream.py `_reconnect_text`（V2） | 直播流中断，{d} 秒后自动重连（本场第 {k} 次中断）… / 直播流中断，{d} 秒后用同一个流地址重连（本场第 {k} 次中断）… / 直播流中断，正在重新查流地址（本场第 {k} 次中断）… | The stream was interrupted. Reconnecting in {d} sec (interruption {k} this session)… / The stream was interrupted. Reconnecting to the same stream URL in {d} sec (interruption {k} this session)… / The stream was interrupted. Looking up the stream URL again (interruption {k} this session)… | 2026-10-19 重连规则 V2（`RECONNECT_V2`）的状态行，替换 #115 之前那句「（第 N/M 次）」——新规则下 N/M 不再对应「还剩几次就放弃」。V1（`TLT_RECONNECT_V2=0`）照旧是「直播流中断，{d} 秒后自动重连（第 {n}/{m} 次）…」 |
| 121c | pipeline_stream.py `_reconnect_text`（V2，到了上限） | 直播流中断，{d} 秒后用同一个流地址重连（本场第 {k} 次中断）。为少向 TikTok 发请求，程序每小时最多查 {n} 次流地址，{t}后再查。 / 同一个流地址拉不到数据。为少向 TikTok 发请求，程序每小时最多查 {n} 次流地址，{t}后再查；程序会一直重试。 | The stream was interrupted. Reconnecting to the same stream URL in {d} sec (interruption {k} this session). To keep requests to TikTok low, the app looks up the stream URL at most {n} times an hour. Next lookup in {t}. / The same stream URL isn’t returning data. To keep requests to TikTok low, the app looks up the stream URL at most {n} times an hour. Next lookup in {t}. The app keeps retrying. | 说的是程序自己的规矩（令牌桶，容量 + 每小时补的个数），不是 TikTok 的回应；不写为什么拉不到。{t} 用 `_duration_text`（不会出现「0 分钟」） |
| 121d | pipeline_stream.py `_coverage_text` | 近 {w}只收到 {p}% 的直播音频，缺的部分没有经过检测。本场已中断 {n} 次。 / 窗口里有晚到的音频：近 {w}只实时收到 {p}% 的直播音频，另有晚到的 {t}已检测，其余没有经过检测。本场已中断 {n} 次。 | one: Only {p}% of stream audio arrived in the last {w}. The missing audio wasn’t checked. 1 interruption this session. / other: … {n} interruptions this session. / with late audio: Only {p}% of stream audio arrived in real time in the last {w}. Another {t} arrived late and was checked. The rest wasn’t checked. 1 interruption / {n} interruptions this session. | 2026-10-19：持续提示，沿用 `session:audio_rate` 这个 id（以前每轮从零算的「过去 1 分钟只收到 {s} 秒直播音频…」删了，同一件事界面上只有一条）。跨轮滚动覆盖率低于 70% 连续两次才出现、回到 85% 以上才撤；{w} 是窗口（5 分钟，开场不足 5 分钟按已过的时间），用 `_duration_text`。「本机连不上 www.tiktok.com」或 #121a 挂着时不出。计数用 `LN`。窗口里有 1 秒以上收尾时晚到的音频时换第二句（同 #121a）：{p} 只算实时收到的，晚到的检测过，不能说缺的都没检测 |
| 121e | pipeline.py `_host_wait`（V2，等房间恢复时令牌不够复查） | {status}，为少向 TikTok 发请求，{t}后再复查（已等 {m} 分钟）… | {status}. To keep requests to TikTok low, the app checks again in {t} ({m} min so far)… | 2026-10-19：复查房间状态和查流地址共用一个令牌桶，复查还要给回到在播之后的那次查询留一个；不够时复查间隔拉长，不能再说「每 60 秒复查一次，最多 10 分钟」。说的是程序自己的规矩（同 #121c 的 To keep requests to TikTok low），不写 limit / rate limit。{t} 用 `_duration_text`。V1、令牌够时照旧是「{status}，每 60 秒复查一次，最多 10 分钟（已等 {m} 分钟）…」 |
| 121f | app.js renderStatusBanner / foldIncident（状态横幅、持续提示行尾的按钮） | 显示全部步骤 / 收起 | Show All Steps / Hide | 2026-10-26 补进术语表（b4-banners，专家审查 U10）：超过 3 行（`BANNER_FOLD_LINES`，3 行不折、4 行才折）时默认只露第一行，这颗按钮接在第一行的字后面，展开后写「收起 / Hide」。按钮名 Title Case；「收起 / Hide」和 §1.6 弹幕留意顶部条的同一个词 |
| 121g | pipeline.py `_gated_text`（#149，专家审查 M4；持续提示 `session:gated_audio`） | 近 {d}收到 {n} 秒直播音频，只有 {m} 秒送去了识别，其余的音量一直低于识别门限；程序继续监听 | In the last {d}, {n} sec of stream audio arrived, but only {m} sec was sent to speech recognition. The rest stayed below the speech recognition threshold. Monitoring continues. | 只写数字和看到的事，不说音量为什么低（第八条）。「识别门限」见 §1.3；「程序继续监听」和 quiet 那句（已收到 N 秒直播音频，但音量一直低于识别门限，没有送去识别）同一个结尾。中文的分号在英文里拆句（§2.2） |

#107–#109 的英文保留与中文相同的行首 emoji（⚠️ 🔴 ✅）：桌面提示条由 `live-ui.js` 的 `stripStatusEmoji`
统一去掉，两臂保持一样，G2 按 emoji 的多重集比。

#116、#117 是 2026-10-26 照专家审查 U4 改的：中文原来写「本地程序似乎已经关闭」「翻译服务可能暂时连不上」，猜了原因，
英文却只说观察，两边意思不一样；#117 的英文还叫人去「设置 > 翻译引擎」，而设置分组在直播中整块收起。现在中英都只写
看到的事（连续几条没译出来、20 秒没连上）和能做的事，#117 写明要先点「停止」。用户 10-22 定 D3「只管翻译失败这一条」：
这两句由 `tests/translate-fail-banner.test.mjs` 单独钉住两臂都不含「可能 / 似乎 / might / may」这类词，自动检查
（`RULE8_FAMILY`）的范围不扩大到整个桌面页。直播中出现、叫人去换引擎、换模型或填密钥的后端提示
（`pipeline.py` `_note_engine_failure` 里 401/403/404、Claude 连续 400/402、本地引擎连续出错的持续提示，以及
`_rejection_fallback` 本场改用本地模型后那句怎么换回来）同样加了「点「停止」后」/ click Stop，见 §1.3「翻译引擎」；
停止后改写的那条只统一了位置的说法。#118 的 429 暂停不叫人换引擎，不在其列。
`tests/test_i18n_pipeline_en.py` 的 `test_every_engine_hint_shown_while_monitoring_says_click_stop_first` 把这几句的中英两臂都过一遍。

### I. 手机同看页（viewer.html / viewer.js）

| # | 中文 | English | 备注 |
|---|---|---|---|
| 122 | 连接中… / 已连接 / 已断开，{n} 秒后重连… | Connecting… / Connected / Reconnecting in {n} sec… | 状态胶囊（见 §4 R4） |
| 123 | 链接已失效，请向中控要新的二维码 | Link expired · Ask for a new QR code | 305 → 222px。只用于服务端不认这把口令（关闭码 4401） |
| 123a | 手机上没有同看钥匙，请重新扫码 | No access key · Scan the QR code | 2026-10-26 加（#148 / D5 之后）：地址栏和这台手机的存储里都没有口令时（从主屏幕图标打开读不到 Safari 存的那份、电脑换了地址或端口、Safari 清掉了网站数据）。这时链接并没有失效、墙上的二维码照样能用，以前却和 #123 共用「链接已失效」。只写看到的（这台手机上没有）和能做的（重新扫码），不说为什么没有（第八条）。「钥匙 / access key」同 #90。胶囊里放得下：中英都不比 #123 长（R4） |
| 124 | 同看人数已满（12 人），稍后再试 | Full (12 max) · Try again later | |
| 125 | 中控已关闭手机同看 / {n} 秒没有新消息 / 状态未知 | Operator stopped sharing / No updates for {n} sec / Status unknown | |
| 126 | 未开始 / 连接中 / 直播中 / 已停止 | Not started / Connecting / Live / Stopped | |
| 127 | 已经 {n} 秒没连上。请找中控确认同看是否还开着，或重新扫码 | Not connected for {n} sec. Ask the operator whether sharing is still on, or scan the QR code again. | can’t 不跟一段时长连用 |
| 128 | 疑似违禁词 / 清除已看过的报警 / 只在这台手机上隐藏，中控电脑和记录不受影响 | Possible Banned Terms / Clear Seen / Hides alerts on this phone only. The operator’s computer and records aren’t affected. | |
| 129 | 重译会用更强的模型重新翻这一句，稍等几秒 | Retranslation uses a stronger model and takes a few seconds. | 2026-10-13 改：字幕引擎是 Claude 时重译用的是 Claude Sonnet 5.5，不在中控电脑上 |
| 130 | 最新 / 弹幕 / 只看模式 · 不能操作 / 提示音 | Latest / Comments / View only / Sound | |
| 131 | 页面切到后台或锁屏后，手机可能停掉提示音。 | Sounds may stop when this page is in the background or the phone is locked. | |
| 132 | 演示数据，不是真实直播 / 违禁词报警已关闭（中控开播时未开启） | Demo data, not a real stream / Banned-term alerts are off for this session. | 2026-10-26 以前中文写「违禁词警示已关闭」（专家审查 V5，见 §1.3「报警」） |
| 133 | 识别开始落后，报警会有延迟 / 检测已降级，识别明显落后 | Recognition is falling behind. Alerts will be delayed. / Detection degraded: recognition is far behind. | |

### J. 浮窗（float.html / float.js）

| # | 位置 | 中文 | English | 备注 |
|---|---|---|---|---|
| 136 | float_window.py FLOAT_TITLE | 浮窗 | Floating Window | 原生窗口标题（页面 `<title>` 是 “Floating Window · TikTok Live Translator”）。窗口窄，标题栏放得下的只有前半 |
| 137 | float.html #f-empty、float.js floatPlaceholder | 等待第一句字幕… / 连接中… | Waiting for the first caption… / Connecting… | 窗口里还没有字幕时的占位，只写观察到的状态。中控页字幕区中间的卡片用同一句（#94b） |
| 138 | float.html #f-comments | 弹幕 | Comments | aria-label；窗口里没有可见的标题 |
| 139 | float.js floatCommentView | 翻译中… | Translating… | 同 #102 |
| 140 | float_window.py OPEN_FAILED | 无法打开浮窗。 | Couldn’t open the Floating Window. | 一次性提示（notice）。不写原因。正文里提到界面元素用它的 Title Case 名字（§2.1） |
| 141 | float.js（经 live-ui.js liveBarChip） | 翻译失败 / 未翻译（接口拒答） / 未翻译（结果不像译文） / 翻译已跳过（积压） | 同 #105a | 浮窗里最新一条字幕的角标，和中控页底部大字幕是同一个函数；翻译中不画 |
| 142 | index.html #float-btn | 浮窗（悬停：在始终置顶的小窗里看最新字幕和弹幕） | Floating Window（悬停：Show the latest caption and comments in a window that stays on top） | 英文默认只留图标，宽于 1400px 才露文字（英文文字约 110px，放不进 1000px 的顶栏）；图标按钮 28px + 间距 8px。中文在放得下的宽度（1000–1100px、1140–1399px，且「报警开」标签没亮）露出「浮窗」两个字、不带图标，其余窄宽度同英文，见 §4.2 |
| 143 | float.js floatCaptionMeta（#f-conn、#f-lag） | 连接中… / 约 N 秒前、约 N 分钟前 | Connecting… / About N sec ago, About N min ago | 已经有字幕时字幕上面状态行里的过时信号：同 #137 的「连接中…」（状态是连接中、或浮窗页和程序断了线），和中控页底部大字幕的「约 N 秒前」（live-ui.js captionAgeText，超过 15 秒才有）。只写观察 |
| 147 | float.js（结束卡片的标题、说明）、float.html #f-end-back | 直播已结束 / 监听已停止 · 详情见主窗口。 · 回到主窗口 | Stream ended / Monitoring stopped · See the main window for details. · **Show Main Window** | 2026-10-26 补进术语表（b3-float-end，专家审查 U3）：下播或出错时浮窗不再自己关，留一张结束卡片。标题是状态文字（sentence case、不加句号；「直播已结束」同 #15），说明是整句，按钮 Title Case（主窗口见 §1.3）。只写看到的结局，不写为什么停（第八条） |


### K. 闪购浮层（index.html #flash-btn / #flash-panel、flash.js、pipeline_flash.py）

2026-10-27 新加：顶栏「闪购」按钮和只读浮层，显示本机闪购助手（另一个程序）的状态。后端只发代码、数字和闪购助手的原话，
文案全在 `web/flash.js` 按代码映射。只写看到的状态，不写原因（同第八条的规矩，flash.js 不在第八条家族里）。
商品标题标 `translate="no"`，「最近记录」的正文是闪购助手写的，带 class `i18n-data`、原样显示不翻。认不出的代码原样显示（放进 `translate="no"`）。

| # | 位置 | 中文 | English | 备注 |
|---|---|---|---|---|
| 150 | index.html #flash-btn `.btn-label` | 闪购 | Flash Sale | 英文窄于 1600px 只留图标；中文 1000–1399px 只露字（「报警开」亮着时只留图标），1400px 起图标加字，见 §4.2 |
| 151 | index.html #flash-btn title、#flash-panel aria-label 和标题；flash.js flashButtonState | 闪购助手 · {胶囊} | Flash Sale Helper · {chip} | 按钮的 title 同时写进 aria-label：颜色之外读屏也念得到要不要处理 |
| 152 | flash.js flashChip | 运行中 / 要处理 / 未开始 / 没在运行 / 正在查看… | Running / Needs attention / Not started / Not running / Checking… | 浮层标题旁的胶囊。「未开始」同手机页 #126（Not started） |
| 153 | flash.js flashLines（连不上） | 闪购助手没在运行。 / 127.0.0.1:9334 连不上，每 15 秒再查一次。 | Flash Sale Helper isn’t running. / Can’t connect to 127.0.0.1:9334. Checking again every 15 sec. | 契约：闪购助手关了接口就连不上。「连不上」只指连接被拒（9334 上没人在听），同 #82 写 can’t connect；连得上却不回话是 #154 的「没有回应」，不能都写 answering。连不上不变色 |
| 154 | 同上（读不出、没有回应、正在查看） | 127.0.0.1:9334 返回的内容读不出来。 / 127.0.0.1:9334 在 2 秒内没有回应。 / 正在查看闪购助手的状态… | Couldn’t read the response from 127.0.0.1:9334. / 127.0.0.1:9334 didn’t answer within 2 sec. / Checking Flash Sale Helper… | 前两句都变色。「没有回应」是后端 why=no_answer：连得上，2 秒内没回完（闪购助手卡住、被挂起时就是这样），只写看到的，不说卡住、也不说没在运行（第八条） |
| 155 | 同上（陈旧） | 闪购助手没有更新状态。 / 上次更新 {HH:MM:SS}。 / 还没有收到过状态。 | Flash Sale Helper hasn’t updated its status. / Last updated at {HH:MM:SS}. / No status has come in yet. | 超过 15 秒没更新。时刻是本机时间（§2.4） |
| 156 | flash.js flashPageText | 专用 Chrome 停在 TikTok 登录页，登录后才能继续。 / 页面上出现了验证码，要人去处理。 / 页面没有回应，或者读取出错。 / 专用 Chrome 里没有打开商品后台（LIVE Manager）。 / 商品后台没在 Buy now 列表上。 / 商品后台正在打开… / 页面状态：{code} | The dedicated Chrome is on the TikTok sign-in page. Sign in to continue. / A CAPTCHA appeared on the page. Someone needs to solve it. / The page isn’t responding, or reading it failed. / LIVE Manager isn’t open in the dedicated Chrome. / LIVE Manager isn’t showing the Buy now list. / LIVE Manager is loading… / Page status: {code} | login_required、captcha、error 七个代码。Buy now 是 TikTok 页面上的标签名，照抄 |
| 157 | flash.js flashPausedText | 闪购助手开着，但还没开始运行。 / 专用 Chrome 里弹出了确认框，核对后点确定或取消。 / 已暂停：确认框里点了取消。要在闪购助手的面板上点继续。 / 出现验证码，已暂停。 / 连续失败太多次，已暂停。 / 已暂停：{code} | Flash Sale Helper is open but hasn’t started. / A confirmation is waiting in the dedicated Chrome. Check it, then confirm or cancel. / Paused: a confirmation was canceled. Resume it from the Flash Sale Helper panel. / Paused for a CAPTCHA. / Paused after too many failures in a row. / Paused: {code} | |
| 158 | flash.js flashLines（卡住、未读、认不出的提醒、没有商品） | {n} 个商品停了超过 1 分钟。 / 最近记录里有 {n} 条新的问题。 / 要处理：{code} / 商品列表里还没有商品。 | 1 product has stayed stopped for over 1 min. / {n} products have stayed stopped for over 1 min. · 1 new problem in Recent Activity. / {n} new problems in Recent Activity. · Needs attention: {code} · No products in the list yet. | 「问题」= error 级、或 ❌ 开头的 warn 级记录；打开浮层就记成看过 |
| 159 | flash.js flashStateText | 进行中 / 两轮之间 / 已停止 / 已手动结束 / 待开始 / 暂时看不准 / 没有闪购 | Ongoing / Between rounds / Stopped / Ended manually / Ready to start / Unclear for now / No flash sale | 「已停止」同手机页 #126（Stopped） |
| 160 | flash.js flashProductParts | 还剩 {t} / {t} 后开始下一轮 / 第 {n} 轮 / 库存 {a}/{b} / 库存 {a} / 续开 {n} 次 / 不自动续开 / 等闪购助手续开 / 停了超过 1 分钟 | {t} left / Next round in {t} / Round {n} / {a} of {b} in stock / {a} in stock / Renewed once, Renewed {n} times / Auto-renew off / Waiting for Flash Sale Helper to renew / Stopped for over 1 min | 倒计时 m:ss，1 小时以上 h:mm:ss，两种语言写法一样。「等闪购助手续开」的条件和后端算卡住的一样（已停止、允许自动续开为 true、闪购助手在运行、没暂停），满 60 秒换成「停了超过 1 分钟」 |
| 161 | flash.js flashSummary | 共 {n} 个商品：已停止 {n} · 待开始 {n} · 看不准 {n} · 进行中 {n} · 两轮之间 {n} · 没有闪购 {n} · 已手动结束 {n} · 其他 {n} · 未列出 {n} | 1 product: / {n} products: {n} stopped · {n} ready to start · {n} unclear · {n} ongoing · {n} between rounds · {n} with no flash sale · {n} ended manually · {n} other · {n} not listed | 只列不为 0 的。已停止合成一项放最前，其余同商品的排序。「未列出」只在闪购助手给的商品超过后端的上限 300 个时出现（后端的 products_total 比商品表长），总数按 products_total 写，各项加起来正好是总数 |
| 162 | flash.js flashMoreText（#flash-more） | 还有 {n} 个 | 1 more / {n} more | 先列 40 个商品，点它全列 |
| 163 | index.html #flash-alerts-title；flash.js flashLevel | 最近记录 · 要处理 / 提醒 / 说明 | Recent Activity · Needs attention / Warning / Info | 级别只是图标的读屏名（八角 / 三角 / ⓘ，形状和颜色一起变） |
| 164 | index.html #flash-open、只读说明；flash.js 版本 | 打开闪购助手 / 这里只显示状态。要操作，请到闪购助手里。 / 版本 {v} | Open Flash Sale Helper / This panel only shows status. Make changes in Flash Sale Helper. / Version {v} | 断线时点「打开」，#flash-note 里写 #80 那句「与本地服务断开，正在重连——稍候再试。」 |
| 165 | pipeline_flash.py（notice，where=flash） | 没能打开闪购助手（open 返回 {rc}）。 / 没能打开闪购助手。 | Couldn’t open Flash Sale Helper (open returned {rc}). / Couldn’t open Flash Sale Helper. | 后端拼好整句，页面写进浮层里的 #flash-note。只写返回码，不猜原因 |
| 166 | index.html #font-btn（窄于 1400px 且有闪购按钮时的「Aa」） | 字幕字号（title / aria-label） | Caption size | 同 #22；按钮上的字是 Aa，两种语言一样，点开是原来的字号滑块，见 §4.2 |

---

## 4. 布局风险

### 4.1 默认窗口 1000×760，直播中的顶栏逐项宽度

场景就是截图 4：直播中，选了品牌，2 台手机在看，报警关（默认）。
顶栏布局规则（style.css:199–389）：

- `.controls` 设了 `flex:none`，不缩也不折行；
- 左边 `.brand` 可以缩：品牌标签先缩（`flex-shrink:2`，最小 48px），再缩状态胶囊（省略号）；
- ≤1100px 时「手机同看」只剩图标加人数；直播中 ≤1080px 时隐藏「字幕译成」四个字（首页一直露出，见 §4.2 最后一条）；≤600px 才允许折行。

| 项 | 中文 px | 英文 px（推荐方案） | 说明 |
|---|---:|---:|---|
| 左右内边距 16+16 | 32 | 32 | |
| `.brand` 和 `.controls` 之间的 gap | 16 | 16 | |
| 状态胶囊（点 + 文字 + 内边距） | 170 | 159 | “Live · @bellaallnatural” 比中文还窄 |
| gap | 8 | 8 | |
| 品牌标签（上限 160） | 143 | 154 | “Brand · Bella All Natural” |
| 「字幕译成」下拉 | 144 | 144 | 选项都是各语言的自称，**不受界面语言影响**。这一列是 2026-10-20 以前 13 种时量的，最宽的一项是 “Bahasa Indonesia”。现在只列四种，下拉是 116（G10 实测）；设置里存着四种以外的语言时会补一项，最宽的补出来的仍是 Bahasa Indonesia，回到 144。所以本表按 144 算的是上限。直播中的 G10 两种都量：`live-full@宽度`、`live-float@宽度` 等目标是 en（116），同名加 `-extra` 的目标是 id（144，下拉补了一项并选中它） |
| 字号控件（小 A + 滑块 + 大 A + 左右 margin） | 146 | 146 | 如果英文版隐藏两个 A，这里是 112 |
| 清空字幕 | 76 | **56**（Clear） | “Clear Captions” 要 114 |
| 手机同看（图标 + 人数） | 40 | 40 | 没有文字，不受影响 |
| 换主播（+16 右 margin） | 63+16 | **126**+16（Switch Streamer） | “Switch Streamer…” 要 137；“Change Streamer” 要 131 |
| 停止 | 50 | 55 | |
| controls 内部 5 个 gap | 40 | 40 | |
| **controls 合计** | **575** | **623** | |
| 左侧可用宽度 = 1000 − 48 − controls | 377 | 329 | |
| 左侧需要（胶囊 + 8 + 品牌标签） | 321 | 321 | |
| **余量** | **+56** | **+8** | |

几种方案对比：

| 方案 | controls | 余量 | 结果 |
|---|---:|---:|---|
| Clear + Switch Streamer（推荐） | 623 | +8 | 一行放下，但只剩 8px，和估算误差差不多 |
| 推荐方案 + 英文版在 ≤1100px 隐藏两个 A（纯装饰，`aria-hidden`） | 589 | **+42** | 稳。README 截图建议用这个 |
| Clear Captions + Switch Streamer | 681 | −50 | 品牌标签被压到约 104px，变成 “Brand · Bella…” |
| Clear Captions + Switch Streamer… | 692 | −61 | 同上，更糟 |
| 再打开报警（多一个 “Alerts on” 标签，74+8） | — | 推荐方案 −74；隐藏 A 后 −40 | 品牌标签被压到约 80–114px。中文版同样场景是 −4 |
| 连接中（还没有字幕，Clear 隐藏） | 559 | 胶囊变成 211 后仍有 +20 | 放得下 |

### 4.2 其它宽度区间

- **1081–1100px**：标签露出来（当时是 Translate to，+80；现在的 Captions in 窄 6px）。controls 703，左侧可用 330，**刚好放下**（+9）。
- **1101–1199px（英文专属风险）**：「手机同看」按钮露出文字。
  - 宽度从 40 变成 145（“Phone Viewing · 2”）；打开但没人看时（“Phone Viewing · On”）是 154。
  - 1101px 时 controls 808，左侧只剩 245，品牌标签被压到约 78px。
  - 中文版同一宽度 controls 718，余量还有 +14。
  - **建议**：英文版把这条断点从 1100 挪到 1200，可以写成 `html[lang="en"]` 作用域下的规则，
    即 ≤1199px 时只留图标。1200px 时 controls 817，可用 335，放得下。
- **「浮窗」按钮（桌面应用窗口模式的直播中）**：多一颗 28px 的图标按钮，加间距 8px，顶栏多 36px。1000px 时英文版品牌标签只有
  约 25px 余量（G10 的下限 100px），所以按钮露出时 `body.has-float-btn` 把字号滑块从 96px 收到 72px（<1400px），省回 24px；
  英文版文字窄于 1400px 视觉隐藏（读屏照念）。G10 的 `live-float@宽度` 场景用同一组下限量这个真实的顶栏，`live-float-extra@宽度` 再量一遍下拉最宽（144px）的样子。
- **中文版的「浮窗」两个字（用户 2026-10-16 定：「中文尽量显示字」）**：放得下就露字，放不下才退回只有图标；英文版不动。
  - **做法：窄于 1400px 时去掉图标、只留文字。** 图标加文字是 63px（7 + 16 + 4 + 26 + 10），比只有图标的 28px 多 35px，
    默认窗口上只剩 +9px 的余量（见下表「图标 + 字」）；只留字、左右内边距各 10px 是 46px，只多 18px。
    「浮窗」是两个 13px 的汉字，26px（同「换主播」63px = 39 + 24 的量法）。≥1400px 照旧图标加文字。
  - **样式**（`style.css`，`html[lang^="zh"] body:not(.has-alert-tag) #float-btn`，`tests/test_i18n_layout_css.py` 钉住不会漏到英文页）：
    1000–1100px 和 1140–1399px 两段露字；其余宽度、以及「报警开」标签亮着时，仍是只有图标。
    标签在 `.brand` 里、按钮在 `.controls` 里，样式选不到，所以 `updateAlertModeTag` 顺手给 `body` 加 `has-alert-tag`
    （同 `has-float-btn` 的做法，不用 `:has()`）。可访问名称不动：按钮的 `title`、`data-en-title` 和 `.btn-label` 里的文字照旧，
    文字不露出时只是视觉隐藏。
  - **余量**（正数 = 品牌标签还够宽、状态胶囊不被挤；负数时品牌标签和状态胶囊按 2:1 分摊，主播名先被截，
    中文页没有英文页那条 `flex-shrink:1000`，规格要求中文版面不动）。基线是 §4.1 的中文列，品牌标签 143px，报警关，
    有浮窗按钮的三列都已计入字号滑块 96 → 72px（−24）：

    | 窗口宽 | 没有浮窗按钮 | 只有图标（+36 −24） | 图标 + 字（再 +35） | 只有字（再 +18，采用） |
    |---:|---:|---:|---:|---:|
    | 1000 | +56 | +44 | **+9** | **+26** |
    | 1080 | +136 | +124 | +89 | +106 |
    | 1081（「字幕译成」露出，+80） | +57 | +45 | +10 | +27 |
    | 1100 | +76 | +64 | +29 | +46 |
    | 1101（「手机同看」露出文字，+63） | +14 | +2 | −33 | **−16** |
    | 1117 | +30 | +18 | −17 | 0 |
    | 1140 | +53 | +41 | +6 | **+23** |
    | 1399 | +312 | +300 | +265 | +282 |

  - **所以范围是 1000–1100px 和 1140–1399px。** 1000px 是默认窗口，也是 G10 量的最窄一档；更窄的没量过（估算里要到 974px 才降到 0），所以不露字。
    1101–1139px 这一小段「手机同看」刚露出文字（+63px），按钮带字在 1117px 才回到 0，再留一份估算误差（§4.1 说约 8px）加一点，
    所以仍是只有图标；1140px 起 +23px，把握和 1000px 的 +26px 相当。
  - **报警标签亮着就不加字。** 中文「报警开」是 3 × 12 + 16 = 52px，加间距 8px，占 60px：1000px 时只有图标就已经 −16px，
    加字是 −34px。这是这个分支原来就有的情况（浮窗按钮只有图标时，中文 1000px 报警开着是 −16px，主播名最多被截约 6px），
    不是这次引入的；报警目前搁置（默认关），所以这里没有去改版面，只是不让它更糟，**也没有断言它**：
    G10 的 `live-float-zh@1000` 只断言顶栏不溢出、字没露出、品牌标签 ≥100px。
  - **G10**：`live-float-zh-quiet@宽度`（报警关、品牌 “Bella All Natural”，和上表同一个基线）和目标在四种以外、下拉 144px 的 `live-float-zh-quiet-extra@宽度`（上表就是按 144 算的）在 1000、1100、1101、1140、1399、1400px
    量：顶栏不溢出、状态字不被截、品牌标签下限同英文（1000px ≥100，1101–1199px ≥120）、「浮窗」两个字和图标露不露出符合上表、
    只有字的按钮宽度 40–52px。1101–1139px 这一段样式不变、越宽余量越多，最紧的是 1101，所以 1117、1139 不另量。
    `live-float-zh@1000`（和 `live-float-zh-extra@1000`）是满编（报警开、长品牌名）的那一档。**这些宽度只在 CI 的 macOS 跑器上量，
    写这条时没有量过**：1140px（+23）和 1000px（+26）若实测偏窄几个像素，可调的是内边距（10 → 8px，省 4px）和 1140 这个起点。
- **「闪购」按钮和「Aa」（2026-10-27，装了闪购助手才有，首页和直播中都在）**：多一颗 28px 的图标按钮，加间距 8px，顶栏多 36px。
  按原来的余量（英文 `live-float-extra@1000` 只剩约 13px、中文 `live-float-zh-quiet-extra@1101` 约 +2px）放不下，所以同一个改动里把宽度补回来：
  - **做法（用户定的方案甲）**：有闪购按钮（`body.has-flash-btn`）、窗口窄于 1400px 时，字号控件收成一颗 28px 的「Aa」（`#font-btn`），
    点开的小浮层就是原来的字号控件（`label#font-ctl` 原地改成浮层样式，两个 A 都露出来，滑条 160px）。英文 ≤1199px 字号控件是 72 + 16 = 88px，
    中文是约 107 + 16 = 123px，换成 28px：英文净省约 24px、中文约 59px。首页和 ≤720px 不显示（同原来的字号控件）。宽窗口、没装闪购助手时一切照旧。
  - **按钮上的字**：中文 1000–1399px 只露「闪购」两个字、不带图标（46px，照「浮窗」的做法；「报警开」标签亮着时不露字），1400px 起图标加字（63px）。
    英文窄于 1600px 只有图标：1400px 起「Aa」退场、字号控件（130px）回到顶栏，Flash Sale 再露出来（按钮约 100px）的话，
    `live-flash@1400` 的品牌标签只剩 82px、`live-flash-extra@1400` 54px；只有图标时是 154 / 126px。
  - **余量**（G10 装置在本机 macOS 实测，无头 Chrome，场景照 G10 原样；正数 = `.brand` 和 `.controls` 之间还空着多少像素，
    负数 = 品牌标签、状态字一共被挤掉多少。`live-float*` 是同一个场景没有闪购按钮的样子，作对照）：

    | 窗口宽 | live-float | live-flash | live-float-extra | live-flash-extra | 说明 |
    |---:|---:|---:|---:|---:|---|
    | 1000 | −18.5（品牌 141.5） | **+5.4**（160） | −46.5（113.5） | −22.6（**137.4**） | G10 下限 100 |
    | 1100 | +7.5 | +31.3 | −20.5（139.5） | +3.3 | |
    | 1101 | +16.5 | +32.3 | −11.5（148.5） | +4.3 | G10 下限 120 |
    | 1199 | +106.5 | +130.3 | +78.5 | +102.3 | |
    | 1200 | −34.5（125.5） | +23.9 | −62.5（97.5） | −4.1（155.9） | |
    | 1400 | — | −6.2（153.8） | — | −34.2（125.8） | 「Aa」退场；英文只有图标。新加的下限 120 |
    | 1600 | — | +122 | — | +94 | 英文露出 Flash Sale |

    | 窗口宽 | live-float-zh-quiet | live-flash-zh-quiet | live-float-zh-quiet-extra | live-flash-zh-quiet-extra |
    |---:|---:|---:|---:|---:|
    | 1000 | +57.4 | +97.7 | +29.4 | +69.7 |
    | 1100 | +99.4 | +139.7 | +71.4 | +111.7 |
    | 1101 | +41.9 | +74.2 | +13.9 | **+46.2** |
    | 1140 | +54.9 | +95.2 | +26.9 | +67.2 |
    | 1399 | +313.9 | +354.2 | +285.9 | +326.2 |
    | 1400 | +273.9 | +202.9 | +245.9 | +174.9 |

    中文满编（报警开、长品牌名）1000px：`live-float-zh` −2.5（状态字被截 2.5px，§4.2「浮窗」那条记的 −4px 那一档）、`live-flash-zh` **+55.8**；
    `-extra` −30.5 对 **+27.8**。首页 `home-flash@1000/860/720` 比 `home-idle` 少 36px，`home-flash-zh@1000` 露字少 54px，最窄的 720px 还空着约 361px。
    中文露字的这一列已经计入「闪购」两个字（比只有图标多 18px）。各档都比没有闪购按钮时宽松，所以「浮窗」两个字的露法（`ZH_FLOAT_SHAPE`）一点没动。
  - **G10**：`live-flash@宽度`、`live-flash-extra@宽度`（进 `LIVE_BASES`，下限一条没放宽，另加 1400、1600 两档，1400、1600 品牌标签 ≥120）、
    `live-flash-zh@1000`、`live-flash-zh-extra@1000`（进 `ZH_FULL_BASES`）、`live-flash-zh-quiet@宽度` 和 `-extra`（进 `ZH_QUIET_BASES`，六档，
    「浮窗」露法同不带闪购的）、`home-flash@宽度`、`home-flash-zh@宽度`（`HOME_WIDTHS`）。另查：闪购按钮露出、「Aa」窄于 1400px 时约 28px、
    字号控件收起，1400px 起反过来；按钮上的字和图标露不露符合上面的规则；闪购助手写的中文记录落在 `i18n-data` 里、标题在 `translate="no"` 里。
    `live-flash`、`home-flash` 进 `DESKTOP_EN`，扫漏翻。上表是写这条时在本机量的（2026-10-27），CI 的 dom-scan 跑同一组断言。
- **≤860 / ≤720 / ≤600**：两种语言都靠现有断点逐级收起，英文版不会更糟。
  600px 以下本来就允许折行。
- **首页的「字幕译成」标签（用户 2026-10-20）**：首页（`body.home`：待机、已结束、出错）在任何宽度都露出这四个字 / Captions in，
  直播中仍是 ≤1080px 才省掉。起因是用户在默认 1000px 的首页只看到一个写着「中文（简体）」的下拉，把它当成了界面语言。
  首页顶栏只有状态胶囊、标签、下拉、「手机同看」图标（停止后还有「清空字幕」），没有字号滑块、品牌标签和换主播：
  G10 实测标签英文 68px、中文 52px，下拉 116px（补了 Bahasa Indonesia 时 144px），1000 / 860 / 720px 三档都不溢出。
  G10 的 `home-idle@宽度`（英文，目标 en）和 `home-zh@宽度`（中文，目标是四种以外的 id，下拉补了一项并选中它）钉着。

### 4.3 风险清单（按优先级）

**P1：影响默认窗口或 README 截图**

- **R1 直播顶栏只有 8px 余量。**
  - 清空字幕用 “Clear”（tooltip 写 “Clear caption history”；aria-label 不另写，原因见 §1.3），
    换主播用 “Switch Streamer”，不要加省略号。
  - 英文版在 ≤1100px 隐藏字号控件的两个 A，余量能到 42px。
  - 不要用 “Clear Captions”、“Viewer Comments”、“Start Translating” 这类长写法。
- **R2 1101–1199px 区间品牌标签被压扁。** 手机同看按钮露出文字的断点，英文版改到 1200，见 §4.2。
- **R3 换主播的确认按钮会溢出（中文版已经有这个问题）。**
  - `#switch-confirm` 是 `width:100%`，而 `.btn` 设了 `white-space:nowrap`。浮层内宽只有 388px。
  - 中文武装态「再点一次：改听 @itzesantana11（停止监听 @bellaallnatural）」估算 415px，**现在就会溢出**。
  - 英文改成 “Click Again to Switch to @B”，TikTok 用户名最长 24 字符时约 335px，放得下。
  - 建议再加一道 CSS 保险：`#switch-confirm { white-space: normal; height: auto; min-height: 36px; }`，两种语言都受益。
- **R4 手机页的状态胶囊不会缩。**
  - viewer.css 里 `.status-pill` 没设 `min-width:0`，`.conn-text` 是 `nowrap` 而且没有省略号。
  - 如果把 “Link expired. Ask the operator for a new QR code.” 放进胶囊，宽度是 305+32=337px，320px 宽的手机上会横向溢出。
  - 用 #122–#125 的短写法（最长约 222px），并给 `.conn-text` 加上省略号。

**P2：界面能用，但会难看或截断**

- **R5 自检列表的名字列不齐。**
  - `.sc-name` 是 `min-width:96px; flex:none`。英文名最长的是 Speech Recognition 120px、
    Translation Engine 110px、Stream Lookup 90px。
  - 超过 96px 的那几行，右边的 detail 起点会往右跳。
  - 英文版改成 `min-width:128px`（或者整个列表改用 grid）。
- **R6 “Alerts on” 标签。** 74px 且 `flex:none`，一出现就把品牌标签压到大约一半宽。
  - 默认是关的，而且报警目前搁置，所以只列为 P2。
  - 可以改成只有铃铛图标的胶囊（22px），完整文字放进 title 和 aria-label：“Banned-term alerts are on”。
- **R7 离线时的状态胶囊。** 完整写成 “Reconnecting to local service…” 要 185px，会跟品牌标签抢位置。
  胶囊里只写 “Reconnecting…”，完整说明交给横幅（#116）。
- **R8 底部延迟统计行会折行。** 已不适用：2026-10-15 这一行删了。
- **R9 开始页的选项行可能折行。**
  - 英文宽度：`Spoken language` 104 + 下拉 199 + 间距 24 + `Brand` 36 + 品牌下拉 119–220 + 文件夹按钮，合计 552–653px。容器是 600px。
  - 品牌名超过约 18 个字符时，品牌那一组会折到第二行。
  - 折行本身是体面的（`flex-wrap`），不算 bug。中文版不会折行。Bella All Natural 放得下。
- **R10 弹幕状态按字符数截断。**
  - app.js:1342/1353 在 80 个字符处截断再补 “…”。
  - 80 个英文字符的像素宽度不到 80 个汉字的一半，而同样的信息英文要多写 1.5–2 倍。结果英文的后端说明会在一半的地方被截掉。
  - 改成按 CSS 宽度截断（比如限制两行），或者英文版放宽到约 140 个字符。
- **R11 翻译引擎那一行的摘要被截断。**
  - 额度摘要按长写法 “(about 3 hr left at current pace)” 约 395px，加上行名 119px，超过 504px 的可用宽度。
  - 用短写法 “(~{h} hr left)”（#62）。
- **R12 Windows 上的字体。**
  - `body` 的字体栈是 `-apple-system, …, "PingFang SC", "Helvetica Neue", "Hiragino Sans GB", "Microsoft YaHei", …, system-ui`。
  - 在 Windows 上，前面几个都不存在，会先落到 **Microsoft YaHei**，英文就用雅黑自带的拉丁字形渲染：更宽，也不是 Segoe UI 的样子。
  - 英文版给 `html[lang="en"] body` 一个把 `"Segoe UI", system-ui` 放在 CJK 字体前面的字体栈。

**P3：不影响宽度，但要一起改，不然会露馅**

- **R13 `lang` 属性写死了。** `<html lang="zh-CN">` 在 index.html:2 和 viewer.html:2 都是写死的，要随界面语言改。
  它影响读屏的发音，也影响浏览器按什么语言挑字形和标点。
- **R14 窗口标题的正则。** `isAlertTitle` 的正则只认中文标题。标题改成英文后就认不出来，后台报警后标题恢复不了（见 #2）。
  改成与语言无关：取出开头的 `(n) `，再与当前语言的 `alertTitle(n)` 比较；一次页面生命周期里语言不变。
- **R15 file:// 提示。** index.html:537 的内联脚本在 i18n 机制加载之前就执行了，中英两段并列显示，不做语言判断（见 #8）。
- **R16 系统层的名字。** Dock、菜单栏里的程序名来自 Info.plist 的 `CFBundleName`，原来写死的是中文。
  - 已按 spec §8.6 落地（提交 N1）：加了 `en.lproj/InfoPlist.strings` 和 `zh-Hans.lproj/InfoPlist.strings`，
    系统按系统语言显示；访达照旧显示文件名（不设 `LSHasLocalizedDisplayName`）。
  - 09-29 复审补了 `zh-Hant.lproj`（内容同样是简体的「TikTok 直播同传」）：只有两份时，繁体中文系统
    （zh-Hant、zh-TW、zh-HK）上 macOS 挑 en，Dock 显示英文名，界面却是中文。
  - 苹果建议 `CFBundleName` 不超过 15 个字符。“TikTok Live Translator” 有 22 个字符。
    最初的建议是菜单栏名用 “Live Translator”（刚好 15 个）、`CFBundleDisplayName` 保留全名；
    N1 按 spec §8.6 两个键都用全名，与窗口标题、通知里的程序名一致。菜单栏里放不放得下要真机看。
- **R17 退出确认框的语言。**（最初的结论作废）pywebview ≥6 的 `window.localization` 是窗口自己的一份字典，
  关窗那一刻才读。切换语言后页面经 JS 桥通知窗口，原地更新这份字典，确认框当场就换。
  只有 pywebview 5 没有这份字典，才要重启后生效。设置行下面不用写说明。
- **R18 所有「HTML 初始值 = JS 兜底值」的成对字符串（† 标记的那些），两边要一起改。**
  比如 #25、#44、#59、#78、#83、#85、#94。
  漏改一边的话，页面刚加载、JS 还没接管的那一瞬间会露出另一种语言，README 截图正好可能截到这一刻。
- **R19 这些地方是换行或自适应的，英文变长也不用改 CSS。**
  更新条会换行；报警条目头部会换行；提示条和横幅会换行；
  手机同看浮层是 480 宽（按钮排法和中文一样）；
  Comments 标题栏里，状态会折到第二行，截图 4 的中文版就是这样；
  磁盘删除按钮英文 159px、中文 163px，一样宽；
  悬浮的 “Jump to Latest” 和 “Comments” 也没问题。

### 4.4 落地与实测（提交 L1，2026-09-29）

上面 §4.1 的估算按「报警关」算。G10 的 `live-full` 场景是满编（主播 + 报警开 + 品牌 + 2 人在看），
PR #69 的 CI dom-scan（macOS 跑器）截图逐项量出来：状态胶囊 154px（其中 “Live · @bellaallnatural” 123）、
“Alerts on” 68、品牌标签上限 160；1101px 时控件区 808（“Phone Viewing · 2” 147，两个 A 连间距 34）。
按比例分摊负空间，五档宽度下主播名都被截（只剩 72–103px），1101px 时品牌标签只剩 58px。

L1 的做法（`style.css`、`viewer.css`，英文专属的一律挂在 `html[lang="en"]` 下，`tests/test_i18n_layout_css.py` 静态钉住）：

| 规则 | 作用范围 | 效果 |
|---|---|---|
| 品牌标签 `flex-shrink: 1000` | 英文 | 品牌标签缩到 48px 之前，主播名不让。报警标签照旧不缩（R6 不做） |
| 收起两个 A：**≤1199px**（R1 原写 ≤1100） | 英文 | 只到 1100 的话，1101px 时品牌标签只分到约 114px |
| 「手机同看」只留图标：1101–1199px（R2） | 英文 | 中文仍在 1100/1101 |
| `#switch-confirm` 可以折行（R3） | 两种语言 | 中文武装态不再溢出；单行时样子不变 |
| `.sc-name` 最小 128px（R5） | 英文 | |
| 字体栈把 Segoe UI 提前（R12）；同看页另加 Roboto | 英文 | macOS 上不变 |
| 同看页胶囊 `min-width:0`、`.conn-text` 省略号（R4） | 两种语言 | 防以后的长文案 |
| 弹幕状态交给 CSS 截断，**最多 6 行**（R10） | 两种语言 | 状态行只有约 175–190px 宽：两行只放得下约 28 个汉字、60 个英文字符，比原来的 80 个字符还少；6 行约等于原来的 80 个汉字，英文约 180 个字符 |

按上面的量法算，英文满编时品牌标签宽度：1000px 约 125、1100/1101px 约 148、1199px 160、1200px 约 106（此时两条英文断点都不生效），
主播名在这五档都完整。860/720px 两档品牌标签缩到 48px 后主播名才开始截，顶栏不溢出。

G10 的手机场景改经 DevTools 协议定视口（`Emulation.setDeviceMetricsOverride`）：macOS 的新无头模式里
`--window-size` 窄不过约 500px，页面按 500 排版、截图只截左边 390px。PR #69 的 `phone-*.png` 右边被裁就是这样来的
（截图底部居中的那行说明左边从 34px 起、右边伸出截图外，按 390 排版的话应当左右对称；底栏右对齐的提示音按钮整颗不见）。
改完以后 `test_phone_status_pill_stays_on_screen` 先核对视口真是 390 宽，再量胶囊。
