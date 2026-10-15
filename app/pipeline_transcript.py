# i18n: done
"""字幕记录的 Pipeline 侧胶水：每场开始时建一份记录文件、热路径上把字幕和弹幕记进去、
收尾时关掉，以及设置里「字幕记录」那一行的两个按钮（导出、打开文件夹）。

记录文件本身的格式、读写、导出都在 app/transcript.py 和 app/transcript_export.py，这里只管
接线。几条不能松的约定：
  * 记录跟着审计走：和审计同一刻开、同一刻关，并且认自己那一场的审计对象（owner）——晚死的
    旧任务只许关自己那一份，不许关下一场的（CLAUDE.md 第七条 my_audit 的道理）；
  * 记录在广播之前写：界面卡住的客户端最多拖 2 秒，不该连累记录；
  * 所有钩子都吞掉异常：这个文件是锦上添花，不在两个 KPI 的关键路径上，出任何事都不能让
    识别、检测、审计、翻译少做一件；
  * 弹幕内容只进字幕记录，不进审计（审计只有代码和数字）；
  * 演示模式不记：虚构的台词不该留成「一场直播」。
"""
import asyncio
import time

from . import transcript as tr
from .i18n import LN, L, of
from .transcript_export import export_transcript


class TranscriptMixin:
    def _init_transcript(self):
        self.transcript = None
        # 设置行的摘要用：已经存了几场。只有数字，没有界面文字
        self.server.config["transcripts"] = {"count": tr.count_transcripts()}

    # ---- 一场的生命周期 ----
    def _open_transcript(self, streamer, owner):
        """_begin_session 里、审计建好之后调用。上一份还开着就先关掉（同一刻只有一份）。"""
        self._close_transcript()
        try:
            self.transcript = tr.TranscriptWriter(
                streamer=streamer, target_lang=getattr(self, "target", ""), owner=owner)
        except Exception as exc:
            self.transcript = None
            print("[警告] 字幕记录没能创建（直播不受影响）：{}".format(exc))

    def _close_transcript(self, owner=None):
        """owner 为 None：不问是谁的，直接关当前这份。给了 owner：只有这份记录就是该审计那一场
        的才关——晚到的旧任务拿旧审计来关，碰不到已经属于下一场的记录。关了一份返回 True，
        调用方据此重新推一次「存了几场」（这一场有没有内容，要到收尾才有定论）。"""
        writer = getattr(self, "transcript", None)
        if writer is None or (owner is not None and writer.owner is not owner):
            return False
        self.transcript = None
        try:
            writer.close()
        except Exception:
            pass
        return True

    async def _publish_transcripts(self):
        """存了几场，告诉设置行（config.transcripts）。新的一场开始时调一次，一场收尾时再调一次：
        只数有内容的（tr.count_transcripts），开始时这一场还是空的，设置分组在监听期间又是隐藏的，
        停下来回到首页时那个数字得已经是对的。"""
        try:
            info = {"count": tr.count_transcripts()}
            self.server.config["transcripts"] = info
            await self.server.broadcast({"type": "config", "transcripts": info})
        except Exception as exc:
            print("[警告] 字幕记录数量没能推给界面：{}".format(exc))

    # ---- 热路径上的钩子（全部吞异常）----
    def _transcript_caption(self, seq, ts, lang, original):
        writer = getattr(self, "transcript", None)
        if writer is None:
            return
        try:
            writer.caption(seq, ts, lang, original)
        except Exception:
            pass

    def _transcript_translation(self, seq, translated, strong):
        writer = getattr(self, "transcript", None)
        if writer is None:
            return
        try:
            writer.translation(seq, time.time(), translated, strong=strong)
        except Exception:
            pass

    async def _comment_broadcast(self, msg):
        """CommentTranslator 的 broadcast：先记进字幕记录，再照常广播。只认「弹幕」和
        「弹幕译好了」两种消息，别的（提示、重译状态）原样过。"""
        writer = getattr(self, "transcript", None)
        if writer is not None:
            try:
                kind = msg.get("type")
                if kind == "comment":
                    writer.comment(msg.get("id"), msg.get("ts"), msg.get("user"), msg.get("text"))
                elif kind == "comment_update" and msg.get("state") == "ok":
                    writer.comment_translation(msg.get("id"), msg.get("translated"))
            except Exception:
                pass
        await self.server.broadcast(msg)

    # ---- 设置里「字幕记录」那一行的两个按钮 ----
    def _open_transcripts_dir(self):
        """「打开文件夹」：确保 transcripts/ 在，再用系统方式打开。只在本机控制面可用（手机同看
        的入站只认 retranslate，见 app/viewer.py）。打不开不算错误，只提示一次，同 _open_brands_dir。"""
        folder = tr.TRANSCRIPT_DIR
        try:
            folder.mkdir(parents=True, exist_ok=True)
            self._brand_dir_opener(folder)
        except OSError as exc:
            print("[警告] 打开字幕记录文件夹失败：{}".format(exc))
            return self._transcript_notice(L("无法打开文件夹：{}",
                                             "Couldn’t open the folder: {}").format(folder))
        return None

    async def _export_transcript(self):
        """「导出最近一场」：正在记的这一场（有的话），否则最近开始的、记到了内容的那一场（没拿到
        流地址的那几次开始只留一个文件头，不算，见 tr.latest_transcript）。生成 .md / .csv / .srt
        放在记录文件旁边，打开文件夹，用 notice 说明文件在哪里或者出了什么事。选中的这一场里什么
        都没有时直说没有，不报「已导出 0 条字幕、0 条弹幕」。"""
        writer = getattr(self, "transcript", None)
        if writer is not None and writer.path is None:
            await self._transcript_notice(L(
                "这一场的字幕记录没能创建，没有可导出的内容",
                "This session’s transcript couldn’t be created, so there is nothing to export."))
            return
        if writer is not None:
            path = writer.path
            if not tr.has_content(path):
                await self._transcript_notice(L(
                    "这一场还没有记到字幕或弹幕，等有内容了再导出。",
                    "Nothing has been recorded in this session yet. Export again once captions "
                    "or comments come in."))
                return
        else:
            path = tr.latest_transcript()
        if path is None:
            await self._transcript_notice(L(
                "还没有字幕记录。开始监听后，每一场会自动记录。",
                "There are no transcripts yet. Each session is recorded automatically once "
                "monitoring starts."))
            return
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, export_transcript, path)
        except Exception as exc:
            print("[警告] 字幕记录导出失败：{}".format(exc))
            await self._transcript_notice(L(
                "没能导出字幕记录。详情：{}",
                "Couldn’t export the transcript. Details: {}").format(of(exc)))
            return
        if not (result.captions or result.comments):
            # 文件里有字幕记录的外形、读出来却一条都没有（被手改坏）：别报一个成功的 0 和 0
            await self._transcript_notice(L(
                "这份字幕记录里读不出字幕或弹幕，没有可导出的内容：{}",
                "No captions or comments could be read from this transcript, so there is "
                "nothing to export: {}").format(str(path)))
            return
        text = L("已导出 {}（Markdown、CSV、SRT 各一份），文件在 {}",
                 "Exported {} (Markdown, CSV and SRT files) to {}").format(
            L("、", ", ").join([
                LN(result.captions, "{} 条字幕", "1 caption", "{} captions").format(result.captions),
                LN(result.comments, "{} 条弹幕", "1 comment", "{} comments").format(result.comments)]),
            str(result.folder))
        try:
            self._brand_dir_opener(result.folder)
        except OSError as exc:
            print("[警告] 打开字幕记录文件夹失败：{}".format(exc))
            text = text + L("。文件夹没能自动打开", ". The folder didn’t open automatically")
        await self._transcript_notice(text)

    async def _transcript_notice(self, text):
        """页脚的一次性提示（所有 notice 都有），where 让「字幕记录」那一行把同一句话也写在自己
        的按钮下面：设置分组在页面中下部，页脚常常不在视野里，点完按钮不能没有回音。"""
        await self.server.broadcast({"type": "notice", "text": text, "where": "transcripts"})
