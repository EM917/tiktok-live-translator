"""关注名单的存储（app/comment_marks.py）和自动加入的口径（spec §3.3、§5.7、I14）。

纯内存加临时目录：不联网、不加载模型、不 import main.py。主播名、昵称、账号都是虚构的。
"""
import asyncio
import json
import os
import threading

import pytest

from app import comment_marks
from app.comment_marks import MarkStore, streamer_key
from tests.guard_support import (LINK_TEXT, RED_TEXT, ROUTED_TEXT, STREAMER, UID, item, new_runtime,
                                 start)
from tests.helpers import run

NOW = 1_760_000_000


def store(tmp_path, **kw):
    return MarkStore(directory=tmp_path / "marked", clock=lambda: NOW, **kw)


def read(tmp_path, name=STREAMER):
    return json.loads((tmp_path / "marked" / (name + ".json")).read_text(encoding="utf-8"))


@pytest.fixture
def fast_save(monkeypatch):
    monkeypatch.setattr(comment_marks, "SAVE_DELAY_SEC", 0.05)


@pytest.fixture
def writes(monkeypatch):
    """记下每一次真正的落盘（路径、是不是在线程池里）。"""
    calls = []
    real = comment_marks._write_atomic

    def spy(path, text):
        calls.append({"path": path, "off_loop": threading.current_thread() is not threading.main_thread()})
        return real(path, text)

    monkeypatch.setattr(comment_marks, "_write_atomic", spy)
    return calls


# ---- 主播键与键的清洗 ---------------------------------------------------------------------

def test_streamer_key_is_lowercase_and_rejects_unsafe_names():
    assert streamer_key("Luna_Demo") == "luna_demo"
    assert streamer_key("  luna.demo ") == "luna.demo"
    for bad in (None, "", "   ", "../etc/passwd", "a/b", "a\\b", ".hidden", "a\x00b", "x" * 65):
        assert streamer_key(bad) is None, bad


def test_unsafe_streamer_name_never_touches_the_disk(tmp_path, writes):
    s = store(tmp_path)

    async def scenario():
        assert await s.load("../evil") is None
        status, primary, _ = s.add([UID], "Ana", by="manual")
        assert status == "added" and primary == UID
        await s.flush_now()

    run(scenario())
    assert writes == [] and not (tmp_path / "marked").exists()


# ---- 多键匹配 ----------------------------------------------------------------------------

def test_any_key_matches_the_same_person(tmp_path):
    s = store(tmp_path)
    status, primary, _ = s.add([UID, "s:MS4wLjAB"], "Ana", handle="ana_demo", by="manual")
    assert (status, primary) == ("added", UID)
    assert s.match(["s:MS4wLjAB"]) == UID and s.match(["nobody", UID]) == UID
    assert s.match(["h:other"]) is None
    # 同一个人换了主键：触碰时把新出现的键并进来，之后用它也认得出
    s.touch(UID, ["h:ana_demo"], "Ana", NOW + 5)
    assert s.match(["h:ana_demo"]) == UID
    assert s.entry(UID)["keys"] == [UID, "s:MS4wLjAB", "h:ana_demo"]
    # 键属于另一个人时不抢
    s.add(["999"], "Bea", by="manual")
    s.touch(UID, ["999"], "Ana", NOW + 6)
    assert s.match(["999"]) == "999"
    # 按任一键移出，所有键一起去掉
    primary, entry = s.remove("s:MS4wLjAB")
    assert primary == UID and entry["nick"] == "Ana"
    assert s.match([UID, "s:MS4wLjAB", "h:ana_demo"]) is None


def test_aka_keeps_at_most_three_other_names(tmp_path):
    s = store(tmp_path)
    s.add([UID], "Ana", by="manual")
    for i, nick in enumerate(["Ana B", "Ana C", "Ana D", "Ana E"]):
        s.touch(UID, [], nick, NOW + i)
    entry = s.entry(UID)
    assert entry["nick"] == "Ana" and entry["aka"] == ["Ana C", "Ana D", "Ana E"]
    assert entry["hits"] == 4


