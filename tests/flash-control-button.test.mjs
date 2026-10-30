// 闪购浮层顶部那一排「开始」「停止」「继续」「打开闪购助手」的接线（web/index.html #flash-ctl，web/app.js）。
// 规则本身（显示什么、能不能点、结果的话）在 tests/flash-control.test.mjs 测 flash.js 的纯函数；这里是 node:vm 里真跑的
// web/app.js（tests/desktop-page.mjs），中文、英文两种界面各跑一遍，钉住：
//   1. 按状态显示哪几颗；版本认得出且低于 0.8.0、连不上时只剩「打开」，最底下那句换回第一步那句（审查第 7、15 条）；
//   2. 「开始」点两下：第一下写「再点一次开始」，武装后 400 毫秒内的第二下不算（审查第 6 条），6 秒复原，关浮层作废；
//      停止、继续点一下就发；发的消息只有 {type:"flash_control", action}；
//   3. 在途：点出去马上禁用、写「正在发送…」；后端把它记成在途、回了结果、重连的 hello 到了才放开（后端在途时收到的第二个
//      指令直接丢、不回话，页面不能一直等它）；断线时不装作发出去了；
//   4. 结果写进浮层里那一句；过时的结果（例如浮层关着时到的 not_seen）在状态显示生效以后自己收掉（审查第 3 条）；
//   5. 焦点：按下的那颗变成不能点或藏起来时挪到 #flash-ctl（不滚动），三颗按钮的 aria-describedby 指着 #flash-ctl-note
//      （审查第 5 条）；
//   6. 开始后的过渡：等生效时顶栏按钮不变色，主句「正在开始…（闪购助手在刷新页面）」；后端 30 秒收掉 control 才照原规则。
// 真浏览器里的版面（1000px 下这一排不溢出、不折乱）由 G10 的 flash-ctl-* 场景量。
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadDesktop, hello, web } from "./desktop-page.mjs";
import { SAMPLE, st } from "./flash-fixtures.mjs";

const calm = (extra = {}) => st(Object.assign({ attention: [], alerts: [], app_version: "0.9.0",
  products: SAMPLE.products.map((p) => Object.assign({}, p, { stuck: false })) }, extra));
const idle = (extra = {}) => calm(Object.assign({ running: false, paused_reason: "not_started" }, extra));
const hidden = (page, id) => page.el(id).classList.contains("hidden");
const visible = (page) => ["flash-start", "flash-stop", "flash-resume", "flash-open"].filter((id) => !hidden(page, id));
const usable = (page) => visible(page).filter((id) => !page.el(id).disabled);
const sent = (page) => page.sent.filter((m) => m.type === "flash_control");
const push = (page, state) => page.push(Object.assign({ type: "flash_sale" }, state));
const result = (page, action, error) => page.push({ type: "flash_control_result", action, ok: error == null, error: error || null });
const text = (page, id) => page.el(id).textContent;

