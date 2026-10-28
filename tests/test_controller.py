import asyncio

from app.controller import Controller


class FakeCodex:
    def __init__(self):
        self.calls = []
        self.block_start = None

    async def request(self, method, params):
        self.calls.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": f"thread-{len(self.calls)}"}}
        if method == "turn/start":
            if self.block_start:
                await self.block_start.wait()
            return {"turn": {"id": f"turn-{len(self.calls)}"}}
        return {}


def run(coro):
    return asyncio.run(coro)


def test_pause_is_durable_and_serialized_with_start(tmp_path):
    async def scenario():
        rpc = FakeCodex()
        app = Controller(tmp_path / "db.sqlite", rpc)
        session = await app.create_session("A", "Implement A", str(tmp_path))
        await app.submit(session, "first")
        rpc.block_start = asyncio.Event()
        starting = asyncio.create_task(app.dispatch_once())
        await asyncio.sleep(0)
        pausing = asyncio.create_task(app.pause())
        await asyncio.sleep(0)
        assert not pausing.done()
        rpc.block_start.set()
        await starting
        await pausing
        await app.submit(session, "second")
        assert not await app.dispatch_once()
        assert len([c for c in rpc.calls if c[0] == "turn/start"]) == 1
        restarted = Controller(tmp_path / "db.sqlite", rpc)
        assert restarted.status()["paused"]

    run(scenario())


def test_stop_holds_only_target_and_keeps_partial_history(tmp_path):
    async def scenario():
        rpc = FakeCodex()
        app = Controller(tmp_path / "db.sqlite", rpc)
        a = await app.create_session("A", "A work", str(tmp_path))
        b = await app.create_session("B", "B work", str(tmp_path))
        await app.submit(a, "do A")
        await app.submit(b, "do B")
        await app.dispatch_once()
        await app.dispatch_once()
        turn_a = app.turns(a)[0]
        await app.stop_turn(turn_a["id"])
        await app.stop_turn(turn_a["id"])
        assert len([call for call in rpc.calls if call[0] == "turn/interrupt"]) == 1
        assert app.session(a)["held"] == 1
        assert app.session(b)["held"] == 0
        assert rpc.calls[-1] == ("turn/interrupt", {"threadId": app.session(a)["thread_id"], "turnId": turn_a["codex_turn_id"]})
        await app.handle_notification("item/completed", {"threadId": app.session(a)["thread_id"], "turnId": turn_a["codex_turn_id"], "item": {"id": "i1", "type": "agentMessage", "text": "partial"}})
        await app.handle_notification("turn/completed", {"threadId": app.session(a)["thread_id"], "turn": {"id": turn_a["codex_turn_id"], "status": "interrupted"}})
        assert app.turns(a)[0]["status"] == "interrupted"
        assert app.turn_items(turn_a["id"])[0]["payload"]["text"] == "partial"
        assert app.session(b)["status"] == "running"

    run(scenario())


def test_user_priority_then_agent_then_cron(tmp_path):
    async def scenario():
        rpc = FakeCodex()
        app = Controller(tmp_path / "db.sqlite", rpc)
        s = await app.create_session("A", "Work", str(tmp_path))
        for source in ("cron", "agent", "manual", "manual"):
            await app.submit(s, source, source)
        for expected in ("manual", "manual", "agent", "cron"):
            assert await app.dispatch_once()
            turn = app.turns(s)[-1]
            assert app.work_item(turn["work_item_id"])["source"] == expected
            await app.handle_notification("turn/completed", {"threadId": app.session(s)["thread_id"], "turn": {"id": turn["codex_turn_id"], "status": "completed"}})

    run(scenario())


def test_neighbors_are_passive_and_manager_contact_limited(tmp_path):
    async def scenario():
        app = Controller(tmp_path / "db.sqlite", FakeCodex())
        ids = [await app.create_session(x, x, str(tmp_path)) for x in "RACD"]
        r, a, c, d = ids
        app.add_edge(r, a, "PARENT_OF")
        app.add_edge(a, c, "PARENT_OF")
        app.publish_summary(a, "New A", "A summary")
        assert not app.work_items(c)
        assert app.can_contact(c, r)
        assert not app.can_contact(c, d)
        await app.submit(c, "next")
        assert "New A" in app.compose_prompt(app.work_items(c)[0])
        assert "A summary" in app.compose_prompt(app.work_items(c)[0])

    run(scenario())