def test_nick_only_entries_live_in_memory_and_never_reach_the_disk(tmp_path, fast_save, writes):
    s = store(tmp_path)

    async def scenario():
        await s.load(STREAMER)
        status, primary, _ = s.add(["n:Ana"], "Ana", by="manual")
        assert (status, primary) == ("added", "n:Ana")
        assert s.match([], nick="Ana") == "n:Ana"
        assert s.entry("n:Ana")["session"] is True
        await asyncio.sleep(0.2)
        await s.flush_now()

    run(scenario())
    assert writes == []                               # 键不稳的不落盘：连文件都不建
    assert not (tmp_path / "marked" / (STREAMER + ".json")).exists()


def test_a_persisted_file_never_contains_nick_only_keys(tmp_path):
    s = store(tmp_path)

    async def scenario():
        await s.load(STREAMER)
        s.add(["n:Ana"], "Ana", by="manual")
        s.add([UID], "Bea", by="manual")
        await s.flush_now()

    run(scenario())
    assert list(read(tmp_path)["viewers"]) == [UID]


# ---- 写盘：2 秒合并、线程池、原子替换 ------------------------------------------------------

def test_changes_are_coalesced_into_one_write_off_the_event_loop(tmp_path, fast_save, writes):
    s = store(tmp_path)

    async def scenario():
        await s.load(STREAMER)
        for i in range(5):
            s.add(["10%d" % i], "Viewer %d" % i, by="manual")
        assert writes == []                           # 还在合并窗口里
        await asyncio.sleep(0.4)
        assert len(writes) == 1                       # 五次改动，一次写盘
        s.add(["200"], "Later", by="manual")
        await asyncio.sleep(0.3)
        assert len(writes) == 2

    run(scenario())
    assert all(w["off_loop"] for w in writes)         # run_in_executor：事件循环上没有磁盘 IO
    assert sorted(read(tmp_path)["viewers"]) == ["100", "101", "102", "103", "104", "200"]


def test_flush_now_writes_what_is_pending_without_waiting(tmp_path, writes):
    s = store(tmp_path)

    async def scenario():
        await s.load(STREAMER)
        s.add([UID], "Ana", by="auto", cat="threat")
        await s.flush_now()

    run(scenario())
    assert len(writes) == 1
    data = read(tmp_path)
    assert data["version"] == 1 and data["streamer"] == STREAMER
    assert data["viewers"][UID]["by"] == "auto" and data["viewers"][UID]["cat"] == "threat"
    assert "text" not in json.dumps(data)             # 不存弹幕原文


def test_only_last_seen_changes_are_lazy_and_do_not_write_every_two_seconds(tmp_path, fast_save, writes,
                                                                            monkeypatch):
    monkeypatch.setattr(comment_marks, "LAZY_SAVE_SEC", 60.0)
    s = store(tmp_path)

    async def scenario():
        await s.load(STREAMER)
        s.add([UID], "Ana", by="manual")
        await asyncio.sleep(0.2)
        assert len(writes) == 1
        for i in range(20):
            s.touch(UID, [], "Ana", NOW + i)          # 常发言的被关注者
        await asyncio.sleep(0.3)
        assert len(writes) == 1                       # 没有因为 last_seen 变化而落盘
        await s.flush_now()                           # 收尾强制写
        assert len(writes) == 2

    run(scenario())
    assert read(tmp_path)["viewers"][UID]["hits"] == 20


def test_write_is_atomic_a_failed_replace_leaves_the_old_file_intact(tmp_path, monkeypatch):
    path = tmp_path / "marked" / "x.json"
    comment_marks._write_atomic(path, '{"a": 1}')
    assert json.loads(path.read_text()) == {"a": 1}
    assert not (tmp_path / "marked" / "x.json.tmp").exists()

    def boom(src, dst):
        raise OSError("disk gone")

    monkeypatch.setattr(os, "replace", boom)
    assert comment_marks._write_safe(path, '{"a": 2}').startswith("OSError")
    assert json.loads(path.read_text()) == {"a": 1}   # 没写完的是 .tmp，原文件还是旧的


def test_a_write_error_is_counted_and_never_raises(tmp_path, monkeypatch, capsys):
    s = store(tmp_path)
    monkeypatch.setattr(comment_marks, "_write_atomic", lambda p, t: (_ for _ in ()).throw(OSError("full")))

    async def scenario():
        await s.load(STREAMER)
        s.add([UID], "Ana", by="manual")
        await s.flush_now()                           # 不抛

    run(scenario())
    assert s.write_errors == 1 and "关注名单写不进去" in capsys.readouterr().out


