"""按场选的品牌词表（brand）：handle_control 的 start 分支怎么解析品牌字段、
怎么按主播记住、怎么把完整映射回显给前端。词表本身的加载/合并/识别热词
逻辑见 tests/test_glossary_profile.py；这里只测 Pipeline 这一层的胶水——
搭最小 Pipeline 的方式照 tests/test_alert_mode.py。
"""
from types import SimpleNamespace

from app import glossary as G
from app import pipeline as pipeline_mod
from app import settings
from app.pipeline import Pipeline
from app.server import CaptionServer
from tests.helpers import run


def make_pipeline(monkeypatch, tmp_path, brand_examples=("acme",)):
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(settings, "_corrupt", {"backup": None, "announced": False})
    monkeypatch.setattr(G, "BRAND_DIR", tmp_path / "brands")
    (tmp_path / "brands").mkdir(exist_ok=True)
    for bid in brand_examples:
        (tmp_path / "brands" / (bid + ".example.txt")).write_text(
            "algo => 什么\n", encoding="utf-8")
    terms_file = tmp_path / "banned_terms.txt"
    terms_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms_file)
    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source="es",
        beam=5, context=False, asr_temperature=None, glossary=None, backend="auto",
        model=None, device="auto", compute_type="auto", denoise="off", banned_terms=None,
        comments=False)
    server = CaptionServer()
    p = Pipeline(args, server)
    p.translator = None

    async def fake_start(url, media=None):
        return None

    p.start_stream = fake_start
    return p, server


# ---------------------------------------------------------------------------
# brand 字段解析：合法 / 不合法（没有对应模板）/ 缺失 / 显式「不限」
# ---------------------------------------------------------------------------

