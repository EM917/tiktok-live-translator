"""asr._FilterMixin：质量过滤 + 幻觉丢弃 + 滚动上下文（纯逻辑，两后端共用）。"""
from app.asr import _FilterMixin


class Filter(_FilterMixin):
    def __init__(self):
        self._context = ""


def fold(segs, lang="en", f=None):
    f = f or Filter()
    return f._fold(iter(segs), lang), f


def test_normal_speech_passes_and_feeds_context():
    (text, lang), f = fold([(0.1, 1.2, -0.3, " Hello world ")])
    assert text == "Hello world"
    assert lang == "en"
    assert "Hello world" in f._context        # 进入滚动上下文


def test_high_no_speech_prob_dropped():
    (text, _), _ = fold([(0.95, 1.2, -0.3, "ghost words")])
    assert text == ""


def test_repetition_dropped():
    (text, _), _ = fold([(0.1, 3.0, -0.3, "la la la la")])   # 复读机式压缩比
    assert text == ""


def test_low_confidence_dropped():
    (text, _), _ = fold([(0.1, 1.2, -2.0, "background music noise")])
    assert text == ""


def test_hallucination_with_low_confidence_dropped():
    # 静音段的经典幻觉：置信度低时整条丢弃
    (text, _), _ = fold([(0.1, 1.2, -0.9, "Thank you.")])
    assert text == ""


def test_hallucination_text_with_high_confidence_kept():
    # 主播真的在说 thank you（高置信）不能误杀
    (text, _), _ = fold([(0.1, 1.2, -0.1, "Thank you.")])
    assert text == "Thank you."


def test_punctuation_only_dropped():
    """音乐/噪声段 Whisper 常吐一个裸 "!"——不是话，不该占字幕更不该送翻译
    （实测一场里 13 段 "!" 被原样翻译上屏）。"""
    result, f = fold([(0.1, 1.2, -0.3, "!")])
    assert result.text == ""
    assert result.rejected[0]["reason"] == "punctuation"
    assert result.raw_text == "!"             # 违禁词检测的输入不受影响
    assert f._context == ""                   # 垃圾不进滚动上下文


def test_digits_survive_punctuation_filter():
    """价格和数量是合规要看的内容，纯数字段不能当垃圾丢。"""
    result, _ = fold([(0.1, 1.2, -0.3, "30?")])
    assert result.text == "30?"


def test_context_rolls_at_400_chars():
    f = Filter()
    f._context = "x" * 400
    (text, _), _ = fold([(0.1, 1.2, -0.2, "new words")], f=f)
    assert text == "new words"
    assert len(f._context) <= 400
    assert f._context.endswith("new words")


# ---- 幻觉过滤：Whisper 会在无人声段吐训练数据里的片尾套话 ----

def test_spanish_youtube_outro_dropped():
    """实测一场西语带货直播里 14% 的字幕是这种幻觉——而当时词表里
    只有英文和中文条目，西语的全部漏了过去。"""
    for text in ("¡Gracias por ver el video!", "¡SUSCRIBETE!",
                 "Gracias por ver este video", "Suscríbete al canal"):
        (out, _), _ = fold([(0.1, 1.2, -0.9, text)])
        assert out == "", text


def test_multiple_outros_in_one_segment_dropped():
    """Whisper 常把几句片尾语粘成一段，只比对整段就会漏掉。"""
    (out, _), _ = fold([(0.1, 1.2, -0.9,
                         "¡Gracias por ver el video! ¡Suscríbete al canal!")])
    assert out == ""


def test_real_speech_containing_gracias_survives():
    """主播真的在句子里说 gracias 不能被误杀——只有整段全是套话才丢。"""
    (out, _), _ = fold([(0.1, 1.2, -0.9, "Gracias por la rosa, se los agradezco")])
    assert "Gracias" in out
    (out2, _), _ = fold([(0.1, 1.2, -0.9, "Gracias por ver. Ahora vamos con la moringa.")])
    assert "moringa" in out2       # 一半是真话，整段不能丢


# ---- 字幕组署名：表里带 "." 的条目曾经永远比不中 ----

def test_amara_credit_dropped_in_every_language_even_with_high_confidence():
    """「Subtitles by the Amara.org community」2026-09-17 线上原样上屏：
    归一化保留句号做拆句，amara.org 里的点把一句拆成两半，表里三条 amara
    条目从未命中过。署名不是任何主播会说的话，置信度再高也丢。"""
    for text in ("Subtitles by the Amara.org community",
                 "Subtítulos realizados por la comunidad de Amara.org",
                 "字幕由Amara.org社区提供",
                 "Legendas pela comunidade Amara.org"):
        (out, _), _ = fold([(0.1, 1.2, -0.1, text)])
        assert out == "", text


