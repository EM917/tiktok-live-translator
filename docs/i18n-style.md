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
   目标语言下拉（它的选项本来就是各语言的自称）、文件名、HTTP 状态码、模型名、终端输出、审计 JSONL。
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
| 本机 / 这台电脑 | **this computer** | this Mac, this PC, localhost | 要跨平台，Mac 和 Windows 都能用 |

### 1.2 直播、场次与状态

| 中文 | English | 不用 | 理由 |
|---|---|---|---|
| 直播 / 直播间 | **live stream**（正文），**stream**（短标签） | LIVE room, live room, broadcast | TikTok 品牌写法是全大写的 LIVE，我们不照搬，当普通名词用；room 是中文直译 |
| 直播间地址 / 直播间链接 | **live link** | room URL, live address | 指 `tiktok.com/@x/live` 这种网页链接 |
| 流地址（.flv/.m3u8） | **stream URL** | stream link, stream address | 故意跟 live link 分开：一个是网页，一个是媒体地址。第八条那段文案就靠这两个词讲清楚 |
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
| 开始同看 / 停止同看 | **Start Sharing / Stop Sharing** | Turn On / Turn Off | 动词加对象，两个按钮成对 |
| 本场品牌 | **Brand**（标签），**this stream’s brand**（正文） | Session Brand | 顶栏标签 “Brand · Bella All Natural” 和中文差不多宽 |
| 不限（默认） | **Any (default)** | None, All brands | None 会被读成「没有品牌」，实际意思是「不限定某一个品牌」 |
| 品牌词表 | **brand glossary** | brand list | |
| 词表 / 领域词表（glossary.txt） | **glossary** | dictionary, term list | 帮助翻译用的 |
| 违禁词表（banned_terms.txt） | **banned-term list** | blacklist, banned glossary | 必须和 glossary 分开：list 决定报不报警，glossary 决定怎么翻译 |
| 违禁词 | **banned term** | prohibited word, prohibited term, forbidden word, blacklisted word | README 已经在用。term 也覆盖短语 |
| 违禁词报警 | **Banned-Term Alerts**（设置行名），正文写 **banned-term alerts** | Compliance Alerts, Warnings | |
| 疑似违禁词 | **possible banned term(s)**；面板标题写 **Possible Banned Terms** | suspected violation | 「疑似」是还没人工确认；violation 是法律判断，不该由程序下 |
| 报警 | **alert** | alarm, warning | warning 留给自检的「提醒」这一级 |
| 命中 | **match** | hit, trigger | |
| 分级：命中 / 变体 / 疑似（手机上叫：精确 / 变体 / 疑似） | **Exact / Variant / Similar** | Fuzzy, Possible | fuzzy 是算法术语；Possible 会跟面板标题撞。手机和桌面统一成同一套 |
| 自检 / 启动自检 | **Startup Check** | Self-Test, Self-check, Diagnostics, Health Check | health 已经被「识别落后」那条 health 提示占了。README 写的是 “Startup self-check”，界面用更短的写法 |
| 自检项 | **check** | test, item | |
| 通过 / 提醒 / 未生效 | **passed / warning / isn’t working** | failed（只在读屏的级别说明里用） | 「未生效」强调的是功能没起作用，不是测试没过。isn’t working 是苹果的常用说法 |
| 翻译引擎 | **Translation Engine**；短写 **engine** | translator, provider | |
| 回退（引擎） | **fallback**；正文写 **switched to {engine}** | downgrade | |
| 本地模型 | **local model** | offline model | |
| 免费额度 | **free quota** | free tier | |
| 密钥 | **API key** | token, secret | |
| 字幕 | **captions** | subtitles | 苹果 Live Captions、Zoom、Meet 在直播场景都叫 captions；subtitles 偏影视。README 宣传语可以继续说 subtitles，界面统一用 captions |
| 字幕字号 | **Caption size** | Font size | |
| 清空字幕 | **Clear**（顶栏按钮），tooltip 写 **Clear caption history** | Clear Captions（放不下，见 §4） | |
| 重译 | **Retranslate** | Redo, Translate Again | |
| 弹幕 / 观众弹幕 / 评论流 | **comments / Comments**；实在需要时才说 **comment stream** | danmaku, bullet comments, chat | TikTok 英文界面就叫 comments。「弹幕」是中文平台的概念 |
| 开始翻译 | **Start** | Start Translating, Start translating, Go | 和 Stop 成对。十几条后端提示要引用这个按钮，“click Start” 最短（38px，中文是 56px；“Start Translating” 要 123px） |
| 停止 | **Stop** | End, Disconnect | |
| 语音识别 / 识别 | **speech recognition / recognition** | ASR, transcription | 只有延迟统计行这种技术读数可以写 ASR |
| 识别落后 / 积压 | **falling behind / backlog** | lagging, queue | |
| 检测已降级 | **Detection degraded** | Detection impaired | |
| 人声降噪 / 降噪 | **Noise Reduction** | Voice Isolation, Denoise | Voice Isolation 是苹果麦克风模式的名字，意思是「只留人声」，RNNoise 做不到这么强。合规工具不该夸大 |
| 审计 / 审计日志 | **audit log** | compliance log, record | 和 README 同名。审计文件的内容不翻译 |
| 磁盘空间 | **Storage** | Disk Space, Disk Usage | macOS 系统设置里同一个功能就叫 Storage，而且更短。自检那一行也用 Storage |
| 模型与日志 | **models and logs** | data | |
| 正在用 / 本程序 / 非本程序 | **In use / This app / Not this app** | Other | 「非本程序」的意思是「不一定是别人的」，照字面翻最诚实 |
| 更新 / 一键更新 / 更新说明 | **Update / Update Now / Release Notes** | Upgrade | |
| 设置 | **Settings** | Preferences | |
| 目标语言 | **Translate to**（标签），正文写 **target language** | Target Language（标签） | |
| 主播语言 | **Spoken language** | Source language, Streamer language | 说的是「识别听哪种语言」，普通用户能懂；source 是翻译行业的行话 |
| 界面语言（新加的设置） | **App Language** | Language, Display Language | 必须和 Translate to 一眼就能分开 |
| 跟随系统 | **System ({resolved})**，如 “System (English)” | Auto, Default | |
| 二维码 | **QR code** | QR | |
| 局域网地址 | **local network address** | LAN IP, intranet address | |
| 只看模式 | **View only** | Read-only mode | |
| 提示音 | **Sound** | Ringtone, Beep | |
| 演示模式 | **Demo mode** | Test mode | |
| 浏览器登录态 | **Browser Login** | Cookies, Session | |
| 完全磁盘访问权限 | **Full Disk Access** | | 照抄 macOS 里的原名 |
| 直播流解析 | **Stream Lookup** | Stream Resolution | 「解析」对用户来说就是去找流地址。Resolution 容易被当成分辨率 |
| 音频组件 ffmpeg | **Audio (ffmpeg)** | Audio Component | ffmpeg 全小写，是它的品牌写法 |

