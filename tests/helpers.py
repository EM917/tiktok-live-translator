"""事件循环入口，给同步的测试函数跑一个协程；外加一个带期限的轮询（poll_until）。

以前这一个函数在将近 34 个测试文件里各自复制了一份，还分裂成两种写法：多数
文件用 ``asyncio.run``，少数文件手写 ``new_event_loop().run_until_complete``
且不 close（GC 收它时会往 stderr 吐 fd 报错噪音，见 test_api_key_ui.py 曾经
的注释）。这里定死用 asyncio.run：新建循环、跑、关——一次到位，噪音也没有。

如果某个文件的夹具会在协程外创建绑定当前事件循环的对象（Python 3.9 上
asyncio.Lock/Event/Queue 一类构造时会绑死 get_event_loop() 给的那个循环），
用这份 asyncio.run 每次都换新循环就会跟那个对象「绑的循环」对不上、炸出
「attached to a different loop」——这种文件继续留着自己的本地 run()，别改，
并在那个文件里写明原因。
"""
import asyncio


def run(coro):
    return asyncio.run(coro)


async def poll_until(predicate, timeout, what, interval=0.01):
    """每 interval 秒看一次 predicate()，成立就返回；timeout 秒内等不到就失败，说清在等什么。

    用例在协程里等管线走到某一步时用它，别写不带期限的 ``while not ...: await asyncio.sleep(...)``：
    等的那一步要是不再出现（2026-10-23：#131 把翻译排空挪出这一轮之后，drain 阶段只剩一两毫秒，10 毫秒
    一看的轮询在 CI 上一直看不到），循环就一直转，直到 pytest-timeout 把整个测试进程连同后面的用例一起
    结束。what 是字符串，或返回字符串的函数（失败时才调，可以带上此刻的状态）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            detail = what() if callable(what) else what
            raise AssertionError("{:.1f} 秒内没等到：{}".format(timeout, detail))
        await asyncio.sleep(interval)
