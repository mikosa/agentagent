from app.mcp_server import MCPBridge
import pytest


def test_mcp_bridge_lists_tools_and_uses_bound_identity():
    calls = []

    def invoke(path, method="GET", body=None):
        calls.append((path, method, body))
        return {"id": "request-1"}

    bridge = MCPBridge("session-a", invoke)
    tools = bridge.handle("tools/list", {})["tools"]
    assert any(tool["name"] == "agent_send_query" for tool in tools)
    result = bridge.handle("tools/call", {"name": "agent_send_query", "arguments": {"target_id": "session-b", "question": "Ready?", "request_key": "question-1"}})
    assert calls == [("/api/agent-requests", "POST", {"source_id": "session-a", "target_id": "session-b", "question": "Ready?", "request_key": "question-1"})]
    assert "request-1" in result["content"][0]["text"]
    with pytest.raises(ValueError):
        bridge.handle("tools/call", {"name": "agent_send_query", "arguments": {"target_id": "session-b", "question": "Ready?"}})
