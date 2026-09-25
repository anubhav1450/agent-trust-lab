import unittest
from agent_trust_lab.evaluation import run_benchmark


class TestEvaluation(unittest.TestCase):
    def _get(self, results, name):
        return next(r for r in results if r["scenario"] == name)

    def test_single_sufficient_cause_is_found_exactly(self):
        results = run_benchmark()
        r = self._get(results, "single_sufficient_cause")
        lo = r["leave_one_out"]
        self.assertEqual(lo["precision"], 1.0)
        self.assertEqual(lo["recall"], 1.0)
        self.assertEqual(lo["detected"], ["c2"])
        # pairwise shouldn't need to add anything here, and shouldn't hurt
        lop = r["leave_one_out_plus_pairwise"]
        self.assertEqual(lop["recall"], 1.0)
        self.assertEqual(lop["redundant_pairs"], [])

    def test_leave_one_out_misses_redundant_causes(self):
        # This is the documented blind spot, not a bug: leave-one-out
        # cannot see a cause that's redundant with another cause.
        results = run_benchmark()
        r = self._get(results, "redundant_sufficient_causes")
        lo = r["leave_one_out"]
        self.assertEqual(lo["recall"], 0.0)
        self.assertEqual(lo["detected"], [])
        self.assertEqual(lo["false_negatives"], ["c2", "c3"])

    def test_pairwise_intervention_recovers_the_redundant_causes(self):
        results = run_benchmark()
        r = self._get(results, "redundant_sufficient_causes")
        lop = r["leave_one_out_plus_pairwise"]
        self.assertEqual(lop["recall"], 1.0)
        self.assertEqual(lop["detected"], ["c2", "c3"])
        self.assertEqual(lop["redundant_pairs"], [["c2", "c3"]])


if __name__ == "__main__":
    unittest.main()
