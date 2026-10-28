"""Local JSON command surface shared by the browser and MCP bridge."""

import json
from urllib.parse import parse_qs, urlsplit


class LocalAPI:
    def __init__(self, controller):
        self.controller = controller

    async def execute(self, method, path, body=None):
        body = body or {}
        c = self.controller
        parsed = urlsplit(path)
        parts = [part for part in parsed.path.split("/") if part]
        query = parse_qs(parsed.query)
        try:
            if parts == ["api", "controller", "status"] and method == "GET":
                return 200, c.status()
            if parts == ["api", "controller", "pause"] and method == "POST":
                await c.pause()
                return 200, c.status()
            if parts == ["api", "controller", "resume"] and method == "POST":
                await c.resume()
                return 200, c.status()
            if parts == ["api", "controller", "stop-all"] and method == "POST":
                errors = await c.stop_all()
                return 200, {**c.status(), "interrupt_errors": errors}
            if parts == ["api", "sessions"] and method == "GET":
                return 200, c.sessions()
            if parts == ["api", "sessions"] and method == "POST":
                sid = await c.create_session(body["title"], body["objective"], body["cwd"], body.get("scope"))
                return 201, {"id": sid}
            if len(parts) >= 3 and parts[:2] == ["api", "sessions"]:
                sid = parts[2]
                if len(parts) == 3 and method == "GET":
                    return (200, c.session(sid)) if c.session(sid) else (404, {"error": "session not found"})
                if len(parts) == 4:
                    action = parts[3]
                    if action == "prompts" and method == "POST":
                        wid = await c.submit(sid, body["prompt"])
                        return 202, {"id": wid}
                    if action == "hold" and method == "POST":
                        c.hold_session(sid)
                        return 200, c.session(sid)
                    if action == "archive" and method == "POST":
                        c.archive_session(sid)
                        return 200, c.session(sid)
                    if action == "resume" and method == "POST":
                        c.resume_session(sid)
                        return 200, c.session(sid)
                    if action == "turns" and method == "GET":
                        turns = c.turns(sid)
                        for turn in turns:
                            turn["items"] = c.turn_items(turn["id"])
                        return 200, turns
                    if action == "turns-page" and method == "GET":
                        return 200, c.turns_page(sid, query.get("limit", [50])[0], query.get("before", [None])[0])
                    if action == "reflections" and method == "GET":
                        rows = c.db.many("""SELECT r.* FROM reflections r JOIN turns t ON t.id=r.worker_turn_id
                            WHERE t.session_id=? ORDER BY r.rowid""", (sid,))
                        for row in rows:
                            row["items"] = c.reflection_items(row["id"])
                        return 200, rows
                    if action == "requests" and method == "GET":
                        return 200, c.db.many("SELECT * FROM agent_requests WHERE source_id=? OR target_id=? ORDER BY rowid", (sid, sid))
                    if action == "events" and method == "GET":
                        return 200, c.events(sid)
                    if action == "queue" and method == "GET":
                        return 200, c.work_items(sid)
                    if action == "neighbors" and method == "GET":
                        return 200, c.neighbors(sid)
                    if action == "inbox" and method == "GET":
                        return 200, c.passive_inbox(sid)
                    if action == "schedule" and method == "GET":
                        return 200, c.db.one("SELECT * FROM cron_schedules WHERE session_id=?", (sid,))
                    if action == "schedule" and method == "PUT":
                        schedule_id = c.set_cron(sid, body["expression"], body.get("timezone", "UTC"), body.get("enabled", True))
                        return 200, {"id": schedule_id}
                    if action == "fork" and method == "POST":
                        child_id = await c.fork_session(sid, body["title"], body["objective"], body.get("scope"))
                        return 201, {"id": child_id}
                    if action == "scope" and method == "PUT":
                        c.update_scope(sid, body["scope"])
                        return 200, c.session(sid)
            if parts == ["api", "graph"] and method == "GET":
                return 200, {"nodes": c.sessions(), "edges": c.graph()}
            if parts == ["api", "graph", "relationships"] and method == "POST":
                eid = c.add_edge(body["source_id"], body["target_id"], body["type"], body.get("communication_enabled", True))
                return 201, {"id": eid}
            if len(parts) == 4 and parts[:3] == ["api", "graph", "relationships"] and method == "DELETE":
                c.remove_edge(parts[3])
                return 200, {"ok": True}
            if len(parts) == 4 and parts[:2] == ["api", "turns"] and method == "POST":
                if parts[3] == "stop":
                    await c.stop_turn(parts[2])
                    return 202, {"ok": True}
                if parts[3] == "retry":
                    return 202, {"id": await c.retry_turn(parts[2])}
            if len(parts) == 4 and parts[:2] == ["api", "reflections"] and parts[3] == "stop" and method == "POST":
                await c.stop_reflection(parts[2])
                return 202, {"ok": True}
            if len(parts) == 4 and parts[:2] == ["api", "reflections"] and parts[3] == "retry" and method == "POST":
                c.retry_reflection(parts[2])
                return 202, {"ok": True}
            if len(parts) == 3 and parts[:2] == ["api", "work-items"] and method == "DELETE":
                c.cancel_work_item(parts[2])
                return 200, {"ok": True}
            if parts == ["api", "cron"] and method == "GET":
                return 200, c.db.many("SELECT * FROM cron_schedules ORDER BY rowid")
            if parts == ["api", "approvals"] and method == "GET":
                return 200, c.db.many("SELECT * FROM approvals WHERE decision IS NULL ORDER BY rowid")
            if len(parts) == 4 and parts[:2] == ["api", "approvals"] and parts[3] == "resolve" and method == "POST":
                await c.resolve_approval(parts[2], body["decision"])
                return 200, {"ok": True}
            if parts == ["api", "agent-requests"] and method == "POST":
                rid = await c.send_request(body["source_id"], body["target_id"], body["question"], body.get("request_key"))
                return 202, {"id": rid}
            if len(parts) == 3 and parts[:2] == ["api", "agent-requests"] and method == "GET":
                return 200, c.request_status(parts[2], query.get("requester_id", [None])[0])
            if len(parts) == 4 and parts[:2] == ["api", "agent-requests"] and parts[3] == "reply" and method == "POST":
                await c.reply(parts[2], body["answer"], body.get("responder_id"))
                return 200, {"ok": True}
        except (KeyError, ValueError, PermissionError, json.JSONDecodeError) as exc:
            return 400, {"error": str(exc)}
        return 404, {"error": "unknown endpoint"}
