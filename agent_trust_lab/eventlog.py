"""Append-only, tamper-evident event log — the backbone of workflow replay.

Each event's hash is chained to the previous event's hash (same idea as a
blockchain's block-hash chain, minus consensus). If anyone edits a stored
event after the fact, recomputing the chain from scratch will no longer
match, which is exactly what `verify_integrity` checks.
"""
import sqlite3
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts REAL NOT NULL,
    agent_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    context_refs TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
"""


def _hash(prev_hash: str, run_id: str, ts: float, agent_id: str, event_type: str,
          payload: str, context_refs: str) -> str:
    h = hashlib.sha256()
    h.update(prev_hash.encode())
    h.update(json.dumps([run_id, ts, agent_id, event_type, payload, context_refs]).encode())
    return h.hexdigest()


@dataclass
class Event:
    seq: int
    run_id: str
    ts: float
    agent_id: str
    event_type: str
    payload: Any
    context_refs: List[str]
    prev_hash: str
    hash: str


class EventLog:
    GENESIS = "0" * 64

    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path, timeout=10)
        self.conn.execute(SCHEMA)
        self.conn.commit()

    def _last_hash(self, run_id: str) -> str:
        row = self.conn.execute(
            "SELECT hash FROM events WHERE run_id=? ORDER BY seq DESC LIMIT 1", (run_id,)
        ).fetchone()
        return row[0] if row else self.GENESIS

    def record(self, run_id: str, agent_id: str, event_type: str, payload: dict,
               context_refs: Optional[List[str]] = None) -> Event:
        context_refs = context_refs or []
        prev_hash = self._last_hash(run_id)
        ts = time.time()
        payload_json = json.dumps(payload, sort_keys=True)
        refs_json = json.dumps(context_refs)
        h = _hash(prev_hash, run_id, ts, agent_id, event_type, payload_json, refs_json)
        cur = self.conn.execute(
            "INSERT INTO events (run_id, ts, agent_id, event_type, payload, context_refs, prev_hash, hash) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (run_id, ts, agent_id, event_type, payload_json, refs_json, prev_hash, h),
        )
        self.conn.commit()
        return Event(cur.lastrowid, run_id, ts, agent_id, event_type, payload, context_refs, prev_hash, h)

    def get_run(self, run_id: str) -> List[Event]:
        rows = self.conn.execute(
            "SELECT seq, run_id, ts, agent_id, event_type, payload, context_refs, prev_hash, hash "
            "FROM events WHERE run_id=? ORDER BY seq ASC", (run_id,)
        ).fetchall()
        return [
            Event(seq, rid, ts, aid, et, json.loads(p), json.loads(cr), ph, h)
            for seq, rid, ts, aid, et, p, cr, ph, h in rows
        ]

    def verify_integrity(self, run_id: str) -> bool:
        """Recomputes the hash chain from stored rows. False if anything
        was edited after the fact (this is what 'state corruption after
        the fact' looks like from the log's point of view)."""
        prev = self.GENESIS
        for e in self.get_run(run_id):
            payload_json = json.dumps(e.payload, sort_keys=True)
            refs_json = json.dumps(e.context_refs)
            expected = _hash(prev, e.run_id, e.ts, e.agent_id, e.event_type, payload_json, refs_json)
            if expected != e.hash or e.prev_hash != prev:
                return False
            prev = e.hash
        return True
