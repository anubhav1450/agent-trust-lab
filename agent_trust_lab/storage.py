"""Persistent run registry.

The EventLog already stores the trace (what happened) on disk. This module
adds the other half: which scenario produced a run, and the exact context
snapshot it used — so a run can be reloaded and replayed later from a fresh
process (e.g. the API server), not just within the run that created it.

It also holds recordings of LLM calls, keyed by a hash of the exact request,
so replaying an unchanged request reads the stored answer instead of asking
the model again.
"""
import sqlite3
import json
import time
from typing import Optional, List

from .context import ContextStore, ContextItem

RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    scenario TEXT NOT NULL,
    created_at REAL NOT NULL,
    context_ids TEXT NOT NULL,
    context_json TEXT NOT NULL,
    forge_attack INTEGER NOT NULL,
    final_status TEXT,
    final_amount REAL,
    brain TEXT NOT NULL DEFAULT 'rule'
);
"""

# One row per distinct model request. `request_json` is the exact request the
# hash was computed from (never the API key), kept so a recording can be
# checked against what it claims to answer.
LLM_CALLS_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_calls (
    request_hash TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    request_json TEXT NOT NULL,
    response TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


class RecordingError(Exception):
    """The LLM recording store could not be read or written."""


def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute(RUNS_SCHEMA)
    conn.execute(LLM_CALLS_SCHEMA)
    # Databases created before the `brain` column existed: add it once. Every
    # run in them was rule-based, which is what the column default says.
    if "brain" not in [row[1] for row in conn.execute("PRAGMA table_info(runs)")]:
        conn.execute("ALTER TABLE runs ADD COLUMN brain TEXT NOT NULL DEFAULT 'rule'")
    return conn


def get_llm_call(db_path: str, request_hash: str) -> Optional[str]:
    """The recorded raw response for this exact request, or None."""
    try:
        conn = _conn(db_path)
        try:
            row = conn.execute(
                "SELECT response FROM llm_calls WHERE request_hash=?", (request_hash,)
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise RecordingError(f"could not read LLM recording: {e}") from e
    return row[0] if row else None


def save_llm_call(db_path: str, request_hash: str, model: str, request_json: str, response: str) -> None:
    try:
        conn = _conn(db_path)
        try:
            # A recording is never overwritten: the first response stored for
            # a request is the one every later replay of it will see.
            conn.execute(
                "INSERT OR IGNORE INTO llm_calls (request_hash, model, request_json, response, created_at) "
                "VALUES (?,?,?,?,?)",
                (request_hash, model, request_json, response, time.time()),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise RecordingError(f"could not save LLM recording: {e}") from e


class LLMRecordings:
    """The two recording operations bound to one database, so a brain can use
    them without knowing where recordings are stored."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def get(self, request_hash: str) -> Optional[str]:
        return get_llm_call(self.db_path, request_hash)

    def save(self, request_hash: str, model: str, request_json: str, response: str) -> None:
        save_llm_call(self.db_path, request_hash, model, request_json, response)


def save_run(db_path: str, run_id: str, scenario: str, store: ContextStore, context_ids: List[str],
             forge_attack: bool, final_status: Optional[str], final_amount: Optional[float],
             brain: str = "rule") -> None:
    context_json = json.dumps({
        cid: {
            "source": store.get(cid).source,
            "trusted": store.get(cid).trusted,
            "content": store.get(cid).content,
        } for cid in context_ids
    })
    conn = _conn(db_path)
    conn.execute(
        "INSERT INTO runs (run_id, scenario, created_at, context_ids, context_json, forge_attack, "
        "final_status, final_amount, brain) VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, scenario, time.time(), json.dumps(context_ids), context_json,
         int(forge_attack), final_status, final_amount, brain),
    )
    conn.commit()
    conn.close()


def list_runs(db_path: str) -> List[dict]:
    conn = _conn(db_path)
    rows = conn.execute(
        "SELECT run_id, scenario, created_at, final_status, final_amount, forge_attack, brain "
        "FROM runs ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return [
        {
            "run_id": r[0], "scenario": r[1], "created_at": r[2],
            "final_status": r[3], "final_amount": r[4], "forge_attack": bool(r[5]), "brain": r[6],
        }
        for r in rows
    ]


def load_run_context(db_path: str, run_id: str) -> Optional[dict]:
    conn = _conn(db_path)
    row = conn.execute(
        "SELECT scenario, context_ids, context_json, forge_attack, brain FROM runs WHERE run_id=?", (run_id,)
    ).fetchone()
    conn.close()
    if row is None:
        return None
    scenario, context_ids_json, context_json, forge_attack, brain = row
    context_ids = json.loads(context_ids_json)
    raw_items = json.loads(context_json)
    store = ContextStore()
    for cid in context_ids:
        item = raw_items[cid]
        store.add(ContextItem(id=cid, source=item["source"], trusted=item["trusted"], content=item["content"]))
    return {"scenario": scenario, "store": store, "context_ids": context_ids,
            "forge_attack": bool(forge_attack), "brain": brain}
