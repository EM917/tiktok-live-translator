# comment_guard/ — 弹幕留意的规则与词表

这个目录放「弹幕留意」（Comment Review）规则层用的词表和说明。规则代码在 `app/comment_guard.py`，
这里只说机制。**示例一律用虚构内容**（虚构主播 `luna_demo`、虚构品牌 Marca Ejemplo）；真实的主播词表、
品牌词表、竞品名单、关注名单都是个人数据，只放本机，不进仓库。

> 当前状态：规则引擎和词表机制已就位，**还没有接进直播管线**，也没有界面。
> 红条档词表的词条全部写在 `[段名:pending]` 里：用户逐条确认之前，命中只出琥珀，不标红、不自动加入关注名单。

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
  local/                 gitignored，用户自己编辑；目录不存在也行，程序不主动生成
  marked/                gitignored：关注名单（后续 PR）
  bench/                 gitignored：本地模型实测记录（后续 PR）
  retained/              gitignored：可选留存（后续 PR）
```

## 词表格式

沿用 `banned_terms.txt` 的写法，再加一样：

- 一行一个词或短语；`re:` 开头是正则；空行忽略。
- **注释只能整行写**。行尾的 `#` 会连同词条一起去匹配（和违禁词表同一条规则），自检会报。
- `[段名]` 一行切换后面条目的档位。段名认不出来时，自检报一条 `unknown_section`，不会静默忽略。
  红条档的段名后面可以加 `:pending`（`[threat:pending]`）：这一段的词条还没经用户确认，命中只出琥珀。
  `:pending` 只允许加在红条档的段名上，加在别的段上报 `pending_not_red_tier`，按普通段处理。
- 词条写什么形式都行：加载时统一折叠一遍，`Estafa`、`ESTAFA`、`estáfa` 等价；ñ 写成 n。
  `re:` 条目原样使用，跑在**折叠后**的文本上（无重音、无标点、ñ 写 n、小写），要按这个形式写。
- 只有符号或表情的词条（没有字母数字，例如一个手势表情）按字面匹配。
- 同一词条写在多个段里，取最严的档。

折叠（`fold()`）会处理手机打字常见的拼写漂移：大小写、重音、拉长（`holaaaa`）、拆字（`e s t a f a`）、
leet（`m13rd4`）、同形字、`q` / `xq` 这类俚语缩写。

## 三层档位

| 层 | 段 | 单独命中的结果 | 默认状态 |
|---|---|---|---|
| **红条档** | insult：`strong` `threat` `sexual` `target`；accuse：`call` `report` `assert` `assert-frame`；promo：`pull` `scam-strong` | 已确认（`[x]`）：红条，带不确定标记时降为琥珀；待确认（`[x:pending]`）：琥珀，`amb == "pending"` | **全部是 `[x:pending]`**，零个已确认段 |
| **线索档** | insult：`medium` `watch` `sexual-soft`；accuse：`institution` `negative`；promo：`offer` `lure` `scam-weak`；`cues.txt` | note：不上界面，只计数；负面线索 `lead` 只决定送不送模型 | 启用 |
| **豁免档** | 各文件的 `exempt-phrases`；promo 的 `ask`；`interact.txt` | 不计分 | 启用 |

要点：

- `target` 段的词（`gorda`、`fea`……）只在「指向对方」的句式里才计分（`eres` / `te ves` + 词，
  `que` + 词 + `eres`，`pinche` + 词，词 + `de mierda`，英语 `you are` + 词）。不接 la / el / esa / tu 这类泛指前缀。
- `assert-frame` 段的词（`falso`、`fake`……）只在「es / son / producto / puro + 词」的句式里才算断言。
- 变体档（单复数、阴阳性）只对 `insult/strong` 和 `accuse/assert` 开；其余段只认原样，免得 perro 被当成 perra。
- 模糊匹配（编辑距离）全关：打字的错误是漏字、换位、拉长，不是听错字母，而且 5–7 个字母的词撞车严重。
- 豁免短语命中被它整个盖住时当没命中；否认式（`no es estafa`）整体剪掉；问句（有 `?` / `¿`、疑问词开头、
  句尾追问 `verdad` / `o no` / `o que`、选择问句 `X o Y`）不计分，句里有指称词时才作为升级型送模型。

## 确认红条档词条

红条档的词条是业务判断：**agent 只产出候选，用户逐条确认**。出厂词表里每个红条档的段都写成
`[段名:pending]`，命中只出琥珀（`amb == "pending"`，`tier` 仍是 `"red"`，`tags` 含 `"pending"`，
`route == "confirm"`），不标红、不自动加入关注名单。同一条弹幕里既有已确认命中又有待确认命中时，按已确认的出红条。

用户确认一条（或一段）的流程：

1. 先跑碰撞计数，看每条词条在主播口播里撞了多少句：
   `nice -n 19 python3 tools/guard_collision.py --logs ~/tiktok-live-translator/logs --section insult/threat`
   （纯 CPU、只读；**直播转写进行中会拒绝运行**）。
2. 把确认过的词条从 `defaults/*.txt` 里的 `[段名:pending]` 挪到**同一个文件里**不带 `:pending` 的同名 `[段名]`
   （没有就新建一段）。整段确认就把段名的 `:pending` 去掉。
