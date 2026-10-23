"""SQLite storage (data/leads.db — git-ignored, stays on the Mac)."""
from __future__ import annotations

import sqlite3
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
    pending_suggestion TEXT,   -- JSON of the latest draft awaiting the salesperson
    offered_slots TEXT,        -- JSON list of slots last offered to the customer
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS leads_phone ON leads(phone) WHERE phone IS NOT NULL;

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    direction TEXT NOT NULL CHECK (direction IN ('out', 'in')),
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    calendar_event_id TEXT,
    created_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    # ---- leads ---------------------------------------------------------
    def upsert_lead(self, *, first_name, last_name, email, phone, phone_raw, model_raw, model_id) -> tuple[int, bool]:
        """Insert a lead; if the phone already exists, keep the existing lead. Returns (id, created)."""
        if phone:
            row = self.conn.execute("SELECT id FROM leads WHERE phone = ?", (phone,)).fetchone()
            if row:
                return row["id"], False
        ts = now()
        cur = self.conn.execute(
            """INSERT INTO leads (first_name, last_name, email, phone, phone_raw, model_raw, model_id,
                                  status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)""",
            (first_name, last_name, email, phone, phone_raw, model_raw, model_id, ts, ts),
        )
        self.conn.commit()
        return cur.lastrowid, True

    def list_leads(self, status: str | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM leads"
        args: tuple = ()
        if status:
            q += " WHERE status = ?"
            args = (status,)
        q += " ORDER BY CASE status WHEN 'replied' THEN 0 WHEN 'handoff' THEN 1 WHEN 'new' THEN 2 " \
             "WHEN 'sent' THEN 3 WHEN 'meeting' THEN 4 ELSE 5 END, updated_at DESC"
        return self.conn.execute(q, args).fetchall()

    def get_lead(self, lead_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()

    def set_status(self, lead_id: int, status: str) -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown status {status}")
        self.conn.execute("UPDATE leads SET status = ?, updated_at = ? WHERE id = ?", (status, now(), lead_id))
        self.conn.commit()

    def set_notes(self, lead_id: int, notes: str) -> None:
        self.conn.execute("UPDATE leads SET notes = ?, updated_at = ? WHERE id = ?", (notes, now(), lead_id))
        self.conn.commit()

    def status_counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) c FROM leads GROUP BY status").fetchall()
        counts = {s: 0 for s in STATUSES}
        counts.update({r["status"]: r["c"] for r in rows})
        return counts

    def set_suggestion(self, lead_id: int, suggestion_json: str | None) -> None:
        self.conn.execute("UPDATE leads SET pending_suggestion = ? WHERE id = ?", (suggestion_json, lead_id))
        self.conn.commit()

    def set_offered_slots(self, lead_id: int, slots_json: str | None) -> None:
        self.conn.execute("UPDATE leads SET offered_slots = ? WHERE id = ?", (slots_json, lead_id))
        self.conn.commit()

    # ---- messages ------------------------------------------------------
    def add_message(self, lead_id: int, direction: str, text: str) -> None:
        self.conn.execute(
            "INSERT INTO messages (lead_id, direction, text, created_at) VALUES (?, ?, ?, ?)",
            (lead_id, direction, text, now()),
        )
        self.conn.execute("UPDATE leads SET updated_at = ? WHERE id = ?", (now(), lead_id))
        self.conn.commit()

    def messages(self, lead_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM messages WHERE lead_id = ? ORDER BY id", (lead_id,)
        ).fetchall()

    # ---- meetings ------------------------------------------------------
    def add_meeting(self, lead_id: int, start: str, end: str, calendar_event_id: str | None) -> None:
        self.conn.execute(
            "INSERT INTO meetings (lead_id, start, end, calendar_event_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (lead_id, start, end, calendar_event_id, now()),
        )
        self.conn.commit()

    def meetings(self, lead_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM meetings WHERE lead_id = ? ORDER BY start", (lead_id,)).fetchall()
