"""app/diskspace.py：磁盘盘点与可选删除。

钉住的规则：只删盘点清单里的 id（永远不接受路径）；正在用的模型标 in_use；
本程序不用的模型标 other；当前会话的审计文件不列不删；HF 目录大小不跟软链；
直播进行中 Pipeline 一律拒绝删除。"""
import asyncio
import os
from datetime import datetime
from types import SimpleNamespace

from app import diskspace
from app import pipeline as pipeline_mod
from app.pipeline import Pipeline


def _hf(tmp_path, name, blob_bytes):
    d = tmp_path / "hub" / name
    (d / "blobs").mkdir(parents=True)
    (d / "blobs" / "b1").write_bytes(b"x" * blob_bytes)
    (d / "snapshots" / "abc").mkdir(parents=True)
    os.symlink(d / "blobs" / "b1", d / "snapshots" / "abc" / "model.bin")   # HF 缓存的软链
    return d


def _logs(tmp_path, names):
    d = tmp_path / "logs"
    d.mkdir(exist_ok=True)
    for n in names:
        (d / "session-{}.jsonl".format(n)).write_text("{}\n" * 10, encoding="utf-8")
    return d


def test_inventory_roles_sizes_and_symlinks(tmp_path):
    _hf(tmp_path, "models--mlx-community--whisper-large-v3-mlx", 3000)
    _hf(tmp_path, "models--Systran--faster-whisper-large-v3", 2000)
    _hf(tmp_path, "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo", 1500)
    _hf(tmp_path, "models--google--madlad400-3b-mt", 5000)
    (tmp_path / "hub" / "not-a-model").mkdir()
    ollama = [{"name": "hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M", "size": 1100},
              {"name": "qwen3:4b", "size": 2500}]
    items = diskspace.inventory(hf_dir=tmp_path / "hub", ollama_models=ollama,
                                log_dir=tmp_path / "nologs",
                                active_asr=("mlx", "large-v3"),
                                active_ollama="hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M")
    by = {it["id"]: it for it in items}
    # 只有真正加载的那个算正在用：mlx 后端认 -mlx 仓库；large-v3 不误吃 turbo
    assert by["hf:models--mlx-community--whisper-large-v3-mlx"]["role"] == "in_use"
    assert by["hf:models--Systran--faster-whisper-large-v3"]["role"] == "app"
    assert by["hf:models--mobiuslabsgmbh--faster-whisper-large-v3-turbo"]["role"] == "app"
    assert by["hf:models--google--madlad400-3b-mt"]["role"] == "other"
    assert "hf:not-a-model" not in by
    # 大小不跟软链：blobs 里 3000 字节，snapshots 的软链不再算一遍
    assert by["hf:models--mlx-community--whisper-large-v3-mlx"]["size"] == 3000
    assert by["ollama:hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M"]["role"] == "in_use"
    assert by["ollama:qwen3:4b"]["role"] == "other" and by["ollama:qwen3:4b"]["size"] == 2500


def test_ct2_backend_marks_the_non_mlx_repo(tmp_path):
    _hf(tmp_path, "models--mlx-community--whisper-large-v3-mlx", 10)
    _hf(tmp_path, "models--Systran--faster-whisper-large-v3", 10)
    items = diskspace.inventory(hf_dir=tmp_path / "hub", log_dir=tmp_path / "nologs",
                                active_asr=("ct2", "large-v3"))
    roles = {it["id"]: it["role"] for it in items}
    assert roles["hf:models--Systran--faster-whisper-large-v3"] == "in_use"
    assert roles["hf:models--mlx-community--whisper-large-v3-mlx"] == "app"


def test_logs_exclude_current_session_and_split_old(tmp_path):
    logs = _logs(tmp_path, ["20260801-100000", "20260901-100000", "20260907-100000"])
    now = datetime(2026, 9, 8, 12, 0, 0)
    items = diskspace.inventory(hf_dir=tmp_path / "hub", log_dir=logs,
                                current_log=logs / "session-20260907-100000.jsonl", now=now)
    by = {it["id"]: it for it in items}
    assert "2 个文件" in by["logs:all"]["label"]          # 本场那份不算
    assert "1 个文件" in by["logs:old"]["label"]          # 只有 8 月那份早于 30 天