### 1.4 不翻译、不改写

`TikTok`、`@username`、品牌名（`Bella All Natural`）、`DeepL`、`Claude`、`OpenAI`、`Google`、
`Ollama`、`Hy-MT2 1.8B/7B`、`TranslateGemma`、`MLX Whisper large-v3-turbo`、`faster-whisper`、
`ffmpeg`、`yt-dlp`、`TikTokLive 7.0.2`、`Safari`、`banned_terms.txt`、`glossary.txt`、
`profiles/`、`brands/`、`logs/`、`.flv`、`.m3u8`、`HTTP 429`、`code 4003110`、
目标语言下拉的 13 个选项、违禁词和词表内容、报警里的 `msg.term`。

### 1.5 设计过程中出现过、现在不用的词

三份实现方案里与本表冲突的写法一律不用：**prohibited term**（用 banned term）、**Phone view**（用 Phone Viewing）、
**Self-check**（用 Startup Check）、分级里的 **Possible**（用 Exact / Variant / Similar；Possible 只出现在
“Possible Banned Terms” 这个面板标题里）、**Start translating**（按钮就叫 Start）。

---

## 2. 风格规则

### 2.1 大小写

要分清两类东西：**给界面元素起的名字**，和**描述性的文字**。

| 元素 | 规则 | 例 |
|---|---|---|
| 窗口标题、产品名 | Title Case | TikTok Live Translator |
| 按钮、顶栏按钮、链接式按钮、二段确认的武装态 | Title Case | Start, Stop, Switch Streamer, Copy Link, Click Again to Clear, Update Now, Release Notes |
| 浮层 / 面板标题、分组标题、设置行名、自检项名 | Title Case | Phone Viewing, Recent Streams, Settings, Startup Check, Noise Reduction |
| 下拉菜单项 | 项名用 Title Case，括号里的补充说明用小写开头 | Spanish + English (default), Local Hy-MT2 7B (more accurate, uses more memory) |
| 控件左边的字段标签 | Sentence case | Spoken language, Brand, Translate to |
| 开关 / 复选框标签 | Sentence case | Show alert on match |
| 状态文字、胶囊、标签、设置行右侧的摘要 | Sentence case | Live, Stream ended, Alerts on, All 11 checks passed, Off · 53 terms, In use |
| 说明、提示条、横幅、错误、对话框正文、通知正文 | Sentence case，完整句子带句号 | Stopped. Enter a live link to start again. |
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
| 句号 | 完整句子加；按钮、标题、标签、胶囊、占位文字不加 |
| 分隔符 ` · `（U+00B7，两边各一个空格） | 沿用中文界面：Live · @bella、Brand · Bella、Off · 53 terms |
| 破折号 | 中文的「——」优先拆成两句。实在要保留，用不带空格的 em dash `—`，一句最多一个 |
| 分号 | 不用，拆成两句 |
| 冒号 | 标签后接值：`Startup Check: Noise Reduction isn’t working.` 冒号后面除非是专有名词或完整句子，否则小写 |
| 引号 | 引用用户内容（比如报警里的违禁词）用弯双引号 “term”。撇号用弯的 `’`（U+2019），代码和文件名里除外 |
| 括号 | ASCII 括号，前面加空格：`Spanish + English (default)` |
| 感叹号 | 不用 |
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
  - 例外：底部的延迟统计行是技术读数，沿用现在的 `1.8s` 写法（没有空格），否则放不下（见 §4 R8）。
