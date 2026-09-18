"""手机同看的三个设置键。

要求 1 是「默认关闭」：settings.json 里没有 viewer_share_enabled 就当关闭，
绝不能因为读不到而默认打开——那等于在用户不知道的情况下把字幕放到局域网上。

另一条相反方向的性质：关闭同看**不**清掉 token。贴在墙上的二维码还要能用，
只有中控明确点「换一个链接」才换。
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app import pipeline as pipeline_mod
from app import settings as settings_mod
from app import viewer as viewer_mod
from app.pipeline import Pipeline


class StubServer:
    port = 8765

    def __init__(self):
        self.config = {}
        self.messages = []
        self.viewer = None
        self.history = []
        self.alerts = []
        self.comments = []

    async def status(self, state, detail="", command=None):
        self.messages.append({"type": "status", "state": state, "detail": detail})

    async def broadcast(self, msg):
        self.messages.append(msg)

    def of_type(self, mtype):
        return [m for m in self.messages if m.get("type") == mtype]


class FakeHub:
    """不绑任何端口的观众面替身。真绑端口的那条在 tests/test_viewer_e2e.py。"""

    def __init__(self, ports, token, fail=None):
        self._ports = list(ports)
        self.token = token
        self.fail = fail
        self.running = False
        self.count = 0
        self.stopped = []
        self.rotated = []

    @property
    def port(self):
        return self._ports[0] if self._ports else None

    async def start(self):
        if self.fail is not None:
            raise self.fail
        self.running = True

    async def stop(self, reason="off"):
        self.stopped.append(reason)
        self.running = False

    def rotate(self, token):
        self.token = token
        self.rotated.append(token)

    def url(self, ip):
        return viewer_mod.viewer_url(ip, self.port, self.token) if ip else None

    def state(self, ip_info=None, qr_rows=None, ip_changed=None):
        info = ip_info or {}
        ips = list(info.get("all") or [])
        url = self.url(info.get("ip")) if self.running else None
        return {"on": self.running, "port": self.port, "url": url,
                "ip": info.get("ip"), "ips": ips,
                "ambiguous": bool(info.get("ambiguous")), "viewers": self.count,
                "max_viewers": viewer_mod.MAX_VIEWERS, "qr_rows": qr_rows,
                "note": viewer_mod.share_note(
                    self.running, port=self.port, url=url, ip=info.get("ip"),
                    ips=ips, viewers=self.count, qr_ok=qr_rows is not None,
                    ip_changed=ip_changed, ports=self._ports)}


@pytest.fixture
def setup(monkeypatch, tmp_path):
    """真的 Pipeline（要验证 __init__ 也把同看那几个字段建好了），但所有
    会碰端口、碰 socket、碰模型的地方都换成替身。"""
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", tmp_path / "settings.json")
    terms = tmp_path / "banned_terms.txt"
    terms.write_text("", encoding="utf-8")
    monkeypatch.setattr(pipeline_mod, "TERMS_FILE", terms)
    args = SimpleNamespace(
        cookies=None, target="zh-CN", translator="none", source="es", beam=5,
        context=False, asr_temperature=None, glossary=None, backend="auto",
        model=None, device="auto", compute_type="auto", denoise="off",
        banned_terms=None)
    server = StubServer()
    pipeline = Pipeline(args, server)
    made = []
    state = {"fail": None}

    def make(ports, token):
        hub = FakeHub(ports, token, fail=state["fail"])
        made.append(hub)
        return hub

    monkeypatch.setattr(pipeline, "_make_viewer_hub", make)

    async def fake_ip():
        return {"ip": "192.168.1.23", "all": ["192.168.1.23"], "source": "route",
                "ambiguous": False}

    monkeypatch.setattr(pipeline, "_viewer_ip_info", fake_ip)
    monkeypatch.setattr(pipeline, "_start_viewer_ip_watch", lambda: None)
    return SimpleNamespace(pipeline=pipeline, server=server, made=made,
                           state=state, path=settings_mod.SETTINGS_FILE)


def saved(setup):
    if not setup.path.exists():
        return {}
    return json.loads(setup.path.read_text(encoding="utf-8"))


def test_missing_settings_means_off(setup):
    """文件都不存在：不开，也不留下任何监听面。"""
    asyncio.run(setup.pipeline.restore_viewer_share())
    assert setup.server.viewer is None
    assert setup.made == []
    published = setup.server.of_type("viewer")
    assert published and published[-1]["on"] is False
    assert setup.server.config["viewer"]["on"] is False


def test_explicit_false_also_means_off(setup):
    settings_mod.save_setting("viewer_share_enabled", False)
    asyncio.run(setup.pipeline.restore_viewer_share())
    assert setup.server.viewer is None


def test_turning_it_on_persists_all_three_keys(setup):
    asyncio.run(setup.pipeline._set_viewer_share(True))
    data = saved(setup)
    assert data["viewer_share_enabled"] is True
    assert data["viewer_port"] == 8766
    assert viewer_mod.sanitize_token(data["viewer_token"]) == data["viewer_token"]
    assert setup.server.viewer is setup.made[-1]


def test_turning_it_off_keeps_the_token(setup):
    """墙上那张二维码还要能用：关掉同看不是「作废链接」。"""
    asyncio.run(setup.pipeline._set_viewer_share(True))
    token = saved(setup)["viewer_token"]
    asyncio.run(setup.pipeline._set_viewer_share(False))
    data = saved(setup)
    assert data["viewer_share_enabled"] is False
    assert data["viewer_token"] == token
    assert setup.server.viewer is None
    assert setup.made[-1].stopped == ["operator"]


def test_rotate_changes_the_token_and_keeps_it_valid(setup):
    asyncio.run(setup.pipeline._set_viewer_share(True))
    before = saved(setup)["viewer_token"]
    asyncio.run(setup.pipeline._rotate_viewer_token())
    after = saved(setup)["viewer_token"]
    assert after != before
    assert viewer_mod.sanitize_token(after) == after
    assert setup.made[-1].rotated == [after]


def test_rotate_works_even_when_sharing_is_off(setup):
    """中控可以先作废旧链接再打开——不该因为当下没开着就什么都不做。"""
    settings_mod.save_setting("viewer_token", "z" * viewer_mod.TOKEN_LEN)
    asyncio.run(setup.pipeline._rotate_viewer_token())
    assert saved(setup)["viewer_token"] != "z" * viewer_mod.TOKEN_LEN


@pytest.mark.parametrize("bad", ["abc", 12345, None, ["x"], "", "a" * 44])
def test_a_hand_edited_token_is_replaced_and_written_back(setup, bad):
    settings_mod.save_setting("viewer_token", bad)
    asyncio.run(setup.pipeline._set_viewer_share(True))
    token = saved(setup)["viewer_token"]
    assert viewer_mod.sanitize_token(token) == token
    assert token != bad


@pytest.mark.parametrize("bad", [0, "x", 99999, 80, None, True])
def test_a_hand_edited_port_falls_back_to_the_default_candidates(setup, bad):
    settings_mod.save_setting("viewer_port", bad)
    asyncio.run(setup.pipeline._set_viewer_share(True))
    assert setup.made[-1].port == 8766
    assert saved(setup)["viewer_port"] == 8766


def test_a_saved_port_is_tried_first(setup):
    settings_mod.save_setting("viewer_port", 9100)
    asyncio.run(setup.pipeline._set_viewer_share(True))
    assert setup.made[-1].port == 9100
    assert saved(setup)["viewer_port"] == 9100


def test_restore_brings_the_share_back_after_a_restart(setup):
    settings_mod.save_setting("viewer_share_enabled", True)
    settings_mod.save_setting("viewer_token", "q" * viewer_mod.TOKEN_LEN)
    asyncio.run(setup.pipeline.restore_viewer_share())
    assert setup.server.viewer is setup.made[-1]
    assert setup.made[-1].token == "q" * viewer_mod.TOKEN_LEN   # 同一把钥匙，同一张码
