import asyncio

from app.controller import Controller


class FakeCodex:
    def __init__(self):
        self.n = 0

    async def request(self, method, params):
        self.n += 1
        if method == "thread/start":
            return {"thread": {"id": f"thread-{self.n}"}}
        if method == "turn/start":
            return {"turn": {"id": f"turn-{self.n}"}}
        return {}


def test_overlapping_file_edits_emit_conflict_event(tmp_path):
    async def scenario():
        app = Controller(tmp_path / "db.sqlite", FakeCodex())
        a = await app.create_session("A", "A", str(tmp_path))
        b = await app.create_session("B", "B", str(tmp_path))
        await app.submit(a, "edit")
        await app.submit(b, "edit")
        await app.dispatch_once()
        await app.dispatch_once()
        ta, tb = app.turns(a)[0], app.turns(b)[0]
        diff = "diff --git a/app/api.py b/app/api.py\n--- a/app/api.py\n+++ b/app/api.py\n@@ -1 +1 @@\n"
        for sid, turn in ((a, ta), (b, tb)):
            await app.handle_notification("turn/diff/updated", {"threadId": app.session(sid)["thread_id"], "turnId": turn["codex_turn_id"], "diff": diff})
        conflicts = [e for e in app.events(b) if e["type"] == "workspace.conflict"]
        assert len(conflicts) == 1
        assert "app/api.py" in conflicts[0]["payload"]

    asyncio.run(scenario())