- 范围用 en dash：`2–5 minutes`。
- 第几次：「第 2/5 次」→ `attempt 2 of 5`。
- 模型规格原样写：`1.8B`、`7B`、`large-v3-turbo`。
- **复数**：所有带计数的模板都要有 one 和 other 两种写法，下表列全了。
  中文原文没有单复数变化，直接套一个模板就会出现 “1 items” 这种错。

| 位置 | one | other |
|---|---|---|
| settings-rows.js:17 自检失败 | 1 feature isn’t working | {n} features aren’t working |
| settings-rows.js:18 自检提醒 | Passed · 1 warning | Passed · {n} warnings |
| settings-rows.js:19 全部通过 | 1 check passed | All {n} checks passed |
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
| 在看人数（live-ui.js:16、app.js:1869、viewer.py:409） | {n} watching | {n} watching（不变形） |
| 窗口标题（alerts.js:9、window_attention.py:20） | ({n}) Possible Banned Terms · TikTok Live Translator | 同左（标签式写法，不变形） |

### 2.4 时间与日期

- 时刻统一用 **24 小时制 `HH:MM:SS`**，补零，两种语言都一样，用于字幕、弹幕、报警、场次分隔线和审计里的时间段。
  理由：这些时间要拿去跟审计 JSONL 和终端日志逐秒对照；换成 12 小时制要多 3 个字符的 AM/PM，行宽也跟中文版不一样。
