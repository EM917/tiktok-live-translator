// 顶部状态横幅和更新条（专家审查 U6、U10、I8、I2；web/app.js renderStatusBanner / syncBannerCover / showZipKeepNote）。
//
// 页面在 node:vm 里真的跑一遍（脚手架见 tests/desktop-dom.mjs），中文页、英文页各走一遍：
//   U6  断网时 app/pipeline.py _wait_for_network 把同一句话既挂成红色持续提示、又发成 status：页面上只出现一次
//       （横幅收起、状态胶囊照常）；那条持续提示撤掉后横幅露出来；文字不一样的 status 照常显示；
//   U10 「接口不给流地址」那种横幅超过 3 行时默认只露第一行（4003110 那句说明）和「显示全部步骤」，可以展开、
//       收起；一两行的变体不折叠、没有按钮；
//   I8  status 带 paths 时每条路径是一行可复制的代码行（含空格的路径也整条复制），文字里那几行不再重复；
//       没有 paths 时不从文字里猜路径；
//   I2  ZIP 安装的更新条多一行：要复制到新文件夹的那几样，链接到 README 里同一份清单。
// 横幅文字本身（中英两套、分行）由 tests/test_browser_login.py 钉着；这里的样例照它的输出抄来（虚构路径）。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { runDesktop, leftoverChinese } from "./desktop-dom.mjs";

const readme = readFileSync(new URL("../README.md", import.meta.url), "utf8").replace(/\r\n/g, "\n");
const style = readFileSync(new URL("../web/style.css", import.meta.url), "utf8");

const ROOM = "https://www.tiktok.com/@luna_demo/live";
const PATHS = ["/opt/homebrew/bin/python3.14", "/Users/demo user/miniconda3/bin/python3.13"];

