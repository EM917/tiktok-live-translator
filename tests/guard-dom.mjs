// 弹幕留意（web/guard.js）渲染函数的假 DOM，tests/guard.test.mjs 和 guard.en.test.mjs 共用。
// 不是测试文件（不以 .test.mjs 结尾，`node --test tests/*.test.mjs` 不会把它当用例跑）。
// 只实现 guard.js 的渲染函数用到的接口：createElement / createTextNode、appendChild / insertBefore /
// removeChild、firstChild / parentNode / children、className / classList、setAttribute / getAttribute、
// dataset、textContent、addEventListener / click、querySelector(".类" 或 "标签")。不认识的选择器直接抛错，免得静默走偏。

export class FakeText {
  constructor(v) { this.nodeType = 3; this.nodeValue = String(v); this.parentNode = null; }
  get textContent() { return this.nodeValue; }
}

export class FakeEl {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase(); this.nodeType = 1; this.childNodes = []; this.parentNode = null;
    this.attrs = new Map(); this.cls = []; this.dataset = {}; this.listeners = {};
  }
  get className() { return this.cls.join(" "); }
  set className(v) { this.cls = String(v).split(/\s+/).filter(Boolean); }
  get classList() {
    const el = this;
    const set = (c, on) => { el.cls = el.cls.filter((x) => x !== c); if (on) el.cls.push(c); };
    return {
      add: (...cs) => cs.forEach((c) => set(c, true)),
      remove: (...cs) => cs.forEach((c) => set(c, false)),
      toggle: (c, on) => { const v = on === undefined ? !el.cls.includes(c) : !!on; set(c, v); return v; },
      contains: (c) => el.cls.includes(c),
    };
  }
  setAttribute(k, v) { this.attrs.set(k, String(v)); }
  getAttribute(k) { return this.attrs.has(k) ? this.attrs.get(k) : null; }
  hasAttribute(k) { return this.attrs.has(k); }
  appendChild(c) { return this.insertBefore(c, null); }
  insertBefore(c, ref) {
    if (c.parentNode) c.parentNode.removeChild(c);
    const i = ref ? this.childNodes.indexOf(ref) : -1;
    if (i === -1) this.childNodes.push(c); else this.childNodes.splice(i, 0, c);
    c.parentNode = this;
    return c;
  }
  removeChild(c) { this.childNodes = this.childNodes.filter((n) => n !== c); c.parentNode = null; return c; }
  get firstChild() { return this.childNodes[0] || null; }
  get children() { return this.childNodes.filter((n) => n.nodeType === 1); }
  get textContent() { return this.childNodes.map((n) => n.textContent).join(""); }
  set textContent(v) { this.childNodes = []; if (v != null && String(v)) this.appendChild(new FakeText(v)); }
  addEventListener(type, cb) { (this.listeners[type] = this.listeners[type] || []).push(cb); }
  click() { (this.listeners.click || []).slice().forEach((cb) => cb({ target: this })); }
  descendants() { return this.children.flatMap((c) => [c, ...c.descendants()]); }
  querySelectorAll(sel) {
    let m;
    if ((m = /^\.([\w-]+)$/.exec(sel))) return this.descendants().filter((el) => el.cls.includes(m[1]));
    if ((m = /^([a-z]+)$/.exec(sel))) return this.descendants().filter((el) => el.tagName === m[1].toUpperCase());
    throw new Error("假 DOM 不认识这个选择器：" + sel);
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}

export function makeDoc() {
  return { createElement: (tag) => new FakeEl(tag), createTextNode: (v) => new FakeText(v) };
}

/** 一条 .cmt-item，结构同 app.js renderComment：头（昵称 + 时间）、译文行、原文行。 */
export function makeItem(doc, user = "Ana", zh = "你好") {
  const item = doc.createElement("div");
  item.className = "cmt-item";
  const head = doc.createElement("div");
  head.className = "cmt-head";
  const name = doc.createElement("span");
  name.textContent = user;
  head.appendChild(name);
  head.appendChild(doc.createTextNode(" 12:00:00"));
  item.appendChild(head);
  const zhEl = doc.createElement("div");
  zhEl.className = "cmt-zh";
  zhEl.textContent = zh;
  item.appendChild(zhEl);
  const orig = doc.createElement("div");
  orig.className = "cmt-orig";
  orig.textContent = "hola";
  item.appendChild(orig);
  return item;
}

/** 汉字（含中文标点和全角符号），同 tests/app.en.test.mjs / tests/i18n_dom/scan.js。 */
export const CJK = /[　-〿぀-ヿ㐀-䶿一-鿿豈-﫿＀-￯]/;

/** 一棵子树里所有节点的全部可见文字和界面属性（title / aria-label / placeholder），用来查英文页有没有剩下中文。 */
export function uiTexts(el, skipData = true) {
  const out = [];
  const isData = (n) => n.getAttribute("translate") === "no" || n.cls.includes("i18n-data");
  (function walk(n) {
    if (skipData && isData(n)) return;
    for (const a of ["title", "aria-label", "placeholder"]) if (n.attrs.has(a)) out.push(n.attrs.get(a));
    for (const c of n.childNodes) {
      if (c.nodeType === 3) out.push(c.nodeValue); else walk(c);
    }
  })(el);
  return out;
}