- 日期很少出现，出现时写 `Sep 28, 2026`（月份缩写）。不写 `9/28`，美式和欧式会读反。
- 相对时间写 `just now`、`5 min ago`。

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
- may 只能用来说「将来可能发生」的事（*Alerts may be delayed*），不能用来解释原因。

### 2.6 第八条（4003110）和「不猜原因」的英文禁用词

4003110 这一族文案包括：`pipeline.browser_only_message`、`resolver.py` 里的各条 `ResolveError`、
`browser_login.py` 的 advice 和 notice、pipeline.py:1683 的重试横幅、index.html:275 的输入帮助、
自检的提示条（`_selfcheck_incident_text`）。

这些文案里只写两件事：「接口没给流地址」，以及用户能做什么。

- 固定说法：**TikTok didn’t provide a stream URL for this live stream (code 4003110).**
- 中文原文有一句「不是网络或限流问题」。英文版**整句删掉**，换成正面的观察：
  **Other live streams worked at the same time, and TikTok doesn’t say why.**
  否定句里出现 rate limit 也不行。这是英文版有意和中文不一样的地方。
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
  `browser_only_message`、`_resolve_media`、`_confirm_offline`、`_host_wait`、`_selfcheck_incident_text`：

  ```
  (?i)\b(because|due to|caused by|the reason|probably|likely|must be|seems to)\b
  ```

- 电脑休眠写 suspended 不在任何一级里，照常用。真有一句必须用到这些词，登记进 `EN_WORD_EXCEPTIONS`，
  写明「中文臂 → 理由」。

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

### B. 顶栏

| # | 位置 | 中文 | English | 宽度 / 备注 |
|---|---|---|---|---|
| 11 | index.html:38 | 等待连接… | Starting… | 57 → 55 |
| 12 | app.js:195 idle | 待机 | Ready | 24 → 38 |
| 13 | app.js:195 connecting | 连接中… | Connecting… | |
| 14 | app.js:195 live | 直播中 | Live | |
| 15 | app.js:195 ended | 直播已结束 | Stream ended | 60 → 85 |
| 16 | app.js:195 error | 出错了 | Error | |
| 17 | app.js:195 offline | 与本地服务断开，重连中… | Reconnecting… | 141 → 91。完整说明放在横幅里（见 #75） |
| 18 | app.js:878 | 直播中 · @x / 连接中… · @x | Live · @x / Connecting… · @x | 胶囊整体 170 → 159 / 179 → 211 |
| 19 | app.js:2240 | 报警开 | Alerts on | 52 → 74（含内边距），见 §4 R6 |
| 20 | app.js:2262 | 品牌 · {name} | Brand · {name} | 143 → 154 |
| 21 | index.html:47–51 | 目标语言（标签 / title / aria） | Translate to | 这个标签在 ≤1080px 时隐藏；52 → 74 |
| 22 | index.html:68–70 | 字幕字号（title / aria） | Caption size | |
| 23 | index.html:76 | 清空字幕 | **Clear** | 76 → 56。“Clear Captions” 要 114，放不下，见 §4 |
| 24 | index.html:76 title | 清空历史字幕 | Clear caption history | |
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
| 33 | index.html:251 | 输入 TikTok 直播间地址 | Enter a TikTok Live Link | 232 → 236 |
| 34 | index.html:253 | 粘贴直播间链接，或只输入主播用户名（如 @somebody） | Paste a live link, or just the streamer’s username (such as @somebody) | 340 → 436，在 600 宽的容器里仍是一行 |
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
| 54 | settings-rows.js:17–19 | {n} 项功能未生效 / 通过 · {n} 项提醒 / 全部通过 · {n} 项 | {n} features aren’t working / Passed · {n} warnings / All {n} checks passed | 复数写法见 §2.3 |
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
| 67 | index.html:437 | 编辑项目文件夹里的 banned_terms.txt，一行一个词或短语，保存后点「停止」再「开始翻译」即可生效。命中时页面顶部会弹出红色警报并写入审计日志。 | Edit banned_terms.txt in the app folder, one word or phrase per line. Save, then click Stop and Start to apply. Matches show a red alert at the top of the window and are written to the audit log. | |
| 68 | index.html:455 | 磁盘空间 | Storage | 56 → 52 |
| 69 | app.js:1667 | 剩余 {free} · 本机模型与日志 {total} | {free} available · Models and logs {total} | 238 → 约 250 |
| 70 | app.js:1676、index.html:462 | 正在用 / 本程序 / 非本程序 + 说明 | In use / This app / Not this app · Items marked In use can’t be deleted. Items marked This app download again when needed. Items marked Not this app may be used by other tools, so check before deleting. | 标签和说明里的用词必须逐字一致 |
| 71 | index.html:463、app.js:1719/1740/1738 | 删除所选 / 删除所选（{n} 项，{size}）/ 删除中… / 确定删除以下内容？删了就没有了。 | Delete Selected / Delete {n} Items ({size}) / Deleting… / Delete these items? This can’t be undone. | |

