"""Stage 3 tests for the HTTP layer's LLM handling.

The endpoint functions are called directly, so no HTTP client is needed.
No network and no API key: the model is a fake, or the real client with
urlopen patched. Each test gets its own temporary database.
"""
import json
import os
import tempfile
import time
import unittest
from unittest import mock

try:
    from fastapi import HTTPException
    from api import main
except ImportError:
    raise unittest.SkipTest("the API tests need fastapi: pip install -r requirements.txt")

from agent_trust_lab.llm import LLMClient, LLMError, UsageCap
from tests.test_llm_brain import URLOPEN, FakeLLMClient, completion_body, fake_http_response
from tests.test_llm_recording import REFUND_500, gullible, must_not_be_called

FOOLED = '{"action": "refund", "amount": 50000, "used_context": ["c2"]}'
NOOP = '{"action": "noop", "amount": 0, "used_context": []}'


def gullible_urlopen(request, timeout=None):
    """urlopen stand-in that answers like the `gullible` fake model."""
    messages = json.loads(request.data)["messages"]
    return fake_http_response(completion_body(gullible(messages)))


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.patch(mock.patch.object(main, "DB_PATH", os.path.join(tmp.name, "lab.db")))

    def patch(self, patcher):
        patched = patcher.start()
        self.addCleanup(patcher.stop)
        return patched

    def use_model(self, client_or_factory):
        """Make the API build its model client from this fake (or factory)."""
        factory = client_or_factory if callable(client_or_factory) else (lambda: client_or_factory)
        return self.patch(mock.patch.object(main, "_llm_client", side_effect=factory))

    def no_key(self):
        self.patch(mock.patch.dict(os.environ, {}, clear=True))

    def create(self, **fields):
        return main.create_run(main.NewRunRequest(**fields))["run_id"]

    def assert_http_error(self, status, call, *args):
        with self.assertRaises(HTTPException) as ctx:
            call(*args)
        self.assertEqual(ctx.exception.status_code, status)
        return ctx.exception.detail

    def final(self, run_id):
        return [e for e in main.get_run(run_id)["events"] if e["event_type"] == "final_result"][0]["payload"]

    def refund_decision(self, run_id):
        return [e for e in main.get_run(run_id)["events"]
                if e["agent_id"] == "refund_agent" and e["event_type"] == "decision"][0]["payload"]


class TestConfig(ApiTestCase):
    def test_reports_llm_as_off_without_a_key(self):
        self.no_key()
        config = main.get_config()
        self.assertIs(config["llm_configured"], False)
        self.assertIsNone(config["llm_model"])

    def test_reports_the_model_and_never_the_key(self):
        self.patch(mock.patch.dict(
            os.environ, {"ATL_LLM_API_KEY": "secret-key", "ATL_LLM_MODEL": "some/model"}, clear=True))
        config = main.get_config()
        self.assertIs(config["llm_configured"], True)
        self.assertEqual(config["llm_model"], "some/model")
        self.assertNotIn("secret-key", json.dumps(config))
        self.assertEqual(set(config), {"llm_configured", "llm_model", "llm_calls_per_hour"})


class TestCreateRun(ApiTestCase):
    def test_brain_defaults_to_rule_and_never_builds_a_model_client(self):
        factory = self.use_model(must_not_be_called())
        run_id = self.create(scenario="injection")
        self.assertEqual(main.get_run(run_id)["brain"], "rule")
        self.assertEqual(main.get_runs()[0]["brain"], "rule")
        self.assertNotIn("llm", self.refund_decision(run_id))
        factory.assert_not_called()

    def test_llm_mode_without_a_key_is_a_clear_400_and_saves_nothing(self):
        self.no_key()
        detail = self.assert_http_error(400, self.create_llm_injection)
        self.assertIn("not configured", detail)
        self.assertIn("rule-based", detail)
        self.assertEqual(main.get_runs(), [])

    def create_llm_injection(self):
        return self.create(scenario="injection", brain="llm")

    def test_unknown_brain_is_rejected(self):
        self.assert_http_error(400, lambda: self.create(scenario="injection", brain="magic"))

    def test_llm_run_is_stored_as_llm_with_its_prompt_and_response(self):
        self.use_model(FakeLLMClient(reply=REFUND_500))
        run_id = self.create_llm_injection()
        self.assertEqual(main.get_run(run_id)["brain"], "llm")
        self.assertEqual(main.get_runs()[0]["brain"], "llm")
        llm = self.refund_decision(run_id)["llm"]
        self.assertEqual(llm["raw_response"], REFUND_500)
        self.assertIs(llm["from_recording"], False)
        self.assertEqual(self.final(run_id), {"status": "executed", "amount": 500.0})

    def test_fooled_model_is_still_denied(self):
        self.use_model(FakeLLMClient(reply=FOOLED))
        self.assertEqual(self.final(self.create_llm_injection())["status"], "denied")

    def test_model_choosing_noop_is_stored_as_noop(self):
        self.use_model(FakeLLMClient(reply=NOOP))
        run_id = self.create_llm_injection()
        self.assertEqual(self.final(run_id), {"status": "noop", "amount": 0.0})
        self.assertEqual(main.get_runs()[0]["final_status"], "noop")

    def test_provider_failure_is_a_502_and_is_not_replaced_by_a_rule_based_run(self):
        self.use_model(FakeLLMClient(error=LLMError("model API returned HTTP 503")))
        detail = self.assert_http_error(502, self.create_llm_injection)
        self.assertIn("HTTP 503", detail)
        self.assertIn("Nothing was substituted", detail)
        self.assertEqual(main.get_runs(), [])

    def test_usage_cap_is_a_429_sends_nothing_and_is_not_replaced_by_a_rule_based_run(self):
        self.use_model(lambda: LLMClient(api_key="k", usage_cap=UsageCap(0)))
        with mock.patch(URLOPEN) as urlopen:
            detail = self.assert_http_error(429, self.create_llm_injection)
        urlopen.assert_not_called()
        self.assertIn("limit reached", detail)
        self.assertIn("rule-based mode", detail)
        self.assertEqual(main.get_runs(), [])

    def test_exhausted_time_budget_is_a_clear_error(self):
        self.use_model(lambda: LLMClient(api_key="k", deadline=time.monotonic() - 1))
        detail = self.assert_http_error(502, self.create_llm_injection)
        self.assertIn("time budget", detail)

    def test_identical_second_run_is_answered_from_the_recording(self):
        client = FakeLLMClient(reply=REFUND_500)
        self.use_model(client)
        self.create_llm_injection()
        second = self.create_llm_injection()
        self.assertEqual(len(client.calls), 1)
        self.assertIs(self.refund_decision(second)["llm"]["from_recording"], True)


