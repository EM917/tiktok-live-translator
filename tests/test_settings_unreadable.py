"""settings.json 在、却读不出来时，写入不许把它盖掉（2026-10-29）。

以前 load_settings 碰到任何 OSError 都当成空设置，save_setting 接着在这个空 dict 上合并一项、整个写回：
文件读不了、文件夹却可写（chmod 000 一类）时，DeepL / Claude 密钥、引擎选择、最近直播间被一次保存
静默抹掉——启动时 i18n.settle 写 ui_lang、更新器记时间戳都会触发。现在只有「文件不在」照旧当空；
文件在却读不出来，写入一律不写，原文件一个字节不动，界面挂一条持续提示，照实说是哪个文件、系统怎么说。

.venv 里的安装记录（.requirements.sha256）同理：在却读不了时不再当成「没装过」每次启动都补装；补装一次、
装成功记进尝试记录，清单变了照旧补装。

复核补的（2026-10-29）：只改一项里一条的写手（api_keys、最近直播间、品牌）读改写在同一把锁里，读错误一下就过去
也不会只剩新的一条；一键更新的两个记号写不进去时如实返回、不再承诺「重启后自动恢复监听」。

第二轮复核（2026-10-30）：update_setting 判「值没变」连类型一起比；尝试记录里的「装成功」只在主记录没变过时算；
「没记过就补装一次」只在真要补装时说。"""
import errno
import json
import os
import sys
from types import SimpleNamespace

import pytest

from app import bootstrap, i18n, settings
from app.i18n import CJK, EN
from app.pipeline import Pipeline
from tests.helpers import run
from tests.test_resilience_evidence import RecordingServer, assert_plain

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="Windows 不用 POSIX 权限位")

ORIGINAL = {"api_keys": {"DEEPL_API_KEY": "k-123"}, "translator": "deepl", "ui_lang": "zh",
            "recent_rooms": [{"streamer": "brisa", "url": "https://www.tiktok.com/@brisa/live",
                              "at": "2026-10-29T10:00:00"}]}


@pytest.fixture
def settings_file(monkeypatch, tmp_path):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings, "SETTINGS_FILE", path)
    monkeypatch.setattr(settings, "_corrupt", {"backup": None, "announced": False})
    monkeypatch.setattr(settings, "_unreadable", {"problem": None}, raising=False)
    return path


def _lock(path):
    """把文件改成 0000（所在文件夹照旧可写）。这个账户照样读得了（root）就跳过：测不出这种情况。"""
    os.chmod(str(path), 0)
    try:
        path.read_bytes()
    except PermissionError:
        return
    os.chmod(str(path), 0o600)
    pytest.skip("这个账户读得了 0000 的文件")


@pytest.fixture
def unreadable(settings_file):
    """chmod 000 的 settings.json，里面是 ORIGINAL。返回原始字节；测完改回 0600，tmp_path 才删得掉。"""
    raw = json.dumps(ORIGINAL, ensure_ascii=False, indent=2).encode("utf-8")
    settings_file.write_bytes(raw)
    _lock(settings_file)
    yield raw
    os.chmod(str(settings_file), 0o600)


def _bytes_after_unlock(path):
    os.chmod(str(path), 0o600)
    return path.read_bytes()


def _fail_reads(monkeypatch, path, times=1, skip=0):
    """让 path 接下来的 read_bytes 先放过 skip 次，再抛 times 次 PermissionError（EACCES），之后照常；times=None
    是一直抛，直到把返回的 dict 里 "on" 改成 False。不靠 chmod：Windows 上也测得了，也造得出「这一次读不了、
    紧接着那一次又读得了」这种 chmod 卡不准的时机。"""
    real = type(path).read_bytes
    state = {"on": True, "skip": skip, "left": times}

    def read_bytes(self):
        if self == path and state["on"]:
            if state["skip"]:
                state["skip"] -= 1
            elif state["left"] is None or state["left"] > 0:
                if state["left"] is not None:
                    state["left"] -= 1
                raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(self))
        return real(self)

    monkeypatch.setattr(type(path), "read_bytes", read_bytes)
    return state


# ---- 读不了：不写回 -----------------------------------------------------------------------------

@posix_only
def test_save_setting_leaves_an_unreadable_file_untouched(settings_file, unreadable):
    settings.save_setting("update_check_ok_at", 1761700000.0)
    assert _bytes_after_unlock(settings_file) == unreadable
    assert not settings_file.with_suffix(".json.tmp").exists()
    assert list(settings_file.parent.iterdir()) == [settings_file]     # 也没被当成损坏改名备份


