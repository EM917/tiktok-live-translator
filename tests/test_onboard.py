"""新主播接入审计工具。

存在的理由：规则本身不泛化，但发现规则的流程泛化。同一套称呼清洗，主播①
1604 句触发 0.4%，主播② 183 句触发 32.8%——80 倍。给所有主播套同一份清单
一定错，而每接一个新主播先扫一轮语料，可以标准化。

**只提建议，永不自己改生产规则。** 今天多个「看起来合理的优化」跑完语料发现
是净负收益（跳过专有名词里的数字：救 2 伤 3；全量爱称映射：修 2 条却造出
「爱邮递员」）。所以必须 发现→度量→找反例→A/B→人工拍板。
"""
from tools.onboard_streamer import _PREP, verdict, vocative_audit


def test_a_term_after_a_preposition_is_not_a_form_of_address():
    """"Le ayuda mucho a mi mami" 里的 mami 是宾语，真的是某人的妈妈。
    这条正是当初手工把 mami 排除的依据，现在是程序判据。"""
    rows = vocative_audit(["Hola mami, ¿cómo estás?"],
                          corpus=["Le ayuda mucho a mi mami."])
    mami = [r for r in rows if r["term"] == "mami"][0]
    assert mami["counter"], "介词后的 mami 应当被记为反例"
    assert verdict(mami).startswith("拒绝")


def test_a_term_after_an_indefinite_article_is_a_predicate():
    """"Sintiéndome como una reina" 是比喻不是称呼。只看一场语料时这条
    查不出来——反例必须跨全部日志找。"""
    rows = vocative_audit(["Vaya y haga su check out, reina."],
                          corpus=["Sintiéndome como una reina. Es rico."])
    reina = [r for r in rows if r["term"] == "reina"][0]
    assert reina["counter"] and verdict(reina).startswith("拒绝")


def test_a_clean_term_is_offered_as_a_candidate():
    lines = ["Vamos amores!", "Aprovechen amores.", "Gracias amores."]
    rows = vocative_audit(lines, corpus=lines)
    amores = [r for r in rows if r["term"] == "amores"][0]
    assert amores["edge"] == 3
    assert verdict(amores).startswith("★")


def test_thin_evidence_is_not_a_candidate():
    """一两次出现不足以立规则。"""
    rows = vocative_audit(["Gracias corazón."], corpus=["Gracias corazón."])
    row = [r for r in rows if r["term"] == "corazón"][0]
    assert "证据不足" in verdict(row)


def test_a_term_the_glossary_already_maps_needs_a_human_call():
    """chicas 已映射成「家人们」。摘掉会丢「在对观众说话」这个信息——
    这是权衡，不是工具能替人做的判断。"""
    lines = ["Vamos chicas!", "Aprovechen chicas.", "Gracias chicas."]
    rows = vocative_audit(lines, corpus=lines)
    row = [r for r in rows if r["term"] == "chicas"][0]
    assert "人工权衡" in verdict(row, mapped={"chicas"})


def test_prepositions_and_articles_are_both_counterexample_signals():
    for prefix in ("le ayuda a ", "le ayuda mucho a mi ",
                   "sintiéndome como una ", "el precio de "):
        assert _PREP.search(prefix), prefix
    for plain in ("buenos días ", "vamos ", "gracias "):
        assert not _PREP.search(plain), plain
