import unittest
from agent_trust_lab.orchestrator import Pipeline
from agent_trust_lab.sandbox import replay
from agent_trust_lab import scenarios

ROOT_KEY = b"test-key"


class TestReplay(unittest.TestCase):
    def test_replay_is_deterministic(self):
        store, ids = scenarios.benign_refund()
        p = Pipeline(ROOT_KEY)
        p.run(store, ids)
        r1 = replay(store, ids, ROOT_KEY)
        r2 = replay(store, ids, ROOT_KEY)
        self.assertEqual(r1, r2)

    def test_tamper_evident_log_detects_corruption(self):
        store, ids = scenarios.state_corruption_after_the_fact()
        p = Pipeline(ROOT_KEY)
        run_id = p.run(store, ids)
        self.assertTrue(p.log.verify_integrity(run_id))
        p.log.conn.execute(
            "UPDATE events SET payload=? WHERE run_id=? AND event_type='tool_call'",
            ('{"amount": 999999}', run_id),
        )
        p.log.conn.commit()
        self.assertFalse(p.log.verify_integrity(run_id))


if __name__ == "__main__":
    unittest.main()
