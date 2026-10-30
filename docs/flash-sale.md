# 顶栏「闪购」：给维护者的说明

2026-10-27 加的。中控电脑上如果装着「闪购助手」（本机另一个程序，不随本程序分发），控制页顶栏多一颗「闪购」按钮，
点开是一个浮层：闪购助手开没开、要不要人处理、每个商品的闪购状态、最近的记录，外加「打开闪购助手」。
第一步（PR #155）只读。第二步起，闪购助手 0.8.0 及以上时，浮层里还能「开始」「停止」「继续」（§9）：等于替人按下
闪购助手面板上的那个大按钮，不新建闪购、不改价、不加商品、不批准、不续开，第一次的确认框照旧在专用 Chrome 里点。
本程序不碰 TikTok：只和闪购助手在本机的接口打交道（读状态、发这三个指令），再就是 `open -a` 把它打开。

README、FAQ 不提这个功能（公开读者拿不到闪购助手）。英文怎么写看 `docs/i18n-style.md` §1.3（术语）、§3 K（文案表）、
§4.2（顶栏宽度和「Aa」）。

## 1. 数据流

```
闪购助手  GET http://127.0.0.1:9334/status
   │  后端每 3 秒取一次；连不上或读不出时 15 秒（连上以后第一次没查到先照旧，3 秒后再查）；
   │  点「打开」成功后、开始 / 停止 / 继续被接受后，30 秒内一律 3 秒，并马上查一次（§5）
   ▼
app/pipeline_flash.py  FlashSaleMixin._flash_watch
   │  app/flash_sale.py：fetch_status → StatusTracker.update（normalize、判陈旧、计卡住、排序、算 attention）
   │  叠上控制指令的进度 control（§9），和上次发布的比，没变就不发
   ▼
server.config["flash_sale"] = 新 dict                hello 整份带上，重连自动补发
server.broadcast({"type": "flash_sale", …})          i18n.DATA_ONLY；手机 DENY；浮窗不收
   ▼
web/app.js（接线、未读、开合） → web/flash.js（文案、排序、渲染） → #flash-btn 的颜色、#flash-panel 的内容
   ▲
「打开闪购助手」 → {"type": "open_flash_helper"} → Pipeline.handle_control → open -a <完整路径>
打开浮层          → {"type": "flash_find"} → 再找一次 .app（只看文件系统），找到的变了才发布（状态里的 app_found，§2）
「开始」「停止」「继续」 → {"type": "flash_control", "action": …} → Pipeline.handle_control
   → 读控制密钥 → POST http://127.0.0.1:9334/<action> → {"type": "flash_control_result", …} 广播给控制页（§9）
```

页面**不直连** 9334：闪购助手的 CORS 只放 `http://127.0.0.1:8765` 和 `http://localhost:8765`，本程序的控制端口
可能漂到 8766–8774，跨域会失败。所以由后端去取，再推给页面。

## 2. 有没有这个按钮

- 找程序（直播助手平台轮廓 §7.2 第 2 步，2026-10-29）按先后看三处：`settings.json` 里本机指定的位置
  （`{"flash_sale": {"app_path": "…/闪购助手.app"}}`，`flash_sale.configured_app`）、`/Applications/闪购助手.app`、
  `~/Applications/闪购助手.app`（`flash_sale.app_candidates`）。不跑 mdfind；不是 macOS 一律当没装。按**完整路径**判断和打开，
  检测到的和打开的是同一个东西；不按 bundle id 打开（它不进公开仓库）。
  - 指定的位置只认完整路径（可以用 `~`）、最后一截正好是 `闪购助手.app` 的（「闪购助手 2.app」不认）；不存在、不是文件夹、
    不是完整路径、名字不对、不是字符串的，跳过，终端各一行（同一种只打一次），接着看另外两处。
  - 这一步只读这个键，不写。「选一下它在哪」（pywebview 的文件对话框）在第 6 步首页「工具」行里。
  - 都没找到：终端一行，列出看过哪几处、指定位置写在哪个键里（双击 `.app` 启动时终端输出进运行日志）。
- 启动时判一次：`main.py` 在第一次 hello 之前 `await pipeline.attach_flash_sale()`。
- **没找到、但闪购助手正开着**（launch-7，用户 10-29 选 B）：没找到 `.app` 时，当场读一次 9334 的 `/status`
  （最多 4 秒；没人在听时立刻连不上），回话像闪购助手的（读懂了、`schema` 是 1、`page` 是契约里的代码，`flash_sale.status_refusal`，
  和开始、继续发之前的那三条一样）就照样接上：有按钮、能看能停，只是没有「打开闪购助手」。推给页面的状态里
  `app_found` 是 `false`（找到了是 `true`；状态里没有这个键时页面当找到了）。这时闪购助手又没在运行、读不出的话，那一排一颗按钮
  都没有，旁边写一句「把它拖进「应用程序」，再关上、打开这个浮层……」（`docs/i18n-style.md` #181）。回话不像闪购助手的
  （schema、page 对不上）终端一行，不接。
- **打开浮层时再找一次**：页面打开闪购浮层时发 `{"type": "flash_find"}`（不带参数，带了也不看），后端照上面三处再找一遍
  （只看文件系统，不连 9334），找到的变了才发布（终端一行「找到了 …」或「没找到 …」）。装进「应用程序」、挪了位置、改了
  `settings.json`，都不用重启。没有按钮就没有浮层：启动时既没找到、9334 上也没有它的，要等第 6 步首页「工具」行的「重新找」，
  现在还是要重启。手机发不进 `flash_find`（入站只认 retranslate），浮窗页的入站一律丢弃。
- 演示模式（`--demo`）不接：不查 9334、不画按钮，`flash_control` 指令也什么都不做。演示模式是拿来截图、
  录屏验界面的，9334 上是真实店铺的真实商品，不能进截图，更不能被演示时的一次点击停掉
  （`tests/test_pipeline_flash.py` 读 main.py 的语法树钉着这个 `if not args.demo`）。要看按钮和浮层的样子，
  用 G10 的 `live-flash*` / `home-flash*` 场景（状态由 `StatusTracker` 从契约的虚构样例生成）。
- 没装：`server.config` 里**没有** `flash_sale` 这个键，页面就不画按钮（同浮窗的 `float_window`）。
- 装了：先同步写一份 `conn: "checking"` 的空状态（第一次 hello 就带着，按钮不会晚一拍冒出来），再起轮询。
- 不在 `Pipeline.__init__` 里检测：测试大量构造 Pipeline，开发机上可能真装着闪购助手，放在那里本机和 CI 的结果
  就会不一样，轮询还会真的去连本机的 9334。
- 按钮首页和直播中都显示（用户 10-27 定）：开播前就看得到闪购助手开没开、要不要登录，也能在首页点「打开」。
- settings 只读 `flash_sale` 这个键里的 `app_path`（上面），没有开关。第 11 步迁到 `tools` 键（直播助手平台轮廓 §5.8）。

## 3. 闪购助手的接口（契约摘要）

闪购助手那边定的格式，这里只记本程序用到的部分：

- `GET http://127.0.0.1:9334/status`，只监听本机。`Host` 必须是 `127.0.0.1:9334` 或 `localhost:9334`，否则 403
  （防 DNS 重绑定）；aiohttp 默认发的就是前者。`/status` 只认 GET、HEAD。0.8.0 起另有控制接口
  `POST /start`、`/stop`、`/resume`（防冒充条件、返回码见 §9）；0.8.0 以前除 `/status` 以外的路径一律 404。
- 闪购助手和它开的专用 Chrome 同生共死：Chrome 一关它就退出，接口就连不上。**连不上（连接被拒）= 闪购助手没在运行**。
  连得上却不回话不算：闪购助手卡住（死锁、被挂起、HTTP 线程堵住）时内核照样完成握手，我们这边只看到读超时，
  那正是要人去看的时候，见 §4 的 `why`。
- `updated_at`：最后一次成功判断出状态的时刻（UTC，`Z` 结尾），后台每 2 秒刷新一次；`page=error` 时不刷新；
  还没判断过时为 null。
