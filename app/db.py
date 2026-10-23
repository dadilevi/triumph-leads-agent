"""SQLite storage (data/leads.db — git-ignored, stays local)."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

STATUSES = {
    "new": "חדש",
    "sent": "נשלח",
    "replied": "ענה",
    "meeting": "נקבעה פגישה",
    "handoff": "הועבר לנציג",
    "not_relevant": "לא רלוונטי",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    first_name TEXT NOT NULL DEFAULT '',
    last_name TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    phone TEXT,                -- normalized 9725XXXXXXXX, NULL if invalid
    phone_raw TEXT NOT NULL DEFAULT '',
    model_raw TEXT NOT NULL DEFAULT '',
    model_id TEXT,             -- matched knowledge model id
    status TEXT NOT NULL DEFAULT 'new',
    notes TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'excel',  -- excel / inbound / simulator
    pending_suggestion TEXT,   -- JSON of the latest draft awaiting a human (supervised mode)
    offered_slots TEXT,        -- JSON list of slots last offered to the customer
    agent_paused INTEGER NOT NULL DEFAULT 0,  -- 1 = a human owns this conversation
    paused_reason TEXT NOT NULL DEFAULT '',
    last_inbound_at TEXT,      -- for WhatsApp's 24h customer-service window
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS leads_phone ON leads(phone) WHERE phone IS NOT NULL;

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    direction TEXT NOT NULL CHECK (direction IN ('out', 'in')),
    author TEXT NOT NULL DEFAULT 'agent',  -- customer / agent / human
    text TEXT NOT NULL,
    wa_id TEXT,                -- WhatsApp message id (dedupe webhook retries)
    status TEXT NOT NULL DEFAULT 'sent',   -- sent / failed
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS messages_wa_id ON messages(wa_id) WHERE wa_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS meetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    calendar_event_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER REFERENCES leads(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,        -- meeting / handoff / error / human_took_over
    text TEXT NOT NULL,
    seen INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""

# columns added after the first release — added to older DBs on startup
MIGRATIONS = {
    "leads": {
        "source": "TEXT NOT NULL DEFAULT 'excel'",
        "agent_paused": "INTEGER NOT NULL DEFAULT 0",
        "paused_reason": "TEXT NOT NULL DEFAULT ''",
        "last_inbound_at": "TEXT",
    },
    "messages": {
        "author": "TEXT NOT NULL DEFAULT 'agent'",
        "wa_id": "TEXT",
        "status": "TEXT NOT NULL DEFAULT 'sent'",
    },
}


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()  # webhook processing runs in background threads
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._migrate_columns()
        self.conn.executescript(SCHEMA)

    def _migrate_columns(self) -> None:
        for table, cols in MIGRATIONS.items():
            existing = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if not existing:
                continue  # fresh DB — SCHEMA creates it
            for col, decl in cols.items():
                if col not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        self.conn.commit()

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def _all(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def _one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, args).fetchone()

    # ---- leads ---------------------------------------------------------
    def upsert_lead(self, *, first_name, last_name, email, phone, phone_raw, model_raw, model_id,
                    source: str = "excel") -> tuple[int, bool]:
        """Insert a lead; if the phone already exists, keep the existing lead. Returns (id, created)."""
        with self.lock:
            if phone:
                row = self._one("SELECT id FROM leads WHERE phone = ?", (phone,))
                if row:
                    return row["id"], False
            ts = now()
            cur = self._exec(
                """INSERT INTO leads (first_name, last_name, email, phone, phone_raw, model_raw, model_id,
                                      source, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)""",
                (first_name, last_name, email, phone, phone_raw, model_raw, model_id, source, ts, ts),
            )
            return cur.lastrowid, True

    def find_lead_by_phone(self, phone: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM leads WHERE phone = ?", (phone,))

    def list_leads(self, status: str | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM leads"
        args: tuple = ()
        if status:
            q += " WHERE status = ?"
            args = (status,)
        q += " ORDER BY CASE status WHEN 'handoff' THEN 0 WHEN 'replied' THEN 1 WHEN 'new' THEN 2 " \
             "WHEN 'sent' THEN 3 WHEN 'meeting' THEN 4 ELSE 5 END, updated_at DESC"
        return self._all(q, args)

    def get_lead(self, lead_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM leads WHERE id = ?", (lead_id,))

    def set_status(self, lead_id: int, status: str) -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown status {status}")
        self._exec("UPDATE leads SET status = ?, updated_at = ? WHERE id = ?", (status, now(), lead_id))

    def set_notes(self, lead_id: int, notes: str) -> None:
        self._exec("UPDATE leads SET notes = ?, updated_at = ? WHERE id = ?", (notes, now(), lead_id))

    def set_suggestion(self, lead_id: int, suggestion_json: str | None) -> None:
        self._exec("UPDATE leads SET pending_suggestion = ? WHERE id = ?", (suggestion_json, lead_id))

    def set_offered_slots(self, lead_id: int, slots_json: str | None) -> None:
        self._exec("UPDATE leads SET offered_slots = ? WHERE id = ?", (slots_json, lead_id))

    def set_paused(self, lead_id: int, paused: bool, reason: str = "") -> None:
        self._exec("UPDATE leads SET agent_paused = ?, paused_reason = ?, updated_at = ? WHERE id = ?",
                   (1 if paused else 0, reason if paused else "", now(), lead_id))

    def touch_inbound(self, lead_id: int) -> None:
        self._exec("UPDATE leads SET last_inbound_at = ? WHERE id = ?", (now(), lead_id))

    def status_counts(self) -> dict[str, int]:
        rows = self._all("SELECT status, COUNT(*) c FROM leads GROUP BY status")
        counts = {s: 0 for s in STATUSES}
        counts.update({r["status"]: r["c"] for r in rows})
        return counts

    # ---- messages ------------------------------------------------------
    def add_message(self, lead_id: int, direction: str, text: str, author: str | None = None,
                    wa_id: str | None = None, status: str = "sent") -> int | None:
        """Returns the new message id, or None if wa_id was already stored (duplicate webhook)."""
        author = author or ("customer" if direction == "in" else "agent")
        with self.lock:
            try:
                cur = self._exec(
                    "INSERT INTO messages (lead_id, direction, author, text, wa_id, status, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (lead_id, direction, author, text, wa_id, status, now()),
                )
            except sqlite3.IntegrityError:
                return None
            self._exec("UPDATE leads SET updated_at = ? WHERE id = ?", (now(), lead_id))
            return cur.lastrowid

    def has_wa_id(self, wa_id: str) -> bool:
        return self._one("SELECT 1 FROM messages WHERE wa_id = ?", (wa_id,)) is not None

    def messages(self, lead_id: int) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM messages WHERE lead_id = ? ORDER BY id", (lead_id,))

    # ---- meetings ------------------------------------------------------
    def add_meeting(self, lead_id: int, start: str, end: str, calendar_event_id: str | None) -> None:
        self._exec(
            "INSERT INTO meetings (lead_id, start, end, calendar_event_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (lead_id, start, end, calendar_event_id, now()),
        )

    def meetings(self, lead_id: int) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM meetings WHERE lead_id = ? ORDER BY start", (lead_id,))

    # ---- alerts --------------------------------------------------------
    def add_alert(self, lead_id: int | None, kind: str, text: str) -> None:
        self._exec("INSERT INTO alerts (lead_id, kind, text, created_at) VALUES (?, ?, ?, ?)",
                   (lead_id, kind, text, now()))

    def alerts(self, unseen_only: bool = False, limit: int = 50) -> list[sqlite3.Row]:
        q = "SELECT a.*, l.first_name, l.last_name FROM alerts a LEFT JOIN leads l ON l.id = a.lead_id"
        if unseen_only:
            q += " WHERE a.seen = 0"
        return self._all(q + " ORDER BY a.id DESC LIMIT ?", (limit,))

    def unseen_alerts_count(self) -> int:
        return self._one("SELECT COUNT(*) c FROM alerts WHERE seen = 0")["c"]

    def mark_alerts_seen(self) -> None:
        self._exec("UPDATE alerts SET seen = 1 WHERE seen = 0")
