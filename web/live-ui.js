// i18n: done
/* 直播中界面几处「显示什么」的纯逻辑（web/live-ui.js）：顶栏手机同看按钮的文案、
 * 字幕区中间的连接中 / 下载模型 / 等待第一句提示、提示条的图标与文字、顶栏浮层的水平位置、报警分级标签、「回到最新」按钮上的未读条数，以及底部大字幕的
 * 翻译状态角标、「语音识别不确定」角标、「约 N 秒前」、字幕字号的比例，还有顶栏「字幕译成」下拉要补哪一项、
 * 选了哪一项时要就地提示「这是字幕，不是界面语言」。
 * 这些判断本身不碰 DOM，放进 app.js 的立即执行函数就没法被 node 测试 require
 * （同 web/settings-rows.js 的起因）。写法和加载方式照 web/switch.js：浏览器里
 * 是全局函数，Node 测试环境走 module.exports。 */

"use strict";

// 浏览器里 L/LN 是 i18n.js 定义的全局函数（这里的 var 不会清掉它）；node 测试里从 i18n.js 取
if (typeof L === "undefined") { var I18N_ = require("./i18n.js"); var L = I18N_.L, LN = I18N_.LN, APP_NAME = I18N_.APP_NAME; }

/* 顶栏「手机同看」按钮：关着只写功能名；开着没人看写「已打开」；有人在看直接写
 * 人数，比「已打开」信息多、字还短（pm.md #3）。count 只给窄窗口用——那时按钮
 * 只剩图标，文字视觉隐藏，人数作为图标旁的数字露出来；没人看时为空串 */
function shareButtonText(on, viewers) {
  if (!on) return { text: L("手机同看", "Phone Viewing"), count: "" };
  var n = Math.floor(Number(viewers));
  if (!(n > 0)) return { text: L("手机同看 · 已打开", "Phone Viewing · On"), count: "" };
  return { text: L("手机同看 · " + n + " 人", "Phone Viewing · " + n), count: String(n) };
}

/* 顶栏「浮窗」按钮（app/float_window.py）：什么时候露出、点亮、禁用。
 * 露出 = 服务端说这个功能存在（config.float_window.available，只有桌面应用窗口模式才有）而且正在监听中/连接中——
 * 和「换主播」按钮同一条显示规则（app.js setStatus），监听一结束服务端会把浮窗关掉，按钮也跟着收起。
 * 点亮 = 浮窗开着（aria-pressed 同步）。禁用 = 刚点过、还没等到服务端回执，防止连点开关出一串请求。 */
function floatButtonState(available, active, open, pending) {
  var show = !!available && !!active;
  return { hidden: !show, on: show && !!open, pressed: show && !!open, disabled: show && !!pending };
}

/* 字幕区中间那张卡片（#connect-hint）写什么。三种情况：
 * 1. 连接中：「正在连接 @主播…」。有主播名就带上——点错了要等连上才看得出来，
 *    而失败的连接要等约 28 秒，这段时间里主播名是唯一能核对「点没点对」的线索（pm.md #5）。
 * 2. 连接中、而且 status 带着结构化的下载进度（progress.kind 为 model_download，后端在首次下载识别模型时发）：
 *    改写「正在下载识别模型… 37%」（专家审查 U7）。以前这几分钟里中间一直写「正在连接 @x…」，真正的进度只在
 *    顶部一行横幅里。只认这个结构化字段，不看 status 的 detail：connecting 的 detail 几乎从不为空
 *    （pipeline.py 里十来处在发），看到 detail 就搬进卡片，中间和顶部横幅就会重复同一句话（计划审查 Q2）。
 *    没有这个字段（老版本服务端、或下载以外的连接中）就照第 1 条。没有百分比时写已下载的 MB（见 modelDownloadText）。
 * 3. 直播中、这一场还没出过字幕（awaitingFirst）：「等待第一句字幕…」，和浮窗没字幕时的占位同一句（U8）。
 *    以前状态已是直播中、第一句还没出来时字幕区一片空白，看不出程序是在等还是停了。
 * 已有字幕时 inline：改成字幕末尾的一行，不把旧字幕盖住。
 * offline（本地连接断线重连）返回 null：跟 viewTransition 一样，不代表直播
 * 本身变了，调用方维持原样不动 */
