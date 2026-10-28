import asyncio

from app.api import LocalAPI
from app.controller import Controller


class FakeCodex:
    async def request(self, method, params):
        if method == "thread/start":
            return {"thread": {"id": "thread"}}
        return {}


def test_api_creates_session_and_queues_prompt(tmp_path):
    async def scenario():
        app = Controller(tmp_path / "db.sqlite", FakeCodex())
        api = LocalAPI(app)
        status, session = await api.execute("POST", "/api/sessions", {"title": "A", "objective": "Goal", "cwd": str(tmp_path)})
        assert status == 201
        sid = session["id"]
        status, work = await api.execute("POST", f"/api/sessions/{sid}/prompts", {"prompt": "hello"})
        assert status == 202
        assert app.work_item(work["id"])["payload"] == "hello"
        status, result = await api.execute("GET", "/api/graph", {})
        assert status == 200
        assert result["nodes"][0]["id"] == sid
        status, reflections = await api.execute("GET", f"/api/sessions/{sid}/reflections", {})
        assert status == 200 and reflections == []
        status, requests = await api.execute("GET", f"/api/sessions/{sid}/requests", {})
        assert status == 200 and requests == []

    asyncio.run(scenario())


def test_turn_history_pages_newest_first(tmp_path):
    async def scenario():
        app = Controller(tmp_path / "db.sqlite", FakeCodex())
        sid = await app.create_session("A", "Goal", str(tmp_path))
        for n in range(3):
            app.db.conn.execute("INSERT INTO turns (id,session_id,work_item_id,status) VALUES (?,?,?,?)", (f"turn-{n}", sid, f"work-{n}", "completed"))
        api = LocalAPI(app)
        status, first = await api.execute("GET", f"/api/sessions/{sid}/turns-page?limit=2", {})
        assert status == 200 and [t["id"] for t in first["data"]] == ["turn-2", "turn-1"]
        status, second = await api.execute("GET", f"/api/sessions/{sid}/turns-page?limit=2&before={first['next_cursor']}", {})
        assert status == 200 and [t["id"] for t in second["data"]] == ["turn-0"]
        assert second["next_cursor"] is None

    asyncio.run(scenario())
