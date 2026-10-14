# comment_guard/ — 弹幕留意的规则与词表

这个目录放「弹幕留意」（Comment Review）规则层用的词表和说明。规则代码在 `app/comment_guard.py`，
这里只说机制。**示例一律用虚构内容**（虚构主播 `luna_demo`、虚构品牌 Marca Ejemplo）；真实的主播词表、
品牌词表、竞品名单、关注名单都是个人数据，只放本机，不进仓库。

> 当前状态：规则引擎、词表机制和界面都就位，**`comment_guard.enabled` 默认开**（只影响 `settings.json` 里没写过这个键的人，
> 只用规则、不调模型）。弹幕读取协程先让守卫看一遍再显示，判定随 `comment` 消息的 `guard` 字段到达本机页面；关注名单按
> 主播存在 `marked/`。红条档词表里没确认的词条写在 `[段名:pending]` 里：用户逐条确认之前，命中只出琥珀，不标红、不自动加入
> 关注名单。**模型复核**（Claude Haiku 5.5，云端，设置里选，默认「不用模型」）是规则之后的第二道：只给规则拿不准的弹幕补一句
> 意见，能把琥珀升红、把 note/线索升成琥珀，**不能撤红、不能降级**；没有密钥、断网、被限流、识别落后时只用规则。见下面「模型复核」。

## 界面在哪

**只在中控电脑上**，手机同看一律没有（标红、顶部条、关注名单都不发手机，页面也不加载 `web/guard.js`）。程序对 TikTok 只读：
只发现和提醒，不能删弹幕、不能禁言。不放首页或开始面板（CLAUDE.md 第六条）。

- **弹幕栏**：红条（左边红条加类别胶囊，如「疑似号召举报或不买」）、琥珀（拿不准，胶囊旁写观察到的理由，如「句子很短，没有问号」）、
  关注的人（★ 和靛蓝边）；每条右上角有 ☆ 按钮，悬停或键盘聚焦时出现，点一下加入 / 移出关注名单。标题旁的状态胶囊写覆盖情况
  （「弹幕留意：只用规则」……），点它打开抽屉。
- **顶部条**：永远一行，只在直播中、有未读时出现（红条、拿不准、关注的人发言，类别最多写 3 类，其余折成「另 n 类」）。
  红条和拿不准**按事件计**：一次刷屏算 1 件，不数事件里的弹幕条数；关注的人发言数的是发言本身（写「n 条」）。
  类别后面的计数一律写成「疑似刷屏 ×2」（英文 “Possible flooding ×2”），数字不紧贴名字。条右侧的控件写的是点了会怎样：抽屉收着写「查看 ›」，开着写「收起 ⌃」。
  点开是条下方的**抽屉**（不是弹窗）：「最近需要留意」页签（每行有时间、昵称、类别、原文、译文、理由，按钮「定位」「加入 / 移出关注名单」
  「忽略」）和「关注名单」页签。抽屉高度固定（窗口的约三分之一，两个页签一样高），列表在里面自己滚动，右边有常驻的滚动条、底部渐隐提示还有更多。「全部看过」把未读清零；弹幕列收起时，浮钮上有未读红条的事件数。
- **设置 → 弹幕留意**（和「违禁词报警」一样是设置分组里的一行，默认收起，永不自动展开）：开关、模型复核（不用模型 / Claude Haiku 5.5，
  选 Claude 且没有密钥时出现密钥框，写进和翻译引擎同一个 Claude 密钥，不切换翻译引擎）、状态、也显示弱信号、本主播的关注名单
  （「移出」「清空自动加入的」，待机时可改）、「打开规则文件夹」（点了才建 `local/`）、规则条数。设置分组在直播中整块隐藏，
  所以直播中要用的（看名单、移出、重新启用）都在抽屉里。
- 用 `python3 main.py --demo` 可以在停播时看到一路虚构弹幕（虚构主播 `luna_demo`），走和真实弹幕同一个守卫；演示的关注名单写进临时目录，
  演示完丢掉。

## 目录

```
comment_guard/
  README.md              本文件（入库）
  defaults/              入库，程序直接读，不复制（学 banned_fuzzy_policy.txt：复制出来的副本收不到升级）
    insult.txt           辱骂 / 威胁 / 涉性言语
    accuse.txt           说假货、骗子；号召举报或不买；扬言举报
    promo.txt            引流 / 冒充官方
    interact.txt         互动词、国家名、正向词簇、正向表情（刷屏豁免）
    cues.txt             负面线索（只决定送不送模型）
    thresholds.txt       阈值
    prompt-v1.txt        模型复核的提示词（英文数据；改了升版本号，哈希写进审计）
  local/                 gitignored，用户自己编辑；目录不存在也行，程序不主动生成（设置里点「打开规则文件夹」才建）
  marked/<主播小写>.json  gitignored：关注名单（见下面「接线、开关和关注名单」）
  bench/                 gitignored：本地模型实测记录（本地模型没有排期）
  retained/              gitignored：可选留存（后续 PR）
```

