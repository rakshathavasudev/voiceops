"""The agent platform's own durable state (SQLite here, Postgres in production).

This is separate from the system of record (the PMS). It holds what the agent
platform owns: conversation state shared across channels, the hash-chained audit
log, the durable write outbox, consent, contact attempts, escalations and traces.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from voiceops import clock

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations(
  id TEXT PRIMARY KEY, org_id TEXT, subject_type TEXT, subject_id TEXT, display_name TEXT,
  status TEXT DEFAULT 'active', purpose TEXT DEFAULT 'inbound',
  verified_until TEXT, verify_failures INTEGER DEFAULT 0, pending_confirmation TEXT,
  unhandled_turns INTEGER DEFAULT 0, turn_count INTEGER DEFAULT 0, last_channel TEXT,
  created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY, conversation_id TEXT, turn_id TEXT, channel TEXT, role TEXT, text TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, conversation_id TEXT, turn_id TEXT, kind TEXT, data TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY, ts TEXT, org_id TEXT, actor TEXT, action TEXT, target TEXT, details TEXT,
  idempotency_key TEXT, trace_id TEXT, outcome TEXT, prev_hash TEXT, hash TEXT);
CREATE TABLE IF NOT EXISTS pending_writes(
  id INTEGER PRIMARY KEY, idempotency_key TEXT UNIQUE, conversation_id TEXT, op TEXT, path TEXT, payload TEXT,
  status TEXT, attempts INTEGER DEFAULT 0, last_error TEXT, response TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS outbox(
  id INTEGER PRIMARY KEY, conversation_id TEXT, channel TEXT, to_addr TEXT, body TEXT, status TEXT,
  provider_id TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS escalations(
  id INTEGER PRIMARY KEY, conversation_id TEXT, category TEXT, reason TEXT, summary TEXT, status TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS consent(
  subject_id TEXT, channel TEXT, status TEXT, source TEXT, updated_at TEXT, PRIMARY KEY(subject_id, channel));
CREATE TABLE IF NOT EXISTS contact_attempts(
  id INTEGER PRIMARY KEY, subject_id TEXT, campaign TEXT, channel TEXT, decision TEXT, reason TEXT,
  conversation_id TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS spans(
  id INTEGER PRIMARY KEY, trace_id TEXT, span_id TEXT, parent_id TEXT, name TEXT, kind TEXT,
  start_ts TEXT, duration_ms REAL, status TEXT, attrs TEXT);
CREATE TABLE IF NOT EXISTS turns(
  turn_id TEXT PRIMARY KEY, conversation_id TEXT, channel TEXT, started_at TEXT, latency_ms REAL,
  llm_calls INTEGER, input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
  cost_usd REAL, model TEXT, escalated INTEGER, blocked_actions INTEGER, tool_errors INTEGER);
CREATE TABLE IF NOT EXISTS contact_index(address TEXT PRIMARY KEY, conversation_id TEXT);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);
CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id);
"""

# Consent seeded for the demo residents/prospects: (subject, channel, status)
SEED_CONSENT = [
    ("R-1001", "sms", "granted"), ("R-1001", "voice", "granted"), ("R-1001", "email", "granted"),
    ("R-1002", "sms", "granted"), ("R-1002", "voice", "granted"), ("R-1002", "email", "granted"),
    ("R-1003", "sms", "revoked"), ("R-1003", "voice", "granted"), ("R-1003", "email", "granted"),
    ("R-1004", "sms", "granted"), ("R-1004", "voice", "granted"),
    ("R-1005", "sms", "granted"), ("R-1005", "voice", "granted"),
    ("P-2001", "sms", "granted"), ("P-2001", "email", "granted"),
    ("P-2002", "sms", "granted"), ("P-2002", "email", "granted"),
]