- 代码表（认不出的代码原样显示，不给通用文案）：
  - `page`：ok、no_page、login_required、loading、no_list、captcha、error；
  - `paused_reason`：null（正常运行）、not_started、awaiting_approval、captcha、user_interaction、error；
  - 商品 `state`：live、between_rounds、stopped、ended_by_user、pending、no_sale、unknown；
  - 商品 `skip`（0.8.2 起）：null 表示闪购助手会续开；不为 null 是「故意不续开」的原因：slot_1（1 号位）、
    no_stock（没有库存）、max_renewals（续开次数到上限）、auto_start_off（没开自动开始）、no_position（读不到序号），
    0.9.2 起还有 next_to_pin（置顶商品下面的第一个商品，按列表顺序，也就是下一个要置顶的那个），0.9.2.2 起还有
    top_n（Buy now 列表从上往下的前 N 个，N 在闪购助手面板上设、默认 2：不点 Start，也不重新设置）。`enabled=false` 的商品
    不标 skip，只看 enabled（「不自动续开」那句话 0.10.0 起看 `checked`，见下面的商品 `checked`）。本程序不分版本：skip 不为 null 就不算卡住（§6），那一行写闪购助手给的原因，七句短说明
    各个版本都对。哪个商品标 slot_1、next_to_pin、no_position、top_n，各版本不一样：
    - 0.9.2：1 号位一直留着（slot_1），有置顶时 next_to_pin 那个也留，两个代码会同时出现；next_to_pin 那个同时又是
      1 号位时报 slot_1。读不到序号的报 no_position；
    - 0.9.2.1 起：有置顶时只留 next_to_pin 那一个，1 号位和置顶的商品本身照常处理，也不看序号（不会因为读不到序号报
      no_position）；没有置顶、看不出有没有置顶、或者置顶的商品不在已加载的列表里时，才按 1 号位留（slot_1），读不到
      序号的报 no_position；
    - 0.9.2.2 起：不再看哪个商品置顶、有没有置顶，只看位置：列表从上往下显示的前 N 个报 top_n（置顶的商品显示在
      最上面时就是第 1 个），不点 Start、也不重新设置；slot_1、next_to_pin 不再出现（用户 10-29 定）；
  - `alerts[].level`：error（报警）、warn（失败、跳过、暂停、等确认）、info。
- 商品 `position`（0.8.2 起）：卡片左边的序号（整数），取最后一次在屏幕上看到这张卡片时的值；从没出现过、读不到时为 null。
  0.8.2 以前没有 `skip`、`position` 这两个键，都当 null。
- 商品 `checked`（0.10.0 起，布尔）：面板上的勾现在画成什么样。取消勾选是 false，没动过、还没闪购是 true，没运行时已停的也是 true；
  运行中已停、又从没进过名单的也是 false，所以 false 不一定是主播取消的。`enabled` 为 false 时，`checked` 为 true 是还没进名单但允许进，
  为 false 是闪购助手不会去动它，这两种 false 里「不自动续开」都成立。0.9.x 没有这个键，
  也不补默认值，本程序当 null（读不出、不是布尔同样当 null）。`enabled` 的含义不变，但 0.10.0 起它不再等于面板上的勾：
  主播没动过、还没闪购（或没运行时已停）的商品，面板上打勾、有闪购时自动加入，`enabled` 却是 false；主播真取消勾选的也是 false。
  所以「不自动续开」那句话看 `checked`，卡住、等续开和排序照旧只认 `enabled`（§6）。
- `alerts` 最多 20 条，最早的在前。页面上跑的还是旧脚本时它一直是空数组，这是正常的，不能当成出错。
- 一场最多 300 个商品（实际见过 103 个）。0.8.0 起每翻完一轮刷新一次 Buy now 列表，列表先变短再补回
  （102 个先变 30 个），排在后面的商品会暂时不在 `/status` 里（§6 的卡住计时因此不随它清零）。

9334 上回话的不一定是闪购助手（本机任何进程都能占这个端口），所以后端读体限 1 MiB、宽容解析：
不认识的键丢掉（`shop`、`page_version` 也丢），类型不对的当 null，字符串截长、去掉控制字符。

## 4. 推给页面的状态

`hello.config.flash_sale` 和 `{"type": "flash_sale", …}` 同形（后者多一个 `type`）。所有键永远都在：

| 键 | 含义 |
|---|---|
| `conn` | `checking` 启动后还没查到；`ok` 连上并读懂了；`unreachable` 连不上（连接被拒，或连接阶段就超时：9334 上没人在听；Windows 上连本机没人听的端口要重试约 2 秒才被拒，所以连接和读分开计时）；`bad_response` 9334 上有东西，但没拿到能用的回话（原因见 `why`）。不是 `ok` 时商品和记录全部清空，不留旧数据。连上以后第一次没查到不发布（§5） |
| `why` | 只在 `bad_response` 时有，其余为 null：`no_answer`（连上了，但 2 秒内没回话，或者没回话就断开、被重置；总时长上限 4 秒）、`cut_off`（回到一半断开）、`http`（回的不是 HTTP）、`http_<状态码>`（非 200，302 也不跟）、`too_large`（超过 1 MiB）、`json`（不是 JSON）、`not_object`（JSON 但不是对象）。页面只给 `no_answer` 单独一句「127.0.0.1:9334 在 2 秒内没有回应。」，其余都是「返回的内容读不出来」 |
| `stale` | 只在 `conn=ok` 时可能为 true，见 §5 |
| `updated_ms` | 只在 `stale` 为 true、且 `updated_at` 认得出时是 epoch 毫秒（页面写「上次更新 HH:MM:SS」），其余为 null。不下发 `updated_at` 本身：它每 2 秒变一次，下发了去重就失效 |
| `app_version`、`page`、`running`、`paused_reason` | 照接口，清洗过 |
| `products[]` | `{id, title, state, round, ends_ms, next_ms, stock_left, stock_total, renewals, enabled, checked, position, skip, stuck}`，最多 300 个，**后端已排好序**（§6）。`position`、`skip` 照接口（0.8.2 以前都是 null），`skip` 认不出的代码原样（清洗后）；`checked` 照接口（0.10.0 起有，0.9.x 是 null，不补默认值，§3） |
| `products_total` | 闪购助手给了多少个商品（含超出 300 被截掉的）。比 `products` 长时页面在摘要末尾写「未列出 n」，不静默丢掉 |
| `alerts[]` | `{at_ms, level, text}`，最后 20 条，最早的在前 |
| `attention` | 要人处理的原因代码，已按浮层主句的优先级排好，见 §7。空表 = 不变色 |
| `stuck_count` | 卡住的商品数 |
| `control` | 控制指令的进度（§9）：null，或 `{action, phase}`。`action` 是 start / stop / resume；`phase` 是 `sending`（在途，按钮禁用）、`waiting`（闪购助手已接受，等它生效；开始、继续生效以后还要等页面稳下来，§9.3；最多 30 秒），或 `retrying`（只有停止：没被接受，隔 3 秒再试，§9.2）。不带时刻：等多久由后端判，页面不自己计时 |

`title` 和 `alerts[].text` 是**数据**：页面一律 `textContent`，绝不 `innerHTML`；商品标题标 `translate="no"`，
记录正文带 class `i18n-data`（留给浏览器翻译，界面本身不翻）。终端日志不打印它们。

时间都是 epoch 毫秒。闪购助手和本程序在同一台机器上、用同一个系统时钟，倒计时由页面拿 `ends_ms` / `next_ms`
用本机时钟自己每秒倒数，后端不推倒计时。

## 5. 轮询节奏与陈旧

- 连上：每 3 秒；连不上或读不出：每 15 秒；点「打开闪购助手」、`open` 返回 0 之后 30 秒内一律 3 秒，并马上查一次。
  开始、停止、继续的指令被闪购助手接受之后同样：30 秒内一律 3 秒（这期间闪购助手连不上了也是 3 秒），并马上查一次，
  每一跳看指令生效了没有（§9.3）。
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
  `page=error` 时闪购助手不刷新 `updated_at`，15 秒后自然变成陈旧（开始、继续被接受后等生效的那 30 秒里不变色，§7）。
- `parse_iso` 自己拆 ISO 8601，不用 `datetime.fromisoformat`：CI 最低是 Python 3.10，它不认结尾的 `Z`。

## 6. 卡住（stuck）与商品排序