3. **同一个提交里**改 `tests/test_comment_guard_rules.py`：
   - `RED_CONFIRMED` 加上这一段（元组 `(文件, 段名)`，例如 `("insult", "threat")`）；
   - `tests/fixtures/comment_guard/` 里的夹具是出厂词表去掉 `:pending` 之后的快照，要同步成一样的内容
     （`test_fixture_lexicon_equals_the_defaults_without_pending` 钉着这条，红了就是两份漂移了）；
   - 三条出厂词表测试都由 `RED_CONFIRMED` 驱动：已确认的段集合必须等于它，不在里面的段命中只出琥珀。

`thresholds.txt` 里的 `accumulate_red` 同样算红条档开关，写成一行生效的 `accumulate_red: off`：同一个人 10 分钟内
第二次触发线索档辱骂，默认只是琥珀（`accumulated`），不标红、不自动加入关注名单。改成 `on` 等于打开一档红条，须用户确认。

## 本机叠加：`local/`

`local/` 整个 gitignored。目录不存在也行。agent 不碰这里。

| 文件 | 内容 |
|---|---|
| `insult.txt` `accuse.txt` `promo.txt` | 同名同格式：普通行 = 追加到所在的 `[段]`；`-词条` = 关掉一条默认（按折叠形式匹配，以后版本里同名词条仍保持关掉） |
| `exempt.txt` | 本机豁免短语（行业词、主播口头禅）。可用 `[insult]` `[accuse]` `[promo]` 分段，不分段就三类都适用 |
| `competitors.txt` | 竞品店名、账号、品牌（引流类用）。默认空；仓库里不放任何具体竞品 |
| `mine.txt` | 自己的号码、链接、账号，永不算引流（子串匹配） |
| `exempt_domains.txt` | 无害的域名（物流官网等），命中的链接不算引流（子串匹配） |
| `thresholds.txt` | `key: value`，覆盖 `defaults/thresholds.txt` |
| `trusted/<主播>.txt` | 该主播本人、房管、助理的 uid，一行一个，永不提醒 |

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
| `same_text_note` / `same_text_alert` | `3/60`、`5/60` | 同人同句：60 秒内 3 次记 note，5 次标红；问句最多 note |
| `brigade_note` / `brigade_alert` | `3/20`、`5/20` | 多账号同句：20 秒内 3 个账号记 note，5 个账号出一个琥珀事件（v1 不单独标红） |
| `brigade_min_words` / `brigade_jaccard` | `3`、`0.8` | 近似句：词集合 Jaccard >= 0.8 且至少 3 个词 |
| `accumulate` | `2/600` | 线索档辱骂 10 分钟内第 2 次 -> 琥珀 |
| `accumulate_red` | off | 红条档开关：累计结果直接标红（改成 on 须用户确认） |
| `caps_lead` | `0.6/3` | 全大写字母占比 / 至少几个词：负面线索 |
| `budget_ms` 等 | `50` | 规则判定超预算的降级（滚动 1 秒累计超过 50 毫秒，进 5 秒结构模式） |

## 判定结果

`CommentGuard.judge()` 返回一个固定形状的字典：

```
{"lvl": "alert" | "amber" | "note" | "lead" | None,
 "amb": None | "maybe_question" | "grievance" | "quote" | "accumulated" | "question_link" | "brigade" | "pending",
 "cats": ["insult"], "sub": "strong",
 "why": ["insult/strong:hijueputa"],        # 命中的词表条目，是我们的数据
 "tier": "red" | "clue" | "struct" | None,  # 来自红条档词表、线索档，还是结构信号（链接/电话/刷屏）
 "tags": [...],
 "route": None | "confirm" | "upgrade"}     # 给后续的模型复核看的路由
```

- **alert** 是红条；**amber** 是规则自己拿不准的（含命中了还没确认的红条档词条：`amb == "pending"`），
  不算红条、不自动加入关注名单；**note** 不上界面，只计数；**lead** 只命中负面线索，只决定送不送模型。
- 自动加入关注名单只认 `lvl == "alert"` 且 `tier == "red"` 且没有不确定标记（`auto_markable()`）。
  链接、电话组合、刷屏都是 `tier == "struct"`，永远不自动加人。
- 类别 id（`insult` `threat` `sexual` `accuse_call` `accuse_report` `accuse_claim` `flood_user` `flood_repeat`
  `flood_brigade` `promo_link` `promo_contact` `promo_pull` `promo_scam`）只是代码，界面文字另有来源。
- 刷屏必须传真实时间戳：`judge(..., ts=...)` 的 `ts` 是必填关键字。

## 词表体检

`load_rules()` 返回的 `Rules.warnings` 是一组只含代码的字典，用来在自检里报告：
`unknown_section`、`pending_not_red_tier`、`entry_before_section`、`invalid_regex`、`trailing_comment`、`accent`、`punctuation`、
`empty`、`unknown_threshold`、`bad_threshold`、`read_error`、`decode_error`、`skipped_line`。
`Rules.hash` 是生效词条加阈值的指纹，审计里用它还原「当时用的是哪份词表」。

## 不做的事

- 程序对 TikTok 只读：不删弹幕、不禁言、不拉黑。
- 不改 `banned_terms.txt`、`app/detector.py`；违禁词报警的任何行为都不受这里影响。
- 弹幕原文、昵称、账号不进审计。