const TEXT = {
  zh: {
    network: (m) => "本机连不上 www.tiktok.com，已 " + m + " 分钟没有监听，网络恢复后自动重连",
    resolving: "正在解析直播流地址…",
    // app/pipeline.py browser_only_message：Safari 被系统拒绝读取 + Chrome 没登录（专家量到中文 5 行的那种）
    blocked: "TikTok 不把这个直播间的流地址给程序（代码 4003110），已自动重试 2 次，原因 TikTok 不说明；有时过一会儿再点「开始翻译」就好，有时整场都不给。\n" +
      "借用浏览器里的 TikTok 登录时观察到——Safari：系统拒绝读取；Chrome：能读取，没有 TikTok 登录 cookie（sessionid 等）。\n" +
      "系统不允许本程序读取浏览器数据。到「系统设置」→「隐私与安全性」→「完全磁盘访问权限」，点「+」，在选文件的窗口里按 ⌘⇧G，粘贴下面的路径后回车、点「打开」，再把它的开关打开（每个路径各做一遍）：\n" +
      "「/opt/homebrew/bin/python3.14」\n「/Users/demo user/miniconda3/bin/python3.13」\n" +
      "列表里显示的名字是 python3.14、python3.13，不是本程序的名字。如果列表里已经有这一项、开关也开着，先选中它点「−」删掉，再按上面的步骤加一次。用 Start.command 启动的话把「终端」也加进去。然后完全退出程序再打开。\n" +
      "这项权限给的是 Python 解释器，不只是本程序：用它运行的任何脚本都能读取这台电脑上受保护的数据。不再需要借用浏览器里的 TikTok 登录时，可以回到同一个列表把它的开关关掉。在 Chrome（或 Safari）里登录 TikTok 后再试。想现在就看：把直播间链接和浏览器里的 .flv 地址一起粘进来（中间空格隔开），一次约两周有效。",
    lead: "TikTok 不把这个直播间的流地址给程序（代码 4003110），已自动重试 2 次，原因 TikTok 不说明；有时过一会儿再点「开始翻译」就好，有时整场都不给。",
    // 没登录的那种：一段话，页面上 2 行
    short: "TikTok 不把这个直播间的流地址给程序（代码 4003110），已自动重试 2 次，原因 TikTok 不说明；有时过一会儿再点「开始翻译」就好，有时整场都不给。借用浏览器里的 TikTok 登录时观察到——Chrome：能读取，没有 TikTok 登录 cookie（sessionid 等）。在 Chrome（或 Safari）里登录 TikTok 后再试。想现在就看：把直播间链接和浏览器里的 .flv 地址一起粘进来（中间空格隔开），一次约两周有效。",
    more: "显示全部步骤", less: "收起", copy: "复制", copied: "已复制", selected: "已选中，按 ⌘C",
    zipAnchor: "zh-updating-zip", howTo: "怎么更新",
  },
  en: {
    network: (m) => "This computer can’t reach www.tiktok.com. Nothing has been monitored for " + m +
      " min. The app reconnects when the network is back.",
    resolving: "Getting the stream URL…",
    blocked: "TikTok didn’t provide a stream URL for this live stream (code 4003110). Retried 2 times. TikTok doesn’t say why. Sometimes it works if you click Start again a little later. Sometimes it doesn’t work for the whole stream.\n" +
      "Looked for a TikTok sign-in in your browsers. Safari: macOS didn’t allow access; Chrome: readable, but no TikTok sign-in cookie (sessionid or similar).\n" +
      "macOS didn’t allow the app to read browser data. Go to System Settings > Privacy & Security > Full Disk Access and click +. In the file window, press ⌘⇧G, paste the path below, press Return, click Open, and turn on its switch (do this for each path):\n" +
      "“/opt/homebrew/bin/python3.14”\n“/Users/demo user/miniconda3/bin/python3.13”\n" +
      "The list shows python3.14, python3.13, not the app’s name. If it’s already in the list with its switch on, select it, click −, and add it again with the steps above. If you start the app with Start.command, add Terminal too. Then quit the app completely and open it again.\n" +
      "This permission goes to the Python interpreter, not just this app, so any script it runs can read protected data on this computer. When you no longer need the TikTok sign-in from your browser, you can turn its switch off in the same list. Sign in to TikTok in Chrome (or Safari), then try again. To watch now, paste the live link and the .flv URL from your browser together, separated by a space. A .flv URL usually works for about two weeks.",
    lead: "TikTok didn’t provide a stream URL for this live stream (code 4003110). Retried 2 times. TikTok doesn’t say why. Sometimes it works if you click Start again a little later. Sometimes it doesn’t work for the whole stream.",
    short: "TikTok didn’t provide a stream URL for this live stream (code 4003110). Retried 2 times. TikTok doesn’t say why. Sometimes it works if you click Start again a little later. Sometimes it doesn’t work for the whole stream. Looked for a TikTok sign-in in your browsers. Chrome: readable, but no TikTok sign-in cookie (sessionid or similar). Sign in to TikTok in Chrome (or Safari), then try again. To watch now, paste the live link and the .flv URL from your browser together, separated by a space. A .flv URL usually works for about two weeks.",
    more: "Show All Steps", less: "Hide", copy: "Copy", copied: "Copied", selected: "Selected. Press ⌘C",
    zipAnchor: "en-updating-zip", howTo: "How to Update",
  },
};

// 要复制到新文件夹的那几样（web/app.js showZipKeepNote；README「更新（ZIP 安装）」一节列的是同一份）
const KEEP = ["settings.json", "banned_terms.txt", "glossary.txt", "logs/", "transcripts/", "profiles/", "brands/",
              "comment_guard/local/", "comment_guard/marked/"];

const shown = (el) => !el.classList.contains("hidden");

function hello(page, extra) {
  page.push({ type: "hello", config: Object.assign({ status: { state: "idle", detail: "" }, room_url: ROOM,
    version: "1.2.3" }, extra || {}) });
}
const status = (page, state, detail, more) => page.push(Object.assign({ type: "status", state, detail }, more || {}));
const incident = (page, id, level, text) => page.push({ type: "incident", id, level, text });