- **卡住**：商品 `state=stopped`、`enabled=true`、`skip=null`，且闪购助手 `running=true`、`paused_reason=null`，
  **连续 60 秒**都这样。`skip` 不为 null 是闪购助手说了故意不续开（1 号位、没库存……），停多久都不算卡住。
  `checked`（0.10.0 起，§3）不参与，只认 `enabled`：`checked=true`、`enabled=false` 的商品停着不算卡住。
  后端按 `product_id` 用单调时钟记起点。60 秒来自闪购助手会话的实测：续开在屏幕上 2–5 秒，循环闪购重设 8–15 秒，
  商品多、卡片不在可视区时 30–40 秒；0.8.1 起停了以后改走 ⚡ 重设，回到进行中要 10–20 秒，中间短暂是 pending。
  一停就变色的话，每次正常续开按钮都会闪一下橙色。
- **什么时候清零**：看到它不再满足条件（state 不是 stopped、enabled 不是 true、skip 不为 null）；全局条件不满足
  （running 不是 true、paused_reason 不是 null），这时不在列表里的也一起清；连续两次没查到（§5）。
  **商品暂时不在列表里时不清零，接着计**：0.8.0 起每翻完一轮刷新一次 Buy now，列表先变短再补回，按以前「不在表里就清零」
  的做法，排在后面的商品每轮都被清一次，真卡住的永远攒不满 60 秒。10 分钟都没再出现的从表里丢掉（隔了这么久才回来的
  从头计），表最多记 600 个，多了先丢最久没出现的。没有 id 的永远不算。
- 浮层里条件满足、还没满 60 秒的商品写「等闪购助手续开」，满了写「停了超过 1 分钟」。前端的条件
  （`flash.js flashWaitingRenewal`）和后端的 `flash_sale.stuck_candidate` 一字不差：`enabled` 只认 true，`skip` 只认 null
  （没有这个键也算 null），`checked` 不看。两边吃同一张表 `tests/fixtures/flash_sale/shared-rules.json`（`waiting_renewal`）。
- 那一行细节的最后写不写「不自动续开」（`flash.js flashAutoRenewOff`，只有前端有这条规则，后端只把 `checked` 规整后传下去）：
  - `checked` 是 false：写，哪怕 `enabled` 是 true，面板上的勾为准；
  - `checked` 是 true：不写，哪怕 `enabled` 是 false（0.10.0 起没动过、还没闪购的商品就是这样）；
  - 没有或读不出（null、不是布尔）：退回老规则，`enabled` 是 false 才写。0.9.x 没有这个键，显示一字不变。

  这句和「等闪购助手续开」各看各的键。`checked=false`、`enabled=true` 的商品停着时两句都会写；照 0.10.0 的定义，主播取消勾选时
  `enabled` 也是 false，正常不会有这个组合。
- `skip` 不为 null 的商品，那一行细节的最前面写闪购助手说的原因（`flash.js flashSkipText`，前五句的文案是闪购助手会话给的，
  next_to_pin 那句照它 0.9.2 的说明写，top_n 那句照 slot_1 的句式写；0.9.2.1 改的是哪个商品标哪个代码（§3），
  每个代码的意思没变）：
  slot_1「1 号位，不续开」、top_n「排在最前面，不续开」、next_to_pin「下一个要置顶，不续开」、no_stock「没有库存，不续开」、max_renewals「续开次数到上限」、
  auto_start_off「没开自动开始」、no_position「读不到序号，先不处理」；认不出的写「不续开：」加原样的代码（`translate="no"`）。`position` 写成标题前的
  「#12」（`.flash-pos`，在会被省略号截掉的标题外面，两种语言一样）；null 就不写。英文见 `docs/i18n-style.md` §3 K #175。
- **排序**（要人看的在前）：卡住的 stopped → pending → unknown → live → between_rounds →
  没卡住的 stopped（还在续开计时里、`enabled=false`、故意不续开，`checked` 不参与）→ no_sale → ended_by_user → 认不出的。**同一档里**按
  `position`（卡片序号）从小到大，null 排在最后，再相同保持闪购助手给的顺序（稳定排序）。后端 `flash_sale._rank` 和
  前端 `flash.js flashRank` 是同一张档位表；档内的规则后端在 `flash_sale.product_sort_key`，前端在 `flashSortProducts`，
  两边吃 `shared-rules.json` 的 `sort`。前端再排一次是稳定排序，对后端排好的结果没有影响。
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

不变色：连不上（连接被拒，闪购助手没开）、正在查看、not_started、loading、no_page、`enabled=false` 的 stopped（不管 `checked` 是什么）、
`skip` 不为 null 的 stopped、ended_by_user、no_sale、unknown。

**开始以后的过渡**（用户 10-27 定）：「开始」或「继续」被闪购助手接受、还在等它生效的那段时间（`control` 是
`{action: start 或 resume, phase: waiting}`，最多 30 秒，见 §9.3），第 1 条不算，第 2 条里 page 是 loading、error、
no_list 的也不算：POST /start 先刷新页面，刷新期间 `/status` 会短暂报 loading、error（这时 `updated_at` 不刷新，
15 秒后还会变陈旧），列表回来之前是 no_list。这段时间浮层主句写「正在开始…（闪购助手在刷新页面）」，按钮不变橙、
不出红字，免得刚点完开始看到红字又去点停止。生效了而且页面稳下来了（连上了、不陈旧、page 不是 loading、error、no_list）、
或者等满 30 秒（`control` 收掉了），照上面的原规则。只看 `running` 变 true 不够：刷新完闪购助手自己接着运行，`running`
先变 true，列表回来之前 page 是 no_list——这时就收掉的话，no_list 加 `running=true` 马上变橙，这段过渡对开始等于没有
（2026-10-28 审查）。
`stale`、`page` 两个字段照旧是看到的样子，只是不进 `attention`。login_required、captcha、暂停、卡住、读不出这段时间
照常变色；停止没有这个过渡；还在途（`phase=sending`）也不算。后端 `flash_sale.starting()` / `attention()` 和前端
吃同一张表 `shared-rules.json` 的 `attention`。

前端那一半（`flash.js`）：`flashStarting` 是后端 `starting()` 的抄本；`stale` 照实是 true，所以 `flashNeedsAttention`
和 `flashLines` 自己也得照同一条规矩——过渡里不拿 `stale` 变色、不写陈旧那两句，page 是 loading、error、no_list 的那句
不写（不是写成灰字：主句已经说了在刷新），「闪购助手开着，但还没开始运行。」也不写；要处理的照常排在前面，过渡那一句
跟在后面。浮层标题旁的胶囊和顶栏按钮的 `title` / `aria-label`（`flashChip`、`flashButtonState`）这段时间也不写
「未开始」，写和按钮上同一句「正在开始…」（继续是「正在继续…」），颜色照「未开始」不变——按钮上写着正在开始、胶囊却写未开始，
很容易读成「没开起来」又去点一次，这正是这段过渡要免的（2026-10-28 审查）；要处理的照常先写「要处理」。
继续被接受后同样不变色，但主句写「正在继续…」，不写「在刷新页面」：契约只说 `POST /start` 会先刷新页面，继续没说（不猜，
第八条）。**这一处有意和第二轮任务原文不同**：原文（`flash-control-followups.js` 的 TASK 第 2 条）写的是「开始或继续被接受后
30 秒内……主句写『正在开始…（闪购助手在刷新页面）』」；用户 10-27 拍板的那个问题只问了点「开始」以后这几秒怎么显示。
要照原文给继续也写「在刷新页面」，需要用户确认（目前还没问）。30 秒的边由后端定：页面没有计时，`control` 一收掉就照原规则画。

1–5 由后端 `flash_sale.attention()` 算，`attention` 表按上面的顺序排好。浮层顶部第一句写最要紧的那条
（stale → page → paused → stuck → bad_response → 认不出的提醒代码 → 未读记录），后面跟着只是说明现状的句子
（商品后台正在打开、还没开始运行……）。只写看到的状态，不写原因（同 CLAUDE.md 第八条的规矩）。

## 8. 「打开闪购助手」

- 2026-10-28 起在浮层顶部那一排的最后（§9.6），不再在最底下：42 个商品时用户在最底下找不到它。连不上、版本太旧时
  这一排只剩它一颗。
- 页面发 `{"type": "open_flash_helper"}`，不带参数（带了也不看）。argv 写死成 `open -a <检测到的完整路径>`
  （`flash_sale.open_argv`），页面发什么都进不了 argv。