def test_table_entries_with_punctuation_are_live():
    """表里写着 "thanks for watching!"（带叹号）这类条目也要能比中。"""
    (out, _), _ = fold([(0.1, 1.2, -0.9, "Thanks for watching!")])
    assert out == ""


def test_decimal_numbers_are_not_split_into_hallucination_parts():
    """摘掉词内的点不能误伤：价格是合规要看的内容，整句必须保留。"""
    (out, _), _ = fold([(0.1, 1.2, -0.9, "Cuesta 3.5 dólares, chicas.")])
    assert "3.5" in out


# ---- 网址署名 / 文字系统离群（2026-09-17 一天里上屏的 45 条幻觉）----

def test_url_credit_line_dropped_even_with_high_confidence():
    """「Más información www.cdc.gov.ar」一天里 5 条上屏：带网址的短句是训练集署名。"""
    for text in ("Más información www.cdc.gov.ar",
                 "Más información www.albertosanagustin.com", "www.example.com"):
        (out, _), _ = fold([(0.1, 1.2, -0.1, text)])
        assert out == "", text


def test_long_sentence_with_a_url_is_kept():
    """长句里夹一个网址可能是主播真在念，不动。"""
    (out, _), _ = fold([(0.1, 1.2, -0.3,
                         "Chicas entren a la página oficial www.tienda.com para ver todos los precios de hoy")])
    assert "www.tienda.com" in out


def test_foreign_script_outlier_dropped_after_latin_history():
    """西语直播里突然一段韩文/阿拉伯文：最近接受的字幕里没这种文字，扣下。"""
    f = Filter()
    for i in range(6):
        fold([(0.1, 1.2, -0.3, "Hola chicas, bienvenidas número {}".format(i))], f=f)
    for text in ("안녕하세요 여러분.", "المترجمات لكثير من الاشتراك في القناة",
                 "И вот она, новая маска."):
        res = f._fold(iter([(0.1, 1.2, -0.1, text)]), "es")
        assert res.text == "", text
        assert text in res.raw_text        # 违禁词检测的输入一个字都不少
        assert res.rejected[-1]["reason"] == "script_outlier"


def test_a_real_language_switch_costs_only_the_first_segment():
    """同一种文字连着来两段就当真换了语言：第二段起放行，之后进入历史。"""
    f = Filter()
    for i in range(6):
        fold([(0.1, 1.2, -0.3, "Hola chicas {}".format(i))], f=f)
    (a, _), _ = fold([(0.1, 1.2, -0.2, "안녕하세요 여러분")], f=f)
    (b, _), _ = fold([(0.1, 1.2, -0.2, "오늘은 립스틱을 소개합니다")], f=f)
    (c, _), _ = fold([(0.1, 1.2, -0.2, "이 제품은 정말 좋아요")], f=f)
    assert a == "" and b != "" and c != ""


def test_no_script_rule_without_history():
    """开播头几段没有参照，不按文字系统判。"""
    (out, _), _ = fold([(0.1, 1.2, -0.2, "안녕하세요 여러분")])
    assert out != ""


def test_mixed_spanish_english_stream_is_never_an_outlier():
    """西语和英语同是拉丁字母：主播中英西夹杂不受影响。"""
    f = Filter()
    for i in range(8):
        fold([(0.1, 1.2, -0.3, "Hola chicas {}".format(i))], f=f)
    (out, _), _ = fold([(0.1, 1.2, -0.2, "Oh my god it's so good, twenty dollars")], f=f)
    assert "twenty" in out


# ---- YouTube 片尾：不看置信度（2026-10-14：114 份日志里按旧表有 276 段整段套话上了屏）----

def test_youtube_outro_dropped_even_with_high_confidence():
    """「¡Gracias por ver el video!」的置信度常贴着 -0.6，拦没拦住近乎抛硬币：10-14 直播录音
    离线重跑，第 549 段就是 -0.38 上的屏。字幕组署名、提到「视频」的片尾语、「¡Suscríbete al
    canal!」整段只有这些就是训练集套话，置信度再高也丢；raw_text 照旧留给违禁词检测。"""
    for text in ("¡Gracias por ver el video!", "¡Gracias por ver este video!",
                 "¡Suscríbete al canal!", "Gracias por ver el video.",
                 "¡Gracias por ver el video! ¡Suscríbete al canal!",
                 "¡Gracias por ver este video! ¡Suscríbete al canal para no perderte los próximos videos!",
                 "Si te gustó el video, dale like, suscríbete y comparte."):
        res, f = fold([(0.1, 1.2, -0.38, text)])
        assert res.text == "", text
        assert res.raw_text == text                       # 违禁词检测的输入不受影响
        assert res.rejected[-1]["reason"] == "hallucination"
        assert f._context == ""                           # 套话不进滚动上下文


