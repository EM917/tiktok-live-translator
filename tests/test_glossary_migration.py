"""「迁移旧词表」：新架构隔离了 profile，但老用户的 glossary.txt 还残留
旧官方模板的主播专属条目，历史配置会继续污染。迁移的铁律：

    扫描 → 只认整条与旧官方模板完全一致的行 → 展示 → 用户确认 → 备份 → 迁移

绝不猜「这是不是用户自己写的」——改过一个字的行都不迁，宁可留着继续提示。
"""
import asyncio
from pathlib import Path
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


def _readonly_profiles(monkeypatch, profiles):
    """让 profiles/ 底下的写入失败，且三个平台上是同一个结果。

    不能用 os.chmod(profiles, 0o500) 造只读：chmod 在 Windows 上只映射到
    **文件**的只读属性，对目录的可写性毫无作用——副本照样建得出来、迁移照样
    返回 total=1，断言只在 Windows CI 上挂；POSIX 下以 root 跑（容器里很常见）
    同样失效，测试会绿着而铁律没人守。改成在 Python 这层抛 PermissionError
    （OSError 的子类，正是 OS 会抛的那个），profile_path() 里的 except OSError
    仍然是真跑的生产代码——比直接把 profile_path 换成返回 None 的假货强：那样
    它哪天改成抛异常而不是返回 None，测试还绿着，调用方却已经炸了。

    只拦 profiles/ 底下的写：备份和 glossary.txt 都在上一级目录，那两步必须
    保持真实，否则就测不出「失败时条目仍原样留在全局」这件事本身了。"""
    profiles = Path(profiles).resolve()
    real_write_text = Path.write_text

    def guarded(self, *args, **kwargs):
        if profiles in Path(self).resolve().parents:
            raise PermissionError("profiles/ 不可写（模拟只读目录）")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", guarded)


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


# 铁律：先写进 profile，写成功的才从全局删。写不进去时「从全局删了但没进
# profile」是数据丢失，比不迁移糟得多——条目必须原样留在全局继续生效，结果里
# 如实报 failed。「写不进去」有两条独立的路径，各钉一个用例：副本根本建不
# 出来（profile_path 返回 None），和副本在、追加失败（prof.open 抛 OSError）。


def test_profile_copy_failure_keeps_entries_in_the_global_table(monkeypatch, tmp_path):
    """副本建不出来（profiles/ 只读、盘满、被杀软锁住）→ profile_path 返回
    None，整组原样留在全局。"""
    root = _files(monkeypatch, tmp_path, TEMPLATE_LINE + "\n")
    _readonly_profiles(monkeypatch, root / "profiles")

    result = G.migrate_legacy_entries()
    assert result["total"] == 0 and result["failed"] == 1 and result["moved"] == {}
    after = (root / "glossary.txt").read_text(encoding="utf-8")
    assert TEMPLATE_LINE in after          # 条目还在，没有丢
    assert (root / result["backup"]).exists()               # 只有 profiles/ 写不进去，备份照做
    assert not (root / "profiles" / "bella.txt").exists()   # 也没留下半个副本


def test_profile_write_failure_keeps_entries_in_the_global_table(monkeypatch, tmp_path):
    """副本在、但追加失败（被别的进程独占、文件只读）→ prof.open 抛 OSError，
    整组同样原样留在全局。这条路径以前完全没有覆盖。

    用一个同名目录占住副本路径来造这个 OSError：往目录里追加必然失败，且是
    内核层面挡的，不看权限位、不看是不是 root——POSIX 抛 IsADirectoryError、
    Windows 抛 PermissionError，都是 OSError 的子类，三个平台同一个结果。"""
    root = _files(monkeypatch, tmp_path, TEMPLATE_LINE + "\n")
    prof = root / "profiles" / "bella.txt"
    prof.mkdir()

    result = G.migrate_legacy_entries()
    assert result["total"] == 0 and result["failed"] == 1 and result["moved"] == {}
    after = (root / "glossary.txt").read_text(encoding="utf-8")
    assert TEMPLATE_LINE in after          # 条目还在，没有丢
    assert (root / result["backup"]).exists()


def test_one_streamer_failing_leaves_only_that_streamer_in_the_global_table(
        monkeypatch, tmp_path):
    """失败是按主播分组算的：bella 写不进去，既不能把 bella 那条从全局删掉，
    也不能连累已经写进 elisa 副本的那条留在全局——迁了却不删就是两边都在，
    profile 优先级白设。多主播才是迁移的常态，单主播用例照不到这一层。"""
    elisa_line = "el colageno => 胶原蛋白"
    root = _files(monkeypatch, tmp_path, TEMPLATE_LINE + "\n" + elisa_line + "\n")
    (root / "profiles" / "elisa.example.txt").write_text(
        elisa_line + "\n", encoding="utf-8")
    (root / "profiles" / "bella.txt").mkdir()          # 只有 bella 写不进去

    result = G.migrate_legacy_entries()
    assert result["moved"] == {"elisa": 1}
    assert result["total"] == 1 and result["failed"] == 1
    after = (root / "glossary.txt").read_text(encoding="utf-8")
    assert TEMPLATE_LINE in after          # 失败的原样留着
    assert elisa_line not in after         # 写成功的才删
    assert elisa_line in (root / "profiles" / "elisa.txt").read_text(encoding="utf-8")


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
