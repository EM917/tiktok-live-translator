"""settings：读-合并-原子写。这是「重启记住语言/直播间」承诺的地基。"""
import json

from app import settings


def _use_tmp(monkeypatch, tmp_path):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings, "SETTINGS_FILE", path)
    return path


def test_roundtrip(monkeypatch, tmp_path):
    path = _use_tmp(monkeypatch, tmp_path)
    settings.save_setting("target_lang", "ja")
    assert settings.load_settings() == {"target_lang": "ja"}
    assert json.loads(path.read_text(encoding="utf-8"))["target_lang"] == "ja"


def test_merge_preserves_other_keys(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    settings.save_setting("target_lang", "ja")
    settings.save_setting("room_url", "https://x/live")
    assert settings.load_settings() == {"target_lang": "ja",
                                        "room_url": "https://x/live"}


def test_missing_file_returns_empty(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    assert settings.load_settings() == {}


def test_corrupted_file_recovers(monkeypatch, tmp_path):
    path = _use_tmp(monkeypatch, tmp_path)
    path.write_text("{ not valid json", encoding="utf-8")
    assert settings.load_settings() == {}
    settings.save_setting("k", "v")            # 坏文件不阻止后续写入
    assert settings.load_settings() == {"k": "v"}


def test_non_dict_json_treated_as_empty(monkeypatch, tmp_path):
    path = _use_tmp(monkeypatch, tmp_path)
    path.write_text('["a", "b"]', encoding="utf-8")
    assert settings.load_settings() == {}


def test_no_stray_tmp_file_left(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    settings.save_setting("k", "v")
    leftovers = [p.name for p in tmp_path.iterdir()]
    assert leftovers == ["settings.json"]      # 临时文件已原子替换掉