for (const lang of ["zh", "en"]) {
  const name = (s) => s + "（" + lang + "）";
  const T = (zh, en) => (lang === "en" ? en : zh);
  const ONLY_HERE = T("这里只能开始、停止和继续，其他操作请到闪购助手里。",
                      "Only Start, Stop and Resume work here. Do everything else in Flash Sale Helper.");
  const STATUS_ONLY = T("这里只显示状态。要操作，请到闪购助手里。", "This panel only shows status. Make changes in Flash Sale Helper.");
  const start = (page) => page.el("flash-start");

  function opened(state) {
    const page = loadDesktop({ lang });
    hello(page, { flash_sale: state });
    page.el("flash-btn").click();
    assert.ok(!hidden(page, "flash-panel"));
    return page;
  }

  test(name("按状态出哪几颗：没在运行出开始，在运行出停止，确认框里点了取消的暂停另出继续；「打开」一直在，最底下那句跟着换"), () => {
    const page = opened(idle());
    assert.deepEqual(visible(page), ["flash-start", "flash-open"]);
    assert.equal(text(page, "flash-start"), T("开始", "Start"));
    assert.equal(text(page, "flash-desc"), ONLY_HERE);
    assert.ok(hidden(page, "flash-ctl-note"));
    push(page, calm());
    assert.deepEqual(visible(page), ["flash-stop", "flash-open"]);
    assert.deepEqual(usable(page), ["flash-stop", "flash-open"]);
    push(page, calm({ paused_reason: "user_interaction" }));
    assert.deepEqual(usable(page), ["flash-stop", "flash-resume", "flash-open"]);
    assert.equal(text(page, "flash-resume"), T("继续", "Resume"));
    push(page, { conn: "unreachable", stale: false, products: [], alerts: [], attention: [], control: null });
    assert.deepEqual(visible(page), ["flash-open"], "连不上：只剩「打开」");
    assert.equal(text(page, "flash-desc"), STATUS_ONLY, "一颗都没有：换回第一步那句");
    assert.ok(hidden(page, "flash-ctl-note"));
  });

  test(name("版本认得出且低于 0.8.0：只剩「打开」，旁边写「0.8.0 起才能……」，最底下那句不和它打架；认不出的照常出"), () => {
    const page = opened(idle({ app_version: "0.7.3" }));
    assert.deepEqual(visible(page), ["flash-open"]);
    assert.ok(!hidden(page, "flash-ctl-note"));
    assert.equal(text(page, "flash-ctl-note"), T("闪购助手 0.8.0 起才能从这里开始、停止。", "Start and Stop need Flash Sale Helper 0.8.0 or later."));
    assert.equal(text(page, "flash-desc"), STATUS_ONLY);
    push(page, idle({ app_version: "dev-build" }));
    assert.deepEqual(visible(page), ["flash-start", "flash-open"], "认不出的版本：照常出，回来 404 / 405 再说");
    assert.ok(hidden(page, "flash-ctl-note"));
  });

  test(name("页面没准备好：开始不能点，旁边说一句（三颗按钮的 aria-describedby 都指着它）；确认框弹着：停止不能点"), () => {
    const page = opened(idle({ page: "login_required", paused_reason: null, attention: ["page"] }));
    assert.deepEqual(visible(page), ["flash-start", "flash-open"]);
    assert.deepEqual(usable(page), ["flash-open"]);
    // 照 /status 的 page 那句说（decisions-step2），和主句同一句
    assert.equal(text(page, "flash-ctl-note"), T("专用 Chrome 停在 TikTok 登录页，登录后才能继续。",
                                                 "The dedicated Chrome is on the TikTok sign-in page. Sign in to continue."));
    for (const id of ["flash-start", "flash-stop", "flash-resume"]) {
      assert.equal(page.el(id).getAttribute("aria-describedby"), "flash-ctl-note", id);
    }
    start(page).click();
    assert.equal(text(page, "flash-start"), T("开始", "Start"), "不能点的不武装");
    push(page, calm({ paused_reason: "awaiting_approval", attention: ["paused"] }));
    assert.deepEqual(usable(page), ["flash-open"]);
    assert.equal(text(page, "flash-ctl-note"), T("确认框处理完以后才能从这里停止。", "Stop works here once the confirmation is handled."));
  });

  test(name("「开始」点两下：第一下写「再点一次开始」不发；武装后 400 毫秒内的第二下不算；400 毫秒到 6 秒之间再点才发"), () => {
    const page = opened(idle());
    start(page).click();
    assert.equal(text(page, "flash-start"), T("再点一次开始", "Click Again to Start"));
    assert.deepEqual(sent(page), []);
    page.advance(150);
    start(page).click();                                          // 双击的第二下
    assert.deepEqual(sent(page), [], "武装后 400 毫秒内的第二下不算");
    assert.equal(text(page, "flash-start"), T("再点一次开始", "Click Again to Start"), "还武装着");
    page.advance(250);                                            // 武装后正好 400 毫秒
    start(page).click();
    assert.deepEqual(sent(page), [{ type: "flash_control", action: "start" }], "只发 action，别的键一个不带");
    assert.equal(text(page, "flash-start"), T("正在发送…", "Sending…"));
    assert.equal(start(page).disabled, true);
    start(page).click();
    assert.equal(sent(page).length, 1, "在途：点不了");
  });

  test(name("「开始」武装 6 秒不点就复原（定时器到点、或者定时器没跑也按时刻算）；关上浮层作废；清空字幕那套不受影响"), () => {
    const page = opened(idle());
    start(page).click();
    page.fire(6000);
    assert.equal(text(page, "flash-start"), T("开始", "Start"), "到点复原");
    start(page).click();
    page.advance(6000);                                           // 定时器被节流、没跑：照时刻算，这一下重新武装
    start(page).click();
    assert.deepEqual(sent(page), []);
    assert.equal(text(page, "flash-start"), T("再点一次开始", "Click Again to Start"));
    page.el("flash-btn").click();                                 // 关上
    assert.equal(text(page, "flash-start"), T("开始", "Start"), "关上浮层：武装作废");
    page.el("flash-btn").click();
    page.advance(1000);
    start(page).click();
    assert.deepEqual(sent(page), [], "重新打开以后又是第一下");
    // 清空字幕（confirmTwice）照旧：武装后马上再点就清，不受 400 毫秒的限制
    const clear = page.el("clear-btn");
    clear.click();
    assert.equal(clear.textContent, T("再点确认", "Clear?"), "「再点确认」只对应 Clear?");
  });

  test(name("停止、继续点一下就发；在途时那一排禁用、写「正在发送…」；后端记成在途以后由状态说了算，结果回来放开"), () => {
    const page = opened(calm({ paused_reason: "user_interaction" }));
    page.el("flash-stop").click();
    assert.deepEqual(sent(page), [{ type: "flash_control", action: "stop" }]);
    assert.equal(text(page, "flash-stop"), T("正在发送…", "Sending…"));
    assert.deepEqual(usable(page), ["flash-open"], "在途：三颗都不能点，「打开」照样能点");
    push(page, calm({ paused_reason: "user_interaction" }));      // 后端还没处理到这个指令时推来的一份（control 还是 null）
    assert.deepEqual(usable(page), ["flash-open"], "本页记着的在途不被这一份冲掉");
    push(page, calm({ paused_reason: "user_interaction", control: { action: "stop", phase: "sending" } }));
    assert.equal(text(page, "flash-stop"), T("正在发送…", "Sending…"));
    push(page, calm({ paused_reason: "user_interaction", control: { action: "stop", phase: "waiting" } }));
    assert.equal(text(page, "flash-stop"), T("正在停止…", "Stopping…"));
    assert.deepEqual(usable(page), ["flash-open"]);
    result(page, "stop", null);
    assert.ok(hidden(page, "flash-note"), "接受了不写");
    push(page, idle());
    assert.deepEqual(usable(page), ["flash-start", "flash-open"]);
    assert.equal(text(page, "flash-start"), T("开始", "Start"));
    // 继续
    push(page, calm({ paused_reason: "user_interaction" }));
    page.el("flash-resume").click();
    assert.deepEqual(sent(page).slice(-1), [{ type: "flash_control", action: "resume" }]);
    assert.equal(text(page, "flash-resume"), T("正在发送…", "Sending…"));
  });

  test(name("后端在途时丢掉的指令不回话：别的控制页的结果（广播）、重连的 hello 都把本页记着的在途放掉"), () => {
    const page = opened(calm());
    page.el("flash-stop").click();
    assert.deepEqual(usable(page), ["flash-open"]);
    result(page, "start", "busy");                                // 另一个控制页的指令的结果，也广播到这一页
    assert.deepEqual(usable(page), ["flash-stop", "flash-open"]);
    page.el("flash-stop").click();
    assert.deepEqual(usable(page), ["flash-open"]);
    hello(page, { flash_sale: calm() });                          // 断线重连
    assert.deepEqual(usable(page), ["flash-stop", "flash-open"]);
  });

  test(name("重连的 hello：「开始」的武装作废"), () => {
    const page = opened(idle());
    start(page).click();
    hello(page, { flash_sale: idle() });
    assert.equal(text(page, "flash-start"), T("开始", "Start"));
  });

  test(name("断线：三颗都禁用；没发出去就不装作发出去了（同「打开」那一句），不记在途"), () => {
    const page = opened(calm());
    page.disconnect();
    page.el("flash-stop").click();
    assert.deepEqual(sent(page), []);
    assert.equal(text(page, "flash-note"), T("与本地服务断开，正在重连——稍候再试。",
      "Lost connection to the local service. Reconnecting… Try again in a moment."));
    assert.deepEqual(usable(page), ["flash-stop", "flash-open"], "没发出去：不进在途");
    page.push({ type: "status", state: "offline", detail: "" });
    assert.deepEqual(usable(page), [], "和本地服务断开期间：「打开」和三颗都禁用");
    page.connect();
    page.push({ type: "status", state: "live", detail: "" });
    assert.deepEqual(usable(page), ["flash-stop", "flash-open"]);
  });

  test(name("结果：闪购助手没接受写「闪购助手没有接受：…」，认不出的代码原样放进 translate=\"no\"；接受了把上一句收掉"), () => {
    const page = opened(idle());
    result(page, "start", "busy");
    assert.ok(!hidden(page, "flash-note"));
    assert.equal(text(page, "flash-note"), T("闪购助手没有接受：正忙，稍等再试。", "Flash Sale Helper declined: It’s busy. Try again in a moment."));
    result(page, "start", "cooldown_x");
    assert.equal(text(page, "flash-note"), T("闪购助手没有接受：", "Flash Sale Helper declined: ") + "cooldown_x");
    const code = page.el("flash-note").children[0];
    assert.equal(code.getAttribute("translate"), "no");
    result(page, "start", "no_token");
    assert.equal(text(page, "flash-note"), T("没有发出：读不到闪购助手的控制密钥。确认它是 0.8.0 以上，重开它以后再试。",
      "Not sent: Couldn’t read Flash Sale Helper’s control key. Make sure it’s 0.8.0 or later, reopen it, then try again."));
    result(page, "start", null);
    assert.ok(hidden(page, "flash-note"));
  });

  test(name("过时的结果自己收掉：浮层关着时到的 not_seen，之后状态显示开始运行了就收掉；断线、open 没打开那几句不跟着收"), () => {
    const page = loadDesktop({ lang });
    hello(page, { flash_sale: idle() });                          // 浮层关着
    push(page, idle({ control: { action: "start", phase: "waiting" } }));
    push(page, idle());                                           // 30 秒：后端收掉 control……
    result(page, "start", "not_seen");                            // ……另发 not_seen
    assert.ok(!hidden(page, "flash-note"));
    assert.equal(text(page, "flash-note"), T("30 秒内没看到闪购助手开始运行。", "Flash Sale Helper didn’t show as running within 30 sec."));
    push(page, calm());                                           // 35 秒：开始运行了
    assert.ok(hidden(page, "flash-note"), "生效了：那句收掉（浮层关着也收）");
    page.el("flash-btn").click();
    assert.ok(hidden(page, "flash-note"), "打开浮层时看不到那句过时的话");
    // 浮层开着时一样
    result(page, "stop", "not_seen");
    assert.ok(!hidden(page, "flash-note"));
    push(page, idle());
    assert.ok(hidden(page, "flash-note"));
    // open 没打开的那句：不是哪个动作的结果，状态怎么变都不收
    page.push({ type: "notice", where: "flash", text: "Couldn’t open Flash Sale Helper (open returned 1)." });
    push(page, calm());
    assert.equal(text(page, "flash-note"), "Couldn’t open Flash Sale Helper (open returned 1).");
  });

  test(name("焦点：按下的那颗变成不能点或藏起来时挪到 #flash-ctl（不滚动）；鼠标点的（焦点不在按钮上）不去动"), () => {
    const page = opened(idle());
    const row = page.el("flash-ctl");
    assert.equal(row.getAttribute("tabindex"), "-1");
    const how = [];
    const realFocus = row.focus;
    row.focus = function (opts) { how.push(opts); return realFocus.call(this, opts); };
    start(page).focus();                                          // 键盘 Tab 到「开始」上
    start(page).click();                                          // 第一下：还能点，焦点不动
    assert.equal(page.active(), start(page));
    page.advance(500);
    start(page).click();                                          // 第二下：变成「正在发送…」、禁用
    assert.equal(page.active(), row, "按钮禁用了：焦点挪到这一排，不掉出浮层");
    assert.equal(JSON.stringify(how), JSON.stringify([{ preventScroll: true }]));
    push(page, calm());                                           // 开始运行了：「开始」藏起来，焦点已经在这一排上
    assert.equal(page.active(), row);
    result(page, "start", null);                                  // 接受了：本页记着的在途放掉，「停止」能点了
    page.el("flash-stop").focus();
    page.el("flash-stop").click();
    assert.equal(page.active(), row);
    // 鼠标点：焦点在别处
    result(page, "stop", "not_running");
    page.el("history").focus();
    page.el("flash-stop").click();
    assert.equal(page.active(), page.el("history"));
    assert.equal(how.length, 2);
  });

  test(name("开始后的过渡：等生效时按钮写「正在开始…」，顶栏按钮不变色、主句「正在开始…（闪购助手在刷新页面）」；30 秒后照原规则"), () => {
    const page = opened(idle());
    const waiting = idle({ page: "error", stale: true, running: false, paused_reason: null, products: [], attention: [],
                           control: { action: "start", phase: "waiting" } });
    push(page, waiting);
    assert.ok(!page.el("flash-btn").classList.contains("attn"), "不变橙");
    // 胶囊和顶栏按钮的 title / aria-label 也不写「未开始」（读起来像没开起来），写按钮上同一句
    assert.equal(text(page, "flash-state"), T("正在开始…", "Starting…"));
    assert.equal(page.el("flash-btn").getAttribute("aria-label"), T("闪购助手 · 正在开始…", "Flash Sale Helper · Starting…"));
    assert.equal(text(page, "flash-headline"), T("正在开始…（闪购助手在刷新页面）", "Starting… (Flash Sale Helper is reloading the page)"));
    assert.ok(!page.el("flash-headline").classList.contains("attn"), "不出红字");
    assert.equal(text(page, "flash-start"), T("正在开始…", "Starting…"));
    assert.equal(start(page).disabled, true);
    assert.ok(hidden(page, "flash-ctl-note"), "过渡里按钮旁边不写页面那句");
    push(page, Object.assign({}, waiting, { control: null, attention: ["stale", "page"] }));
    result(page, "start", "not_seen");
    assert.ok(page.el("flash-btn").classList.contains("attn"), "30 秒后：照原规则变色");
    assert.ok(page.el("flash-headline").classList.contains("attn"));
    assert.equal(text(page, "flash-note"), T("30 秒内没看到闪购助手开始运行。", "Flash Sale Helper didn’t show as running within 30 sec."));
  });
}

