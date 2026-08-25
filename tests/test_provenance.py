"""语料来源清单。

起因是一次真实污染：一个测试建审计日志时漏传目录，往生产 logs/ 写了 106 个
会话、371 段，占我口中「真实字幕」的两成。清理和 conftest 的防线只堵住那一次，
真正的防线是分析工具别再见文件就信。
"""
# ---- 语料来源 --------------------------------------------------------

def test_fixture_sessions_never_enter_the_corpus(tmp_path):
    """一个漏传 log_dir 的测试曾往生产 logs/ 写了 106 个会话、371 段，
    占我口中「真实字幕」的两成。清理和 conftest 防线只堵住那一次；真正的
    防线是分析工具**别再见文件就信**。"""
    import json

    from app import provenance

    def write(name, url, text="hola mundo bonito"):
        (tmp_path / name).write_text(
            json.dumps({"type": "session_start", "room_url": url}) + "\n"
            + json.dumps({"type": "segment", "text": text}) + "\n",
            encoding="utf-8")

    write("session-1.jsonl", "https://www.tiktok.com/@x/live")
    write("session-2.jsonl", "https://www.tiktok.com/@a/live")
    write("session-3.jsonl", "https://cdn.example.com/room/stream.flv")
    write("session-4.jsonl", "https://www.tiktok.com/@susanm00/live")
    got = provenance.corpus(log_dir=tmp_path)
    assert [m["streamer"] for m in got] == ["susanm00"]


def test_a_session_with_no_captions_is_not_corpus(tmp_path):
    import json

    from app import provenance

    (tmp_path / "session-5.jsonl").write_text(
        json.dumps({"type": "session_start",
                    "room_url": "https://www.tiktok.com/@real/live"}) + "\n",
        encoding="utf-8")
    assert provenance.corpus(log_dir=tmp_path) == []


def test_corpus_can_be_narrowed_to_one_streamer(tmp_path):
    """hold-out 评测只看一个主播，别把别人的语料混进去。"""
    import json

    from app import provenance

    for i, who in enumerate(("susanm00", "bellaallnatural"), 1):
        (tmp_path / "session-{}.jsonl".format(i)).write_text(
            json.dumps({"type": "session_start",
                        "room_url": "https://www.tiktok.com/@{}/live".format(who)})
            + "\n" + json.dumps({"type": "segment", "text": "hola mundo"}) + "\n",
            encoding="utf-8")
    assert len(provenance.corpus(log_dir=tmp_path, streamer="susanm00")) == 1
