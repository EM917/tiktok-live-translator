"""主播专属词表（profiles/<主播>.txt）。

商品知识是逐主播的：把 Bella 的品名装进全局表，Elisa 的直播里就会凭空冒出
别家商品——盲评实测把「esta limonada → 瘦身柠檬水」判成了捏造。全局表只放
任何西语带货直播都成立的词条，品牌与商品线按直播间地址自动加载。
"""
from app import glossary as G


def _files(monkeypatch, tmp_path, global_text, profiles=None, brands=None):
    monkeypatch.setattr(G, "GLOSSARY_FILE", tmp_path / "glossary.txt")
    monkeypatch.setattr(G, "GLOSSARY_EXAMPLE", tmp_path / "none.example.txt")
    monkeypatch.setattr(G, "PROFILE_DIR", tmp_path / "profiles")
    monkeypatch.setattr(G, "BRAND_DIR", tmp_path / "brands")
    (tmp_path / "glossary.txt").write_text(global_text, encoding="utf-8")
    (tmp_path / "profiles").mkdir()
    (tmp_path / "brands").mkdir()
    for name, text in (profiles or {}).items():
        (tmp_path / "profiles" / name).write_text(text, encoding="utf-8")
    for name, text in (brands or {}).items():
        (tmp_path / "brands" / name).write_text(text, encoding="utf-8")


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


# ---------------------------------------------------------------------------
# 品牌词表（brands/<品牌>.txt）：本场「只卖这一个牌子」时按需加载
# ---------------------------------------------------------------------------

def test_three_layer_priority_profile_over_brand_over_global(monkeypatch, tmp_path):
    """合并顺序 profile > 品牌 > 全局：三层都挂同一个写法时，profile 赢；
    品牌与全局重叠时品牌赢；三层互不重叠的条目都照常生效。"""
    _files(monkeypatch, tmp_path,
           "la crema => 面霜\nel carrito => 小黄车\n",
           profiles={"bella.txt": "la crema => 主播特供面霜\n"},
           brands={"acme.txt": "la crema | crema acme => ACME 面霜\n"
                                "el carrito => ACME 小黄车\n"})
    g = G.load(streamer="bella", brand="acme")
    assert g.matching("la crema")[0][1] == "主播特供面霜"      # profile 赢
    assert g.matching("el carrito")[0][1] == "ACME 小黄车"     # 品牌赢过全局
    # 品牌独有的变体也生效
    assert g.matching("crema acme")[0][1] == "ACME 面霜"


def test_brand_wins_over_global_but_loses_to_profile_without_overlap(monkeypatch, tmp_path):
    _files(monkeypatch, tmp_path, "el jabon => 肥皂\n",
           brands={"acme.txt": "el jabon => ACME 肥皂\nla vela => 蜡烛\n"})
    g = G.load(brand="acme")
    assert g.matching("el jabon")[0][1] == "ACME 肥皂"
    assert g.matching("la vela")[0][1] == "蜡烛"


def test_brand_none_or_empty_leaves_entries_fingerprint_and_asr_prompt_unchanged(
        monkeypatch, tmp_path):
    """brand 为 None/空 时，条目、fingerprint、asr_prompt 必须与不传 brand
    逐字相同——这是三层合并这次改动最不能破坏的底线。

    profile 与全局故意挂同一个写法（la crema）：品牌层为空时 asr_entries 走
    的必须还是 entries 本身（_merge 去重后的结果），而不是三段拼接——两条
    路径在无重叠 fixture 下会凑巧算出同一个值，光靠「和另一次 load() 调用
    比较」测不出这类回归，必须钉一个字面预期。"""
    _files(monkeypatch, tmp_path, "la crema => 面霜\nel carrito => 小黄车\n",
           profiles={"bella.txt": "la crema => 主播特供面霜\nD3 K2 => D3 K2 滴剂\n"},
           brands={"acme.txt": "algo => 别的东西\n"})
    expected_entries = [(["la crema", "tu crema", "tus crema", "su crema",
                          "sus crema", "mi crema", "mis crema", "nuestro crema",
                          "nuestra crema"], "主播特供面霜"),
                        (["D3 K2"], "D3 K2 滴剂"),
                        (["el carrito", "tu carrito", "tus carrito", "su carrito",
                          "sus carrito", "mi carrito", "mis carrito",
                          "nuestro carrito", "nuestra carrito"], "小黄车")]
    expected_prompt = "Productos: la crema, D3 K2, el carrito."
    baseline = G.load(streamer="bella")
    assert baseline.entries == expected_entries
    assert baseline.asr_prompt() == expected_prompt
    for brand in (None, ""):
        g = G.load(streamer="bella", brand=brand)
        assert g.entries == baseline.entries == expected_entries
        assert G.fingerprint(g.entries) == G.fingerprint(baseline.entries)
        assert g.asr_prompt() == baseline.asr_prompt() == expected_prompt


def test_brand_path_rejects_bad_ids_without_creating_anything(monkeypatch, tmp_path):
    _files(monkeypatch, tmp_path, "x => y\n")
    for bad in ("../x", "Bella", "", None, "bella_all_natural", "a" * 41,
                "-bella", "bella-"):
        assert G.brand_path(bad) is None
    assert list((tmp_path / "brands").iterdir()) == []   # 没建出任何文件


