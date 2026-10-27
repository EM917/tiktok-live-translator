# 顶栏「闪购」：给维护者的说明

2026-10-27 加的。中控电脑上如果装着「闪购助手」（本机另一个程序，不随本程序分发），控制页顶栏多一颗「闪购」按钮，
点开是一个**只读**浮层：闪购助手开没开、要不要人处理、每个商品的闪购状态、最近的记录。唯一能点的是「打开闪购助手」。
本程序不控制闪购助手（不开始、停止、批准、续开），也不碰 TikTok：只读它在本机的状态接口，再就是 `open -a` 把它打开。

README、FAQ 不提这个功能（公开读者拿不到闪购助手）。英文怎么写看 `docs/i18n-style.md` §1.3（术语）、§3 K（文案表）、
§4.2（顶栏宽度和「Aa」）。

## 1. 数据流

```
闪购助手  GET http://127.0.0.1:9334/status
   │  后端每 3 秒取一次；连不上或读不出时 15 秒（连上以后第一次没查到先照旧，3 秒后再查）；点「打开」成功后 30 秒内一律 3 秒
   ▼
app/pipeline_flash.py  FlashSaleMixin._flash_watch
   │  app/flash_sale.py：fetch_status → StatusTracker.update（normalize、判陈旧、计卡住、排序、算 attention）
   │  和上次发布的比，没变就不发
   ▼
server.config["flash_sale"] = 新 dict                hello 整份带上，重连自动补发
server.broadcast({"type": "flash_sale", …})          i18n.DATA_ONLY；手机 DENY；浮窗不收
   ▼
web/app.js（接线、未读、开合） → web/flash.js（文案、排序、渲染） → #flash-btn 的颜色、#flash-panel 的内容
   ▲
「打开闪购助手」 → {"type": "open_flash_helper"} → Pipeline.handle_control → open -a <完整路径>
```

页面**不直连** 9334：闪购助手的 CORS 只放 `http://127.0.0.1:8765` 和 `http://localhost:8765`，本程序的控制端口
可能漂到 8766–8774，跨域会失败。所以由后端去取，再推给页面。

## 2. 有没有这个按钮

- 只看两个位置：`/Applications/闪购助手.app`、`~/Applications/闪购助手.app`（`flash_sale.app_candidates`），不跑 mdfind；
  不是 macOS 一律当没装。按**完整路径**判断和打开，检测到的和打开的是同一个东西；不按 bundle id 打开（它不进公开仓库）。
- 只在启动时判一次：`main.py` 在第一次 hello 之前 `await pipeline.attach_flash_sale()`。运行中装上或删掉，要重启才变。
- 演示模式（`--demo`）不接：不查 9334、不画按钮。演示模式是拿来截图、录屏验界面的，9334 上是真实店铺的真实商品，
  不能进截图（`tests/test_pipeline_flash.py` 读 main.py 的语法树钉着这个 `if not args.demo`）。要看按钮和浮层的样子，
  用 G10 的 `live-flash*` / `home-flash*` 场景（状态由 `StatusTracker` 从契约的虚构样例生成）。
- 没装：`server.config` 里**没有** `flash_sale` 这个键，页面就不画按钮（同浮窗的 `float_window`）。
- 装了：先同步写一份 `conn: "checking"` 的空状态（第一次 hello 就带着，按钮不会晚一拍冒出来），再起轮询。
- 不在 `Pipeline.__init__` 里检测：测试大量构造 Pipeline，开发机上可能真装着闪购助手，放在那里本机和 CI 的结果
  就会不一样，轮询还会真的去连本机的 9334。
- 按钮首页和直播中都显示（用户 10-27 定）：开播前就看得到闪购助手开没开、要不要登录，也能在首页点「打开」。
- 不加 settings 键，没有开关。

## 3. 闪购助手的接口（契约摘要）

闪购助手那边定的格式，这里只记本程序用到的部分：