## 接线、开关和关注名单

代码在 `app/pipeline_guard.py`（接线）和 `app/comment_marks.py`（关注名单的存储）。

- **开关**：`settings.json` 里的 `comment_guard: {"enabled": true, "backend": "none", "weak": false}`，缺省就是开（PR-4 起；写过 `false` 的照旧关着）。
  `backend` 只有 `none`（只用规则）和 `claude`（Claude Haiku 5.5，见下面「模型复核」；没有密钥时只用规则）。
  直播中不改：设置在待机时改、下一场生效。**应急总开关**：环境变量 `TLT_COMMENT_GUARD=off`（也认 `0` `false` `no`），
  不管设置里写什么都整体关掉，弹幕照常显示。关着的时候开场不读词表、不碰 `marked/`，`observe` 一次都不调用。
- **一场的流程**：`_begin_session` 里载入词表、阈值、可信名单（`local/trusted/<主播>.txt`）和这个主播的关注名单；
  每批弹幕先 `observe`（同步、纯 CPU、自带预算，出任何错都原样放行）、再交给 `accept`、最后 `flush`；
  `_end_session` 里把名单写完盘，并把计数并进 `session_end.comment_guard`。词表文件在直播中被改了只提示一次，
  下一场才生效。
- **消息**（只发本机页面，手机同看一律不发）：`comment.guard`（规则判定，随消息到）、`comment_guard`（标注变了才发，
  每个 cid 每秒至多 1 条）、`guard_event`（聚合后的事件，全局每秒至多 1 条）、`guard_mark`（名单增删）；
  `config` 里有 `guard_state`、`guard_events`（最多 50 条）、`guard_marked`（只放当前主播）。
  页面发 `guard_set`（开关、模型复核选择、弱信号、Claude 密钥）、`guard_mark`（加入/移出；待机时只能移出）、`guard_clear_auto`（清空自动加入的）、`guard_dismiss`（忽略一个事件）、`guard_reenable`（熔断后本场重新启用；Claude 后端不熔断，只在识别落后时暂停、自己恢复，所以现在什么都不做）、`open_guard_dir`（打开规则文件夹）。
- **关注名单文件** `marked/<主播小写>.json`：`{"version": 1, "streamer": …, "viewers": {主键: {keys, nick, handle, aka,
  by, cat, first, last_seen, hits}}}`。`keys` 里任一命中就算同一个人；不存弹幕原文；`aka` 最多 3 个；每个主播最多
  500 人，满了先淘汰 `by=auto` 里 `last_seen` 最旧的，手动加入的不淘汰；`by=auto` 的人 30 天没再发言，下一场载入时
  自动移出；只有昵称的人（`n:<昵称>`）只在本场内存里有效、不落盘。内存为准，改动 2 秒合并成一次写盘（线程池里
  `.tmp` → fsync → `os.replace`）。写盘失败不丢脏标记：下一次改动和收尾都会再写一次，仍然写不进去就提示一次。
  文件内容不对（不是合法 JSON、结构或版本不认识）时改名成 `.bak-<时间>`，按空名单继续，并提示一次；
  文件读不到（权限），或内容不对但改名失败时，没有备份：不改名、不覆盖，按空名单继续，本场的改动只在内存里、
  不写盘，提示里也这么说。
- **审计**只记计数：`session_start.comment_guard`（开关、词表指纹、条数、体检警告、阈值）、`session_end.comment_guard`
  （按类别的红条/琥珀/note 计数、预算降级次数、自动/手动加入和移出的人数……）。弹幕原文、昵称、账号一律不进审计。
- **多账号同一句只数、不报**（用户 2026-10-14 决定）：不同账号发同一句话，几乎都是主播要求的抽奖口令（giveaway），
  所以它**没有琥珀、没有顶部条的数、没有抽屉事件、没有 `guard_event`**，`judge()` 也不为它出任何类别。引擎照常数，
  数记在 `session_end.comment_guard.chorus`：`events`（几波口令；同一波只算 1 个事件）、`comments`（落在事件上的弹幕条数，
  从第 3 个账号起每条算 1）、`max_accounts`（最大一波的账号数）。这是「操作员没看到的」，所以不进 `counts` 和
  `amber_by_reason`（那两个写的是操作员看到的样子）。同一条弹幕被别的规则命中——红条档词条、引流（链接、电话）、单人刷屏
  （C1 / C3）——时，照旧按那条规则标，口令不会把它盖住或降级。

## 模型复核（Claude Haiku 5.5）

代码在 `app/comment_review.py`，提示词在 `defaults/prompt-v1.txt`（英文、固定不变；改了要升版本号，哈希写进 `session_start.comment_guard.prompt_hash`）。
设置 → 弹幕留意 → 模型复核：**不用模型（默认）** / **Claude Haiku 5.5（云端）**。没有本地模型，直播时也不会装任何模型。

