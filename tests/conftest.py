"""让 `import app.xxx` 在任何工作目录下都成立。

测试只依赖 pytest + numpy + aiohttp——被测模块的顶层导入都不含
faster-whisper / yt-dlp 等重型依赖（重型导入全部在函数内延迟进行）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
