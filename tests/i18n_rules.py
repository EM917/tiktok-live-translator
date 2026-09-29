"""英文界面几道机械检查用的规则表（G2–G5 与无损证明），只放数据，不放逻辑。

检查逻辑在 tools/i18n_pairs.py（抽对、G2 成对、G3 同中文→同英文、G4 覆盖、G5 静态页）
和 tools/i18n_strip_check.py（剥回中文与基点比）。文案本身怎么写见 docs/i18n-style.md。

改这里任何一张表都要写理由：例外清单每一项都是「中文臂 → 为什么可以」，
不许为了让检查变绿而放宽正则。"""
import re

# ---- 抽对范围 ----------------------------------------------------------------------------
# 生产代码里可能上界面的文件。tests/ 不在内：测试里故意写坏的对是夹具；
# tools/ 只在终端里跑（CLI 不英文化，见 docs/i18n-style.md §0）。
PAIR_SOURCES = ("main.py", "app/*.py", "web/*.js", "web/*.html")

# ---- R8：不翻的中文 -----------------------------------------------------------------------
# 以这些标签开头的串是终端输出，自动豁免
TERMINAL_PREFIXES = ("[信息]", "[警告]", "[错误]", "[解析]", "[自检]", "[提示]", "[初始化]",
                     "[时钟]", "[网络]", "[磁盘]", "[同看]", "[弹幕]", "[健康]", "[警报]", "[严重]")
# 行尾注记 `# i18n: <tag>`（JS 写 `// i18n: <tag>`）：这一行的中文不上界面
NOTE_TAGS = ("terminal", "audit", "data", "os")
# 文件头注记：整个文件没有界面文字（comment_worker.py、qr.py 一类）
FILE_TAG = "file=terminal"
# R9 迁移完成的标记；G4/G5 只对带它的文件生效
DONE_TAG = "done"

# ---- R7：数据区 ----------------------------------------------------------------------------
# 页面上的数据有两种标法（用户决定 6，2026-09-29，改了 spec §6）。G4/G5 和 G10 的 scan.js
# 对两种都豁免子树里的文字与子孙的属性，元素自己的 title/aria-label/placeholder/alt 照查：
# - 名字（主播名、观众名、品牌名、违禁词条、模型名、链接和地址）标 translate="no"，浏览器的
#   「翻译此页」也不去改它们；
# - 正文（字幕、弹幕、报警原话及其译文）要留给浏览器翻译，不标 translate，只带这个 class。
# 改名要连 web/app.js、web/viewer.js、web/index.html、tests/i18n_dom/scan.js 一起改，
# tests/test_i18n_static_html.py 钉着它们一致。
DATA_TEXT_CLASS = "i18n-data"

# ---- R6：句子里引用的按钮与界面元素名 ----------------------------------------------------
# 中文臂里写了「X」、X 在表里，英文臂就必须以整词出现 UI_NAMES[X]（英文里不加引号）。
# 只收英文名已经在 docs/i18n-style.md 里定死的；有歧义的（「关闭」：Off / Close）不收。
UI_NAMES = {
    "开始翻译": "Start",
    "开始": "Start",
    "停止": "Stop",
    "换主播": "Switch Streamer",
    "确认换主播": "Switch Streamer",
    "一键更新": "Update Now",
    "更新说明": "Release Notes",
    "重译": "Retranslate",
    "不限": "Any",
    "不限（默认）": "Any (default)",
    "设置": "Settings",
    "翻译引擎": "Translation Engine",
    "启动自检": "Startup Check",
    "音频组件 ffmpeg": "Audio (ffmpeg)",
    "人声降噪": "Noise Reduction",
    "语音识别": "Speech Recognition",
    "违禁词表": "Banned-Term List",
    "领域词表": "Glossary",
    "审计日志": "Audit Log",
    "直播流解析": "Stream Lookup",
    "浏览器登录态": "Browser Login",
    "观众弹幕": "Comments",
    "磁盘空间": "Storage",
    "违禁词报警": "Banned-Term Alerts",
    "手机同看": "Phone Viewing",
    "开始同看": "Start Sharing",
    "停止同看": "Stop Sharing",
    "复制链接": "Copy Link",
    "换一个链接": "New Link",
    "清空字幕": "Clear",
    "清除记录": "Clear History",
    "清空警报": "Clear Alerts",
    "清除已看过的报警": "Clear Seen",
    "回到最新": "Jump to Latest",
    "删除所选": "Delete Selected",
    "保存": "Save",
    "最近的直播间": "Recent Streams",
    "最近直播间": "Recent Streams",
    "本场品牌": "Brand",
    "打开词表文件夹": "Open Brands Folder",
    "打开 brands 文件夹": "Open Brands Folder",
    # macOS 自己的界面：照抄苹果的英文原名
    "系统设置": "System Settings",
    "隐私与安全性": "Privacy & Security",
    "完全磁盘访问权限": "Full Disk Access",
    "允许": "Allow",
    "始终允许": "Always Allow",
}