function connectHint(state, streamer, hasCaptions, progress, awaitingFirst) {
  if (state === "offline") return null;
  if (state === "live" && awaitingFirst) {
    return { show: true, inline: !!hasCaptions, text: L("等待第一句字幕…", "Waiting for the first caption…") };
  }
  if (state !== "connecting") return { show: false, inline: false, text: "" };
  var download = modelDownloadText(progress);
  if (download) return { show: true, inline: !!hasCaptions, text: download };
  return {
    show: true,
    inline: !!hasCaptions,
    text: streamer ? L("正在连接 @" + streamer + "…", "Connecting to @" + streamer + "…")
                   : L("正在连接…", "Connecting…"),
  };
}

/* status.progress（app/server.py 留存在 config.status 里、hello 时重放）→ 卡片上的下载进度。
 * 只认 kind 为 model_download 的；别的 kind（以后加的）、不是对象、缺 kind 都返回空串，调用方照旧写「正在连接」。
 * pct 夹在 0–100、取整；后端在下载完之前最多发 99。百分号紧贴数字（docs/i18n-style.md §2.3）。
 * 模型大小未知时后端的 pct 是 null、只有 mb（已经下了多少 MB）：写「已下载 580 MB」，和横幅那句的说法一致，
 * 卡片上也看得出数字在走。pct、mb 都不是数时只写「正在下载识别模型…」 */
function modelDownloadText(progress) {
  if (!progress || typeof progress !== "object" || progress.kind !== "model_download") return "";
  var pct = progress.pct;
  if (typeof pct === "number" && isFinite(pct)) {
    var p = Math.max(0, Math.min(100, Math.floor(pct)));
    return L("正在下载识别模型… " + p + "%", "Downloading speech model… " + p + "%");
  }
  var mb = progress.mb;
  if (typeof mb === "number" && isFinite(mb) && mb >= 0) {
    var m = Math.round(mb);
    return L("正在下载识别模型… 已下载 " + m + " MB", "Downloading speech model… " + m + " MB so far");
  }
  return L("正在下载识别模型…", "Downloading speech model…");
}

/* 后端提示条（health / incident）的文字开头自带状态 emoji（app/pipeline.py 的
 * 「🔴 检测已降级…」「⚠️ 识别开始落后…」）：那几条同时打印到终端，终端里要留着；
 * 界面上换成按级别画的线性图标，文字开头那几个去掉，不然图标和 emoji 叠两遍。
 * 只去开头连续的这几种，句中的不动 */
var STATUS_EMOJI_PREFIX = /^(?:(?:🔴|🟠|🟡|⚠|✅|❌)️?\s*)+/;
function stripStatusEmoji(text) {
  return String(text == null ? "" : text).replace(STATUS_EMOJI_PREFIX, "");
}

/* 提示条级别 → 图标名（index.html .icon-sprite 里的 #i-<名字>）。形状跟级别一起变，
 * 不只靠底色：提醒＝三角，降级/出错＝八角，说明＝ⓘ（designer.md #1 的原则） */
function barIconFor(level) {
  if (level === "error" || level === "degraded") return "octagon";
  if (level === "info") return "info";
  return "warn";
}

/* 顶栏浮层的 right（CSS px，相对顶栏右边缘）：右边缘对齐触发按钮的右边缘；
 * 按钮太靠左、浮层会从左边出界时往右推，左右都至少留 margin */
function popoverRight(anchorRight, viewportWidth, panelWidth, margin) {
  var m = margin == null ? 12 : margin;
  var right = Math.round(viewportWidth - anchorRight);
  var maxRight = Math.floor(viewportWidth - panelWidth - m);
  return Math.max(m, Math.min(right, maxRight));
}

/* 报警分级胶囊上的字。以前是「🔴 命中」这类 emoji 前缀，颜色现在交给 CSS
 * （.alert-item.tier-*），这里只给字；认不出的分级按 exact 那一档处理（同旧逻辑）。
 * exact 档中文写「精确」，和手机一样（专家审查 V5）：以前桌面写「命中」，而术语表里「命中」是 match
 * （「命中时弹出报警」），拿它当分级名会混（docs/i18n-style.md §1.3） */
