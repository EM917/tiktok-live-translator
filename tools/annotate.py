"""标注界面：一个把「人工确认 target」当数据生产的本地工具，不只是网页。

设计铁律（缺一条都会在几百条标注之后追悔莫及）：
  * 每个动作**立即**落盘，且原子化（临时文件 → fsync → rename）。浏览器崩、
    电脑睡眠，重开页面直接「57/401 已完成，从 58 继续」。
  * accept_local / accept_strong 的 target 由**服务端**从队列里原样复制，
    客户端传什么都不认——「接受参考」和「人工翻译」绝不混类；改一个字就是
    manual。这个区分以后能回答「1.8B 有多少比例可直接当 gold」。
  * 每条结果带完整 provenance：queue_id / cluster / session / seq / 来源
    引擎 / 时间戳 / 指南 hash / 代码 commit / 队列文件 hash——未来任何一条
    训练 pair 都能回答「来自哪场直播、当时看到什么参考、按哪版指南、
    在哪个代码版本标的」。
  * 盲复标（--relabel）：从已标前 100 条固定种子抽 20 条、重新洗序、隐去
    原 id 与第一次的 action/target；--compare 自动对比一致性。测的是标注
    流程，不是记忆力。

用法：
    python3 tools/annotate.py [--logs DIR] [--port 8788]    # 标注
    python3 tools/annotate.py --relabel                     # 生成并进入盲复标
    python3 tools/annotate.py --compare                     # 对比两次标注
"""
import argparse
import hashlib
import json
import os
import random
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, ".")

from app import provenance                                    # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ACTIONS = ("accept_local", "accept_strong", "manual", "asr_garbage",
           "context_required", "profile_only", "skip")


def file_hash(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]
    except OSError:
        return "?"


def latest_queue(log_dir):
    files = sorted(Path(log_dir).glob("annotation-queue-*.jsonl"))
    return files[-1] if files else None


def load_jsonl(path):
    rows = []
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue          # 崩溃可能留半行，跳过
    except OSError:
        pass
    return rows


def results_path(queue_path, relabel=False):
    stem = Path(queue_path).stem.replace("annotation-queue-", "")
    kind = "annotation-relabel-" if relabel else "annotation-results-"
    return Path(queue_path).parent / (kind + stem + ".jsonl")


def effective_results(rows):
    """同一 queue_id 允许重标，最后一条生效。"""
    out = {}
    for r in rows:
        out[r["queue_id"]] = r
    return out


def make_record(row, action, target, provenance_stamp, relabel_batch=None):
    """由服务端裁决的结果记录。accept_* 的 target 从队列原样复制；
    manual 必须带非空 target；其余动作 target 为 None。"""
    if action not in ACTIONS:
        raise ValueError("未知动作: " + str(action))
    adopted_engine = None
    if action == "accept_local":
        if not row.get("fast"):
            raise ValueError("本地参考被隔离或缺失，不能 accept_local")
        target = row["fast"]
        adopted_engine = row.get("fast_engine")
    elif action == "accept_strong":
        if not row.get("strong"):
            raise ValueError("强译参考被隔离或缺失，不能 accept_strong")
        target = row["strong"]
        adopted_engine = row.get("strong_engine")
    elif action == "manual":
        if not (target or "").strip():
            raise ValueError("manual 必须填写人工 target")
        target = target.strip()
    else:
        target = None
    # manual 与参考逐字相同：不拦（复制后确认无需改也是合法的人工确认），
    # 但打上标记——「1.8B 可直接当 gold 的比例」这个指标要能把这类和
    # 真正从零人工翻译区分开
    equals_ref = None
    if action == "manual":
        if target == (row.get("fast") or "").strip():
            equals_ref = "fast"
        elif target == (row.get("strong") or "").strip():
            equals_ref = "strong"
    return dict(provenance_stamp,
                manual_equals_reference=equals_ref,
                queue_id=row["id"], cluster_id=row.get("cluster_id"),
                session=row["session"], seq=row["seq"],
                streamer=row["streamer"], bucket=row["bucket"],
                src=row["src"], action=action, target=target,
                adopted_engine=adopted_engine,
                fast_engine=row.get("fast_engine"),
                strong_engine=row.get("strong_engine"),
                at=datetime.now().isoformat(timespec="seconds"),
                relabel_batch=relabel_batch)