- `GET http://127.0.0.1:9334/status`，只监听本机。`Host` 必须是 `127.0.0.1:9334` 或 `localhost:9334`，否则 403
  （防 DNS 重绑定）；aiohttp 默认发的就是前者。只有 `/status`，只认 GET、HEAD。
- 闪购助手和它开的专用 Chrome 同生共死：Chrome 一关它就退出，接口就连不上。**连不上（连接被拒）= 闪购助手没在运行**。
  连得上却不回话不算：闪购助手卡住（死锁、被挂起、HTTP 线程堵住）时内核照样完成握手，我们这边只看到读超时，
  那正是要人去看的时候，见 §4 的 `why`。
- `updated_at`：最后一次成功判断出状态的时刻（UTC，`Z` 结尾），后台每 2 秒刷新一次；`page=error` 时不刷新；
  还没判断过时为 null。
- 代码表（认不出的代码原样显示，不给通用文案）：
  - `page`：ok、no_page、login_required、loading、no_list、captcha、error；
  - `paused_reason`：null（正常运行）、not_started、awaiting_approval、captcha、user_interaction、error；
  - 商品 `state`：live、between_rounds、stopped、ended_by_user、pending、no_sale、unknown；
  - `alerts[].level`：error（报警）、warn（失败、跳过、暂停、等确认）、info。
- `alerts` 最多 20 条，最早的在前。页面上跑的还是旧脚本时它一直是空数组，这是正常的，不能当成出错。
- 一场最多 300 个商品（实际见过 103 个）。

9334 上回话的不一定是闪购助手（本机任何进程都能占这个端口），所以后端读体限 1 MiB、宽容解析：
不认识的键丢掉（`shop`、`page_version` 也丢），类型不对的当 null，字符串截长、去掉控制字符。

## 4. 推给页面的状态

`hello.config.flash_sale` 和 `{"type": "flash_sale", …}` 同形（后者多一个 `type`）。所有键永远都在：

| 键 | 含义 |
|---|---|
| `conn` | `checking` 启动后还没查到；`ok` 连上并读懂了；`unreachable` 连不上（只有连接被拒才算：9334 上没人在听）；`bad_response` 9334 上有东西，但没拿到能用的回话（原因见 `why`）。不是 `ok` 时商品和记录全部清空，不留旧数据。连上以后第一次没查到不发布（§5） |
| `why` | 只在 `bad_response` 时有，其余为 null：`no_answer`（2 秒内没回完，或者没回话就断开、被重置）、`cut_off`（回到一半断开）、`http`（回的不是 HTTP）、`http_<状态码>`（非 200，302 也不跟）、`too_large`（超过 1 MiB）、`json`（不是 JSON）、`not_object`（JSON 但不是对象）。页面只给 `no_answer` 单独一句「127.0.0.1:9334 在 2 秒内没有回应。」，其余都是「返回的内容读不出来」 |
| `stale` | 只在 `conn=ok` 时可能为 true，见 §5 |
| `updated_ms` | 只在 `stale` 为 true、且 `updated_at` 认得出时是 epoch 毫秒（页面写「上次更新 HH:MM:SS」），其余为 null。不下发 `updated_at` 本身：它每 2 秒变一次，下发了去重就失效 |
| `app_version`、`page`、`running`、`paused_reason` | 照接口，清洗过 |
| `products[]` | `{id, title, state, round, ends_ms, next_ms, stock_left, stock_total, renewals, enabled, stuck}`，最多 300 个，**后端已排好序**（§6） |
| `products_total` | 闪购助手给了多少个商品（含超出 300 被截掉的）。比 `products` 长时页面在摘要末尾写「未列出 n」，不静默丢掉 |
| `alerts[]` | `{at_ms, level, text}`，最后 20 条，最早的在前 |
| `attention` | 要人处理的原因代码，已按浮层主句的优先级排好，见 §7。空表 = 不变色 |
| `stuck_count` | 卡住的商品数 |