### E. 换主播浮层

| # | 位置 | 中文 | English | 宽度 / 备注 |
|---|---|---|---|---|
| 72 | index.html:166 | 换主播 | Switch Streamer | 标题 |
| 73 | app.js:2129 | 当前 @x，确认前不会中断 / 确认前不会中断当前监听 | Now monitoring @x. Nothing changes until you confirm. / Monitoring continues until you confirm. | 12px 字号，会折成两行，可以接受 |
| 74 | index.html:174 | 新主播的直播间地址或 @用户名 | New streamer’s live link or @username | 199 → 245 |
| 75 | switch.js:94–103 | 确认换主播 / 换到 @B / 再点一次：改听 @B（停止监听 @A）/ 正在监听的就是 @A | Switch Streamer / Switch to @B / **Click Again to Switch to @B** / Already Monitoring @A | 武装态去掉括号那半句（@A 已经写在上面的副标题里）：415 → 283px。中文版这里本身就会溢出，见 §4 R3 |
| 76 | app.js:2100/2103 | @x（当前）/ 填入并武装改听 @x（还要再点一次确认才会真的换） | @x (current) / Select @x. You’ll still need to confirm. | |
| 77 | index.html:190 | 沿用：主播语言 {x} | Keeps spoken language: {x} | |
| 78 | app.js:2035 †index.html:193 | 切换期间两个主播都没有字幕，直到 @B / 新主播出现第一句。 | No captions from either streamer until @B starts speaking. / …until the new streamer starts speaking. | |
| 79 | app.js:424/2168 | 认不出这个输入：请粘贴直播间链接，或输入主播的英文用户名（到主播主页复制 @ 后面的部分，中文昵称不行）。 | This isn’t a live link or username. Paste the live link, or enter the streamer’s username, which is the part after @ on their profile. Display names won’t work. | |
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
| 93 | viewer.py:417 | 端口 {ports} 都没能打开监听，系统返回：{err}。已保持关闭。可以关掉占用这些端口的程序后再打开一次。 | Couldn’t listen on ports {ports}. Details: {err}. Phone Viewing is still off. Quit apps using these ports, then try again. | `{err}` 可能是系统给的任意语言，放在 Details 后面 |

### G. 直播中的字幕区和弹幕列