def _now() -> str:
    return clock.now().isoformat()


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        if not self.q("SELECT 1 FROM consent LIMIT 1"):
            for subject, channel, status in SEED_CONSENT:
                self.set_consent(subject, channel, status, "seed")

    # ---- primitives ----

    def q(self, sql: str, *args) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def x(self, sql: str, *args) -> int:
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.lastrowid

    def reset(self) -> None:
        with self.lock:
            for (name,) in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                self.db.execute(f"DELETE FROM {name}")
            self.db.commit()
        for subject, channel, status in SEED_CONSENT:
            self.set_consent(subject, channel, status, "seed")

    # ---- conversations (one per subject + open case, shared by every channel) ----

    def open_conversation(self, org_id: str, subject_type: str, subject_id: str, display_name: str,
                          purpose: str = "inbound") -> dict:
        rows = self.q("SELECT * FROM conversations WHERE org_id=? AND subject_id=? AND status IN ('active','escalated') "
                      "ORDER BY created_at DESC LIMIT 1", org_id, subject_id)
        if rows:
            conv = rows[0]
            if purpose != "inbound" and conv["purpose"] == "inbound":
                self.update_conversation(conv["id"], purpose=purpose)
                conv["purpose"] = purpose
            return conv
        cid = "C-" + uuid.uuid4().hex[:10]
        self.x("INSERT INTO conversations(id, org_id, subject_type, subject_id, display_name, purpose, created_at, updated_at) "
               "VALUES (?,?,?,?,?,?,?,?)", cid, org_id, subject_type, subject_id, display_name, purpose, _now(), _now())
        return self.get_conversation(cid)

    def get_conversation(self, cid: str) -> dict:
        return self.q("SELECT * FROM conversations WHERE id=?", cid)[0]

    def update_conversation(self, cid: str, **fields: Any) -> None:
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE conversations SET {cols} WHERE id=?", *fields.values(), cid)

    def add_message(self, cid: str, turn_id: str | None, channel: str, role: str, text: str) -> None:
        self.x("INSERT INTO messages(conversation_id, turn_id, channel, role, text, created_at) VALUES (?,?,?,?,?,?)",
               cid, turn_id, channel, role, text, _now())

    def messages(self, cid: str, limit: int = 40) -> list[dict]:
        rows = self.q("SELECT * FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?", cid, limit)
        return rows[::-1]

    def add_event(self, cid: str, turn_id: str | None, kind: str, data: dict) -> None:
        self.x("INSERT INTO events(conversation_id, turn_id, kind, data, created_at) VALUES (?,?,?,?,?)",
               cid, turn_id, kind, json.dumps(data, default=str), _now())

    def events(self, cid: str, limit: int = 30) -> list[dict]:
        rows = self.q("SELECT * FROM events WHERE conversation_id=? ORDER BY id DESC LIMIT ?", cid, limit)
        return [{**r, "data": json.loads(r["data"])} for r in rows[::-1]]

    # ---- audit log: append-only, hash-chained so tampering is detectable ----

    def audit(self, org_id: str, actor: str, action: str, target: str, details: dict,
              outcome: str, idempotency_key: str | None = None, trace_id: str | None = None) -> None:
        with self.lock:
            prev = self.q("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1")
            prev_hash = prev[0]["hash"] if prev else "genesis"
            ts = _now()
            body = json.dumps([ts, org_id, actor, action, target, details, idempotency_key, trace_id, outcome],
                              sort_keys=True, default=str)
            h = hashlib.sha256((prev_hash + body).encode()).hexdigest()
            self.x("INSERT INTO audit_log(ts, org_id, actor, action, target, details, idempotency_key, trace_id, outcome, prev_hash, hash) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?)", ts, org_id, actor, action, target, json.dumps(details, default=str),
                   idempotency_key, trace_id, outcome, prev_hash, h)

    def verify_audit_chain(self) -> dict:
        prev_hash = "genesis"
        rows = self.q("SELECT * FROM audit_log ORDER BY id")
        for r in rows:
            body = json.dumps([r["ts"], r["org_id"], r["actor"], r["action"], r["target"], json.loads(r["details"]),
                               r["idempotency_key"], r["trace_id"], r["outcome"]], sort_keys=True, default=str)
            if r["prev_hash"] != prev_hash or hashlib.sha256((prev_hash + body).encode()).hexdigest() != r["hash"]:
                return {"valid": False, "broken_at": r["id"], "entries": len(rows)}
            prev_hash = r["hash"]
        return {"valid": True, "entries": len(rows)}

    # ---- consent ----

    def set_consent(self, subject_id: str, channel: str, status: str, source: str) -> None:
        self.x("INSERT INTO consent(subject_id, channel, status, source, updated_at) VALUES (?,?,?,?,?) "
               "ON CONFLICT(subject_id, channel) DO UPDATE SET status=excluded.status, source=excluded.source, updated_at=excluded.updated_at",
               subject_id, channel, status, source, _now())

    def consent(self, subject_id: str, channel: str) -> str:
        rows = self.q("SELECT status FROM consent WHERE subject_id=? AND channel=?", subject_id, channel)
        return rows[0]["status"] if rows else "unknown"
