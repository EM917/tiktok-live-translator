"""字幕记录接进 Pipeline（app/pipeline_transcript.py）：会话生命周期、热路径上的钩子、
设置里的两个按钮（导出、打开文件夹），以及「审计里没有多一个字」。

用假服务端（RecordingServer）和脚本化的一场直播（tests/test_resilience_stream.py 的骨架）：
不联网、不起 ffmpeg、不加载模型。导出的格式细节在 tests/test_transcript_export.py。
"""
import json
import time
from types import SimpleNamespace

import pytest

from app import i18n, resolver
from app import pipeline_transcript as pt
from app import transcript as tr
from app.i18n import CJK
from app.pipeline import QUALITY_STRONG, Pipeline
from app.resolver import ResolveError
from tests.helpers import run
from tests.test_resilience_stream import audit_rows, make_pipeline, scripted_resolve
from tests.test_resilience_translation import wait_for

ROOM = "https://www.tiktok.com/@luna_demo/live"
SECRET_COMMENT = "COMENTARIO-SECRETO-DE-PRUEBA"
SECRET_USER = "viewer_secreto_demo"


class FastTranslator:
    """弹幕翻译只认常驻的本地快速模型；逐条返回 "[zh] " + 原文。"""

    name = "hymt2"

    async def translate(self, text, target, source="auto", glossary=None):
        return "[zh] " + text


def res(text, lang="es"):
    return SimpleNamespace(text=text, raw_text=text, language=lang, rejected=[])


def transcript_files(tmp_path):
    """从旧到新（同一秒起的两场，文件名带 -2 后缀，按名字排会倒过来：用程序自己的排法）。"""
    return tr.list_transcripts(tmp_path / "transcripts")


def records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def scripted_session(monkeypatch, p, body):
    """跑一场：解析一次成功、body 在第一轮“拉流”里执行、随后房间已结束。第一轮播了一分钟；之后的轮次
    一帧都没有——V1 每次都重新解析，第二次解析就收到「已结束」；V2 有声之后先用原地址连一轮，没声音
    再去查，同样收到「已结束」。"""
    rounds = []

    async def fake_session(media, *a, **k):
        rounds.append(media)
        if len(rounds) > 1:
            return False, 0.0
        await body()
        return True, 60.0

    async def probe(url):
        return 4, ""

    scripted_resolve(monkeypatch, ["http://cdn/a.flv", ResolveError("没开播", kind="offline", status=4)])
    monkeypatch.setattr(resolver, "probe_room_status", probe)
    monkeypatch.setattr(p, "_stream_session", fake_session)
    run(p._run_stream_inner(ROOM))


def full_body(p, server):
    """一场里该发生的事：一条字幕（快译、再被强模型重译）、一条弹幕（含译文）。"""
    async def body():
        job = await p._emit_original(res("Hola a todos"), time.time() - 1, 900)
        await p._translate_and_update(job)
        p.comments.BATCH_WINDOW_SEC = 0.0
        await p.comments.accept({"items": [{"id": "c1", "user": SECRET_USER, "text": SECRET_COMMENT}]})
        assert await wait_for(lambda: any(m.get("type") == "comment_update" for m in server.messages),
                              limit=2000)
        await p._publish_translation(job["id"], "大家好，欢迎！", True, 2300, QUALITY_STRONG, "zh-CN",
                                     {"strong": True})
    return body


# ---- 一场直播留下一份完整的记录 ----------------------------------------------------------------

