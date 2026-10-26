"""降噪模型固定版本、下载后核对 sha256（专家审查 I5、计划审查 Q9）。

钉住的问题：RNNOISE_URL 跟着上游的 master 走，下载下来的文件只验文件头和长度。上游一变、或者被换掉，
新装机拿到的就是另一份，没人知道。现在 URL 固定到一个提交，下载后核对 sha256：对不上就不落盘、终端打
中文警告，自检「人声降噪」一行变成 FAIL（不只在终端打印——selfcheck.py 模块说明那次事故的教训）。

期望的校验值是这样定下来的（2026-10-26，不在直播时）：上游只有 6c2c130 这一个提交动过这个文件，它的
blob 和本机一直在用的那份逐字节相同；从固定的 URL 实际下载一次，sha256 也一致。不连网，不跑 ffmpeg。
"""
import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from app import pipeline as pipeline_mod
from app import selfcheck
from app import settings as settings_mod
from app.pipeline import Pipeline
from app.server import CaptionServer
from tests.helpers import run

GOOD = b"rnnoise-nu model file version 1.\n" + b"0" * 120_000


class RecordingServer(CaptionServer):
    def __init__(self):
        super().__init__(port=8765)
        self.sent = []

    async def broadcast(self, msg):
        self.sent.append(dict(msg))
        await super().broadcast(msg)


class FakeContent:
    def __init__(self, data):
        self._data = data

    async def read(self, n):
        chunk, self._data = self._data[:n], self._data[n:]
        return chunk


class FakeResp:
    def __init__(self, data, status=200):
        self.status = status
        self.content = FakeContent(data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def fake_aiohttp(monkeypatch, body, seen):
    import aiohttp

    class Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, url, **k):
            seen.append(url)
            return FakeResp(body)

    monkeypatch.setattr(aiohttp, "ClientSession", Session)


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", tmp_path / "settings.json")
    model = tmp_path / "models" / "bd.rnnn"
    monkeypatch.setattr(pipeline_mod, "DENOISE_MODEL", model)
    probes = []

    def probe(path):
        probes.append(path)
        return True

    monkeypatch.setattr(pipeline_mod, "_arnndn_probe", probe)
    p = Pipeline.__new__(Pipeline)
    p.server = RecordingServer()
    p.args = SimpleNamespace(denoise="auto")
    return SimpleNamespace(p=p, model=model, probes=probes, tmp=tmp_path)


def test_the_url_is_pinned_to_one_upstream_commit():
    url = pipeline_mod.RNNOISE_URL
    assert "/master/" not in url
    assert url == ("https://raw.githubusercontent.com/GregorR/rnnoise-models/"
                   "6c2c1301c30a6be5ddf93704a693f4956ce2aa55/beguiling-drafter-2018-08-30/bd.rnnn")
    assert pipeline_mod.RNNOISE_SHA256 == \
        "ae3f7411e1e6a884f839a4a145c394408398f09854dbc1216ee02faafc98a17b"


def test_a_download_with_the_pinned_checksum_is_saved(monkeypatch, world):
    monkeypatch.setattr(pipeline_mod, "RNNOISE_SHA256", hashlib.sha256(GOOD).hexdigest())
    seen = []
    fake_aiohttp(monkeypatch, GOOD, seen)

    assert run(world.p._ensure_denoise_model()) == str(world.model)

    assert seen == [pipeline_mod.RNNOISE_URL]
    assert world.model.read_bytes() == GOOD and world.probes == [str(world.model)]
    assert world.p._denoise_check_state() is None


