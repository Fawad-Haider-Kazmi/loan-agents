"""SQLite persistence: cases, audit trail, agent messages, feedback, blacklist.

One long-lived connection per thread: safe for the workflow and the Flask dashboard
threads, and far faster than reconnecting per write (closing the last connection
forces a WAL checkpoint, which made every audit entry cost ~1 ms of disk work).
Swap for PostgreSQL by re-implementing this class.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

from . import serialization
from .models import AgentMessage
from .state import WorkflowContext
from .utils import now_iso, normalize_id

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    application_id TEXT PRIMARY KEY,
    national_id    TEXT NOT NULL,
    account_number TEXT NOT NULL,
    status         TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    payload        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_app_national ON applications(national_id);
CREATE INDEX IF NOT EXISTS idx_app_account ON applications(account_number);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id TEXT, ts TEXT, actor TEXT, event TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_app ON audit_log(application_id);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id TEXT, ts TEXT, sender TEXT, recipient TEXT, kind TEXT, summary TEXT
);
CREATE INDEX IF NOT EXISTS idx_msg_app ON messages(application_id);
CREATE TABLE IF NOT EXISTS feedback (
    application_id TEXT PRIMARY KEY,
    features TEXT NOT NULL, predicted_pd REAL NOT NULL,
    defaulted INTEGER NOT NULL, approved_amount REAL NOT NULL, recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS blacklist (national_id TEXT PRIMARY KEY, reason TEXT);
"""


class Repository:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._local = threading.local()
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # -------------------------------------------------------------------- cases
    def exists(self, application_id: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM applications WHERE application_id=?",
                             (application_id,)).fetchone() is not None

    def save_context(self, ctx: WorkflowContext) -> None:
        app = ctx.application
        payload = serialization.to_json(ctx)
        now = now_iso()
        with self._conn() as c:
            c.execute(
                """INSERT INTO applications
                   (application_id, national_id, account_number, status, created_at, updated_at, payload)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(application_id) DO UPDATE SET
                     status=excluded.status, updated_at=excluded.updated_at, payload=excluded.payload""",
                (app.application_id, normalize_id(app.national_id), app.account_number,
                 ctx.status.value, now, now, payload))

    def load_context(self, application_id: str) -> WorkflowContext:
        with self._conn() as c:
            row = c.execute("SELECT payload FROM applications WHERE application_id=?",
                            (application_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown application {application_id}")
        return serialization.from_json(WorkflowContext, row["payload"])

    def list_contexts(self, status: Optional[str] = None) -> list[WorkflowContext]:
        q, args = "SELECT payload FROM applications", ()
        if status:
            q, args = q + " WHERE status=?", (status,)
        with self._conn() as c:
            rows = c.execute(q + " ORDER BY created_at, application_id", args).fetchall()
        return [serialization.from_json(WorkflowContext, r["payload"]) for r in rows]

    # ------------------------------------------------------------ fraud look-ups
    def recent_application_count(self, national_id: str, days: int, exclude_id: str) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        with self._conn() as c:
            rows = c.execute(
                "SELECT created_at FROM applications WHERE national_id=? AND application_id<>?",
                (normalize_id(national_id), exclude_id)).fetchall()
        return sum(1 for r in rows if datetime.fromisoformat(r["created_at"]) >= cutoff)

    def other_applicants_on_account(self, account_number: str, national_id: str) -> int:
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(DISTINCT national_id) n FROM applications "
                "WHERE account_number=? AND national_id<>?",
                (account_number, normalize_id(national_id))).fetchone()
        return int(row["n"])

    def add_to_blacklist(self, national_id: str, reason: str = "") -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO blacklist VALUES (?,?)",
                      (normalize_id(national_id), reason))

    def is_blacklisted(self, national_id: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM blacklist WHERE national_id=?",
                             (normalize_id(national_id),)).fetchone() is not None

    # --------------------------------------------------------- audit and messages
    def add_audit(self, application_id: str, actor: str, event: str, detail: str = "") -> None:
        with self._conn() as c:
            c.execute("INSERT INTO audit_log (application_id, ts, actor, event, detail) VALUES (?,?,?,?,?)",
                      (application_id, now_iso(), actor, event, detail))

    def get_audit(self, application_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT ts, actor, event, detail FROM audit_log "
                             "WHERE application_id=? ORDER BY id", (application_id,)).fetchall()
        return [dict(r) for r in rows]

    def add_message(self, m: AgentMessage) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO messages (application_id, ts, sender, recipient, kind, summary) "
                      "VALUES (?,?,?,?,?,?)",
                      (m.application_id, m.timestamp or now_iso(), m.sender, m.recipient, m.kind, m.summary))

    def get_messages(self, application_id: str) -> list[AgentMessage]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM messages WHERE application_id=? ORDER BY id",
                             (application_id,)).fetchall()
        return [AgentMessage(r["application_id"], r["sender"], r["recipient"], r["kind"],
                             r["summary"], r["ts"]) for r in rows]

    # ------------------------------------------------------------------ feedback
    def add_feedback(self, application_id: str, features: dict, predicted_pd: float,
                     defaulted: bool, approved_amount: float) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO feedback VALUES (?,?,?,?,?,?)",
                      (application_id, json.dumps(features), predicted_pd, int(defaulted),
                       approved_amount, now_iso()))

    def get_feedback(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM feedback ORDER BY recorded_at, application_id").fetchall()
        return [{"application_id": r["application_id"], "features": json.loads(r["features"]),
                 "predicted_pd": r["predicted_pd"], "defaulted": bool(r["defaulted"]),
                 "approved_amount": r["approved_amount"]} for r in rows]
