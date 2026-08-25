"""DeepL 原生术语表。

存在的理由是一次实测：同一批 60 句真实字幕，不挂术语表的 DeepL 词表遵从率
只有 26.5%，挂上是 91.8%（本地 Hy-MT2 7B 是 87.4%、慢 3.7 倍）。差的那些
全是商品名——对一个违禁词监听器来说，商品名翻错就等于这条引擎不能用。

这一层容易写反的地方全在这里守着：免费版只有一个术语表槽位（先删后建）、
只能删自己建的表、繁体不挂表、源语言认第一个。
"""
import asyncio

import pytest

from app.translator import DeepLTranslator


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


@pytest.fixture
def tr(monkeypatch):
    monkeypatch.setenv("DEEPL_API_KEY", "test-key:fx")
    return DeepLTranslator()


class FakeAPI:
    """记录每一次调用，按脚本回应。测试只关心调用序列和请求体。"""

    def __init__(self, glossaries=None, create_status=201, translate_status=200):
        self.calls = []
        self.glossaries = list(glossaries or [])
        self.create_status = create_status
        self.translate_status = translate_status
        self._next_id = 0

    async def __call__(self, method, path, form=None, body=None):
        # 记副本：被测代码若原地改请求体，历史会被篡改，测试就看不出来了
        self.calls.append((method, path, form,
                           dict(body) if isinstance(body, dict) else body))
        if path == "/v2/glossaries" and method == "GET":
            return 200, {"glossaries": self.glossaries}
        if path == "/v2/glossaries" and method == "POST":
            if self.create_status >= 400:
                return self.create_status, {"message": "Too many glossaries"}
            self._next_id += 1
            gid = "gid-%d" % self._next_id
            self.glossaries.append({"glossary_id": gid, "name": form["name"],
                                    "ready": True, "entry_count": 3})
            return self.create_status, {"glossary_id": gid, "entry_count": 3}
        if method == "DELETE":
            gid = path.rsplit("/", 1)[-1]
            self.glossaries = [g for g in self.glossaries if g["glossary_id"] != gid]
            return 204, {}
        if path == "/v2/translate":
            st = self.translate_status
            if st != 200:
                self.translate_status = 200      # 只失败一次，便于测重试
                return st, {"message": "bad glossary"}
            return 200, {"translations": [{"text": "译文"}]}
        raise AssertionError("没预料到的调用 " + path)

    def bodies(self):
        return [b for m, p, f, b in self.calls if p == "/v2/translate"]

    def seq(self):
        return [(m, p.split("/v2/")[-1].split("/")[0]) for m, p, f, b in self.calls]


# ---- TSV 拼装 ----------------------------------------------------------

def test_every_variant_becomes_its_own_row():
    """DeepL 只做字面匹配，`la limpieza` 和 `la limpiecita` 对它是两个词。"""
    tsv = DeepLTranslator.glossary_tsv([(["la limpieza", "el detox"], "排毒粉")])
    assert tsv.splitlines() == ["la limpieza\t排毒粉", "el detox\t排毒粉"]


def test_duplicate_source_terms_are_dropped_case_insensitively():
    """源词重复会让整表创建失败——必须去重，且先出现的赢（词表里靠前的优先）。"""
    tsv = DeepLTranslator.glossary_tsv([(["Gotas"], "维生素滴剂"),
                                        (["gotas"], "别的译法")])
    assert tsv.splitlines() == ["Gotas\t维生素滴剂"]


def test_rows_with_tabs_or_newlines_are_skipped():
    """TSV 里混进制表符/换行会把整张表错位。"""
    tsv = DeepLTranslator.glossary_tsv([(["a\tb"], "甲"), (["c"], "乙\n丙"),
                                        (["d"], "  "), (["e"], "戊")])
    assert tsv.splitlines() == ["e\t戊"]


def test_the_name_carries_a_fingerprint_of_the_contents(tr):
    """改了 glossary.txt 就该是另一个名字，否则会拿着过期的表继续用。"""
    a = tr.glossary_name("es", "zh", "x\t甲")
    b = tr.glossary_name("es", "zh", "x\t乙")
    assert a != b
    assert a == tr.glossary_name("es", "zh", "x\t甲")
    assert a.startswith(DeepLTranslator.GLOSSARY_PREFIX + "es-zh-")


# ---- 建表 / 复用 / 删表 -------------------------------------------------

def test_an_existing_matching_glossary_is_reused_without_creating(tr, monkeypatch):
    api = FakeAPI()
    monkeypatch.setattr(tr, "_api", api)
    first = run(tr._ensure_glossary("es", "zh-CN"))
    api.calls.clear()

    second = DeepLTranslator.__new__(DeepLTranslator)
    second.__init__()
    monkeypatch.setattr(second, "_api", api)
    assert run(second._ensure_glossary("es", "zh-CN")) == first
    assert ("POST", "glossaries") not in api.seq()


def test_stale_glossaries_are_deleted_before_creating_not_after(tr, monkeypatch):
    """免费版只放得下 1 个表（实测建第 2 个直接 456）。顺序写反就永远建不出来。"""
    api = FakeAPI(glossaries=[{"glossary_id": "old", "name": "tlt-es-zh-deadbeef",
                               "ready": True}])
    monkeypatch.setattr(tr, "_api", api)
    run(tr._ensure_glossary("es", "zh-CN"))
    seq = api.seq()
    assert seq.index(("DELETE", "glossaries")) < seq.index(("POST", "glossaries"))


