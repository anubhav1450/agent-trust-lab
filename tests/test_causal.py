import unittest
from agent_trust_lab.sandbox import causal_report
from agent_trust_lab import scenarios

ROOT_KEY = b"test-key"


class TestCausal(unittest.TestCase):
    def test_causal_analysis_identifies_injected_context_as_the_cause(self):
        store, ids = scenarios.prompt_injection_attack()
        report = causal_report(store, ids, ROOT_KEY)
        # c2 is the malicious attachment note: removing it should flip the outcome.
        self.assertTrue(report["per_context"]["c2"]["causally_relevant"])
        # c1 is the legitimate request: removing it should NOT change the
        # (already-bad) outcome, since it wasn't the cause of the bad decision.
        self.assertFalse(report["per_context"]["c1"]["causally_relevant"])


if __name__ == "__main__":
    unittest.main()