# ---- 载入：损坏、版本、过期 ----------------------------------------------------------------

def test_a_corrupt_file_is_backed_up_and_treated_as_empty(tmp_path, writes):
    folder = tmp_path / "marked"
    folder.mkdir()
    (folder / (STREAMER + ".json")).write_text("{ not json", encoding="utf-8")
    s = store(tmp_path)

    async def scenario():
        code = await s.load(STREAMER)
        s.add([UID], "Ana", by="manual")
        await s.flush_now()
        return code

    assert run(scenario()) == "marks_corrupt"
    backups = list(folder.glob(STREAMER + ".json.bak-*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == "{ not json"
    assert list(read(tmp_path)["viewers"]) == [UID]   # 之后照常写一份新的，备份留着


@pytest.mark.parametrize("payload", ['[]', '{"version": 2, "viewers": {}}', '{"version": 1, "viewers": []}',
                                     '{"viewers": {}}', '"x"', ''])
def test_unknown_shapes_and_versions_count_as_corrupt(tmp_path, payload):
    folder = tmp_path / "marked"
    folder.mkdir()
    (folder / (STREAMER + ".json")).write_text(payload, encoding="utf-8")
    s = store(tmp_path)
    assert run(s.load(STREAMER)) == "marks_corrupt" and len(s) == 0
    assert list(folder.glob("*.bak-*"))


def test_missing_file_is_an_empty_list_without_a_notice(tmp_path):
    assert run(store(tmp_path).load(STREAMER)) is None


def test_unreadable_file_is_not_renamed_and_not_overwritten(tmp_path, monkeypatch, writes):
    folder = tmp_path / "marked"
    folder.mkdir()
    target = folder / (STREAMER + ".json")
    target.write_text('{"version": 1, "viewers": {}}', encoding="utf-8")
    real = type(target).read_bytes

    def deny(self):
        if self == target:
            raise PermissionError("denied")
        return real(self)

    monkeypatch.setattr(type(target), "read_bytes", deny)
    s = store(tmp_path)

    async def scenario():
        code = await s.load(STREAMER)
        s.add([UID], "Ana", by="manual")
        await s.flush_now()
        return code

    assert run(scenario()) == "marks_unreadable"       # 没有备份，所以不能叫 marks_corrupt
    assert writes == [] and target.exists() and not list(folder.glob("*.bak-*"))
    assert s.persist is False


def test_a_corrupt_file_that_cannot_be_moved_aside_is_unreadable_and_left_alone(tmp_path, monkeypatch, writes):
    folder = tmp_path / "marked"
    folder.mkdir()
    target = folder / (STREAMER + ".json")
    target.write_text("{ not json", encoding="utf-8")

    def refuse(src, dst):
        raise OSError("read-only volume")

    monkeypatch.setattr(os, "replace", refuse)
    s = store(tmp_path)

    async def scenario():
        code = await s.load(STREAMER)
        s.add([UID], "Ana", by="manual")
        await s.flush_now()
        return code

    assert run(scenario()) == "marks_unreadable"
    assert target.read_text(encoding="utf-8") == "{ not json" and not list(folder.glob("*.bak-*"))
    assert writes == [] and s.persist is False


def test_only_a_real_backup_is_reported_as_one(tmp_path):
    folder = tmp_path / "marked"
    folder.mkdir()
    (folder / (STREAMER + ".json")).write_text("{ not json", encoding="utf-8")
    assert run(store(tmp_path).load(STREAMER)) == "marks_corrupt"
    assert len(list(folder.glob("*.bak-*"))) == 1


def test_entries_with_bad_fields_are_cleaned_or_dropped_on_load(tmp_path):
    folder = tmp_path / "marked"
    folder.mkdir()
    (folder / (STREAMER + ".json")).write_text(json.dumps({"version": 1, "viewers": {
        UID: {"keys": [UID, "n:Ana", 5, "s:ok"], "nick": "Ana", "by": "weird", "hits": "x",
              "aka": ["a", "b", "c", "d"]},
        "bad": "not a dict", "n:only": {"keys": ["n:only"]}}}), encoding="utf-8")
    s = store(tmp_path)
    assert run(s.load(STREAMER)) is None
    entry = s.entry(UID)
    assert entry["keys"] == [UID, "s:ok"] and entry["by"] == "manual" and entry["hits"] == 0
    assert entry["aka"] == ["a", "b", "c"]
    assert len(s) == 1


def test_auto_entries_expire_after_30_days_manual_ones_never(tmp_path, writes):
    folder = tmp_path / "marked"
    folder.mkdir()
    day = 86400
    old, recent = NOW - 31 * day, NOW - 29 * day
    (folder / (STREAMER + ".json")).write_text(json.dumps({"version": 1, "viewers": {
        "1": {"keys": ["1"], "by": "auto", "first": old, "last_seen": old},
        "2": {"keys": ["2"], "by": "auto", "first": old, "last_seen": recent},
        "3": {"keys": ["3"], "by": "manual", "first": old, "last_seen": old}}}), encoding="utf-8")
    s = store(tmp_path)

    async def scenario():
        await s.load(STREAMER)
        assert sorted(s.viewers) == ["2", "3"]
        await s.flush_now()                           # 过期移出的结果也落盘

    run(scenario())
    assert len(writes) == 1 and sorted(read(tmp_path)["viewers"]) == ["2", "3"]


# ---- 上限 --------------------------------------------------------------------------------

def test_cap_evicts_the_oldest_auto_entry_and_never_a_manual_one(tmp_path, monkeypatch):
    monkeypatch.setattr(comment_marks, "MAX_VIEWERS", 4)
    s = store(tmp_path)
    s.add(["m1"], "M1", by="manual", ts=NOW)
    s.add(["a1"], "A1", by="auto", ts=NOW + 10)
    s.add(["a2"], "A2", by="auto", ts=NOW + 5)
    s.add(["a3"], "A3", by="auto", ts=NOW + 20)
    status, primary, evicted = s.add(["m2"], "M2", by="manual", ts=NOW + 30)
    assert (status, primary, evicted) == ("added", "m2", ["a2"])     # last_seen 最旧的 auto
    status, primary, evicted = s.add(["a4"], "A4", by="auto", ts=NOW + 40)
    assert evicted == ["a1"] and sorted(s.viewers) == ["a3", "a4", "m1", "m2"]
    s.add(["m3"], "M3", by="manual")
    s.add(["m4"], "M4", by="manual")                  # 把 auto 挤光
    status, primary, evicted = s.add(["m5"], "M5", by="manual")
    assert status == "full" and primary is None       # 全是手动加入的：不淘汰，拒绝第 501 个
    assert all(e["by"] == "manual" for e in s.viewers.values()) and len(s) == 4


def test_marking_an_auto_entry_by_hand_upgrades_it_and_it_stops_expiring(tmp_path):
    s = store(tmp_path)
    s.add([UID], "Ana", by="auto", cat="threat", ts=NOW)
    status, primary, _ = s.add([UID, "s:abc"], "Ana", by="manual", ts=NOW + 1)
    assert (status, primary) == ("upgraded", UID)
    assert s.entry(UID)["by"] == "manual" and s.entry(UID)["keys"] == [UID, "s:abc"]
    assert s.add(["bad\x07key", "", None, "x" * 97], "x", by="manual")[0] == "bad_key"


# ---- 自动加入的口径（I14，spec §5.7）------------------------------------------------------

def feed(rt, items, now=1_000.0):
    return rt.observe(items, now=now)


def test_auto_add_only_from_a_confirmed_red_tier_hit_with_a_stable_key(monkeypatch, tmp_path, writes):
    rt = new_runtime(monkeypatch, tmp_path)

    async def scenario():
        await start(rt)
        out = feed(rt, [item("c1", "Ana", RED_TEXT, uid=UID, sec="s:MS4wLjAB", handle="ana_demo")])
        assert out[0]["guard"]["lvl"] == "alert" and out[0]["guard"]["tier"] == "red"
        assert out[0]["uid_ok"] is True
        await rt.marks.flush_now()

    run(scenario())
    entry = rt.marks.entry(UID)
    assert entry["by"] == "auto" and entry["cat"] == "threat" and entry["keys"] == [UID, "s:MS4wLjAB"]
    assert entry["handle"] == "ana_demo" and rt.marked_auto == 1
    assert len(writes) == 1 and read(tmp_path)["viewers"][UID]["by"] == "auto"
    assert "te voy a matar" not in json.dumps(read(tmp_path))      # 名单里没有弹幕原文


def test_what_never_adds_anyone_and_never_writes(monkeypatch, tmp_path, writes):
    """链接、电话组合、刷屏三种、琥珀、模型升红：各造一例，名单不变、零写盘。"""
    rt = new_runtime(monkeypatch, tmp_path)

    async def scenario():
        await start(rt)
        # 链接（结构信号的红条）
        out = feed(rt, [item("l1", "Link", LINK_TEXT, uid="101")], now=1000.0)
        assert out[0]["guard"]["lvl"] == "alert" and out[0]["guard"]["tier"] == "struct"
        # 电话组合（promo_contact）
        out = feed(rt, [item("p1", "Phone", "mandame whatsapp al 5512345678 y te lo vendo mas barato",
                             uid="102")], now=1001.0)
        assert out[0]["guard"]["lvl"] == "alert" and out[0]["guard"]["cats"] == ["promo_contact"]
        # C1 单人高频
        for i in range(6):
            out = feed(rt, [item("b%d" % i, "Burst", "texto numero %s hermoso" % "abcdef"[i], uid="103")],
                       now=1010.0 + i * 1.2)
        assert out[0]["guard"]["cats"] == ["flood_user"] and out[0]["guard"]["lvl"] == "alert"
        # C3 同人同句
        for i in range(5):
            out = feed(rt, [item("s%d" % i, "Same", "quiero el combo rosa para mi hermana", uid="104")],
                       now=1100.0 + i * 5)
        assert out[0]["guard"]["cats"] == ["flood_repeat"] and out[0]["guard"]["lvl"] == "alert"
        # C2 多账号同句：琥珀事件
        for i in range(6):
            out = feed(rt, [item("a%d" % i, "Acc%d" % i, "participo en el sorteo", uid="2%d" % i)],
                       now=1200.0 + i * 1.5)
        assert out[0]["guard"]["lvl"] == "amber" and out[0]["guard"]["amb"] == "brigade"
        # 琥珀（没问号的短断言）
        out = feed(rt, [item("m1", "Maybe", "es falso", uid="105")], now=1300.0)
        assert out[0]["guard"]["lvl"] == "amber"
        # 模型意见（影子期只在旁边写一句，不改档位，更不会加人）
        rt._apply_review("m1", "agree")
        assert rt._ann["m1"]["rv"] == "agree" and rt._ann["m1"]["lvl"] == "amber"
        await rt.marks.flush_now()

    run(scenario())
    assert len(rt.marks) == 0 and rt.marked_auto == 0
    assert writes == [] and not (tmp_path / "marked").exists()


@pytest.mark.parametrize("extra,uid_src", [
    ({}, "handle"),                                    # 只有 handle：键不稳
    ({"role": "mod"}, "id"),                           # 房管
    ({"role": "anchor"}, "id"),                        # 主播本人
])
def test_red_hit_without_a_stable_key_or_from_a_trusted_role_is_not_added(monkeypatch, tmp_path, writes,
                                                                          extra, uid_src):
    rt = new_runtime(monkeypatch, tmp_path)

    async def scenario():
        await start(rt)
        out = feed(rt, [item("c1", "Ana", RED_TEXT, uid=UID if uid_src == "id" else "h:ana", uid_src=uid_src,
                             **extra)])
        await rt.marks.flush_now()
        return out

    out = run(scenario())
    assert len(rt.marks) == 0 and writes == []
    assert out[0].get("uid_ok") is (uid_src == "id")


def test_red_hit_from_a_nick_only_viewer_is_not_added(monkeypatch, tmp_path, writes):
    rt = new_runtime(monkeypatch, tmp_path)

    async def scenario():
        await start(rt)
        out = feed(rt, [item("c1", "Ana", RED_TEXT)])
        await rt.marks.flush_now()
        return out

    out = run(scenario())
    assert "uid_ok" not in out[0] and out[0]["guard"]["lvl"] == "alert"
    assert len(rt.marks) == 0 and writes == []


def test_red_hit_that_the_rules_call_uncertain_is_not_added(monkeypatch, tmp_path, writes):
    rt = new_runtime(monkeypatch, tmp_path)

    async def scenario():
        await start(rt)
        feed(rt, [item("q1", "Ana", ROUTED_TEXT, uid=UID)])           # 问句里的断言词：note，不是红条
        await rt.marks.flush_now()

    run(scenario())
    assert len(rt.marks) == 0 and writes == []


def test_a_marked_viewer_is_recognised_by_any_key_and_updated_not_duplicated(monkeypatch, tmp_path):
    rt = new_runtime(monkeypatch, tmp_path)

    async def scenario():
        await start(rt)
        rt.mark(UID, True, nick="Ana", handle="ana_demo")
        assert rt.marks.entry(UID)["by"] == "manual"
        # 同一个人这次只带了 sec：认得出，并把多出来的键并进来
        feed(rt, [item("c1", "Ana B", "hola buenas", uid="s:MS4wLjAB", uid_src="sec_uid", sec=None)],
             now=1000.0)
        rt.marks.touch(UID, ["s:MS4wLjAB"], "Ana B", 1000)
        assert rt.marks.match(["s:MS4wLjAB"]) == UID
        # 又一条红条的发言不会再自动加一个人
        feed(rt, [item("c2", "Ana", RED_TEXT, uid=UID)], now=1001.0)
        assert len(rt.marks) == 1 and rt.marked_auto == 0 and rt.marks.entry(UID)["hits"] >= 1

    run(scenario())


def test_trusted_accounts_are_exempt_they_get_no_verdict_and_are_never_added(monkeypatch, tmp_path, writes):
    """可信名单（spec §3.3）：role 是主播/房管的、本机 trusted/<主播>.txt 里的 uid、主播自己的 handle，
    整条豁免——不判定，不自动加入。"""
    trusted_dir = tmp_path / "no-local-dir" / "trusted"
    trusted_dir.mkdir(parents=True)
    (trusted_dir / (STREAMER + ".txt")).write_text("# 助理\n999000111\n", encoding="utf-8")
    rt = new_runtime(monkeypatch, tmp_path)

    async def scenario():
        await start(rt)
        out = feed(rt, [
            item("a1", "Mod", RED_TEXT, uid="1", role="mod"),
            item("a2", "Host", RED_TEXT, uid="2", role="anchor"),
            item("a3", "Helper", RED_TEXT, uid="999000111"),                       # 本机 trusted 文件里的 uid
            item("a4", "Me", RED_TEXT, uid="h:luna_demo", uid_src="handle", handle="Luna_Demo"),   # 主播自己的账号
            item("a5", "Stranger", RED_TEXT, uid="5")])                             # 对照：普通观众照常判
        await rt.marks.flush_now()
        return out

    out = run(scenario())
    assert [("guard" in o) for o in out] == [False, False, False, False, True]
    assert list(rt.marks.viewers) == ["5"] and rt.marked_auto == 1
    assert rt.counts == {"threat": {"alert": 1, "amber": 0, "note": 0}}


# ---- 写盘失败不丢脏标记：收尾和下一次改动都会再试 ----------------------------------------------------

def fail_first_write(monkeypatch):
    """_write_safe 第一次返回一句错误说明，之后恢复正常（磁盘满了一下又好了）。"""
    real = comment_marks._write_safe
    calls = {"n": 0}

    def flaky(path, text):
        calls["n"] += 1
        if calls["n"] == 1:
            return "OSError: No space left on device"
        return real(path, text)

    monkeypatch.setattr(comment_marks, "_write_safe", flaky)
    return calls


def test_a_failed_write_keeps_the_dirty_flag_so_the_end_of_session_flush_retries(tmp_path, monkeypatch,
                                                                                 fast_save):
    calls = fail_first_write(monkeypatch)
    s = store(tmp_path)

    async def scenario():
        await s.load(STREAMER)
        s.add([UID], "Ana", by="manual")
        await asyncio.sleep(0.3)
        assert s.write_errors == 1 and s.failing and calls["n"] == 1
        assert not (tmp_path / "marked" / (STREAMER + ".json")).exists()
        await s.flush_now()                           # 收尾：脏标记还在，再写一次
        assert not s.failing

    run(scenario())
    assert calls["n"] == 2 and list(read(tmp_path)["viewers"]) == [UID]


def test_a_failed_last_seen_only_write_is_retried_at_the_end_too(tmp_path, monkeypatch, fast_save):
    monkeypatch.setattr(comment_marks, "LAZY_SAVE_SEC", 0.05)
    s = store(tmp_path)

    async def scenario():
        await s.load(STREAMER)
        s.add([UID], "Ana", by="manual")
        await asyncio.sleep(0.2)                      # 第一次写盘正常
        fail_first_write(monkeypatch)
        s.touch(UID, [], "Ana", NOW + 5)              # 只是 last_seen / 条数变了：轻量脏标记
        await asyncio.sleep(0.3)
        assert s.failing and s.write_errors == 1
        await s.flush_now()

    run(scenario())
    assert read(tmp_path)["viewers"][UID]["hits"] == 1 and read(tmp_path)["viewers"][UID]["last_seen"] == NOW + 5


def test_after_a_failed_write_the_next_change_writes_everything(tmp_path, monkeypatch, fast_save):
    fail_first_write(monkeypatch)
    s = store(tmp_path)

    async def scenario():
        await s.load(STREAMER)
        s.add(["100"], "A", by="manual")
        await asyncio.sleep(0.3)
        assert s.failing
        s.add(["200"], "B", by="manual")              # 磁盘好了，下一次改动照常排一次写盘
        await asyncio.sleep(0.3)
        assert not s.failing

    run(scenario())
    assert sorted(read(tmp_path)["viewers"]) == ["100", "200"]


def test_loading_another_streamer_resets_the_failing_flag(tmp_path, monkeypatch, fast_save):
    fail_first_write(monkeypatch)
    s = store(tmp_path)

    async def scenario():
        await s.load(STREAMER)
        s.add([UID], "Ana", by="manual")
        await asyncio.sleep(0.3)
        assert s.failing
        await s.load("marca_demo")
        assert not s.failing

    run(scenario())


# ---- 一些只在规格里写了数字、测试却引用符号的口径 --------------------------------------------------------

def test_the_limits_are_the_numbers_in_the_spec():
    assert comment_marks.MAX_VIEWERS == 500
    assert comment_marks.SAVE_DELAY_SEC == 2.0 and comment_marks.LAZY_SAVE_SEC == 60.0
    assert comment_marks.AUTO_EXPIRE_SEC == 30 * 86400
    assert comment_marks.MAX_KEYS == 4 and comment_marks.MAX_AKA == 3


def test_structural_changes_are_scheduled_2_seconds_out_and_last_seen_ones_60(tmp_path):
    s = store(tmp_path)

    async def scenario():
        await s.load(STREAMER)
        loop = asyncio.get_running_loop()
        delays = []
        real = loop.call_later
        loop.call_later = lambda delay, callback, *args: (delays.append(delay), real(delay, callback, *args))[1]
        s.add([UID], "Ana", by="manual")
        await s.flush_now()
        s.touch(UID, [], "Ana", NOW + 5)
        s.close()
        return delays

    assert run(scenario()) == [2.0, 60.0]


def test_the_write_syncs_the_tmp_file_before_it_replaces_the_real_one(tmp_path, monkeypatch):
    order = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(os, "replace", lambda a, b: (order.append("replace"), real_replace(a, b))[1])
    comment_marks._write_atomic(tmp_path / "marked" / "x.json", '{"a": 1}')
    assert order == ["fsync", "replace"]


# ---- 关注名单里的人又发言了：接线（touch）要钉住 -------------------------------------------------------

def test_a_marked_viewer_speaking_again_updates_hits_last_seen_and_new_keys(monkeypatch, tmp_path):
    rt = new_runtime(monkeypatch, tmp_path)
    marks_sent = []

    async def broadcast(msg):
        if msg["type"] == "guard_mark":
            marks_sent.append(msg)

    async def scenario():
        await start(rt)
        feed(rt, [item("c1", "Ana", RED_TEXT, uid=UID, sec="s:AAA")], now=1000.0)      # 自动加入
        assert rt.marks.entry(UID)["keys"] == [UID, "s:AAA"] and rt.marked_auto == 1
        await rt.flush(broadcast)
        del marks_sent[:]

        # 第一次再发言：带了一个新的 handle——键并进来，hits / last_seen 前进，恰好排一条 guard_mark
        out = feed(rt, [item("h0", "Ana", "hola buenas tardes", uid=UID, sec="s:AAA", handle="ana_demo")],
                   now=2000.0)
        assert "guard" not in out[0]
        entry = rt.marks.entry(UID)
        assert entry["hits"] == 1 and entry["last_seen"] == 2000
        assert entry["keys"] == [UID, "s:AAA", "h:ana_demo"] and rt.marks.match(["h:ana_demo"]) == UID
        assert list(rt._pending_marks) == [UID]
        await rt.flush(broadcast)
        assert [m["key"] for m in marks_sent] == [UID] and marks_sent[0]["entry"]["keys"][-1] == "h:ana_demo"
        del marks_sent[:]

        # 之后的发言没有任何新东西：计数照涨，但一条 guard_mark 都不排
        feed(rt, [item("h1", "Ana", "gracias por la info", uid=UID, sec="s:AAA", handle="ana_demo")], now=2001.0)
        feed(rt, [item("h2", "Ana", "me interesa el rosa", uid=UID, sec="s:AAA", handle="ana_demo")], now=2002.0)
        entry = rt.marks.entry(UID)
        assert entry["hits"] == 3 and entry["last_seen"] == 2002 and rt._pending_marks == {}
        await rt.flush(broadcast)
        assert marks_sent == []

        # 换了个昵称：aka 记下来，排一条
        feed(rt, [item("h3", "Ana B", "hola otra vez", uid=UID, sec="s:AAA", handle="ana_demo")], now=2003.0)
        assert rt.marks.entry(UID)["aka"] == ["Ana B"] and list(rt._pending_marks) == [UID]
        assert rt.marks.entry(UID)["hits"] == 4
        # 名单里的人再说一次红条的话：不会再自动加一个人
        feed(rt, [item("h4", "Ana", "ojala te mueras", uid=UID, sec="s:AAA", handle="ana_demo")], now=2004.0)
        assert len(rt.marks) == 1 and rt.marked_auto == 1

    run(scenario())


def test_a_marked_viewer_who_keeps_talking_does_not_expire_after_30_days(monkeypatch, tmp_path):
    base = 1_760_000_000
    day = 86400
    rt = new_runtime(monkeypatch, tmp_path)
    audit = object()

    async def scenario():
        await start(rt, audit=audit)
        feed(rt, [item("c1", "Ana", RED_TEXT, uid=UID), item("c2", "Bea", RED_TEXT, uid="2")], now=base)
        feed(rt, [item("h1", "Ana", "hola buenas tardes", uid=UID)], now=base + 20 * day)   # 一直在聊
        await rt.end(audit)                                                                  # 收尾强制写盘
        later = MarkStore(directory=tmp_path / "marked", clock=lambda: base + 40 * day)
        await later.load(STREAMER)
        return later

    later = run(scenario())
    assert list(later.viewers) == [UID]                    # 40 天前加入、但 20 天前还发过言的留下；沉默的 Bea 过期
    assert later.entry(UID)["last_seen"] == base + 20 * day and later.entry(UID)["hits"] == 1


def test_handles_match_whatever_their_case(monkeypatch, tmp_path):
    rt = new_runtime(monkeypatch, tmp_path)

    async def scenario():
        await start(rt)
        rt.mark("h:ana_demo", True, nick="Ana")
        feed(rt, [item("c1", "Ana B", "hola buenas tardes", handle="Ana_Demo")], now=1000.0)

    run(scenario())
    assert rt.marks.entry("h:ana_demo")["hits"] == 1


def test_a_nickname_only_viewer_is_flood_checked_and_a_nick_mark_matches_them(monkeypatch, tmp_path):
    rt = new_runtime(monkeypatch, tmp_path)

    async def scenario():
        await start(rt)
        out = None
        for i in range(6):                                  # 没有 uid / sec / handle：只有昵称这一把钥匙
            out = feed(rt, [item("b%d" % i, "Burst", "texto numero %s hermoso" % "abcdef"[i])],
                       now=1010.0 + i * 1.2)
        assert out[0]["guard"]["lvl"] == "alert" and out[0]["guard"]["cats"] == ["flood_user"]
        assert rt.marked_auto == 0 and len(rt.marks) == 0   # 刷屏、昵称都不自动加人
        rt.mark("n:Burst", True, nick="Burst")              # 手动加入：键是 n:<昵称>，只在本场有效
        feed(rt, [item("b9", "Burst", "hola buenas")], now=1040.0)
        # 带 uid 的同昵称观众不是同一个人：不能被昵称键认走
        feed(rt, [item("b10", "Burst", "hola de nuevo", uid="555")], now=1041.0)

    run(scenario())
    entry = rt.marks.entry("n:Burst")
    assert entry["session"] is True and entry["hits"] == 1