def save_results_atomic(path, records):
    """整文件重写：唯一临时文件 → fsync → rename。几百条的量级，原子性比
    追加省的那点 IO 值钱得多。临时文件名必须唯一——固定名字时两个并发写
    会互相把对方的 tmp 挪走（调用方还要加锁，这里只保证单次写不半途）。"""
    import tempfile

    fd, tmp = tempfile.mkstemp(dir=str(Path(path).parent),
                               prefix=Path(path).name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def build_relabel_batch(results, n=20, seed=20260829, pool=100):
    """盲复标批：已标前 pool 条里固定种子抽 n 条、重新洗序。
    输出行不含第一次的 action/target/id（用 r1..rN 的盲 id）。"""
    done = [r for r in results if r.get("action")]
    base = done[:pool]
    rng = random.Random(seed)
    chosen = rng.sample(base, min(n, len(base)))
    rng.shuffle(chosen)
    return [{"blind_id": "r%d" % (i + 1), "queue_id": r["queue_id"]}
            for i, r in enumerate(chosen)]


def compare_relabel(first, second):
    """两次标注的一致性。first/second: queue_id -> record。"""
    common = sorted(set(first) & set(second))
    stats = {"n": len(common), "action_agree": 0, "target_exact": 0,
             "manual_vs_accept": 0, "gate_disagree": 0, "diffs": []}
    # skip 刻意不算门类：它的语义是「没想好/近重复」，不是一次判断；
    # 两次之间 skip↔别的动作属于正常犹豫，不该按判断分歧统计
    gates = {"context_required", "profile_only", "asr_garbage"}
    for qid in common:
        a, b = first[qid], second[qid]
        same_action = a["action"] == b["action"]
        if same_action:
            stats["action_agree"] += 1
        if (a.get("target") or "") == (b.get("target") or "") and a.get("target"):
            stats["target_exact"] += 1
        pair = {a["action"], b["action"]}
        if not same_action and "manual" in pair and \
                pair & {"accept_local", "accept_strong"}:
            stats["manual_vs_accept"] += 1
        if not same_action and pair & gates:
            stats["gate_disagree"] += 1
        if not same_action or (a.get("target") != b.get("target")):
            stats["diffs"].append({"queue_id": qid,
                                   "first": [a["action"], a.get("target")],
                                   "second": [b["action"], b.get("target")]})
    return stats


PAGE = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>标注 · TikTok 同传</title><style>
body{font-family:system-ui;margin:0;background:#111;color:#eee}
main{max-width:880px;margin:0 auto;padding:16px}
.ctx{color:#888;font-size:14px;margin:2px 0}
.src{font-size:22px;margin:10px 0;padding:12px;background:#1c1c28;border-radius:8px}
.ref{margin:6px 0;padding:10px;background:#191919;border-radius:8px;font-size:16px}
.ref .tag{color:#7a7;font-size:12px;margin-right:8px}
.ref .withheld{color:#c66;font-size:13px}
.ref a{color:#69c;font-size:12px;margin-left:8px;cursor:pointer}
textarea{width:100%;height:64px;font-size:16px;background:#181822;color:#eee;
border:1px solid #333;border-radius:8px;padding:8px;box-sizing:border-box}
.acts{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}
.acts button{padding:8px 12px;border-radius:8px;border:1px solid #444;
background:#222;color:#eee;cursor:pointer;font-size:14px}
.acts button.primary{background:#2a4}
.acts button:disabled{opacity:.35;cursor:not-allowed}
.meta{color:#777;font-size:13px}.nav{margin:8px 0;display:flex;gap:10px;align-items:center}
.done{color:#5c5}.progress{font-size:15px}
kbd{background:#333;border-radius:4px;padding:0 5px;font-size:12px}
</style></head><body><main>
<div class="nav"><button onclick="move(-1)">← 上一条</button>
<button onclick="move(1)">下一条 →</button>
<span class="progress" id="progress"></span>
<span class="meta" id="mode"></span></div>
<div class="ctx" id="prev"></div>
<div class="src" id="src"></div>
<div class="ctx" id="next"></div>
<div class="ref" id="fast"></div>
<div class="ref" id="strong"></div>
<textarea id="target" placeholder="manual 的人工 target 写这里（accept 按钮不读这个框）"></textarea>
<div class="acts" id="acts"></div>
<div class="meta" id="meta"></div>
<div class="meta">快捷键：<kbd>1</kbd>-<kbd>7</kbd> 对应按钮 · <kbd>←</kbd><kbd>→</kbd> 翻条</div>
</main><script>
const ACTIONS=[["accept_local","1 接受本地"],["accept_strong","2 接受强译"],
["manual","3 manual"],["asr_garbage","4 ASR坏句"],["context_required","5 需上下文"],
["profile_only","6 主播专属"],["skip","7 跳过"]];
let state=null,idx=0;
async function load(){state=await (await fetch("api/state")).json();
idx=state.rows.findIndex(r=>!r.done);if(idx<0)idx=state.rows.length-1;render();}
function esc(s){const d=document.createElement("div");d.textContent=s??"";return d.innerHTML}
function render(){const r=state.rows[idx];
document.getElementById("progress").textContent=
  `${state.rows.filter(x=>x.done).length} / ${state.rows.length} 已完成`;
document.getElementById("mode").textContent=state.relabel?"盲复标模式":"";
document.getElementById("prev").innerHTML="↑ "+esc(r.context_prev);
document.getElementById("next").innerHTML="↓ "+esc(r.context_next);
document.getElementById("src").innerHTML=esc(r.src);
refBox("fast","本地 "+(r.fast_engine||""),r.fast,r.fast_withheld);
refBox("strong","强译 "+(r.strong_engine||""),r.strong,r.strong_withheld);
document.getElementById("target").value="";
// 盲复标：只显示盲 id，绝不显示真实编号（编号眼熟就不盲了）
const shown=state.relabel?r.display_id:("#"+r.id);
document.getElementById("meta").textContent=
  `${shown} · ${r.bucket} · ${r.streamer} · ${(r.families||[]).join("+")}`+
  (r.done?` · 已标: ${r.done}`:"");
const acts=document.getElementById("acts");acts.innerHTML="";
for(const [a,label] of ACTIONS){const b=document.createElement("button");
b.textContent=label;b.className=a.startsWith("accept")?"primary":"";
if((a=="accept_local"&&!r.fast)||(a=="accept_strong"&&!r.strong))b.disabled=true;
b.onclick=()=>act(a);acts.appendChild(b);}}
function refBox(id,tag,text,withheld){const el=document.getElementById(id);
if(text){el.innerHTML=`<span class="tag">${esc(tag)}</span>${esc(text)}`+
`<a onclick="copyRef('${id}')">复制到编辑框→改一字即 manual</a>`;el.dataset.text=text;}
else{el.innerHTML=`<span class="tag">${esc(tag)}</span><span class="withheld">`+
`参考被隔离（${esc(withheld||"缺失")}）</span>`;el.dataset.text="";}}
function copyRef(id){document.getElementById("target").value=
document.getElementById(id).dataset.text;}
let busy=false;
async function act(action){if(busy)return;busy=true;   // 双击/连按只算一次
const r=state.rows[idx];
try{
const resp=await fetch("api/label",{method:"POST",headers:{"Content-Type":"application/json"},
body:JSON.stringify({id:r.id,action,target:document.getElementById("target").value})});
const out=await resp.json();
if(!resp.ok){alert(out.error);return}
r.done=action;let j=idx+1;while(j<state.rows.length&&state.rows[j].done)j++;
if(j<state.rows.length)idx=j;render();
}catch(e){alert("保存失败（网络/服务异常），这条尚未落盘："+e)}
finally{busy=false}}
function move(d){idx=Math.min(Math.max(idx+d,0),state.rows.length-1);render();}
document.addEventListener("keydown",e=>{
if(e.target.tagName==="TEXTAREA"&&e.key!=="Escape")return;
if(e.key>="1"&&e.key<="7")act(ACTIONS[e.key-1][0]);
if(e.key==="ArrowLeft")move(-1);if(e.key==="ArrowRight")move(1);});
load();
</script></body></html>"""


def relabel_batch_path(queue_path):
    """盲复标批按**队列**隔离命名。曾经是固定文件名：换一天重建队列后，
    旧批次的 queue_id 会指向新队列里完全不同的句子——要么 KeyError 打挂
    /api/state，要么一致性统计悄悄比对两条不相干的句子。"""
    stem = Path(queue_path).stem.replace("annotation-queue-", "")
    return Path(queue_path).parent / ("pilot-relabel-batch-" + stem + ".json")


class Annotator:
    def __init__(self, log_dir, relabel=False):
        self.queue_path = latest_queue(log_dir)
        if self.queue_path is None:
            raise SystemExit("[错误] 没有标注队列——先跑 tools/build_annotation_queue.py")
        self.relabel = relabel
        self.rows = {r["id"]: r for r in load_jsonl(self.queue_path)}
        from app.provenance import code_commit
        self.stamp = {
            "guide_hash": file_hash(ROOT / "benchmarks" / "annotation_guide.md"),
            "commit": code_commit(),
            "queue_hash": file_hash(self.queue_path),
        }
        self.res_path = results_path(self.queue_path, relabel=relabel)
        # 单实例锁：第二个进程用旧的内存快照整文件重写，会静默抹掉第一个
        # 进程已 fsync 的记录（实测复现过）。宁可拒绝启动
        import threading
        self._mutex = threading.Lock()
        self.lock_path = self.res_path.with_suffix(".lock")
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
        except FileExistsError:
            raise SystemExit(
                "[错误] 另一个标注实例正在使用 {}（或上次异常退出残留）。"
                "确认没有别的实例后删除该锁文件再启动。".format(
                    self.lock_path.name)) from None
        import atexit
        atexit.register(lambda: self.lock_path.unlink(missing_ok=True))
        self.records = load_jsonl(self.res_path)
        if relabel:
            base = load_jsonl(results_path(self.queue_path))
            batch_path = relabel_batch_path(self.queue_path)
            if batch_path.exists():
                batch = json.loads(batch_path.read_text(encoding="utf-8"))
            else:
                batch = build_relabel_batch(
                    list(effective_results(base).values()))
                if not batch:
                    raise SystemExit("[错误] 还没有已标结果，无法生成盲复标批")
                batch_path.write_text(json.dumps(batch, ensure_ascii=False,
                                                 indent=1), encoding="utf-8")
                print("[信息] 盲复标批已生成：{}（{} 条）".format(
                    batch_path.name, len(batch)))
            self.order = [b["queue_id"] for b in batch
                          if b["queue_id"] in self.rows]
            if len(self.order) < len(batch):
                print("[警告] 盲复标批里 {} 条的 queue_id 不在当前队列——"
                      "批次可能来自旧队列，已跳过".format(
                          len(batch) - len(self.order)))
            self.blind = {b["queue_id"]: b["blind_id"] for b in batch}
        else:
            self.order = sorted(self.rows)
            self.blind = None

    def state(self):
        done = {r["queue_id"]: r["action"]
                for r in effective_results(self.records).values()}
        out = []
        for qid in self.order:
            r = dict(self.rows[qid])
            if self.blind:
                # 盲复标：真 id 只用于落盘，界面展示盲 id
                r["display_id"] = self.blind[qid]
            r["done"] = done.get(qid)
            out.append(r)
        return {"rows": out, "relabel": bool(self.blind)}

    def label(self, qid, action, target):
        row = self.rows[qid]
        rec = make_record(row, action, target, self.stamp,
                          relabel_batch=("pilot" if self.blind else None))
        # 串行化「追加 + 整文件重写」：ThreadingHTTPServer 每请求一线程，
        # 双击/快捷键连按的两个 POST 并发重写同一文件，实测会在 os.replace
        # 上竞态（共享 tmp 名被对方挪走 → FileNotFoundError → 记录丢失）
        with self._mutex:
            self.records.append(rec)
            save_results_atomic(self.res_path, self.records)
        return rec


def serve(annotator, port):
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json"):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            try:
                if self.path in ("/", "/index.html"):
                    self._send(200, PAGE, "text/html")
                elif self.path == "/api/state":
                    self._send(200, json.dumps(annotator.state(),
                                               ensure_ascii=False))
                else:
                    self._send(404, "{}")
            except Exception as exc:
                # 任何异常都要回成 JSON——线程里吞掉 traceback、客户端
                # 无响应，页面就是静默打不开
                self._send(500, json.dumps({"error": str(exc)},
                                           ensure_ascii=False))

        def do_POST(self):
            if self.path != "/api/label":
                return self._send(404, "{}")
            try:
                n = int(self.headers.get("Content-Length", 0))
                if n > 1_000_000:
                    raise ValueError("请求体过大")
                body = json.loads(self.rfile.read(n))
                rec = annotator.label(int(body["id"]), body.get("action"),
                                      body.get("target"))
                self._send(200, json.dumps({"ok": True,
                                            "action": rec["action"]},
                                           ensure_ascii=False))
            except (ValueError, KeyError) as exc:
                self._send(400, json.dumps({"error": str(exc)},
                                           ensure_ascii=False))
            except Exception as exc:
                self._send(500, json.dumps({"error": str(exc)},
                                           ensure_ascii=False))

        def log_message(self, *a):
            pass

    import signal

    # SIGTERM 默认不跑 atexit，锁会残留；转成正常退出让锁释放
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("标注界面: http://127.0.0.1:{}  （队列 {}，结果 {}）".format(
        port, annotator.queue_path.name, annotator.res_path.name))
    srv.serve_forever()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", default=None)
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--relabel", action="store_true", help="盲复标模式")
    ap.add_argument("--compare", action="store_true", help="对比两次标注")
    args = ap.parse_args()
    provenance.eval_holdout(strict=True)   # 数据生产工具：没有冻结清单不开工
    log_dir = Path(args.logs) if args.logs else provenance.LOG_DIR

    if args.compare:
        qp = latest_queue(log_dir)
        first = effective_results(load_jsonl(results_path(qp)))
        second = effective_results(load_jsonl(results_path(qp, relabel=True)))
        stats = compare_relabel(first, second)
        print("一致性（n={}）: action {} / target 完全一致 {} / "
              "manual↔accept 翻转 {} / 门类动作分歧 {}".format(
                  stats["n"], stats["action_agree"], stats["target_exact"],
                  stats["manual_vs_accept"], stats["gate_disagree"]))
        for d in stats["diffs"]:
            print("  #{}: {} → {}".format(d["queue_id"], d["first"], d["second"]))
        return
    serve(Annotator(log_dir, relabel=args.relabel), args.port)


if __name__ == "__main__":
    main()
