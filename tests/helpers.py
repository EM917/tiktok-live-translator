"""事件循环入口，给同步的测试函数跑一个协程。

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
