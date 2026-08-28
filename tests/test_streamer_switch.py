"""跨场状态：Bella → Elisa → Bella 同进程三段切换。

这批改动最大的风险不是单场行为，而是**跨场状态**——引擎实例、词表全局态、
DeepL 术语表缓存都跨场存活。三段切换验证三件事：profile 是双向真切换而不是
只会清掉旧状态；DeepL 术语表随内容指纹重建、不跨主播复用；审计头的词表
归属四件套逐场如实。
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import glossary as G
from app.pipeline import Pipeline
from app.telemetry import Telemetry
from app.translator import CachedTranslator, DeepLTranslator

BELLA = "https://www.tiktok.com/@bellaallnatural/live"
ELISA = "https://www.tiktok.com/@elisa._martinez/live"


class FakeServer:
    def __init__(self):
        self.sent = []
        self.config = {}

    async def broadcast(self, msg):
        self.sent.append(msg)

    async def status(self, *a, **k):
        pass


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.glossaries = []
        self._next = 0

    async def __call__(self, method, path, form=None, body=None):
        self.calls.append((method, path))
        if path == "/v2/usage":
            return 200, {"character_count": 1, "character_limit": 1000000}
        if path == "/v2/glossaries" and method == "GET":
            return 200, {"glossaries": list(self.glossaries)}
        if path == "/v2/glossaries" and method == "POST":
            self._next += 1
            gid = "gid-%d" % self._next
            self.glossaries.append({"glossary_id": gid, "name": form["name"],
                                    "ready": True})
            return 201, {"glossary_id": gid, "entry_count": 1}
        if method == "DELETE":
            gid = path.rsplit("/", 1)[-1]
            self.glossaries = [g for g in self.glossaries
                               if g["glossary_id"] != gid]
            return 204, {}
        raise AssertionError("没预料到的调用 " + path)


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setattr(G, "GLOSSARY_FILE", tmp_path / "glossary.txt")
    monkeypatch.setattr(G, "GLOSSARY_EXAMPLE", tmp_path / "no.example.txt")
    monkeypatch.setattr(G, "PROFILE_DIR", tmp_path / "profiles")
    (tmp_path / "glossary.txt").write_text("el carrito => 小黄车\n",
                                           encoding="utf-8")
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "bellaallnatural.example.txt").write_text(
        "la limpieza => 排毒粉\nD3 K2 => D3 K2 维生素滴剂\n", encoding="utf-8")
    terms = tmp_path / "terms.txt"
    terms.write_text("", encoding="utf-8")

    async def _noop(self, *a, **k):
        return None

    monkeypatch.setattr(Pipeline, "run_selfcheck", _noop)
    monkeypatch.setattr(Pipeline, "ensure_local_translator", _noop)
    monkeypatch.setattr(Pipeline, "_stats_loop", _noop)

    api = FakeAPI()
    monkeypatch.setenv("DEEPL_API_KEY", "test:fx")
    inner = DeepLTranslator()
    inner._api = api

    p = Pipeline.__new__(Pipeline)
    p.server = FakeServer()
    p.args = SimpleNamespace(translator="deepl", translator_note=None,
                             banned_terms=str(terms), glossary=None,
                             source="es", source_requested="es")
    p.telemetry = Telemetry()
    p.translator = CachedTranslator(inner)
    p.audit = None
    p._quality = {}
    p._strong_inflight = set()
    p._stats_task = None
    p._provision_task = None
    return p, inner, api


def test_bella_elisa_bella_round_trip(world):
    p, deepl, api = world
    loop = asyncio.new_event_loop()

    def begin(url):
        loop.run_until_complete(p._begin_session(url))
        loop.run_until_complete(asyncio.sleep(0))    # 让 ensure_future 的杂务跑完
        rows = [json.loads(line) for line in
                Path(p.audit.path).read_text(encoding="utf-8").splitlines()]
        return [r for r in rows if r["type"] == "session_start"][-1]

    def gid():
        return loop.run_until_complete(deepl._ensure_glossary("es", "zh-CN"))

    try:
        # ---- 第一场：Bella ----
        h1 = begin(BELLA)
        assert p.glossary.matching("ya pagó la limpieza")[0][1] == "排毒粉"
        assert p.glossary.matching("todo en el carrito")[0][1] == "小黄车"
        gid_bella = gid()
        name_bella = deepl._glossary_ids[("es", "zh")][0]
        assert h1["streamer"] == "bellaallnatural"
        assert h1["profile"] == "bellaallnatural"
        assert h1["profile_hash"] and h1["profile_hash"] != "?"
        assert h1["merged_glossary_hash"]
        assert h1["translator_active"] == "deepl"

        # ---- 第二场：Elisa（无 profile）----
        h2 = begin(ELISA)
        assert p.glossary.matching("ya pagó la limpieza") == []   # 不再排毒粉
        assert p.glossary.matching("todo en el carrito")[0][1] == "小黄车"
        gid_elisa = gid()
        name_elisa = deepl._glossary_ids[("es", "zh")][0]
        assert gid_elisa != gid_bella                 # 术语表真的换了
        assert name_elisa != name_bella               # 指纹变了
        assert not any(g["glossary_id"] == gid_bella  # Bella 的表已被删
                       for g in api.glossaries)
        assert h2["profile"] is None and h2["profile_hash"] is None
        assert h2["merged_glossary_hash"] != h1["merged_glossary_hash"]

        # ---- 第三场：切回 Bella ----
        h3 = begin(BELLA)
        assert p.glossary.matching("ya pagó la limpieza")[0][1] == "排毒粉"
        gid_back = gid()
        name_back = deepl._glossary_ids[("es", "zh")][0]
        assert name_back == name_bella                # 同内容 → 同指纹
        assert gid_back != gid_elisa                  # 但表是新建的（单槽位）
        assert h3["merged_glossary_hash"] == h1["merged_glossary_hash"]
        assert h3["profile"] == "bellaallnatural"
    finally:
        for task in asyncio.all_tasks(loop):
            task.cancel()
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()