// ---- 页面文件本身 -------------------------------------------------------------------------

test("顶部一排紧跟着标题行：开始、停止、继续、打开闪购助手（挪上来了），说明和结果的那两句跟在后面，最底下那句有 id", () => {
  const html = web("index.html");
  const panel = /<div id="flash-panel"[\s\S]*?\n {6}<\/div>\n/.exec(html)[0];
  const at = (needle) => {
    const i = panel.indexOf(needle);
    assert.ok(i >= 0, "浮层里没有 " + needle);
    return i;
  };
  const order = ['class="share-head"', 'id="flash-ctl"', 'id="flash-start"', 'id="flash-stop"', 'id="flash-resume"', 'id="flash-open"',
                 'id="flash-ctl-note"', 'id="flash-note"', 'id="flash-headline"', 'id="flash-products"', 'id="flash-alerts"',
                 'id="flash-version"', 'id="flash-desc"'];
  const pos = order.map(at);
  assert.deepEqual(pos.slice().sort((a, b) => a - b), pos, "先后不对");
  assert.ok(!panel.includes("flash-foot"), "「打开闪购助手」不再在最底下");
  const row = /<div id="flash-ctl" class="flash-ctl" tabindex="-1">([\s\S]*?)<\/div>/.exec(panel);
  assert.ok(row, "#flash-ctl 带 tabindex=-1");
  const tags = [...row[1].matchAll(/<button [^>]*>[^<]*<\/button>/g)].map((m) => m[0]);
  assert.equal(tags.length, 4);
  for (const [i, [id, zh, en]] of [["flash-start", "开始", "Start"], ["flash-stop", "停止", "Stop"], ["flash-resume", "继续", "Resume"],
                                   ["flash-open", "打开闪购助手", "Open Flash Sale Helper"]].entries()) {
    const tag = tags[i];
    assert.match(tag, new RegExp('id="' + id + '"'));
    assert.match(tag, /type="button"/);
    assert.match(tag, new RegExp('data-en="' + en + '">' + zh + "</button>$"));
    if (id !== "flash-open") {
      assert.match(tag, /aria-describedby="flash-ctl-note"/);
      assert.match(tag, /class="btn small [a-z]+ hidden"/, "没收到状态之前藏着");
    }
  }
  assert.match(panel, /<p id="flash-note" class="flash-note hidden" aria-live="polite"><\/p>/);
  assert.match(panel, /<p id="flash-desc" class="share-desc" data-en="This panel only shows status\. Make changes in Flash Sale Helper\.">这里只显示状态。要操作，请到闪购助手里。<\/p>/);
  assert.match(html, /<script src="\/static\/flash\.js\?v=2"><\/script>\n {2}<script src="\/static\/app\.js\?v=84"><\/script>/);
});

test("样式：那一排放不下就折行（不横向撑出浮层），焦点环只给键盘；卡片序号不跟标题一起缩", () => {
  const css = web("style.css").replace(/\/\*[\s\S]*?\*\//g, "");
  const rule = (sel) => {
    const m = new RegExp(sel.replace(/[.:()-]/g, "\\$&") + " \\{([^}]*)\\}").exec(css);
    assert.ok(m, "没有 " + sel);
    return m[1];
  };
  assert.match(rule(".flash-ctl"), /display: flex; flex-wrap: wrap;/);
  assert.match(rule(".flash-ctl:focus"), /outline: none/);
  assert.match(rule(".flash-ctl:focus-visible"), /box-shadow: var\(--focus\)/);
  assert.match(rule(".flash-pos"), /flex: none/);
  assert.match(rule(".flash-version:empty"), /display: none/);
});
