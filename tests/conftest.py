"""让 `import app.xxx` 在任何工作目录下都成立。

测试只依赖 pytest + numpy + aiohttp——被测模块的顶层导入都不含
faster-whisper / yt-dlp 等重型依赖（重型导入全部在函数内延迟进行）。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _keep_audit_out_of_the_real_log_dir(tmp_path, monkeypatch):
    """测试写的审计日志必须落在临时目录。

    一个漏传 log_dir 的测试曾把 371 段夹具字幕写进 logs/，而那个目录正是语料
    分析的输入——「hola」和两句癌症宣称被当成真实直播字幕统计了进去，占了
    两成。分母错了，用它算出来的结论也就跟着虚。

    autouse 是刻意的：靠每个测试自己记得传 log_dir，已经被证明会漏。
    """
    from app import audit

    monkeypatch.setattr(audit, "LOG_DIR", tmp_path / "logs")


@pytest.fixture(autouse=True)
def _reset_active_glossary(monkeypatch):
    """会话词表的模块级全局态不能在测试之间泄漏。

    _begin_session 会 set_active()，很多管线测试都会走到它；DeepL 的术语表
    测试又会读 active()。现在测试恰好按字母序读在写之前，但那是运气不是
    设计——并行或乱序执行时就是间歇性失败。"""
    from app import glossary

    monkeypatch.setattr(glossary, "_ACTIVE", None)


@pytest.fixture(autouse=True)
def _reset_resolver_process_state(monkeypatch):
    """resolver 里有两样进程级的状态，不能在测试之间泄漏：

    * 上一次匿名请求的时刻（_ANON）——借登录抓直播页之前要和它隔开 8 秒。前一个用例留下的
      时刻会让后一个用例**真的**去等；这里清掉，并把那段等待换成立刻返回（要验证等了多久的
      用例自己再换成记录用的假 sleep）。
    * 在途的浏览器登录读取（_LOGIN_READS）——生产里同一个浏览器同一时刻只读一次，后来的
      调用接在在途的那一次上；测试里前一个用例故意读得很慢的假读取，不能被后一个用例接上。
    """
    from app import resolver

    async def no_wait(_seconds):
        return None

    monkeypatch.setitem(resolver._ANON, "last", None)
    monkeypatch.setattr(resolver, "_gap_sleep", no_wait)
    resolver._LOGIN_READS.clear()
    yield
    resolver._LOGIN_READS.clear()