- `handle_control` 立刻返回，`open` 放后台跑：不挡同一页面后面的「停止」。上一次还没返回时，连点只跑一次。
- 成功（返回 0）：不发任何提示，状态自己会从「没在运行」变成别的。
- 失败：终端一行，再发 `{"type": "notice", "where": "flash", "text": …}`，页面写进浮层里的 `#flash-note`（全局提示照样排队）。
  2026-10-29（launch-8）起 open 的标准错误接成管道读回来：句子里照实写返回码和系统给的错误号（`Code=-10669`、
  `error -10810` 这两种写法，`flash_sale.open_error_codes`，最多 3 个），再说能做什么——「可以在访达里双击闪购助手，再试一次。」
  （`docs/i18n-style.md` #165）。只写数，不解释那个号、不猜原因（CLAUDE.md 第八条）。open 没能执行、10 秒没返回时没有返回码，
  只写「没能打开闪购助手。」加那半句。标准错误整理成一行（去掉控制字符，最多 300 个字）另进终端。
- 没找到 `.app`（状态里 `app_found` 是 `false`，§2）时不画这颗按钮；本机脚本硬发 `open_flash_helper` 也什么都不做。
- 和本地服务断开时按钮置灰；点了没发出去，`#flash-note` 写「与本地服务断开，正在重连——稍候再试。」，不装作点成了。
- 本机控制页能发（没带 Origin 的本机进程也能，见 §9.4）：手机入站只认 retranslate，浮窗页的入站一律丢弃。

## 9. 开始、停止、继续（闪购助手 0.8.0 起）

2026-10-27 第二步。等于替人按下闪购助手面板上的那个大按钮，不新建闪购、不改价、不加商品；第一次遇到的确认框
照旧弹在专用 Chrome 里，等人去点（这时 `paused_reason=awaiting_approval`，浮层主句本来就会说）。

| 指令 | 闪购助手的接口 | 等于面板上的 |
|---|---|---|
| start | `POST /start` | 「▶ 开始自动续开」：先刷新页面，刷新完自动开始运行，原来就停着的商品依次续开 |
| stop | `POST /stop` | 「⏸ 停止」 |
| resume | `POST /resume` | 暂停（确认框里点了取消，`paused_reason=user_interaction`）后的「▶ 继续」 |

§9.1–§9.5 写后端（`app/flash_sale.py` 的控制部分、`app/pipeline_flash.py` 的 `_flash_control*`），§9.6 写页面
（`web/index.html` 的 `#flash-ctl`、`web/flash.js` 的 `flashControlView` 等、`web/app.js` 的接线）。

**发布闸**（`flash_sale.START_ENABLED`，出厂 `False`；先例 `i18n.I18N_ENABLED`）：开始、继续会让闪购助手对观众做事，
等直播助手平台轮廓第 3 步（动作记录、指令来源、两步确认）都合进来再打开（轮廓 §9.2 第 8 条，用户 10-29 同意）。这样这一步
合进 main 以后，main 随时还能发 hotfix（hotfix 只能从 main 打），不会把「会动手、没记录」的开始、继续一起带出去。
- 关着时：后端收到开始、继续什么都不做（终端一行「没有发出（开始、继续还没开放）」，不读密钥、不回结果），也不让停止的重试
  作废；页面不画「开始」「继续」（§9.6）。**停止不受闸管**（停是安全的方向）。
- 页面怎么知道：装了闪购助手时，hello 的 config 里和 `flash_sale` 一起带 `flash_start_enabled`（布尔）。只有 `true` 算开闸，
  没有这个键、别的值都当关着。浮窗不收（不在 `FLOAT_CONFIG_KEYS` 里），手机收不到 config。
- 开闸：把 `START_ENABLED` 改成 `True`，同时改 `tests/test_pipeline_flash.py::test_the_start_gate_ships_closed`。回退就是改回去。
  测闸某一边的语义要显式 monkeypatch 它：`tests/test_pipeline_flash.py` 有一个 autouse 夹具把它打开（那个文件测的是开闸以后
  开始、继续怎么走），闸关着的几条在用例里显式关上；node 测试的整页接线同理，hello 里显式带 `flash_start_enabled: true`。

### 9.1 消息

- 页面 → 后端：`{"type": "flash_control", "action": "stop" | "resume"}`；开始点两下、两条都发（§9.2「开始点两下」）：
  `{"type": "flash_control", "action": "start", "step": "arm", "id": 编号}`，再 `{…, "step": "fire", "id": 同一个编号}`。
  只读 `action`，开始另读 `step`、`id`，别的键一概不看（地址、请求头、请求体都写死）；不在白名单 `flash_sale.CONTROL_ACTIONS`
  里的（大小写不同、带路径、带参数、不是字符串）什么都不做、不回结果。没装闪购助手、演示模式（不接闪购）时同样什么都不做。
- 后端 → 控制页：`{"type": "flash_control_result", "action", "ok", "error"}`，广播给所有控制页（`handle_control`
  不知道是哪个连接发的）。`i18n.DATA_ONLY`；手机 DENY；浮窗不收。只带代码，文案在前端。停止再试过的（§9.2「停止」）
  另带 `tries`：一共试了几次，2 起；只试了一次的不带这个键。
  `ok=true` 时 `error` 为 null：闪购助手接受了（200）。`ok=false` 时 `error` 是下面三种之一：
  - 后端自己的代码（`flash_sale.CONTROL_OWN_ERRORS`）：

    | 代码 | 意思 |
    |---|---|
    | `no_status` | 开始、继续：最近一次查 `/status` 不是连上并读懂了，或者读懂了却不像闪购助手的回话；停止：发之前当场读的那一次没读懂，或者 `schema` 不是 1。都没读密钥、没发（§9.2） |
    | `no_token` | 控制密钥读不出：没发 |
    | `unreachable` | 连不上 9334（连接被拒，或连接阶段就超时） |
    | `no_answer` | 连上了，2 秒内没回完，或者没回话就断开 |
    | `bad_reply` | 回话读不出：回的不是 HTTP、回到一半断开、超过 4 KB，或者 200 却不是 `{"ok":true}` |
    | `no_control` | 404 / 405：这个版本的闪购助手没有控制接口（0.8.0 以前只有 `/status`） |
    | `failed` | 后端自己出错（终端有异常的类型名） |
    | `not_seen` | 已接受，但 30 秒内没看到生效（§9.3）。接受之后另发的第二条，同一个 `action` |
    | `local_only` | 开始、继续来自没带 Origin 的本机进程：本机脚本只能停（§9.4）。没发 |
    | `not_confirmed` | 开始没有对得上的两下：没带 `step`、前面没有 `arm`、编号对不上、离 `arm` 不到 0.4 秒或满 6 秒（§9.2）。没发 |

  - `http_<状态码>`：403（防冒充的条件没过，例如密钥对不上）、3xx（不跟随）、5xx 等；409 但读不出代码也是 `http_409`；
  - 其余都是闪购助手 409 给的代码，原样（同 `/status` 的代码字段清洗）。契约里有 no_page、login_required、loading、
    captcha、awaiting_approval、already_running、paused、not_running、not_paused、busy、no_response、error，
    0.9.3 起还有 stop_pending（有一个停止已经收下、还在排队送到页面，只有开始和继续会收到）和 superseded
    （停止刚收下时页面里有人点了开始，以开始为准，只有停止会收到，极少）；认不出的原样交给页面显示，不猜原因。
  - 0.9.3 起停止在页面刷新、脚本没装好时也收下，回 `200 {"ok":true,"accepted":"stop","queued":true}`；
    `queued` 不认识也照成功处理，之后照常等 `/status` 的 running 变成 false。请求体写错回 400（`http_400`）。
- 状态里的 `control`（§4）是指令的进度：`{action, phase: "sending"}` 在途，`{action, phase: "waiting"}` 已接受、等生效，
  `{action: "stop", phase: "retrying"}` 停止没被接受、等着隔 3 秒再试（§9.2「停止」）。页面靠它禁用按钮、写「正在开始…」
  「正在重试…」，重连的页面从 hello 里也拿得到；不带时刻，等多久由后端判。

### 9.2 发送