def test_brand_path_first_use_copies_the_example_template(monkeypatch, tmp_path):
    _files(monkeypatch, tmp_path, "x => y\n")
    (tmp_path / "brands" / "acme.example.txt").write_text(
        "la crema => ACME 面霜\n", encoding="utf-8")
    path = G.brand_path("acme")
    assert path == tmp_path / "brands" / "acme.txt"
    assert path.exists()
    assert G.load(brand="acme").matching("la crema")[0][1] == "ACME 面霜"


def test_asr_hotwords_take_brands_first_three_skipping_profile_claimed_variants(
        monkeypatch, tmp_path):
    """热词顺序 profile → 品牌前 BRAND_ASR_TERMS 条 → 全局；品牌第 4 条不进热词
    （但仍正常参与 matching）；一条的变体整个被 profile 收过就跳过这一条；
    没被收全的条目取第一个没被收过的变体。"""
    monkeypatch.setattr(G, "BRAND_ASR_TERMS", 3)
    _files(monkeypatch, tmp_path, "de vuelta => 库存单位\n",
           profiles={"bella.txt": "b1 nombre | b1 alias => 品一\n"},
           brands={"acme.txt":
                   "b1 nombre | b1 alias => 品一\n"     # 变体全被 profile 收过：跳过
                   "b2 alias1 | b2 alias2 => 品二\n"    # 第一变体没被收过：取 b2 alias1
                   "b3 nombre => 品三\n"                 # 第 3 条，进热词
                   "b4 nombre => 品四\n"})               # 第 4 条，不进热词
    g = G.load(streamer="bella", brand="acme")
    prompt = g.asr_prompt(limit=G.MAX_ASR_TERMS)
    assert prompt == "Productos: b1 nombre, b2 alias1, b3 nombre, de vuelta."
    assert "b4 nombre" not in prompt
    # matching() 不受热词截断限制：品牌第 4 条照常生效
    assert g.matching("b4 nombre")[0][1] == "品四"


# ---------------------------------------------------------------------------
# brand_options()：下拉框可选品牌由 brands/ 目录内容决定，不写死在前端
# ---------------------------------------------------------------------------

def test_brand_options_lists_txt_and_example_files_sorted_by_name(monkeypatch, tmp_path):
    _files(monkeypatch, tmp_path, "x => y\n")
    (tmp_path / "brands" / "zeta.example.txt").write_text("algo => 什么\n", encoding="utf-8")
    (tmp_path / "brands" / "acme.example.txt").write_text(
        "name: Acme 优选\nalgo => 什么\n", encoding="utf-8")
    assert G.brand_options() == [{"id": "acme", "name": "Acme 优选"},
                                 {"id": "zeta", "name": "zeta"}]


def test_brand_options_name_line_ignores_trailing_comment(monkeypatch, tmp_path):
    _files(monkeypatch, tmp_path, "x => y\n")
    (tmp_path / "brands" / "acme.example.txt").write_text(
        "name: Acme 优选  # 备注\nalgo => 什么\n", encoding="utf-8")
    assert G.brand_options() == [{"id": "acme", "name": "Acme 优选"}]


def test_brand_options_user_file_wins_over_example_for_the_same_id(monkeypatch, tmp_path):
    """同一个 id 两份都有时，用户可能已经改过显示名——用户文件优先。"""
    _files(monkeypatch, tmp_path, "x => y\n")
    (tmp_path / "brands" / "acme.example.txt").write_text(
        "name: 模板名\nalgo => 什么\n", encoding="utf-8")
    (tmp_path / "brands" / "acme.txt").write_text(
        "name: 我改过的名字\nalgo => 什么\n", encoding="utf-8")
    assert G.brand_options() == [{"id": "acme", "name": "我改过的名字"}]


def test_brand_options_ignores_invalid_ids_and_non_txt_files(monkeypatch, tmp_path):
    """README.md 这类说明文档、大写/下划线这类不合法 id，一律不出现在下拉框里
    ——校验规则和 brand_path() 完全一样。"""
    _files(monkeypatch, tmp_path, "x => y\n")
    (tmp_path / "brands" / "README.md").write_text("说明", encoding="utf-8")
    (tmp_path / "brands" / "Bad_ID.txt").write_text("algo => 什么\n", encoding="utf-8")
    (tmp_path / "brands" / "-leading.txt").write_text("algo => 什么\n", encoding="utf-8")
    (tmp_path / "brands" / "trailing-.txt").write_text("algo => 什么\n", encoding="utf-8")
    assert G.brand_options() == []


def test_brand_options_skips_an_unreadable_file_without_blocking_the_rest(
        monkeypatch, tmp_path, capsys):
    _files(monkeypatch, tmp_path, "x => y\n")
    (tmp_path / "brands" / "acme.example.txt").write_text("algo => 什么\n", encoding="utf-8")
    bad = tmp_path / "brands" / "broken.example.txt"
    bad.write_bytes(b"\xff\xfe\xff\xfe")      # 不是合法 UTF-8，读取会炸
    options = G.brand_options()
    assert options == [{"id": "acme", "name": "acme"}]
    assert "broken" in capsys.readouterr().out


def test_brand_options_empty_when_brands_dir_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(G, "BRAND_DIR", tmp_path / "no-such-dir")
    assert G.brand_options() == []


def test_brand_options_empty_when_directory_has_no_valid_brand_files(monkeypatch, tmp_path):
    _files(monkeypatch, tmp_path, "x => y\n")
    assert G.brand_options() == []