var ALERT_TIER_TEXT = { exact: L("精确", "Exact"), variant: L("变体", "Variant"), fuzzy: L("疑似", "Similar") };
function alertTierText(tier) {
  return Object.prototype.hasOwnProperty.call(ALERT_TIER_TEXT, tier) ? ALERT_TIER_TEXT[tier] : L("精确", "Exact");
}

/* 「回到最新」按钮上的字。翻上去看的时候，新来的行不会把视野拉走，但得让人知道下面攒了多少没看的：
 * 「回到最新 · 12 条新字幕」。n 是翻上去之后新来的条数（回放的不算）；0、负数、不是数字时只写按钮
 * 本来的名字。字幕和弹幕各一个函数：英文的名词和单复数都不同（caption / comment），整句写成模板，
 * 不拼接（docs/i18n-style.md §0 第 5 条） */
function jumpLatestCaptionsText(n) {
  var k = Math.floor(Number(n));
  if (!(k > 0)) return L("回到最新", "Jump to Latest");
  return LN(k, "回到最新 · " + k + " 条新字幕",
            "Jump to Latest · 1 new caption", "Jump to Latest · " + k + " new captions");
}
function jumpLatestCommentsText(n) {
  var k = Math.floor(Number(n));
  if (!(k > 0)) return L("回到最新", "Jump to Latest");
  return LN(k, "回到最新 · " + k + " 条新弹幕",
            "Jump to Latest · 1 new comment", "Jump to Latest · " + k + " new comments");
}

/* ---- 字幕的翻译状态：历史卡片和底部大字幕共用一份 ----
 * 译不出来的字幕屏幕上显示的是原文。卡片早就有角标说明为什么（未翻译（接口拒答）……），
 * 底部大字幕以前没有：大字号的西语孤零零摆在那里，看不出是「还没译」「译失败了」还是「本来就不用译」。
 * 两处从同一个函数取字，改一处不会漏另一处。
 * 只写观察到的：接口拒答、结果不像译文、翻译失败、因积压被跳过。不写原因（CLAUDE.md 第八条）。
 * 返回 { text, fail }：text 为空串表示没有角标；fail 表示翻译没成（卡片上画成红色） */
function captionStateChip(msg) {
  var state = msg ? msg.translate_state : undefined;
  if (state === "failed") {
    return {
      text: msg.refused ? L("未翻译（接口拒答）", "Not translated (API refusal)")
        : msg.not_translation ? L("未翻译（结果不像译文）", "Not translated (the result didn’t look like a translation)")
        : L("翻译失败", "Translation failed"),
      fail: true,
    };
  }
  if (state === "dropped") return { text: L("翻译已跳过（积压）", "Skipped (backlog)"), fail: false };
  return { text: "", fail: false };
}

/* 底部大字幕的状态角标：译文到了没有角标；没译成（还有被积压跳过的）用卡片同款的字。
 * 译文还在路上（pending）时不画：那是每一句都要走的几秒，角标来了又走会在主阅读区闪个不停；
 * 译文迟迟不来的时候，下面的「约 N 秒前」照样在涨，而服务端最终一定会给出结局（译好、失败或被跳过）。 */
function liveBarChip(msg) {
  if (!msg || msg.translated) return { text: "", fail: false };
  return captionStateChip(msg);
}

/* ---- 「语音识别不确定」角标：历史卡片的 .meta 行和底部大字幕的 #live-meta 行共用一份 ----
 * 服务端只在 settings.json 的 asr_uncertain_chip 打开（默认关）、而且这一段命中了识别风险规则 v1 里
 * 会挂角标的那几条（app/asr_risk.py CHIP_FLAGS）时，才给 caption 消息带 asr_flag（代码列表）。
 * 这几条只抓得到「像是出了大问题」的段：通顺的错句标不出来，所以没有角标**不等于**识别对了。
 * 说明（title）只写看到了什么，不写为什么（CLAUDE.md 第八条）：「有声时长」量的是能量，背景音乐也算，
 * 写「说话时长」就是替人认定了原因。每样观察一句整句，后面接同一句「照原样显示」，不拼半句。
 * 认不出的代码（以后升了规则版本、这一页还是旧的）照样挂角标，说明用笼统的那一句。
 * 返回 { text, title }：text 为空串表示没有角标。手机页和浮窗 v1 不显示（手机白名单不放这个键）。 */