def test_every_youtube_outro_entry_is_live():
    """表里每一条都要真能比中：条目中间带了句末标点（拆成两句），这一条就永远比不中。"""
    from app.asr import _HALLUCINATIONS, _YOUTUBE_OUTROS
    assert _YOUTUBE_OUTROS <= _HALLUCINATIONS
    for entry in _YOUTUBE_OUTROS:
        (out, _), _ = fold([(0.1, 1.2, -0.1, entry)])
        assert out == "", entry


def test_ambiguous_phrase_mixed_with_an_outro_dropped():
    """整段都是套话、其中有一句只可能是片尾，那句 thank you 也不是主播说的。"""
    (out, _), _ = fold([(0.1, 1.2, -0.2, "Thank you. ¡Suscríbete al canal!")])
    assert out == ""


# 主播真会说的话：TikTok 有订阅和点赞，有主播把自己的页面叫「mi canal」，双语主播会用英文
# 道别。这些放进「不看置信度」那一层就会误丢真话，钉住它们只在低置信时才丢。
STREAMER_SAYS = ("¡Suscríbete!", "¡Nos vemos!", "Música", "¡Hasta la próxima!", "Gracias por ver.",
                 "Dale like y suscríbete.", "¡Suscríbete a mi canal!", "¡Suscríbete a nuestro canal!",
                 "No olvides suscribirte.", "Thank you for watching!", "Please subscribe.",
                 "Thank you.", "Bye!", "Gracias por su atención.")


def test_phrases_a_streamer_really_says_still_need_low_confidence():
    for text in STREAMER_SAYS:
        (out, _), _ = fold([(0.1, 1.2, -0.2, text)])
        assert out == text, text
        (low, _), _ = fold([(0.1, 1.2, -0.9, text)])
        assert low == "", text                            # 低置信时照旧当套话丢


def test_every_conditional_entry_needs_low_confidence():
    """要看置信度那一层逐条钉住：哪一条被挪进 _YOUTUBE_OUTROS，这里就红。"""
    from app.asr import _HALLUCINATIONS, _YOUTUBE_OUTROS
    for entry in _HALLUCINATIONS - _YOUTUBE_OUTROS:
        (out, _), _ = fold([(0.1, 1.2, -0.2, entry)])
        assert out == entry, entry
        (low, _), _ = fold([(0.1, 1.2, -0.9, entry)])
        assert low == "", entry


def test_split_keeps_every_old_entry():
    """分层前的表一条都不能丢（2026-10-14 分层时的全集）。"""
    from app.asr import _HALLUCINATIONS
    old = {
        "thank you", "thanks for watching", "thank you for watching", "you",
        "please subscribe", "subscribe", "bye", "so", "thanks for watching!", "see you next time",
        "subtitles by the amara.org community", "subtitles by amara.org",
        "subtitled by the amara.org community", "legendas pela comunidade amara.org",
        "sous-titres realises par la communaute d'amara.org",
        "gracias por ver", "gracias por ver el video", "gracias por ver este video", "gracias por vernos",
        "suscribete", "suscribete al canal", "suscribanse", "no olvides suscribirte",
        "no olviden suscribirse", "hasta la proxima", "nos vemos", "gracias por su atencion",
        "subtitulos realizados por la comunidad de amara.org", "mas videos", "dale like y suscribete",
        "aplausos", "musica", "risas", "mas informacion", "obrigado", "obrigada",
        "字幕由amara.org社区提供", "请不吝点赞订阅转发打赏支持明镜与点点栏目", "谢谢观看", "请订阅",
        "감사합니다", "구독과 좋아요 부탁드립니다", "다음 영상에서 만나요",
        "المترجمات لكثير من الاشتراك في القناة", "اشتركوا في القناة",
        "субтитры сделал dimatorzok", "субтитры создавал dimatorzok", "продолжение следует",
        "ご視聴ありがとうございました", "チャンネル登録お願いします", "תודה רבה",
    }
    assert old <= _HALLUCINATIONS


def test_outro_words_inside_real_speech_survive():
    """日志里有一段识别成「Suscríbete al canal y usa los cupones de TikTok」：整段不全是套话就不动。"""
    for text in ("Suscríbete al canal y usa los cupones de TikTok.",
                 "Gracias por ver el video. Ahora vamos con la moringa."):
        (out, _), _ = fold([(0.1, 1.2, -0.2, text)])
        assert out == text, text


def test_empty_and_punctuation_only_input_is_not_a_hallucination():
    from app.asr import _has_youtube_outro, _is_all_hallucination
    for text in ("", None, ".", "¡¿?!", "..."):
        assert not _is_all_hallucination(text), text
        assert not _has_youtube_outro(text), text