def test_delete_only_known_ids_and_never_paths(tmp_path):
    d = _hf(tmp_path, "models--Systran--faster-whisper-small", 10)
    logs = _logs(tmp_path, ["20260801-100000", "20260907-100000"])
    outside = tmp_path / "precious.txt"
    outside.write_text("keep")

    async def go():
        return await diskspace.delete(
            ["hf:models--Systran--faster-whisper-small", "hf:../precious.txt",
             str(outside), "logs:old", "nope:x"],
            hf_dir=tmp_path / "hub", log_dir=logs,
            current_log=logs / "session-20260907-100000.jsonl",
            now=datetime(2026, 9, 8))

    freed, done, failed = asyncio.run(go())
    assert not d.exists()                                      # 清单里的 HF 目录删了
    assert outside.exists()                                    # 路径一律不认
    assert not (logs / "session-20260801-100000.jsonl").exists()
    assert (logs / "session-20260907-100000.jsonl").exists()   # 本场那份不动
    assert set(done) == {"hf:models--Systran--faster-whisper-small", "logs:old"}
    assert len(failed) == 3 and freed > 0


def test_delete_ollama_goes_through_callback(tmp_path):
    seen = []

    async def fake_delete(name):
        seen.append(name)
        return True

    async def go():
        return await diskspace.delete(
            ["ollama:qwen3:4b"], hf_dir=tmp_path / "hub", log_dir=tmp_path / "nologs",
            ollama_models=[{"name": "qwen3:4b", "size": 7}], ollama_delete=fake_delete)

    freed, done, failed = asyncio.run(go())
    assert seen == ["qwen3:4b"] and done == ["ollama:qwen3:4b"] and freed == 7


# ---- Pipeline：直播中拒绝删除 ----

class StubServer:
    def __init__(self):
        self.config = {"status": {"state": "idle"}}
        self.messages = []

    async def status(self, state, detail=""):
        self.messages.append({"type": "status", "state": state, "detail": detail})

    async def broadcast(self, msg):
        self.messages.append(msg)


def _pipeline(monkeypatch, tmp_path):
    from app import settings
    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    terms = tmp_path / "banned_terms.txt"
    terms.write_text("", encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms)
    args = SimpleNamespace(cookies=None, target="zh-CN", translator="none", source="es",
                           beam=5, context=False, asr_temperature=None, glossary=None,
                           backend="auto", model=None, device="auto", compute_type="auto",
                           denoise="off", banned_terms=None)
    server = StubServer()
    return Pipeline(args, server), server


def test_pipeline_refuses_to_delete_while_live(monkeypatch, tmp_path):
    p, server = _pipeline(monkeypatch, tmp_path)
    server.config["status"] = {"state": "live"}
    called = []

    async def never(*a, **k):
        called.append(a)

    monkeypatch.setattr(diskspace, "delete", never)
    asyncio.run(p.handle_control({"type": "disk_delete", "ids": ["hf:x"]}))
    assert called == []
    assert any("直播进行中" in (m.get("text") or "") for m in server.messages)


def test_pipeline_delete_calls_module_and_republishes(monkeypatch, tmp_path):
    p, server = _pipeline(monkeypatch, tmp_path)
    got = {}

    async def fake_delete(ids, **kw):
        got["ids"] = ids
        return 12345, list(ids), []

    async def no_models(self):
        return []

    monkeypatch.setattr(diskspace, "delete", fake_delete)
    monkeypatch.setattr(Pipeline, "_ollama_models", no_models)
    monkeypatch.setattr(diskspace, "inventory", lambda **kw: [])
    asyncio.run(p.handle_control({"type": "disk_delete", "ids": ["hf:a", 7]}))
    assert got["ids"] == ["hf:a", "7"]
    assert any(m.get("type") == "disk" for m in server.messages)     # 删完重新盘点
    assert "disk" in server.config
