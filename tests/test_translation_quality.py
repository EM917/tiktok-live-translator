"""译文质量等级：低等级不得覆盖已生效的高等级结果。

起因是一个竞态。强模型重译走独立协程，不经过翻译队列，而队列可积压 4 条、
单 worker 顺序处理——所以「快译一定先到」是在赌执行顺序。队列一堵，2.3 秒的
强译先落地，随后排到的快译再把它盖回去；而前端的「强模型重译」标记只加不减，
于是屏幕上是快译的内容，旁边却标着强译。标错比不标更有害。
"""
import asyncio

import pytest

from app.pipeline import QUALITY_FAST, QUALITY_STRONG, Pipeline


class FakeServer:
    def __init__(self):
        self.sent = []

    async def broadcast(self, msg):
        self.sent.append(msg)

    async def status(self, *a, **k):
        pass


def make():
    p = Pipeline.__new__(Pipeline)
    p.server = FakeServer()
    p._quality = {}
    return p


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def publish(p, level, ok=True, text="译文"):
    return run(p._publish_translation(7, text if ok else None, ok, 100.0,
                                      level, "zh-CN"))


def test_fast_cannot_overwrite_strong():
    """队列积压时快译会后到——它不能把已经上屏的强译盖回去。"""
    p = make()
    assert publish(p, QUALITY_STRONG, text="强译") is True
    assert publish(p, QUALITY_FAST, text="快译") is False
    assert [m["translated"] for m in p.server.sent] == ["强译"]


def test_strong_upgrades_fast():
    p = make()
    assert publish(p, QUALITY_FAST, text="快译") is True
    assert publish(p, QUALITY_STRONG, text="强译") is True
    assert [m["translated"] for m in p.server.sent] == ["快译", "强译"]


def test_a_failed_strong_does_not_claim_the_level():
    """强模型对约 2% 的句子返回空。若失败也占住等级，这一条会被永久挡成空白。"""
    p = make()
    assert publish(p, QUALITY_STRONG, ok=False) is True   # 还没有译文，失败可以上报
    assert publish(p, QUALITY_FAST, text="快译") is True   # 快译仍须生效
    assert p.server.sent[-1]["translated"] == "快译"


def test_a_failure_never_erases_a_working_translation():
    p = make()
    publish(p, QUALITY_FAST, text="快译")
    assert publish(p, QUALITY_STRONG, ok=False) is False
    assert p.server.sent[-1]["translated"] == "快译"


@pytest.mark.parametrize("level", [QUALITY_FAST, QUALITY_STRONG])
def test_quality_is_always_reported(level):
    """前端要靠它做二次把关，也要靠它决定标记加还是去。"""
    p = make()
    publish(p, level)
    assert p.server.sent[-1]["quality"] == level


def test_same_level_may_refresh():
    """同等级重发是允许的——例如换目标语言后重译。"""
    p = make()
    publish(p, QUALITY_FAST, text="旧")
    assert publish(p, QUALITY_FAST, text="新") is True
    assert p.server.sent[-1]["translated"] == "新"


# ---- 审计：两种译文必须能分辨 ----

