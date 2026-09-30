"""tools/readme_shots.py 不开 Chrome 的那一半：场景组得起来、数据是 README 要的那种干净数据、
动图时间轴先原文后译文、输出路径对、直播转写进行中拒绝运行、出图前的核对真的会拦。

开 Chrome 出图不在这里跑（要一整个浏览器进程），见工具文件头。"""
import json
import re

import pytest

from app import i18n
from tests.i18n_dom import scenarios as S
from tools import readme_shots as R

CJK = re.compile(r"[㐀-䶿一-鿿]")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = {}
    for lang in R.LANGS:
        tmp = tmp_path_factory.mktemp("readme-" + lang)
        scenes = R.build(lang, tmp)
        scenes[R.GIF] = R.demo(lang, tmp)
        out[lang] = scenes
    return out


def _hello(scene):
    return scene["messages"][0]["config"]


def test_every_scene_builds_in_both_languages(built):
    for lang in R.LANGS:
        assert set(built[lang]) == set(R.SCENES) | {R.GIF}
        for name, sc in built[lang].items():
            assert json.loads(json.dumps(sc)) == sc, name        # scenario.js 里原样可用
            assert sc["lang"] == sc["page_lang"] == lang, name
            want = R.PHONE if name == "phone" else R.DESKTOP
            assert tuple(sc["size"]) == tuple(want), name
            if sc["page"] == "desktop":
                config = _hello(sc)
                assert config["ui_lang"] == lang and config["target_lang"] == R.TARGET[lang]


def test_readme_data_is_fictional_and_the_english_set_has_no_chinese(built):
    real = {S.STREAMER, *S.RECENT, S.BRAND_ZH}              # G10 场景里借用的名字，README 不用
    for lang in R.LANGS:
        for name, sc in built[lang].items():
            text = json.dumps(sc, ensure_ascii=False)
            assert not [r for r in real if r in text], (lang, name)
            if lang == "en":
                assert not CJK.search(text), (name, CJK.findall(text)[:20])
    live = built["zh"]["live"]["messages"]
    zh = [m["translated"] for m in live if m["type"] == "caption" and m.get("translated")]
    assert zh == [row[2] for row in R.CAPTIONS]              # 中文那套译成中文
    en = [m["translated"] for m in built["en"]["live"]["messages"]
          if m["type"] == "caption" and m.get("translated")]
    assert en == [row[1] for row in R.CAPTIONS]
    assert all(m["src_lang"] == "es" for m in live if m["type"] == "caption")


def test_scenes_are_the_clean_state(built):
    """README 的图不带提示条：自检全绿、没有持续提示、没有识别积压、没有报警。"""
    for lang in R.LANGS:
        for name, sc in built[lang].items():
            types = {m["type"] for m in sc["messages"]}
            assert not types & {"health", "incident", "alert", "alert_update"}, (lang, name)
            if sc["page"] == "desktop":
                config = _hello(sc)
                summary = config["selfcheck"]["summary"]
                assert summary["fail"] == 0 and summary["warn"] == 0, summary
                assert not config.get("incidents")
                assert not config["alerts_enabled"]                # 报警默认关着
        phone = built[lang]["phone"]["messages"]
        # 手机页：报警开着、没有命中，顶上不会常驻「违禁词警示已关闭」
        assert {"type": "alert_mode", "on": True} in phone
        assert phone[-1]["type"] != "status" or phone[-1]["state"] == "live"


def test_live_scenes_fit_one_screen(built):
    for lang in R.LANGS:
        live = [m for m in built[lang]["live"]["messages"] if m["type"] == "caption"]
        assert len(live) == len(R.CAPTIONS) and live[-1].get("restore")   # 底部大字幕恢复成最后一句
        demo = [m for m in built[lang][R.GIF]["messages"] if m["type"] == "caption"]
        assert len(demo) == R.DEMO_HISTORY
        assert R.DEMO_HISTORY + len(R.DEMO_CAPTIONS) <= len(R.CAPTIONS)


@pytest.mark.parametrize("lang", R.LANGS)
def test_demo_timeline_sends_the_original_first_then_the_translation(lang):
    steps = R.timeline(lang)
    assert steps[0] == (0.0, [])                            # 开场帧
    times = [t for t, _ in steps]
    assert times == sorted(times) and len(set(times)) == len(times)
    assert 10 <= R.DEMO_END_SEC <= 15 and times[-1] < R.DEMO_END_SEC
    seen = {}
    for t, msgs in steps:
        for m in msgs:
            key = (m["type"].split("_")[0], m["id"])
            if m["type"] in ("caption", "comment"):
                assert key not in seen
                seen[key] = t
                assert m.get("translated") is None           # 原文先到，还没有译文
            else:
                assert m["type"] in ("caption_update", "comment_update"), m
                gap = t - seen[key]
                assert 0.5 <= gap <= 1.5, (key, gap)          # 译文后补，与 pipeline 同一量级
                assert m["translated"] and bool(CJK.search(m["translated"])) == (lang == "zh")
    caps = [k for k in seen if k[0] == "caption"]
    assert len(caps) == len(R.DEMO_CAPTIONS)
    assert [k[1] for k in caps] == list(range(R.DEMO_HISTORY + 1,
                                              R.DEMO_HISTORY + 1 + len(R.DEMO_CAPTIONS)))


