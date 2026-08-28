"""「迁移旧词表」：新架构隔离了 profile，但老用户的 glossary.txt 还残留
旧官方模板的主播专属条目，历史配置会继续污染。迁移的铁律：

    扫描 → 只认整条与旧官方模板完全一致的行 → 展示 → 用户确认 → 备份 → 迁移

绝不猜「这是不是用户自己写的」——改过一个字的行都不迁，宁可留着继续提示。
"""
import asyncio
from types import SimpleNamespace

from app import glossary as G

TEMPLATE_LINE = "la limpieza | la limpiecita => 排毒粉"


def _files(monkeypatch, tmp_path, global_text):
    monkeypatch.setattr(G, "GLOSSARY_FILE", tmp_path / "glossary.txt")
    monkeypatch.setattr(G, "GLOSSARY_EXAMPLE", tmp_path / "no.example.txt")
    monkeypatch.setattr(G, "PROFILE_DIR", tmp_path / "profiles")
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "bella.example.txt").write_text(
        TEMPLATE_LINE + "\nD3 K2 => D3 K2 维生素滴剂\n", encoding="utf-8")
    (tmp_path / "glossary.txt").write_text(global_text, encoding="utf-8")
    return tmp_path


def test_plan_only_matches_entries_identical_to_the_template(monkeypatch, tmp_path):
    _files(monkeypatch, tmp_path,
           "# 我的注释\n"
           "el carrito => 小黄车\n"                       # 用户自己的：不动
           + TEMPLATE_LINE + "   # 用户加的备注\n"          # 整条同模板：迁（备注无碍）
           "la limpieza => 大扫除\n"                       # 中文改过：不迁
           "la limpieza => 排毒粉\n")                      # 变体集不同：不迁
    plan = G.migration_plan()
    assert [p["streamer"] for p in plan] == ["bella"]
    assert plan[0]["display"].startswith("la limpieza")


def test_migrate_backs_up_and_preserves_everything_else(monkeypatch, tmp_path):
    root = _files(monkeypatch, tmp_path,
                  "# 我的注释\nel carrito => 小黄车\n" + TEMPLATE_LINE + "\n")
    result = G.migrate_legacy_entries()
    assert result["total"] == 1 and result["moved"] == {"bella": 1}

    backups = list(root.glob("glossary.txt.bak-*"))
    assert len(backups) == 1
    assert TEMPLATE_LINE in backups[0].read_text(encoding="utf-8")  # 备份是迁移前

    after = (root / "glossary.txt").read_text(encoding="utf-8")
    assert TEMPLATE_LINE not in after
    assert "# 我的注释" in after and "el carrito => 小黄车" in after

    # profile 副本由模板生成、本来就含该条 → 不重复追加
    prof = (root / "profiles" / "bella.txt").read_text(encoding="utf-8")
    assert prof.count("la limpieza | la limpiecita") == 1

    assert G.migrate_legacy_entries() is None      # 幂等：第二次无事可做
    assert len(list(root.glob("glossary.txt.bak-*"))) == 1   # 也不再产生备份


def test_migrate_appends_when_the_profile_lacks_the_entry(monkeypatch, tmp_path):
    root = _files(monkeypatch, tmp_path, TEMPLATE_LINE + "\n")
    # 用户的 profile 副本已存在但被删掉了这一条
    (root / "profiles" / "bella.txt").write_text("otra cosa => 别的\n",
                                                 encoding="utf-8")
    G.migrate_legacy_entries()
    prof = (root / "profiles" / "bella.txt").read_text(encoding="utf-8")
    assert TEMPLATE_LINE in prof
    assert "otra cosa => 别的" in prof             # 用户内容原样保留


def test_profile_write_failure_keeps_entries_in_the_global_table(monkeypatch, tmp_path):
    """铁律：先写进 profile，写成功的才从全局删。profiles/ 写不进去时，
    「从全局删了但没进 profile」是数据丢失——条目必须原样留在全局继续生效，
    结果里如实报 failed。"""
    import os

    root = _files(monkeypatch, tmp_path, TEMPLATE_LINE + "\n")
    profiles = root / "profiles"
    os.chmod(profiles, 0o500)              # 只读：副本建不出来
    try:
        result = G.migrate_legacy_entries()
    finally:
        os.chmod(profiles, 0o700)
    assert result["total"] == 0 and result["failed"] == 1
    after = (root / "glossary.txt").read_text(encoding="utf-8")
    assert TEMPLATE_LINE in after          # 条目还在，没有丢


def test_no_plan_touches_nothing(monkeypatch, tmp_path):
    root = _files(monkeypatch, tmp_path, "el carrito => 小黄车\n")
    before = (root / "glossary.txt").read_text(encoding="utf-8")
    assert G.migrate_legacy_entries() is None
    assert (root / "glossary.txt").read_text(encoding="utf-8") == before
    assert not list(root.glob("glossary.txt.bak-*"))


def test_pipeline_flow_previews_then_migrates_on_confirm(monkeypatch, tmp_path):
    from app.pipeline import Pipeline

    _files(monkeypatch, tmp_path, TEMPLATE_LINE + "\nel carrito => 小黄车\n")

    class FakeServer:
        def __init__(self):
            self.sent = []
            self.config = {}

        async def broadcast(self, msg):
            self.sent.append(msg)

    p = Pipeline.__new__(Pipeline)
    p.server = FakeServer()
    p.args = SimpleNamespace(glossary=None)
    p.glossary = G.load()
    p.detector = None

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(p._migrate_glossary(False))     # 预览
        plan_msg = [m for m in p.server.sent
                    if m.get("stage") == "plan"][0]
        assert len(plan_msg["entries"]) == 1                    # 只列可安全迁的

        p.server.config["glossary_migration"] = {"count": 1}    # 模拟入口在挂
        loop.run_until_complete(p._migrate_glossary(True))      # 确认执行
        done = [m for m in p.server.sent if m.get("stage") == "done"][0]
        assert done["result"]["total"] == 1
        assert done["result"]["failed"] == 0
        # 热重载：全局表里不再有 bella 的条目
        assert not [1 for vs, zh in p.glossary.entries if zh == "排毒粉"]
        # 迁完撤掉入口（重连后 hello 不再带它）
        assert "glossary_migration" not in p.server.config
    finally:
        loop.close()