def test_completion_arriving_before_start_reply_is_replayed(tmp_path):
    class EarlyCodex(FakeCodex):
        app = None

        async def request(self, method, params):
            if method == "turn/start":
                await self.app.handle_notification("turn/completed", {"threadId": self.app.sessions()[0]["thread_id"], "turn": {"id": "early-turn", "status": "completed"}})
                return {"turn": {"id": "early-turn"}}
            return await super().request(method, params)

    async def scenario():
        rpc = EarlyCodex()
        app = Controller(tmp_path / "db.sqlite", rpc)
        rpc.app = app
        sid = await app.create_session("A", "Goal", str(tmp_path))
        await app.submit(sid, "work")
        await app.dispatch_once()
        assert app.turns(sid)[0]["status"] == "completed"

    run(scenario())


def test_agent_reply_requires_target_identity(tmp_path):
    async def scenario():
        app = Controller(tmp_path / "db.sqlite", FakeCodex())
        source = await app.create_session("Source", "S", str(tmp_path))
        target = await app.create_session("Target", "T", str(tmp_path))
        outsider = await app.create_session("Outsider", "O", str(tmp_path))
        app.add_edge(source, target, "RELATED_TO")
        request_id = await app.send_request(source, target, "Question")
        try:
            await app.reply(request_id, "Forged", responder_id=outsider)
        except PermissionError:
            pass
        else:
            assert False, "outsider reply should be rejected"
        await app.reply(request_id, "Answer", responder_id=target)
        assert app.db.one("SELECT reply FROM agent_requests WHERE id=?", (request_id,))["reply"] == "Answer"

    run(scenario())


def test_archiving_session_records_cancelled_user_work(tmp_path):
    async def scenario():
        app = Controller(tmp_path / "db.sqlite", FakeCodex())
        sid = await app.create_session("A", "Goal", str(tmp_path))
        work_id = await app.submit(sid, "queued prompt")
        app.archive_session(sid)
        assert app.session(sid)["status"] == "archived"
        assert app.work_item(work_id)["status"] == "cancelled"
        assert any(event["type"] == "work.cancelled" for event in app.events(sid))

    run(scenario())


def test_stop_all_continues_after_one_interrupt_error(tmp_path):
    class FailingCodex(FakeCodex):
        interrupts = 0

        async def request(self, method, params):
            if method == "turn/interrupt":
                self.interrupts += 1
                if self.interrupts == 1:
                    raise RuntimeError("unavailable")
            return await super().request(method, params)

    async def scenario():
        rpc = FailingCodex()
        app = Controller(tmp_path / "db.sqlite", rpc)
        for title in ("A", "B"):
            sid = await app.create_session(title, title, str(tmp_path))
            await app.submit(sid, "work")
        await app.dispatch_once()
        await app.dispatch_once()
        errors = await app.stop_all()
        assert rpc.interrupts == 2
        assert len(errors) == 1
        assert app.status()["paused"]

    run(scenario())


def test_archive_blocks_future_dispatch(tmp_path):
    async def scenario():
        app = Controller(tmp_path / "db.sqlite", FakeCodex())
        sid = await app.create_session("A", "Goal", str(tmp_path))
        await app.submit(sid, "queued")
        app.archive_session(sid)
        assert app.session(sid)["status"] == "archived"
        assert not await app.dispatch_once()

    run(scenario())


def test_resume_waits_for_interrupt_confirmation(tmp_path):
    async def scenario():
        app = Controller(tmp_path / "db.sqlite", FakeCodex())
        sid = await app.create_session("A", "Goal", str(tmp_path))
        await app.submit(sid, "first")
        await app.submit(sid, "second")
        await app.dispatch_once()
        first = app.turns(sid)[0]
        await app.stop_turn(first["id"])
        try:
            app.resume_session(sid)
        except ValueError:
            pass
        else:
            assert False, "resume must wait for interruption confirmation"
        assert not await app.dispatch_once()
        await app.handle_notification("turn/completed", {"threadId": app.session(sid)["thread_id"], "turn": {"id": first["codex_turn_id"], "status": "interrupted"}})
        app.resume_session(sid)
        assert await app.dispatch_once()

    run(scenario())


