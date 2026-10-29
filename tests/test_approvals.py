import asyncio

from app.controller import Controller


class FakeCodex:
    def __init__(self):
        self.responses = []

    async def respond(self, rpc_id, result):
        self.responses.append((rpc_id, result))

    async def request(self, method, params):
        if method == "thread/start":
            return {"thread": {"id": "thread"}}
        if method == "turn/start":
            return {"turn": {"id": "turn"}}
        return {}


def test_permission_approval_returns_profile_or_empty_grant(tmp_path):
    async def scenario():
        rpc = FakeCodex()
        app = Controller(tmp_path / "db.sqlite", rpc)
        params = {"threadId": "thread", "permissions": {"network": {"enabled": True}}}
        accepted = await app.record_approval(8, "item/permissions/requestApproval", params)
        await app.resolve_approval(accepted, "accept")
        declined = await app.record_approval(9, "item/permissions/requestApproval", params)
        await app.resolve_approval(declined, "decline")
        assert rpc.responses[0] == (8, {"permissions": params["permissions"], "scope": "turn"})
        assert rpc.responses[1] == (9, {"permissions": {}, "scope": "turn"})

    asyncio.run(scenario())


def test_approval_wait_blocks_next_prompt_and_is_visible_in_status(tmp_path):
    async def scenario():
        rpc = FakeCodex()
        app = Controller(tmp_path / "db.sqlite", rpc)
        sid = await app.create_session("A", "Goal", str(tmp_path))
        await app.submit(sid, "first")
        await app.submit(sid, "second")
        await app.dispatch_once()
        approval_id = await app.record_approval(10, "item/fileChange/requestApproval",
                                                {"threadId": "thread", "turnId": "turn", "itemId": "change-1"})
        assert app.turns(sid)[0]["status"] == "waiting_for_approval"
        assert app.session(sid)["status"] == "waiting_for_approval"
        assert app.status()["pending_approvals"] == 1
        assert not await app.dispatch_once()
        await app.resolve_approval(approval_id, "decline")
        assert rpc.responses[-1] == (10, {"decision": "decline"})
        assert app.turns(sid)[0]["status"] == "running"
        assert app.session(sid)["status"] == "running"
        assert app.status()["pending_approvals"] == 0
        assert not await app.dispatch_once()

    asyncio.run(scenario())


def test_restart_marks_unanswerable_approval_stale(tmp_path):
    async def scenario():
        rpc = FakeCodex()
        path = tmp_path / "db.sqlite"
        app = Controller(path, rpc)
        sid = await app.create_session("A", "Goal", str(tmp_path))
        await app.submit(sid, "work")
        await app.dispatch_once()
        approval_id = await app.record_approval(10, "item/fileChange/requestApproval",
                                                {"threadId": "thread", "turnId": "turn", "itemId": "change-1"})
        restarted = Controller(path, rpc)
        restarted.recover_inflight()
        approval = restarted.db.one("SELECT decision FROM approvals WHERE id=?", (approval_id,))
        assert approval["decision"] == "stale_after_restart"
        assert restarted.status()["pending_approvals"] == 0
        assert restarted.status()["stale_approvals"] == 1
        assert restarted.turns(sid)[0]["status"] == "reconcile_required"

    asyncio.run(scenario())