- **开始、继续：发之前看状态**（`StatusTracker.control_refusal()`）：最近一次查 `/status` 必须连上并读懂了（`last_ok()`），而且
  `schema` 是 1、`page` 是契约里的七个代码之一（`flash_sale.PAGE_CODES`）。「连上以后第一次没查到」时发布的照旧是上一份
  ok 状态、页面上按钮还在，但这时也不算。不是就不读密钥、不发，`error=no_status`；后两种终端另带原因（`schema`、`page`）。
  9334 上回话的不一定是闪购助手：`normalize` 收任何 JSON 对象，`{"status":"ok"}` 甚至 `{}` 都算「连上并读懂了」，
  页面照样画；密钥只给刚刚还在好好回 `/status`、回话的样子也对得上契约的那个。在等生效的上一个指令照旧等着。
  代价：闪购助手以后改了 `schema`、加了 `page` 代码，开始、继续会一直 `no_status`，直到这里跟上（终端那一行带着原因，
  schema 不是 1 另有一行）。
- **停止：当场读一次就发，没被接受的再试**（`_flash_stop_run`；直播助手平台轮廓 §5.5、§9.2 第 7 条，用户 10-29 同意）。
  停止是出事时最要发得出去的那一个，上一条挡它的几种情况都不该挡：读失败一次以后要隔 15 秒才再读，这 15 秒里会一律
  `no_status`；闪购助手以后加一个新的 `page` 代码，旧版本程序就永远停不了它；页面正在刷新，闪购助手自己也拒。所以：
  - 不看上一次轮询读得怎么样。发之前当场读一次 `/status`（`flash_sale.stop_precheck`）：连得上、`schema` 是 1 就读密钥、
    发。认不出的 `page` 代码只影响显示，不挡停止。`schema` 不是 1 的不发、不再试（`no_status`，终端带 `schema`）。
  - 再试（`flash_sale.STOP_RETRY_CODES`）：连不上（当场读 `/status` 连不上也算）、当场读的 `/status` 没读懂、发出了没回话
    （`no_answer`）、闪购助手回 `busy`、`loading`、`no_response`：隔 3 秒（`STOP_RETRY_GAP_SEC`；闪购助手两次指令之间要隔
    2 秒）再试，直到收到别的结果，或者从第一次算起再试一次就要超过 30 秒（`STOP_RETRY_SEC`）。都连不上时是第 0、3……30 秒，
    一共 11 次。每一次都重新读 `/status`、重新读密钥。等着再试的那几秒，`control` 是 `{stop, retrying}`。
  - 不再试：`awaiting_approval`、`captcha`（要人去专用 Chrome 里处理，页面那句说去哪儿点，§9.6）、`not_running`、别的拒绝、
    `no_token`、`failed`。接受了照常等生效（§9.3）。结果只发一条，再试过的带 `tries`。
  - 等着再试时点了开始、继续：还没发的重试立刻作废（终端一行），这一下照常走上一条（没发出去也照样作废：人最后要的是开始）；
    停止不另发结果。这时再点停止：已经在试，什么都不做。
  - 别的指令（开始、继续）一律不自动重发。
- **开始点两下，两下都到后端**（`_flash_take_arm`；直播助手平台轮廓 §4.4、§9.2 第 6 条，用户 10-29 同意）。以前「点两下」
  只做在页面里，后端只收到一条 `start`，分不出是人点了两下，还是一个脚本发了一条。现在页面第一下发 `step=arm` 带一个新编号
  （`flash.js flashNewArmId`，16 位十六进制），后端只记下编号和此刻（单调时钟）、不回话；第二下发 `step=fire` 带同一个编号，
  离 `arm` 0.4 秒（`flash_sale.START_GUARD_SEC`）以上、6 秒（`START_CONFIRM_SEC`）以内才往下走（再看上一条「发之前看状态」）。
  不是这样的一律不转发，结果 `not_confirmed`，终端带是哪一种：`no_step`（只发了一条，旧页面、脚本）、`no_arm`（前面没有 `arm`，
  或已经用掉、被停止清掉了；编号不是 1–64 个字母、数字、`_`、`-` 的 `arm` 不记）、`other_arm`（编号不是最近那次 `arm` 的：
  别的控制页又点了第一下，就换成那一个）、`too_soon`（双击一次）、`too_late`。`arm` 只用一次，对不对得上都用掉；停止也把它清掉。
  编号由页面生成，只用来把两下对上，不是口令（同一用户的进程想发什么就发什么，§9.4）。继续、停止点一下就发。
- **本机脚本只能停**（`app/server.py CONTROL_SOURCE`；轮廓 §5.6、§9.2 第 6 条）：`_ws` 处理每一条控制消息时记下是哪种连接
  （带本机同端口 Origin 的页面是 `page`，没带 Origin 的本机进程是 `local`），处理完复原。开始、继续来自 `local` 的：不读密钥、
  不发，结果 `local_only`（`arm`、`fire` 都一样，也碰不掉控制页刚点的第一下）。停止照发（安全的方向）。不改 `on_control` 的
  签名：直接调 `handle_control` 的测试和老写法拿到缺省的 `page`，按控制页算。
- **在途时**（`phase=sending`）新的指令不发、不回结果：同一页面连点、另一个控制页同时点，都只发第一个。
  页面按 `control.phase` 把按钮禁用。停止在等着再试（`phase=retrying`）时不算在途，见上一条。
- **密钥**：`~/Library/Application Support/FlashSaleHelper/control-token`，每次发之前读一遍（`flash_sale.read_control_token`，
  契约：删掉文件后闪购助手下次启动会换一个新的）。`O_NOFOLLOW` 打开（文件本身是符号链接就不读）、`O_NONBLOCK`
  （管道、设备文件打开时不卡住事件循环），`fstat` 确认是普通文件，最多读 1025 字节（读满就是太长），按 ASCII 解码、
  去掉首尾空白和换行后必须正好 64 位十六进制。读不到就不发，`error=no_token`，终端带原因代码（missing、symlink、
  not_file、unreadable、too_long、format、no_home），不带文件内容。不查 600 权限位：读的一方查它不能让密钥更安全。
- **请求**（`flash_sale.post_control`）：地址写死 `http://127.0.0.1:9334/<action>`（`CONTROL_BASE`），Host 是
  `127.0.0.1:9334`（aiohttp 默认），`Authorization: Bearer <密钥>`，`Content-Type: application/json`，请求体
  `{"source":"tlt"}`；不带 `Origin`、`Sec-Fetch-*`、`Cookie`（`DummyCookieJar`）；不跟随重定向（307、308 跟过去会带着
  密钥再 POST 一次）；`trust_env=False`（不读环境变量里的代理：设了 `HTTP_PROXY` 的机器上，带密钥的请求不能交给代理）；
  连接 2 秒、读 2 秒，总时长最多 4 秒；每次一个短命 session。
- **回话**：200 且 `{"ok":true}`（带了 `accepted` 就得是这个动作）→ 接受；409 → 闪购助手给的代码；404 / 405 →
  `no_control`；其余见 §9.1 的表。aiohttp 的异常在 `post_control` 里全部换成代码，一个都不往外抛（§9.4）。
- **发布**：发出去的同时把 `phase=sending` 发布出去（别的控制页也禁用按钮），不等广播再发：广播最坏被卡住的页面拖 2 秒，
  「停止」不该跟着晚。轮询和控制两条路的发布在一把锁里排队（`_flash_refresh`），锁里才合成状态，所以页面不会停在旧的进度上。
- **没接受、没发出、出错**：`control` 换回发之前的那一份。例如开始在等生效时点了停止、被闪购助手拒了（busy），开始照旧等着。
- **退出时不取消在途的请求**：`stop_flash_watch` 只停轮询。POST 最多 4 秒，让它发完——尤其是「停止」，半路取消了不知道
  闪购助手收到没有。停止的重试也不取消。进程等不等得到它看收尾的时限；结果广播不到也没关系（页面已经关了）。

### 9.3 接受以后：等它生效

- `control` 换成 `{action, phase: "waiting"}`，30 秒（`flash_sale.CONTROL_WAIT_SEC`）里每 3 秒查一次 `/status`，
  并马上查一次（§5）。契约：接口马上返回，不等页面做完；开始后真实页面一般 5–15 秒 `running` 才变 true，
  超过 30 秒还不是就当这次没成功。
- 每一跳查完、发布之前看指令生效没有（`flash_sale.control_done`）：start、resume 是 `running=true` 且 `paused_reason`
  不是 user_interaction；stop 是 `running=false`；只认 `conn=ok` 的状态（连不上、读不出时看不到，不算）。前端
  `flashControlDone` 一字不差（`shared-rules.json` 的 `control_done`），生效了页面就收掉浮层里那句结果。
