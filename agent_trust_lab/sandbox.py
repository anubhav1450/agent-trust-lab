"""Sandboxed replay: re-execute a past run against recorded context, optionally
with individual context items neutralized, without ever touching the real
PaymentAPI ledger. This is what makes counterfactual analysis safe to run
against a "did this really happen" incident.
"""
from typing import Dict, Optional
from .context import ContextStore, ContextItem
from .eventlog import EventLog
from .agents import SupportAgent, RefundAgent, PaymentAPI
from .brains import RuleBasedBrain
from .capability import TokenAuthority


def replay(store: ContextStore, context_ids: list, root_key: bytes,
           override: Optional[Dict[str, Optional[str]]] = None) -> dict:
    """Re-runs the pipeline in an isolated sandbox (fresh authority, fresh
    ledger, fresh log). `override` maps a context_id to either replacement
    text, or None to remove that item entirely."""
    override = override or {}
    sandbox_store = ContextStore()
    for cid in context_ids:
        item = store.get(cid)
        if cid in override and override[cid] is None:
            continue
        content = override.get(cid, item.content)
        sandbox_store.add(ContextItem(id=cid, source=item.source, trusted=item.trusted, content=content))

    authority = TokenAuthority(root_key)
    log = EventLog()
    payment_api = PaymentAPI(authority)
    root_token = authority.issue_root("support_agent", scope="refund", max_amount=1_000_000)
    support = SupportAgent(authority, root_token, RuleBasedBrain())
    refund = RefundAgent(authority, RuleBasedBrain(), payment_api)

    live_ids = [c for c in context_ids if c in sandbox_store.all_ids()]
    _, token = support.handle("sandbox", log, sandbox_store, live_ids)
    return refund.handle("sandbox", log, sandbox_store, live_ids, token)


def _changed(baseline: dict, counterfactual: dict) -> bool:
    return (
        counterfactual.get("amount") != baseline.get("amount")
        or counterfactual.get("status") != baseline.get("status")
    )


def causal_report(store: ContextStore, context_ids: list, root_key: bytes) -> dict:
    """For each context item present in the original run, remove it in
    isolation and see whether the outcome changes. Items whose removal
    changes status/amount are causally relevant to the outcome — this is
    the counterfactual-intervention version of provenance, not a
    text-summarization one."""
    baseline = replay(store, context_ids, root_key)
    per_context = {}
    for cid in context_ids:
        counterfactual = replay(store, context_ids, root_key, override={cid: None})
        per_context[cid] = {
            "causally_relevant": _changed(baseline, counterfactual),
            "without_this_context": counterfactual,
        }
    return {"baseline": baseline, "per_context": per_context}


def redundant_pair_report(store: ContextStore, context_ids: list, root_key: bytes,
                           single: Optional[dict] = None) -> dict:
    """Leave-one-out is blind to redundant/OR-type causes: if two items are
    each independently sufficient, removing either one alone leaves the
    other in place and the outcome doesn't change, so neither gets flagged
    (see evaluation.py's 'redundant_sufficient_causes' case). This runs a
    bounded second pass: among items leave-one-out already cleared, test
    removing every PAIR together and see if the outcome changes when
    neither one did individually.

    This is deliberately not exhaustive over all 2^n subsets — it only
    covers pairs, and only among items already cleared individually. It
    catches redundant pairs; it does not claim to find arbitrary
    higher-order interactions. That's the honest scope of this pass."""
    single = single or causal_report(store, context_ids, root_key)
    baseline = single["baseline"]
    not_relevant_alone = [cid for cid in context_ids if not single["per_context"][cid]["causally_relevant"]]

    pairs = []
    for i, a in enumerate(not_relevant_alone):
        for b in not_relevant_alone[i + 1:]:
            counterfactual = replay(store, context_ids, root_key, override={a: None, b: None})
            if _changed(baseline, counterfactual):
                pairs.append({"pair": [a, b], "without_both": counterfactual})
    return {"baseline": baseline, "redundant_pairs": pairs}
