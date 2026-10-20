"""测试不读、不建仓库里的真实词表：tests/conftest.py 的 _keep_glossaries_out_of_the_repo、
_keep_banned_terms_out_of_the_repo 与那张审计网。

brands/<品牌>.txt、profiles/<主播>.txt、glossary.txt、banned_terms.txt 是开发者本机的私人词表，CI 上不存在。
这里钉住：词表的落点在每个用例里都指到临时目录；直播间开场要走的那几步一步都碰不到仓库；那张网真的
看得见碰仓库路径的操作。试网用的都是不存在的文件名——既读不到真实内容，也不在仓库里留下东西。
"""
import os
from pathlib import Path

import pytest

from app import audit
from app import glossary as G
from app import pipeline as P

REPO = Path(__file__).resolve().parent.parent


def test_every_glossary_location_is_inside_pytests_tmp_dir(tmp_path_factory):
    base = tmp_path_factory.getbasetemp()
    for path in (G.GLOSSARY_FILE, G.PROFILE_DIR, G.BRAND_DIR):
        assert base in path.parents, path
        assert REPO not in path.parents, path


def test_the_global_glossary_starts_from_the_public_template_like_on_ci():
    """全局表首启从入库的公开模板复制：本机和 CI 上用例读到的是同一份内容。"""
    G.load()
    assert (G.GLOSSARY_FILE.read_text(encoding="utf-8")
            == G.GLOSSARY_EXAMPLE.read_text(encoding="utf-8-sig"))


def test_a_real_room_session_start_touches_no_repo_glossary(glossary_net):
    """Pipeline() 与 _begin_session 碰词表的每一步：扫品牌目录、按主播和品牌加载、读开关行、
    查全局表里跑错地方的条目、审计头记全局表指纹。"""
    G.brand_options()
    G.load(streamer="bellaallnatural", brand="acme")
    G.profile_path("bellaallnatural")
    G.brand_path("acme")
    G.profile_options("bellaallnatural")
    G.misplaced_entries(G.load().entries)
    audit.AuditLog(room_url="https://www.tiktok.com/@bellaallnatural/live").close()
    assert glossary_net == []


@pytest.mark.parametrize("rel, mode", [
    ("glossary.txt.bak-net-probe", "rb"),                  # 全局表和它的迁移备份
    ("profiles/net-probe.txt", "rb"),                      # 主播 profile 的用户副本
    ("brands/net-probe.txt", "rb"),                        # 品牌词表的用户副本
    ("brands/no-such-dir/net-probe.example.txt", "w"),     # 模板名也不许写
    ("banned_terms.txt.bak-net-probe", "rb"),              # 违禁词表旁边的备份、临时文件
])
def test_the_net_sees_an_open_of_a_private_glossary_path(glossary_net, rel, mode):
    with pytest.raises(FileNotFoundError):
        open(REPO / rel, mode)
    assert [(event, path) for _test, event, path in glossary_net] == [("open", rel)]


def test_the_net_sees_a_folder_being_created_in_the_repo(glossary_net):
    with pytest.raises(FileNotFoundError):
        os.mkdir(REPO / "profiles" / "no-such-dir" / "child")
    assert [(event, path) for _test, event, path in glossary_net] == [
        ("os.mkdir", "profiles/no-such-dir/child")]


def test_reading_a_tracked_readme_is_not_a_hit(glossary_net):
    """入库的说明文件和 *.example.txt 模板是公开的、本机和 CI 一样：只读不算（tests/test_readme_one_page.py
    要扫 brands/README.md）。"""
    assert (REPO / "brands" / "README.md").read_text(encoding="utf-8")
    assert glossary_net == []


# ---- 违禁词表 banned_terms.txt ----------------------------------------------------------------

def test_the_banned_terms_file_is_inside_pytests_tmp_dir(tmp_path_factory):
    assert tmp_path_factory.getbasetemp() in P.TERMS_FILE.parents
    assert REPO not in P.TERMS_FILE.parents
    assert P.TERMS_EXAMPLE == REPO / "banned_terms.example.txt"      # 模板还是入库的那份


def test_the_banned_terms_file_holds_the_public_template_like_on_ci():
    """CI 上第一次 load_detector() 从入库模板复制一份，之后的用例读的都是它：本机和 CI 读到同一份内容。"""
    assert (P.TERMS_FILE.read_text(encoding="utf-8")
            == P.TERMS_EXAMPLE.read_text(encoding="utf-8"))


def test_loading_the_default_banned_list_touches_no_repo_file(glossary_net):
    """Pipeline() 和每场开场都调 load_detector(getattr(args, "banned_terms", None))：不传路径就读 TERMS_FILE。"""
    detector = P.load_detector()
    assert detector.source_path == str(P.TERMS_FILE)
    assert detector.count == P.load_detector(P.TERMS_EXAMPLE).count
    assert glossary_net == []


def test_the_net_sees_a_file_being_moved_onto_the_banned_list(glossary_net):
    """原子写（先写临时文件再 os.replace 过去）落到 banned_terms.txt 上，也是写。源文件不存在，什么都挪不动。"""
    with pytest.raises(FileNotFoundError):
        os.replace(REPO / "no-such-file.net-probe", REPO / "banned_terms.txt")
    assert [(event, path) for _test, event, path in glossary_net] == [("os.rename", "banned_terms.txt")]


def test_reading_the_tracked_banned_terms_template_is_not_a_hit(glossary_net):
    """入库的 banned_terms.example.txt 是公开模板：只读不算（tests/test_resilience_asr.py、
    tests/test_fuzzy_recall.py 要读它）。"""
    assert (REPO / "banned_terms.example.txt").read_text(encoding="utf-8")
    assert glossary_net == []