var ASR_FLAG_NOTES = [
  [["few_words"], function () { return L("识别出的字数和有声时长对不上。", "The word count didn’t match how long there was sound."); }],
  [["low_conf"], function () { return L("识别置信度偏低。", "Recognition confidence was low."); }],
  // nan_lp 是解码器没给出有限的置信度（10-14 #355 只解出一个「!」）：没有这个数，不是这个数偏低
  [["nan_lp"], function () { return L("这一段没有拿到识别置信度。", "No recognition confidence was reported for this segment."); }],
  [["loop"], function () { return L("识别出的文字有重复。", "The recognized text repeats."); }],
];
function captionAsrChip(msg) {
  var flags = msg && Array.isArray(msg.asr_flag) ? msg.asr_flag : null;
  if (!flags || !flags.length) return { text: "", title: "" };
  var notes = [];
  ASR_FLAG_NOTES.forEach(function (row) {
    if (row[0].some(function (code) { return flags.indexOf(code) !== -1; })) notes.push(row[1]());
  });
  if (!notes.length) notes.push(L("这一段的识别被标记为不确定。", "This segment’s recognition was marked unclear."));
  notes.push(L("原文和译文都照识别结果显示，没有删改。", "The original and translation are shown as recognized, with nothing removed."));
  return { text: L("语音识别不确定，建议查看原文", "Recognition unclear · check the original"),
           title: notes.join(L("", " ")) };
}

/* ---- 覆盖率角标：近几分钟实际收到了多少直播音频（2026-10-19，改进清单第 4 条）----
 * 服务端每 10 秒的 stats 带 coverage_pct（最近 5 分钟、开场不足 5 分钟按已过的时间收到的直播音频占比）、
 * coverage_window_min（这个窗口几分钟）、coverage_low（低于 70% 连续两次才为真、回到 85% 以上才撤）、
 * breaks（本场断了几次），见 app/pipeline_stream.py _coverage_stats。只在 coverage_low 时出角标：
 * 正常的时候主阅读区底下不多一个数字。同一件事页面顶部另有一条持续提示（session:audio_rate），说缺的部分
 * 没有经过检测；角标只给数字，title 说这个数是什么。只写观察，不写为什么（CLAUDE.md 第八条）。
 * 返回 { text, title }：text 为空串表示没有角标。手机页和浮窗不显示。 */
function coverageChip(stats) {
  if (!stats || !stats.coverage_low || typeof stats.coverage_pct !== "number" ||
      !isFinite(stats.coverage_pct)) return { text: "", title: "" };
  var p = Math.max(0, Math.min(100, Math.round(stats.coverage_pct)));
  var m = Math.max(1, Math.round(Number(stats.coverage_window_min) || 5));
  var n = Math.max(0, Math.floor(Number(stats.breaks) || 0));
  return {
    text: L("近 " + m + " 分钟收到 " + p + "%", "Last " + m + " min: " + p + "% received"),
    title: LN(n, "近 " + m + " 分钟里实际收到的直播音频占比；本场已中断 " + n + " 次",
              "Share of stream audio received in the last " + m + " min. 1 interruption this session.",
              "Share of stream audio received in the last " + m + " min. " + n + " interruptions this session.")
  };
}

/* ---- 底部大字幕的滞后：大字幕上这一句的声音结束多久了 ----
 * 服务端的 caption 消息带 ts（原文上屏的时刻）和 e2e_ms（这一段音频结束 → 上屏）。
 * ts − e2e_ms = 这一段音频结束的时刻，和违禁词报警延迟（now − audio_end_ts）是同一个起点。
 * 不从「最早一个词说出口」算：那个数被句子长短主导。一条新字幕上屏时就已经是 片段时长 + e2e ≈ 10 秒，
 * 下一句八九秒后才来，所以健康的直播里它一直在 10～18 秒之间转圈，永远写不出「刚刚」，
 * 中控没有基线，分不出这是正常还是积压了 10 秒。从最早一个词算的那个数是服务端的「字幕等待」
 * （Telemetry.caption_wait，最近 200 句的 p50 / P95）：以前写在页脚统计行，2026-10-15 那一行删了，
 * 现在只随 stats 消息发出，页面不画。
 * e2e_ms 缺了（演示模式、老版本服务端）就返回 null：不知道就不写。ts 缺失时按收到的这一刻算（nowMs）。 */
