"""Persistent run registry.

The EventLog already stores the trace (what happened) on disk. This module
adds the other half: which scenario produced a run, and the exact context
snapshot it used — so a run can be reloaded and replayed later from a fresh
process (e.g. the API server), not just within the run that created it.
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
    final_amount REAL
);
"""


def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute(RUNS_SCHEMA)
    return conn


def save_run(db_path: str, run_id: str, scenario: str, store: ContextStore, context_ids: List[str],
             forge_attack: bool, final_status: Optional[str], final_amount: Optional[float]) -> None:
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
        "final_status, final_amount) VALUES (?,?,?,?,?,?,?,?)",
        (run_id, scenario, time.time(), json.dumps(context_ids), context_json,
         int(forge_attack), final_status, final_amount),
    )
    conn.commit()
    conn.close()


def list_runs(db_path: str) -> List[dict]:
    conn = _conn(db_path)
    rows = conn.execute(
        "SELECT run_id, scenario, created_at, final_status, final_amount, forge_attack "
        "FROM runs ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return [
        {
            "run_id": r[0], "scenario": r[1], "created_at": r[2],
            "final_status": r[3], "final_amount": r[4], "forge_attack": bool(r[5]),
        }
        for r in rows
    ]


def load_run_context(db_path: str, run_id: str) -> Optional[dict]:
    conn = _conn(db_path)
    row = conn.execute(
        "SELECT scenario, context_ids, context_json, forge_attack FROM runs WHERE run_id=?", (run_id,)
    ).fetchone()
    conn.close()
    if row is None:
        return None
    scenario, context_ids_json, context_json, forge_attack = row
    context_ids = json.loads(context_ids_json)
    raw_items = json.loads(context_json)
    store = ContextStore()
    for cid in context_ids:
        item = raw_items[cid]
        store.add(ContextItem(id=cid, source=item["source"], trusted=item["trusted"], content=item["content"]))
    return {"scenario": scenario, "store": store, "context_ids": context_ids, "forge_attack": bool(forge_attack)}
