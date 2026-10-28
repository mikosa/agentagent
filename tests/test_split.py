import asyncio
import json
import sqlite3

from app.controller import Controller
from app.db import Database


class FakeCodex:
    def __init__(self):
        self.n = 0

    async def request(self, method, params):
        if method == "thread/start":
            self.n += 1
            return {"thread": {"id": f"thread-{self.n}"}}
        if method == "thread/fork":
            return {"thread": {"id": "reflection-thread"}}
        if method == "turn/start":
            return {"turn": {"id": "reflection-turn" if params["threadId"] == "reflection-thread" else "worker-turn"}}
        return {}


def test_high_context_split_creates_scoped_successors(tmp_path):
    async def scenario():
        app = Controller(tmp_path / "db.sqlite", FakeCodex())
        sid = await app.create_session("Original", "Broad goal", str(tmp_path))
        app.db.conn.execute("UPDATE sessions SET context_tokens=210000 WHERE id=?", (sid,))
        await app.submit(sid, "work")
        await app.dispatch_once()
        await app.handle_notification("turn/completed", {"threadId": "thread-1", "turn": {"id": "worker-turn", "status": "completed"}})
        await app.dispatch_reflection_once()
        result = {"rename": {"required": False}, "summary": "Two streams remain", "split": {"recommended": True, "relationship": "parent-child", "original_role": "coordinator", "branches": [{"title": "API", "objective": "Implement API"}, {"title": "UI", "objective": "Implement UI"}]}}
        await app.handle_notification("item/completed", {"threadId": "reflection-thread", "turnId": "reflection-turn", "item": {"type": "agentMessage", "text": json.dumps(result)}})
        await app.handle_notification("turn/completed", {"threadId": "reflection-thread", "turn": {"id": "reflection-turn", "status": "completed"}})
        assert len(app.sessions()) == 3
        assert len([e for e in app.graph() if e["type"] == "PARENT_OF"]) == 2
        for child in app.sessions()[1:]:
            assert child["scope"] != "{}"
            assert app.work_items(child["id"])[0]["source"] == "agent"
            assert "Two streams remain" in app.work_items(child["id"])[0]["payload"]
            assert child["objective"] in app.work_items(child["id"])[0]["payload"]
            assert "thread-1" in app.work_items(child["id"])[0]["payload"]

    asyncio.run(scenario())


def test_split_retry_reuses_created_successors(tmp_path):
    async def scenario():
        app = Controller(tmp_path / "db.sqlite", FakeCodex())
        sid = await app.create_session("Original", "Broad goal", str(tmp_path))
        original = app.session(sid)
        plan = {"relationship": "parent-child", "original_role": "coordinator", "branches": [
            {"title": "API", "objective": "Implement API"},
            {"title": "UI", "objective": "Implement UI"},
        ]}
        submit = app.submit
        calls = 0

        async def fail_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("queue temporarily unavailable")
            return await submit(*args, **kwargs)

        app.submit = fail_once
        try:
            await app._commit_split(original, plan)
        except RuntimeError:
            pass
        else:
            assert False, "expected the simulated failure"
        assert len(app.sessions()) == 2

        restarted = Controller(tmp_path / "db.sqlite", app.codex)
        await restarted.resume_pending_splits()
        assert len(restarted.sessions()) == 3
        successors = [s for s in restarted.sessions() if s["id"] != sid]
        assert len({s["id"] for s in successors}) == 2
        assert all(len(restarted.work_items(s["id"])) == 1 for s in successors)
        assert len([e for e in restarted.events(sid) if e["type"] == "split.committed"]) == 1

    asyncio.run(scenario())


def test_uncertain_split_thread_start_is_held_for_reconciliation(tmp_path):
    class FailingCodex(FakeCodex):
        async def request(self, method, params):
            if method == "thread/start" and self.n == 1:
                raise RuntimeError("lost thread start response")
            return await super().request(method, params)

    async def scenario():
        codex = FailingCodex()
        app = Controller(tmp_path / "db.sqlite", codex)
        sid = await app.create_session("Original", "Broad goal", str(tmp_path))
        plan = {"branches": [{"title": "A", "objective": "A"}, {"title": "B", "objective": "B"}]}
        try:
            await app._commit_split(app.session(sid), plan)
        except RuntimeError:
            pass
        else:
            assert False, "expected lost response"
        restarted = Controller(tmp_path / "db.sqlite", codex)
        await restarted.resume_pending_splits()
        assert codex.n == 1
        assert len(restarted.sessions()) == 1
        assert any(e["type"] == "split.recovery_failed" for e in restarted.events(sid))

    asyncio.run(scenario())


