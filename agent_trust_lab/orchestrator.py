"""Runs the full pipeline and returns a run_id you can inspect / replay."""
import uuid
from .capability import TokenAuthority, Token
from .context import ContextStore
from .eventlog import EventLog
from .agents import SupportAgent, RefundAgent, PaymentAPI
from .brains import RuleBasedBrain


class Pipeline:
    def __init__(self, root_key: bytes = b"demo-root-key-do-not-use-in-prod", db_path: str = ":memory:"):
        self.authority = TokenAuthority(root_key)
        self.log = EventLog(db_path)
        self.payment_api = PaymentAPI(self.authority)
        root_token = self.authority.issue_root("support_agent", scope="refund", max_amount=1_000_000)
        self.support_agent = SupportAgent(self.authority, root_token, RuleBasedBrain())
        self.refund_agent = RefundAgent(self.authority, RuleBasedBrain(), self.payment_api)

    def run(self, store: ContextStore, context_ids: list, forge_attack: bool = False) -> str:
        run_id = str(uuid.uuid4())
        self.log.record(run_id, "user", "context_received", {}, context_refs=context_ids)

        _, delegated_token = self.support_agent.handle(run_id, self.log, store, context_ids)

        forge_token = None
        if forge_attack:
            # a compromised refund_agent mints its own token without the signing key
            forge_token = Token(
                issuer="refund_agent", subject="refund_agent", scope="refund",
                max_amount=1_000_000, expires_at=delegated_token.expires_at,
                parent_sig=None, signature="forged" * 8,
            )

        result = self.refund_agent.handle(run_id, self.log, store, context_ids, delegated_token, forge_token)
        self.log.record(run_id, "pipeline", "final_result", result)
        return run_id
