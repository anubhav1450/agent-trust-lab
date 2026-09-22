"""Run every scenario and print a human-readable report.

No external services, no API keys, no cost: everything here is stdlib
Python + SQLite.
"""
from .orchestrator import Pipeline
from .sandbox import causal_report
from . import scenarios

ROOT_KEY = b"demo-root-key-do-not-use-in-prod"


def run_all():
    print("=" * 72)
    print("SCENARIO 1: benign refund")
    print("=" * 72)
    store, ids = scenarios.benign_refund()
    pipeline = Pipeline(ROOT_KEY)
    run_id = pipeline.run(store, ids)
    for e in pipeline.log.get_run(run_id):
        print(f"  [{e.agent_id:>13}] {e.event_type:<14} {e.payload}")
    print("  integrity check:", pipeline.log.verify_integrity(run_id))

    print()
    print("=" * 72)
    print("SCENARIO 2: prompt injection (user asked for 500, attacker wants 50000)")
    print("=" * 72)
    store, ids = scenarios.prompt_injection_attack()
    pipeline = Pipeline(ROOT_KEY)
    run_id = pipeline.run(store, ids)
    for e in pipeline.log.get_run(run_id):
        print(f"  [{e.agent_id:>13}] {e.event_type:<14} {e.payload}")
    print("  integrity check:", pipeline.log.verify_integrity(run_id))

    print()
    print("  -- causal analysis: which context item actually drove the outcome? --")
    report = causal_report(store, ids, ROOT_KEY)
    print("  baseline outcome:", report["baseline"])
    for cid, info in report["per_context"].items():
        tag = "CAUSALLY RELEVANT" if info["causally_relevant"] else "not relevant"
        print(f"    {cid}: {tag:<18} (without it -> {info['without_this_context']})")

    print()
    print("=" * 72)
    print("SCENARIO 3: compromised sub-agent forges its own token")
    print("=" * 72)
    store, ids = scenarios.compromised_subagent_forges_token()
    pipeline = Pipeline(ROOT_KEY)
    run_id = pipeline.run(store, ids, forge_attack=True)
    for e in pipeline.log.get_run(run_id):
        print(f"  [{e.agent_id:>13}] {e.event_type:<14} {e.payload}")

    print()
    print("=" * 72)
    print("SCENARIO 4: tamper-evident log detects state corruption")
    print("=" * 72)
    store, ids = scenarios.state_corruption_after_the_fact()
    pipeline = Pipeline(ROOT_KEY)
    run_id = pipeline.run(store, ids)
    print("  integrity before tampering:", pipeline.log.verify_integrity(run_id))
    pipeline.log.conn.execute(
        "UPDATE events SET payload=? WHERE run_id=? AND event_type='tool_call'",
        ('{"amount": 999999}', run_id),
    )
    pipeline.log.conn.commit()
    print("  integrity after tampering: ", pipeline.log.verify_integrity(run_id))


if __name__ == "__main__":
    run_all()
