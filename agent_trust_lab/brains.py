"""Decision-making backends.

The default is a free, fully deterministic rule-based brain, for two
reasons: it costs nothing and it makes replay exact (a real LLM call is
non-deterministic, which is a genuine hard problem for replay systems, not
something to paper over). Swap in a real LLM later behind this same
interface — the rest of the system (authority, replay, causal analysis)
doesn't need to change.

The rule-based brain is deliberately naive: it treats any instruction-shaped
text as a valid instruction, whether or not the context it came from is
trusted. That's what makes the injection scenario meaningful — the *brain*
gets fooled, and it's the authority layer's job to stop the damage anyway.
"""
from typing import Dict
from .context import ContextItem
from .utils import extract_refund_amount


class Brain:
    def decide(self, role: str, context_items: Dict[str, ContextItem]) -> dict:
        raise NotImplementedError


class RuleBasedBrain(Brain):
    def decide(self, role: str, context_items: Dict[str, ContextItem]) -> dict:
        requested_amount = None
        source_ids = []
        for cid, item in context_items.items():
            amount = extract_refund_amount(item.as_text())
            if amount is not None:
                requested_amount = amount  # last mention wins, regardless of trust
                source_ids.append(cid)
        return {
            "action": "refund" if requested_amount is not None else "noop",
            "amount": requested_amount,
            "used_context": source_ids,
        }