| # | 位置 | 中文 | English | 宽度 / 备注 |
|---|---|---|---|---|
| 94 | live-ui.js:30 †index.html:489 | 正在连接 @x… / 正在连接… | Connecting to @x… / Connecting… | 161 → 194 |
| 95 | index.html:501 | 观众弹幕 | Comments | 52 → 70（13px/600）。不用 “Viewer Comments”（119） |
| 96 | app.js:1336–1358 | 评论流已连接 / 正在连接评论流… / 评论流断开，重连中… / 未连接 | Connected / Connecting… / Disconnected. Reconnecting… / Not connected | 放在 Comments 标题旁边，上下文已经够了 |
| 97 | app.js:1338/1358 | 已连接，等待观众发评论… / 开播后自动连接评论流 | Connected. Waiting for comments… / Comments connect when the stream starts. | 空状态文字 |
| 98 | index.html:504/505 | 收起 / 清空弹幕 | Hide / Clear | title 写 “Hide comments to widen captions” |
| 99 | index.html:514 | 弹幕 {n}（悬浮入口）/ 展开观众弹幕 | Comments {n} / Show comments | |
| 100 | index.html:521 | 回到最新 | Jump to Latest | |
| 101 | app.js:1016/1017/1109 | 重译 / 重译中… / 重译失败 / 用最强模型重新翻译这一条 | Retranslate / Retranslating… / Couldn’t retranslate / Retranslate with the most accurate model | 悬停时才出现的按钮，68+16px |
| 102 | app.js:1138/1146 | 翻译中… / 翻译失败 / 翻译已跳过（积压） | Translating… / Translation failed / Skipped (backlog) | |
| 103 | session-divider.js:27 | ── 14:01:29 以下为 @x ── / 以下为新的一场 | ── 14:01:29 Now monitoring @x ── / New session | |
| 104 | index.html:225/227、app.js:1222、live-ui.js:62 | 疑似违禁词 / 清空警报 / 上一场 / 命中·变体·疑似 | Possible Banned Terms / Clear Alerts / Previous / Exact · Variant · Similar | |
| 105 | app.js:1297、viewer.js:724/819 | 译文失败（{why}）——请看上面的原话 | Couldn’t translate ({why}). See the original above. | 「中文正在补…」统一写 Translating…，不提具体语言 |

### H. 常见的提示条和横幅

| # | 位置 | 中文 | English |
|---|---|---|---|
| 106 | pipeline.py:1456 | 自检：{name} 未生效——{fix} | Startup Check: {name} isn’t working. {fix} ——截图 8：“Startup Check: Noise Reduction isn’t working. Restart the app and try again.”（13px/600 下约 450px，1000 宽的窗口里一行放得下） |
| 107 | pipeline.py:968 | ⚠️ 识别开始落后（积压 {n} 秒），报警会相应延迟 | ⚠️ Speech recognition is falling behind ({n} sec backlog). Alerts will be delayed. |
| 108 | pipeline.py:961/965 | 🔴 检测已降级：识别落后 {n} 秒；… | 🔴 Detection degraded: recognition is {n} sec behind. {k} audio segments older than {m} sec were dropped and weren’t checked for banned terms. |
| 109 | pipeline.py:970 | ✅ 识别已追上，检测恢复正常 | ✅ Speech recognition caught up. Detection is back to normal. |
| 110 | pipeline.py:667/673/784/828 | 已收到指令，正在连接… / 正在切换到 @B：先停止 @A 的监听… / 正在停止… / 已停止。输入直播间地址可重新开始。 | Connecting… / Switching to @B: stopping @A, then connecting… / Stopping… / Stopped. Enter a live link to start again. |
| 111 | pipeline.py:2241/2356/2357 | 正在解析直播流地址… / 已连接直播间，开始实时识别（人声降噪已开启） | Getting the stream URL… / Connected. Transcribing live (noise reduction on). |
| 112 | pipeline.py:2131/2219 | 直播已结束。可以往下翻看这一场的字幕，或在上方输入新的直播间地址。 | The stream has ended. Scroll down to review this session’s captions, or enter a new live link above. |
| 113 | pipeline.py:154–158 | 4003110 整段 | TikTok didn’t provide a stream URL for this live stream (code 4003110). Retried {n} times. Other live streams worked at the same time, and TikTok doesn’t say why. Sometimes it works if you click Start again a little later. Sometimes it doesn’t work for the whole stream. {browser advice} To watch now, paste the live link and the .flv URL from your browser together, separated by a space. A .flv URL usually works for about two weeks. |
| 114 | pipeline.py:1683 | TikTok 暂时没有把这个直播间的流地址给程序，{s} 秒后自动重试（第 {i}/{n} 次）… | TikTok didn’t provide a stream URL for this live stream. Retrying in {s} sec (attempt {i} of {n})… |
| 115 | pipeline.py:2400 | 直播流多次中断且自动重连失败——可能直播已结束，或网络不稳。… | The stream was interrupted several times and couldn’t reconnect. Check that the stream is still live and your network is working, then click Start. |
| 116 | app.js:449/653 | 与本地服务断开，正在重连——稍候再点「开始翻译」 / 本地程序似乎已经关闭——请重新双击打开… | Lost connection to the local service. Reconnecting… Click Start again in a moment. / No connection to the app’s local service for over 20 seconds. If the app has quit, reopen it: on Mac, double-click TikTok Live Translator.app; on Windows, double-click Start.bat. |
| 117 | app.js:964 | 连续多条字幕翻译失败——翻译服务可能暂时连不上… | The last several captions couldn’t be translated, so captions show the original text. Speech recognition isn’t affected. If this continues, choose another engine in Settings > Translation Engine. |
| 118 | pipeline.py:3826 | {engine} 返回 HTTP 429，程序暂停请求 {s} 秒后自动重试… | {engine} returned HTTP 429. Requests are paused for {s} sec, then retried automatically. Captions show the original text meanwhile. Banned-term alerts aren’t affected. |
| 119 | pipeline.py:1946 | {t} 电脑休眠或挂起了约 {d}… | {t} This computer was asleep for about {d}. The app wasn’t running, so that part of the stream wasn’t monitored. During streams, keep the computer plugged in with the lid open. |
| 120 | app.js:530–538、516/524 | 发现新版本 / 一键更新 / 更新说明 / 前往下载新版本 / 再点一次确认更新：更新期间监听暂停，更新完自动恢复 / 更新中… | A new version is available / Update Now / Release Notes / Download New Version / Click Again to Update. Monitoring pauses and resumes afterward. / Updating… |
| 121 | app.js:1517–1529 延迟统计行 | 违禁词最迟 {a} / P95 {b} 内报警 · 其中 切段 {c} + 识别 {d} · 译文再等 {e} · 积压 … | Alerts ≤{a} (P95 {b}) · Segment {c} + ASR {d} · Translation +{e} · Backlog {n}s · Dropped {n} · ASR timeouts {n} · Skipped {n} · Queue {x}/{y} |

