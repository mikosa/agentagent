import asyncio

from app.controller import Controller
from app.cron import next_due


class FakeCodex:
    def __init__(self):
        self.calls = []

    async def request(self, method, params):
        self.calls.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "worker-thread"}}
        if method == "thread/fork":
            return {"thread": {"id": "reflection-thread"}}
        if method == "turn/start":
            return {"turn": {"id": "reflection-turn" if params["threadId"] == "reflection-thread" else "worker-turn"}}
        return {}


def test_reflection_runs_once_and_publishes_summary(tmp_path):
    async def scenario():
        rpc = FakeCodex()
        app = Controller(tmp_path / "db.sqlite", rpc)
        sid = await app.create_session("Old", "Goal", str(tmp_path))
        await app.submit(sid, "work")
        await app.dispatch_once()
        await app.handle_notification("turn/completed", {"threadId": "worker-thread", "turn": {"id": "worker-turn", "status": "completed"}})
        assert len(app.pending_reflections()) == 1
        assert await app.dispatch_reflection_once()
        assert next(params for method, params in rpc.calls if method == "thread/fork")["sandbox"] == "read-only"
        await app.handle_notification("item/completed", {"threadId": "reflection-thread", "turnId": "reflection-turn", "item": {"id": "answer", "type": "agentMessage", "text": '{"rename":{"required":true,"new_title":"New"},"summary":"Done","split":{"recommended":false}}'}})
        await app.handle_notification("turn/completed", {"threadId": "reflection-thread", "turn": {"id": "reflection-turn", "status": "completed"}})
        assert app.session(sid)["title"] == "New"
        assert app.session(sid)["summary"] == "Done"
        assert ("thread/name/set", {"threadId": "worker-thread", "name": "New"}) in rpc.calls
        assert not app.pending_reflections()
        assert not await app.dispatch_reflection_once()
        reflection = app.db.one("SELECT id FROM reflections WHERE worker_turn_id=?", (app.turns(sid)[0]["id"],))
        assert app.reflection_items(reflection["id"])[0]["payload"]["text"].startswith('{"rename"')

    asyncio.run(scenario())


def test_pause_blocks_reflection(tmp_path):
    async def scenario():
        rpc = FakeCodex()
        app = Controller(tmp_path / "db.sqlite", rpc)
        sid = await app.create_session("A", "Goal", str(tmp_path))
        await app.submit(sid, "work")
        await app.dispatch_once()
        await app.handle_notification("turn/completed", {"threadId": "worker-thread", "turn": {"id": "worker-turn", "status": "completed"}})
        await app.pause()
        assert not await app.dispatch_reflection_once()
        assert not any(method == "thread/fork" for method, _ in rpc.calls)

    asyncio.run(scenario())


def test_stop_all_interrupts_reflection_as_well_as_worker(tmp_path):
    async def scenario():
        rpc = FakeCodex()
        app = Controller(tmp_path / "db.sqlite", rpc)
        sid = await app.create_session("A", "Goal", str(tmp_path))
        await app.submit(sid, "work")
        await app.dispatch_once()
        await app.handle_notification("turn/completed", {"threadId": "worker-thread", "turn": {"id": "worker-turn", "status": "completed"}})
        await app.dispatch_reflection_once()
        await app.stop_all()
        assert ("turn/interrupt", {"threadId": "reflection-thread", "turnId": "reflection-turn"}) in rpc.calls
        assert app.status()["paused"]

    asyncio.run(scenario())


def test_single_reflection_can_be_interrupted(tmp_path):
    async def scenario():
        rpc = FakeCodex()
        app = Controller(tmp_path / "db.sqlite", rpc)
        sid = await app.create_session("A", "Goal", str(tmp_path))
        await app.submit(sid, "work")
        await app.dispatch_once()
        await app.handle_notification("turn/completed", {"threadId": "worker-thread", "turn": {"id": "worker-turn", "status": "completed"}})
        await app.dispatch_reflection_once()
        reflection = app.db.one("SELECT id FROM reflections WHERE status='running'")
        await app.stop_reflection(reflection["id"])
        assert rpc.calls[-1] == ("turn/interrupt", {"threadId": "reflection-thread", "turnId": "reflection-turn"})
        assert app.db.one("SELECT status FROM reflections WHERE id=?", (reflection["id"],))["status"] == "interrupt_requested"

    asyncio.run(scenario())


def test_failed_reflection_can_be_requeued(tmp_path):
    async def scenario():
        rpc = FakeCodex()
        app = Controller(tmp_path / "db.sqlite", rpc)
        sid = await app.create_session("A", "Goal", str(tmp_path))
        await app.submit(sid, "work")
        await app.dispatch_once()
        await app.handle_notification("turn/completed", {"threadId": "worker-thread", "turn": {"id": "worker-turn", "status": "completed"}})
        await app.dispatch_reflection_once()
        await app.handle_notification("item/completed", {"threadId": "reflection-thread", "turnId": "reflection-turn", "item": {"type": "agentMessage", "text": "invalid JSON"}})
        await app.handle_notification("turn/completed", {"threadId": "reflection-thread", "turn": {"id": "reflection-turn", "status": "completed"}})
        reflection = app.db.one("SELECT * FROM reflections WHERE worker_turn_id=?", (app.turns(sid)[0]["id"],))
        assert reflection["status"] == "failed"
        app.retry_reflection(reflection["id"])
        assert len(app.pending_reflections()) == 1

    asyncio.run(scenario())


def test_cron_next_due_uses_timezone_and_step():
    assert next_due("*/15 * * * *", "UTC", "2026-10-27T12:01:00+00:00") == "2026-10-27T12:15:00+00:00"


def test_cron_day_of_month_and_weekday_follow_standard_or_rule():
    assert next_due("0 9 1 * 1", "UTC", "2026-10-27T12:01:00+00:00") == "2026-11-01T09:00:00+00:00"