# 产品名映射：中文臂含前者，英文臂必须含后者
PRODUCT_NAME = ("TikTok 直播同传", "TikTok Live Translator")

# ---- 禁用词两级（CLAUDE.md 第八条；docs/i18n-style.md §2.6） ------------------------------
# 第一级「标签词」：全仓英文臂都查。放过产品术语 banned term / Banned-Term List。
EN_LABELS = re.compile(
    r"(?i)\b(age|aged|age[- ]restrict\w*|rate[- ]?limit\w*|throttl\w*"
    r"|ban(s|ned)?\b(?![- ]terms?)|block(ed|ing|s)?|restrict\w*)\b")
# 第二级「因果词」：只查第八条家族（拿不到流地址、登录、弹幕、手机同看这几路的文案）
EN_CAUSAL = re.compile(
    r"(?i)\b(because|due to|caused by|the reason|probably|likely|must be|seems to)\b")

# 第八条家族：值为 "*" 表示整个文件；元组里是函数名（Python）或 "#元素 id"（HTML，含子孙）
RULE8_FAMILY = {
    "app/resolver.py": "*",
    "app/browser_login.py": "*",
    "app/comment_source.py": "*",
    "app/viewer.py": "*",
    "app/viewer_share.py": "*",
    "web/viewer.js": "*",
    "web/viewer.html": "*",
    "web/index.html": ("#input-help",),
    "app/pipeline.py": ("browser_only_message", "_resolve_media", "_confirm_offline",
                        "_host_wait", "_selfcheck_incident_text"),
}

# 禁用词例外：中文臂 → 理由。命中这里的对不查两级禁用词。
# （电脑休眠写 suspended 不需要登记：suspend 不在任何一级里。）
EN_WORD_EXCEPTIONS = {}

# ---- G3：同一句中文只许有一种英文 -------------------------------------------------------
# 中文臂 → 理由。同一句中文在不同位置确实该译成不同英文时才登记。
SAME_ZH_DIFFERENT_EN = {
    "关闭": "状态读作 Off（设置行摘要、同看开关），窗口按钮读作 Close（关窗确认框）",
    "；": "把几条完整的句子连起来时读作 '. '（自检行的 notes、弹幕被拒后的更新检查结果），"
          "照 docs/i18n-style.md §2.2 拆句；只有在冒号或括号后面逐条列「名字：原因」时读作 '; '"
          "（磁盘「未删除」清单、浏览器登录的观察、没过安全校验的解析层），"
          "改成句号会让后面几条脱离冒号，读不出还是同一张清单",
}

# ---- R2：英文可以少引用中文里的变量（只限 JS，逐条登记） ------------------------------------
# JS 的对按两臂引用的变量比（tools/i18n_pairs.py 的 R2）。英文有意省掉中文里的某个变量时登记在这里：
# 中文臂的字面文字（变量处为空，即 Pair.zh）→ 理由。登记了的对，英文臂的变量只要是中文臂的子集就算过；
# 英文多出中文里没有的变量照样报。
R2_EN_OMITS = {
    "再点一次：改听 @（停止监听 @）": "换主播的武装态：@A 已经写在上面的副标题里（Now monitoring @A. "
                                  "Nothing changes until you confirm.），英文只说改听谁，按钮才放得下"
                                  "（docs/i18n-style.md #75、§4.3 R3）",
}

