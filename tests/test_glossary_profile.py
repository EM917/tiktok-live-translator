"""主播专属词表（profiles/<主播>.txt）。

商品知识是逐主播的：把 Bella 的品名装进全局表，Elisa 的直播里就会凭空冒出
别家商品——盲评实测把「esta limonada → 瘦身柠檬水」判成了捏造。全局表只放
任何西语带货直播都成立的词条，品牌与商品线按直播间地址自动加载。
"""
from app import glossary as G


def _files(monkeypatch, tmp_path, global_text, profiles=None):
    monkeypatch.setattr(G, "GLOSSARY_FILE", tmp_path / "glossary.txt")
    monkeypatch.setattr(G, "GLOSSARY_EXAMPLE", tmp_path / "none.example.txt")
    monkeypatch.setattr(G, "PROFILE_DIR", tmp_path / "profiles")
    (tmp_path / "glossary.txt").write_text(global_text, encoding="utf-8")
    (tmp_path / "profiles").mkdir()
    for name, text in (profiles or {}).items():
        (tmp_path / "profiles" / name).write_text(text, encoding="utf-8")


def test_profile_entries_win_over_global(monkeypatch, tmp_path):
    """同一个西语写法两边都有时，profile 赢——必须做到变体级：matching()
    会收集所有命中条目，两边各挂一个中文的话会同时进提示词互相打架。"""
    _files(monkeypatch, tmp_path,
           "la limonada => 柠檬水\nel carrito => 小黄车\n",
           {"bella.txt": "la limonada | skinny lemonade => 瘦身柠檬水\n"})
    g = G.load(streamer="bella")
    hits = g.matching("me encanta la limonada")
    assert [(h[0], h[1]) for h in hits] == [("la limonada", "瘦身柠檬水")]
    assert g.matching("todo en el carrito")[0][1] == "小黄车"   # 全局条目仍在


def test_without_a_profile_nothing_changes(monkeypatch, tmp_path):
    _files(monkeypatch, tmp_path, "el carrito => 小黄车\n")
    g = G.load(streamer="nadie")
    assert g.matching("el carrito")[0][1] == "小黄车"
    assert len(g.entries) == 1


def test_first_use_copies_the_profile_example(monkeypatch, tmp_path):
    """和 glossary.txt 一样：模板入库、副本不入库，用户编辑副本。"""
    _files(monkeypatch, tmp_path, "x => y\n")
    (tmp_path / "profiles" / "bella.example.txt").write_text(
        "D3 K2 => D3 K2 维生素滴剂\n", encoding="utf-8")
    path = G.profile_path("bella")
    assert path == tmp_path / "profiles" / "bella.txt"
    assert path.exists()
    assert G.load(streamer="bella").matching("D3 K2")[0][1] == "D3 K2 维生素滴剂"


def test_streamer_name_cannot_escape_the_profile_dir(monkeypatch, tmp_path):
    """主播名来自直播间 URL，是外部输入——不能被拼进路径逃出 profiles/。"""
    _files(monkeypatch, tmp_path, "x => y\n")
    for evil in ("../../etc/passwd", "a/b", "", None):
        p = G.profile_path(evil)
        assert p is None or p.parent == tmp_path / "profiles"


def test_misplaced_entries_flag_brand_leftovers(monkeypatch, tmp_path):
    """老版全局模板的品牌条目留在用户 glossary.txt 里时要被点名——
    模板升级不会改用户已经复制出去的那份，不提示就永远修不掉。"""
    _files(monkeypatch, tmp_path, "x => y\n")
    (tmp_path / "profiles" / "bella.example.txt").write_text(
        "D3 K2 | de tres ka dos => D3 K2 维生素滴剂\n", encoding="utf-8")
    entries = G.parse("D3 K2 => D3 K2 维生素滴剂\nel carrito => 小黄车\n")
    out = G.misplaced_entries(entries)
    assert out == [("bella", "D3 K2", "D3 K2 维生素滴剂")]


def test_same_variant_different_meaning_is_not_misplaced(monkeypatch, tmp_path):
    """判据取精确档：中文也要一致。用户自己把同一个词映射成别的意思，
    那是刻意为之，不该被当成遗留品牌条目。"""
    _files(monkeypatch, tmp_path, "x => y\n")
    (tmp_path / "profiles" / "bella.example.txt").write_text(
        "la limonada => 瘦身柠檬水\n", encoding="utf-8")
    entries = G.parse("la limonada => 柠檬水\n")
    assert G.misplaced_entries(entries) == []


def test_profile_options_gate_per_streamer_behaviour(monkeypatch, tmp_path):
    """vocative_strip 这类行为按主播验证后才开：同一份称呼名单在不同主播
    身上触发率差 60 倍，没验证过的主播必须默认关。开关写在 profile 里，
    parse() 认不出它（没有 =>），词条解析不受影响。"""
    _files(monkeypatch, tmp_path, "x => y\n",
           {"susan.txt": "vocative_strip: on\nla crema => 面霜\n",
            "bella.txt": "la limpieza => 排毒粉\n"})
    assert G.profile_options("susan") == {"vocative_strip": True}
    assert G.profile_options("bella") == {}
    assert G.profile_options("nadie") == {}
    # 开关行不会被当成词条
    assert len(G.load(streamer="susan").entries) == 2   # la crema + 全局 x


def test_pipeline_strips_only_when_the_profile_says_so(monkeypatch, tmp_path):
    from app.pipeline import Pipeline

    p = Pipeline.__new__(Pipeline)
    p.glossary = None
    line = "llevar tres productos mi niña"
    p._vocative_strip = False
    assert p._for_translation(line)[0] == line          # 默认：原样送翻
    p._vocative_strip = True
    assert p._for_translation(line)[0] == "llevar tres productos"


def test_fingerprint_tracks_content_not_identity(monkeypatch, tmp_path):
    a = G.parse("la limpieza => 排毒粉\n")
    b = G.parse("la limpieza => 排毒粉\n")
    c = G.parse("la limpieza => 清洁\n")
    assert G.fingerprint(a) == G.fingerprint(b)
    assert G.fingerprint(a) != G.fingerprint(c)


def test_active_glossary_roundtrip(monkeypatch, tmp_path):
    """DeepL 的原生术语表从 active() 拿词表——会话开始时 set_active 的
    必须是合并后的那份，没设置过则退回全局。"""
    _files(monkeypatch, tmp_path, "el carrito => 小黄车\n")
    monkeypatch.setattr(G, "_ACTIVE", None)
    assert G.active().matching("el carrito")           # 回退到全局
    merged = G.load(streamer="nadie")
    G.set_active(merged)
    assert G.active() is merged
    monkeypatch.setattr(G, "_ACTIVE", None)            # 别泄漏到其他测试