def test_start_with_a_valid_brand_sets_it_and_saves_it_per_streamer(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    run(p.handle_control({"type": "start",
                          "url": "https://www.tiktok.com/@daisycabral_/live",
                          "brand": "acme"}))
    assert p.brand == "acme"
    assert settings.streamer_brands() == {"daisycabral_": "acme"}
    assert server.config["brands"] == {"daisycabral_": "acme"}


def test_start_with_an_invalid_brand_id_falls_back_to_unrestricted(monkeypatch, tmp_path):
    """没有对应模板/副本的 id（如拼错、或没这个品牌）一律按「不限」处理——
    和 alerts 字段缺失时的宽容原则一致，不能因为一个字段解析不出来就让开播失败。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    run(p.handle_control({"type": "start",
                          "url": "https://www.tiktok.com/@daisycabral_/live",
                          "brand": "no-such-brand"}))
    assert p.brand is None
    assert settings.streamer_brands() == {}    # 不留一条无效品牌的记录
    assert server.config["brands"] == {}


def test_start_with_a_malformed_brand_id_falls_back_to_unrestricted(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    run(p.handle_control({"type": "start",
                          "url": "https://www.tiktok.com/@daisycabral_/live",
                          "brand": "../etc/passwd"}))
    assert p.brand is None


def test_start_without_the_brand_field_defaults_to_unrestricted(monkeypatch, tmp_path):
    """缺字段（老页面缓存）同 alerts 字段一样按「不限」处理，不沿用上一场的值。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    p.brand = "acme"       # 上一场选过
    run(p.handle_control({"type": "start",
                          "url": "https://www.tiktok.com/@daisycabral_/live"}))
    assert p.brand is None


def test_start_with_empty_brand_string_records_unrestricted_and_clears_memory(
        monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    settings.save_streamer_brand("daisycabral_", "acme")    # 上次记住的
    run(p.handle_control({"type": "start",
                          "url": "https://www.tiktok.com/@daisycabral_/live",
                          "brand": ""}))
    assert p.brand is None
    assert settings.streamer_brands() == {}     # 这场改选「不限」：记住的这条被清掉
    assert server.config["brands"] == {}


def test_start_without_a_streamer_in_the_url_does_not_touch_settings(monkeypatch, tmp_path):
    """直连地址没有主播身份：品牌本场照样生效，但没法按主播记忆——不写入 settings。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    run(p.handle_control({"type": "start",
                          "url": "https://pull.example.com/live.flv",
                          "brand": "acme"}))
    assert p.brand == "acme"
    assert settings.streamer_brands() == {}


def test_switching_streamers_updates_brand_before_the_next_start(monkeypatch, tmp_path):
    """直播中收到新的 start（换主播）：同一条路径，self.brand 跟着新的一场变。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    run(p.handle_control({"type": "start",
                          "url": "https://www.tiktok.com/@daisycabral_/live",
                          "brand": "acme"}))
    assert p.brand == "acme"
    run(p.handle_control({"type": "start",
                          "url": "https://www.tiktok.com/@otherstreamer/live"}))
    assert p.brand is None


# ---------------------------------------------------------------------------
# 构造时：不给 self.brand 设默认值，只把记住的映射回显进 config 供前端挑默认值
# ---------------------------------------------------------------------------

def test_construction_seeds_config_brands_from_settings_without_defaulting_self_brand(
        monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(settings, "_corrupt", {"backup": None, "announced": False})
    settings.save_streamer_brand("bella", "acme")
    p, server = make_pipeline(monkeypatch, tmp_path)
    assert server.config["brands"] == {"bella": "acme"}
    assert not hasattr(p, "brand")


def test_construction_with_no_saved_brands_seeds_an_empty_mapping(monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path)
    assert server.config["brands"] == {}


# ---------------------------------------------------------------------------
# 「本场选的品牌文件后来被删了」：不报错，按不限处理，控制台留一行
# ---------------------------------------------------------------------------

def test_a_brand_whose_files_were_deleted_falls_back_with_a_console_note(
        monkeypatch, tmp_path, capsys):
    """场景：这个品牌之前用过（模板已经复制成 acme.txt），后来用户把 brands/
    整个清理掉了。下一次带着同一个 id 点「开始」不该报错，只在控制台留一行——
    和「id 打错/没这个品牌」共享同一条 brand_path() 返回 None 的判断。"""
    p, server = make_pipeline(monkeypatch, tmp_path)
    (tmp_path / "brands" / "acme.txt").unlink(missing_ok=True)
    (tmp_path / "brands" / "acme.example.txt").unlink()
    run(p.handle_control({"type": "start",
                          "url": "https://www.tiktok.com/@daisycabral_/live",
                          "brand": "acme"}))
    assert p.brand is None
    assert "acme" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 可选品牌列表：构造时从 brands/ 扫一遍放进 config，refresh_brands 重新扫描
# ---------------------------------------------------------------------------

def test_construction_seeds_config_brand_options_from_the_brands_directory(
        monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path, brand_examples=("acme", "zeta"))
    assert server.config["brand_options"] == [{"id": "acme", "name": "acme"},
                                               {"id": "zeta", "name": "zeta"}]


def test_refresh_brands_rescans_the_directory_and_broadcasts_config(monkeypatch, tmp_path):
    """CaptionServer（真实类，见 app/server.py）自己没有「发送记录」这种测试
    专用属性——它的 broadcast() 对 "config" 类型消息做的事就是把内容合进
    self.config，所以换一份能记录调用的 broadcast 来确认真的发出去了，
    而不是只看 handle_control 自己手动写的那份（两处理论上该一致，但只看
    后者测不出广播漏发）。"""
    p, server = make_pipeline(monkeypatch, tmp_path, brand_examples=())
    assert server.config["brand_options"] == []
    sent = []

    async def fake_broadcast(msg):
        sent.append(msg)

    monkeypatch.setattr(server, "broadcast", fake_broadcast)
    # 用户在这之后才往文件夹里放了一份新词表——构造时那次扫描当然看不到它
    (tmp_path / "brands" / "acme.example.txt").write_text("algo => 什么\n", encoding="utf-8")
    run(p.handle_control({"type": "refresh_brands"}))
    assert server.config["brand_options"] == [{"id": "acme", "name": "acme"}]
    assert len(sent) == 1 and sent[0]["type"] == "config"
    assert sent[0]["brand_options"] == [{"id": "acme", "name": "acme"}]
    assert sent[0]["brands"] == server.config["brands"]


# ---------------------------------------------------------------------------
# 「打开词表文件夹」：调用注入的 opener，不真的弹访达；确保目录存在
# ---------------------------------------------------------------------------

def test_open_brands_dir_creates_the_directory_and_calls_the_injected_opener(
        monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path, brand_examples=())
    brands_dir = tmp_path / "brands"
    import shutil
    shutil.rmtree(brands_dir)          # 模拟目录还不存在（从没选过品牌的机器）
    assert not brands_dir.exists()
    opened = []
    p._brand_dir_opener = lambda path: opened.append(path)
    # 成功路径同步返回 None（mkdir + 调用 opener 都不需要 await），
    # 直接调用而不经 run()——和 apply_update 分支的测试用法一致
    assert p.handle_control({"type": "open_brands_dir"}) is None
    assert brands_dir.exists()
    assert opened == [brands_dir]


def test_open_brands_dir_reports_failure_through_a_notice_without_raising(
        monkeypatch, tmp_path):
    p, server = make_pipeline(monkeypatch, tmp_path, brand_examples=())
    sent = []

    async def fake_broadcast(msg):
        sent.append(msg)

    monkeypatch.setattr(server, "broadcast", fake_broadcast)

    def boom(path):
        raise OSError("没有图形界面")

    p._brand_dir_opener = boom
    run(p.handle_control({"type": "open_brands_dir"}))   # 不抛异常
    notices = [m for m in sent if m.get("type") == "notice"]
    assert notices and "无法打开文件夹" in notices[-1]["text"]