@posix_only
def test_every_writer_of_settings_refuses_while_the_file_is_unreadable(settings_file, unreadable):
    """读-改-写的几个入口（最近直播间、按主播记品牌）都经 save_setting：一样不写。"""
    settings.push_recent_room("luna_demo", "https://www.tiktok.com/@luna_demo/live")
    settings.save_streamer_brand("luna_demo", "acme")
    assert _bytes_after_unlock(settings_file) == unreadable


@posix_only
def test_settle_at_startup_does_not_overwrite_an_unreadable_file(settings_file, unreadable,
                                                                 monkeypatch):
    """开闸后第一次启动、设置里读不到 ui_lang 就写一次：读不了的文件里明明有 ui_lang，不能拿空设置去写。"""
    monkeypatch.setattr(i18n, "I18N_ENABLED", True)
    env = {i18n.ENV_INSTALL: "fresh", i18n.ENV_SYSTEM: "en", i18n.ENV_SYSTEM_ZH: "0"}
    i18n.settle(settings_file.parent, environ=env)
    assert _bytes_after_unlock(settings_file) == unreadable


@posix_only
def test_save_setting_says_whether_it_wrote(settings_file, unreadable):
    assert settings.save_setting("k", "v") is False
    os.chmod(str(settings_file), 0o600)
    assert settings.save_setting("k", "v") is True
    assert settings.load_settings() == dict(ORIGINAL, k="v")


@posix_only
def test_unreadable_settings_reports_the_file_and_what_the_system_said(settings_file, unreadable):
    assert settings.load_settings() == {}
    problem = settings.unreadable_settings()
    assert problem is not None
    path, detail = problem
    assert path == str(settings_file)
    assert os.strerror(errno.EACCES) in detail          # 系统原话，不是我们换的说法
    os.chmod(str(settings_file), 0o600)
    assert settings.load_settings() == ORIGINAL
    assert settings.unreadable_settings() is None        # 又读得出来了：不再报


def test_a_directory_in_place_of_the_file_is_not_replaced(settings_file):
    """读不出来的不只是权限：路径上是个文件夹时 read_bytes 抛 IsADirectoryError（Windows 上是
    PermissionError），一样不写——os.replace 换不掉文件夹，但也不该去试。"""
    settings_file.mkdir()
    (settings_file / "keep").write_text("x", encoding="utf-8")
    assert settings.save_setting("k", "v") is False
    assert settings_file.is_dir() and (settings_file / "keep").read_text(encoding="utf-8") == "x"
    assert settings.unreadable_settings()[0] == str(settings_file)
    # 连写都没试：试了的话临时文件已经建出来、写进去了，只是换不掉文件夹才碰巧没成
    assert not settings_file.with_suffix(".json.tmp").exists()


def test_the_terminal_warning_is_printed_once_per_problem(settings_file, monkeypatch, capsys):
    """浮窗每隔几秒存一次位置（float_window.py）：读不出来的那段时间里，终端只说一次。好了之后又坏，再说一次。"""
    settings_file.write_bytes(json.dumps(ORIGINAL).encode("utf-8"))
    blocked = _fail_reads(monkeypatch, settings_file, times=None)
    for i in range(5):
        assert settings.save_setting("float_geom", i) is False
    settings.load_settings()
    assert capsys.readouterr().out.count("[警告]") == 1
    blocked["on"] = False
    assert settings.load_settings() == ORIGINAL
    blocked["on"] = True
    assert settings.save_setting("float_geom", 9) is False
    assert capsys.readouterr().out.count("[警告]") == 1


def test_an_unreadable_file_that_is_then_deleted_is_no_longer_reported(settings_file, monkeypatch):
    """读不出来、后来文件被删了：那就是没有设置，照常新建；不能还挂着「读不出来、改的设置不会保存」。"""
    settings_file.write_bytes(json.dumps(ORIGINAL).encode("utf-8"))
    blocked = _fail_reads(monkeypatch, settings_file, times=None)
    assert settings.load_settings() == {}
    assert settings.unreadable_settings() is not None
    blocked["on"] = False
    settings_file.unlink()
    assert settings.load_settings() == {}
    assert settings.unreadable_settings() is None
    assert settings.save_setting("k", "v") is True
    assert json.loads(settings_file.read_text(encoding="utf-8")) == {"k": "v"}


