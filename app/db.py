"""SQLite journal and projections for controller-owned sessions."""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return str(uuid4())


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS controller_state (
                id INTEGER PRIMARY KEY CHECK (id = 1), paused INTEGER NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 0, changed_at TEXT NOT NULL
            );
            INSERT OR IGNORE INTO controller_state VALUES (1, 0, 0, CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, thread_id TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
                objective TEXT NOT NULL, scope TEXT NOT NULL DEFAULT '{}', cwd TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ready', held INTEGER NOT NULL DEFAULT 0,
                summary TEXT NOT NULL DEFAULT '', summary_revision INTEGER NOT NULL DEFAULT 0,
                context_tokens INTEGER NOT NULL DEFAULT 0, context_window INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS session_edges (
                id TEXT PRIMARY KEY, source_id TEXT NOT NULL, target_id TEXT NOT NULL,
                type TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                communication_enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS work_items (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, source TEXT NOT NULL,
                priority INTEGER NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
                correlation_id TEXT, created_at TEXT NOT NULL, claimed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS work_queue ON work_items(status, priority, created_at);
            CREATE TABLE IF NOT EXISTS turns (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, work_item_id TEXT NOT NULL,
                codex_turn_id TEXT UNIQUE, role TEXT NOT NULL DEFAULT 'worker',
                status TEXT NOT NULL, started_at TEXT, finished_at TEXT, error TEXT
            );
            CREATE TABLE IF NOT EXISTS turn_items (
                id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, item_id TEXT,
                type TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS turn_paths (
                turn_id TEXT NOT NULL, path TEXT NOT NULL,
                first_seen_at TEXT NOT NULL, PRIMARY KEY(turn_id,path)
            );
            CREATE TABLE IF NOT EXISTS reflections (
                id TEXT PRIMARY KEY, worker_turn_id TEXT UNIQUE NOT NULL,
                thread_id TEXT, codex_turn_id TEXT, status TEXT NOT NULL,
                analysis TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reflection_items (
                id TEXT PRIMARY KEY, reflection_id TEXT NOT NULL, item_id TEXT,
                type TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS summaries (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, revision INTEGER NOT NULL,
                title TEXT NOT NULL, summary TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS context_delivery (
                turn_id TEXT NOT NULL, neighbor_id TEXT NOT NULL, revision INTEGER NOT NULL,
                acknowledged INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(turn_id, neighbor_id)
            );
            CREATE TABLE IF NOT EXISTS agent_requests (
                id TEXT PRIMARY KEY, source_id TEXT NOT NULL, target_id TEXT NOT NULL,
                question TEXT NOT NULL, reply TEXT, status TEXT NOT NULL,
                request_key TEXT UNIQUE, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cron_schedules (
                id TEXT PRIMARY KEY, session_id TEXT UNIQUE NOT NULL, expression TEXT NOT NULL,
                timezone TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                last_due TEXT, last_started TEXT, last_finished TEXT, next_due TEXT,
                last_outcome TEXT
            );
            CREATE TABLE IF NOT EXISTS approvals (
                id TEXT PRIMARY KEY, rpc_id TEXT NOT NULL, thread_id TEXT,
                method TEXT NOT NULL, params TEXT NOT NULL, decision TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS split_plans (
                id TEXT PRIMARY KEY, original_id TEXT NOT NULL, source_key TEXT UNIQUE,
                plan TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS split_branches (
                plan_id TEXT NOT NULL, branch_index INTEGER NOT NULL,
                session_id TEXT, status TEXT NOT NULL,
                PRIMARY KEY(plan_id, branch_index)
            );
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, type TEXT NOT NULL, origin TEXT NOT NULL,
                entity_id TEXT, correlation_id TEXT, causation_id TEXT,
                payload TEXT NOT NULL, created_at TEXT NOT NULL
            );
        """)
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(sessions)")}
        if "context_window" not in columns:
            self.conn.execute("ALTER TABLE sessions ADD COLUMN context_window INTEGER")
        request_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(agent_requests)")}
        if "request_key" not in request_columns:
            self.conn.execute("ALTER TABLE agent_requests ADD COLUMN request_key TEXT")
            self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS agent_request_key ON agent_requests(request_key)")
        cron_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(cron_schedules)")}
        if "last_outcome" not in cron_columns:
            self.conn.execute("ALTER TABLE cron_schedules ADD COLUMN last_outcome TEXT")
        split_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(split_plans)")}
        if "source_key" not in split_columns:
            with self.transaction():
                self.conn.execute("""CREATE TABLE split_plans_v2 (
                    id TEXT PRIMARY KEY, original_id TEXT NOT NULL, source_key TEXT UNIQUE,
                    plan TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL)""")
                self.conn.execute("""INSERT INTO split_plans_v2 (id,original_id,plan,status,created_at)
                    SELECT id,original_id,plan,status,created_at FROM split_plans""")
                self.conn.execute("DROP TABLE split_plans")
                self.conn.execute("ALTER TABLE split_plans_v2 RENAME TO split_plans")
        self.conn.execute("CREATE INDEX IF NOT EXISTS split_plans_original ON split_plans(original_id,status)")

    @contextmanager
    def transaction(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def one(self, query, args=()):
        row = self.conn.execute(query, args).fetchone()
        return dict(row) if row else None

    def many(self, query, args=()):
        return [dict(row) for row in self.conn.execute(query, args).fetchall()]

    def backup(self, destination):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        try:
            copy = sqlite3.connect(destination)
            try:
                self.conn.backup(copy)
            finally:
                copy.close()
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        return destination

    def event(self, kind, origin, entity_id=None, payload=None, correlation_id=None, causation_id=None):
        event_id = uid()
        self.conn.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",
            (event_id, kind, origin, entity_id, correlation_id, causation_id,
             json.dumps(payload or {}), now()),
        )
        return event_id