- **送什么、不送什么**：只送规则筛出来的灰区——确认型（琥珀：没问号的短断言、说自己的遭遇、转述、同一人累计）和升级型
  （note / 只命中负面线索的弹幕）。明确的红条、刷屏、链接和电话、互动词、主播本人、房管、可信名单里的人都不送。
  同一句话（送出去的打码文本、规则提示和前一条一模一样，问号、表情、大小写也算）30 分钟内只问一次——不按 fold 之后的文本
  认「同一句」：`¿es falso?` 是提问、`es falso 🤡` 是断言，不能共用一个判定；同一个人 30 秒内最多送 1 条；一次最多 8 条；每分钟至多 10 次、每场至多
  600 次；队列 40 条，满了先丢最旧的线索档。
- **打码后才出本机**：电话、数字合计 7 位以上的串（不管上下文；数字之间隔着空白、标点或符号，如 `: _ | * ~ +`，都算）、西语数字读法、链接（含 `punto com`、`[.]`、`wa . me`
  这类绕写）、邮箱、`@账号` 和 `ig: 名字` 一律换成 `#NUM#`、`[link]`、`@usuario`，`<` `>` 换成 `‹` `›`，截到 280 字。
  **不发**昵称、uid、handle、主播名、时间戳、主播字幕。上下文只带同一个人 2 分钟内的前一条（同样打码）。
  打码之后还认得出链接、电话或账号的，不送。
- **合并只加不减**（影子期）：确认型琥珀 + 模型判「攻击」且有把握 → 升红，写「模型意见：同意规则」；非攻击 → 琥珀保留，
  写「模型意见：提问 / 客诉 / 友好 / 未见攻击」；拿不准 → 写「拿不准」。升级型 note / 线索 + 模型判攻击且有把握 → 升为琥珀
  （「模型标出」）。**模型升出来的红条不自动加入关注名单**（自动加入只认规则的明确命中）。命中还没确认的词条（`pending`）
  和第一人称自指（`self`）的琥珀只写模型意见，不升红：没确认的词条不被模型绕过。证据片段要是打码文本的子串，否则清空
  （判定保留）；界面只显示类别名和从原文里截出的证据，模型自己写的句子一律不上界面。
- **闸门**：识别积压 >= 10 秒（健康等级不是 ok）暂停送审，回到 ok 满 30 秒自动恢复；没密钥（`no_key`）、密钥被拒
  （`key_rejected`，换了密钥才再试）、模型找不到（`model_missing`，本场）、被限流（`cooldown`，按 `retry-after`，最长 300 秒）、
  连不上（`offline`，连续 3 次暂停 120 秒）、请求被拒（`invalid`，连续 3 次本场停用）、复核提示词文件读不到
  （`prompt_missing`，和没密钥分开说）时只用规则。规则永远照常。收尾时还排着、还在问模型的弹幕记成「未复核」，
  算进 `session_end.comment_guard.unreviewed`。
- **请求**：裸 aiohttp 直发 `https://api.anthropic.com/v1/messages`（不读 `ANTHROPIC_BASE_URL`），模型 `claude-haiku-5-5`，
  关思考、`effort: low`、结构化输出（`output_config.format`）、`cache_control` 写在系统提示上；没有 temperature / top_p / top_k /
  fallbacks / 预填。密钥和翻译引擎共用（`ANTHROPIC_API_KEY` 环境变量优先，其次设置里的 `api_keys`）。单次 8 秒时限。
- **审计**（只有计数和代码，没有弹幕内容）：`comment_guard_review`（每次调用一条：`n`、`ms`、`outcome`、token 数……）、
  `comment_guard_gate`（暂停 / 恢复 / 停用的转折）、`session_start.comment_guard.{model, prompt_hash}`、
  `session_end.comment_guard.{reviews, unreviewed, review_invalid, review_agree, review_disagree, review_promoted}`
  （`review_invalid` 只数模型回应不合格和请求被拒一类；限流、密钥被拒、模型找不到不算，看 `comment_guard_review.outcome`）。
- **评估**：`tools/guard_review_eval.py claude tests/fixtures/comment_guard/review_cases.jsonl out.json`——约 150 条虚构弹幕 ×
  3 遍打真的接口，只测格式合格率、几遍一致率、P50 / P95 延迟、拒答率和花费，**不主张准确率**。直播转写进行中拒绝运行；
  花费有硬上限（默认 $0.10，每次调用前按最坏情况算）；价格表只有 Haiku 5.5 的，`TLT_GUARD_CLAUDE_MODEL` 换了别的模型就拒绝运行；
  延迟统计包含超时的那几次，格式合格率只算拿到回应的调用（超时、HTTP 错误另报）；密钥不打印、不写报告。
  准确率要靠真实弹幕标注（后续的留存功能）。

## 词表格式