# ---- 不在：照旧新建 -----------------------------------------------------------------------------

def test_a_missing_file_is_still_created(settings_file):
    assert settings.load_settings() == {}
    assert settings.unreadable_settings() is None
    assert settings.save_setting("translator", "deepl") is True
    assert json.loads(settings_file.read_text(encoding="utf-8")) == {"translator": "deepl"}


def test_a_missing_folder_is_neither_unreadable_nor_written(settings_file, tmp_path, monkeypatch):
    """设置文件所在的文件夹都不在：读的时候算「没有文件」，写的时候建不出临时文件——和以前一样不写，
    也不报「读不出来」。"""
    path = tmp_path / "gone" / "settings.json"
    monkeypatch.setattr(settings, "SETTINGS_FILE", path)
    assert settings.load_settings() == {}
    assert settings.save_setting("k", "v") is False
    assert settings.unreadable_settings() is None
    assert not path.parent.exists()


# ---- 解析不了：照旧改名备份；备份不成就不写 ------------------------------------------------------

def test_a_damaged_file_is_still_backed_up_and_then_written_fresh(settings_file, tmp_path):
    settings_file.write_text("{ not valid json", encoding="utf-8")
    assert settings.save_setting("translator", "deepl") is True
    backups = list(tmp_path.glob("settings.json.corrupt-*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == "{ not valid json"
    assert json.loads(settings_file.read_text(encoding="utf-8")) == {"translator": "deepl"}
    assert settings.unreadable_settings() is None


def test_a_damaged_file_that_could_not_be_backed_up_is_not_overwritten(settings_file, monkeypatch):
    """解析不了、改名备份又没成（这里让改名那一步失败）：原文件还在原处，写回就会把它盖掉——不写，
    并照实报解析器的原话。"""
    settings_file.write_text("{ not valid json", encoding="utf-8")
    real_replace = os.replace

    def refuse_backup(src, dst):
        if str(src) == str(settings_file):
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(src))
        return real_replace(src, dst)
    monkeypatch.setattr(settings.os, "replace", refuse_backup)
    assert settings.save_setting("translator", "deepl") is False
    assert settings_file.read_text(encoding="utf-8") == "{ not valid json"
    path, detail = settings.unreadable_settings()
    assert path == str(settings_file) and "Expecting" in detail
    assert settings.corrupt_backup_name() is None


def test_a_damaged_file_rewritten_before_its_backup_is_not_overwritten(settings_file, monkeypatch):
    """解析不了，改名备份之前别的写手（第二个实例、编辑器）已经把它换成了合法的设置：备份前再读一次，内容变了就
    不挪，这一次也不写——以前照样拿空设置写回，把那份新设置盖掉。"""
    settings_file.write_text("{ not valid json", encoding="utf-8")
    good = json.dumps(ORIGINAL, ensure_ascii=False).encode("utf-8")
    real = settings._backup_corrupt

    def rewritten_meanwhile(raw):
        settings_file.write_bytes(good)
        return real(raw)
    monkeypatch.setattr(settings, "_backup_corrupt", rewritten_meanwhile)
    assert settings.save_setting("k", "v") is False
    assert settings_file.read_bytes() == good
    assert not list(settings_file.parent.glob("settings.json.corrupt-*"))


def test_a_damaged_file_that_cannot_be_read_again_for_its_backup_is_not_overwritten(settings_file,
                                                                                    monkeypatch):
    """解析不了，备份前再读那一次又读不了：不知道它此刻是什么，不挪也不写。"""
    settings_file.write_text("{ not valid json", encoding="utf-8")
    _fail_reads(monkeypatch, settings_file, skip=1)       # 第一次读出坏内容；备份前再读的那一次读不了
    assert settings.save_setting("k", "v") is False
    assert settings_file.read_text(encoding="utf-8") == "{ not valid json"
    assert not list(settings_file.parent.glob("settings.json.corrupt-*"))


# ---- 只改一项里一条的写手：读改写在同一把锁里 -------------------------------------------------------

COMPOSITE = {"api_keys": {"ANTHROPIC_API_KEY": "k-claude", "OPENAI_API_KEY": "k-openai"},
             "translator": "claude",
             "recent_rooms": [{"streamer": "brisa", "url": "https://www.tiktok.com/@brisa/live",
                               "at": "2026-10-29T10:00:00"},
                              {"streamer": "luna_demo", "url": "https://www.tiktok.com/@luna_demo/live",
                               "at": "2026-10-29T09:00:00"}],
             "brands": {"brisa": "acme", "luna_demo": "bolt"}}


def test_a_read_error_that_passes_at_once_does_not_cut_recent_rooms_or_brands(settings_file, monkeypatch):
    """以前 push_recent_room、save_streamer_brand 先在锁外读一次，改一条再交给 save_setting 整项写回：锁外那次
    碰上读错误拿到空的，锁里那次又读出来了，写回去的就只剩新的一条。现在只在锁里读一次，读不了就不写。"""
    settings_file.write_text(json.dumps(COMPOSITE), encoding="utf-8")
    url = "https://www.tiktok.com/@cola_demo/live"
    _fail_reads(monkeypatch, settings_file)
    settings.push_recent_room("cola_demo", url)
    _fail_reads(monkeypatch, settings_file)
    settings.save_streamer_brand("cola_demo", "cola")
    saved = settings.load_settings()
    assert {"brisa", "luna_demo"} <= {r["streamer"] for r in saved["recent_rooms"]}
    assert {"brisa": "acme", "luna_demo": "bolt"}.items() <= saved["brands"].items()
    # 读得出来了：照常合进去
    entries = settings.push_recent_room("cola_demo", url)
    assert [r["streamer"] for r in entries] == ["cola_demo", "brisa", "luna_demo"]
    assert settings.save_streamer_brand("cola_demo", "cola") == dict(COMPOSITE["brands"], cola_demo="cola")
    saved = settings.load_settings()
    assert [r["streamer"] for r in saved["recent_rooms"]] == ["cola_demo", "brisa", "luna_demo"]
    assert saved["brands"] == dict(COMPOSITE["brands"], cola_demo="cola")


class _EngineServer:
    def __init__(self):
        self.sent = []
        self.config = {}

    async def broadcast(self, msg):
        self.sent.append(msg)

    async def status(self, *a, **k):
        pass


def test_saving_a_key_right_after_a_passing_read_error_keeps_the_other_keys(settings_file, monkeypatch):
    """界面里填一把 DeepL 密钥（Pipeline.set_engine）：以前先 saved_keys() 读一次、整项写回 api_keys，那次读不了
    就只剩这一把，Claude、OpenAI 的密钥全没了。"""
    from app import translator
    settings_file.write_text(json.dumps(COMPOSITE), encoding="utf-8")
    fake = lambda name: type("X", (), {"name": name})()      # noqa: E731
    monkeypatch.setattr(translator, "create_translator", fake)
    monkeypatch.setattr("app.pipeline.create_translator", fake)
    p = Pipeline.__new__(Pipeline)
    p.server = _EngineServer()
    p.translator = None
    p.args = SimpleNamespace(translator="auto")
    _fail_reads(monkeypatch, settings_file)
    run(p.set_engine("deepl", "k-deepl"))
    keys = settings.load_settings()["api_keys"]
    assert keys["ANTHROPIC_API_KEY"] == "k-claude" and keys["OPENAI_API_KEY"] == "k-openai"
    run(p.set_engine("deepl", "k-deepl"))                  # 读得出来了：合进去
    assert settings.load_settings()["api_keys"] == dict(COMPOSITE["api_keys"], DEEPL_API_KEY="k-deepl")


def test_update_setting_reads_changes_and_writes_under_one_lock(settings_file):
    """update_setting 交给 change 的是文件里这一项此刻的值（一份拷贝）；值没变就不写，也算成。"""
    settings_file.write_text(json.dumps(COMPOSITE), encoding="utf-8")
    seen = []

    def change(cur):
        seen.append(dict(cur))
        cur["brisa"] = "changed-in-place"          # 改的是拷贝：不改文件里读出来的那份
        return dict(COMPOSITE["brands"])
    before = settings_file.read_bytes()
    assert settings.update_setting("brands", change) is True
    assert seen == [COMPOSITE["brands"]]
    assert settings_file.read_bytes() == before          # 值没变：没写
    assert settings.update_setting("brands", lambda cur: dict(cur, cola_demo="cola")) is True
    assert settings.load_settings() == dict(COMPOSITE, brands=dict(COMPOSITE["brands"], cola_demo="cola"))


@pytest.mark.parametrize("on_file, new", [
    ({"enabled": 0, "weak": False}, {"enabled": False, "weak": False}),
    ({"enabled": True, "weak": 1}, {"enabled": True, "weak": True}),
    ([1, 0], [True, False]),
    ({"at": 1}, {"at": 1.0}),
])
def test_update_setting_counts_a_different_json_type_as_a_change(settings_file, on_file, new):
    """「值没变就不写」按 JSON 比，类型不同就是变了：Python 的 == 认 0 == False、1 == True、1 == 1.0，拿它比，
    文件里手改成 0 / 1 的开关在用户点了之后写不进去，重启后又按文件里的读。"""
    settings_file.write_text(json.dumps({"comment_guard": on_file}), encoding="utf-8")
    assert settings.update_setting("comment_guard", lambda cur: new) is True
    assert json.dumps(settings.load_settings()["comment_guard"]) == json.dumps(new)      # 类型也对


# ---- 一键更新的两个记号：写不进去就照实说 ----------------------------------------------------------

def test_the_resume_marker_says_when_it_was_not_written(settings_file, monkeypatch, capsys):
    """一键更新重启前留「重启后接着监听」的记号（Pipeline._save_update_resume）：设置文件读不出来时它写不进去。
    以前照样返回 True，更新器就在界面上说「重启后自动恢复监听」，重启后其实停在空闲。"""
    raw = json.dumps(ORIGINAL).encode("utf-8")
    settings_file.write_bytes(raw)
    blocked = _fail_reads(monkeypatch, settings_file, times=None)
    p = _pipeline()
    assert p._save_update_resume({"url": "https://www.tiktok.com/@brisa/live"}, "9.9.9") is False
    assert "重启后不会自动接着监听" in capsys.readouterr().out
    blocked["on"] = False
    assert settings_file.read_bytes() == raw


def test_the_update_guard_marker_says_when_it_was_not_written(settings_file, monkeypatch, capsys):
    """「新版本起不来就自动退回」的记号（update_guard.arm）同理：写不进去返回 False，终端说这次没有这层保险。"""
    from app import update_guard
    from app.updater import Updater
    raw = json.dumps(ORIGINAL).encode("utf-8")
    settings_file.write_bytes(raw)
    blocked = _fail_reads(monkeypatch, settings_file, times=None)
    assert update_guard.arm("f" * 40, "e" * 40, "1.0.0", "1.1.0") is False
    Updater._arm_update_guard("f" * 40, "e" * 40, "1.0.0", "1.1.0")
    assert "新版本起不来时，不会自动退回" in capsys.readouterr().out
    blocked["on"] = False
    assert settings_file.read_bytes() == raw


# ---- 界面提示 -----------------------------------------------------------------------------------

def _pipeline():
    p = Pipeline.__new__(Pipeline)
    p.server = RecordingServer()
    return p


@posix_only
def test_an_unreadable_settings_file_gets_a_banner_until_it_reads_again(settings_file, unreadable):
    settings.load_settings()
    p = _pipeline()

    async def go():
        await p._announce_settings_backup()
        await p._announce_settings_backup()             # 同一件事只挂一次
        shown = dict(p.server.config["incidents"]["settings-unreadable"])
        os.chmod(str(settings_file), 0o600)
        await p._announce_settings_backup()
        return shown

    shown = run(go())
    assert shown["level"] == "warn"
    assert str(settings_file) in shown["text"]
    assert os.strerror(errno.EACCES) in shown["text"]
    assert "不会保存" in shown["text"]
    assert_plain(shown["text"])
    assert [m["level"] for m in p.server.incidents("settings-unreadable")] == ["warn", "clear"]
    assert "settings-corrupt" not in p.server.config.get("incidents", {})


@posix_only
def test_the_unreadable_settings_banner_reads_as_english(settings_file, unreadable, monkeypatch):
    monkeypatch.setattr(i18n, "NET", "strict")
    settings.load_settings()
    p = _pipeline()
    with i18n.use(EN):
        run(p._announce_settings_backup())
    [msg] = p.server.incidents("settings-unreadable")
    text = i18n.render(msg["text"], EN)
    assert text.startswith("Couldn’t read the settings file {}: ".format(settings_file))
    assert os.strerror(errno.EACCES) in text
    assert not CJK.search(text.replace(str(settings_file), ""))


# ---- .venv 里的安装记录 -------------------------------------------------------------------------

def _project(tmp_path):
    (tmp_path / ".venv").mkdir()
    (tmp_path / "requirements.txt").write_text("aiohttp>=3.9\n", encoding="utf-8")
    return tmp_path


@posix_only
def test_an_unreadable_install_record_installs_once_and_then_not_every_launch(tmp_path, capsys):
    """记录在、却读不了：不知道装没装过，补装一次。装成功了主记录却写不进去：记进尝试记录
    （.requirements-attempt.json），之后的启动认它、不再补装——以前当成没装过，每次启动都跑一遍 pip。
    终端照实说一次：同一个启动进程里 ready_fast_path 和 plan_install 各问一次 requirements_pending，两次都走到
    这句。"""
    root = _project(tmp_path)
    stamp = root / ".venv" / bootstrap.REQ_STAMP
    stamp.write_text("0" * 64 + "\n", encoding="utf-8")
    _lock(stamp)
    try:
        assert bootstrap.requirements_pending(root) is True            # ready_fast_path
        assert bootstrap.plan_install(True, root) is not None          # 同一个进程里 plan_install 再问一次
        out = capsys.readouterr().out
        assert out.count(str(stamp)) == 1                               # 一个进程里说一次
        assert os.strerror(errno.EACCES) in out
        assert bootstrap.write_requirements_stamp(root, bootstrap.requirements_file_digest(root)) is True
        assert bootstrap.requirements_pending(root) is False
        assert bootstrap.requirements_pending(root) is False
        assert bootstrap.plan_install(True, root) is None
    finally:
        os.chmod(str(stamp), 0o600)


@posix_only
def test_an_unreadable_install_record_says_nothing_about_installing_while_the_failure_cooldown_holds(
        tmp_path, capsys):
    """记录读不了、这份清单 24 小时内又装失败过：这次不装（同记录读得了时）。终端那句「没记过就补装一次」只在真要
    补装时说——以前先说了这句，接着按冷却不装，说的和做的对不上。"""
    root = _project(tmp_path)
    stamp = root / ".venv" / bootstrap.REQ_STAMP
    stamp.write_text("0" * 64 + "\n", encoding="utf-8")
    _lock(stamp)
    try:
        assert bootstrap.record_requirements_failure(root, bootstrap.requirements_file_digest(root), 1, now=1000.0)
        assert bootstrap.requirements_pending(root, now=1060.0) is False
        assert bootstrap.plan_install(True, root, now=1060.0) is None
        assert str(stamp) not in capsys.readouterr().out
        later = 1000.0 + bootstrap.REQ_RETRY_SEC + 60
        assert bootstrap.requirements_pending(root, now=later) is True           # 冷却过了：补装，这时才说
        out = capsys.readouterr().out
        assert out.count(str(stamp)) == 1 and "补装" in out
    finally:
        os.chmod(str(stamp), 0o600)


def _stamp_trouble(monkeypatch, root):
    """让 .venv 里的安装记录读不了（read_text 抛 EACCES）、尝试记录删不掉（unlink 抛 EACCES），各由返回的 dict 开关。
    不靠 chmod：Windows 上也测得了（那边文件被别的程序占着时就删不掉）。"""
    stamp = root / ".venv" / bootstrap.REQ_STAMP
    attempt = root / ".venv" / bootstrap.REQ_ATTEMPT
    cls = type(stamp)
    real_read, real_write, real_unlink = cls.read_text, cls.write_text, cls.unlink
    state = {"read": False, "write": False, "unlink": False}

    def denied(self):
        return PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(self))

    def read_text(self, *a, **kw):
        if self == stamp and state["read"]:
            raise denied(self)
        return real_read(self, *a, **kw)

    def write_text(self, *a, **kw):
        if self == stamp and state["write"]:
            raise denied(self)
        return real_write(self, *a, **kw)

    def unlink(self, *a, **kw):
        if self == attempt and state["unlink"]:
            raise denied(self)
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(cls, "read_text", read_text)
    monkeypatch.setattr(cls, "write_text", write_text)
    monkeypatch.setattr(cls, "unlink", unlink)
    return state


def test_a_success_noted_beside_an_unreadable_record_stops_counting_once_the_record_is_rewritten(
        tmp_path, monkeypatch):
    """安装记录读不了时装成功，「装成功」记在尝试记录里。后来记录又读得了、装了另一份清单 B 也写进了主记录，
    只是删尝试记录没删掉（Windows 上文件被占用）。再回到清单 A（自动回退、手动 checkout）：机器上装的是 B 锁的版本，
    要按 A 补装——以前那条旧的「A 装成功」盖过读得出来的主记录，永远不补装；main 上会补装。"""
    root = _project(tmp_path)
    trouble = _stamp_trouble(monkeypatch, root)
    manifest = root / "requirements.txt"
    a_text = manifest.read_text(encoding="utf-8")
    a = bootstrap.requirements_file_digest(root)
    trouble["read"] = True
    assert bootstrap.write_requirements_stamp(root, a) is True             # 写了读不回：记进尝试记录
    assert bootstrap.requirements_pending(root) is False                   # 认它
    trouble["read"] = False
    manifest.write_text(a_text + "another>=2\n", encoding="utf-8")         # 清单 B
    assert bootstrap.requirements_pending(root) is True
    trouble["unlink"] = True
    assert bootstrap.write_requirements_stamp(root, bootstrap.requirements_file_digest(root)) is True
    assert bootstrap.recorded_requirements_digest(root) == bootstrap.requirements_file_digest(root)
    manifest.write_text(a_text, encoding="utf-8")                          # 回到清单 A
    assert bootstrap.requirements_pending(root) is True
    assert bootstrap.plan_install(True, root) is not None


def test_a_success_noted_beside_a_record_that_could_not_be_written_still_counts(tmp_path, monkeypatch):
    """主记录读得了、写不进去（只读文件）：装成功记进尝试记录，之后的启动认它、不再补装；主记录后来被写成了别的，
    这条就不再算。"""
    root = _project(tmp_path)
    trouble = _stamp_trouble(monkeypatch, root)
    manifest = root / "requirements.txt"
    a_text = manifest.read_text(encoding="utf-8")
    a = bootstrap.requirements_file_digest(root)
    stamp = root / ".venv" / bootstrap.REQ_STAMP
    stamp.write_text("1" * 64 + "\n", encoding="utf-8")                   # 上一份清单装成功时写的
    trouble["write"] = True
    assert bootstrap.write_requirements_stamp(root, a) is True
    assert bootstrap.requirements_pending(root) is False
    assert bootstrap.plan_install(True, root) is None
    trouble["write"] = False
    manifest.write_text(a_text + "another>=2\n", encoding="utf-8")
    trouble["unlink"] = True
    assert bootstrap.write_requirements_stamp(root, bootstrap.requirements_file_digest(root)) is True
    manifest.write_text(a_text, encoding="utf-8")
    assert bootstrap.requirements_pending(root) is True


@posix_only
def test_a_changed_manifest_is_installed_although_the_install_record_is_unreadable(tmp_path):
    """手动更新（git pull，一键更新以外的路）带来的新依赖、锁的版本变了，只有启动时这里会装：记录读不了也不能
    一律不装。程序缺模块起不来（forget_requirements_failure）之后的那次启动同样要装——不然一直弹「会自动继续安装」
    却从不安装。"""
    root = _project(tmp_path)
    assert bootstrap.write_requirements_stamp(root, bootstrap.requirements_file_digest(root))
    stamp = root / ".venv" / bootstrap.REQ_STAMP
    (root / "requirements.txt").write_text("aiohttp>=3.9\nanother>=2\n", encoding="utf-8")
    _lock(stamp)
    try:
        assert bootstrap.plan_install(True, root) is not None              # 清单变了：补装
        assert bootstrap.write_requirements_stamp(root, bootstrap.requirements_file_digest(root))
        assert bootstrap.plan_install(True, root) is None                  # 装成功记下了
        assert bootstrap.forget_requirements_failure(root)                  # 缺模块起不来
        assert bootstrap.plan_install(True, root) is not None              # 下次启动照旧补装
    finally:
        os.chmod(str(stamp), 0o600)


def test_a_missing_or_garbled_install_record_still_reinstalls(tmp_path):
    """记录不在（从没装成功过）照旧要补装；内容不是 UTF-8 文本也照旧补装一次——装成功就写上新的，自己会好。"""
    root = _project(tmp_path)
    assert bootstrap.requirements_pending(root) is True
    (root / ".venv" / bootstrap.REQ_STAMP).write_bytes(b"\xff\xfe\x00garbage")
    assert bootstrap.recorded_requirements_digest(root) is None
    assert bootstrap.requirements_pending(root) is True