def test_a_session_leaves_a_complete_transcript_and_closes_it_at_the_end(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    p.translator = FastTranslator()
    scripted_session(monkeypatch, p, full_body(p, server))
    (path,) = transcript_files(tmp_path)
    assert path.name.endswith("-luna_demo.jsonl")
    rows = records(path)
    assert [r["type"] for r in rows] == [
        "transcript_start", "caption", "translation", "comment", "comment_translation",
        "translation", "transcript_end"]
    head, caption, fast, comment, comment_tr, strong, end = rows
    assert (head["streamer"], head["target_lang"]) == ("luna_demo", "zh-CN")
    assert (caption["src_lang"], caption["original"]) == ("es", "Hola a todos")
    assert (fast["seq"], fast["translated"], fast["quality"]) == (caption["seq"], "[zh] Hola a todos", "fast")
    assert (comment["user"], comment["text"]) == (SECRET_USER, SECRET_COMMENT)
    assert (comment_tr["id"], comment_tr["translated"]) == ("c1", "[zh] " + SECRET_COMMENT)
    assert (strong["seq"], strong["translated"], strong["quality"]) == (caption["seq"], "大家好，欢迎！", "strong")
    assert p.transcript is None                        # 收尾时关了
    # 导出看到的是强模型那一版
    parsed = tr.read_transcript(path)
    assert [(c.translated, c.quality) for c in parsed.captions] == [("大家好，欢迎！", "strong")]
    assert parsed.ended is not None


def test_the_settings_row_learns_how_many_sessions_are_saved(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    assert server.config["transcripts"] == {"count": 0}

    async def one_caption():
        await p._emit_original(res("Hola"), time.time() - 1, 900)

    scripted_session(monkeypatch, p, one_caption)
    assert server.config["transcripts"] == {"count": 1}
    announced = [m for m in server.messages if m.get("type") == "config" and "transcripts" in m]
    assert announced and announced[-1]["transcripts"] == {"count": 1}


def test_the_count_is_pushed_again_when_a_session_ends_because_settings_is_hidden_while_it_runs(
        monkeypatch, tmp_path):
    """开始时推一次（那时这一场还是空的，不算），结束时再推一次：中控停下来回到首页，设置行上
    的数字得是对的，不能要等到下一场开始才变。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    seen_while_running = []

    async def one_caption():
        await p._emit_original(res("Hola"), time.time() - 1, 900)
        seen_while_running.append(dict(server.config["transcripts"]))

    scripted_session(monkeypatch, p, one_caption)
    assert seen_while_running == [{"count": 0}]
    assert server.config["transcripts"] == {"count": 1}
    pushed = [m["transcripts"]["count"] for m in server.messages
              if m.get("type") == "config" and "transcripts" in m]
    assert pushed == [0, 1]


def test_a_session_that_heard_nothing_is_not_counted_as_saved(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def nothing():
        return None

    scripted_session(monkeypatch, p, nothing)
    assert len(transcript_files(tmp_path)) == 1            # 文件还在（程序从不删记录）……
    assert server.config["transcripts"] == {"count": 0}    # ……但不算「存了一场」


def test_a_second_session_gets_its_own_file_and_the_first_is_not_touched(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def one_caption():
        await p._emit_original(res("primera"), time.time() - 1, 900)

    scripted_session(monkeypatch, p, one_caption)
    (first,) = transcript_files(tmp_path)
    before = first.read_bytes()

    async def another_caption():
        await p._emit_original(res("segunda"), time.time() - 1, 900)

    scripted_session(monkeypatch, p, another_caption)
    assert len(transcript_files(tmp_path)) == 2
    assert first.read_bytes() == before
    assert [r["original"] for r in records(transcript_files(tmp_path)[-1]) if r["type"] == "caption"] == ["segunda"]


def test_stopping_a_session_closes_its_transcript_together_with_its_audit(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    audit = object()
    p.audit = None
    p._open_transcript("luna_demo", audit)
    p._transcript_caption(1, time.time(), "es", "Hola")
    assert p.transcript is not None and p.transcript.path.exists()
    path = p.transcript.path
    run(p._stop_locked(quiet=True))
    assert p.transcript is None
    assert [r["type"] for r in records(path)][-1] == "transcript_end"


# ---- 晚死的旧任务关不掉新会话的记录（CLAUDE.md 第七条） -----------------------------------------

def test_a_late_old_task_cannot_close_the_next_sessions_transcript(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    old_audit, new_audit = object(), object()
    p._open_transcript("luna_demo", old_audit)
    old = p.transcript
    old.caption(1, time.time(), "es", "de la primera")
    p._open_transcript("luna_demo", new_audit)           # 新的一场开始，旧的顺手关掉
    new = p.transcript
    assert old._fh is None and new is not old
    p._close_transcript(old_audit)                      # 旧任务终于走到收尾，拿着旧审计来关
    assert p.transcript is new and new._fh is not None
    # 上一场迟到的译文（序号对不上这一场）也串不进来
    p._transcript_translation(1, "迟到的译文", False)
    new.caption(2, time.time(), "es", "de la segunda")
    p._close_transcript(new_audit)
    assert p.transcript is None and new._fh is None
    assert [r["type"] for r in records(new.path)] == ["transcript_start", "caption", "transcript_end"]


def test_a_stale_session_end_leaves_the_next_sessions_transcript_open(monkeypatch, tmp_path):
    """真的收尾函数：旧任务晚到的 _end_session(旧审计) 只关自己那一场，下一场的记录和审计都不碰。"""
    from app.audit import AuditLog

    p, server = make_pipeline(monkeypatch, tmp_path)
    old_audit = AuditLog(room_url=ROOM, log_dir=tmp_path / "logs-old")
    new_audit = AuditLog(room_url=ROOM, log_dir=tmp_path / "logs-new")
    p.audit = old_audit
    p._open_transcript("luna_demo", old_audit)
    old = p.transcript
    p.audit = new_audit
    p._open_transcript("luna_demo", new_audit)
    new = p.transcript
    run(p._end_session(old_audit, reason="user_stop"))
    assert p.audit is new_audit and p.transcript is new and new._fh is not None
    assert old._fh is None
    run(p._end_session(new_audit, reason="user_stop"))
    assert p.audit is None and p.transcript is None and new._fh is None


# ---- 记录出任何问题，直播照常 -------------------------------------------------------------------

def test_a_transcript_that_cannot_be_written_does_not_stop_captions_or_translations(monkeypatch, tmp_path, capsys):
    p, server = make_pipeline(monkeypatch, tmp_path)
    p.translator = FastTranslator()
    p._open_transcript("luna_demo", object())

    def boom(self, record):
        raise OSError("disk on fire")

    monkeypatch.setattr(tr.TranscriptWriter, "caption", boom)
    monkeypatch.setattr(tr.TranscriptWriter, "translation", boom)
    monkeypatch.setattr(tr.TranscriptWriter, "comment", boom)

    async def go():
        job = await p._emit_original(res("Hola a todos"), time.time() - 1, 900)
        await p._translate_and_update(job)
        await p._comment_broadcast({"type": "comment", "id": "c1", "user": "u", "text": "hola", "ts": 1.0})
        return job

    job = run(go())
    kinds = [m["type"] for m in server.messages]
    assert "caption" in kinds and "comment" in kinds
    update = [m for m in server.messages if m["type"] == "caption_update" and m.get("translated")]
    assert update and update[-1]["translated"] == "[zh] Hola a todos"
    assert job["text"] == "Hola a todos"


def test_a_transcript_folder_that_cannot_be_created_still_lets_the_session_run(monkeypatch, tmp_path, capsys):
    blocker = tmp_path / "transcripts"
    blocker.write_text("a file where the folder should be", encoding="utf-8")
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def one_caption():
        await p._emit_original(res("Hola"), time.time() - 1, 900)

    scripted_session(monkeypatch, p, one_caption)
    assert [m for m in server.messages if m.get("type") == "caption"]
    assert "[警告] 字幕记录写入失败" in capsys.readouterr().out
    rows = audit_rows(tmp_path)
    assert [r["type"] for r in rows if r["type"] == "session_end"] == ["session_end"]


def test_a_postponed_translation_hook_with_no_open_transcript_is_a_no_op(monkeypatch, tmp_path):
    """演示模式、连接还没建好、记录没建起来：钩子都在没有记录时空转。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    assert p.transcript is None
    p._transcript_caption(1, time.time(), "es", "Hola")
    p._transcript_translation(1, "你好", True)
    run(p._comment_broadcast({"type": "comment", "id": "c1", "user": "u", "text": "hola", "ts": 1.0}))
    assert [m["type"] for m in server.messages] == ["comment"]
    assert not (tmp_path / "transcripts").exists()


# ---- 审计里没有多一个字 ------------------------------------------------------------------------

def audit_shape(tmp_path):
    return [(r["type"], sorted(r)) for r in audit_rows(tmp_path)]


def test_the_audit_log_gets_no_comment_text_and_no_new_record_types(monkeypatch, tmp_path):
    """同一场直播跑两遍，一遍开着字幕记录、一遍把它关掉：审计的记录类型和每条记录的字段一模一样；
    弹幕的内容、昵称、译文在审计文件里一个字都找不到（弹幕不进审计）。"""
    from app import audit as audit_mod

    shapes = {}
    for label in ("with", "without"):
        sub = tmp_path / label
        sub.mkdir()
        monkeypatch.setattr(audit_mod, "LOG_DIR", sub / "logs")
        monkeypatch.setattr(tr, "TRANSCRIPT_DIR", sub / "transcripts")
        if label == "without":
            monkeypatch.setattr(Pipeline, "_open_transcript", lambda self, streamer, owner: None)
        p, server = make_pipeline(monkeypatch, sub)
        p.translator = FastTranslator()
        scripted_session(monkeypatch, p, full_body(p, server))
        shapes[label] = audit_shape(sub)
        text = "".join(f.read_text(encoding="utf-8") for f in (sub / "logs").glob("session-*.jsonl"))
        for secret in (SECRET_COMMENT, SECRET_USER, "大家好，欢迎！"):
            assert secret not in text, (label, secret)
        assert "transcript" not in text.lower()
    assert shapes["with"] == shapes["without"]
    assert list((tmp_path / "with" / "transcripts").glob("*.jsonl"))
    assert not (tmp_path / "without" / "transcripts").exists()


def test_the_transcript_file_is_not_inside_the_audit_folder(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)

    async def nothing():
        return None

    scripted_session(monkeypatch, p, nothing)
    assert list((tmp_path / "logs").glob("transcript*")) == []
    assert all(f.name.startswith("session-") for f in (tmp_path / "logs").iterdir())


# ---- 「导出最近一场」与「打开文件夹」 ------------------------------------------------------------

def opened_with(p):
    opened = []
    p._brand_dir_opener = opened.append
    return opened


def notices(server):
    return [m for m in server.messages if m.get("type") == "notice"]


def recorded_session(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    p.translator = FastTranslator()
    scripted_session(monkeypatch, p, full_body(p, server))
    server.messages.clear()
    return p, server


def test_export_writes_the_files_opens_the_folder_and_says_where_they_are(monkeypatch, tmp_path):
    p, server = recorded_session(monkeypatch, tmp_path)
    opened = opened_with(p)
    run(p.handle_control({"type": "export_transcript"}))
    (path,) = transcript_files(tmp_path)
    assert [path.with_suffix(s).exists() for s in (".md", ".csv", ".srt")] == [True, True, True]
    assert opened == [path.parent]
    (note,) = notices(server)
    assert note["where"] == "transcripts"
    assert str(path.parent) in note["text"] and "1 条字幕" in note["text"] and "1 条弹幕" in note["text"]
    with i18n.use(i18n.EN):
        english = i18n.render(note["text"], i18n.EN)
    assert english == "Exported 1 caption, 1 comment (Markdown, CSV and SRT files) to {}".format(path.parent)
    assert not CJK.search(english.replace(str(path.parent), ""))
    assert "COMENTARIO" in path.with_suffix(".csv").read_text(encoding="utf-8-sig")


def test_export_picks_the_latest_session_when_nothing_is_live(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    folder = tmp_path / "transcripts"
    older = tr.TranscriptWriter("mia_demo", "zh-CN", directory=folder, now=1_760_000_000)
    older.caption(1, 1_760_000_001, "es", "vieja")
    older.close()
    newer = tr.TranscriptWriter("luna_demo", "zh-CN", directory=folder, now=1_760_003_600)
    newer.caption(1, 1_760_003_601, "es", "nueva")
    newer.close()
    opened_with(p)
    run(p.handle_control({"type": "export_transcript"}))
    assert newer.path.with_suffix(".md").exists() and not older.path.with_suffix(".md").exists()


def test_export_while_a_session_is_being_recorded_exports_that_one(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    folder = tmp_path / "transcripts"
    other = tr.TranscriptWriter("mia_demo", "zh-CN", directory=folder, now=1_760_100_000)   # 文件名更新
    other.close()
    p._open_transcript("luna_demo", object())            # 正在记的这一场（文件名里的开始时间更早）
    p.transcript.caption(1, 1_760_000_001, "es", "en vivo")
    opened_with(p)
    run(p.handle_control({"type": "export_transcript"}))
    live = p.transcript.path
    assert live.with_suffix(".srt").exists() and not other.path.with_suffix(".srt").exists()
    assert "en vivo" in live.with_suffix(".md").read_text(encoding="utf-8")


def test_export_with_nothing_recorded_yet_says_so_and_opens_nothing(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    opened = opened_with(p)
    run(p.handle_control({"type": "export_transcript"}))
    (note,) = notices(server)
    assert note["text"] == "还没有字幕记录。开始监听后，每一场会自动记录。" and note["where"] == "transcripts"
    assert i18n.render(note["text"], i18n.EN).startswith("There are no transcripts yet.")
    assert opened == [] and not (tmp_path / "transcripts").exists()


def failed_start(monkeypatch, p):
    """一次开始监听：房间不给流地址，一场没听到就结束了（文件头写了，一条字幕都没有）。"""
    scripted_resolve(monkeypatch, [ResolveError("没开播", kind="offline", status=4)])
    run(p._run_stream_inner(ROOM))


@pytest.mark.i18n_fixture      # 解析失败的中文提示是测试交给生产代码的夹具
def test_a_failed_start_after_a_real_session_does_not_hide_the_real_one_from_export(monkeypatch, tmp_path):
    p, server = recorded_session(monkeypatch, tmp_path)
    (real,) = transcript_files(tmp_path)
    failed_start(monkeypatch, p)
    assert len(transcript_files(tmp_path)) == 2            # 空的那份还躺在文件夹里
    assert server.config["transcripts"] == {"count": 1}    # 但设置行只数有内容的
    opened = opened_with(p)
    server.messages.clear()
    run(p.handle_control({"type": "export_transcript"}))
    (note,) = notices(server)
    assert "1 条字幕" in note["text"] and "1 条弹幕" in note["text"]
    assert [real.with_suffix(s).exists() for s in (".md", ".csv", ".srt")] == [True, True, True]
    empty = transcript_files(tmp_path)[-1]
    assert not empty.with_suffix(".md").exists()           # 没有为空的那份导出三个空文件
    assert opened == [real.parent]


@pytest.mark.i18n_fixture      # 解析失败的中文提示是测试交给生产代码的夹具
def test_a_second_failed_start_changes_nothing_either(monkeypatch, tmp_path):
    p, server = recorded_session(monkeypatch, tmp_path)
    for _ in range(2):
        failed_start(monkeypatch, p)
    assert len(transcript_files(tmp_path)) == 3
    assert tr.latest_transcript() == transcript_files(tmp_path)[0]
    assert server.config["transcripts"] == {"count": 1}


@pytest.mark.i18n_fixture      # 解析失败的中文提示是测试交给生产代码的夹具
def test_export_after_nothing_but_failed_starts_says_there_are_no_transcripts_yet(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    failed_start(monkeypatch, p)
    assert len(transcript_files(tmp_path)) == 1
    opened = opened_with(p)
    server.messages.clear()
    run(p.handle_control({"type": "export_transcript"}))
    (note,) = notices(server)
    assert note["text"] == "还没有字幕记录。开始监听后，每一场会自动记录。"
    assert opened == []
    assert not transcript_files(tmp_path)[0].with_suffix(".md").exists()


def test_export_says_plainly_when_the_session_being_recorded_has_nothing_yet(monkeypatch, tmp_path):
    """正在记的这一场还一条都没有：不报「已导出 0 条字幕、0 条弹幕」，直说还没有内容。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    older = tr.TranscriptWriter("mia_demo", "zh-CN", directory=tmp_path / "transcripts", now=1_760_000_000)
    older.caption(1, 1_760_000_001, "es", "vieja")
    older.close()
    p._open_transcript("luna_demo", object())
    opened = opened_with(p)
    run(p.handle_control({"type": "export_transcript"}))
    (note,) = notices(server)
    assert note["where"] == "transcripts" and "已导出" not in note["text"]
    assert note["text"] == "这一场还没有记到字幕或弹幕，等有内容了再导出。"
    english = i18n.render(note["text"], i18n.EN)
    assert english == "Nothing has been recorded in this session yet. Export again once captions or comments come in."
    assert not CJK.search(english)
    assert opened == [] and not older.path.with_suffix(".md").exists()
    p._transcript_caption(1, time.time(), "es", "Hola")        # 有了第一条，就导出这一场
    server.messages.clear()
    run(p.handle_control({"type": "export_transcript"}))
    (note,) = notices(server)
    assert "1 条字幕" in note["text"] and p.transcript.path.with_suffix(".md").exists()


def test_export_does_not_claim_success_when_the_file_has_nothing_the_exporter_can_use(monkeypatch, tmp_path):
    """文件里有字幕记录的外形、但读出来一条都没有（被手改坏）：同样直说，不报成功的 0 和 0。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    folder = tmp_path / "transcripts"
    folder.mkdir()
    broken = folder / "transcript-20251009-000000-luna_demo.jsonl"
    broken.write_text('{"type":"transcript_start","v":1,"started_at":"nope"}\n'
                      '{"type":"caption","time":"nope","original":"sin numero"}\n', encoding="utf-8")
    opened = opened_with(p)
    run(p.handle_control({"type": "export_transcript"}))
    (note,) = notices(server)
    assert "已导出" not in note["text"]
    assert note["text"] == "这份字幕记录里读不出字幕或弹幕，没有可导出的内容：{}".format(broken)
    english = i18n.render(note["text"], i18n.EN)
    assert english == ("No captions or comments could be read from this transcript, "
                       "so there is nothing to export: {}".format(broken))
    assert opened == []


def test_export_says_what_happened_when_it_fails_and_the_terminal_stays_chinese(monkeypatch, tmp_path, capsys):
    p, server = recorded_session(monkeypatch, tmp_path)
    opened = opened_with(p)

    def denied(path):
        raise PermissionError(13, "Permission denied", "/tmp/x.csv")

    monkeypatch.setattr(pt, "export_transcript", denied)
    capsys.readouterr()
    with i18n.use(i18n.EN):
        run(p.handle_control({"type": "export_transcript"}))
    (note,) = notices(server)
    assert note["text"].startswith("没能导出字幕记录。详情：") and "Permission denied" in note["text"]
    assert i18n.render(note["text"], i18n.EN) == (
        "Couldn’t export the transcript. Details: [Errno 13] Permission denied: '/tmp/x.csv'")
    assert "[警告] 字幕记录导出失败：" in capsys.readouterr().out       # 终端永远是中文，界面是英文也一样
    assert opened == []


def test_export_still_reports_success_when_the_folder_will_not_open(monkeypatch, tmp_path, capsys):
    p, server = recorded_session(monkeypatch, tmp_path)

    def no_gui(path):
        raise OSError("no display")

    p._brand_dir_opener = no_gui
    run(p.handle_control({"type": "export_transcript"}))
    (note,) = notices(server)
    assert note["text"].startswith("已导出 ") and note["text"].endswith("。文件夹没能自动打开")
    assert i18n.render(note["text"], i18n.EN).endswith(". The folder didn’t open automatically")
    (path,) = transcript_files(tmp_path)
    assert path.with_suffix(".md").exists()


def test_export_says_so_when_this_sessions_transcript_could_not_be_created(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    blocker = tmp_path / "blocked"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(tr, "TRANSCRIPT_DIR", blocker / "sub")
    p._open_transcript("luna_demo", object())
    assert p.transcript is not None and p.transcript.path is None
    run(p.handle_control({"type": "export_transcript"}))
    (note,) = notices(server)
    assert "没能创建" in note["text"]


def test_open_folder_creates_it_and_opens_it(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    opened = opened_with(p)
    assert p.handle_control({"type": "open_transcripts_dir"}) is None
    assert (tmp_path / "transcripts").is_dir()
    assert opened == [tmp_path / "transcripts"]


def test_open_folder_reports_failure_through_a_notice_without_raising(monkeypatch, tmp_path, capsys):
    p, server = make_pipeline(monkeypatch, tmp_path)

    def no_gui(path):
        raise OSError("no display")

    p._brand_dir_opener = no_gui
    run(p.handle_control({"type": "open_transcripts_dir"}))
    (note,) = notices(server)
    assert note["text"].startswith("无法打开文件夹：") and note["where"] == "transcripts"
    assert "[警告] 打开字幕记录文件夹失败" in capsys.readouterr().out


@pytest.mark.parametrize("message", ["export_transcript", "open_transcripts_dir"])
def test_the_two_buttons_are_control_messages_the_phone_viewer_cannot_send(message):
    """手机同看的入站白名单只认 retranslate（app/viewer.py）；这两个只有本机页面发得出来。"""
    from app import viewer

    assert message not in str(viewer.ALLOW)
