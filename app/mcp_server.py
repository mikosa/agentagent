"""Stdio MCP bridge bound to one controller-owned session identity."""

import json
import os
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def _tool(name, description, properties=None, required=None):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties or {}, "required": required or []}}


STRING = {"type": "string"}
TOOLS = [
    _tool("agent_list", "List controller-owned sessions"),
    _tool("agent_get_status", "Get this session's controller status"),
    _tool("agent_get_graph", "Inspect session relationships"),
    _tool("agent_send_request", "Queue work for an authorized neighbor; reuse request_key on retries", {"target_id": STRING, "question": STRING, "request_key": STRING}, ["target_id", "question", "request_key"]),
    _tool("agent_send_query", "Ask an authorized neighbor; reuse request_key on retries", {"target_id": STRING, "question": STRING, "request_key": STRING}, ["target_id", "question", "request_key"]),
    _tool("agent_reply", "Reply to an agent request", {"request_id": STRING, "answer": STRING}, ["request_id", "answer"]),
    _tool("agent_create_session", "Create a scoped controller-owned session", {"title": STRING, "objective": STRING, "cwd": STRING}, ["title", "objective", "cwd"]),
    _tool("agent_fork_session", "Fork this session into a new session", {"title": STRING, "objective": STRING}, ["title", "objective"]),
    _tool("agent_update_scope", "Update this session's scope", {"scope": {"type": "object"}}, ["scope"]),
    _tool("agent_set_relationship", "Create a session relationship", {"target_id": STRING, "type": STRING}, ["target_id", "type"]),
    _tool("agent_remove_relationship", "Remove a relationship", {"edge_id": STRING}, ["edge_id"]),
    _tool("agent_set_cron", "Set this session's schedule", {"expression": STRING, "timezone": STRING}, ["expression"]),
    _tool("agent_get_request_status", "Inspect an outgoing request", {"request_id": STRING}, ["request_id"]),
]


class MCPBridge:
    def __init__(self, session_id, invoke):
        self.session_id = session_id
        self.invoke = invoke

    def handle(self, method, params):
        if method == "initialize":
            return {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                    "serverInfo": {"name": "agent-controller", "version": "0.1.0"}}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method != "tools/call":
            raise ValueError("unknown method")
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in {tool["name"] for tool in TOOLS}:
            raise ValueError("unknown tool")
        required = next(tool["inputSchema"]["required"] for tool in TOOLS if tool["name"] == name)
        for field in required:
            if field not in args:
                raise ValueError("missing " + field)
        sid = self.session_id
        if name == "agent_list":
            result = self.invoke("/api/sessions")
        elif name == "agent_get_status":
            result = self.invoke(f"/api/sessions/{sid}")
        elif name == "agent_get_graph":
            result = self.invoke("/api/graph")
        elif name in ("agent_send_request", "agent_send_query"):
            payload = {"source_id": sid, "target_id": args["target_id"], "question": args["question"], "request_key": args["request_key"]}
            result = self.invoke("/api/agent-requests", "POST", payload)
        elif name == "agent_reply":
            result = self.invoke(f"/api/agent-requests/{args['request_id']}/reply", "POST", {"answer": args["answer"], "responder_id": sid})
        elif name == "agent_create_session":
            result = self.invoke("/api/sessions", "POST", {"title": args["title"], "objective": args["objective"], "cwd": args["cwd"]})
        elif name == "agent_fork_session":
            result = self.invoke(f"/api/sessions/{sid}/fork", "POST", {"title": args["title"], "objective": args["objective"]})
        elif name == "agent_update_scope":
            result = self.invoke(f"/api/sessions/{sid}/scope", "PUT", {"scope": args["scope"]})
        elif name == "agent_set_relationship":
            result = self.invoke("/api/graph/relationships", "POST", {"source_id": sid, "target_id": args["target_id"], "type": args["type"]})
        elif name == "agent_remove_relationship":
            result = self.invoke(f"/api/graph/relationships/{args['edge_id']}", "DELETE")
        elif name == "agent_set_cron":
            result = self.invoke(f"/api/sessions/{sid}/schedule", "PUT", {"expression": args["expression"], "timezone": args.get("timezone", "UTC")})
        else:
            result = self.invoke(f"/api/agent-requests/{args['request_id']}?requester_id={sid}")
        return {"content": [{"type": "text", "text": json.dumps(result)}]}


def main():
    base = os.environ["AGENT_CONTROLLER_URL"]
    session_id = os.environ["AGENT_CONTROLLER_SESSION_ID"]

    def invoke(path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = Request(base + path, method=method, data=data, headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            raise ValueError(exc.read().decode()) from exc

    bridge = MCPBridge(session_id, invoke)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if "id" not in request:
                continue
            result = bridge.handle(request.get("method"), request.get("params") or {})
            response = {"jsonrpc": "2.0", "id": request["id"], "result": result}
        except Exception as exc:
            response = {"jsonrpc": "2.0", "id": request.get("id"),
                        "error": {"code": -32000, "message": str(exc)}}
        print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
