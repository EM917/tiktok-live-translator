"""持续提示（incident）：各项保险共用的「直到清除才消失」的界面提示。

钉住：按 id 覆盖且保留最初出现的时间；level=clear 去掉；落进 config 让重连的页面
照样看得到；最多 20 条；"session:" 开头的在下一场开始时清掉，其余的保留。"""

from app.pipeline import Pipeline
from app.server import CaptionServer
from tests.helpers import run


def test_incidents_are_kept_in_config_updated_by_id_and_cleared():
    s = CaptionServer(port=8765)

    async def scenario():
        await s.broadcast({"type": "incident", "id": "session:sleep", "level": "warn",
                           "text": "电脑休眠过", "ts": 100.0})
        await s.broadcast({"type": "incident", "id": "session:sleep", "level": "error",
                           "text": "电脑休眠过两次", "ts": 200.0})
        first = dict(s.config["incidents"]["session:sleep"])
        await s.broadcast({"type": "incident", "id": "session:sleep", "level": "clear", "ts": 300.0})
        return first

    first = run(scenario())
    assert first["text"] == "电脑休眠过两次" and first["level"] == "error"
    assert first["since"] == 100.0 and first["ts"] == 200.0
    assert "session:sleep" not in s.config["incidents"]


def test_incidents_are_capped_dropping_the_stalest():
    s = CaptionServer(port=8765)

    async def scenario():
        for n in range(25):
            await s.broadcast({"type": "incident", "id": "k{}".format(n), "level": "warn",
                               "text": str(n), "ts": float(n)})

    run(scenario())
    keys = set(s.config["incidents"])
    assert len(keys) == 20 and "k0" not in keys and "k24" in keys


def test_incident_without_id_is_ignored():
    s = CaptionServer(port=8765)
    run(s.broadcast({"type": "incident", "level": "warn", "text": "x"}))
    assert not s.config.get("incidents")


def test_pipeline_incident_helper_and_session_scoped_clearing():
    p = Pipeline.__new__(Pipeline)
    p.server = CaptionServer(port=8765)

    async def scenario():
        await p._incident("session:audit", "error", "审计日志写不进去")
        await p._incident("settings-corrupt", "warn", "设置文件损坏，已备份")
        await p._clear_session_incidents()

    run(scenario())
    assert set(p.server.config["incidents"]) == {"settings-corrupt"}