#107–#109 的英文保留与中文相同的行首 emoji（⚠️ 🔴 ✅）：桌面提示条由 `live-ui.js` 的 `stripStatusEmoji`
统一去掉，两臂保持一样，G2 按 emoji 的多重集比。

### I. 手机同看页（viewer.html / viewer.js）

| # | 中文 | English | 备注 |
|---|---|---|---|
| 122 | 连接中… / 已连接 / 已断开，{n} 秒后重连… | Connecting… / Connected / Reconnecting in {n} sec… | 状态胶囊（见 §4 R4） |
| 123 | 链接已失效，请向中控要新的二维码 | Link expired · Ask for a new QR code | 305 → 222px |
| 124 | 同看人数已满（12 人），稍后再试 | Full (12 max) · Try again later | |
| 125 | 中控已关闭手机同看 / {n} 秒没有新消息 / 状态未知 | Operator stopped sharing / No updates for {n} sec / Status unknown | |
| 126 | 未开始 / 连接中 / 直播中 / 已停止 | Not started / Connecting / Live / Stopped | |
| 127 | 已经 {n} 秒没连上。请找中控确认同看是否还开着，或重新扫码 | Can’t connect for {n} sec. Ask the operator whether sharing is still on, or scan the QR code again. | |
| 128 | 疑似违禁词 / 清除已看过的报警 / 只在这台手机上隐藏，中控电脑和记录不受影响 | Possible Banned Terms / Clear Seen / Hides alerts on this phone only. The operator’s computer and records aren’t affected. | |
| 129 | 重译会用中控电脑上的大模型，稍等几秒 | Retranslation uses the larger model on the operator’s computer and takes a few seconds. | |
| 130 | 最新 / 弹幕 / 只看模式 · 不能操作 / 提示音 | Latest / Comments / View only / Sound | |
| 131 | 页面切到后台或锁屏后，手机可能停掉提示音。 | Sounds may stop when this page is in the background or the phone is locked. | |
| 132 | 演示数据，不是真实直播 / 违禁词警示已关闭（中控开播时未开启） | Demo data, not a real stream / Banned-term alerts are off for this session. | |
| 133 | 识别开始落后，报警会有延迟 / 检测已降级，识别明显落后 | Recognition is falling behind. Alerts will be delayed. / Detection degraded: recognition is far behind. | |