function captionAudioEndAt(msg, nowMs) {
  if (!msg || typeof msg.e2e_ms !== "number" || !(msg.e2e_ms >= 0)) return null;
  var shownAt = typeof msg.ts === "number" && msg.ts > 0 ? msg.ts * 1000 : nowMs;
  if (typeof shownAt !== "number" || !isFinite(shownAt)) return null;
  return shownAt - msg.e2e_ms;
}

/* 多久才值得写。2026-10-13～15 三场直播的 3216 次换句，「音频结束 → 被下一句换掉」p50 10.4 秒、
 * p90 11.8 秒、p99 13.0 秒：分段上限 9 秒 + 上屏 p90 2.8 秒，所以 12 秒会在健康的直播里闪（6.1% 的换句
 * 超过它），15 秒只剩 0.6%——那些是主播停了很久，或者真的积压了。低于它这一行不出现：
 * 正常的时候不让一个数字在主阅读区底下一秒一跳，它出现就是一个信号。 */
var CAPTION_LAG_SHOW_SEC = 15;

/* 「约 N 秒前」。ageMs 是从这一段音频结束到现在的毫秒数，页面每秒重算一次，所以数字会随时间涨——
 * 大字幕上的这一句停多久，它就老多久。不到 CAPTION_LAG_SHOW_SEC 秒、负数（本机时钟被拨回）、
 * 离谱的大数（超过 6 小时）都不写，返回空串。秒数四舍五入；一分钟以上改写分钟
 * （大字幕停着不动多半是主播不说话了）。只写观察，不猜原因（CLAUDE.md 第八条）。
 * 英文照 docs/i18n-style.md §2.3、§2.4：sec / min 缩写，数字和单位之间一个空格 */
function captionAgeText(ageMs) {
  if (typeof ageMs !== "number" || !isFinite(ageMs) || ageMs > 6 * 3600 * 1000) return "";
  if (ageMs < CAPTION_LAG_SHOW_SEC * 1000) return "";
  if (ageMs < 60000) {
    var sec = Math.round(ageMs / 1000);
    return L("约 " + sec + " 秒前", "About " + sec + " sec ago");
  }
  var min = Math.round(ageMs / 60000);
  return L("约 " + min + " 分钟前", "About " + min + " min ago");
}

/* ---- 字幕字号 ----
 * 顶栏滑块（20–60，宽窗口默认 34）定底部大字幕的字号；历史卡片的译文/原文和弹幕栏的译文/原文
 * 按同一个比例跟着变，不再各自固定 17/13/15px。比例 = 滑块值 / 34，夹在 0.85–1.75 之间：
 * 往小调时卡片不低于 85%（17px 的译文最小约 14.5px，13px 的原文最小约 11px），往大调到顶是 1.75 倍。
 * 没调过滑块的人什么也不设，样式表里 --cap-scale 的默认值是 1，卡片还是今天的 17/13/15px。 */
var CAPTION_SIZE_MIN = 20;       // 滑块 #font-size 的 min/max；服务端 app/settings.py 里是同一对数
var CAPTION_SIZE_MAX = 60;
var CAPTION_SIZE_BASE = 34;
var CAPTION_SCALE_MIN = 0.85;
var CAPTION_SCALE_MAX = 1.75;
function captionScale(size) {
  var n = Number(size);
  if (!(n > 0) || !isFinite(n)) return 1;
  var scale = Math.min(CAPTION_SCALE_MAX, Math.max(CAPTION_SCALE_MIN, n / CAPTION_SIZE_BASE));
  return Math.round(scale * 1000) / 1000;
}
/* 服务端（config.caption_size）或 localStorage 里的字号：是 20–60 之间的整数才认，否则 null。
 * 不夹到边界——来路不明的值不该被当成用户选的（同 app/settings.py normalize_caption_size） */
function captionSizeFromConfig(value) {
  if (typeof value === "boolean" || value == null || value === "") return null;
  var n = Number(value);
  if (!isFinite(n)) return null;
  n = Math.round(n);
  return n >= CAPTION_SIZE_MIN && n <= CAPTION_SIZE_MAX ? n : null;
}

