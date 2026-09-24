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
    """坏文件不阻止后续写入——但原文件要先改名备份：以前下一次保存直接盖掉它，
    手填的 DeepL 密钥、引擎选择、最近直播间全没了，连个副本都不剩。"""
    monkeypatch.setattr(settings, "_corrupt", {"backup": None, "announced": False})
    path = _use_tmp(monkeypatch, tmp_path)
    path.write_text("{ not valid json", encoding="utf-8")
    assert settings.load_settings() == {}
    backups = list(tmp_path.glob("settings.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{ not valid json"
    settings.save_setting("k", "v")            # 坏文件不阻止后续写入
    assert settings.load_settings() == {"k": "v"}
    assert backups[0].exists()                 # 备份没有被这次保存碰到


def test_non_dict_json_treated_as_empty(monkeypatch, tmp_path):
    path = _use_tmp(monkeypatch, tmp_path)
    path.write_text('["a", "b"]', encoding="utf-8")
    assert settings.load_settings() == {}


def test_no_stray_tmp_file_left(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    settings.save_setting("k", "v")
    leftovers = [p.name for p in tmp_path.iterdir()]
    assert leftovers == ["settings.json"]      # 临时文件已原子替换掉


# ---- resolve_source：CLI > 界面上次选择 > 西语+英语（产品默认，不是 auto）----

def test_source_first_run_defaults_to_spanish_and_english():
    """首次使用不逐段猜语言：实测 auto 档一场里 22.7% 的段语言标签乱跳。
    默认是列表形式 "es,en" 而不是单纯 "es"——带货主播西语夹英语是常态，
    列表形式仍然只在这两种语言里自动检测，不会被贴上无关语言标签。"""
    assert settings.resolve_source(None, None) == "es,en"
    assert settings.resolve_source(None, "") == "es,en"


def test_source_list_value_round_trips():
    """列表形式原样传下去，不在这一层校验/拆解（见 app.asr._parse_language_spec）。"""
    assert settings.resolve_source(None, "es,en") == "es,en"
    assert settings.resolve_source("ja,ko,ru,ar", "es") == "ja,ko,ru,ar"


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


# ---- 按主播记住的品牌词表：空串="不限"（删键）、类型防御 --------------------

def test_save_streamer_brand_roundtrips_and_lowercases_the_key(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    settings.save_streamer_brand("DaisyCabral_", "acme")
    assert settings.streamer_brands() == {"daisycabral_": "acme"}


def test_save_streamer_brand_with_empty_string_deletes_the_key(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    settings.save_streamer_brand("bella", "acme")
    settings.save_streamer_brand("itzesantana11", "acme")
    settings.save_streamer_brand("bella", "")       # 改选「不限」
    got = settings.streamer_brands()
    assert "bella" not in got                       # 不留一条「不限」的记录
    assert got == {"itzesantana11": "acme"}


def test_save_streamer_brand_preserves_other_settings_keys(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    settings.save_setting("target_lang", "ja")
    settings.save_streamer_brand("bella", "acme")
    assert settings.load_settings()["target_lang"] == "ja"


def test_save_streamer_brand_without_a_streamer_is_a_noop(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    settings.save_streamer_brand("", "acme")
    assert settings.streamer_brands() == {}


def test_streamer_brands_defends_against_bad_shapes(monkeypatch, tmp_path):
    """settings.json 可能被手改过：整个值不是 dict，或某个键/值类型不对，
    都要被静默滤掉而不是让读取本身炸掉。"""
    path = _use_tmp(monkeypatch, tmp_path)
    path.write_text(json.dumps({"brands": "not a dict"}), encoding="utf-8")
    assert settings.streamer_brands() == {}

    # JSON 对象的键解析出来总是字符串，值的类型才可能跑偏（如手改成了数字）
    path.write_text(json.dumps({"brands": {"bella": "acme",
                                           "elisa": 7}}), encoding="utf-8")
    assert settings.streamer_brands() == {"bella": "acme"}


def test_streamer_brands_empty_when_missing(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    assert settings.streamer_brands() == {}