`title` 和 `alerts[].text` 是**数据**：页面一律 `textContent`，绝不 `innerHTML`；商品标题标 `translate="no"`，
记录正文带 class `i18n-data`（留给浏览器翻译，界面本身不翻）。终端日志不打印它们。

时间都是 epoch 毫秒。闪购助手和本程序在同一台机器上、用同一个系统时钟，倒计时由页面拿 `ends_ms` / `next_ms`
用本机时钟自己每秒倒数，后端不推倒计时。

## 5. 轮询节奏与陈旧

- 连上：每 3 秒；连不上或读不出：每 15 秒；点「打开闪购助手」、`open` 返回 0 之后 30 秒内一律 3 秒，并马上查一次。
- **连续两次没查到才发布**：连上以后第一次没查到（连不上、读不出、没回话），`StatusTracker` 交回上一份状态，
  陈旧和卡住的计时都留着，调用方看到的还是 `ok`，3 秒后再查；连续第二次才发布空状态、计时清零、退到 15 秒。
  一次抖动（偶尔一次超时、事件循环卡了一下）不至于清空浮层、把按钮染橙 15 秒、把快满 60 秒的卡住计时清零
  （否则 60 秒该报的卡住要推迟到两分多钟）；真的退出了也只晚 3 秒说。还没连上过、或上一次也没查到时照实发布。
- 每一跳单独兜异常（一个 `except` 包住整个循环，出一次错轮询就永久停了），同一种异常终端只打一行。
  连不上、读不出（原因变了也算）只在跳变时打一行。
- 陈旧（`stale`）：**同一个 `updated_at` 在单调时钟上停了超过 15 秒**（null、认不出的都当同一个「没有」）。
  不拿 `time.time()` 去减 `updated_at`：合盖睡眠时单调时钟不走（macOS 上 `time.monotonic` 是 `mach_absolute_time`）、
  墙钟照走，醒来第一跳闪购助手多半还没刷新，墙钟一减就是睡了多久，会白白变一次色；墙钟往前往后调也不受影响。
  代价是刚连上时要先看满 15 秒才判得出陈旧（哪怕它早就停了）。`updated_ms` 照旧下发 `updated_at` 的墙钟时刻，给页面写「上次更新」。
  `page=error` 时闪购助手不刷新 `updated_at`，15 秒后自然变成陈旧。
- `parse_iso` 自己拆 ISO 8601，不用 `datetime.fromisoformat`：CI 最低是 Python 3.10，它不认结尾的 `Z`。

## 6. 卡住（stuck）与商品排序

- **卡住**：商品 `state=stopped`、`enabled=true`，且闪购助手 `running=true`、`paused_reason=null`，**连续 60 秒**都这样。
  后端按 `product_id` 用单调时钟记起点；任一条件断了、商品不在表里、连续两次没查到，就清零；没有 id 的永远不算。
  60 秒来自闪购助手会话的实测：续开在屏幕上 2–5 秒，循环闪购重设 8–15 秒，商品多、卡片不在可视区时 30–40 秒。
  一停就变色的话，每次正常续开按钮都会闪一下橙色。
- 浮层里条件满足、还没满 60 秒的商品写「等闪购助手续开」，满了写「停了超过 1 分钟」。前端的条件
  （`flash.js flashWaitingRenewal`）和后端的 `flash_sale.stuck_candidate` 一字不差：`enabled` 只认 true。
- **排序**（要人看的在前，同一档保持闪购助手给的顺序）：卡住的 stopped → pending → unknown → live → between_rounds →
  没卡住的 stopped（还在续开计时里、或不自动续开）→ no_sale → ended_by_user → 认不出的。后端 `flash_sale._rank` 和
  前端 `flash.js flashRank` 是同一张表；前端再排一次是稳定排序，对后端排好的结果没有影响。
