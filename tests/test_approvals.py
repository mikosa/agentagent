import asyncio

from app.controller import Controller


class FakeCodex:
    def __init__(self):
        self.responses = []

    async def respond(self, rpc_id, result):
        self.responses.append((rpc_id, result))


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
