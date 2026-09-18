"""磁盘空间盘点/删除的 Pipeline 侧胶水：从 app/pipeline.py 纯搬移出来，方法名/
签名/方法体逐字未改动。真正的盘点与删除逻辑在 app/diskspace.py；这里只是把
Pipeline 手头的状态（当前 ASR/引擎、审计文件、直播中标志）转成那边要的参数。

_publish_disk 用到的 TERMS_FILE 常量定义在 app/pipeline.py（跟违禁词表相关，
不是磁盘模块自己的东西），故从那里导入。
"""
import asyncio

from .pipeline import TERMS_FILE


class DiskSpaceMixin:
    # ---- 磁盘空间：盘点与可选删除（见 app/diskspace.py）----
    def _disk_active(self):
        """正在用的语音模型 (backend, model) 与翻译模型名——盘点时标成「正在用」。"""
        key = self._transcriber_key or self._loading_key
        asr = (key[0], key[1]) if key else None
        engine = getattr(self.translator, "name", None) or ""
        from .translator import HYMT2_LARGE, HYMT2_SMALL
        ollama = {"hymt2": HYMT2_SMALL, "hymt2-7b": HYMT2_LARGE,
                  "gemma": "translategemma"}.get(engine)
        return asr, ollama

    async def _ollama_models(self):
        """/api/tags 的模型列表；Ollama 没在跑就是空列表。"""
        import aiohttp

        from .localmodel import base_url
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as s:
                async with s.get(base_url() + "/api/tags") as r:
                    data = await r.json()
            return [m for m in (data.get("models") or []) if isinstance(m, dict)]
        except Exception:
            return []

    async def _ollama_delete(self, name):
        import aiohttp

        from .localmodel import base_url
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
                async with s.delete(base_url() + "/api/delete", json={"name": name}) as r:
                    return r.status == 200
        except Exception:
            return False

    async def _publish_disk(self):
        """盘点结果推给页面并存进 config。目录大小要 walk 几十 GB 的缓存，
        放线程池里算，不卡事件循环。"""
        import shutil

        from . import diskspace
        asr, ollama = self._disk_active()
        models = await self._ollama_models()
        current_log = getattr(self.audit, "path", None) if self.audit else None
        loop = asyncio.get_running_loop()
        items = await loop.run_in_executor(
            None, lambda: diskspace.inventory(
                ollama_models=models, active_asr=asr, active_ollama=ollama,
                current_log=current_log))
        try:
            free = shutil.disk_usage(str(TERMS_FILE.parent)).free
        except OSError:
            free = None
        info = {"items": items, "free": free}
        self.server.config["disk"] = info
        await self.server.broadcast(dict(info, type="disk"))

    async def _disk_delete(self, ids):
        """删所选。直播进行中一律拒绝——模型可能正在用，日志正在写。"""
        from . import diskspace
        state = (self.server.config.get("status") or {}).get("state")
        if state in ("connecting", "live") or (
                self._stream_task is not None and not self._stream_task.done()):
            await self.server.broadcast({"type": "notice",
                                         "text": "直播进行中不删除文件，先点「停止」"})
            return
        asr, ollama = self._disk_active()
        models = await self._ollama_models()
        current_log = getattr(self.audit, "path", None) if self.audit else None
        freed, done, failed = await diskspace.delete(
            ids, ollama_models=models, ollama_delete=self._ollama_delete,
            current_log=current_log, active_asr=asr, active_ollama=ollama)
        text = "已删除 {} 项，释放 {}".format(len(done), diskspace.human(freed))
        if failed:
            text += "；未删除：" + "；".join(failed)[:300]
        print("[磁盘] " + text)
        await self.server.broadcast({"type": "notice", "text": text})
        await self._publish_disk()