- 第一次看到生效：终端一行「已生效」，不另发结果（状态自己会变）。停止这时就收掉 `control`；开始、继续要等页面也稳下来
  （`flash_sale.control_settled`：连上了、不陈旧、page 不是 loading、error、no_list，§7「开始以后的过渡」）才收，在那之前
  照旧是 `waiting`。状态已经显示生效，所以按钮那边照「`running` 为 true」那一行画（停止能点，§9.6），主句照旧是过渡那一句。
- 等满 30 秒：`control` 收掉。一直没看到生效的另发一条 `{ok: false, error: "not_seen"}`，终端一行，只写看到的，不猜原因；
  看到过生效、只是页面一直没稳下来的不再报，之后照原规则变色。
- 指令还在途时正好跑了一跳轮询：不拿这一跳判生效（在途的开始碰上 `running=true` 也不提前收掉），回来以后照常进入等生效。
- 开始、继续在等生效时的变色规则（页面刷新那几秒不变色）见 §7「开始以后的过渡」。

### 9.4 谁能发、安全边界

- 能发 `flash_control` 的：本机控制页（Origin 是本机同端口的页面），以及**没带 Origin 的本机进程**（curl、本地脚本：
  `app/server.py` 的 `_classify_origin` 把它们也当控制面）——后者只能发停止，开始、继续回 `local_only`（§9.2「本机脚本只能停」）。
  手机同看发不进来（`app/viewer.py` 的 `_handle_inbound` 只认 retranslate）；浮窗页的入站一律丢弃；别的网页过不了 Origin 检查。
- 「本机脚本只能停」和「开始两下都到后端」**防的是误触**（自己的截图脚本、测试工具、别的小程序只发一条消息），**不是安全
  边界**：非浏览器进程想带什么 Origin 就带什么，想发两条就发两条；同一用户的进程本来就读得到密钥。自动化浏览器打开真实的
  8765 会带本机 Origin，技术上能点两下开始，只能靠规矩（项目 CLAUDE.md：自动化浏览器只开演示模式、单独端口）。
- 所以闪购控制的**安全边界是「连得上本机 127.0.0.1 的控制端口」，不是密钥文件的 600 权限**。同一用户的进程本来就能
  直接读那个文件、自己去 POST 9334，借这条路不多得什么；多出来的只是连得上回环、却读不到这个用户 `~/Library` 的进程
  （别的 macOS 账户、带网络权限的沙盒应用）。单用户的中控电脑上可以接受：没带 Origin 的连接本来就能发 `disk_delete`、
  `apply_update`，比开始、停止危险。没做「没带 Origin 的连接丢掉 `flash_control`」：停止要让本机脚本也发得出去（安全的方向），
  而且非浏览器进程想带什么 Origin 就带什么，这道检查挡不住真想绕过的进程。
- 契约本身的限制：有进程占了 9334、又照着契约回 `/status`，就拿得到密钥（本机回环上的明文 HTTP，客户端没法认证
  服务端）。这里能做的是 §9.2 的「发之前看状态」：连上并读懂了、`schema` 是 1、`page` 是契约里的代码才发。这挡得住
  碰巧占着 9334、回了个 JSON 对象的别的服务（只要是对象就算「读懂了」），挡不住存心冒充的进程——它照着契约回就是了。
  碰上那种服务，人点按钮本来也发不出去（页面只在 page 是 ok / no_list 时让开始能点，停止、继续只在 `running`、
  `paused_reason` 这么说时才出现）；这一条挡的是上面说的那些连得上本机控制端口、直接发 `flash_control` 的进程。
- 密钥只在 `_flash_control_run` 的一个局部变量里待到交给 `post_control`：不打印、不广播、不进 config、不写审计。
  aiohttp 的 `ClientResponseError` 的 repr 里带着请求头（也就带着 `Authorization`），所以 `post_control` 把 aiohttp 的
  异常全部换成代码；后端自己出错时终端只打异常的类型名。

### 9.5 终端

每个指令一行中文，等生效的结果再一行。不打印密钥、商品标题：

```
[信息] 闪购助手：中控请求开始 → 已接受
[警告] 闪购助手：中控请求停止 → 没有接受（not_running）        409 的代码、no_control、http_<状态码>
[警告] 闪购助手：中控请求开始 → 没有发出（no_status）
[警告] 闪购助手：中控请求停止 → 没有发出（no_status：schema）   另有 page：回话不像闪购助手的（§9.2）
[警告] 闪购助手：中控请求开始 → 没有发出（no_token：missing）
[警告] 闪购助手：中控请求停止 → 连不上（unreachable）           另有「没有回话（no_answer）」「回话读不出（bad_reply）」
[警告] 闪购助手：中控请求停止 → 出错（failed：RuntimeError）
[信息] 闪购助手：中控请求开始 → 已生效
[警告] 闪购助手：中控请求开始 → 30 秒内没看到生效
[警告] 闪购助手：中控请求停止 → 没有接受（busy），30 秒内每 3 秒再试      停止第一次没被接受、接下来再试（§9.2）
[信息] 闪购助手：中控请求停止 → 已接受（第 3 次）
[警告] 闪购助手：中控请求停止 → 连不上（unreachable），一共试了 11 次
[信息] 闪购助手：中控请求开始，停止的重试作废
[警告] 闪购助手：中控请求开始 → 没有发出（not_confirmed：too_late）  另有 no_step、no_arm、other_arm、too_soon（§9.2）
[警告] 闪购助手：中控请求继续 → 没有发出（local_only）
[信息] 闪购助手：中控请求开始 → 没有发出（开始、继续还没开放）       发布闸关着（§9 开头）
```

停止再试的中间几次不另打行：第一次没被接受打一行，最后的结果打一行。

闪购助手那边也会把每个控制指令（包括被拒绝的）记一条 info 级的 alert（例如「🖥 中控请求开始」），浮层的「最近记录」里
看得到。

### 9.6 页面：浮层顶部那一排

`web/index.html` 的 `#flash-ctl`（`tabindex="-1"`）紧跟在浮层标题行下面：`#flash-start`「开始」、`#flash-stop`「停止」、
`#flash-resume`「继续」、`#flash-open`「打开闪购助手」（从最底下挪上来的，§8）。下面依次是 `#flash-ctl-note`（按钮为什么
不在、为什么不能点；三颗按钮的 `aria-describedby` 指着它）、`#flash-note`（指令的结果、断线、open 没打开，`aria-live`），
再往下才是主句、商品表、最近记录。英文和用词见 `docs/i18n-style.md` §1.3、§3 K #167–#175。

**显示什么、能不能点**（`flash.js flashControlView`，纯函数，decisions-step2 的规则）：

| 状态 | 那一排 | 旁边那句（`#flash-ctl-note`） |
|---|---|---|
| 连不上、读不出、正在查看 | 只有「打开」 | 不写 |
| `app_version` 认得出且低于 0.8.0 | 只有「打开」 | 闪购助手 0.8.0 起才能从这里开始、停止。 |
| `running` 不是 true | 开始、打开；page 不是 ok / no_list 时开始不能点 | page 不是 ok / no_list 时写 page 那句（`flashNotReadyNote`，见表下） |
| `running` 为 true | 停止、打开 | 不写 |
| 再加 `paused_reason=user_interaction` | 停止、继续、打开；page 不是 ok / no_list 时继续不能点 | 同上 |
| `paused_reason=awaiting_approval` | 停止（不能点）、打开 | 确认框处理完以后才能从这里停止。（契约：这时回 409 awaiting_approval） |
| 在途（本页刚点的，或 `control.phase=sending`） | 三颗都不能点，按下的那颗写「正在发送…」 | 不写 |
| 等生效（`control.phase=waiting`，状态还没显示生效） | 按下的那颗写「正在开始…」「正在停止…」「正在继续…」、不能点；开始、继续不能点（闪购助手这时回 busy），停止照样能点（停是安全的方向） | 不写（免得和「正在开始…」打架） |
| 停止等着再试（`control.phase=retrying`） | 停止写「正在重试…」、不能点（后端已经在试）；开始、继续照常（点了，后端把还没发的重试作废） | 停止还没被接受，30 秒内每 3 秒再试一次。（连不上、读不出时那一排一颗按钮都不出，这句照样写） |
| 和本地服务断开 | 三颗都不能点（「打开」本来就禁用） | 不写 |
| 发布闸关着（hello 的 `flash_start_enabled` 不是 true，出厂就是这样） | 开始、继续不出；停止照上面的规则 | 版本那句换成「闪购助手 0.8.0 起才能从这里停止。」；其余照上面 |

