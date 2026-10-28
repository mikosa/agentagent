"""Dispatch, graph, stop, and history logic. All new prompts pass one gate."""

import asyncio
import json
import re
import sys
from pathlib import Path

from .db import Database, now, uid
from .cron import next_due


PRIORITY = {"manual": 0, "agent": 1, "cron": 2}
EDGE_TYPES = {"PARENT_OF", "SIBLING_OF", "DEPENDS_ON", "RELATED_TO", "FORKED_FROM"}


class Controller:
    def __init__(self, database_path, codex, mcp_url=None):
        self.db = Database(database_path)
        self.codex = codex
        self.mcp_url = mcp_url
        self.dispatch_gate = asyncio.Lock()
        self.early_notifications = {}

    def _mcp_config(self, session_id):
        return {"mcp_servers": {"agent_controller": {
            "command": sys.executable,
            "args": [str(Path(__file__).with_name("mcp_server.py"))],
            "env": {"AGENT_CONTROLLER_URL": self.mcp_url,
                    "AGENT_CONTROLLER_SESSION_ID": session_id},
        }}}

    def status(self):
        state = self.db.one("SELECT * FROM controller_state WHERE id=1")
        counts = {row["status"]: row["count"] for row in self.db.many(
            "SELECT status, count(*) AS count FROM turns GROUP BY status")}
        unread = self.db.one("""WITH links AS (
                SELECT source_id AS target_id,target_id AS neighbor_id FROM session_edges
                    WHERE active=1 AND communication_enabled=1 AND type!='FORKED_FROM'
                UNION
                SELECT target_id AS target_id,source_id AS neighbor_id FROM session_edges
                    WHERE active=1 AND communication_enabled=1 AND type!='FORKED_FROM'
            ), delivered AS (
                SELECT t.session_id AS target_id,d.neighbor_id,max(d.revision) AS revision
                FROM context_delivery d JOIN turns t ON t.id=d.turn_id
                WHERE d.acknowledged=1 GROUP BY t.session_id,d.neighbor_id
            ) SELECT count(*) AS n FROM links l
                JOIN sessions target ON target.id=l.target_id
                JOIN sessions neighbor ON neighbor.id=l.neighbor_id
                LEFT JOIN delivered d ON d.target_id=l.target_id AND d.neighbor_id=l.neighbor_id
                WHERE target.status!='archived' AND neighbor.status!='archived'
                    AND neighbor.summary_revision>coalesce(d.revision,0)""")["n"]
        return {"paused": bool(state["paused"]), "revision": state["revision"],
                "running": counts.get("running", 0) + counts.get("starting", 0) + counts.get("interrupt_requested", 0),
                "queued": self.db.one("SELECT count(*) AS n FROM work_items WHERE status='queued'")["n"],
                "reflecting": self.db.one("SELECT count(*) AS n FROM reflections WHERE status IN ('starting','running','interrupt_requested')")["n"],
                "held": self.db.one("SELECT count(*) AS n FROM sessions WHERE held=1")["n"],
                "pending_requests": self.db.one("SELECT count(*) AS n FROM agent_requests WHERE status='queued'")["n"],
                "unread_updates": unread,
                "failures": counts.get("failed", 0) + counts.get("interrupt_failed", 0)}

    @staticmethod
    def should_evaluate_split(session):
        window = session.get("context_window")
        threshold = min(200_000, int(window * 0.8)) if window else 200_000
        return session["context_tokens"] >= threshold

    def recover_inflight(self):
        """Fail closed after process loss; operator must inspect before resuming."""
        active = self.db.many("SELECT * FROM turns WHERE status IN ('starting','running','interrupt_requested')")
        reflections = self.db.many("SELECT * FROM reflections WHERE status IN ('starting','running','interrupt_requested')")
        with self.db.transaction():
            for turn in active:
                self.db.conn.execute("UPDATE turns SET status='reconcile_required' WHERE id=?", (turn["id"],))
                self.db.conn.execute("UPDATE sessions SET status='held',held=1 WHERE id=?", (turn["session_id"],))
                self.db.event("codex.reconcile_required", "controller", turn["session_id"], {"turn_id": turn["id"]})
            for reflection in reflections:
                self.db.conn.execute("UPDATE reflections SET status='reconcile_required' WHERE id=?", (reflection["id"],))
                self.db.event("reflection.reconcile_required", "controller", reflection["id"])
        return len(active) + len(reflections)

    async def reconcile_inflight(self):
        pending = self.db.many("SELECT * FROM turns WHERE status='reconcile_required'")
        for turn in pending:
            if not turn["codex_turn_id"]:
                continue
            session = self.session(turn["session_id"])
            try:
                await self.codex.request("thread/resume", {"threadId": session["thread_id"], "excludeTurns": True})
                cursor = None
                while True:
                    params = {"threadId": session["thread_id"], "limit": 100}
                    if cursor:
                        params["cursor"] = cursor
                    page = await self.codex.request("thread/turns/list", params)
                    found = next((entry for entry in page.get("data", []) if entry.get("id") == turn["codex_turn_id"]), None)
                    if found:
                        if found.get("status") in ("completed", "failed", "interrupted"):
                            await self.handle_notification("turn/completed", {"threadId": session["thread_id"], "turn": found})
                        await self.backfill_turn_items(turn["id"])
                        break
                    cursor = page.get("nextCursor")
                    if not cursor:
                        break
            except Exception as exc:
                self.db.event("codex.reconcile_failed", "controller", session["id"], {"turn_id": turn["id"], "error": str(exc)})
        for reflection in self.db.many("SELECT * FROM reflections WHERE status='reconcile_required'"):
            if not reflection["thread_id"] or not reflection["codex_turn_id"]:
                continue
            try:
                await self.codex.request("thread/resume", {"threadId": reflection["thread_id"], "excludeTurns": True})
                cursor = None
                while True:
                    params = {"threadId": reflection["thread_id"], "limit": 100}
                    if cursor:
                        params["cursor"] = cursor
                    page = await self.codex.request("thread/turns/list", params)
                    found = next((entry for entry in page.get("data", []) if entry.get("id") == reflection["codex_turn_id"]), None)
                    if found:
                        if found.get("status") in ("completed", "failed", "interrupted"):
                            await self.backfill_reflection_items(reflection)
                            await self.handle_notification("turn/completed", {"threadId": reflection["thread_id"], "turn": found})
                        break
                    cursor = page.get("nextCursor")
                    if not cursor:
                        break
            except Exception as exc:
                self.db.event("reflection.reconcile_failed", "controller", reflection["id"], {"error": str(exc)})

    async def backfill_reflection_items(self, reflection):
        cursor = None
        while True:
            params = {"threadId": reflection["thread_id"], "turnId": reflection["codex_turn_id"], "limit": 100}
            if cursor:
                params["cursor"] = cursor
            page = await self.codex.request("thread/items/list", params)
            for entry in page.get("data", []):
                await self.handle_notification("item/completed", {
                    "threadId": reflection["thread_id"], "turnId": reflection["codex_turn_id"],
                    "item": entry.get("item", {})})
            cursor = page.get("nextCursor")
            if not cursor:
                break

    async def backfill_turn_items(self, turn_id):
        turn = self.db.one("SELECT * FROM turns WHERE id=?", (turn_id,))
        session = self.session(turn["session_id"])
        cursor = None
        while True:
            params = {"threadId": session["thread_id"], "turnId": turn["codex_turn_id"], "limit": 100}
            if cursor:
                params["cursor"] = cursor
            page = await self.codex.request("thread/items/list", params)
            with self.db.transaction():
                for entry in page.get("data", []):
                    item = entry.get("item", {})
                    item_id = item.get("id")
                    item_type = item.get("type", "unknown")
                    if item_id and self.db.one("SELECT id FROM turn_items WHERE turn_id=? AND item_id=? AND type=?",
                                               (turn_id, item_id, item_type)):
                        continue
                    self.db.conn.execute("INSERT INTO turn_items VALUES (?,?,?,?,?,?)",
                                         (uid(), turn_id, item_id, item_type, json.dumps(item), now()))
            cursor = page.get("nextCursor")
            if not cursor:
                break

    def sessions(self):
        return self.db.many("SELECT * FROM sessions ORDER BY created_at, id")

    def session(self, session_id):
        return self.db.one("SELECT * FROM sessions WHERE id=?", (session_id,))

    def work_item(self, item_id):
        return self.db.one("SELECT * FROM work_items WHERE id=?", (item_id,))

    def work_items(self, session_id):
        return self.db.many("SELECT * FROM work_items WHERE session_id=? ORDER BY priority, created_at, rowid", (session_id,))

    def turns(self, session_id):
        return self.db.many("SELECT * FROM turns WHERE session_id=? ORDER BY rowid", (session_id,))

    def turns_page(self, session_id, limit=50, before=None):
        limit = max(1, min(int(limit), 100))
        if before is None:
            rows = self.db.many("SELECT rowid AS cursor,* FROM turns WHERE session_id=? ORDER BY rowid DESC LIMIT ?", (session_id, limit + 1))
        else:
            rows = self.db.many("SELECT rowid AS cursor,* FROM turns WHERE session_id=? AND rowid<? ORDER BY rowid DESC LIMIT ?", (session_id, int(before), limit + 1))
        has_more = len(rows) > limit
        rows = rows[:limit]
        for turn in rows:
            turn["items"] = self.turn_items(turn["id"])
        return {"data": rows, "next_cursor": rows[-1]["cursor"] if has_more else None}

    def turn_items(self, turn_id):
        rows = self.db.many("SELECT * FROM turn_items WHERE turn_id=? ORDER BY rowid", (turn_id,))
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows

    def reflection_items(self, reflection_id):
        rows = self.db.many("SELECT * FROM reflection_items WHERE reflection_id=? ORDER BY rowid", (reflection_id,))
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows

    def events(self, entity_id=None):
        if entity_id:
            return self.db.many("SELECT * FROM events WHERE entity_id=? ORDER BY rowid", (entity_id,))
        return self.db.many("SELECT * FROM events ORDER BY rowid")

    async def create_session(self, title, objective, cwd, scope=None, _split_branch=None):
        cwd = str(Path(cwd).resolve())
        if not Path(cwd).is_dir():
            raise ValueError("working directory does not exist")
        session_id = uid()
        params = {"cwd": cwd}
        if self.mcp_url:
            params["config"] = self._mcp_config(session_id)
        response = await self.codex.request("thread/start", params)
        thread_id = response["thread"]["id"]
        with self.db.transaction():
            self.db.conn.execute("INSERT INTO sessions (id,thread_id,title,objective,scope,cwd,created_at) VALUES (?,?,?,?,?,?,?)",
                                 (session_id, thread_id, title, objective, json.dumps(scope or {}), cwd, now()))
            if _split_branch:
                self.db.conn.execute("UPDATE split_branches SET session_id=?,status='created' WHERE plan_id=? AND branch_index=?",
                                     (session_id, _split_branch[0], _split_branch[1]))
            self.db.event("session.created", "user", session_id, {"title": title, "thread_id": thread_id})
        return session_id

    async def fork_session(self, parent_id, title, objective, scope=None):
        parent = self.session(parent_id)
        if not parent:
            raise KeyError(parent_id)
        latest = self.db.one("SELECT codex_turn_id FROM turns WHERE session_id=? AND status='completed' ORDER BY rowid DESC LIMIT 1", (parent_id,))
        session_id = uid()
        params = {"threadId": parent["thread_id"], "ephemeral": False}
        if self.mcp_url:
            params["config"] = self._mcp_config(session_id)
        if latest and latest["codex_turn_id"]:
            params["lastTurnId"] = latest["codex_turn_id"]
        response = await self.codex.request("thread/fork", params)
        with self.db.transaction():
            self.db.conn.execute("INSERT INTO sessions (id,thread_id,title,objective,scope,cwd,created_at) VALUES (?,?,?,?,?,?,?)",
                                 (session_id, response["thread"]["id"], title, objective, json.dumps(scope or {}), parent["cwd"], now()))
            self.db.event("session.created", "agent", session_id, {"forked_from": parent_id})
        self.add_edge(parent_id, session_id, "FORKED_FROM", False)
        return session_id

    def update_scope(self, session_id, scope):
        if not isinstance(scope, dict):
            raise ValueError("scope must be an object")
        with self.db.transaction():
            self.db.conn.execute("UPDATE sessions SET scope=? WHERE id=?", (json.dumps(scope), session_id))
            self.db.event("session.scope_changed", "agent", session_id, {"scope": scope})

    async def submit(self, session_id, payload, source="manual", correlation_id=None):
        if source not in PRIORITY:
            raise ValueError("invalid source")
        session = self.session(session_id)
        if not session:
            raise KeyError(session_id)
        if session["status"] == "archived":
            raise ValueError("session is archived")
        item_id = uid()
        with self.db.transaction():
            self.db.conn.execute("INSERT INTO work_items (id,session_id,source,priority,payload,correlation_id,created_at) VALUES (?,?,?,?,?,?,?)",
                                 (item_id, session_id, source, PRIORITY[source], payload, correlation_id, now()))
            self.db.event("work." + {"manual": "manual_submitted", "agent": "agent_requested", "cron": "cron_due"}[source],
                          source, session_id, {"work_item_id": item_id}, correlation_id)
        return item_id

    async def pause(self):
        async with self.dispatch_gate:
            with self.db.transaction():
                self.db.conn.execute("UPDATE controller_state SET paused=1, revision=revision+1, changed_at=? WHERE id=1", (now(),))
                self.db.event("controller.paused", "user")

    async def resume(self):
        async with self.dispatch_gate:
            with self.db.transaction():
                self.db.conn.execute("UPDATE controller_state SET paused=0, revision=revision+1, changed_at=? WHERE id=1", (now(),))
                self.db.event("controller.resumed", "user")

    def compose_prompt(self, item, neighbors=None):
        s = self.session(item["session_id"])
        lines = [f"Session objective: {s['objective']}", f"Scope: {s['scope']}",
                 f"Actionable {item['source']} input: {item['payload']}"]
        for neighbor in neighbors if neighbors is not None else self.neighbors(s["id"]):
            lines.append(f"Direct neighbor {neighbor['id']}: {neighbor['title']} — {neighbor['summary']}")
            delivered = self._delivered_revision(s["id"], neighbor["id"])
            changes = self.db.many("""SELECT revision,title,summary FROM summaries
                WHERE session_id=? AND revision>? AND revision<=? ORDER BY revision""",
                (neighbor["id"], delivered, neighbor["summary_revision"]))
            for change in changes:
                lines.append(f"Undelivered neighbor revision {change['revision']}: {change['title']} — {change['summary']}")
        return "\n".join(lines)

    def _delivered_revision(self, session_id, neighbor_id):
        return self.db.one("""SELECT coalesce(max(d.revision),0) AS revision FROM context_delivery d
            JOIN turns t ON t.id=d.turn_id WHERE t.session_id=? AND d.neighbor_id=? AND d.acknowledged=1""",
            (session_id, neighbor_id))["revision"]

    def passive_inbox(self, session_id):
        updates = []
        for neighbor in self.neighbors(session_id):
            delivered = self._delivered_revision(session_id, neighbor["id"])
            if neighbor["summary_revision"] > delivered:
                updates.append({"neighbor_id": neighbor["id"], "title": neighbor["title"],
                                "summary": neighbor["summary"], "revision": neighbor["summary_revision"],
                                "delivered_revision": delivered})
        return updates

    async def dispatch_once(self):
        async with self.dispatch_gate:
            if self.status()["paused"]:
                return False
            item = self.db.one("""SELECT w.* FROM work_items w JOIN sessions s ON s.id=w.session_id
                WHERE w.status='queued' AND s.held=0 AND s.status NOT IN ('running','reflecting','splitting','archived')
                ORDER BY w.priority, w.created_at, w.rowid LIMIT 1""")
            if not item:
                return False
            s = self.session(item["session_id"])
            turn_id = uid()
            neighbor_snapshot = self.neighbors(s["id"])
            prompt = self.compose_prompt(item, neighbor_snapshot)
            with self.db.transaction():
                self.db.conn.execute("UPDATE work_items SET status='claimed',claimed_at=? WHERE id=?", (now(), item["id"]))
                self.db.conn.execute("INSERT INTO turns (id,session_id,work_item_id,status,started_at) VALUES (?,?,?,?,?)",
                                     (turn_id, s["id"], item["id"], "starting", now()))
                self.db.conn.execute("UPDATE sessions SET status='running' WHERE id=?", (s["id"],))
                self.db.event("codex.turn_starting", "controller", s["id"], {"turn_id": turn_id})
            try:
                response = await self.codex.request("turn/start", {"threadId": s["thread_id"], "input": [{"type": "text", "text": prompt}]})
                codex_turn_id = response["turn"]["id"]
            except Exception as exc:
                # An uncertain start must be reconciled before retrying this work item.
                with self.db.transaction():
                    self.db.conn.execute("UPDATE turns SET status='reconcile_required',error=? WHERE id=?", (str(exc), turn_id))
                    self.db.conn.execute("UPDATE work_items SET status='reconcile_required' WHERE id=?", (item["id"],))
                    self.db.conn.execute("UPDATE sessions SET status='held',held=1 WHERE id=?", (s["id"],))
                    self.db.event("codex.turn_start_uncertain", "controller", s["id"], {"error": str(exc), "turn_id": turn_id})
                return False
            with self.db.transaction():
                self.db.conn.execute("UPDATE turns SET codex_turn_id=?,status='running' WHERE id=?", (codex_turn_id, turn_id))
                self.db.conn.execute("UPDATE work_items SET status='running' WHERE id=?", (item["id"],))
                if item["source"] == "cron" and item["correlation_id"]:
                    self.db.conn.execute("UPDATE cron_schedules SET last_started=? WHERE id=?", (now(), item["correlation_id"]))
                for neighbor in neighbor_snapshot:
                    self.db.conn.execute("INSERT OR REPLACE INTO context_delivery VALUES (?,?,?,1)",
                                         (turn_id, neighbor["id"], neighbor["summary_revision"]))
                self.db.event("codex.turn_started", "codex", s["id"], {"turn_id": turn_id, "codex_turn_id": codex_turn_id})
            for method, params in self.early_notifications.pop((s["thread_id"], codex_turn_id), []):
                await self.handle_notification(method, params)
            return True

    async def stop_turn(self, turn_id):
        t = self.db.one("SELECT * FROM turns WHERE id=?", (turn_id,))
        if not t or t["status"] not in ("running", "interrupt_requested"):
            raise ValueError("turn is not running")
        if t["status"] == "interrupt_requested":
            return
        s = self.session(t["session_id"])
        with self.db.transaction():
            self.db.conn.execute("UPDATE sessions SET held=1,status='held' WHERE id=?", (s["id"],))
            self.db.conn.execute("UPDATE turns SET status='interrupt_requested' WHERE id=?", (turn_id,))
            self.db.event("turn.stop_requested", "user", s["id"], {"turn_id": turn_id})
        try:
            await self.codex.request("turn/interrupt", {"threadId": s["thread_id"], "turnId": t["codex_turn_id"]})
        except Exception as exc:
            self.db.conn.execute("UPDATE turns SET status='interrupt_failed',error=? WHERE id=?", (str(exc), turn_id))
            self.db.event("turn.interrupt_failed", "controller", s["id"], {"turn_id": turn_id, "error": str(exc)})
            raise

    async def stop_all(self):
        await self.pause()
        errors = []
        active = self.db.many("SELECT id FROM turns WHERE status='running'")
        for turn in active:
            try:
                await self.stop_turn(turn["id"])
            except Exception as exc:
                errors.append({"turn_id": turn["id"], "error": str(exc)})
        reflections = self.db.many("SELECT * FROM reflections WHERE status='running'")
        for reflection in reflections:
            try:
                await self.stop_reflection(reflection["id"])
            except Exception as exc:
                errors.append({"reflection_id": reflection["id"], "error": str(exc)})
        return errors

    async def stop_reflection(self, reflection_id):
        reflection = self.db.one("SELECT * FROM reflections WHERE id=?", (reflection_id,))
        if not reflection or reflection["status"] != "running":
            raise ValueError("reflection is not running")
        self.db.conn.execute("UPDATE reflections SET status='interrupt_requested' WHERE id=?", (reflection_id,))
        try:
            await self.codex.request("turn/interrupt", {"threadId": reflection["thread_id"], "turnId": reflection["codex_turn_id"]})
        except Exception:
            self.db.conn.execute("UPDATE reflections SET status='interrupt_failed' WHERE id=?", (reflection_id,))
            raise

    def hold_session(self, session_id):
        with self.db.transaction():
            self.db.conn.execute("UPDATE sessions SET held=1,status='held' WHERE id=?", (session_id,))
            self.db.event("session.held", "user", session_id)

    def archive_session(self, session_id):
        session = self.session(session_id)
        if not session:
            raise KeyError(session_id)
        if session["status"] in ("running", "reflecting", "splitting"):
            raise ValueError("stop the active turn before archiving")
        if self.db.one("SELECT id FROM work_items WHERE session_id=? AND status IN ('claimed','running','reconcile_required') LIMIT 1",
                       (session_id,)):
            raise ValueError("resolve active work before archiving")
        with self.db.transaction():
            queued = self.db.many("SELECT id FROM work_items WHERE session_id=? AND status='queued'", (session_id,))
            for item in queued:
                self.db.conn.execute("UPDATE work_items SET status='cancelled' WHERE id=?", (item["id"],))
                self.db.event("work.cancelled", "user", session_id, {"work_item_id": item["id"], "reason": "session archived"})
            self.db.conn.execute("UPDATE sessions SET status='archived',held=1 WHERE id=?", (session_id,))
            self.db.event("session.archived", "user", session_id)

    def _has_unfinished_work(self, session_id):
        return bool(self.db.one(
            "SELECT id FROM work_items WHERE session_id=? AND status IN ('queued','claimed','running','reconcile_required') LIMIT 1",
            (session_id,)))

    def resume_session(self, session_id):
        if self.session(session_id)["status"] == "archived":
            raise ValueError("archived sessions cannot resume")
        active = self.db.one("SELECT id FROM turns WHERE session_id=? AND status IN ('starting','running','interrupt_requested','interrupt_failed','reconcile_required')", (session_id,))
        if active:
            raise ValueError("wait for the active turn to finish or reconcile before resuming")
        with self.db.transaction():
            self.db.conn.execute("UPDATE sessions SET held=0,status='ready' WHERE id=?", (session_id,))
            self.db.event("session.resumed", "user", session_id)

    def cancel_work_item(self, item_id):
        with self.db.transaction():
            self.db.conn.execute("UPDATE work_items SET status='cancelled' WHERE id=? AND status='queued'", (item_id,))
            self.db.event("work.cancelled", "user", item_id)

    async def retry_turn(self, turn_id):
        t = self.db.one("SELECT * FROM turns WHERE id=?", (turn_id,))
        if not t or t["status"] not in ("failed", "interrupted"):
            raise ValueError("only failed or interrupted turns can be retried")
        old = self.work_item(t["work_item_id"])
        return await self.submit(t["session_id"], old["payload"], "manual")

    async def handle_notification(self, method, params):
        thread_id = params.get("threadId")
        s = self.db.one("SELECT * FROM sessions WHERE thread_id=?", (thread_id,)) if thread_id else None
        if not s:
            reflection = self.db.one("SELECT * FROM reflections WHERE thread_id=?", (thread_id,)) if thread_id else None
            if reflection:
                await self._handle_reflection_notification(reflection, method, params)
            return
        codex_turn_id = params.get("turnId") or params.get("turn", {}).get("id")
        t = self.db.one("SELECT * FROM turns WHERE codex_turn_id=?", (codex_turn_id,)) if codex_turn_id else None
        if not t and codex_turn_id and (method == "turn/completed" or method.startswith("item/") or method == "turn/diff/updated"):
            starting = self.db.one("SELECT id FROM turns WHERE session_id=? AND status='starting'", (s["id"],))
            if starting:
                self.early_notifications.setdefault((thread_id, codex_turn_id), []).append((method, params))
            return
        if method == "turn/diff/updated" and t:
            diff = params.get("diff", "")
            paths = set(re.findall(r"^\+\+\+ b/(.+)$", diff, re.MULTILINE))
            with self.db.transaction():
                for path in paths:
                    inserted = self.db.conn.execute("INSERT OR IGNORE INTO turn_paths VALUES (?,?,?)", (t["id"], path, now())).rowcount
                    if not inserted:
                        continue
                    overlapping = self.db.many("""SELECT other.turn_id FROM turn_paths other
                        JOIN turns ot ON ot.id=other.turn_id JOIN sessions os ON os.id=ot.session_id
                        WHERE other.path=? AND other.turn_id<>? AND os.cwd=?
                        AND (ot.finished_at IS NULL OR ot.finished_at>=?)""",
                        (path, t["id"], s["cwd"], t["started_at"]))
                    for other in overlapping:
                        self.db.event("workspace.conflict", "controller", s["id"],
                                      {"path": path, "turn_ids": [other["turn_id"], t["id"]]})
                self.db.event("codex.turn_diff_updated", "codex", s["id"], {"turn_id": t["id"], "diff": diff})
        elif method == "turn/completed" and t:
            status = params["turn"].get("status", "failed")
            if status not in ("completed", "interrupted", "failed"):
                status = "failed"
            with self.db.transaction():
                self.db.conn.execute("UPDATE turns SET status=?,finished_at=? WHERE id=?", (status, now(), t["id"]))
                self.db.conn.execute("UPDATE work_items SET status=? WHERE id=?", (status, t["work_item_id"]))
                work = self.work_item(t["work_item_id"])
                if work["source"] == "cron" and work["correlation_id"]:
                    messages = [item["payload"].get("text", "") for item in self.turn_items(t["id"])
                                if item["type"] == "agentMessage"]
                    outcome = "no_work" if status == "completed" and messages and messages[-1].strip().lower() == "no_work" else status
                    self.db.conn.execute("UPDATE cron_schedules SET last_finished=?,last_outcome=? WHERE id=?",
                                         (now(), outcome, work["correlation_id"]))
                    self.db.event("cron.run_finished", "cron", s["id"], {"schedule_id": work["correlation_id"], "outcome": outcome})
                self.db.conn.execute("UPDATE sessions SET status=? WHERE id=?", ("held" if s["held"] else "ready", s["id"]))
                self.db.event("codex.turn_completed", "codex", s["id"], {"turn_id": t["id"], "status": status})
                if status == "completed":
                    self.db.conn.execute("INSERT OR IGNORE INTO reflections (id,worker_turn_id,status,created_at) VALUES (?,?,?,?)",
                                         (uid(), t["id"], "queued", now()))
        elif method.startswith("item/") and t:
            item = params.get("item", params)
            with self.db.transaction():
                self.db.conn.execute("INSERT INTO turn_items VALUES (?,?,?,?,?,?)",
                                     (uid(), t["id"], item.get("id"), item.get("type", method), json.dumps(item), now()))
                self.db.event("codex." + method.replace("/", "_"), "codex", s["id"], {"turn_id": t["id"], "item": item})
        elif method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage", {})
            tokens = usage.get("last", {}).get("totalTokens", 0)
            self.db.conn.execute("UPDATE sessions SET context_tokens=?,context_window=coalesce(?,context_window) WHERE id=?",
                                 (tokens, usage.get("modelContextWindow"), s["id"]))

    def graph(self):
        return self.db.many("SELECT * FROM session_edges WHERE active=1")

    def neighbors(self, session_id):
        ids = set()
        for e in self.graph():
            if e["communication_enabled"] and e["type"] != "FORKED_FROM":
                if e["source_id"] == session_id:
                    ids.add(e["target_id"])
                if e["target_id"] == session_id:
                    ids.add(e["source_id"])
        return [s for sid in sorted(ids) if (s := self.session(sid)) and s["status"] != "archived"]

    def _parent(self, session_id):
        e = self.db.one("SELECT source_id FROM session_edges WHERE target_id=? AND type='PARENT_OF' AND active=1", (session_id,))
        return e["source_id"] if e else None

    def can_contact(self, source_id, target_id):
        if source_id == target_id:
            return False
        if target_id in {s["id"] for s in self.neighbors(source_id)}:
            return True
        parent = self._parent(source_id)
        return bool(parent and (parent == target_id or self._parent(parent) == target_id))

    def add_edge(self, source_id, target_id, edge_type, communication_enabled=True):
        if edge_type not in EDGE_TYPES or source_id == target_id:
            raise ValueError("invalid relationship")
        if not self.session(source_id) or not self.session(target_id):
            raise ValueError("unknown session")
        if edge_type == "PARENT_OF":
            if self._parent(target_id):
                raise ValueError("target already has a parent")
            p = source_id
            while p:
                if p == target_id:
                    raise ValueError("parent cycle")
                p = self._parent(p)
        edge_id = uid()
        with self.db.transaction():
            self.db.conn.execute("INSERT INTO session_edges VALUES (?,?,?,?,?,?,?)",
                                 (edge_id, source_id, target_id, edge_type, 1, int(communication_enabled), now()))
            self.db.event("session.relationship_changed", "controller", source_id, {"target": target_id, "type": edge_type})
        return edge_id

    def remove_edge(self, edge_id):
        with self.db.transaction():
            self.db.conn.execute("UPDATE session_edges SET active=0 WHERE id=?", (edge_id,))
            self.db.event("session.relationship_changed", "controller", edge_id, {"active": False})

    def publish_summary(self, session_id, title, summary):
        s = self.session(session_id)
        revision = s["summary_revision"] + 1
        with self.db.transaction():
            self.db.conn.execute("UPDATE sessions SET title=?,summary=?,summary_revision=? WHERE id=?",
                                 (title, summary, revision, session_id))
            self.db.conn.execute("INSERT INTO summaries VALUES (?,?,?,?,?,?)",
                                 (uid(), session_id, revision, title, summary, now()))
            self.db.event("session.summary_published", "reflection", session_id, {"revision": revision})

    async def send_request(self, source_id, target_id, question, idempotency_key=None):
        if not self.can_contact(source_id, target_id):
            raise PermissionError("target is outside the permitted contact graph")
        request_key = f"{source_id}:{idempotency_key}" if idempotency_key else None
        if request_key:
            existing = self.db.one("SELECT * FROM agent_requests WHERE request_key=?", (request_key,))
            if existing:
                if existing["target_id"] != target_id or existing["question"] != question:
                    raise ValueError("idempotency key was used for different request content")
                return existing["id"]
        request_id = uid()
        with self.db.transaction():
            self.db.conn.execute("INSERT INTO agent_requests VALUES (?,?,?,?,?,?,?,?)",
                                 (request_id, source_id, target_id, question, None, "queued", request_key, now()))
        await self.submit(target_id, question, "agent", request_id)
        return request_id

    async def reply(self, request_id, answer, responder_id=None):
        req = self.db.one("SELECT * FROM agent_requests WHERE id=?", (request_id,))
        if not req:
            raise KeyError(request_id)
        if responder_id is not None and responder_id != req["target_id"]:
            raise PermissionError("only the target session may reply")
        if req["status"] == "answered":
            raise ValueError("request already answered")
        self.db.conn.execute("UPDATE agent_requests SET reply=?,status='answered' WHERE id=?", (answer, request_id))
        await self.submit(req["source_id"], answer, "agent", request_id)

    def request_status(self, request_id, requester_id=None):
        request = self.db.one("SELECT * FROM agent_requests WHERE id=?", (request_id,))
        if not request:
            raise KeyError(request_id)
        if requester_id is not None and requester_id not in (request["source_id"], request["target_id"]):
            raise PermissionError("requester is not a participant")
        return request

    async def record_approval(self, rpc_id, method, params):
        approval_id = uid()
        self.db.conn.execute("INSERT INTO approvals VALUES (?,?,?,?,?,?,?)",
                             (approval_id, str(rpc_id), params.get("threadId"), method, json.dumps(params), None, now()))
        self.db.event("approval.requested", "codex", params.get("threadId"), {"approval_id": approval_id, "method": method})
        return approval_id

    async def resolve_approval(self, approval_id, decision):
        approval = self.db.one("SELECT * FROM approvals WHERE id=? AND decision IS NULL", (approval_id,))
        if not approval:
            raise ValueError("approval is not pending")
        if decision not in ("accept", "decline"):
            raise ValueError("decision must be accept or decline")
        legacy = approval["method"] in ("execCommandApproval", "applyPatchApproval")
        if approval["method"] == "item/permissions/requestApproval":
            requested = json.loads(approval["params"]).get("permissions", {})
            result = {"permissions": requested if decision == "accept" else {}, "scope": "turn"}
        elif legacy or approval["method"] in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            result = {"decision": ("approved" if decision == "accept" else "denied") if legacy else decision}
        else:
            raise ValueError("this approval type requires a specialized response")
        await self.codex.respond(int(approval["rpc_id"]), result)
        self.db.conn.execute("UPDATE approvals SET decision=? WHERE id=?", (decision, approval_id))
        self.db.event("approval.resolved", "user", approval["thread_id"], {"approval_id": approval_id, "decision": decision})

    def pending_reflections(self):
        return self.db.many("SELECT * FROM reflections WHERE status='queued' ORDER BY rowid")

    def retry_reflection(self, reflection_id):
        reflection = self.db.one("SELECT * FROM reflections WHERE id=?", (reflection_id,))
        if not reflection or reflection["status"] not in ("failed", "interrupt_failed"):
            raise ValueError("reflection is not retryable")
        with self.db.transaction():
            self.db.conn.execute("UPDATE reflections SET status='queued',thread_id=NULL,codex_turn_id=NULL WHERE id=?", (reflection_id,))
            self.db.event("reflection.retried", "user", reflection_id)

    async def dispatch_reflection_once(self):
        async with self.dispatch_gate:
            if self.status()["paused"]:
                return False
            reflection = self.db.one("SELECT * FROM reflections WHERE status='queued' ORDER BY rowid LIMIT 1")
            if not reflection:
                return False
            worker = self.db.one("SELECT * FROM turns WHERE id=?", (reflection["worker_turn_id"],))
            session = self.session(worker["session_id"])
            with self.db.transaction():
                self.db.conn.execute("UPDATE reflections SET status='starting' WHERE id=?", (reflection["id"],))
                self.db.event("reflection.queued", "controller", session["id"], {"reflection_id": reflection["id"]})
            try:
                fork = await self.codex.request("thread/fork", {
                    "threadId": session["thread_id"], "lastTurnId": worker["codex_turn_id"],
                    "ephemeral": True, "sandbox": "read-only",
                    "developerInstructions": "Reflection only. Inspect the completed turn and return JSON. Do not edit files, execute external operations, or use agent controller MCP tools.",
                })
                reflection_thread = fork["thread"]["id"]
                prompt = ("Reflect on the completed worker turn. Return only JSON with keys: "
                          "rename {required,new_title}, summary, accomplishments, decisions, resolved, "
                          "remaining_work, blockers, artifacts, split {recommended,reason,relationship,"
                          "original_role,original_objective,original_scope,branches}. "
                          "For a recommended split choose original_role coordinator, archived, or specialist. "
                          "For specialist include a focused original_objective and original_scope. "
                          "Each branch needs title, objective, in_scope, out_of_scope, deliverables, dependencies, and handoff. "
                          "Report only outcomes supported by the turn. "
                          f"Current title: {session['title']}. Objective: {session['objective']}. "
                          f"Current context tokens: {session['context_tokens']}.")
                self.db.conn.execute("UPDATE reflections SET thread_id=? WHERE id=?", (reflection_thread, reflection["id"]))
                result = await self.codex.request("turn/start", {"threadId": reflection_thread, "input": [{"type": "text", "text": prompt}]})
                self.db.conn.execute("UPDATE reflections SET codex_turn_id=?,status='running' WHERE id=? AND status='starting'",
                                     (result["turn"]["id"], reflection["id"]))
            except Exception as exc:
                self.db.conn.execute("UPDATE reflections SET status='failed',analysis=? WHERE id=?", (str(exc), reflection["id"]))
                return False
            return True

    async def _handle_reflection_notification(self, reflection, method, params):
        if reflection["status"] == "completed":
            return
        if method.startswith("item/"):
            item = params.get("item", params)
            if not (item.get("id") and self.db.one("SELECT id FROM reflection_items WHERE reflection_id=? AND item_id=? AND type=?",
                                                    (reflection["id"], item.get("id"), item.get("type", method)))):
                self.db.conn.execute("INSERT INTO reflection_items VALUES (?,?,?,?,?,?)",
                                     (uid(), reflection["id"], item.get("id"), item.get("type", method), json.dumps(item), now()))
        if method == "item/completed":
            if item.get("type") == "agentMessage":
                self.db.conn.execute("UPDATE reflections SET analysis=? WHERE id=?", (item.get("text", ""), reflection["id"]))
        if method != "turn/completed":
            return
        status = params.get("turn", {}).get("status")
        if status != "completed":
            self.db.conn.execute("UPDATE reflections SET status='failed' WHERE id=?", (reflection["id"],))
            return
        try:
            analysis = json.loads(self.db.one("SELECT analysis FROM reflections WHERE id=?", (reflection["id"],))["analysis"])
            if not isinstance(analysis.get("summary"), str):
                raise ValueError("reflection summary missing")
            worker = self.db.one("SELECT * FROM turns WHERE id=?", (reflection["worker_turn_id"],))
            s = self.session(worker["session_id"])
            rename = analysis.get("rename") or {}
            title = rename.get("new_title") if rename.get("required") and rename.get("new_title") else s["title"]
            self.publish_summary(s["id"], title, analysis["summary"])
            if title != s["title"]:
                try:
                    await self.codex.request("thread/name/set", {"threadId": s["thread_id"], "name": title})
                except Exception as exc:
                    self.db.event("session.codex_name_sync_failed", "controller", s["id"], {"error": str(exc)})
            with self.db.transaction():
                self.db.conn.execute("UPDATE reflections SET status='completed',analysis=? WHERE id=?", (json.dumps(analysis), reflection["id"]))
                self.db.event("reflection.completed", "reflection", s["id"], {"reflection_id": reflection["id"]})
            split = analysis.get("split") or {}
            if self.should_evaluate_split(s) and split.get("recommended") and len(split.get("branches") or []) >= 2:
                try:
                    await self._commit_split(s, split, source_key=reflection["id"])
                except Exception as exc:
                    self.db.event("split.failed", "controller", s["id"], {"error": str(exc)})
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            self.db.conn.execute("UPDATE reflections SET status='failed',analysis=? WHERE id=?", (str(exc), reflection["id"]))

    async def _commit_split(self, original, plan, source_key=None):
        original = self.session(original["id"])
        branches = plan["branches"]
        if not all(isinstance(b.get("title"), str) and isinstance(b.get("objective"), str) for b in branches):
            raise ValueError("split branches need title and objective")
        if plan.get("original_role") == "specialist":
            if not isinstance(plan.get("original_objective"), str) or not plan["original_objective"].strip() or not isinstance(plan.get("original_scope"), dict):
                raise ValueError("specialist split needs original objective and scope")
        if plan.get("original_role") in ("archived", "archive") and self._has_unfinished_work(original["id"]):
            raise ValueError("cannot archive a split origin with queued or active work")
        saved = self.db.one("SELECT * FROM split_plans WHERE source_key=?", (source_key,)) if source_key else None
        if not saved:
            pending = self.db.one("SELECT * FROM split_plans WHERE original_id=? AND status='pending' ORDER BY rowid DESC LIMIT 1",
                                  (original["id"],))
            if pending:
                if source_key and pending["source_key"] != source_key:
                    raise ValueError("another split plan is still pending for this session")
                saved = pending
            elif not source_key:
                latest = self.db.one("SELECT * FROM split_plans WHERE original_id=? ORDER BY rowid DESC LIMIT 1",
                                     (original["id"],))
                if latest and latest["status"] == "committed" and json.loads(latest["plan"]) == plan:
                    return
        if saved:
            if saved["original_id"] != original["id"] or json.loads(saved["plan"]) != plan:
                raise ValueError("split retry does not match its recorded plan")
            if saved["status"] == "committed":
                return
            plan_id = saved["id"]
        else:
            plan_id = uid()
            with self.db.transaction():
                self.db.conn.execute("""INSERT INTO split_plans
                    (id,original_id,source_key,plan,status,created_at) VALUES (?,?,?,?,?,?)""",
                                     (plan_id, original["id"], source_key, json.dumps(plan), "pending", now()))
                self.db.event("split.proposed", "reflection", original["id"], plan)
        if plan.get("original_role") == "coordinator":
            scope = {"in_scope": ["Coordinate successor sessions"], "out_of_scope": [b["objective"] for b in branches]}
            if json.loads(self.session(original["id"])["scope"]) != scope:
                self.update_scope(original["id"], scope)
        elif plan.get("original_role") == "specialist":
            if json.loads(self.session(original["id"])["scope"]) != plan["original_scope"]:
                self.update_scope(original["id"], plan["original_scope"])
            if self.session(original["id"])["objective"] != plan["original_objective"]:
                with self.db.transaction():
                    self.db.conn.execute("UPDATE sessions SET objective=? WHERE id=?",
                                         (plan["original_objective"], original["id"]))
                    self.db.event("session.objective_changed", "reflection", original["id"],
                                  {"objective": plan["original_objective"], "split_plan_id": plan_id})
        created = []
        for index, branch in enumerate(branches):
            scope = {"in_scope": branch.get("in_scope", [branch["objective"]]),
                     "out_of_scope": branch.get("out_of_scope", []),
                     "deliverables": branch.get("deliverables", []),
                     "dependencies": branch.get("dependencies", []),
                     "split_origin": original["id"]}
            record = self.db.one("SELECT * FROM split_branches WHERE plan_id=? AND branch_index=?", (plan_id, index))
            if record and record["session_id"]:
                sid = record["session_id"]
            else:
                if record:
                    raise RuntimeError(f"split branch {index} has an uncertain thread start; inspect before retrying")
                with self.db.transaction():
                    self.db.conn.execute("INSERT INTO split_branches VALUES (?,?,NULL,'starting')", (plan_id, index))
                sid = await self.create_session(branch["title"], branch["objective"], original["cwd"], scope,
                                                _split_branch=(plan_id, index))
            created.append(sid)
            edge_type = "PARENT_OF" if plan.get("relationship") == "parent-child" else "SIBLING_OF"
            if not self.db.one("SELECT id FROM session_edges WHERE source_id=? AND target_id=? AND type=? AND active=1",
                               (original["id"], sid, edge_type)):
                self.add_edge(original["id"], sid, edge_type)
            if not self.db.one("SELECT id FROM work_items WHERE session_id=? AND source='agent' AND correlation_id=?",
                               (sid, plan_id)):
                reflection = self.db.one("SELECT analysis FROM reflections WHERE id=?", (source_key,)) if source_key else None
                analysis = json.loads(reflection["analysis"]) if reflection and reflection["analysis"] else {}
                handoff = {
                    "origin_session_id": original["id"],
                    "origin_thread_id": original["thread_id"],
                    "origin_summary": original["summary"],
                    "objective": branch["objective"],
                    "scope": scope,
                    "decisions": analysis.get("decisions", []),
                    "remaining_work": analysis.get("remaining_work", []),
                    "blockers": analysis.get("blockers", []),
                    "artifacts": analysis.get("artifacts", []),
                    "branch_handoff": branch.get("handoff", ""),
                }
                await self.submit(sid, "Begin your scoped assignment using this handoff package. "
                                  "Verify inherited claims in the shared workspace before acting.\n"
                                  + json.dumps(handoff, ensure_ascii=False), "agent", plan_id)
        if plan.get("relationship") != "parent-child":
            for index, source_id in enumerate(created):
                for target_id in created[index + 1:]:
                    if not self.db.one("""SELECT id FROM session_edges WHERE type='SIBLING_OF' AND active=1
                        AND ((source_id=? AND target_id=?) OR (source_id=? AND target_id=?))""",
                        (source_id, target_id, target_id, source_id)):
                        self.add_edge(source_id, target_id, "SIBLING_OF")
        with self.db.transaction():
            if plan.get("original_role") in ("archived", "archive") and self.session(original["id"])["status"] != "archived":
                self.db.conn.execute("UPDATE sessions SET status='archived',held=1 WHERE id=?", (original["id"],))
                self.db.event("session.archived", "reflection", original["id"], {"split_plan_id": plan_id})
            self.db.conn.execute("UPDATE split_plans SET status='committed' WHERE id=?", (plan_id,))
            self.db.event("split.committed", "controller", original["id"], {"successors": created})

    async def resume_pending_splits(self):
        for saved in self.db.many("SELECT * FROM split_plans WHERE status='pending'"):
            try:
                await self._commit_split(self.session(saved["original_id"]), json.loads(saved["plan"]),
                                         source_key=saved["source_key"])
            except Exception as exc:
                self.db.event("split.recovery_failed", "controller", saved["original_id"], {"error": str(exc)})

    def set_cron(self, session_id, expression, timezone_name="UTC", enabled=True):
        if not self.session(session_id):
            raise KeyError(session_id)
        due = next_due(expression, timezone_name, now())
        old = self.db.one("SELECT id FROM cron_schedules WHERE session_id=?", (session_id,))
        schedule_id = old["id"] if old else uid()
        with self.db.transaction():
            self.db.conn.execute("""INSERT INTO cron_schedules
                (id,session_id,expression,timezone,enabled,next_due) VALUES (?,?,?,?,?,?)
                ON CONFLICT(session_id) DO UPDATE SET expression=excluded.expression,
                timezone=excluded.timezone,enabled=excluded.enabled,next_due=excluded.next_due""",
                (schedule_id, session_id, expression, timezone_name, int(enabled), due))
            self.db.event("cron.configured", "controller", session_id, {"expression": expression, "timezone": timezone_name})
        return schedule_id

    async def fire_due_cron(self, at=None):
        at = at or now()
        due = self.db.many("SELECT * FROM cron_schedules WHERE enabled=1 AND next_due<=?", (at,))
        for schedule in due:
            session = self.session(schedule["session_id"])
            with self.db.transaction():
                self.db.conn.execute("UPDATE cron_schedules SET last_due=?,next_due=? WHERE id=?",
                                     (schedule["next_due"], next_due(schedule["expression"], schedule["timezone"], at), schedule["id"]))
                self.db.event("cron.due", "cron", session["id"], {"schedule_id": schedule["id"]})
                if schedule["next_due"] < at:
                    self.db.event("cron.missed_coalesced", "cron", session["id"],
                                  {"first_due": schedule["next_due"], "observed_at": at})
            if self.status()["paused"]:
                self.db.conn.execute("UPDATE cron_schedules SET last_outcome='skipped_paused' WHERE id=?", (schedule["id"],))
                self.db.event("cron.skipped_while_paused", "cron", session["id"])
            elif session["status"] == "archived":
                self.db.conn.execute("UPDATE cron_schedules SET last_outcome='skipped_archived' WHERE id=?", (schedule["id"],))
                self.db.event("cron.skipped_archived", "cron", session["id"])
            elif not self.db.one("SELECT id FROM work_items WHERE session_id=? AND source='cron' AND status IN ('queued','claimed','running')", (session["id"],)):
                await self.submit(session["id"], "Review your objective and open work; execute one useful in-scope action and verify it, or report no_work.", "cron", schedule["id"])