# ---- R12：中文恒等函数 ------------------------------------------------------------------
# 只在英文一路改东西、中文一路原样返回的函数。strip-check 把 f(x, …) 剥成 x。
# 值是「模块:属性」，tests/test_i18n_strip_check.py 逐个证明中文模式下 str(f(x)) == x。
ZH_IDENTITY_CALLS = {
    "render": "app.i18n:render",
    "i18n.render": "app.i18n:render",
    "i18n.text": "app.i18n:text",
    "_ui_layer": "app.resolver:_ui_layer",
    "_ui_reason": "app.resolver:_ui_reason",
    "_ui_check_error": "app.updater:_ui_check_error",
    "_space_before": "app.browser_login:_space_before",
}

# ---- strip-check 认的几种等价写法（tools/i18n_strip_check.py） ----------------------------
# 剥回中文之后仍有差别时才用；用到哪条，结果表的「剥掉」一列就列出哪条，证明里看得见。
#
# 1. 新版在它自己的模块里定义了 ZH_IDENTITY_CALLS 登记的函数（如 resolver._ui_layer）：
#    调用处已经剥成 x，所以定义本身、以及只有这些函数读的模块级常量（如 resolver.LAYER_LABEL），
#    在基点里没有时从新版里去掉再比。基点里已经有了就照常比，改动照样拦得住。
# 2. "…".format(…, str(x), …) 当作 "…".format(…, x, …)：对应的占位符没有格式规格、没有
#    !r/!s/!a 时，str.format 调的是 format(x, "")，对异常和内置类型就是 str(x)。
#    of(exc) 剥出来正是 str(exc)，所以 L(…).format(of(exc)) 能与基点的 "…".format(exc) 对上。
# 3. 这些函数第一步就是 str() 它们的第一个参数：f(str(x), …) 当作 f(x, …)。
#    函数名 → 理由。x 为 None 时两者不同，理由里要写清用处为什么不会是 None。
STR_FIRST_CALLS = {
    "strip_query": "app/redact.py 的 strip_query 先 str(text) 再清洗；只有 text 为 None 时不同"
                   "（返回空串，而 str(None) 是 \"None\"）。用处 audit.clean_error 的 exc 是"
                   " except 子句里接到的异常，不会是 None",
}
# 4. spec §2.3 的 node 守卫：web/*.js 顶上这一行只给 node 测试取 L/LN，浏览器里 L 早已定义，
#    整行不执行。新版恰好多出一行、与这里逐字节相同，而基点没有时，从新版里去掉再比。
JS_NODE_GUARD = ('if (typeof L === "undefined") { var I18N_ = require("./i18n.js"); '
                 'var L = I18N_.L, LN = I18N_.LN, APP_NAME = I18N_.APP_NAME; }')
# 5. 已登记的中文可见修正（spec §0.3：只有 §13 标成「中文可见修正」的提交可以改中文）。
#    路径 → {模块级字典常量: {新增的键: 理由}}。只在基点的这个字典里还没有这个键时，
#    从新版里去掉再比；基点已经有了就照常比。
STRIP_KNOWN_ADDITIONS = {
    "app/window_close.py": {"CLOSE_LOCALIZATION": {
        "global.ok": "M11（spec §8.2）：cocoa 下 JS confirm() 的确认键取 global.ok，以前缺这一项，"
                     "中文界面上是 pywebview 自带的「OK」配我们的「取消」；补上「好」是 §13 登记的"
                     "中文可见修正",
    }},
}

# ---- R11 ④：切片直接当这些调用的实参（或 status/broadcast 的字典值）会丢英文 ----------------
SLICE_SINKS = ("status", "broadcast", "_incident", "_check", "_notice", "_fail_alert",
               "_info_dialog")
SLICE_SINK_PREFIXES = ("_publish_",)
# status/broadcast 的实参里，字典字面量的值也算
SLICE_DICT_SINKS = ("status", "broadcast")
# R11 的四种写法出现在这些调用的实参里不算违例（终端与审计本来就是中文）
R11_EXEMPT_CALLS = ("print",)
R11_EXEMPT_RECEIVERS = ("audit",)