def test_a_mismatching_download_is_not_saved_and_the_selfcheck_row_turns_red(monkeypatch, world, capsys):
    """内容对不上（哪怕文件头、长度都像样）：不写文件、不留 .part，终端一行中文警告，自检那一行变 FAIL，
    顶部持续提示也跟着出来（_sync_selfcheck_incident 只认 fail）。"""
    bad = GOOD[:-1] + b"1"
    seen = []
    fake_aiohttp(monkeypatch, bad, seen)
    p = world.p
    p.server.config["selfcheck"] = {"checks": [
        selfcheck._check(selfcheck.NAMES["denoise"], "warn", "not downloaded"),
        selfcheck._check(selfcheck.NAMES["asr"], "ok", "fine")], "summary": {}}

    assert run(p._ensure_denoise_model()) is None

    assert not world.model.exists() and not world.model.with_suffix(".rnnn.part").exists()
    assert world.probes == []
    out = capsys.readouterr().out
    assert "[警告] 降噪模型下载下来的文件和固定版本的校验值对不上" in out and "没有保存" in out
    assert p._denoise_check_state()["checksum_mismatch"] is True
    rows = {c["name"]: c for c in p.server.config["selfcheck"]["checks"]}
    row = rows[selfcheck.NAMES["denoise"]]
    assert row["level"] == "fail" and "校验值对不上" in row["detail"]
    assert rows[selfcheck.NAMES["asr"]]["level"] == "ok"           # 别的行不动
    assert p.server.config["incidents"]["selfcheck"]["level"] == "error"


def test_the_denoise_row_reads_the_download_state(monkeypatch, tmp_path):
    from app import i18n

    args = SimpleNamespace(denoise="auto")
    monkeypatch.setattr(pipeline_mod, "DENOISE_MODEL", tmp_path / "does-not-exist.rnnn")
    plain = run(selfcheck.check_denoise(args))
    red = run(selfcheck.check_denoise(args, {"checksum_mismatch": True}))
    assert plain["level"] == "warn"                                 # 还没下载过：照旧黄
    assert red["level"] == "fail"
    english = i18n.text(red["detail"], "en") + " " + i18n.text(red["fix"], "en")
    assert not i18n.CJK.search(english) and "checksum" in english
    off = run(selfcheck.check_denoise(SimpleNamespace(denoise="off"), {"checksum_mismatch": True}))
    assert off["level"] == "ok"                                     # 主动关掉的不受影响


def test_run_selfcheck_passes_the_state_only_after_a_mismatch(monkeypatch, world):
    """没出过事时 run_all 的调用和以前一样（不多传参数）；出过事才带上 denoise_state。"""
    calls = []

    async def fake_run_all(args, detector=None, glossary=None, translator=None, asr_state=None,
                           **extra):
        calls.append(extra)
        return []

    monkeypatch.setattr(selfcheck, "run_all", fake_run_all)
    p = world.p
    p.detector = p.glossary = p.translator = None
    p.guard = None
    run(p.run_selfcheck())
    p._denoise_download = {"checksum_mismatch": True, "sha256": "x", "size": 1}
    run(p.run_selfcheck())
    assert calls == [{}, {"denoise_state": p._denoise_download}]


def test_a_later_good_download_clears_the_red_row(monkeypatch, world):
    p = world.p
    p._denoise_download = {"checksum_mismatch": True}
    monkeypatch.setattr(pipeline_mod, "RNNOISE_SHA256", hashlib.sha256(GOOD).hexdigest())
    fake_aiohttp(monkeypatch, GOOD, [])
    assert run(p._ensure_denoise_model()) == str(world.model)
    assert p._denoise_check_state() is None


def test_run_all_still_works_with_the_old_call(monkeypatch):
    """run_all 不带 denoise_state 照旧能跑（tests/test_resilience_asr.py 那种只认老参数的替身也要能用）。"""
    async def quick(*a, **k):
        return selfcheck._check("x", "ok", "fine")

    for name in ("check_ffmpeg", "check_denoise", "check_asr", "check_translator", "check_watchlist",
                 "check_glossary", "check_audit", "check_resolver", "check_browser_login",
                 "check_comments", "check_disk"):
        monkeypatch.setattr(selfcheck, name, quick)
    rows = asyncio.run(selfcheck.run_all(SimpleNamespace()))
    assert len(rows) == 11
