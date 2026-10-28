import asyncio
import json

from app.controller import Controller


class FakeCodex:
    async def request(self, method, params):
        if method == "thread/start":
            return {"thread": {"id": "thread"}}
        if method == "turn/start":
            return {"turn": {"id": "turn"}}
        return {}


def test_restart_holds_uncertain_inflight_turn(tmp_path):
    async def scenario():
        path = tmp_path / "db.sqlite"
        app = Controller(path, FakeCodex())
        sid = await app.create_session("A", "Goal", str(tmp_path))
        await app.submit(sid, "work")
        await app.dispatch_once()
        restarted = Controller(path, FakeCodex())
        restarted.recover_inflight()
        assert restarted.session(sid)["held"]
        assert not await restarted.dispatch_once()
        assert restarted.turns(sid)[0]["status"] == "reconcile_required"

    asyncio.run(scenario())


def test_cron_skips_during_pause(tmp_path):
    async def scenario():
        app = Controller(tmp_path / "db.sqlite", FakeCodex())
        sid = await app.create_session("A", "Goal", str(tmp_path))
        schedule = app.set_cron(sid, "* * * * *", "UTC")
        await app.pause()
        await app.fire_due_cron("2026-11-01T00:00:00+00:00")
        assert not app.work_items(sid)
        assert any(event["type"] == "cron.skipped_while_paused" for event in app.events(sid))

    asyncio.run(scenario())


def test_reconciliation_reads_codex_turn_status(tmp_path):
    class ReconcilingCodex(FakeCodex):
        async def request(self, method, params):
            if method == "thread/turns/list":
                return {"data": [{"id": "turn", "status": "completed"}], "nextCursor": None}
            if method == "thread/items/list":
                return {"data": [{"turnId": "turn", "item": {"id": "answer", "type": "agentMessage", "text": "Recovered output"}}], "nextCursor": None}
            return await super().request(method, params)

    async def scenario():
        rpc = ReconcilingCodex()
        path = tmp_path / "db.sqlite"
        app = Controller(path, rpc)
        sid = await app.create_session("A", "Goal", str(tmp_path))
        await app.submit(sid, "work")
        await app.dispatch_once()
        restarted = Controller(path, rpc)
        restarted.recover_inflight()
        await restarted.reconcile_inflight()
        assert restarted.turns(sid)[0]["status"] == "completed"
        assert len(restarted.pending_reflections()) == 1
        assert restarted.turn_items(restarted.turns(sid)[0]["id"])[0]["payload"]["text"] == "Recovered output"

    asyncio.run(scenario())


def test_cron_records_run_timestamps(tmp_path):
    async def scenario():
        app = Controller(tmp_path / "db.sqlite", FakeCodex())
        sid = await app.create_session("A", "Goal", str(tmp_path))
        app.set_cron(sid, "* * * * *")
        await app.fire_due_cron("2026-11-01T00:00:00+00:00")
        await app.dispatch_once()
        turn = app.turns(sid)[0]
        await app.handle_notification("turn/completed", {"threadId": app.session(sid)["thread_id"], "turn": {"id": turn["codex_turn_id"], "status": "completed"}})
        schedule = app.db.one("SELECT * FROM cron_schedules WHERE session_id=?", (sid,))
        assert schedule["last_started"] and schedule["last_finished"]

    asyncio.run(scenario())


def test_uncertain_start_is_held_for_reconciliation(tmp_path):
    class LostAckCodex(FakeCodex):
        async def request(self, method, params):
            if method == "turn/start":
                raise ConnectionError("ack lost")
            return await super().request(method, params)

    async def scenario():
        app = Controller(tmp_path / "db.sqlite", LostAckCodex())
        sid = await app.create_session("A", "Goal", str(tmp_path))
        item_id = await app.submit(sid, "work")
        assert not await app.dispatch_once()
        turn = app.turns(sid)[0]
        assert turn["status"] == "reconcile_required"
        assert app.work_item(item_id)["status"] == "reconcile_required"
        assert app.session(sid)["held"]
        try:
            await app.retry_turn(turn["id"])
        except ValueError:
            pass
        else:
            assert False, "uncertain start must not retry"

    asyncio.run(scenario())


