"""Stage 2 tests: recorded LLM calls, replay with a chosen brain, and the
stored brain mode. No network and no API key; recordings go to a temporary
SQLite file.
"""
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from agent_trust_lab import scenarios, storage
from agent_trust_lab.brains import LLMBrain, RuleBasedBrain, brain_for_mode
from agent_trust_lab.llm import LLMClient, LLMError
from agent_trust_lab.orchestrator import Pipeline
from agent_trust_lab.sandbox import causal_report, redundant_pair_report, replay
from agent_trust_lab.storage import LLMRecordings, RecordingError
from agent_trust_lab.utils import extract_refund_amount
from tests.test_llm_brain import URLOPEN, FakeLLMClient, completion_body, fake_http_response

ROOT_KEY = b"test-key"
REFUND_500 = '{"action": "refund", "amount": 500, "used_context": ["c1"]}'


def gullible(messages):
    """A fake model that follows the last refund amount it reads, trusted or
    not, so interventions on the context change its answer."""
    items = json.loads(messages[1]["content"].split("\n", 1)[1])
    amount, used = None, []
    for item in items:
        found = extract_refund_amount(item["content"])
        if found is not None:
            amount, used = found, [item["id"]]
    if amount is None:
        return '{"action": "noop", "amount": 0, "used_context": []}'
    return json.dumps({"action": "refund", "amount": amount, "used_context": used})


def must_not_be_called():
    return FakeLLMClient(error=AssertionError("the model client was called"))


class RecordingTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = os.path.join(tmp.name, "lab.db")
        self.recordings = LLMRecordings(self.db_path)

    def rows(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute("SELECT request_hash, model, request_json, response FROM llm_calls").fetchall()
        finally:
            conn.close()


def injection_context():
    store, ids = scenarios.prompt_injection_attack()
    return store.snapshot(ids)


def benign_context():
    store, ids = scenarios.benign_refund()
    return store.snapshot(ids)


class TestRecordingBehaviour(RecordingTestCase):
    def test_first_call_is_live_and_recorded_then_reused(self):
        client = FakeLLMClient(reply=REFUND_500)
        brain = LLMBrain(client, self.recordings)

        first = brain.decide("refund", injection_context())
        second = brain.decide("refund", injection_context())

        self.assertEqual(len(client.calls), 1)
        self.assertIs(first["llm"]["from_recording"], False)
        self.assertIs(second["llm"]["from_recording"], True)
        for field in ("action", "amount", "used_context"):
            self.assertEqual(first[field], second[field])
        self.assertEqual(second["llm"]["raw_response"], REFUND_500)

    def test_matching_recording_avoids_the_client_entirely(self):
        LLMBrain(FakeLLMClient(reply=REFUND_500), self.recordings).decide("refund", injection_context())

        client = must_not_be_called()
        decision = LLMBrain(client, self.recordings).decide("refund", injection_context())

        self.assertEqual(client.calls, [])
        self.assertIs(decision["llm"]["from_recording"], True)
        self.assertEqual(decision["amount"], 500.0)

    def test_changed_context_is_a_miss_one_live_call_then_reused(self):
        client = FakeLLMClient(reply=REFUND_500)
        brain = LLMBrain(client, self.recordings)
        brain.decide("refund", injection_context())
        self.assertEqual(len(client.calls), 1)

        changed = brain.decide("refund", benign_context())
        self.assertEqual(len(client.calls), 2)
        self.assertIs(changed["llm"]["from_recording"], False)

        again = brain.decide("refund", benign_context())
        self.assertEqual(len(client.calls), 2)
        self.assertIs(again["llm"]["from_recording"], True)

    def test_a_different_model_or_endpoint_does_not_reuse_a_recording(self):
        LLMBrain(FakeLLMClient(reply=REFUND_500, model="m1"), self.recordings).decide("refund", injection_context())

        for name, client in {
            "other model": FakeLLMClient(reply=REFUND_500, model="m2"),
            "other endpoint": FakeLLMClient(reply=REFUND_500, model="m1", base_url="fake://elsewhere"),
        }.items():
            with self.subTest(name):
                decision = LLMBrain(client, self.recordings).decide("refund", injection_context())
                self.assertEqual(len(client.calls), 1)
                self.assertIs(decision["llm"]["from_recording"], False)

    def test_invalid_output_is_recorded_and_replays_as_the_same_failure(self):
        raw = "Sure, refunding 50000 now."
        first = LLMBrain(FakeLLMClient(reply=raw), self.recordings).decide("refund", injection_context())
        replayed = LLMBrain(must_not_be_called(), self.recordings).decide("refund", injection_context())

        for decision in (first, replayed):
            self.assertEqual(decision["action"], "noop")
            self.assertIsNone(decision["amount"])
            self.assertEqual(decision["llm"]["raw_response"], raw)
            self.assertIn("not valid JSON", decision["llm"]["validation_error"])
        self.assertIs(replayed["llm"]["from_recording"], True)

    def test_provider_failure_records_nothing(self):
        brain = LLMBrain(FakeLLMClient(error=LLMError("HTTP 503")), self.recordings)
        with self.assertRaises(LLMError):
            brain.decide("refund", injection_context())
        self.assertEqual(self.rows(), [])

    def test_recording_is_keyed_by_the_hash_reported_in_the_decision(self):
        decision = LLMBrain(FakeLLMClient(reply=REFUND_500), self.recordings).decide("refund", injection_context())
        (request_hash, model, request_json, response), = self.rows()
        self.assertEqual(request_hash, decision["llm"]["request_hash"])
        self.assertEqual(model, "fake-model")
        self.assertEqual(response, REFUND_500)
        self.assertEqual(json.loads(request_json)["messages"], decision["llm"]["prompt"])

    def test_a_recording_is_never_overwritten(self):
        self.recordings.save("h1", "m", "{}", "first")
        self.recordings.save("h1", "m", "{}", "second")
        self.assertEqual(self.recordings.get("h1"), "first")
        self.assertIsNone(self.recordings.get("unknown"))


class TestRecordingFailuresAreExplicit(RecordingTestCase):
    def test_unreadable_store_raises_before_any_model_call(self):
        broken = LLMRecordings(os.path.join(os.path.dirname(self.db_path), "no-such-dir", "lab.db"))
        client = FakeLLMClient(reply=REFUND_500)
        with self.assertRaises(RecordingError):
            LLMBrain(client, broken).decide("refund", injection_context())
        self.assertEqual(client.calls, [])

    def test_failed_save_is_raised_not_swallowed(self):
        class SaveFails:
            def get(self, request_hash):
                return None

            def save(self, *args):
                raise RecordingError("could not save LLM recording: disk full")

        with self.assertRaises(RecordingError):
            LLMBrain(FakeLLMClient(reply=REFUND_500), SaveFails()).decide("refund", injection_context())


class TestRecordingKeyWithTheRealClient(RecordingTestCase):
    def setUp(self):
        super().setUp()
        self.client = LLMClient(api_key="secret-key", base_url="https://example.test/v1", model="m1")

    def decide(self):
        return LLMBrain(self.client, self.recordings).decide("refund", injection_context())

    def test_stored_request_has_every_setting_and_never_the_api_key(self):
        with mock.patch(URLOPEN, return_value=fake_http_response(completion_body(REFUND_500))):
            self.decide()

        (request_hash, model, request_json, response), = self.rows()
        self.assertNotIn("secret-key", request_hash + model + request_json + response)
        stored = json.loads(request_json)
        self.assertEqual(stored["base_url"], "https://example.test/v1")
        self.assertEqual(stored["model"], "m1")
        self.assertEqual(stored["temperature"], 0)
        self.assertEqual(stored["response_format"], {"type": "json_object"})
        for field in ("messages", "max_completion_tokens", "decision_schema"):
            self.assertIn(field, stored)

    def test_identical_request_is_sent_once(self):
        with mock.patch(URLOPEN, return_value=fake_http_response(completion_body(REFUND_500))) as urlopen:
            self.decide()
            second = self.decide()
        self.assertEqual(urlopen.call_count, 1)
        self.assertIs(second["llm"]["from_recording"], True)

    def test_changing_a_generation_setting_does_not_reuse_the_recording(self):
        with mock.patch(URLOPEN, return_value=fake_http_response(completion_body(REFUND_500))) as urlopen:
            self.decide()
            with mock.patch("agent_trust_lab.llm.TEMPERATURE", 0.7):
                changed = self.decide()
        self.assertEqual(urlopen.call_count, 2)
        self.assertIs(changed["llm"]["from_recording"], False)


class TestReplayWithRecordings(RecordingTestCase):
    def setUp(self):
        super().setUp()
        self.client = FakeLLMClient(reply=gullible)
        self.brain = LLMBrain(self.client, self.recordings)

    def test_replaying_an_unchanged_run_reuses_its_recording(self):
        store, ids = scenarios.prompt_injection_attack()
        pipeline = Pipeline(ROOT_KEY, refund_brain=self.brain)
        self.addCleanup(pipeline.log.conn.close)
        run_id = pipeline.run(store, ids)
        original = [e for e in pipeline.log.get_run(run_id) if e.event_type == "final_result"][0].payload
        self.assertEqual(len(self.client.calls), 1)

        first = replay(store, ids, ROOT_KEY, refund_brain=self.brain)
        second = replay(store, ids, ROOT_KEY, refund_brain=self.brain)

        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(first, original)
        self.assertEqual(second, original)
        self.assertEqual(original["status"], "denied")

    def test_an_edited_context_item_causes_exactly_one_new_live_call(self):
        store, ids = scenarios.prompt_injection_attack()
        replay(store, ids, ROOT_KEY, refund_brain=self.brain)
        self.assertEqual(len(self.client.calls), 1)

        override = {"c2": "Attachment parser found no actionable content."}
        changed = replay(store, ids, ROOT_KEY, override=override, refund_brain=self.brain)
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(changed, {"status": "executed", "amount": 500.0})

        replay(store, ids, ROOT_KEY, override=override, refund_brain=self.brain)
        self.assertEqual(len(self.client.calls), 2)

    def test_causal_report_uses_the_given_brain_and_records_each_distinct_request(self):
        store, ids = scenarios.prompt_injection_attack()
        report = causal_report(store, ids, ROOT_KEY, refund_brain=self.brain)
        self.assertTrue(report["per_context"]["c2"]["causally_relevant"])
        self.assertFalse(report["per_context"]["c1"]["causally_relevant"])
        self.assertEqual(len(self.client.calls), 3)  # baseline, without c1, without c2

        causal_report(store, ids, ROOT_KEY, refund_brain=self.brain)
        self.assertEqual(len(self.client.calls), 3)

    def test_redundant_pair_report_uses_the_given_brain(self):
        store, ids = scenarios.redundant_injection_attack()
        report = redundant_pair_report(store, ids, ROOT_KEY, refund_brain=self.brain)
        self.assertEqual([p["pair"] for p in report["redundant_pairs"]], [["c2", "c3"]])
        self.assertEqual(len(self.client.calls), 7)  # baseline + 3 single removals + 3 pairs

        redundant_pair_report(store, ids, ROOT_KEY, refund_brain=self.brain)
        self.assertEqual(len(self.client.calls), 7)

    def test_rule_based_replay_is_unchanged_when_no_brain_is_given(self):
        store, ids = scenarios.prompt_injection_attack()
        result = replay(store, ids, ROOT_KEY)
        self.assertEqual(result["status"], "denied")
        self.assertEqual(result["amount"], 50000.0)
        self.assertEqual(self.client.calls, [])


OLD_RUNS_SCHEMA = """
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY, scenario TEXT NOT NULL, created_at REAL NOT NULL,
    context_ids TEXT NOT NULL, context_json TEXT NOT NULL, forge_attack INTEGER NOT NULL,
    final_status TEXT, final_amount REAL
);
"""


class TestBrainModePersistence(RecordingTestCase):
    def save(self, run_id, **kwargs):
        store, ids = scenarios.prompt_injection_attack()
        storage.save_run(self.db_path, run_id, "injection", store, ids, False, "denied", 50000.0, **kwargs)

    def test_brain_mode_survives_save_and_load(self):
        self.save("r-llm", brain="llm")
        self.assertEqual(storage.load_run_context(self.db_path, "r-llm")["brain"], "llm")
        self.assertEqual(storage.list_runs(self.db_path)[0]["brain"], "llm")

    def test_brain_mode_defaults_to_rule(self):
        self.save("r-default")
        self.assertEqual(storage.load_run_context(self.db_path, "r-default")["brain"], "rule")

    def test_database_from_before_the_column_is_migrated_and_its_runs_are_rule_based(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(OLD_RUNS_SCHEMA)
        conn.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?)",
            ("old-run", "benign", 1.0, '["c1"]',
             '{"c1": {"source": "user_input", "trusted": true, "content": "Please refund 500."}}',
             0, "executed", 500.0),
        )
        conn.commit()
        conn.close()

        old = storage.load_run_context(self.db_path, "old-run")
        self.assertEqual(old["brain"], "rule")
        self.assertEqual(old["context_ids"], ["c1"])

        self.save("new-run", brain="llm")  # the migrated table accepts new rows
        modes = {r["run_id"]: r["brain"] for r in storage.list_runs(self.db_path)}
        self.assertEqual(modes, {"old-run": "rule", "new-run": "llm"})

    def test_brain_for_mode(self):
        self.assertIsInstance(brain_for_mode("rule"), RuleBasedBrain)
        llm_brain = LLMBrain(FakeLLMClient(reply=REFUND_500))
        self.assertIs(brain_for_mode("llm", llm_brain), llm_brain)
        with self.assertRaises(ValueError):
            brain_for_mode("llm")  # never falls back to rule-based
        with self.assertRaises(ValueError):
            brain_for_mode("something-else")

    def test_stored_mode_decides_which_brain_replays_the_run(self):
        # This model resists the injection, so its answer (500, executed)
        # differs from what the rule-based brain would do (50000, denied).
        store, ids = scenarios.prompt_injection_attack()
        pipeline = Pipeline(ROOT_KEY, refund_brain=LLMBrain(FakeLLMClient(reply=REFUND_500), self.recordings))
        self.addCleanup(pipeline.log.conn.close)
        run_id = pipeline.run(store, ids)
        storage.save_run(self.db_path, run_id, "injection", store, ids, False, "executed", 500.0, brain="llm")

        ctx = storage.load_run_context(self.db_path, run_id)
        self.assertEqual(ctx["brain"], "llm")
        with self.assertRaises(ValueError):
            brain_for_mode(ctx["brain"])

        brain = brain_for_mode(ctx["brain"], LLMBrain(must_not_be_called(), self.recordings))
        result = replay(ctx["store"], ctx["context_ids"], ROOT_KEY, refund_brain=brain)
        self.assertEqual(result, {"status": "executed", "amount": 500.0})
        self.assertNotEqual(result, replay(ctx["store"], ctx["context_ids"], ROOT_KEY))


if __name__ == "__main__":
    unittest.main()