def test_neighbor_context_includes_only_undelivered_revisions(tmp_path):
    async def scenario():
        app = Controller(tmp_path / "db.sqlite", FakeCodex())
        a = await app.create_session("A", "A", str(tmp_path))
        b = await app.create_session("B", "B", str(tmp_path))
        app.add_edge(a, b, "RELATED_TO")
        app.publish_summary(a, "A one", "First change")
        app.publish_summary(a, "A two", "Second change")
        await app.submit(b, "work")
        prompt = app.compose_prompt(app.work_items(b)[0])
        assert "A two" in prompt and "First change" in prompt and "Second change" in prompt
        await app.dispatch_once()
        turn = app.turns(b)[0]
        await app.handle_notification("turn/completed", {"threadId": app.session(b)["thread_id"], "turn": {"id": turn["codex_turn_id"], "status": "completed"}})
        app.publish_summary(a, "A three", "Third change")
        await app.submit(b, "next")
        prompt = app.compose_prompt(app.work_items(b)[-1])
        assert "Third change" in prompt
        assert "First change" not in prompt and "Second change" not in prompt

    run(scenario())


def test_neighbor_update_during_start_is_not_marked_delivered(tmp_path):
    class UpdatingCodex(FakeCodex):
        callback = None

        async def request(self, method, params):
            if method == "turn/start" and self.callback:
                self.callback()
                self.callback = None
            return await super().request(method, params)

    async def scenario():
        rpc = UpdatingCodex()
        app = Controller(tmp_path / "db.sqlite", rpc)
        a = await app.create_session("A", "A", str(tmp_path))
        b = await app.create_session("B", "B", str(tmp_path))
        app.add_edge(a, b, "RELATED_TO")
        app.publish_summary(a, "A one", "First")
        await app.submit(b, "work")
        rpc.callback = lambda: app.publish_summary(a, "A two", "Second")
        await app.dispatch_once()
        turn = app.turns(b)[0]
        delivered = app.db.one("SELECT revision FROM context_delivery WHERE turn_id=? AND neighbor_id=?", (turn["id"], a))
        assert delivered["revision"] == 1
        await app.submit(b, "next")
        assert "Second" in app.compose_prompt(app.work_items(b)[-1])

    run(scenario())


def test_agent_request_replay_uses_idempotency_key(tmp_path):
    async def scenario():
        app = Controller(tmp_path / "db.sqlite", FakeCodex())
        a = await app.create_session("A", "A", str(tmp_path))
        b = await app.create_session("B", "B", str(tmp_path))
        app.add_edge(a, b, "RELATED_TO")
        first = await app.send_request(a, b, "Question", idempotency_key="request-7")
        second = await app.send_request(a, b, "Question", idempotency_key="request-7")
        assert first == second
        assert len([w for w in app.work_items(b) if w["source"] == "agent"]) == 1

    run(scenario())


def test_manager_contact_is_allowed_without_edge_communication_flag(tmp_path):
    async def scenario():
        app = Controller(tmp_path / "db.sqlite", FakeCodex())
        parent = await app.create_session("Parent", "P", str(tmp_path))
        child = await app.create_session("Child", "C", str(tmp_path))
        app.add_edge(parent, child, "PARENT_OF", communication_enabled=False)
        assert app.can_contact(child, parent)

    run(scenario())


def test_request_status_is_limited_to_participants(tmp_path):
    async def scenario():
        app = Controller(tmp_path / "db.sqlite", FakeCodex())
        a = await app.create_session("A", "A", str(tmp_path))
        b = await app.create_session("B", "B", str(tmp_path))
        outsider = await app.create_session("C", "C", str(tmp_path))
        app.add_edge(a, b, "RELATED_TO")
        request_id = await app.send_request(a, b, "Question")
        assert app.request_status(request_id, a)["status"] == "queued"
        try:
            app.request_status(request_id, outsider)
        except PermissionError:
            pass
        else:
            assert False, "outsider should not inspect the request"

    run(scenario())


def test_passive_inbox_tracks_undelivered_neighbor_updates(tmp_path):
    async def scenario():
        app = Controller(tmp_path / "db.sqlite", FakeCodex())
        a = await app.create_session("A", "A", str(tmp_path))
        b = await app.create_session("B", "B", str(tmp_path))
        app.add_edge(a, b, "RELATED_TO")
        app.publish_summary(a, "A updated", "New status")
        assert len(app.passive_inbox(b)) == 1
        assert app.status()["unread_updates"] == 1
        assert not app.work_items(b)
        await app.submit(b, "work")
        await app.dispatch_once()
        assert app.passive_inbox(b) == []
        assert app.status()["unread_updates"] == 0

    run(scenario())
