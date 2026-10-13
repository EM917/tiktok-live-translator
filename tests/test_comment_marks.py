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

    assert run(scenario()) == "marks_corrupt"
    assert writes == [] and target.exists() and not list(folder.glob("*.bak-*"))


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