/* 顶栏「字幕译成」下拉（#target-lang）。用户 2026-10-20 决定只列四种：简体中文、繁体中文、英语、西语。
 * 原来列 13 种、标签叫「目标语言」，他把它当成了界面语言，切成 English 等着界面变（界面语言在
 * 「设置 > 界面语言」，#ui-lang-select）。四种写死在 index.html 的 <option> 里，tests/target-lang.test.mjs
 * 钉着两边一致。后端照旧什么语言代码都收：settings.json 里存着以前选的日语、或者 --target ja 启动时，
 * 下拉补一项把它显示出来，不能静默落到第一项——那样中控以为字幕是简体中文，实际出的是日语。
 * 补的那项只在它是当前目标时存在（app.js showTargetLang）。名字是各语言的自称，是数据，哪种界面
 * 语言都不翻（同下拉本身的 translate="no"）；没收录的代码原样显示。
 * 返回要补的 { value, text }；当前目标就在四种里、或是空的，返回 null */
var TARGET_LANGS = ["zh-CN", "zh-TW", "en", "es"];
var TARGET_LANG_OTHER_NAMES = {
  ja: "日本語", ko: "한국어", fr: "Français", de: "Deutsch", ru: "Русский",   // i18n: data（语言自称）
  pt: "Português", vi: "Tiếng Việt", th: "ไทย", id: "Bahasa Indonesia",
};
function targetLangExtra(code) {
  var value = code == null ? "" : String(code);
  if (!value || TARGET_LANGS.indexOf(value) >= 0) return null;
  var known = Object.prototype.hasOwnProperty.call(TARGET_LANG_OTHER_NAMES, value);
  return { value: value, text: known ? TARGET_LANG_OTHER_NAMES[value] : value };
}

/* 「字幕译成」下拉刚被这一页的人换成了另一种界面语言：英文界面选了中文（简体或繁体），或者中文界面选了 English。
 * 2026-10-29 用户在英文界面里想切回中文，点的就是这个下拉：连接中、默认 1000px 的窗口里标签被省掉（style.css
 * ≤1080px 那一段），顶栏只剩一个写着「中文（简体）」的下拉；选了它只改了字幕目标，当时没在播，什么都看不出来，
 * 以为「没反应」。和 10-20 是同一种误会（docs/i18n-style.md §1.3「字幕译成」那一行）。
 * 返回那种界面语言（"zh" / "en"），app.js 在下拉下面弹一个小浮层，给一颗切换界面的按钮；不像是这种误会时返回 null。
 * uiLang 是本页的界面语言（i18n.js 的 UI_LANG），不是 "en" 的一律按中文算（同 settings-rows.js langReload）。
 * 西语、四种以外的语言、和本页同一种语言都不提示：那些不会被读成「我要换界面」 */
function captionsLangHint(uiLang, value) {
  var v = value == null ? "" : String(value);
  if (uiLang === "en") return v === "zh-CN" || v === "zh-TW" ? "zh" : null;
  return v === "en" ? "en" : null;
}

/* 该写到 <html> 上的 CSS 变量：大字幕的字号和卡片/弹幕的比例 */
function captionSizeVars(size) {
  var n = Math.round(Number(size));
  return { "--sub-size": n + "px", "--cap-scale": String(captionScale(n)) };
}

/* Node 测试环境导出；浏览器里作为全局函数被 app.js 使用 */
if (typeof module !== "undefined" && module.exports) {
  module.exports = { shareButtonText: shareButtonText, floatButtonState: floatButtonState,
                     connectHint: connectHint, modelDownloadText: modelDownloadText,
                     stripStatusEmoji: stripStatusEmoji, barIconFor: barIconFor,
                     popoverRight: popoverRight, alertTierText: alertTierText,
                     jumpLatestCaptionsText: jumpLatestCaptionsText,
                     jumpLatestCommentsText: jumpLatestCommentsText,
                     captionStateChip: captionStateChip, liveBarChip: liveBarChip,
                     captionAsrChip: captionAsrChip, coverageChip: coverageChip,
                     captionAudioEndAt: captionAudioEndAt, captionAgeText: captionAgeText,
                     CAPTION_LAG_SHOW_SEC: CAPTION_LAG_SHOW_SEC,
                     captionScale: captionScale, captionSizeVars: captionSizeVars,
                     captionSizeFromConfig: captionSizeFromConfig,
                     TARGET_LANGS: TARGET_LANGS, targetLangExtra: targetLangExtra,
                     captionsLangHint: captionsLangHint };
}
