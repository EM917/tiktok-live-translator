"""settings.json 在、却读不出来时，写入不许把它盖掉（2026-10-29）。

以前 load_settings 碰到任何 OSError 都当成空设置，save_setting 接着在这个空 dict 上合并一项、整个写回：
文件读不了、文件夹却可写（chmod 000 一类）时，DeepL / Claude 密钥、引擎选择、最近直播间被一次保存
静默抹掉——启动时 i18n.settle 写 ui_lang、更新器记时间戳都会触发。现在只有「文件不在」照旧当空；
文件在却读不出来，写入一律不写，原文件一个字节不动，界面挂一条持续提示，照实说是哪个文件、系统怎么说。

.venv 里的安装记录（.requirements.sha256）同理：在却读不了时不再当成「没装过」每次启动都补装。"""
import errno
import json
import os
import sys

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
def test_an_unreadable_install_record_does_not_reinstall_every_launch(tmp_path, capsys):
    """记录在、却读不了：不知道装没装过。当成没装过会每次启动都跑一遍 pip（记录要是也写不进去，
    就次次如此）——和清单本身读不了一样不补装，照实在终端说一句。真缺核心组件时 import 检查照样会装。"""
    root = _project(tmp_path)
    stamp = root / ".venv" / bootstrap.REQ_STAMP
    stamp.write_text("0" * 64 + "\n", encoding="utf-8")
    _lock(stamp)
    try:
        assert bootstrap.requirements_pending(root) is False
        assert bootstrap.requirements_pending(root) is False
        out = capsys.readouterr().out
        assert out.count(str(stamp)) == 1                       # 一个进程里说一次
        assert os.strerror(errno.EACCES) in out
    finally:
        os.chmod(str(stamp), 0o600)


def test_a_missing_or_garbled_install_record_still_reinstalls(tmp_path):
    """记录不在（从没装成功过）照旧要补装；内容不是 UTF-8 文本也照旧补装一次——装成功就写上新的，自己会好。"""
    root = _project(tmp_path)
    assert bootstrap.requirements_pending(root) is True
    (root / ".venv" / bootstrap.REQ_STAMP).write_bytes(b"\xff\xfe\x00garbage")
    assert bootstrap.recorded_requirements_digest(root) is None
    assert bootstrap.requirements_pending(root) is True
