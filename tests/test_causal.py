import unittest
from agent_trust_lab.sandbox import causal_report, redundant_pair_report
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

    def test_leave_one_out_misses_redundant_causes(self):
        # documenting the known blind spot with an assertion, not just prose
        store, ids = scenarios.redundant_injection_attack()
        report = causal_report(store, ids, ROOT_KEY)
        self.assertFalse(report["per_context"]["c2"]["causally_relevant"])
        self.assertFalse(report["per_context"]["c3"]["causally_relevant"])

    def test_pairwise_intervention_catches_the_redundant_pair(self):
        store, ids = scenarios.redundant_injection_attack()
        report = redundant_pair_report(store, ids, ROOT_KEY)
        pairs = [set(p["pair"]) for p in report["redundant_pairs"]]
        self.assertIn({"c2", "c3"}, pairs)

    def test_pairwise_intervention_finds_nothing_when_there_is_no_redundancy(self):
        store, ids = scenarios.prompt_injection_attack()
        report = redundant_pair_report(store, ids, ROOT_KEY)
        self.assertEqual(report["redundant_pairs"], [])


if __name__ == "__main__":
    unittest.main()