- **页面没准备好时旁边那句**（`flashNotReadyNote`）：照 decisions-step2（用户 10-27 定的「旁边一句说明为什么，照 /status
  的 page 文案」）写 `flashPageText` 的那句，例如「专用 Chrome 停在 TikTok 登录页，登录后才能继续。」——和下面主句里那句是
  同一句，主句管变不变色、排第几，这里管按钮为什么不能点（读屏念按钮时经 `aria-describedby` 一起念到）；认不出的代码写
  「页面状态：」加原样的代码；状态里连 page 都没有时写「页面准备好以后才能从这里开始、继续。」。2026-10-28 审查以前这里
  一律写那句通用的，理由是不和主句重复，但没经用户确认，所以照 decisions-step2 改回来；要换回通用的那句，改
  `flashNotReadyNote` 一处，并请用户拍板后记在这里。
- **确认框里点了取消的暂停**：那一排出「继续」时，主句只写「已暂停：确认框里点了取消。」，不再叫人「要在闪购助手的面板上点
  继续」——按钮就在上面；点了以后等生效那几秒，主句下面跟一句「正在继续…」。版本认得出且低于 0.8.0、只剩「打开」时照旧写
  长的那句（`flashPausedText` 的 `resumeHere`，2026-10-28 审查）。
- **版本**：只认两到四段数字开头、后面不再接数字或点的（`0.8`、`0.8.0-rc1`、`0.9.2.1` 认得；`v0.8.0`、`0.8.x`、
  五段的认不出）。四段都比，没写的段当 0（`flash.js flashVersionOlder`）：`0.9.2.1` 比 `0.9.2` 新，`0.8.0.0` 就是 0.8.0，
  `0.7.9.9` 仍比 0.8.0 旧。原先只认两三段，四段的一律当认不出；闪购助手的说明里出现了 0.9.2.1，四段是它真会发的，
  所以改成认得：0.8.0 及以上的四段版本原先也照常出按钮（认不出），现在是比出来的，结果一样；0.8.0 以下的四段版本原先
  照常出按钮、点了才说没有控制接口，现在和两三段的一样只留「打开」。认不出的照常出按钮，
  回来 404 / 405 就是 `no_control`「这个版本的闪购助手没有控制接口。」。不记住这个结果去藏按钮：再点一次就再说一次。
- **最底下那句**（`#flash-desc`，`renderFlashControls`）：三颗里有一颗露着就写「这里只能开始、停止和继续，其他操作请到
  闪购助手里。」（发布闸关着时写「这里只能停止，其他操作请到闪购助手里。」），一颗都没有时换回第一步那句「这里只显示状态。
  要操作，请到闪购助手里。」，不和版本那句打架。
- **发布闸关着时的主句**：没有「继续」按钮，确认框里点了取消的暂停照旧写长的那句「……要在闪购助手的面板上点继续。」
  （`flashLines` 的第三个参数、`renderFlashPanel` 的 `gated`）。
- **「开始」点两下**（`flash.js flashStartClick` + `app.js`）：第一下把字换成「再点一次开始」，6 秒（`FLASH_START_CONFIRM_MS`）
  内再点才发，到点复原；武装后 400 毫秒（`FLASH_START_GUARD_MS`）内的第二下不算——鼠标双击一次就发出开始的话，专用 Chrome
  里的页面会被刷新。定时器被节流没跑也按时刻算（满 6 秒的那一下当作重新点第一下）。关上浮层、重连的 hello、「开始」不在了
  或不能点了，武装都作废。两下都发给后端（§9.2「开始点两下」）：第一下发 `step=arm` 和新编号（把上一句结果收掉），第二下发
  `step=fire` 和同一个编号，后端照同样的 400 毫秒、6 秒再判一次；后端判不过（比如第一下在路上被事件循环耽误了）回
  `not_confirmed`，那句话叫人再点两下。第一下没发出去（断线）就不武装，写断线那一句：不然第二下的 `fire` 后端没见过 `arm`。
  只改这一颗：清空字幕、清空弹幕那套（`confirmTwice`，「再点确认 / Clear?」）不动。停止、继续点一下就发。
- **发什么**：停止、继续只发 `{"type": "flash_control", "action": …}`；开始见上一条。没发出去（断线）就不装作发出去了：
  `#flash-note` 写「与本地服务断开，正在重连——稍候再试。」，不进在途。
- **在途**：点出去马上把这一排禁用（本页记一个 `flashPending`），不等后端把 `phase=sending` 发回来。放开的时机：状态里
  `control.phase` 成了 sending 或 retrying（之后由状态说了算）、收到任何一条 `flash_control_result`（广播给所有控制页，别的页点的也算）、
  重连的 hello。后端在途时收到的第二个指令直接丢、不回话（§9.2），页面不能一直等它；后端还没处理到这个指令时推来的那份
  状态（`control` 还是 null）不放开。
- **结果**（`flash.js flashResultLine`，写进 `#flash-note`）：接受了（`ok: true`）不写，把上一句收掉——状态自己会变，按钮上
  有「正在开始…」。没接受：闪购助手 409 的代码写「闪购助手没有接受：」加契约里这个代码的意思（no_page、login_required、
  captcha 用页面那几句，awaiting_approval 用确认框那句，其余各一句；认不出的原样放进 `translate="no"`）；`http_<状态码>`
  写「没有接受：127.0.0.1:9334 回了 HTTP {n}。」（9334 上回话的不一定是闪购助手）；本程序这边的代码照后端走到了哪一步写
  「没有发出：…」「发出了，但…看看状态有没有变。」；`not_seen` 按动作写「30 秒内没看到闪购助手开始运行 / 停下 / 继续运行。」。
  停止被拒在确认框、验证码上（后端不再试）写清去哪儿点：「闪购助手没有接受停止：专用 Chrome 里弹着确认框。到那里点确定或
  取消，再点停止。」「……专用 Chrome 的页面上有验证码。到那里处理完验证码，再点停止。」（`flashStopBlockedText`；开始、继续
  照旧是上面那几句）。再试过的停止（结果带 `tries`）句子前面加「试了 n 次，」。只写看到的，不猜为什么。
- **停止在等着再试**：状态里的 `control.phase=retrying` 也放开本页记着的在途（同 sending：之后由状态说了算）。
- **过时的结果自己收掉**（审查第 3 条）：`#flash-note` 记着那句是哪个动作的结果；之后某一份状态让 `flashControlDone`（后端
  `control_done` 的抄本，`shared-rules.json` 的 `control_done`）说这个动作已经生效，就收掉——浮层开着、关着都一样。最典型的
  是浮层关着时到的 `not_seen`，之后闪购助手开始运行了，下次打开浮层不会再看到那句旧话。断线、open 没打开那几句不是哪个动作
  的结果，不跟着状态收（关上浮层照旧都收）。
- **焦点**（审查第 5 条）：按下的那颗变成不能点（正在发送…）或藏起来（开始运行了，开始换成停止）时，焦点挪到 `#flash-ctl`
  本身（`focus({preventScroll: true})`，`:focus-visible` 才画焦点环），键盘和读屏的人不会掉出浮层、掉回页面开头。只看重画前
  焦点在不在那三颗上：鼠标点的（焦点在别处）不去动。
- **开始后的过渡**：等生效时顶栏按钮不变色，主句「正在开始…（闪购助手在刷新页面）」，胶囊和顶栏按钮的 title 写「正在开始…」
  （不写「未开始」），见 §7。
- **版面**：这一排 `flex-wrap: wrap`，放不下折行，不横向撑出浮层（浮层 440px）。G10 的 `flash-ctl-*` 场景在默认 1000px 窗口
  量：浮层和这一排不溢出、露出来的按钮在同一行、字不被截，顶栏的下限照旧（§12）。

## 10. 顶栏宽度：「Aa」

