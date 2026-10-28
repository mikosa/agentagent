import asyncio

from app.controller import Controller


class CaptureCodex:
    def __init__(self):
        self.params = None
        self.fork_params = None

    async def request(self, method, params):
        if method == "thread/start":
            self.params = params
            return {"thread": {"id": "thread"}}
        if method == "thread/fork":
            self.fork_params = params
            return {"thread": {"id": "forked-thread"}}
        return {}


def test_new_session_configures_local_mcp_identity(tmp_path):
    async def scenario():
        rpc = CaptureCodex()
        app = Controller(tmp_path / "db.sqlite", rpc, mcp_url="http://127.0.0.1:8765")
        sid = await app.create_session("A", "Goal", str(tmp_path))
        config = rpc.params["config"]["mcp_servers"]["agent_controller"]
        assert config["env"]["AGENT_CONTROLLER_SESSION_ID"] == sid
        assert config["env"]["AGENT_CONTROLLER_URL"] == "http://127.0.0.1:8765"

    asyncio.run(scenario())


def test_fork_overrides_parent_mcp_identity(tmp_path):
    async def scenario():
        rpc = CaptureCodex()
        app = Controller(tmp_path / "db.sqlite", rpc, mcp_url="http://127.0.0.1:8765")
        parent = await app.create_session("A", "Goal", str(tmp_path))
        child = await app.fork_session(parent, "Child", "Child goal")
        env = rpc.fork_params["config"]["mcp_servers"]["agent_controller"]["env"]
        assert env["AGENT_CONTROLLER_SESSION_ID"] == child
        assert env["AGENT_CONTROLLER_SESSION_ID"] != parent

    asyncio.run(scenario())