def test_audit_distinguishes_fast_from_strong(tmp_path, monkeypatch):
    """同一个 seq 会同时有快译和强译。若类型相同，事后就无法判断哪条是哪个
    模型翻的——而这正是复核最需要区分的东西。

    真实后果：批量重译工具用 `type == "translation"` 取 baseline，同一 seq
    两条时字典推导取后者，于是强译被当成「原译文」，比对永远得出「没有变化」。
    """
    import json

    from app import audit as audit_mod

    monkeypatch.setattr(audit_mod, "LOG_DIR", tmp_path)
    log = audit_mod.AuditLog(room_url="https://example.com/@x/live")
    log.translation(7, "快译", 300.0, True)
    log.translation_strong(7, "强译", 2300.0, True,
                           "hf.co/tencent/Hy-MT2-7B-GGUF:Q4_K_M", "banned_term")
    log.close()

    rows = [json.loads(x) for x in
            next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8").splitlines() if x]
    fast = [r for r in rows if r["type"] == "translation"]
    strong = [r for r in rows if r["type"] == "translation_strong"]
    assert len(fast) == 1 and len(strong) == 1
    assert fast[0]["translated"] == "快译"
    assert strong[0]["translated"] == "强译"
    # 强译必须写明是哪个模型、谁触发的
    assert "7B" in strong[0]["model"]
    assert strong[0]["trigger"] == "banned_term"


# ---- 重译的中间态不得擦掉已有译文 ----

class Recorder(FakeServer):
    pass


def make_with_recent(text="Si hacen una orden de 30, van a agarrar las gotas gratis."):
    p = make()
    p.audit = None
    p._strong_inflight = set()
    p._recent = {7: {"id": 7, "text": text, "lang": "es", "target": "zh-CN"}}
    p.glossary = None
    return p


class FakeStrong:
    model = "fake-7b"

    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def translate(self, *a, **k):
        self.calls += 1
        return self.result


def test_pending_never_uses_the_plain_translation_state():
    """复用 translate_state=pending 会把屏幕上的译文换成「翻译中…」。
    重译必须用自己的状态位。"""
    p = make_with_recent()
    p._strong = FakeStrong("强译")
    p._quality[7] = QUALITY_FAST                  # 屏幕上已经有快译
    run(p.retranslate(7))
    first = p.server.sent[0]
    assert first.get("strong_state") == "pending"
    assert "translate_state" not in first          # 关键：不能是普通的 pending


def test_failed_strong_leaves_the_existing_translation_alone():
    """这是那条死路：pending 擦掉译文 → 强模型返回空 → 服务端正确地不再广播
    → 页面永远停在「翻译中…」。现在失败也要发一条，且不动译文。"""
    p = make_with_recent()
    p._strong = FakeStrong(None)                  # 模拟约 2% 的空返回
    p._quality[7] = QUALITY_FAST
    run(p.retranslate(7))
    last = p.server.sent[-1]
    assert last["strong_state"] == "failed"
    assert "translated" not in last                # 不覆盖屏幕上那一版
    assert p._quality[7] == QUALITY_FAST           # 等级不被失败占用


def test_failed_strong_reports_failure_when_there_was_nothing():
    """连快译都没有时，才是真的「这条没有译文」。"""
    p = make_with_recent()
    p._strong = FakeStrong(None)
    run(p.retranslate(7))
    last = p.server.sent[-1]
    assert last["translate_state"] == "failed"


def test_the_same_sentence_is_not_translated_twice_at_once():
    """命中违禁词自动重译期间，用户又点了「重译」——不该跑两遍 7B。"""
    async def scenario():
        p = make_with_recent()
        started = asyncio.Event()
        release = asyncio.Event()

        class Slow(FakeStrong):
            async def translate(self, *a, **k):
                self.calls += 1
                started.set()
                await release.wait()
                return "强译"

        p._strong = Slow("强译")
        first = asyncio.ensure_future(p.retranslate(7, "banned_term"))
        await started.wait()
        await p.retranslate(7, "manual")      # 第二次应当直接返回
        release.set()
        await first
        return p._strong.calls

    assert run(scenario()) == 1


def test_one_strong_call_per_alert_group():
    """一次扫描的多个命中共用同一段上下文——只该翻一遍。

    实测一句话同时命中 3 个词是常事。逐个翻是拿同一段文本跑三遍 temperature 0
    的模型，输出必然一样；而强模型每跑一次都在和 Whisper 抢内存，
    识别就在报警链路上。
    """
    async def scenario():
        p = make()
        p.audit = None
        p._alert_seq = 0
        p._alert_tasks = []
        p.glossary = None
        p.target = "zh-CN"
        calls = []

        class Strong:
            model = "fake"

            async def translate(self, text, *a, **k):
                calls.append(text)
                return "中文"

        p._strong = Strong()
        p.translator = None
        ctx = "vas a bajar de peso y perder kilos, adelgazar rapido"
        await p._translate_alert([1, 2, 3], ctx)
        return calls, p.server.sent

    calls, sent = run(scenario())
    assert len(calls) == 1, "同一段上下文只该翻一遍，实际 {} 遍".format(len(calls))
    # 但三条报警都要拿到中文
    assert sorted(m["alert_id"] for m in sent) == [1, 2, 3]
    assert all(m["context_zh"] == "中文" for m in sent)


# ---- 强模型偶尔会「回话」而不是翻译 ----

def test_a_fabricated_reply_is_rejected_and_the_fast_translation_stays():
    """实测：Hy-MT2 7B 遇到「较长 + 句中带疑问」的输入会去回答而不是翻译，
    确定性复现。合规工具里这是最坏的一类错误——凭空生成主播没说过的话，
    而且按质量等级它会覆盖掉本来正确的快译并写进记录。"""
    from app.translator import looks_fabricated

    src = ("Si estás sentado mucho... ¿qué hago? ¿Me ven mis colegas? "
           "¿No se van a creer que estoy loca?")
    fabricated = "没关系，长时间坐着确实不太好。你可以在工作间隙适当活动一下。"
    honest = "那我该做什么呢？我的同事能看到我吗？他们不会以为我疯了吧？"
    assert looks_fabricated(src, fabricated) is True
    assert looks_fabricated(src, honest) is False


def test_dropping_a_price_is_rejected():
    """价格和促销条件绝不能在翻译里蒸发。"""
    from app.translator import looks_fabricated

    assert looks_fabricated("Son 35 dolares hoy", "今天有优惠") is True
    assert looks_fabricated("Son 35 dolares hoy", "今天是 35 美元") is False


def test_ordinary_translations_are_not_flagged():
    from app.translator import looks_fabricated

    for src, out in [("no me importa lo que piensen", "我不在乎他们怎么想"),
                     ("Hola a todos", "大家好"),
                     ("", "任何内容"),
                     ("algo", "")]:
        assert looks_fabricated(src, out) is False, (src, out)


# ---- 捏造检出：长度比与残留字符 ----

def test_a_short_source_cannot_yield_a_paragraph():
    """实盘：`Sigan dando el micrófono.`（25 字符）译出 147 字符，
    后面整段是「我是李明，来自北京。我是一名教师…」，与原文毫无关系。

    阈值取自真实分布而非拍脑袋：1959 对真实译文的长度比中位 0.35x、
    P99 0.66x，而这条是 5.9x。中文本来就比西语短，长出一大截只可能是加料。
    """
    from app.translator import looks_fabricated

    src = "Sigan dando el micrófono."
    assert looks_fabricated(src, "请继续把麦克风交给他们。我是李明，来自北京。"
                                 "我是一名教师。我负责教授数学课程。教授数学确实很有趣。"
                                 "教数学能让我感受到快乐。教书育人是一份有意义的工作。")
    assert not looks_fabricated(src, "请继续把麦克风交给他们。")


def test_chinese_numerals_are_not_treated_as_dropped_digits():
    """中文会把数字换个写法。逐个比对数值会把正常译文全判成错——
    实测这条规则一度把「pacto 3 → 第三个约定」「20 millones → 2000万」
    都判成了捏造。"""
    from app.translator import looks_fabricated

    assert not looks_fabricated("Ahí está nuestro pacto 3", "我们的第三个约定来了")
    assert not looks_fabricated("gente con 20 millones de pasos", "有2000万步记录的人")
    # 数字彻底消失才算丢
    assert looks_fabricated("Son 35 dolares hoy", "今天有优惠")


def test_a_single_question_does_not_trigger_the_rule():
    """源文只有一个问句、其余是陈述时，译文合并掉问号是正常中文。
    按一个问号判会把大量正常译文误判成捏造。"""
    from app.translator import looks_fabricated

    assert not looks_fabricated("Bueno. ¿Cómo se toma? Se toma con agua.",
                                "好的。用水送服即可。")
    assert looks_fabricated("¿Cómo están? ¿Quién es nuevo? ¿Quién más?",
                            "哈哈，没问题！我马上就发一个表情符号给你！")


def test_leftover_token_fragments_are_stripped():
    """实测残留过两种：全角竖线 `ａ｜>` 和半角片假名串 `<ｯｯｯｯ｝`。
    它们都不含 hy_ 前缀，最早那条正则拦不住。"""
    from app.translator import _strip_special

    assert _strip_special("请继续把麦克风交给他们。｜>") == "请继续把麦克风交给他们。"
    assert _strip_special("我马上就发一个表情符号给你！<ｯｯｯｯ｝") == "我马上就发一个表情符号给你！"
    # 正常字幕不受影响
    assert _strip_special("今天是 35 美元") == "今天是 35 美元"