// 横幅里每一块：{ kind: "line" | "path", text, rest }。行的 text 只算它自己的字（第一行后面可能接着按钮）
function blocks(page) {
  return page.el("status-banner").children.map((el) => ({
    kind: el.cls.includes("banner-path") ? "path" : "line",
    text: el.cls.includes("banner-path") ? el.children[0].textContent
      : el.childNodes.filter((n) => n.nodeType === 3).map((n) => n.textContent).join(""),
    rest: el.cls.includes("banner-rest"),
    el,
  }));
}
// 「显示全部步骤」按钮（接在第一行的字后面）
const moreBtn = (page) => page.el("status-banner").querySelectorAll(".banner-more")[0] || null;
// 折叠时看得见的：.folded 下 .banner-rest 不显示（style.css）
const visible = (page) => blocks(page).filter((b) => !(page.el("status-banner").cls.includes("folded") && b.rest));

for (const lang of ["zh", "en"]) {
  const W = TEXT[lang];

  // ---- U6 ----------------------------------------------------------------------------------------------
  test(lang + "：断网时同一句话只出现一次——持续提示在顶部，状态横幅收起，胶囊照常（U6）", () => {
    const page = runDesktop(lang);
    hello(page, { status: { state: "live", detail: "" } });
    const banner = page.el("status-banner");
    // 后端的顺序：先挂持续提示，再发 status（同一句）
    incident(page, "session:network", "error", W.network(1));
    status(page, "connecting", W.network(1));
    const bar = page.el("incident-bar");
    assert.equal(shown(bar), true);
    assert.equal(bar.textContent, W.network(1));
    assert.equal(shown(banner), false, "横幅不再重复同一句");
    assert.equal(page.el("status-dot").className, "dot connecting", "状态胶囊照常");
    // 每次重试两边都换成新的时长：仍只出现一次（中间那一下也不把旧的一句放出来）
    incident(page, "session:network", "error", W.network(2));
    assert.equal(shown(banner), false, "持续提示更新了文字，紧跟着的 status 还没到：横幅不闪出旧的那句");
    status(page, "connecting", W.network(2));
    assert.equal(shown(banner), false);
    // 持续提示撤掉：横幅露出来（和改之前一样，直到下一条 status）
    incident(page, "session:network", "clear");
    assert.equal(shown(bar), false);
    assert.equal(shown(banner), true, "持续提示撤掉后横幅出现");
    assert.equal(banner.textContent, W.network(2));
    status(page, "connecting", W.resolving);
    assert.equal(shown(banner), true);
    assert.equal(banner.textContent, W.resolving);
    if (lang === "en") assert.deepEqual(leftoverChinese(page), []);
  });

  test(lang + "：先有 status、后挂上同一句的持续提示也只出现一次；文字不一样的照常两处都显示（U6）", () => {
    const page = runDesktop(lang);
    hello(page, { status: { state: "live", detail: "" } });
    const banner = page.el("status-banner");
    status(page, "connecting", W.network(3));
    assert.equal(shown(banner), true);
    incident(page, "session:network", "error", "🔴 " + W.network(3));          // 开头的状态 emoji 不算
    assert.equal(shown(banner), false);
    status(page, "connecting", W.resolving);
    assert.equal(shown(banner), true, "说的不是同一句：横幅照常");
    assert.equal(shown(page.el("incident-bar")), true);
    // 重连（hello）时持续提示和状态一起回放：照样只出现一次
    hello(page, { status: { state: "connecting", detail: W.network(4) },
                  incidents: { "session:network": { id: "session:network", level: "error", text: W.network(4) } } });
    assert.equal(shown(banner), false);
    assert.equal(page.el("incident-bar").textContent, W.network(4));
  });

  // ---- U10 ---------------------------------------------------------------------------------------------
  test(lang + "：「接口不给流地址」超过 3 行的那种默认只露第一行和「显示全部步骤」，能展开、收起（U10）", () => {
    const page = runDesktop(lang);
    hello(page);
    status(page, "error", W.blocked, { paths: PATHS });
    const banner = page.el("status-banner");
    assert.equal(shown(banner), true);
    assert.equal(banner.cls.includes("folded"), true);
    const seen = visible(page);
    assert.deepEqual(seen.map((b) => b.kind), ["line"]);
    assert.equal(seen[0].text, W.lead, "第一行就是 4003110 那句说明，一个字都没折掉");
    const more = moreBtn(page);
    assert.ok(seen[0].el.children.includes(more), "按钮接在第一行的字后面（行内），折着时只占这一行");
    assert.equal(more.textContent, W.more);
    assert.equal(more.getAttribute("aria-expanded"), "false");
    // 展开：全部步骤都在，路径是两行代码行
    more.click();
    assert.equal(banner.cls.includes("folded"), false);
    assert.equal(more.textContent, W.less);
    assert.equal(more.getAttribute("aria-expanded"), "true");
    const all = visible(page);
    const lines = W.blocked.split("\n");
    assert.equal(all.length, lines.length, "每行一块");
    assert.ok(all[0].el.children.includes(more), "展开后按钮不挪位置");
    assert.deepEqual(all.filter((b) => b.kind === "line").map((b) => b.text),
                     lines.filter((l) => !PATHS.some((p) => l.includes(p))));
    assert.deepEqual(all.filter((b) => b.kind === "path").map((b) => b.text), PATHS);
    // 收起
    more.click();
    assert.equal(banner.cls.includes("folded"), true);
    assert.equal(more.textContent, W.more);
    // 同一句重发（比如重连回放）不把中控展开的那一下收回去；换了一句就重新折起来
    more.click();
    status(page, "error", W.blocked, { paths: PATHS });
    assert.equal(banner.cls.includes("folded"), false);
    status(page, "error", W.blocked.replace("2", "3"), { paths: PATHS });
    assert.equal(banner.cls.includes("folded"), true);
    if (lang === "en") assert.deepEqual(leftoverChinese(page), []);
  });

  test(lang + "：一两行的变体（没登录、借到登录仍不给）不折叠，没有「显示全部步骤」（U10）", () => {
    const page = runDesktop(lang);
    hello(page);
    status(page, "error", W.short);
    const banner = page.el("status-banner");
    assert.equal(banner.cls.includes("folded"), false);
    assert.deepEqual(blocks(page).map((b) => b.kind), ["line"]);
    assert.equal(banner.textContent, W.short);
    assert.equal(moreBtn(page), null);
    // 先来一条会折的，再来一条不会折的：折叠的样子跟着清掉
    status(page, "error", W.blocked, { paths: PATHS });
    assert.equal(banner.cls.includes("folded"), true);
    status(page, "error", W.short);
    assert.equal(banner.cls.includes("folded"), false);
    assert.equal(moreBtn(page), null);
  });

  // ---- I8 ----------------------------------------------------------------------------------------------
  test(lang + "：status 带 paths 时每条路径一行，各有「复制」，带空格的路径整条复制（I8）", async () => {
    const page = runDesktop(lang);
    const copied = [];
    page.sandbox.navigator.clipboard = { writeText: (t) => { copied.push(t); return Promise.resolve(); } };
    hello(page);
    status(page, "error", W.blocked, { paths: PATHS });
    const rows = blocks(page).filter((b) => b.kind === "path");
    assert.equal(rows.length, 2);
    for (const [i, row] of rows.entries()) {
      const [code, btn] = row.el.children;
      assert.equal(code.tagName, "CODE");
      assert.equal(code.textContent, PATHS[i], "整条路径，没有引号");
      assert.equal(code.getAttribute("translate"), "no");
      assert.equal(btn.textContent, W.copy);
      assert.ok(row.el.cls.includes("fix-command"), "复用 #fix-command 的样子");
      btn.click();
      await Promise.resolve();
      assert.equal(copied[i], PATHS[i]);
      assert.equal(btn.textContent, W.copied);
    }
    assert.equal(copied[1], "/Users/demo user/miniconda3/bin/python3.13", "带空格的路径一个字不少");
    // 文字里那两行换成了代码行：同一个路径在横幅里只出现一次
    const text = blocks(page).filter((b) => b.kind === "line").map((b) => b.text).join("\n");
    for (const p of PATHS) assert.equal(text.includes(p), false, p);
    // 剪贴板不给用：至少帮着选中
    delete page.sandbox.navigator.clipboard;
    const btn = blocks(page).filter((b) => b.kind === "path")[0].el.children[1];
    btn.click();
    assert.equal(btn.textContent, W.selected);
    if (lang === "en") assert.deepEqual(leftoverChinese(page), []);
  });

  test(lang + "：没有 paths 就不从文字里认路径；paths 里有、文字里没有的路径接在最后（I8）", () => {
    const page = runDesktop(lang);
    hello(page);
    status(page, "error", W.blocked);                         // 同一段文字，没带 paths
    assert.equal(blocks(page).filter((b) => b.kind === "path").length, 0, "不按「以 / 开头」之类的规则猜");
    const lines = blocks(page).filter((b) => b.kind === "line").map((b) => b.text);
    assert.ok(lines.includes(lang === "zh" ? "「/opt/homebrew/bin/python3.14」" : "“/opt/homebrew/bin/python3.14”"));
    // 带了 paths、文字是别的（比如以后改了措辞）：路径照样有可复制的一行，排在文字后面
    status(page, "error", W.lead, { paths: [PATHS[1]] });
    const got = blocks(page);
    assert.deepEqual(got.map((b) => b.kind), ["line", "path"]);
    assert.equal(got[1].text, PATHS[1]);
  });

  // ---- I2 ----------------------------------------------------------------------------------------------
  test(lang + "：ZIP 安装的更新条写明要复制到新文件夹的几样，并链到 README 里同一份清单（I2）", () => {
    const page = runDesktop(lang);
    hello(page);
    page.push({ type: "update_available", version: "9.9.9", can_auto: false, url: "https://example.invalid/dl" });
    const bar = page.el("update-bar");
    const note = bar.children.find((el) => el.cls.includes("update-zip-note"));
    assert.ok(note, "ZIP 安装多一行说明");
    assert.equal(shown(note), true);
    for (const name of KEEP) assert.ok(note.textContent.includes(name), name);
    assert.ok(note.textContent.includes(lang === "zh" ? "不要直接替换旧文件夹" : "Don’t replace the old folder"));
    const link = note.children.find((el) => el.tagName === "A");
    assert.equal(link.textContent, W.howTo);
    assert.equal(link.href, "https://github.com/EM917/tiktok-live-translator#" + W.zipAnchor);
    assert.equal(link.target, "_blank");
    // README 里真有这个锚点，那一节列的是同一份清单，并写明审计日志在 logs/
    const at = readme.indexOf('<a id="' + W.zipAnchor + '"></a>');
    assert.ok(at > 0, "README 缺锚点 " + W.zipAnchor);
    const section = readme.slice(at, readme.indexOf("\n<a id=", at + 10));
    for (const name of KEEP) assert.ok(section.includes("`" + name.replace(/\/$/, "") ), name + " 不在 README 那一节");
    assert.ok(section.includes(lang === "zh" ? "审计日志" : "audit log"));
    // 能一键更新的安装：这一行收起
    page.push({ type: "update_available", version: "9.9.10", can_auto: true, url: "https://example.invalid/rel" });
    assert.equal(shown(note), false);
    assert.equal(bar.children.filter((el) => el.cls.includes("update-zip-note")).length, 1, "不重复建");
    if (lang === "en") assert.deepEqual(leftoverChinese(page), []);
  });
}

test("样式：折叠时只藏 .banner-rest；路径行在横幅里有上下空；ZIP 说明独占一行", () => {
  assert.match(style, /\.status-banner\.folded \.banner-rest \{ display: none; \}/);
  assert.match(style, /\.status-banner \.banner-path \{[^}]*margin: 6px auto/);
  assert.match(style, /\.update-zip-note \{[^}]*flex-basis: 100%/);
  // 「完全磁盘访问权限」那几步分了行：自检那一行和「没有读到登录」那条持续提示都照着换行显示
  assert.match(style, /\.sc-item \.sc-fix \{[^}]*white-space: pre-line/);
  assert.match(style, /\.incident > span \{ white-space: pre-line; \}/);
});