沿用 `banned_terms.txt` 的写法，再加一样：

- 一行一个词或短语；`re:` 开头是正则；空行忽略。
- **注释只能整行写**。行尾的 `#` 会连同词条一起去匹配（和违禁词表同一条规则），自检会报。
- `[段名]` 一行切换后面条目的档位。`[` 开头的行一律是段头，绝不当词条：大小写、两边空格、`_` 写成 `-`、
  全角括号都认（`[ Sexual_Soft ]` 等于 `[sexual-soft]`）。段名认不出来时，自检报一条 `unknown_section`，不会静默忽略；
  缺 `]` 的行（`[medium`、`[medium]:`）报 `bad_section`，这一行丢掉，**后面的词条也跳过**，不会悄悄落进上一段。
  红条档的段名后面可以加 `:pending`（`[threat:pending]`）：这一段的词条还没经用户确认，命中只出琥珀。
  `:pending` 只允许加在红条档的段名上，加在别的段上报 `pending_not_red_tier`，按普通段处理。
- 词条写什么形式都行：加载时统一折叠一遍，`Estafa`、`ESTAFA`、`estáfa` 等价；ñ 写成 n。
  `re:` 条目原样使用，跑在**折叠后**的文本上（无重音、无标点、ñ 写 n、小写），要按这个形式写。
- 只有符号或表情的词条（没有字母数字，例如一个手势表情）按字面匹配，但只认非 ASCII 的符号类字符
  （Unicode 类别 So / Sk / Sm）；`!!!`、`?`、`.`、`-`、`====` 这种纯标点会匹配几乎每一条弹幕，报 `empty` 并丢掉。
- `re:` 条目只能写在交给检测器匹配的段和 `promo` 的 `ask` 段；别的段写了报 `regex_not_supported`。写错的正则报
  `invalid_regex`；能匹配空串、或对任何弹幕都匹配（`re:`、`re:.*`、`re:x|`）的报 `matches_empty`，这一条丢掉，
  免得每条弹幕都成了红条。
- 同一词条写在多个段里，取最严的档。
- promo 段的 `re:` 里写 `zzat` 表示「同句有 @账号」：折叠会把 `@` 变成字母 `a`，正则里没法直接认 `@`，所以规则层在折叠文本
  末尾补一个标记词 `zzat`（主播自己的账号不算）。`felicidades ganaste @premios`、`seguidores gratis en @x` 就靠它。
- 红条档里带 `re:` 的词条，多半是**收窄过的写法**：词条后面接正常说法就不算（`te voy a matar de risa`、`no compren tantos`、
  `reporten su pedido aqui`、`ojala te pase algo bueno`、`mas barato en mi pais`）。写成 `(?!\s+…)` 的否定前瞻或前置的 `(?<!…)`，
  跑在折叠后的文本上。agent 新写、还没经用户确认的候选词条在文件里用 `# 候选（agent 新写，待确认）` 标出来。
- `insult` 的 `slur`（种族 / 排外辱骂）是单独一段：确认以后可以和 `strong` 分开开关。`mojado / mojada` 在西语里首先是「湿的」
  （`cabello mojado`），所以只认前面或后面带辱骂词的写法。

折叠（`fold()`）会处理手机打字常见的拼写漂移：大小写、重音、拉长（`holaaaa`）、拆字（`e s t a f a`，
空格、点、逗号、斜杠、表情隔开的都认，>= 4 个连续单字母才拼回）、leet（`m13rd4`）、同形字、`q` / `xq` 这类俚语缩写、
格式字符和不可见字符（Unicode 类别 Cf、蒙古文自由变体选择符、标签字符、韩文填充符等一律删掉；它们既可能把一个词劈开，
也可能是两个词之间唯一的分隔，所以词表匹配另跑一遍把它们当空格的读法，两遍的命中取并集：`est[U+2800]afa` 和
`eres[U+2066]una[U+2066]pendeja`、`hola[U+2069]pendeja` 都认；表情的粘合符 FE0E / FE0F / ZWJ / keycap 不当空格读），以及词内遮挡符（`p*ta`、`est*fa`、
`f*ck`：一个遮挡符代表一个任意字母，只对 `strong` 和 `assert` 段里 >= 4 个字母的单词条生效）。

## 三层档位

| 层 | 段 | 单独命中的结果 | 默认状态 |
|---|---|---|---|
| **红条档** | insult：`strong` `threat` `sexual` `target` `slur`；accuse：`call` `report` `assert` `assert-frame`；promo：`pull` `scam-strong` | 已确认（`[x]`）：红条，带不确定标记时降为琥珀；待确认（`[x:pending]`）：琥珀，`amb == "pending"` | `insult/threat` 与 `accuse/call` 的大部分词条已由用户确认（2026-10-13，各留几条在同名 `:pending` 段）；其余红条档段全部是 `[x:pending]` |
| **线索档** | insult：`medium` `watch` `sexual-soft`；accuse：`institution` `negative`；promo：`offer` `lure` `scam-weak`；`cues.txt` | note：不上界面，只计数；负面线索 `lead` 只决定送不送模型 | 启用 |
| **豁免档** | 各文件的 `exempt-phrases`；promo 的 `ask`；`interact.txt` | 不计分 | 启用 |