def test_glossaries_we_did_not_create_are_never_deleted(tr, monkeypatch):
    """用户在 DeepL 后台自建的表不能被我们清掉。"""
    api = FakeAPI(glossaries=[{"glossary_id": "theirs", "name": "客户自己的表",
                               "ready": True}])
    monkeypatch.setattr(tr, "_api", api)
    run(tr._ensure_glossary("es", "zh-CN"))
    assert "theirs" in [g["glossary_id"] for g in api.glossaries]
    assert not [c for c in api.calls if c[0] == "DELETE"]


def test_a_second_source_language_does_not_evict_the_first(tr, monkeypatch):
    """只有一个槽位。直播里蹦出一句被判成英语的字幕，不能把西语表挤掉。"""
    api = FakeAPI()
    monkeypatch.setattr(tr, "_api", api)
    es = run(tr._ensure_glossary("es", "zh-CN"))
    assert run(tr._ensure_glossary("en", "zh-CN")) is None
    assert [g["glossary_id"] for g in api.glossaries] == [es]


def test_traditional_chinese_gets_no_native_glossary(tr, monkeypatch):
    """术语表只有一个 zh，而词表里的译法是简体——实测挂上去会把简体词塞进
    繁体译文（"quiero las gotas" → "我想要维生素滴剂"）。"""
    api = FakeAPI()
    monkeypatch.setattr(tr, "_api", api)
    assert run(tr._ensure_glossary("es", "zh-TW")) is None
    assert not api.calls


def test_a_failed_creation_is_not_retried_on_every_caption(tr, monkeypatch):
    """建不起来是常态（额度、权限、语言对）。不能每句字幕都去撞一次。"""
    api = FakeAPI(create_status=456)
    monkeypatch.setattr(tr, "_api", api)
    assert run(tr._ensure_glossary("es", "zh-CN")) is None
    n = len(api.calls)
    assert run(tr._ensure_glossary("es", "zh-CN")) is None
    assert len(api.calls) == n


# ---- 翻译请求 ----------------------------------------------------------

def test_translate_attaches_the_glossary_when_the_source_is_known(tr, monkeypatch):
    api = FakeAPI()
    monkeypatch.setattr(tr, "_api", api)
    assert run(tr.translate("hola", "zh-CN", source="es")) == "译文"
    body = api.bodies()[0]
    assert body["glossary_id"] and body["source_lang"] == "ES"


def test_auto_source_means_no_glossary(tr, monkeypatch):
    """DeepL 的术语表必须带明确 source_lang。源语言未知时宁可不挂，
    也不能瞎猜成西语——猜错了整句都会被按西语硬解。"""
    api = FakeAPI()
    monkeypatch.setattr(tr, "_api", api)
    assert run(tr.translate("hola", "zh-CN", source="auto")) == "译文"
    assert "glossary_id" not in api.bodies()[0]


def test_a_deleted_glossary_falls_back_instead_of_going_dark(tr, monkeypatch):
    """表被人在后台删了会让请求 400。整条引擎不能因此哑掉。"""
    api = FakeAPI(translate_status=400)
    monkeypatch.setattr(tr, "_api", api)
    assert run(tr.translate("hola", "zh-CN", source="es")) == "译文"
    first, second = api.bodies()
    assert "glossary_id" in first and "glossary_id" not in second


def test_quota_exhausted_starts_a_cooldown(tr, monkeypatch):
    api = FakeAPI(translate_status=456)
    monkeypatch.setattr(tr, "_api", api)
    assert run(tr.translate("hola", "zh-CN", source="es")) is None
    assert tr._cooldown_until > 0


# ---- 自检 --------------------------------------------------------------

def test_selfcheck_reports_the_glossary_and_warns_when_it_is_missing(tr, monkeypatch):
    """表没建起来时 DeepL 照样返回通顺的中文，商品名全是直译——屏幕上看不出
    异常。自检必须把这种静默退化喊出来。"""
    from types import SimpleNamespace

    from app import selfcheck

    args = SimpleNamespace(translator="deepl", target="zh-CN", source="es")

    ok = FakeAPI()
    monkeypatch.setattr(tr, "_api", ok)
    good = run(selfcheck._check_deepl(args, tr))
    assert good["level"] == selfcheck.OK and "原生术语表" in good["detail"]

    broken = DeepLTranslator()
    monkeypatch.setattr(broken, "_api", FakeAPI(create_status=456))
    bad = run(selfcheck._check_deepl(args, broken))
    assert bad["level"] == selfcheck.WARN and "26.5%" in bad["detail"]


def test_selfcheck_says_traditional_chinese_has_no_native_glossary(tr, monkeypatch):
    from types import SimpleNamespace

    from app import selfcheck

    monkeypatch.setattr(tr, "_api", FakeAPI())
    out = run(selfcheck._check_deepl(
        SimpleNamespace(translator="deepl", target="zh-TW", source="es"), tr))
    assert out["level"] == selfcheck.WARN


def test_the_engine_can_be_built_outside_any_event_loop(monkeypatch):
    """引擎对象在管线线程之外建、在管线线程里用。Python 3.9 的
    asyncio.Lock() 会绑到构造时的事件循环——绑错了要到第一次建表才炸。"""
    monkeypatch.setenv("DEEPL_API_KEY", "test-key:fx")
    built = DeepLTranslator()                      # 此处没有运行中的事件循环
    monkeypatch.setattr(built, "_api", FakeAPI())
    assert run(built._ensure_glossary("es", "zh-CN"))