def test_cron_fast_forwards_missed_periods(tmp_path):
    async def scenario():
        app = Controller(tmp_path / "db.sqlite", FakeCodex())
        sid = await app.create_session("A", "Goal", str(tmp_path))
        schedule_id = app.set_cron(sid, "* * * * *")
        app.db.conn.execute("UPDATE cron_schedules SET next_due='2026-10-27T12:01:00+00:00' WHERE id=?", (schedule_id,))
        at = "2026-10-27T12:10:00+00:00"
        await app.fire_due_cron(at)
        schedule = app.db.one("SELECT * FROM cron_schedules WHERE id=?", (schedule_id,))
        assert schedule["next_due"] > at
        assert len(app.work_items(sid)) == 1
        await app.fire_due_cron(at)
        assert len(app.work_items(sid)) == 1

    asyncio.run(scenario())


def test_cron_records_no_work_outcome(tmp_path):
    async def scenario():
        app = Controller(tmp_path / "db.sqlite", FakeCodex())
        sid = await app.create_session("A", "Goal", str(tmp_path))
        schedule_id = app.set_cron(sid, "* * * * *")
        await app.fire_due_cron("2026-11-01T00:00:00+00:00")
        await app.dispatch_once()
        turn = app.turns(sid)[0]
        await app.handle_notification("item/completed", {"threadId": app.session(sid)["thread_id"], "turnId": turn["codex_turn_id"], "item": {"id": "answer", "type": "agentMessage", "text": "no_work"}})
        await app.handle_notification("turn/completed", {"threadId": app.session(sid)["thread_id"], "turn": {"id": turn["codex_turn_id"], "status": "completed"}})
        assert app.db.one("SELECT last_outcome FROM cron_schedules WHERE id=?", (schedule_id,))["last_outcome"] == "no_work"

    asyncio.run(scenario())


def test_restart_reconciles_completed_reflection(tmp_path):
    class ReflectionCodex(FakeCodex):
        async def request(self, method, params):
            if method == "thread/turns/list":
                return {"data": [{"id": "reflection-turn", "status": "completed"}], "nextCursor": None}
            if method == "thread/items/list":
                return {"data": [{"item": {"id": "reflection-answer", "type": "agentMessage",
                                           "text": json.dumps({"summary": "Recovered summary", "rename": {"required": False},
                                                               "split": {"recommended": False}})}}], "nextCursor": None}
            return await super().request(method, params)

    async def scenario():
        rpc = ReflectionCodex()
        path = tmp_path / "db.sqlite"
        app = Controller(path, rpc)
        sid = await app.create_session("A", "Goal", str(tmp_path))
        await app.submit(sid, "work")
        await app.dispatch_once()
        await app.handle_notification("turn/completed", {"threadId": "thread", "turn": {"id": "turn", "status": "completed"}})
        reflection = app.pending_reflections()[0]
        app.db.conn.execute("UPDATE reflections SET status='running',thread_id='reflection-thread',codex_turn_id='reflection-turn' WHERE id=?",
                            (reflection["id"],))

        restarted = Controller(path, rpc)
        restarted.recover_inflight()
        assert restarted.db.one("SELECT status FROM reflections WHERE id=?", (reflection["id"],))["status"] == "reconcile_required"
        await restarted.reconcile_inflight()
        assert restarted.db.one("SELECT status FROM reflections WHERE id=?", (reflection["id"],))["status"] == "completed"
        assert restarted.session(sid)["summary"] == "Recovered summary"
        assert len(restarted.reflection_items(reflection["id"])) == 1

    asyncio.run(scenario())
