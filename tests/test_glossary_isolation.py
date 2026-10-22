"""测试不读、不建仓库里的真实词表：tests/conftest.py 的 _keep_glossaries_out_of_the_repo 与那张审计网。

brands/<品牌>.txt、profiles/<主播>.txt、glossary.txt 是开发者本机的私人词表，CI 上不存在。这里钉住：
词表的落点在每个用例里都指到临时目录；直播间开场要走的那几步一步都碰不到仓库；那张网真的看得见
碰仓库路径的操作。试网用的都是不存在的文件名——既读不到真实内容，也不在仓库里留下东西。
"""
import os
from pathlib import Path

import pytest

from app import audit
from app import glossary as G

REPO = Path(__file__).resolve().parent.parent


def test_every_glossary_location_is_inside_pytests_tmp_dir(tmp_path_factory):
    base = tmp_path_factory.getbasetemp()
    for path in (G.GLOSSARY_FILE, G.PROFILE_DIR, G.BRAND_DIR):
        assert base in path.parents, path
        assert REPO not in path.parents, path


def test_each_test_starts_from_a_fresh_empty_glossary_dir_outside_its_tmp_path(tmp_path):
    """全局表所在的目录已经建好、是空的（load() 首启才把模板复制进来）；profiles/、brands/ 还没建；
    这个目录不在用例自己的 tmp_path 里——有用例断言 tmp_path 里只有它自己写的文件。"""
    home = G.GLOSSARY_FILE.parent
    assert G.GLOSSARY_FILE.name == "glossary.txt"
    assert home.is_dir() and list(home.iterdir()) == []
    assert G.PROFILE_DIR == home / "profiles" and not G.PROFILE_DIR.exists()
    assert G.BRAND_DIR == home / "brands" and not G.BRAND_DIR.exists()
    assert home != tmp_path and tmp_path not in home.parents


_DIRS_SEEN = []


@pytest.mark.parametrize("n", range(3))
def test_no_two_tests_share_a_dir_and_none_pays_a_numbered_mktemp(n, tmp_path_factory):
    """每个用例的词表目录、设置目录都是它独有的；而且都开在同一个会话级父目录里，不是每个用例各做一次
    带编号的 tmp_path_factory.mktemp()——那样每次都要把 basetemp 根目录整个扫一遍，六千个用例下来是
    O(N²)，CI 的 Windows 跑器因此超过 15 分钟被取消过（tests/conftest.py 的 _fresh_dir）。"""
    from app import settings

    mine = [G.GLOSSARY_FILE.parent, settings.SETTINGS_FILE.parent]
    assert mine[0] != mine[1]
    assert not set(mine) & set(_DIRS_SEEN)
    assert {path.parent for path in _DIRS_SEEN + mine} == {mine[0].parent}
    # 每个用例各做一次 mktemp 时，这些目录也都挂在同一个父目录下——就是 basetemp 本身；要拦住改回去，得断言父目录不是它
    assert mine[0].parent != tmp_path_factory.getbasetemp()
    _DIRS_SEEN.extend(mine)


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
