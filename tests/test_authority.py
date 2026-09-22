import unittest
from agent_trust_lab.orchestrator import Pipeline
from agent_trust_lab import scenarios

ROOT_KEY = b"test-key"


class TestAuthority(unittest.TestCase):
    def test_benign_refund_executes(self):
        store, ids = scenarios.benign_refund()
        p = Pipeline(ROOT_KEY)
        run_id = p.run(store, ids)
        final = [e for e in p.log.get_run(run_id) if e.event_type == "final_result"][0]
        self.assertEqual(final.payload["status"], "executed")
        self.assertEqual(final.payload["amount"], 500.0)

    def test_prompt_injection_is_blocked_by_authority_layer(self):
        store, ids = scenarios.prompt_injection_attack()
        p = Pipeline(ROOT_KEY)
        run_id = p.run(store, ids)
        final = [e for e in p.log.get_run(run_id) if e.event_type == "final_result"][0]
        # the agent's own decision was manipulated to 50000, but the delegated
        # token was capped at the trusted amount (500), so the API must deny it.
        self.assertEqual(final.payload["status"], "denied")
        self.assertEqual(p.payment_api.ledger, [])

    def test_forged_token_from_compromised_subagent_is_rejected(self):
        store, ids = scenarios.compromised_subagent_forges_token()
        p = Pipeline(ROOT_KEY)
        run_id = p.run(store, ids, forge_attack=True)
        final = [e for e in p.log.get_run(run_id) if e.event_type == "final_result"][0]
        self.assertEqual(final.payload["status"], "denied")
        self.assertIn("signature", final.payload["reason"])


if __name__ == "__main__":
    unittest.main()
