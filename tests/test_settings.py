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


# ---- resolve_source：CLI > 界面上次选择 > 西语（产品默认，不是 auto）----

def test_source_first_run_defaults_to_spanish():
    """首次使用不逐段猜语言：实测 auto 档一场里 22.7% 的段语言标签乱跳。"""
    assert settings.resolve_source(None, None) == "es"
    assert settings.resolve_source(None, "") == "es"


def test_source_saved_choice_wins_over_default():
    assert settings.resolve_source(None, "pt") == "pt"


def test_source_saved_auto_means_autodetect():
    """用户在界面里明确选过「自动检测」，不能被产品默认盖掉。"""
    assert settings.resolve_source(None, "auto") is None


def test_source_cli_wins_over_saved():
    assert settings.resolve_source("en", "es") == "en"


def test_source_cli_auto_normalized_to_none():
    """Whisper 的 language 参数只认语言码或 None，"auto" 字符串会炸。"""
    assert settings.resolve_source("auto", "es") is None


# ---- 最近直播间：按主播去重、新的在前、只收有主播名的 ----

def test_push_recent_room_keeps_newest_first(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    settings.push_recent_room("bella", "https://www.tiktok.com/@bella/live")
    settings.push_recent_room("jessy", "https://www.tiktok.com/@jessy/live")
    got = settings.recent_rooms()
    assert [x["streamer"] for x in got] == ["jessy", "bella"]
    assert got[0]["url"] == "https://www.tiktok.com/@jessy/live"
    assert got[0]["at"]                       # 带时间戳


def test_push_recent_room_dedups_by_streamer(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    settings.push_recent_room("bella", "https://www.tiktok.com/@bella/live")
    settings.push_recent_room("jessy", "https://www.tiktok.com/@jessy/live")
    settings.push_recent_room("bella", "https://www.tiktok.com/@bella/live?x=1")
    got = settings.recent_rooms()
    assert [x["streamer"] for x in got] == ["bella", "jessy"]   # bella 回到最前，不重复
    assert got[0]["url"].endswith("?x=1")                       # 用最新那次的地址


def test_push_recent_room_respects_limit(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    for i in range(12):
        settings.push_recent_room("s{}".format(i), "https://www.tiktok.com/@s{}/live".format(i))
    got = settings.recent_rooms()
    assert len(got) == settings.RECENT_ROOMS_MAX
    assert got[0]["streamer"] == "s11"        # 最新的在最前


def test_push_recent_room_ignores_entries_without_a_streamer(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    settings.push_recent_room("", "https://cdn.example/a.flv")   # 直连地址没有主播身份
    settings.push_recent_room("bella", "")                        # 缺地址
    assert settings.recent_rooms() == []


def test_recent_rooms_skips_corrupt_entries(monkeypatch, tmp_path):
    path = _use_tmp(monkeypatch, tmp_path)
    path.write_text(json.dumps({"recent_rooms": [
        {"streamer": "ok", "url": "https://www.tiktok.com/@ok/live", "at": "t"},
        {"streamer": "", "url": "x"},          # 无主播名，丢弃
        "not a dict",                          # 类型不对，丢弃
        {"nope": 1},                           # 缺字段，丢弃
    ]}), encoding="utf-8")
    got = settings.recent_rooms()
    assert [x["streamer"] for x in got] == ["ok"]


def test_recent_rooms_empty_when_missing(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    assert settings.recent_rooms() == []
