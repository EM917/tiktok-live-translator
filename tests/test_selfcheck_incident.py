"""自检失败要变成顶部持续提示（incident），直播中也能看见。

设置面板里那份自检明细（#start-panel 里的红行）只有待机时看得到——直播开始后
整块面板被隐藏，中控这时候恰恰最需要知道识别、降噪这类核心能力是不是真的在
工作。产品意见 #2、设计意见 #1 末条都是这个缺口：钉住「出现即报、清零即撤、
只对 fail 报、直播中会跟着刷新」这四件事。
"""
from types import SimpleNamespace

from app import selfcheck
from app.pipeline import Pipeline
from app.server import CaptionServer
from tests.helpers import run


def _bare_pipeline():
    p = Pipeline.__new__(Pipeline)
    p.server = CaptionServer(port=8765)
    return p


def _checks(*levels_and_names):
    """快速拼一份自检结果：[(level, name, fix), ...]。"""
    return [selfcheck._check(name, level, "详情：" + name, fix)
            for level, name, fix in levels_and_names]


def test_incident_appears_when_a_check_fails():
    p = _bare_pipeline()
    checks = _checks(("ok", "音频组件 ffmpeg", ""),
                     ("fail", "人声降噪", "删除模型后重新开始，程序会重新下载"))
    run(p._publish_selfcheck(checks))
    incident = p.server.config["incidents"][p.SELFCHECK_INCIDENT]
    assert incident["level"] == "error"
    assert incident["text"] == "自检：人声降噪 未生效——删除模型后重新开始，程序会重新下载"


def test_incident_text_states_the_fact_and_the_fix_not_a_guessed_cause():
    """文案只写观察和能做的事：名字 + fix，不出现「因为/原因是」这类猜测。"""
    p = _bare_pipeline()
    checks = _checks(("fail", "语音识别", "确认网络和磁盘空间后点「开始翻译」重试"))
    run(p._publish_selfcheck(checks))
    text = p.server.config["incidents"][p.SELFCHECK_INCIDENT]["text"]
    assert text.startswith("自检：语音识别 未生效——")
    assert "确认网络和磁盘空间后点「开始翻译」重试" in text
    for guess in ("年龄", "限流", "封禁", "因为"):
        assert guess not in text


def test_incident_reports_count_and_names_when_several_checks_fail():
    p = _bare_pipeline()
    checks = _checks(("fail", "人声降噪", "修法A"),
                     ("ok", "音频组件 ffmpeg", ""),
                     ("fail", "语音识别", "修法B"))
    run(p._publish_selfcheck(checks))
    text = p.server.config["incidents"][p.SELFCHECK_INCIDENT]["text"]
    assert text == "自检：2 项未生效（人声降噪、语音识别）"


def test_warn_alone_does_not_raise_an_incident():
    """warn 是「能用但有取舍」（免费翻译接口限流、领域词表没配……），不该占顶栏。"""
    p = _bare_pipeline()
    checks = _checks(("ok", "音频组件 ffmpeg", ""),
                     ("warn", "领域词表", "编辑 glossary.txt"))
    run(p._publish_selfcheck(checks))
    assert p.SELFCHECK_INCIDENT not in p.server.config.get("incidents", {})


def test_incident_is_withdrawn_once_every_failure_clears():
    p = _bare_pipeline()
    run(p._publish_selfcheck(_checks(("fail", "人声降噪", "修法"))))
    assert p.SELFCHECK_INCIDENT in p.server.config["incidents"]
    run(p._publish_selfcheck(_checks(("ok", "人声降噪", ""))))
    assert p.SELFCHECK_INCIDENT not in p.server.config["incidents"]


def test_incident_does_not_repeat_when_the_same_failure_is_republished():
    """同样的失败重新发布（比如直播中语音识别每轮都刷新一次）不该重复起一条新提示。"""
    p = _bare_pipeline()
    run(p._publish_selfcheck(_checks(("fail", "人声降噪", "修法"))))
    since = p.server.config["incidents"][p.SELFCHECK_INCIDENT]["since"]
    run(p._publish_selfcheck(_checks(("fail", "人声降噪", "修法"))))
    assert p.server.config["incidents"][p.SELFCHECK_INCIDENT]["since"] == since


def test_incident_key_is_not_session_scoped_so_it_survives_a_new_session():
    """自检状态跟哪一场直播无关：下一场开始时的 _clear_session_incidents 不该把它带走，
    否则会在新场次的第一刻假装「没问题」，等下一轮自检跑完才重新报，中间有个空窗。"""
    p = _bare_pipeline()
    run(p._publish_selfcheck(_checks(("fail", "人声降噪", "修法"))))
    run(p._clear_session_incidents())
    assert p.SELFCHECK_INCIDENT in p.server.config["incidents"]


# ---- 直播中自检会变化，要跟着刷新（_refresh_asr_check） -------------------------

def _asr_check_world(monkeypatch):
    async def fake_check_asr(args, state=None):
        state = state or {}
        if state.get("load_error"):
            return selfcheck._check("语音识别", selfcheck.FAIL,
                                    "识别模型没能加载，本场不会识别",
                                    "确认网络和磁盘空间后点「开始翻译」重试")
        return selfcheck._check("语音识别", selfcheck.OK, "ct2 + small")
    monkeypatch.setattr(selfcheck, "check_asr", fake_check_asr)


def test_incident_follows_a_live_asr_load_failure_and_its_recovery(monkeypatch):
    _asr_check_world(monkeypatch)
    p = _bare_pipeline()
    p.args = SimpleNamespace(backend="auto", model=None, device="auto")
    p._asr_load_error = None
    p._asr_fallback = None
    p.server.config["selfcheck"] = {
        "checks": [selfcheck._check("语音识别", selfcheck.OK, "ct2 + small"),
                  selfcheck._check("音频组件 ffmpeg", selfcheck.OK, "实测可用")],
        "summary": {"total": 2, "ok": 2, "warn": 0, "fail": 0}}

    # 开播时模型加载失败：_refresh_asr_check 只重查这一行，顶栏要跟着挂出来
    p._asr_load_error = {"backend": "ct2", "model": "small", "device": "cpu",
                         "error": "Unable to open file 'model.bin'"}
    run(p._refresh_asr_check())
    incident = p.server.config["incidents"][p.SELFCHECK_INCIDENT]
    assert incident["level"] == "error" and "语音识别" in incident["text"]

    # 「停止/开始」之后模型加载恢复：这一行转绿，顶栏要跟着撤
    p._asr_load_error = None
    run(p._refresh_asr_check())
    assert p.SELFCHECK_INCIDENT not in p.server.config["incidents"]


def test_incident_survives_a_session_boundary_across_asr_refresh(monkeypatch):
    """跨场景组合：session 开始清掉的是 "session:" 前缀的提示，自检这条不在其中。"""
    _asr_check_world(monkeypatch)
    p = _bare_pipeline()
    p.args = SimpleNamespace(backend="auto", model=None, device="auto")
    p._asr_load_error = {"backend": "ct2", "model": "small", "device": "cpu",
                         "error": "boom"}
    p._asr_fallback = None
    p.server.config["selfcheck"] = {
        "checks": [selfcheck._check("语音识别", selfcheck.OK, "ct2 + small")],
        "summary": {"total": 1, "ok": 1, "warn": 0, "fail": 0}}
    run(p._refresh_asr_check())
    assert p.SELFCHECK_INCIDENT in p.server.config["incidents"]
    run(p._clear_session_incidents())
    assert p.SELFCHECK_INCIDENT in p.server.config["incidents"]