要点：

- `target` 段的词（`gorda`、`fea`……）只在「指向对方」的句式里才计分（`eres` / `te ves` + 词，
  `que` + 词 + `eres`，`pinche` + 词，词 + `de mierda`，英语 `you are` + 词）。不接 la / el / esa / tu 这类泛指前缀。
- `assert-frame` 段的词（`falso`、`fake`……）只在「es / son / producto / puro + 词」的句式里才算断言。
- 变体档（单复数、阴阳性）只对 `insult/strong` 和 `accuse/assert` 开；其余段只认原样，免得 perro 被当成 perra。
- 模糊匹配（编辑距离）全关：打字的错误是漏字、换位、拉长，不是听错字母，而且 5–7 个字母的词撞车严重。
- 豁免短语命中被它整个盖住时当没命中（`puta madre` 是感叹，但 `hijo de la / tu puta madre`、`tu puta madre`
  前面带这些词时是骂人，不盖）；否认式（`no es estafa`）整体剪掉，担心、求证、替主播辩护的说法
  （`espero que no sea estafa`、`no creo que sea`、`pense que era estafa pero……`、`not a scam`）同理；否认只在一个
  子句里算（`no, es una estafa` 的 no 不否认后面那句）；问句（有 `?` / `¿`、疑问词开头、句尾追问 `verdad` /
  `o no` / `o que`、选择问句 `X o Y`）不计分，句里有指称词时才作为升级型送模型。
  `hay` / `tienen` / `dan` / `pueden` / `alguien` 和感叹的 `cuanta estafa` / `what a scam` 不是疑问词：没有问号时不算问句，
  带 `maybe_question` 标记出琥珀。
- `target` 的系动词句式前面紧挨着否定（`no eres fea`、`ya no estas gorda`）或条件（`si estas operada`、`si tu ya estas operada`、
  `if you are fat`、`if you think you are fat`：条件词在同一个子句里、和句式之间隔不超过 3 个词，中间有 `pero` / `y` /
  `porque` 的不算）不算；否定词要紧挨着句式词，`no inventes eres fea`、`ni modo eres fea`、`no tu eres fea` 照样算，
  否定词和句式词之间隔着被豁免的短语、表情或符号（`no no manches eres fea`、`no 🙄 eres fea`、`no (eres fea)`）也照样算
  （宁可多报一个可见的）；`no, eres fea`、`dije que no. eres fea` 的 no 是应答，按子句重判；`tu si eres fea`、`si, eres fea`、`sí eres fea` 的 si 是「确实」，不是条件；`pinche X`、`X de mierda`、
  `que X eres` 不查否定。裸 `estas` 只接单数目标词（`estas feas manchas`、`que feas estas fajas` 的 feas 是复数）；
  英语的裸 `ur` 只认在子句开头（前面最多垫 `omg` / `lol` 这类语气词）、目标词收尾的句子（`ur ugly`、`ur fat lol`、`omg ur fat`），
  后面还有话的（`ur fat burner`、`ur ugly sweater`）、`ur` 前面是动词介词的（`burn ur fat`、`melt ur fat`）不算；
  `you are fat burning`、`fat loss` 这类商品词不算。
- @账号只有被账号标签引出时才算引流的载荷：平台名（`ig: x`、`wsp: x`，不写 @ 也行）、`mi ig @x`、`sigueme en @x`、
  `mi cuenta @x`，以及紧挨在 @ 前面的命令式（`sigueme @x`、`dm me @x`、`follow me @x`）；标签按去重音的文本认（`sígueme`、`página`）。
  标签和 @ 之间可以隔一小段非字母（`IG ➡️ @x`、`siganme 👉 @x`、`siganme!! @x`）或 `es` / `de` / `en` / `a` 这类连接词
  （`mi ig es @x`、`instagram de @x`）。顾客报自己的号（`mi ig @x para el sorteo`、`mi cuenta @x no me deja entrar`）和
  `mi ig @x` 的引流写法分不开，也会出红条（结构信号，不自动加人）：这是已知的取舍，要不要为抽奖、账号问题单独放行由用户决定。
  `cuenta` / `perfil` / `pagina` 这类泛称必须带 @（`mi perfil: privado` 不是账号）；纯数字的不算账号（`wsp: 5512345678` 的号码）。
  句首或句中呼叫朋友的 `@名字 ……` 不算。电话 + 任何 @账号同现仍是红条（结构信号），有下单语境词（`quiero comprar 5512345678 @ana`）
  时只记 note；这条没有话术垫底，所以电话要像真的：只有 1~2 种数字（`666666666`）、连号（`12345678`）、只由 1~2 位一组
  拼成的（`36 37 38 39`）、3-3-3 分组（`350 450 550`）、年份串（`2024-2025-2026`）都不算，连写的 8~12 位、2-4-4 / 3-3-4 / 4-4 分组、
  带 + 的国际号、西语数字词读法照旧算。邮箱地址不是链接，是联系方式；`https://x@evil.shop`、`tiktok.com@evil.shop` 这种把链接伪装成登录信息的，
  以及域名后面紧跟路径、端口、查询串的（`a@mitienda.com/promo`、`x@wa.me/5512345678`），仍然是链接。
