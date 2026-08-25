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

    一个漏传 log_dir 的测试曾把 348 条夹具字幕写进 logs/，而那个目录正是语料
    分析的输入——「hola」「esto cura el cancer」被当成真实直播字幕统计了进去，
    占了 19.4%。分母错了，结论也就跟着虚。

    autouse 是刻意的：靠每个测试自己记得传 log_dir 已经被证明会漏。
    """
    from app import audit

    monkeypatch.setattr(audit, "LOG_DIR", tmp_path / "logs")
