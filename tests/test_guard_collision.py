"""tools/guard_collision.py：弹幕留意词表在主播口播里的碰撞计数。

两件事要钉住：
  * 直播转写进行中必须拒绝运行（CLAUDE.md 第三条：批量工作叠在直播 ASR 上，丢过 16 段音频）——
    `pgrep` 打桩成命中：退出码非零，而且一份语料都没读、一份词表都没载；
  * 计数本身：数的是「这条词条真的会命中的不同句子」、跨几场；写在 `[段名:pending]` 里还没确认的
    红条档词条照样算，报告里标「待确认」，--term 的候选标「候选（未入表）」。

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
    monkeypatch.setattr(G, "build_rules", boom)
    monkeypatch.setattr(G, "candidate_rules", boom)
    monkeypatch.setattr(G, "corpus_sentences", boom)
    monkeypatch.setattr(G, "collide", boom)


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


@pytest.mark.parametrize("pattern", ["tiktokcdn", "main.py --browser"])
def test_the_refusal_also_covers_term_mode_and_writes_no_json(monkeypatch, pattern, capsys, tmp_path):   # #64
    stub_pgrep(monkeypatch, {pattern: 0})
    forbid_work(monkeypatch)
    out = tmp_path / "out.json"
    with pytest.raises(SystemExit) as exc:
        G.main(["--logs", "/nonexistent", "--term", "no compren", "--as", "accuse/call", "--json", str(out)])
    assert exc.value.code not in (0, None) and "直播转写仍在进行" in str(exc.value.code)
    assert capsys.readouterr().out == "" and not out.exists()


def test_the_gate_checks_both_live_process_patterns(monkeypatch, tmp_path):
    calls = stub_pgrep(monkeypatch, {})
    assert G.live_running() is None
    assert [c[2] for c in calls] == ["tiktokcdn", "main.py --browser"]


def test_without_pgrep_the_gate_cannot_tell_but_says_so_on_stderr(monkeypatch, capsys):   # #64
    def no_pgrep(argv, **kwargs):
        raise FileNotFoundError("pgrep")

    monkeypatch.setattr(G.subprocess, "run", no_pgrep)
    assert G.live_running() is None                       # 行为不变：查不了就当没有
    err = capsys.readouterr()
    assert "查不了直播是否在进行" in err.err and err.out == ""
    assert err.err.count("\n") == 1                      # 就一行


def test_gate_is_the_first_thing_main_does(monkeypatch):
    order = []
    monkeypatch.setattr(G, "live_running", lambda: order.append("gate") or None)
    class Stop(Exception):
        pass

    def stop(*a, **k):
        order.append("rules")
        raise Stop()

    monkeypatch.setattr(G, "build_rules", stop)
    with pytest.raises(Stop):
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
    # `no compren` 是收窄过的正则写法（后面接 tantos / todo 不算）：按前缀找那一行
    no_compren = [r for (f, s, e), r in rows.items() if (f, s) == ("accuse", "call") and e.startswith("re:\\bno compren")]
    assert len(no_compren) == 1 and no_compren[0]["sentences"] == 1


def test_pending_entries_are_counted_and_marked_pending_not_enabled(logs, no_live, tmp_path, capsys):
    d = tmp_path / "defaults"
    d.mkdir()
    (d / "insult.txt").write_text(
        "[medium]\ntonta\n\n# 红条档\n[strong:pending]\npendeja\nzoquete\n[strong]\nidiota\n",
        encoding="utf-8")
    out = tmp_path / "out.json"
    G.main(["--logs", str(logs), "--defaults", str(d), "--json", str(out), "--all"])
    data = json.loads(out.read_text(encoding="utf-8"))
    rows = {r["entry"]: r for r in data["entries"]}
    assert rows["pendeja"]["status"] == "pending" and rows["pendeja"]["sentences"] == 1
    assert rows["pendeja"]["section"] == "strong" and rows["pendeja"]["tier"] == "red"
    assert rows["tonta"]["status"] == "enabled" and rows["idiota"]["status"] == "enabled"
    assert rows["zoquete"]["status"] == "pending" and rows["zoquete"]["sentences"] == 0
    assert data["sections"]["insult/strong:pending"] == 1             # 待确认的段单独计
    text = capsys.readouterr().out
    assert "待确认" in text and "已打开" in text


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


def test_term_mode_labels_the_candidate_as_not_in_the_list_yet(logs, no_live, capsys, tmp_path):   # #66
    out = tmp_path / "out.json"
    G.main(["--logs", str(logs), "--term", "no compren", "--as", "accuse/call", "--all", "--json", str(out)])
    text = capsys.readouterr().out
    assert "候选（未入表）" in text and "已打开" not in text
    rows = json.loads(out.read_text(encoding="utf-8"))["entries"]
    assert [r["status"] for r in rows] == ["candidate"]


def test_pending_sections_are_labelled_pending_in_the_text_report(logs, no_live, capsys, tmp_path):   # #66
    d = tmp_path / "defaults"
    d.mkdir()
    (d / "accuse.txt").write_text("[call:pending]\nno compren\n[assert]\nestafa\n", encoding="utf-8")
    G.main(["--logs", str(logs), "--defaults", str(d), "--all"])
    text = capsys.readouterr().out
    assert "[red档，待确认]" in text and "[red档，已打开]" in text


def header_counts(text):
    """报告里每个分段标题的 (文件/段, 档位+状态, 命中的不同句子数)。"""
    out = {}
    for line in text.splitlines():
        if line.startswith("== "):
            name, _, rest = line[3:].partition("  [")
            out[name] = (rest.split("]")[0], int(rest.rsplit("命中的不同句子", 1)[1]))
    return out


def test_the_header_sentence_count_is_right_for_a_pending_group(logs, no_live, capsys, tmp_path):   # #66
    d = tmp_path / "defaults"
    d.mkdir()
    (d / "insult.txt").write_text("[strong:pending]\npendeja\n", encoding="utf-8")
    (d / "accuse.txt").write_text("[call:pending]\nno compren\n[assert]\nestafa\n", encoding="utf-8")
    G.main(["--logs", str(logs), "--defaults", str(d), "--all"])
    got = header_counts(capsys.readouterr().out)
    assert got["insult/strong"] == ("red档，待确认", 1)           # 同一个句子在 2 场里各出现过，只算 1 个
    assert got["accuse/call"] == ("red档，待确认", 1)
    assert got["accuse/assert"] == ("red档，已打开", 0)


@pytest.mark.parametrize("as_section", ["accuse/call", "accuse/call:pending"])
def test_term_mode_accepts_the_pending_spelling_of_a_section_and_counts_the_same(logs, no_live, capsys, as_section):   # #66
    G.main(["--logs", str(logs), "--term", "no compren", "--as", as_section, "--all"])
    text = capsys.readouterr().out
    assert header_counts(text)["accuse/call"] == ("red档，候选（未入表）", 1), text
    assert "1 句 / " in text or "    1 句" in text


def test_term_mode_error_lists_only_the_plain_section_names(logs, no_live):                # #66
    with pytest.raises(SystemExit) as exc:
        G.main(["--logs", str(logs), "--term", "x", "--as", "nada/nada"])
    assert "insult/strong" in str(exc.value.code) and ":pending" not in str(exc.value.code)


def test_an_empty_corpus_is_an_error_not_a_clean_looking_report(no_live, capsys, tmp_path):   # #20
    empty = tmp_path / "empty-logs"
    empty.mkdir()
    for argv in (["--logs", str(empty)], ["--logs", str(tmp_path / "missing")],
                 ["--logs", str(empty), "--term", "no compren", "--as", "accuse/call"]):
        with pytest.raises(SystemExit) as exc:
            G.main(argv)
        assert exc.value.code not in (0, None) and "语料为空" in str(exc.value.code), argv
        assert capsys.readouterr().out == "", "一份空报告都不许打印"


def test_sessions_without_any_segment_text_are_also_an_empty_corpus(no_live, tmp_path):   # #20
    d = tmp_path / "logs"
    d.mkdir()
    write_session(d, "session-1.jsonl", "https://www.tiktok.com/@luna_demo/live", [])
    with pytest.raises(SystemExit) as exc:
        G.main(["--logs", str(d)])
    assert "语料为空" in str(exc.value.code)


def test_default_logs_falls_back_to_the_home_checkout_when_the_repo_has_none(monkeypatch, tmp_path):   # #20
    monkeypatch.setattr(G.provenance, "LOG_DIR", tmp_path / "no-such-logs")
    monkeypatch.setattr(G.Path, "home", classmethod(lambda cls: tmp_path))
    assert G.default_logs() == tmp_path / "tiktok-live-translator" / "logs"
    real = tmp_path / "logs"
    real.mkdir()
    monkeypatch.setattr(G.provenance, "LOG_DIR", real)
    assert G.default_logs() is None                       # 仓库自己的 logs/ 在：走 provenance 的默认


def test_main_uses_the_fallback_logs_dir_when_none_is_given(no_live, monkeypatch, logs, tmp_path, capsys):   # #20
    home_logs = tmp_path / "home" / "tiktok-live-translator" / "logs"
    home_logs.parent.mkdir(parents=True)
    logs.rename(home_logs)
    monkeypatch.setattr(G.provenance, "LOG_DIR", tmp_path / "no-such-logs")
    monkeypatch.setattr(G.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    G.main(["--defaults", str(FIX)])
    assert "2 场" in capsys.readouterr().out


def test_readme_collision_command_names_the_logs_directory():                        # #20
    text = (HERE.parent / "comment_guard" / "README.md").read_text(encoding="utf-8")
    assert "tools/guard_collision.py --logs ~/tiktok-live-translator/logs" in text


def test_the_tool_imports_no_network_or_model_code():
    text = Path(G.__file__).read_text(encoding="utf-8")
    for word in ("ollama", "import requests", "urllib", "aiohttp", "socket", "mlx"):
        assert word not in text.lower(), word


def test_term_mode_knows_the_slur_section_and_regex_candidates(logs, no_live, capsys, tmp_path):
    """insult/slur 是新的红条档段；候选可以直接写 re: 正则（收窄过的写法），报告里照样按句子计数。"""
    out = tmp_path / "out.json"
    G.main(["--logs", str(logs), "--term", "beaner", "--as", "insult/slur", "--all", "--json", str(out)])
    rows = json.loads(out.read_text(encoding="utf-8"))["entries"]
    assert [(r["file"], r["section"], r["status"], r["sentences"]) for r in rows] == [
        ("insult", "slur", "candidate", 0)]
    G.main(["--logs", str(logs), "--term", r"re:\bno compren\b(?!\s+ese)", "--as", "accuse/call", "--all",
            "--json", str(out)])
    rows = json.loads(out.read_text(encoding="utf-8"))["entries"]
    assert [(r["section"], r["status"], r["sentences"]) for r in rows] == [("call", "candidate", 0)]   # 「ya no compren ese frasco」被 (?!\s+ese) 排除