- 强词出现在第一人称自指的句子里（`que pendeja soy`，没有第二人称标记）降为琥珀（`amb == "self"`）。

## 确认红条档词条

红条档的词条是业务判断：**agent 只产出候选，用户逐条确认**。出厂词表里每个红条档的段都写成
`[段名:pending]`，命中只出琥珀（`amb == "pending"`，`tier` 仍是 `"red"`，`tags` 含 `"pending"`，
`route == "confirm"`），不标红、不自动加入关注名单。同一条弹幕里既有已确认命中又有待确认命中时，按已确认的出红条。

用户确认一条（或一段）的流程：

1. 先跑碰撞计数，看每条词条在主播口播里撞了多少句：
   `nice -n 19 python3 tools/guard_collision.py --logs ~/tiktok-live-translator/logs --section insult/threat`
   （纯 CPU、只读；**直播转写进行中会拒绝运行**；语料为空会报错退出，不会打一份看着干净的空报告。
   `--logs` 不写时，仓库自己的 `logs/` 不存在就退到 `~/tiktok-live-translator/logs`。）
   新增或收窄一条词条之前，除了碰撞计数，还要拿一批虚构的「顾客正常说法」过一遍，确认没有新增任何可见（红或琥珀）的误报；
   碰撞的删掉或收窄，并在 `tests/test_comment_guard_lexicon.py` 里钉一条正常说法和一条恶意说法。
2. 把确认过的词条从 `defaults/*.txt` 里的 `[段名:pending]` 挪到**同一个文件里**不带 `:pending` 的同名 `[段名]`
   （没有就新建一段）。整段确认就把段名的 `:pending` 去掉。
3. **同一个提交里**改测试，**只有两处**：
   - `tests/comment_guard_confirmed.py` 的 `RED_CONFIRMED` 加上这一段（元组 `(文件, 段名)`，例如 `("insult", "threat")`）。
     读出厂词表的测试都由它驱动，不需要再改别的期望：`tests/test_comment_guard_rules.py` 钉「出厂词表里已确认的段集合
     == `RED_CONFIRMED`、不在里面的段命中只出琥珀」；`tests/test_comment_guard_lexicon.py` 里收窄词条和候选词条的期望
     （已确认的段里，恶意说法是红条，或者那一条还没确认时是琥珀）也跟着它走。
   - `tests/fixtures/comment_guard/` 里的夹具是出厂词表去掉 `:pending` 之后的快照，要同步成一样的内容
     （`test_fixture_lexicon_equals_the_defaults_without_pending` 钉着这条，红了就是两份漂移了）。整段确认只改了段头，
     去掉 `:pending` 之后内容不变，夹具不用动；逐条挪过去的话，重新生成一遍：
     ```
     for f in insult accuse promo interact cues thresholds; do
       { head -1 tests/fixtures/comment_guard/$f.txt
         sed -E 's/^\[([a-z0-9-]+):pending\]$/[\1]/' comment_guard/defaults/$f.txt; } > /tmp/$f.fx
       mv /tmp/$f.fx tests/fixtures/comment_guard/$f.txt
     done
     ```
   `test_confirming_threat_and_call_the_way_the_readme_says_keeps_these_tests_green`（`tests/test_comment_guard_lexicon.py`）
   在出厂词表的临时副本上把「威胁」和「号召举报·别买」两段按上面的步骤确认一遍，再用同一批期望去判：它红了，就说明这份
   流程写的和测试对不上了。

`thresholds.txt` 里的 `accumulate_red` 同样算红条档开关，写成一行生效的 `accumulate_red: off`：同一个人 10 分钟内
第二次触发线索档辱骂，默认只是琥珀（`accumulated`），不标红、不自动加入关注名单。改成 `on` 等于打开一档红条，须用户确认。

## 本机叠加：`local/`

`local/` 整个 gitignored。目录不存在也行。agent 不碰这里。

