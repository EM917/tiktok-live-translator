"""后端的英文场景（spec §12.1 G7 / G8）。

G8：审计和终端与界面语言无关。同一组场景分别在 use("zh") 和 use("en") 下跑，审计 JSONL 写进
临时目录、终端输出由 capsys 抓下，去掉时刻字段后两次必须**逐字节相同**——「终端、日志、审计
永远是中文」（不变量 2）由此从推论变成测试事实。同时断言英文那一次本机页面收到的确实是
英文：不然「两次一样」可能只是因为英文模式根本没生效。

这里是骨架（C4）：场景用的是生产里接受调用方文字的几个出口（_incident、_announce_health、
_publish_selfcheck），句子由测试写成 L()。各迁移提交（M 系列）把自己改过的产出函数加成新场景，
并按 G7 断言自己拥有的句子英文里没有 CJK；跨模块的组合留给收紧闸的提交（Z0）。
"""
import json

from app import i18n
from app.audit import AuditLog
from app.i18n import CJK, L
from app.pipeline import Pipeline
from app.server import CaptionServer
from tests.helpers import run

TIME_KEYS = ("at", "ts", "started_at", "ended_at")


class FakeClient:
    def __init__(self):
        self.got = []

    async def send_json(self, msg):
        self.got.append(msg)


def _pipeline(log_dir):
    """半成品 Pipeline：真的 CaptionServer（带一个本机页面替身）和真的 AuditLog。"""
    p = Pipeline.__new__(Pipeline)
    p.server = CaptionServer(port=0)
    client = FakeClient()
    p.server.clients.add(client)
    p.server._transports[client] = None
    p.audit = AuditLog(room_url="https://www.tiktok.com/@demo/live", log_dir=log_dir)
    return p, client


def _audit_lines(path):
    """审计记录去掉时刻字段后重新编码。键序照原样：比较的是写出来的样子。"""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        out.append(json.dumps({k: v for k, v in rec.items() if k not in TIME_KEYS},
                              ensure_ascii=False))
    return out


def run_in_both_languages(scenario, tmp_path, capsys):
    """scenario(pipeline) 返回一个协程。分别在中文、英文下跑一遍，返回
    {lang: (审计行, 终端输出, 本机页面收到的消息)}。"""
    results = {}
    for lang in (i18n.ZH, i18n.EN):
        i18n._bad_once.clear()            # 坏模板的警告每个进程只打一次：两次各打各的
        capsys.readouterr()
        with i18n.use(lang):
            p, client = _pipeline(tmp_path / lang)
            run(scenario(p))
            p.audit.close(reason="user_stop")
        results[lang] = (_audit_lines(p.audit.path), capsys.readouterr().out, client.got)
    return results


def assert_language_independent(results):
    zh_audit, zh_out, _ = results[i18n.ZH]
    en_audit, en_out, _ = results[i18n.EN]
    assert en_audit == zh_audit
    assert en_out == zh_out
    assert not [line for line in en_audit + en_out.splitlines()
                if "Possible" in line or "asleep" in line or "falling behind" in line]


# ---- 场景 ---------------------------------------------------------------------------------

SLEPT = L("电脑休眠过 {} 秒，这段时间没有监听", "The computer was asleep for {} sec. Nothing was "
          "monitored during that time.")
LAGGING = L("⚠️ 识别开始落后（积压 {:.0f} 秒），报警会相应延迟",
            "⚠️ Speech recognition is falling behind ({:.0f} sec backlog). Alerts will be delayed.")
CHECKS = [
    {"name": L("语音识别", "Speech Recognition"), "level": "fail",
     "detail": L("模型没加载上", "The model didn’t load."),
     "fix": L("关掉程序再打开", "Quit and reopen the app.")},
    {"name": L("人声降噪", "Noise Reduction"), "level": "ok",
     "detail": L("已开启", "On"), "fix": ""},
]


async def _lifecycle(p):
    await p._incident("session:sleep", "warn", SLEPT.format(30))
    await p._announce_health("lagging", 12.0, text=LAGGING.format(12.0))
    await p._publish_selfcheck(CHECKS)
    await p._incident("session:sleep", "clear")


def test_audit_and_terminal_do_not_depend_on_the_ui_language(tmp_path, capsys):
    results = run_in_both_languages(_lifecycle, tmp_path, capsys)
    assert_language_independent(results)
    zh_audit, zh_out, zh_got = results[i18n.ZH]
    # 审计与终端确实写了这些句子（中文），不是两边都空
    assert "[提示] 电脑休眠过 30 秒，这段时间没有监听" in zh_out
    assert "[健康] ⚠️ 识别开始落后（积压 12 秒），报警会相应延迟" in zh_out
    types = [json.loads(line)["type"] for line in zh_audit]
    assert types == ["session_start", "health", "selfcheck", "session_end"]
    health = json.loads(zh_audit[1])
    assert health["text"] == "⚠️ 识别开始落后（积压 12 秒），报警会相应延迟"
    selfcheck = json.loads(zh_audit[2])
    assert [c["name"] for c in selfcheck["checks"]] == ["语音识别", "人声降噪"]
    # 本机页面：中文那次是中文，英文那次是英文
    _, _, en_got = results[i18n.EN]
    by_type = {m["type"]: m for m in en_got if m["type"] != "incident"}
    assert en_got[0]["text"] == "The computer was asleep for 30 sec. Nothing was monitored " \
                                "during that time."
    assert by_type["health"]["text"].startswith("⚠️ Speech recognition is falling behind (12 sec")
    assert [c["name"] for c in by_type["selfcheck"]["checks"]] == \
        ["Speech Recognition", "Noise Reduction"]
    assert zh_got[0]["text"] == "电脑休眠过 30 秒，这段时间没有监听"


def test_english_desktop_text_from_these_producers_has_no_chinese(tmp_path, capsys):
    """G7 的形状：渲染后登记为界面文字的字段里没有 CJK。自检那条持续提示的句子由
    pipeline._selfcheck_incident_text 拼（M5a 迁移），这里跳过它，只看本场景自己的句子。"""
    _, _, en_got = run_in_both_languages(_lifecycle, tmp_path, capsys)[i18n.EN]
    for msg in en_got:
        if msg.get("id") == Pipeline.SELFCHECK_INCIDENT:
            continue
        for path, value in i18n.ui_values(msg):
            assert not (isinstance(value, str) and CJK.search(value)), (msg["type"], path, value)
