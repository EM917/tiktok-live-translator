// 弹幕留意前端测试共用的数据（虚构的主播、昵称、账号）。不是测试文件。

// 19 位的账号：超过 2^53，转成数字后这两个会变成同一个数
export const UID_A = "7234567890123456789";
export const UID_B = "7234567890123456790";

export const ALL_CATS = ["insult", "threat", "sexual", "accuse_claim", "accuse_call", "accuse_report",
  "flood_user", "flood_repeat", "flood_brigade", "promo_link", "promo_contact", "promo_pull", "promo_scam"];
export const AMBER_CODES = ["maybe_question", "grievance", "quote", "accumulated", "question_link", "pending",
  "self", "review", "brigade"];
export const RV_CODES = ["pending", "agree", "question", "complaint", "friendly", "neutral", "unsure", "skipped"];
export const STATUS_CODES = ["off", "env_off", "rules", "starting", "claude", "paused", "tripped", "no_key",
  "no_sdk", "no_review", "key_rejected", "offline", "cooldown"];

export const STATE = { enabled: true, backend: "none", status: "rules", since: 1, trip: null,
  rules: { count: 510, warnings: 0 }, n_marked: 0, unreviewed: 0, weak: false, streamer: "luna_demo" };

export const comment = (over = {}) => Object.assign(
  { type: "comment", id: "c1", user: "Ana", text: "hola", ts: 1760000000, uid: UID_A, uid_ok: true,
    handle: "ana_demo" }, over);

export const RED = { lvl: "alert", amb: null, cats: ["threat"], sub: "threat", tier: "red", src: "rule",
  rv: null, ev: "g1", why: ["insult/threat:te voy a matar"] };
export const AMBER = { lvl: "amber", amb: "maybe_question", cats: ["accuse_claim"], sub: "assert", tier: "red",
  src: "rule", rv: null, ev: "g2", why: ["accuse/assert:es falso"] };
export const NOTE = { lvl: "note", amb: null, cats: ["insult"], sub: "medium", tier: "clue", src: "rule",
  rv: null, ev: null, why: [] };

export const entry = (over = {}) => Object.assign(
  { keys: [UID_A, "s:MS4wLjAB", "h:ana_demo"], nick: "Ana", handle: "ana_demo", aka: [], by: "auto",
    cat: "threat", first: 1760000000, last_seen: 1760000100, hits: 2 }, over);

export const event = (over = {}) => Object.assign(
  { id: "g1", kind: "comment", cat: "threat", lvl: "alert", amb: null, n: 1, accounts: [], window_sec: 60,
    first_ts: 1760000000, ts: 1760000010, rv: null,
    sample: { cid: "c1", user: "Ana", text: "te voy a matar", uid: UID_A, uid_src: "id", handle: "ana_demo" } }, over);