| 文件 | 内容 |
|---|---|
| `insult.txt` `accuse.txt` `promo.txt` | 同名同格式：普通行 = 追加到所在的 `[段]`；`-词条` = 关掉一条默认（按折叠形式匹配，以后版本里同名词条仍保持关掉） |
| `exempt.txt` | 本机豁免短语（行业词、主播口头禅）。可用 `[insult]` `[accuse]` `[promo]` 分段，不分段就三类都适用 |
| `competitors.txt` | 竞品店名、账号、品牌（引流类用）。默认空；仓库里不放任何具体竞品 |
| `mine.txt` | 自己的号码、链接、账号、邮箱，永不算引流。号码按子串比；账号是精确匹配（`marcaejemplo` 不盖 `@marcaejemplo_oficial`）；域名按 host 比（见下），自家域名下的邮箱（`soporte@marcaejemplo.com`）同样不算；条目可以带账号主页路径（`tiktok.com/@marcaejemplo`，盖住 `…/@marcaejemplo/video/…`，不盖 `…/@marcaejemplo2`） |
| `exempt_domains.txt` | 无害的域名（物流官网等），命中的链接不算引流。按 host 比：host 等于条目或以 `.条目` 结尾，条目带路径时路径还要以它开头；`tienda-marcaejemplo.com`、`marcaejemplo.com.evil.shop`、`marcaejemplo.com.mx`、`marcaejemplo.com.éxito.shop`（带重音的标签也算标签）不被 `marcaejemplo.com` 盖住；host 后面直接粘着表情或标点（`www.marcaejemplo.com😍`）不影响，粘着第二个链接（`…com,https://evil.com`）则不放过 |
| `thresholds.txt` | `key: value`，覆盖 `defaults/thresholds.txt` |
| `trusted/<主播>.txt` | 该主播本人、房管、助理的 uid，一行一个，永不提醒。`judge()` 的 `user` 或 `keys` 里任一个命中就算（不分大小写） |

示例（虚构）：`local/accuse.txt`

```
# 追加一条本场主播的口头禅，别被当成指称
[exempt-phrases]
pestanas ejemplo
# 关掉一条默认
-reportar
```

`defaults/` 也认 `exempt_domains.txt`（可选，目前没有）：TikTok Shop 商品链接的写法不凭记忆写，等用户贴一条自家
店里的商品链接核对之后，再把写法放进 `defaults/exempt_domains.txt`，这样顾客转发商品链接不会被当成引流。

## 阈值（`thresholds.txt`）

| key | 默认 | 含义 |
|---|---|---|
| `user_burst_note` / `user_burst_alert` | `4/10`、`6/10` | 单人不同句：10 秒内 4 条记 note，6 条标红 |
| `user_burst_alert_long` | `12/60` | 60 秒内 12 条标红 |
| `same_text_note` / `same_text_alert` | `3/60`、`5/60` | 同人同句：60 秒内 3 次记 note，5 次标红；问句（含没有问号的 `hay` / `tienen` / `alguien` 起头的短句）最多 note。按原句比，`quiero el 1` / `quiero el 2` 是不同的句子；但去掉数字之后还剩 3 个以上的词时，只改数字的刷屏（`esto es una burla 3` / `10` / `17`）也算同一句 |
| `brigade_note` / `brigade_alert` | `3/20`、`5/20` | 多账号同句（**只数不报**，见上）：20 秒内 3 个账号开始算一波口令（记进 `chorus`）；5 个账号只决定引擎事件落在 note 还是琥珀这一档，界面和审计都不看这一档 |
| `brigade_min_words` / `brigade_jaccard` | `3`、`0.8` | 近似句：词集合 Jaccard >= 0.8 且至少 3 个词（同上，只影响 `chorus` 计数） |
| `accumulate` | `2/600` | 线索档辱骂 10 分钟内第 2 次 -> 琥珀 |
| `accumulate_red` | off | 红条档开关：累计结果直接标红（改成 on 须用户确认） |
| `caps_lead` | `0.6/3` | 全大写字母占比 / 至少几个词：负面线索 |
| `budget_ms` 等 | `50` | 规则判定超预算的降级（滚动 1 秒累计超过 50 毫秒，进 5 秒结构模式）。`CostBudget` 要传单调时钟 |

阈值有范围：条数 / 账号数 >= 1、窗口 > 0、`0 < brigade_jaccard <= 1`、`budget_ms` > 0；越界的行报 `bad_threshold`，
保留默认值（不会因为一行 `user_burst_note: 0/10` 让每条弹幕都成了刷屏）。窗口计数只算 `ts - 窗口 <= t <= ts` 的条目，
系统时钟被往回拨时比 `ts` 更晚的旧条目不会被算进去。

## 判定结果

`CommentGuard.judge()` 返回一个固定形状的字典：

```
{"lvl": "alert" | "amber" | "note" | "lead" | None,
 "amb": None | "maybe_question" | "grievance" | "quote" | "accumulated" | "question_link" | "pending" | "self",
 "cats": ["insult"], "sub": "strong",
 "why": ["insult/strong:hijueputa"],        # 命中的词表条目，是我们的数据
 "tier": "red" | "clue" | "struct" | None,  # 来自红条档词表、线索档，还是结构信号（链接/电话/刷屏）
 "tags": [...],
 "route": None | "confirm" | "upgrade"}     # 给模型复核看的路由
```