def test_output_paths(tmp_path, capsys):
    assert R.shot_path(tmp_path, "en", "live") == tmp_path / "assets/screenshots/en/live.png"
    assert R.gif_path(tmp_path, "zh") == tmp_path / "assets/demo.zh.gif"
    assert R.main(["--list", "--out", str(tmp_path)]) == 0
    listed = capsys.readouterr().out
    for lang in R.LANGS:
        for name in R.SCENES:
            assert str(R.shot_path(tmp_path, lang, name)) in listed
        assert str(R.gif_path(tmp_path, lang)) in listed
    assert not list(tmp_path.iterdir())                     # --list 不写任何文件
    R.main(["--list", "--lang", "en", "--only", "phone", "--out", str(tmp_path)])
    assert capsys.readouterr().out.split() == ["en", "phone", str(R.shot_path(tmp_path, "en", "phone"))]
    R.main(["--list", "--lang", "zh", "--only", "demo", "--out", str(tmp_path)])   # 只要动图
    assert capsys.readouterr().out.split() == ["zh", "demo", str(R.gif_path(tmp_path, "zh"))]
    assert R.build("en", tmp_path / "none", []) == {}          # 一张截图都不要时不组场景


def test_refuses_while_live_transcription_runs(monkeypatch):
    calls = []

    class Hit:
        returncode = 0

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return Hit()

    monkeypatch.setattr(R.subprocess, "run", fake_run)
    monkeypatch.setattr(R.G10, "find_chrome", lambda *a: pytest.fail("直播中不该去找 Chrome"))
    with pytest.raises(SystemExit, match="直播转写仍在进行"):
        R.run(["en"], ["home"], False, "/nonexistent")
    assert calls == [["pgrep", "-f", "tiktokcdn"]]


def _facts(**over):
    facts = {"page": "desktop", "status": "Live · luna.demo", "conn": None, "stream": None,
             "caps": len(R.CAPTIONS), "comments": 5, "liveBar": True,
             "startPanel": False, "commentPanel": True, "switchPanel": False,
             "switchArmed": False, "sharePanel": False, "qr": 0, "langBody": False,
             "brandTag": True, "bars": [], "expanded": {}, "cut": [], "clipped": [],
             "visible": [{"at": "div", "text": "Today we have free shipping", "data": True}],
             "viewport": {"width": R.DESKTOP[0], "height": R.DESKTOP[1]}}
    facts.update(over)
    return facts


def test_the_pre_shot_check_catches_what_the_readme_must_not_show():
    assert R.problems("live", "en", _facts()) == []
    assert R.problems("live", "en", _facts(status="Starting…"))              # 数据没到
    assert R.problems("live", "en", _facts(caps=3))
    assert R.problems("live", "en", _facts(bars=["health-bar"]))
    assert R.problems("live", "en", _facts(cut=["span#status-text：Live · @luna.de…"]))
    assert R.problems("live", "en", _facts(clipped=["19:57:30 ES Hoy les…"]))
    assert R.problems("live", "en", _facts(viewport={"width": 1280, "height": 760}))
    leak = [{"at": "span.set-name", "text": "违禁词报警", "data": False}]
    assert R.problems("live", "en", _facts(visible=leak))                    # 英文图里有汉字
    assert R.problems("live", "zh", _facts(visible=leak, status="直播中 · luna.demo")) == []
    autonym = [{"at": "button#lang-toggle", "text": "中文", "data": True}]
    phone = _facts(page="phone", status=None, conn="Connected", caps=len(R.PHONE_CAPTIONS),
                   visible=autonym, viewport={"width": R.PHONE[0], "height": R.PHONE[1]})
    assert R.problems("phone", "en", phone) == []                            # 语言自称不算
    assert R.problems("phone", "en", dict(phone, conn="Connecting…"))


def test_the_page_gets_a_push_hook_after_the_fake_websocket():
    site = R.ReadmeSite(R.G10.WEB_DIR)
    try:
        site.scenario = {"page": "desktop", "page_lang": "en"}
        body, ctype = site.resolve("/static/fake_ws.js")
        original = (R.G10.FIXTURE_DIR / "fake_ws.js").read_bytes()
        assert body == original + R.PUSH_JS.encode("utf-8") and ctype.startswith("application/javascript")
        assert site.resolve("/static/scan.js")[0] == (R.G10.FIXTURE_DIR / "scan.js").read_bytes()
        assert site.resolve("/static/app.js")[0] == (R.G10.WEB_DIR / "app.js").read_bytes()
    finally:
        site.server_close()


def test_the_english_set_is_rendered_in_english():
    """后端文字由生产代码按界面语言生成：英文那套里自检行是英文的。"""
    with i18n.use("en"):
        rows = R._clean_checks()
    names = [i18n.render(r["name"], "en") for r in rows]
    assert "Speech Recognition" in names and not any(CJK.search(n) for n in names)