多一颗按钮要 36px，原来的顶栏余量放不下。用户定的方案甲：有闪购按钮（`body.has-flash-btn`）、窗口窄于 1400px 时，
字号滑块收成一颗 28px 的「Aa」（`#font-btn`），点开的小浮层就是原来的字号控件（`label#font-ctl` 原地改成浮层样式）。
宽窗口、没装闪购助手时一切照旧。按钮上的字：中文 1000–1399px 只露「闪购」两个字，1400px 起图标加字；英文 1600px 起才露
Flash Sale。实测余量和 G10 场景在 `docs/i18n-style.md` §4.2。

## 11. 隔离

- **手机同看**：`flash_sale`、`flash_control_result` 不在 `app/viewer.py` 的 ALLOW 里，另外显式写进 DENY；
  `replay_snapshot` 只挑固定几个键。手机发不进 `flash_control`、`open_flash_helper`、`flash_find`（入站只认 retranslate）。
- **浮窗**：两种消息都不在 `app/server.py` 的 `FLOAT_TYPES` / `FLOAT_CONFIG_KEYS` 里；浮窗页的入站一律丢弃；
  `float.html` 不加载 flash.js。
- **报警（CLAUDE.md 第六条）**：闪购的提醒只走这颗按钮的颜色和这个浮层，不借 incident、status、顶部提示条、
  「疑似违禁词」面板，也不挪报警的位置。不发系统通知。控制的结果也只进这个浮层。
- **审计、session 日志**：一条都不写。终端只在状态跳变时打一行中文（已连上、连不上、读不出、schema 不是 1、
  轮询出错、执行 open），控制指令每个一行、等生效的结果一行（§9.5），不打印商品标题、记录正文和控制密钥。

## 12. 测试

**绝不连真的 127.0.0.1:9334**（开发机上闪购助手可能正开着，返回的是真实商品；一个真的 `POST /stop` 会停掉直播里
正在续开的闪购），**绝不读真的控制密钥**，也不真的执行 `open`。

- `tests/conftest.py` 有两层 autouse 夹具，换的是同几样：`flash_sale.app_candidates` 指到不存在的路径、`flash_sale.STATUS_URL`
  指到 `http://127.0.0.1:9/status`、`flash_sale.CONTROL_BASE` 指到 `http://127.0.0.1:9`（没人听的端口），
  `flash_sale.control_token_path` 换成返回一个不存在的路径。会话级那一层比任何模块级夹具都先搭好，同是会话级的也排在
  点名要的那些前面（在那里造的 Pipeline 构造时就取控制地址）；函数级那一层让自己换过这几样的用例结束后退回来。不放进 `pytest_configure`：那样导入测试
  模块之前就换了，`test_flash_sale_control.py` 留的真函数 `REAL_CONTROL_TOKEN_PATH` 就拿不到。任何测试都看不到本机真装的
  闪购助手、连不到真的 9334、读不到真的密钥。`fetch_status`、`post_control`、`read_control_token` 的默认值都在调用时才取，
  这张网才兜得住。`test_flash_sale_control.py` 末尾两条钉着它（一条从模块级夹具里看会话级那一层），
  `test_pipeline_flash.py::test_a_pipeline_built_in_tests_points_at_no_real_port_and_no_real_token` 钉 Pipeline 这一层。
  钉的时候也不真的去读、去连：`open` 换成只记路径、`tmp_path` 以外一概不打开的替身，POST、GET 换成只记地址、马上抛异常的
  替身——缺省值要是被改成导入时就取，CI 上没有真的密钥文件和 9334，照样是绿的；开发机上则会真的读到密钥，断言失败时还会
  把它印在终端上。
- 要测「发出去了」：`tests/test_flash_sale_control.py` 在本机临时端口起一个假闪购助手，照契约「控制」一节逐条检查
  （Host、不带 Origin / Sec-Fetch-*、Bearer 密钥、Content-Type、请求体），任一条不满足回 403、什么都不做——所以
  200 回来了就说明我们发的请求每一条都满足；另有一组对照证明它真的会拦。密钥是 `tmp_path` 里的假文件（`7e57` 重复
  16 次），它的任何一截都不许出现在终端、广播、config 里。409 各代码、404 / 405、超时、重定向不跟随、
  不读环境变量里的代理（本机临时端口上只记账的假代理）都在这里。
- `tests/test_pipeline_flash.py` 的控制部分换掉 `_flash_post`、`_flash_read_token`（假 POST、假密钥）和停止两次尝试之间的
  `_flash_retry_sleep`（拨钟，不真的睡），钉住 no_status、停止当场读 `/status`、停止再试（哪些代码再试、3 秒一次、30 秒为止、
  重试中点开始作废、开始和继续不重发）、发布闸、开始的两下（0.4 秒、6 秒两条边，对不上的每一种）、本机脚本只能停（另有一条
  走真的 `_ws`：没带 Origin 的连接和带本机 Origin 的页面）、在途、等生效、30 秒的两条边、过渡不变色、退出不取消、
  终端两种界面语言下逐字节相同。用例里开始一律用 `tap` / `press` 点两下（arm、单调时钟拨 1 秒、fire）；最后一条整条链走真的
  `read_control_token` + `post_control` + 上面那个假闪购助手。`RecordingServer` 没有连着的页面，广播从不让出事件循环，
  所以另有两条用 `HeldServer`（卡住一条 `flash_sale` 广播，像慢页面那样）钉住 §9.2 的两件事：在途那一份的广播还卡着时
  POST 已经发出去了；轮询和控制同时发布时，页面最后收到的、config 里的都是最新的控制进度。
- 后端和前端各有一份、必须一字不差的规则（卡住的条件、排序、`control_done`、开始后的过渡），两边吃同一张表
  `tests/fixtures/flash_sale/shared-rules.json`：`test_flash_sale.py` 测后端，node 测试测 `web/flash.js`。改规则先改表。
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
  `popover.test.mjs`（顶栏浮层登记表）。顶部那一排：`tests/flash-control.test.mjs`、`flash-control.en.test.mjs`
  （`flashControlView` 的每条规则、两次点击的 400 毫秒和 6 秒两条边、结果的每一句、不续开原因和序号；`shared-rules.json`
  的四节逐条跑前端那一半；过渡 30 秒的边从页面这一边看：等生效时不变色、`control` 收掉以后照原规则）、
  `flash-control-button.test.mjs`（真跑 `app.js`：在途的放开时机、过时结果收掉、焦点、断线、最底下那句、HTML 结构）。
  `test_flash_sale.py::test_the_panel_ranks_orders_and_quotes_numbers_like_the_backend` 另钉着页面写死的「30 秒」、
  停止再试的 `FLASH_STOP_RETRY_SEC`、`FLASH_STOP_RETRY_GAP_SEC` 和 `FLASH_ACTIONS`、`FLASH_OWN_ERRORS`、`FLASH_QUIET_PAGES`
  三张表与后端一样。
- G10（`TLT_DOM_SCAN=1 pytest tests/test_i18n_dom.py`）：`live-flash*`、`live-flash-zh*`、`home-flash*` 场景，
  `config.flash_sale` 由 `flash_sale.StatusTracker` 从 running.json 生成。顶部那一排另有 `flash-ctl-<种类>`（英文，进扫描）
  和 `flash-ctl-<种类>-zh`：idle、armed（点了一下开始）、running、paused、awaiting、old（0.7.3）、starting（开始被接受、
  页面出错且陈旧）、skip（五种原因、一个认不出的代码、序号）、rejected（409 busy）、gated（在运行、发布闸关着：只有停止），
  除了 gated，hello 里都带开闸的 `flash_start_enabled: true`；默认 1000px 窗口，查哪几颗露出、能不能点、
  文案，量浮层和这一排不溢出、在同一行，顶栏下限不放宽。截图（`TLT_DOM_SHOTS`）带 `--force-prefers-reduced-motion`
  （`SHOT_FLAGS`）：浮层弹出动画半路截下来是半透明的（审查第 8 条）。直播转写进行中不开 Chrome。

## 13. 不做的事

- 除了开始、停止、继续（§9），不控制闪购助手：浮层里没有批准、续开、新建闪购、改价、加商品这类按钮；
  第一次的确认框照旧在专用 Chrome 里点。
- 不显示在手机同看页和浮窗里。
- 不翻译商品标题和记录正文；不显示 `shop`、`page_version`。
- 不用 mdfind 找程序，不按 bundle id 打开。
- 仓库里（代码、测试、注释、文档）不出现真实的品牌、店铺、主播名，测试只用契约里的虚构样例。
