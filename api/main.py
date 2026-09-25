"""HTTP layer over the agent-trust-lab engine, for the dashboard.

Every endpoint here is a thin wrapper around functions that already exist
and are already unit-tested in agent_trust_lab/. This file adds no new
trust/replay/causal logic of its own — it only exposes it and persists runs
to a single SQLite file so they survive across requests and server restarts.
"""
import os
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent_trust_lab.orchestrator import Pipeline
from agent_trust_lab.eventlog import EventLog
from agent_trust_lab.sandbox import replay, causal_report, redundant_pair_report
from agent_trust_lab import scenarios, storage

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("AGENT_TRUST_LAB_DB", os.path.join(ROOT_DIR, "data", "lab.db"))
WEB_DIR = os.path.join(ROOT_DIR, "web")
ROOT_KEY = b"demo-root-key-do-not-use-in-prod"

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

app = FastAPI(title="Agent Trust Lab")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class NewRunRequest(BaseModel):
    scenario: str  # "benign" | "injection" | "forged_token" | "custom"
    user_amount: Optional[float] = 500
    injected_note: Optional[str] = None
    forge_attack: Optional[bool] = False


class ReplayRequest(BaseModel):
    overrides: Dict[str, Optional[str]] = {}


SCENARIO_CATALOG = [
    {"id": "benign", "label": "Benign refund",
     "description": "User asks for a refund. Nothing unusual happens."},
    {"id": "injection", "label": "Prompt injection",
     "description": "An untrusted note tries to push the refund from 500 to 50000."},
    {"id": "forged_token", "label": "Compromised sub-agent",
     "description": "The refund agent mints its own authority token, bypassing the signer."},
    {"id": "redundant", "label": "Redundant injection",
     "description": "Two independently-sufficient malicious notes. Leave-one-out misses both; "
                     "pairwise intervention catches them as a redundant pair."},
    {"id": "custom", "label": "Custom",
     "description": "Type your own request amount and an optional injected note."},
]


def _build_scenario(req: NewRunRequest):
    if req.scenario == "benign":
        return scenarios.benign_refund(), False
    if req.scenario == "injection":
        return scenarios.prompt_injection_attack(), False
    if req.scenario == "forged_token":
        return scenarios.compromised_subagent_forges_token(), True
    if req.scenario == "redundant":
        return scenarios.redundant_injection_attack(), False
    if req.scenario == "custom":
        return scenarios.custom_scenario(req.user_amount or 0, req.injected_note), bool(req.forge_attack)
    raise HTTPException(400, f"unknown scenario '{req.scenario}'")


def _event_to_dict(e):
    return {
        "seq": e.seq, "agent_id": e.agent_id, "event_type": e.event_type,
        "payload": e.payload, "context_refs": e.context_refs, "ts": e.ts,
    }


def _context_items(ctx):
    return [
        {
            "id": cid,
            "source": ctx["store"].get(cid).source,
            "trusted": ctx["store"].get(cid).trusted,
            "content": ctx["store"].get(cid).content,
        }
        for cid in ctx["context_ids"]
    ]


@app.get("/api/scenarios")
def list_scenarios():
    return SCENARIO_CATALOG


@app.post("/api/runs")
def create_run(req: NewRunRequest):
    (store, ids), forge_attack = _build_scenario(req)

    pipeline = Pipeline(ROOT_KEY, db_path=DB_PATH)
    run_id = pipeline.run(store, ids, forge_attack=forge_attack)

    final = [e for e in pipeline.log.get_run(run_id) if e.event_type == "final_result"][0]
    storage.save_run(DB_PATH, run_id, req.scenario, store, ids, forge_attack,
                      final.payload.get("status"), final.payload.get("amount"))
    return {"run_id": run_id}


@app.get("/api/runs")
def get_runs():
    return storage.list_runs(DB_PATH)


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    ctx = storage.load_run_context(DB_PATH, run_id)
    if ctx is None:
        raise HTTPException(404, "run not found")
    log = EventLog(DB_PATH)
    events = log.get_run(run_id)
    if not events:
        raise HTTPException(404, "run not found")
    return {
        "run_id": run_id,
        "scenario": ctx["scenario"],
        "integrity_ok": log.verify_integrity(run_id),
        "context_items": _context_items(ctx),
        "events": [_event_to_dict(e) for e in events],
    }


@app.post("/api/runs/{run_id}/replay")
def replay_run(run_id: str, req: ReplayRequest):
    ctx = storage.load_run_context(DB_PATH, run_id)
    if ctx is None:
        raise HTTPException(404, "run not found")
    baseline = replay(ctx["store"], ctx["context_ids"], ROOT_KEY)
    result = replay(ctx["store"], ctx["context_ids"], ROOT_KEY, override=req.overrides)
    changed = result.get("amount") != baseline.get("amount") or result.get("status") != baseline.get("status")
    return {"baseline": baseline, "result": result, "outcome_changed": changed}


@app.get("/api/runs/{run_id}/causal")
def causal_run(run_id: str):
    ctx = storage.load_run_context(DB_PATH, run_id)
    if ctx is None:
        raise HTTPException(404, "run not found")
    single = causal_report(ctx["store"], ctx["context_ids"], ROOT_KEY)
    pairwise = redundant_pair_report(ctx["store"], ctx["context_ids"], ROOT_KEY, single=single)
    return {**single, "redundant_pairs": pairwise["redundant_pairs"]}


@app.delete("/api/runs")
def wipe_runs():
    """Dev convenience: wipes the local demo database, not exposed in the UI nav."""
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    return {"status": "wiped"}


# Mounted last so it never shadows the /api/* routes above.
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
