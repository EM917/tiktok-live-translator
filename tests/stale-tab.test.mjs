// 一键更新重启之后的旧标签页（专家审查 F8 (c)，web/app.js reloadForBuild）。
//
// 浏览器模式下一键更新重启，以前新进程每次再开一个新标签页，点更新的那个旧标签页也重连上来：新旧两个控制页
// 同时连着，旧的那页跑的还是旧 JS——hello 里的 version 只用来改页脚文字，没人比对。现在服务端启动时按 web/
// 的内容算出 config.build（app/server.py web_build），页面记下第一次连上时的值，之后哪一次 hello 里的不一样
// 就重新载入；重启的新进程在浏览器模式下带 --no-open，不再另开标签页（app/updater.py relaunch_argv，
// tests/test_updater_relaunch.py）。页面在 node:vm 里真跑一遍（脚手架见 tests/desktop-dom.mjs）。
import { test } from "node:test";
import assert from "node:assert/strict";

import { runDesktop } from "./desktop-dom.mjs";

const ROOM = "https://www.tiktok.com/@luna_demo/live";

function page() {
  const p = runDesktop("zh");
  p.reloads = 0;
  p.sandbox.location.reload = () => { p.reloads += 1; };
  p.hello = (config) => p.push({ type: "hello", config: Object.assign({
    status: { state: "idle", detail: "" }, room_url: ROOM }, config) });
  return p;
}

test("第一次连上记下 build；重连时 build 没变就照常画，不重新载入", () => {
  const p = page();
  p.hello({ build: "aaaaaaaaaaaa", version: "1.0.0" });
  p.hello({ build: "aaaaaaaaaaaa", version: "1.0.0" });
  assert.equal(p.reloads, 0);
  assert.match(p.el("app-version").textContent, /v1\.0\.0/);
});

test("重连上来 build 变了（服务端换了一套页面文件）：重新载入，这条 hello 不再往下画", () => {
  const p = page();
  p.hello({ build: "aaaaaaaaaaaa", version: "1.0.0" });
  p.hello({ build: "bbbbbbbbbbbb", version: "2.0.0" });
  assert.equal(p.reloads, 1);
  assert.doesNotMatch(p.el("app-version").textContent, /2\.0\.0/, "页面马上就换掉了：不在旧 JS 里画新版本的 hello");
});

test("没有 build 的 hello（老的服务端）不比对，也不占「第一次」", () => {
  const p = page();
  p.hello({ version: "1.0.0" });
  p.hello({ build: "" });
  p.hello({ build: "aaaaaaaaaaaa" });
  assert.equal(p.reloads, 0, "第一个真正的 build 才算第一次");
  p.hello({});
  assert.equal(p.reloads, 0, "某一次没带 build：不当成变了");
  p.hello({ build: "cccccccccccc" });
  assert.equal(p.reloads, 1);
});

test("重连时补发的「开始」先发出去，再重新载入", () => {
  // 「开始」发出去了却没有回执（半死的连接上发进了黑洞），随后重连：hello 里先补发，再按 build 重新载入
  const p = page();
  p.hello({ build: "aaaaaaaaaaaa" });
  p.el("room-input").value = ROOM;
  p.el("start-btn").click();
  const starts = () => p.sent.filter((m) => m.type === "start").length;
  assert.equal(starts(), 1);
  p.hello({ build: "bbbbbbbbbbbb" });
  assert.equal(starts(), 2, "补发的「开始」不因为重新载入而丢");
  assert.equal(p.reloads, 1);
});
