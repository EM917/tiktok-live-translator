"""标准输出/错误流的编码护栏——进程启动的第一件事。

2026-09-15 在真实 Windows 11 25H2（中文、ANSI 代码页 936）上实测：

    & $Venv main.py --doctor 2>&1 | Out-File -Encoding utf8 doctor.log

崩在体检报告的第一个 ✅ 上、exit 1、报告一行都没写出来：

    File "app\\hwdetect.py", line 150, in doctor
    UnicodeEncodeError: 'gbk' codec can't encode character '\\u2705' ...

原因不是代码页选错了，而是**错误处理策略**：Windows 上 stdout 一旦不是控制台
（管道、`> log.txt`、Out-File、任务计划、以 SYSTEM 身份运行、没有控制台的服务），
Python 就按 ANSI 代码页编码、并且 errors='strict'——一个 emoji 就是异常。交互
控制台下 Python 走 UTF-16 控制台 API，永远不会触发，所以开发机、mac 上看不到。

为什么这值得一个模块，而不是在崩的那一行删掉 emoji：**print 出现在关键路径上**。
`⚠️`/`✅` 只是最先撞上的字符，真正会反复出现的是运行时文本——西语识别文本里的
`ñ ¿ ¡`（GBK 编不了）、子进程输出按 `errors="replace"` 解码出来的 U+FFFD（GBK
也编不了）。而这些 print 有不少在报警链路上：`_emit_original` 先打印再广播报警，
一抛就是「这场直播的第一条报警之后监听结束」。同理，非中文 Windows（cp1252）上
每一行中文文案都编不出来——所以问题不是 emoji，是「strict」。

做法只改一个常量：**保留平台编码，只把 errors='strict' 换成 'replace'**。

  * 不改成 UTF-8：PowerShell 5.1（以及默认配置的 7）用 `[Console]::OutputEncoding`
    也就是 OEM 代码页解码原生命令的输出，`cmd >` 更是直接落原始字节。输出 UTF-8
    会把中控真正要读的每一行中文变成乱码——VM 上的日志已经演示过一次。
  * 只碰 errors 仍是 'strict' 的流：操作员显式设的 `PYTHONIOENCODING=...:replace`、
    `PYTHONUTF8=1` 都原样保留。
  * 流是 None（pythonw）、没有 reconfigure（IDLE、pytest 的捕获对象）就跳过，
    并且本函数自己永不抛——护栏抛异常就是又一个同类 bug。

子进程另说：父进程按 UTF-8 逐行解析 stdout 的那些 worker（app/comment_worker.py、
app/webkit_fetch.py）要用 utf8_stdio()，它们的字节不是给人看的，换代码页会直接
让父进程 json.loads 失败。
"""
import sys


def harden_stdio(streams=None):
    """把 errors='strict' 的标准流改成 'replace'，编码不动。返回真正改过的流。

    幂等：改过一次之后 errors 已经不是 'strict'，再调用什么都不做。
    """
    changed = []
    for stream in (_default_streams() if streams is None else streams):
        if _soften(stream):
            changed.append(stream)
    return changed


def utf8_stdio(streams=None):
    """把标准流固定成 UTF-8/replace——只给「输出由父进程按 UTF-8 解析」的子进程用。

    别用在 main.py 上（见模块说明：中控读的捕获日志会变乱码）。
    """
    changed = []
    for stream in (_default_streams() if streams is None else streams):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:      # None / 没有 reconfigure / 已被接管：保持原样
            continue
        changed.append(stream)
    return changed


def _default_streams():
    return (sys.stdout, sys.stderr)


def _soften(stream):
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:            # None（pythonw）、pytest 捕获对象、自定义流
        return False
    if (getattr(stream, "errors", None) or "strict") != "strict":
        return False                   # 操作员或别的护栏已经指定过：不覆盖
    try:
        # 只传 errors：encoding/newline/行缓冲都保持原样（TextIOWrapper.reconfigure，3.7+）
        reconfigure(errors="replace")
    except Exception:
        return False
    return True
