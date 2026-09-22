"""The three-hop pipeline: SupportAgent -> RefundAgent -> PaymentAPI.

Key design choice: SupportAgent delegates a token capped at the amount
found in *trusted* context only (the user's own words), never at whatever
its own brain "decided" (which can be manipulated by untrusted context).
RefundAgent's brain can still be fooled into requesting more — but
PaymentAPI enforces the token's cap regardless of what RefundAgent asks
for. This separates "what an agent was tricked into wanting" from "what it
actually had authority to do."
"""
from typing import Optional
from .capability import TokenAuthority, Token, AuthorityError
from .context import ContextStore
from .brains import Brain
from .eventlog import EventLog
from .utils import extract_refund_amount


class PaymentAPI:
    """The only place money actually moves — so the only place authority
    truly needs to be enforced."""

    def __init__(self, authority: TokenAuthority):
        self.authority = authority
        self.ledger = []

    def refund(self, run_id: str, log: EventLog, caller: str, token: Token, amount: float) -> dict:
        try:
            self.authority.authorize(token, subject=caller, scope="refund", amount=amount)
        except AuthorityError as e:
            result = {"status": "denied", "reason": str(e), "amount": amount}
            log.record(run_id, "payment_api", "denial", result)
            return result

        self.ledger.append({"caller": caller, "amount": amount})
        result = {"status": "executed", "amount": amount}
        log.record(run_id, "payment_api", "tool_result", result)
        return result


class SupportAgent:
    def __init__(self, authority: TokenAuthority, root_token: Token, brain: Brain):
        self.authority = authority
        self.root_token = root_token
        self.brain = brain
        self.id = "support_agent"

    def handle(self, run_id: str, log: EventLog, store: ContextStore, context_ids: list):
        items = store.snapshot(context_ids)
        log.record(run_id, self.id, "context_received", {}, context_refs=context_ids)

        decision = self.brain.decide("support", items)
        log.record(run_id, self.id, "decision", decision, context_refs=decision["used_context"])

        legitimate_amount = self._trusted_amount(items)
        delegated = self.authority.delegate(
            self.root_token, subject="refund_agent", scope="refund", max_amount=legitimate_amount
        )
        log.record(run_id, self.id, "delegate", {"subject": "refund_agent", "max_amount": legitimate_amount})
        return decision, delegated

    @staticmethod
    def _trusted_amount(items) -> float:
        for item in items.values():
            if item.source == "user_input" and item.trusted:
                amount = extract_refund_amount(item.as_text())
                if amount is not None:
                    return amount
        return 0.0


class RefundAgent:
    def __init__(self, authority: TokenAuthority, brain: Brain, payment_api: PaymentAPI):
        self.authority = authority
        self.brain = brain
        self.payment_api = payment_api
        self.id = "refund_agent"

    def handle(self, run_id: str, log: EventLog, store: ContextStore, context_ids: list,
               token: Token, forge_token: Optional[Token] = None) -> dict:
        items = store.snapshot(context_ids)
        log.record(run_id, self.id, "context_received", {}, context_refs=context_ids)

        decision = self.brain.decide("refund", items)
        log.record(run_id, self.id, "decision", decision, context_refs=decision["used_context"])

        amount = decision["amount"] or 0.0
        use_token = forge_token or token  # a compromised agent minting its own token
        log.record(run_id, self.id, "tool_call", {"amount": amount})
        return self.payment_api.refund(run_id, log, "refund_agent", use_token, amount)