- 浮层先列 40 个，其余收进「还有 N 个」，点开全列（浮层本身可滚动）。点开后按钮自己藏起来，焦点要是在它身上就挪到
  商品列表（`#flash-products` 带 `tabindex="-1"`，不滚动），键盘和读屏的人不会掉回页面开头。

## 7. 什么时候变色

按钮变橙色并带一个 7px 的橙点（形状和颜色一起变；点用 `--orange-ink`，叠在按钮的橙底上浅色 4.64:1、深色 4.98:1，
过非文字元素的 3:1），`title` / `aria-label` 跟着写「闪购助手 · 要处理」。任一成立：

1. `stale`：连得上，但超过 15 秒没更新；
2. `page`：page 是 login_required、captcha、error；或 page 是 no_list 且 running 为 true；
3. `paused`：paused_reason 不是 null 也不是 not_started（awaiting_approval、captcha、user_interaction、error、认不出的都算）；
4. `stuck`：有商品卡住（§6）；
5. `bad_response`：9334 上有东西，但没拿到能用的回话——读不出，或者连得上、2 秒内没回完（`why=no_answer`，
   闪购助手卡住时就是这样。用户定的规则里只写了「连不上不变色」「回了但读不出变色」，连得上不回话是按「漏报代价大」
   归到变色这一边的）；
6. 未读的问题记录（前端算，不在 `attention` 里）：level=error，或 level=warn 且正文以 ❌ 开头，按 `(at_ms, text)`
   和「已看过」比。打开浮层即记成已看（按钮马上恢复原色），存 localStorage 的 `flashSeen`（读写都包 try/catch，
   存不了时只在这一页记着）。浮层开着期间照**打开前**的「已看过」画：打开那一刻没看过的问题记录照样加粗列着（比最后 5 条早
   的也列），主句照样写「有 n 条新的问题」，关上再开才收起——不然让按钮变色的那一条一打开就从浮层里不见了。
   浮层开着时新来的问题记录照样算没看过（按钮再变色），下次打开才记。

不变色：连不上（连接被拒，闪购助手没开）、正在查看、not_started、loading、no_page、`enabled=false` 的 stopped、
ended_by_user、no_sale、unknown。

1–5 由后端 `flash_sale.attention()` 算，`attention` 表按上面的顺序排好。浮层顶部第一句写最要紧的那条
（stale → page → paused → stuck → bad_response → 认不出的提醒代码 → 未读记录），后面跟着只是说明现状的句子
（商品后台正在打开、还没开始运行……）。只写看到的状态，不写原因（同 CLAUDE.md 第八条的规矩）。

## 8. 「打开闪购助手」

- 页面发 `{"type": "open_flash_helper"}`，不带参数（带了也不看）。argv 写死成 `open -a <检测到的完整路径>`
  （`flash_sale.open_argv`），页面发什么都进不了 argv。
- `handle_control` 立刻返回，`open` 放后台跑：不挡同一页面后面的「停止」。上一次还没返回时，连点只跑一次。
- 成功（返回 0）：不发任何提示，状态自己会从「没在运行」变成别的。
- 失败：终端一行，再发 `{"type": "notice", "where": "flash", "text": …}`，页面写进浮层里的 `#flash-note`（全局提示照样排队）。
  只有两句：「没能打开闪购助手（open 返回 {rc}）。」「没能打开闪购助手。」只写返回码，不猜原因。
- 和本地服务断开时按钮置灰；点了没发出去，`#flash-note` 写「与本地服务断开，正在重连——稍候再试。」，不装作点成了。
- 只有本机控制页能发：手机入站只认 retranslate，浮窗页的入站一律丢弃。

## 9. 顶栏宽度：「Aa」