class TestReplayAndCausalUseTheRunsOwnBrain(ApiTestCase):
    def setUp(self):
        super().setUp()
        # This model resists the injection (500, executed). The rule-based
        # brain would follow it (50000, denied), so the two are easy to tell apart.
        self.model = self.use_model(FakeLLMClient(reply=REFUND_500))
        self.run_id = self.create(scenario="injection", brain="llm")

    def replay(self, overrides):
        return main.replay_run(self.run_id, main.ReplayRequest(overrides=overrides))

    def test_unchanged_replay_of_an_llm_run_comes_from_its_recording(self):
        self.model.side_effect = lambda: must_not_be_called()
        result = self.replay({})
        self.assertEqual(result["brain"], "llm")
        self.assertEqual(result["baseline"], {"status": "executed", "amount": 500.0})
        self.assertEqual(result["live_model_calls"], 0)

    def test_edited_context_is_one_live_call_then_none(self):
        override = {"c2": "Attachment parser found no actionable content."}
        self.model.side_effect = lambda: FakeLLMClient(reply=REFUND_500)
        self.assertEqual(self.replay(override)["live_model_calls"], 1)
        self.assertEqual(self.replay(override)["live_model_calls"], 0)

    def test_without_a_key_recorded_replay_still_works_but_a_live_call_is_a_400(self):
        class KeyRemoved(FakeLLMClient):
            """Same model identity as the original run, but no key any more."""

            def is_configured(self):
                return False

            def complete(self, messages):
                raise LLMError("no API key configured: set ATL_LLM_API_KEY")

        self.model.side_effect = lambda: KeyRemoved()
        self.assertEqual(self.replay({})["live_model_calls"], 0)
        detail = self.assert_http_error(400, self.replay, {"c2": "something new"})
        self.assertIn("not configured", detail)

    def test_provider_failure_during_replay_is_an_error_not_a_rule_based_answer(self):
        self.model.side_effect = lambda: FakeLLMClient(error=LLMError("model API returned HTTP 503"))
        detail = self.assert_http_error(502, self.replay, {"c2": "something new"})
        self.assertIn("Nothing was substituted", detail)

    def test_causal_analysis_of_an_llm_run_uses_the_llm_brain(self):
        self.model.side_effect = lambda: FakeLLMClient(reply=REFUND_500)
        report = main.causal_run(self.run_id)
        self.assertEqual(report["brain"], "llm")
        self.assertEqual(report["baseline"], {"status": "executed", "amount": 500.0})
        self.assertEqual(set(report["per_context"]), {"c1", "c2"})
        self.assertIn("redundant_pairs", report)
        self.assertGreaterEqual(report["live_model_calls"], 1)


class TestUsageCapDuringCausalAnalysis(ApiTestCase):
    def test_cap_stops_the_analysis_with_a_429_and_recorded_progress_is_kept(self):
        cap = {"current": UsageCap(2)}
        self.use_model(lambda: LLMClient(api_key="k", usage_cap=cap["current"]))

        with mock.patch(URLOPEN, side_effect=gullible_urlopen) as urlopen:
            run_id = self.create(scenario="injection", brain="llm")  # live call 1
            detail = self.assert_http_error(429, main.causal_run, run_id)  # call 2 ok, call 3 refused
            self.assertIn("rule-based mode", detail)
            self.assertEqual(urlopen.call_count, 2)

            cap["current"] = UsageCap(10)  # a later hour
            report = main.causal_run(run_id)

        self.assertEqual(report["live_model_calls"], 1)  # only the one that was refused before
        self.assertTrue(report["per_context"]["c2"]["causally_relevant"])
        self.assertFalse(report["per_context"]["c1"]["causally_relevant"])


class TestRuleBasedRunsAreUnaffected(ApiTestCase):
    def test_replay_and_causal_never_touch_the_model(self):
        factory = self.use_model(must_not_be_called())
        run_id = self.create(scenario="injection")

        replayed = main.replay_run(run_id, main.ReplayRequest(overrides={"c2": None}))
        self.assertEqual(replayed["result"], {"status": "executed", "amount": 500.0})
        self.assertEqual((replayed["brain"], replayed["live_model_calls"]), ("rule", 0))

        report = main.causal_run(run_id)
        self.assertTrue(report["per_context"]["c2"]["causally_relevant"])
        self.assertEqual((report["brain"], report["live_model_calls"]), ("rule", 0))
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