---

## 4. 布局风险

### 4.1 默认窗口 1000×760，直播中的顶栏逐项宽度

场景就是截图 4：直播中，选了品牌，2 台手机在看，报警关（默认）。
顶栏布局规则（style.css:199–389）：

- `.controls` 设了 `flex:none`，不缩也不折行；
- 左边 `.brand` 可以缩：品牌标签先缩（`flex-shrink:2`，最小 48px），再缩状态胶囊（省略号）；
- ≤1100px 时「手机同看」只剩图标加人数；≤1080px 时隐藏「目标语言」四个字；≤600px 才允许折行。

| 项 | 中文 px | 英文 px（推荐方案） | 说明 |
|---|---:|---:|---|
| 左右内边距 16+16 | 32 | 32 | |
| `.brand` 和 `.controls` 之间的 gap | 16 | 16 | |
| 状态胶囊（点 + 文字 + 内边距） | 170 | 159 | “Live · @bellaallnatural” 比中文还窄 |
| gap | 8 | 8 | |
| 品牌标签（上限 160） | 143 | 154 | “Brand · Bella All Natural” |
| 目标语言下拉 | 144 | 144 | 选项都是各语言的自称，最宽的一项是 “Bahasa Indonesia”，**不受界面语言影响** |
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

- **1081–1100px**：「Translate to」标签露出来（+80）。controls 703，左侧可用 330，**刚好放下**（+9）。
- **1101–1199px（英文专属风险）**：「手机同看」按钮露出文字。
  - 宽度从 40 变成 145（“Phone Viewing · 2”）；打开但没人看时（“Phone Viewing · On”）是 154。
  - 1101px 时 controls 808，左侧只剩 245，品牌标签被压到约 78px。
  - 中文版同一宽度 controls 718，余量还有 +14。
  - **建议**：英文版把这条断点从 1100 挪到 1200，可以写成 `html[lang="en"]` 作用域下的规则，
    即 ≤1199px 时只留图标。1200px 时 controls 817，可用 335，放得下。
- **≤860 / ≤720 / ≤600**：两种语言都靠现有断点逐级收起，英文版不会更糟。
  600px 以下本来就允许折行。

### 4.3 风险清单（按优先级）

**P1：影响默认窗口或 README 截图**

- **R1 直播顶栏只有 8px 余量。**
  - 清空字幕用 “Clear”（tooltip 写 “Clear caption history”，aria-label 写 “Clear captions”），
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
- **R8 底部延迟统计行会折行。**
  - 所有计数都出现时，逐字翻译约 1013px，超过 1000 宽窗口的 960px 可用宽度，折行后会盖到底部的大字幕上。
  - 用 #121 的紧凑写法（约 800px），而且只有这一行保留 `1.8s` 写法。
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
- **R16 系统层的名字。** Dock、菜单栏里的程序名来自 Info.plist 的 `CFBundleName`，现在写死的是中文。
  - 建议加 `en.lproj/InfoPlist.strings` 和 `zh-Hans.lproj/InfoPlist.strings`，这样系统会按系统语言显示。
  - 苹果建议 `CFBundleName` 不超过 15 个字符。“TikTok Live Translator” 有 22 个字符。
    菜单栏名可以用 “Live Translator”（刚好 15 个），`CFBundleDisplayName` 保留全名。
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
  悬浮的 “Jump to Latest” 和 “Comments 4” 也没问题。