多一颗按钮要 36px，原来的顶栏余量放不下。用户定的方案甲：有闪购按钮（`body.has-flash-btn`）、窗口窄于 1400px 时，
字号滑块收成一颗 28px 的「Aa」（`#font-btn`），点开的小浮层就是原来的字号控件（`label#font-ctl` 原地改成浮层样式）。
宽窗口、没装闪购助手时一切照旧。按钮上的字：中文 1000–1399px 只露「闪购」两个字，1400px 起图标加字；英文 1600px 起才露
Flash Sale。实测余量和 G10 场景在 `docs/i18n-style.md` §4.2。

## 10. 隔离

- **手机同看**：`flash_sale` 不在 `app/viewer.py` 的 ALLOW 里，另外显式写进 DENY；`replay_snapshot` 只挑固定几个键。
- **浮窗**：不在 `app/server.py` 的 `FLOAT_TYPES` / `FLOAT_CONFIG_KEYS` 里；`float.html` 不加载 flash.js。
- **报警（CLAUDE.md 第六条）**：闪购的提醒只走这颗按钮的颜色和这个浮层，不借 incident、status、顶部提示条、
  「疑似违禁词」面板，也不挪报警的位置。不发系统通知。
- **审计、session 日志**：一条都不写。终端只在状态跳变时打一行中文（已连上、连不上、读不出、schema 不是 1、
  轮询出错、执行 open），不打印商品标题和记录正文。

## 11. 测试

**绝不连真的 127.0.0.1:9334**（开发机上闪购助手可能正开着，返回的是真实商品），也不真的执行 `open`。

- `tests/conftest.py` 有一个 autouse 夹具：`flash_sale.app_candidates` 指到不存在的路径、`flash_sale.STATUS_URL`
  指到 `http://127.0.0.1:9/status`（没人听的端口）。任何测试都看不到本机真装的闪购助手。
  `fetch_status` 的默认地址在调用时才取 `STATUS_URL`，这张网才兜得住。
- 要测「装了」：`attach_flash_sale(platform="darwin", candidates=(tmp 目录,))`。
- 要测「连上了」：`tests/test_flash_sale_http.py` 用 `aiohttp.test_utils.TestServer` 在本机临时端口起假的 `/status`，
  照契约检查 `Host`（不对就回 403）；连得上不回话、回到一半断开、回的不是 HTTP 这几种用本机临时端口上的裸 TCP 对端
  （或只 `listen()` 不 `accept()` 的套接字）测。`tests/test_pipeline_flash.py` 换掉 `_flash_fetch`、`_flash_exec`、
  `_flash_mono`、`_flash_sleep`，不起真的网络和子进程。
- 夹具：`tests/fixtures/flash_sale/{idle,running,awaiting}.json` 是契约的虚构样例原样（标题都是「(Fictional)」，
  店铺是 `example_shop`）；`message-running.json` 是推给页面的形状，前端的 node 测试用它。
  `test_flash_sale.py::test_the_node_fixture_is_what_the_tracker_sends` 钉着它就是 `StatusTracker` 对 running.json
  计满 60 秒后的输出（只差商品顺序），两边谁改了形状都会红。
- 前端：`tests/flash.test.mjs`、`flash.en.test.mjs`（纯函数 + 假 DOM 渲染）、`flash-button.test.mjs`（整页接线）、
  `popover.test.mjs`（顶栏浮层登记表）。
- G10（`TLT_DOM_SCAN=1 pytest tests/test_i18n_dom.py`）：`live-flash*`、`live-flash-zh*`、`home-flash*` 场景，
  `config.flash_sale` 由 `flash_sale.StatusTracker` 从 running.json 生成。直播转写进行中不开 Chrome。

## 12. 不做的事

- 不控制闪购助手，浮层里没有开始、停止、批准、续开这类按钮。
- 不显示在手机同看页和浮窗里。
- 不翻译商品标题和记录正文；不显示 `shop`、`page_version`。
- 不用 mdfind 找程序，不按 bundle id 打开。
- 仓库里（代码、测试、注释、文档）不出现真实的品牌、店铺、主播名，测试只用契约里的虚构样例。