def test_later_reflection_can_split_same_session_again(tmp_path):
    async def scenario():
        app = Controller(tmp_path / "db.sqlite", FakeCodex())
        sid = await app.create_session("Original", "Broad goal", str(tmp_path))
        first = {"relationship": "parent-child", "branches": [
            {"title": "A", "objective": "Build A"}, {"title": "B", "objective": "Build B"}]}
        second = {"relationship": "parent-child", "branches": [
            {"title": "C", "objective": "Build C"}, {"title": "D", "objective": "Build D"}]}
        await app._commit_split(app.session(sid), first, source_key="reflection-1")
        await app._commit_split(app.session(sid), second, source_key="reflection-2")
        await app._commit_split(app.session(sid), first, source_key="reflection-1")
        assert len(app.sessions()) == 5
        assert len([e for e in app.events(sid) if e["type"] == "split.committed"]) == 2

    asyncio.run(scenario())


def test_existing_split_plan_schema_migrates_without_losing_plan(tmp_path):
    path = tmp_path / "db.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE split_plans (id TEXT PRIMARY KEY, original_id TEXT NOT NULL UNIQUE, plan TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL)")
    conn.execute("INSERT INTO split_plans VALUES ('plan-1','session-1','{}','committed','2026-01-01')")
    conn.commit()
    conn.close()
    db = Database(path)
    assert db.one("SELECT plan FROM split_plans WHERE id='plan-1'")["plan"] == "{}"
    db.conn.execute("INSERT INTO split_plans (id,original_id,source_key,plan,status,created_at) VALUES (?,?,?,?,?,?)",
                    ("plan-2", "session-1", "reflection-2", "{}", "pending", "2026-01-02"))


def test_split_can_archive_original_after_successors_are_ready(tmp_path):
    async def scenario():
        app = Controller(tmp_path / "db.sqlite", FakeCodex())
        sid = await app.create_session("Original", "Broad goal", str(tmp_path))
        plan = {"original_role": "archived", "relationship": "sibling", "branches": [
            {"title": "A", "objective": "Build A"}, {"title": "B", "objective": "Build B"}]}
        await app._commit_split(app.session(sid), plan, source_key="reflection-1")
        assert app.session(sid)["status"] == "archived"
        successors = [s["id"] for s in app.sessions() if s["id"] != sid]
        assert any(e["type"] == "SIBLING_OF" and {e["source_id"], e["target_id"]} == set(successors)
                   for e in app.graph())
        assert len([e for e in app.events(sid) if e["type"] == "session.archived"]) == 1
        await app._commit_split(app.session(sid), plan, source_key="reflection-1")
        assert len([e for e in app.events(sid) if e["type"] == "session.archived"]) == 1

    asyncio.run(scenario())


def test_split_can_repurpose_original_as_specialist(tmp_path):
    async def scenario():
        app = Controller(tmp_path / "db.sqlite", FakeCodex())
        sid = await app.create_session("Original", "Broad goal", str(tmp_path))
        plan = {"original_role": "specialist", "original_objective": "Own the database layer",
                "original_scope": {"in_scope": ["Database layer"], "out_of_scope": ["UI"]},
                "branches": [{"title": "API", "objective": "Build API"},
                             {"title": "UI", "objective": "Build UI"}]}
        await app._commit_split(app.session(sid), plan, source_key="reflection-1")
        original = app.session(sid)
        assert original["objective"] == "Own the database layer"
        assert json.loads(original["scope"])["out_of_scope"] == ["UI"]
        assert original["status"] == "ready"

    asyncio.run(scenario())


def test_archive_split_rejects_queued_manual_work_before_creating_successors(tmp_path):
    async def scenario():
        app = Controller(tmp_path / "db.sqlite", FakeCodex())
        sid = await app.create_session("Original", "Broad goal", str(tmp_path))
        queued_id = await app.submit(sid, "Keep this user request")
        plan = {"original_role": "archived", "branches": [
            {"title": "A", "objective": "Build A"}, {"title": "B", "objective": "Build B"}]}
        try:
            await app._commit_split(app.session(sid), plan, source_key="reflection-1")
        except ValueError:
            pass
        else:
            assert False, "archiving must preserve queued manual work"
        assert len(app.sessions()) == 1
        assert app.work_item(queued_id)["status"] == "queued"

    asyncio.run(scenario())
