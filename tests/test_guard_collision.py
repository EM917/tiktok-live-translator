"""tools/guard_collision.py：弹幕留意词表在主播口播里的碰撞计数。

两件事要钉住：
  * 直播转写进行中必须拒绝运行（CLAUDE.md 第三条：批量工作叠在直播 ASR 上，丢过 16 段音频）——
    `pgrep` 打桩成命中：退出码非零，而且一份语料都没读、一份词表都没载；
  * 计数本身：数的是「这条词条真的会命中的不同句子」、跨几场，「待业务确认」块里注释掉的词条
    按已打开来算。

测试里不跑真 pgrep（直播中跑测试也不能被它影响），不碰模型和网络。主播名用虚构的。
"""
import json
from pathlib import Path

import pytest

from tools import guard_collision as G

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures" / "comment_guard"


class Hit:
    returncode = 0


class Miss:
    returncode = 1


def stub_pgrep(monkeypatch, answers):
    """answers: 按 pgrep 的 pattern 给 returncode；返回调用记录。"""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return Hit() if answers.get(argv[-1], 1) == 0 else Miss()

    monkeypatch.setattr(G.subprocess, "run", fake_run)
    return calls


def forbid_work(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("直播转写进行中，不该读语料或载词表")

    monkeypatch.setattr(G.provenance, "corpus", boom)
    monkeypatch.setattr(G, "build_all_rules", boom)
    monkeypatch.setattr(G, "corpus_sentences", boom)


@pytest.mark.parametrize("pattern", ["tiktokcdn", "main.py --browser"])
def test_refuses_to_run_while_the_live_transcription_is_going(monkeypatch, pattern, capsys):
    calls = stub_pgrep(monkeypatch, {pattern: 0})
    forbid_work(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        G.main(["--logs", "/nonexistent"])
    assert exc.value.code not in (0, None)
    assert "直播转写仍在进行" in str(exc.value.code)
    assert all(c[:2] == ["pgrep", "-f"] for c in calls)
    assert calls[-1] == ["pgrep", "-f", pattern]
    assert capsys.readouterr().out == ""                 # 一个字都没输出过报告


def test_the_gate_checks_both_live_process_patterns(monkeypatch, tmp_path):
    calls = stub_pgrep(monkeypatch, {})
    assert G.live_running() is None
    assert [c[2] for c in calls] == ["tiktokcdn", "main.py --browser"]


def test_without_pgrep_the_gate_cannot_tell_and_lets_it_run(monkeypatch):
    def no_pgrep(argv, **kwargs):
        raise FileNotFoundError("pgrep")

    monkeypatch.setattr(G.subprocess, "run", no_pgrep)
    assert G.live_running() is None


def test_gate_is_the_first_thing_main_does(monkeypatch):
    order = []
    monkeypatch.setattr(G, "live_running", lambda: order.append("gate") or None)
    monkeypatch.setattr(G, "build_all_rules", lambda *a, **k: order.append("rules") or (_ for _ in ()).throw(StopIteration))
    with pytest.raises(StopIteration):
        G.main(["--logs", "/nonexistent"])
    assert order == ["gate", "rules"]


# ------------------------------------------------------------------------------------------
# 计数
# ------------------------------------------------------------------------------------------

def write_session(logs, name, url, sentences):
    rows = [{"type": "session_start", "room_url": url}]
    for i, text in enumerate(sentences):
        rows.append({"type": "segment", "seq": i, "text": "[译] " + text, "raw_text": text})
    (logs / name).write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")


@pytest.fixture
def logs(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    write_session(d, "session-1.jsonl", "https://www.tiktok.com/@luna_demo/live", [
        "Hola chicas, ¿cómo están? Eres una pendeja si no lo compras.",       # 两句，第二句撞 pendeja
        "tirar a la basura el envase vacio", "Hola chicas, ¿cómo están?",
        "Eres una pendeja si no lo compras."])                                # 同一句再出现：算 1 个不同句子，2 次
    write_session(d, "session-2.jsonl", "https://www.tiktok.com/@otra_demo/live", [
        "Eres una pendeja si no lo compras.", "ya no compren ese frasco viejo", "no es estafa, es real"])
    # 测试夹具的直播间地址（单字母句柄）：provenance.corpus 不收，算不进分母
    write_session(d, "session-3.jsonl", "https://www.tiktok.com/@x/live", ["pendeja pendeja"])
    return d


@pytest.fixture
def no_live(monkeypatch):
    monkeypatch.setattr(G, "live_running", lambda: None)


def test_counts_distinct_sentences_and_sessions_per_entry(logs, no_live, capsys, tmp_path):
    out = tmp_path / "out.json"
    assert G.main(["--logs", str(logs), "--defaults", str(FIX), "--json", str(out), "--all"]) == 0
    text = capsys.readouterr().out
    assert "2 场" in text and "不同的口播句子" in text
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["corpus"] == {"sessions": 2, "segments": 7, "sentences": 5}
    assert data["sections"]["insult/strong"] == 1                 # 同一个句子只算一次，不管出现几次
    rows = {(r["file"], r["section"], r["entry"]): r for r in data["entries"]}
    pendeja = rows[("insult", "strong", "pendeja")]
    assert (pendeja["sentences"], pendeja["sessions"], pendeja["occurrences"]) == (1, 2, 3)
    assert pendeja["status"] == "enabled" and pendeja["tier"] == "red"
    assert pendeja["examples"] == ["Eres una pendeja si no lo compras."]
    # 目标词只在「指向对方」的句式里算：tirar a la basura 不撞
    assert rows[("insult", "target", "basura")]["sentences"] == 0
    # 否认式 / 豁免短语先剪掉：「no es estafa」不撞
    assert rows[("accuse", "assert", "estafa")]["sentences"] == 0
    assert rows[("accuse", "call", "no compren")]["sentences"] == 1


def test_pending_block_entries_are_counted_as_if_they_were_switched_on(logs, no_live, tmp_path):
    d = tmp_path / "defaults"
    d.mkdir()
    (d / "insult.txt").write_text(
        "[medium]\ntonta\n\n# ======== 待业务确认 ========\n## 说明用两个井号\n"
        "# [strong]\n# pendeja\n# zoquete\n", encoding="utf-8")
    out = tmp_path / "out.json"
    G.main(["--logs", str(logs), "--defaults", str(d), "--json", str(out), "--all"])
    rows = {r["entry"]: r for r in json.loads(out.read_text(encoding="utf-8"))["entries"]}
    assert rows["pendeja"]["status"] == "pending" and rows["pendeja"]["sentences"] == 1
    assert rows["tonta"]["status"] == "enabled"
    assert rows["zoquete"]["sentences"] == 0
    assert "说明用两个井号" not in rows and "说明用两个井号" not in "".join(rows)


def test_section_filter_and_min_hits_limit_the_report(logs, no_live, capsys):
    G.main(["--logs", str(logs), "--defaults", str(FIX), "--section", "insult/strong"])
    text = capsys.readouterr().out
    assert "insult/strong" in text and "accuse/call" not in text
    assert "pendeja" in text and "idiota" not in text           # 默认只列有命中的
    G.main(["--logs", str(logs), "--defaults", str(FIX), "--section", "insult/strong", "--all"])
    assert "idiota" in capsys.readouterr().out


def test_term_mode_audits_one_candidate_before_it_goes_into_the_list(logs, no_live, capsys):
    G.main(["--logs", str(logs), "--term", "no compren", "--as", "accuse/call", "--all"])
    text = capsys.readouterr().out
    assert "accuse/call" in text and "no compren" in text and "1 句" in text
    with pytest.raises(SystemExit):
        G.main(["--logs", str(logs), "--term", "x", "--as", "nada/nada"])


def test_uncomment_pending_only_touches_the_pending_block():
    src = ("# 普通注释\n[medium]\ntonta\n# 另一条普通注释\n\n"
           "# =========== 待业务确认 ===========\n## 说明\n# [strong]\n# pendeja\n#\n\nrest\n")
    out = G.uncomment_pending(src).splitlines()
    assert "# 普通注释" in out and "# 另一条普通注释" in out          # 块外原样
    assert "[strong]" in out and "pendeja" in out and "## 说明" in out
    assert G.count_pending(src) == 1                                # 只数词条，不数 [段名] 和说明


def test_the_tool_imports_no_network_or_model_code():
    text = Path(G.__file__).read_text(encoding="utf-8")
    for word in ("ollama", "import requests", "urllib", "aiohttp", "socket", "mlx"):
        assert word not in text.lower(), word
