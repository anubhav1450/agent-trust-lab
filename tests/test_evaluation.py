import unittest
from agent_trust_lab.evaluation import run_benchmark


class TestEvaluation(unittest.TestCase):
    def _get(self, results, name):
        return next(r for r in results if r["scenario"] == name)

    def test_single_sufficient_cause_is_found_exactly(self):
        results = run_benchmark()
        r = self._get(results, "single_sufficient_cause")
        self.assertEqual(r["precision"], 1.0)
        self.assertEqual(r["recall"], 1.0)
        self.assertEqual(r["detected"], ["c2"])

    def test_redundant_sufficient_causes_are_both_missed(self):
        # This is the documented blind spot, not a bug: leave-one-out
        # cannot see a cause that's redundant with another cause.
        results = run_benchmark()
        r = self._get(results, "redundant_sufficient_causes")
        self.assertEqual(r["recall"], 0.0)
        self.assertEqual(r["detected"], [])
        self.assertEqual(r["false_negatives"], ["c2", "c3"])


if __name__ == "__main__":
    unittest.main()