- **alert** 是红条；**amber** 是规则自己拿不准的（含命中了还没确认的红条档词条：`amb == "pending"`），
  不算红条、不自动加入关注名单；**note** 不上界面，只计数；**lead** 只命中负面线索，只决定送不送模型。
- 自动加入关注名单只认 `lvl == "alert"` 且 `tier == "red"` 且没有不确定标记（`auto_markable(result)`）；
  账号键还要稳定：`auto_markable(result, uid_src=...)` 在 `uid_src` 不是 `id` / `id_str` / `sec_uid`（只有 handle 或昵称）时返回 False
  （`uid_ok()`）；**显式传 `None` 也是 False**——只有昵称的观众在 `event_to_item` 里压根没有 `uid_src` 这个键，
  `auto_markable(r, item.get("uid_src"))` 拿到的就是 `None`。不传这个参数时只看判定本身。链接、电话组合、刷屏都是 `tier == "struct"`，永远不自动加人。
- 类别 id（`insult` `threat` `sexual` `accuse_call` `accuse_report` `accuse_claim` `flood_user` `flood_repeat`
  `promo_link` `promo_contact` `promo_pull` `promo_scam`）只是代码，界面文字另有来源。`flood_brigade` 和 `amb == "brigade"`
  是 2026-10-14 之前的多账号同句：现在判定不再产生它们，只留在 `web/guard.js` 里认旧数据。
- 刷屏必须传真实时间戳：`judge(..., ts=...)` 的 `ts` 是必填关键字，必须是有限的 int / float（否则在改任何状态之前抛
  `TypeError` / `ValueError`）。`text` 不是 str 就转成 str，`user` / `cid` 不可哈希就转成 str。
- 主播自己的账号用 `judge(..., handle=...)` 的 handle 字段比（不分大小写），**不按昵称比**：任何人都能把昵称改成主播名；
  `role` 是 anchor / mod 的、trusted 名单命中 `user` 或 `keys` 的也整条豁免。
- 多账号同句在引擎里是一个事件（只数不报，见上；`judge()` 的结果里没有它，只有 `judge().last_flood` 里有）：第一次到 `brigade_note` 时分配 id，之后相似的弹幕并进同一个事件（账号数原地增长、id 不变；并进来的时候
  回看窗口，把所有和这一条相似的账号补进去，所以只和中间的变体相似的弹幕也数得进来），
  最后一条相似弹幕之后「窗口 + 20 秒」没有新的就结束；`judge().last_flood` 里的事件带 `ev`、`n`、`first_ts`、`last_ts`、`accounts`（前 20 个，按各自第一次说这句的先后排）。
  **出不出事件、note 还是琥珀这一档，只看 20 秒窗口里和这一句相似的账号数**（>= 3 记 note，>= 5 到琥珀这一档，规格 §4.5 C2）；
  事件上的 `n` 是整个事件累计的账号数，`chorus.max_accounts` 记的就是它的最大值。事件还活着时来的零散相似弹幕（窗口里只有它自己）
  不出事件、不升档，所以每 9 秒来一个新账号说「me gusta mucho el color rosa」永远算不成一波口令。口令的变体 V0 / V2 互不相似、V1 和两边都相似时：V2 看窗口里
  已经属于某个事件的 V1，并进那个事件；先各自开了两个事件的，V1 一出现就并成最早的那个。

## 词表体检

`load_rules()` 返回的 `Rules.warnings` 是一组只含代码的字典，用来在自检里报告：
`unknown_section`、`bad_section`、`pending_not_red_tier`、`entry_before_section`、`invalid_regex`、`matches_empty`、
`regex_not_supported`、`trailing_comment`、`accent`、`punctuation`、`empty`、`removal_unmatched`、`missing_file`、
`defaults_missing`、`unknown_threshold`、`bad_threshold`、`read_error`、`decode_error`、`skipped_line`。
最多留 50 条，多出来的按 reason 聚合成最后一条 `{"reason": "more", "n": …, "by_reason": {…}}`。
`Rules.hash` 是生效词条、阈值，加上本机叠加里同样会改判定的几份（`exempt.txt`、`competitors.txt`、`mine.txt`、
`exempt_domains.txt`）的指纹，审计里用它还原「当时用的是哪份词表」；`trusted/` 不进（它按主播在开场时读）。

## 不做的事

- 程序对 TikTok 只读：不删弹幕、不禁言、不拉黑。
- 不改 `banned_terms.txt`、`app/detector.py`；违禁词报警的任何行为都不受这里影响。
- 弹幕原文、昵称、账号不进审计。
- 手机同看一律没有：标红、顶部条、关注名单、模型意见都不发手机，复核的结果也只回到中控电脑。
- 模型复核不撤红、不降级、不自动加人，也不会因为模型说「没问题」就把规则标出来的东西藏起来。
