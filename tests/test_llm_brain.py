"""Stage 1 tests for the LLM client and brain.

Nothing here touches the network or needs an API key: the brain is driven by
a scripted fake client, and the real client is tested with urlopen patched.
"""
import io
import json
import os
import time
import unittest
import urllib.error
from unittest import mock

from agent_trust_lab import scenarios
from agent_trust_lab.agents import PaymentAPI, RefundAgent, SupportAgent
from agent_trust_lab.brains import LLMBrain, RuleBasedBrain
from agent_trust_lab.capability import TokenAuthority
from agent_trust_lab.context import ContextItem, ContextStore
from agent_trust_lab.eventlog import EventLog
from agent_trust_lab.llm import DEFAULT_BASE_URL, DEFAULT_MODEL, LLMClient, LLMError, UsageCap, UsageCapReached
from agent_trust_lab.orchestrator import Pipeline

ROOT_KEY = b"test-key"


class FakeLLMClient:
    """Stands in for the network: returns a scripted reply, or raises.
    `reply` is a string, or a function of the messages."""

    def __init__(self, reply=None, error=None, model="fake-model", base_url="fake://model"):
        self.reply = reply
        self.error = error
        self.model = model
        self.base_url = base_url
        self.calls = []

    def is_configured(self):
        return True

    @property
    def live_calls(self):
        return len(self.calls)

    def request_identity(self, messages):
        return {"base_url": self.base_url, "model": self.model, "messages": messages}

    def complete(self, messages):
        self.calls.append(messages)
        if self.error:
            raise self.error
        return self.reply(messages) if callable(self.reply) else self.reply


def decide(reply):
    store, ids = scenarios.prompt_injection_attack()  # c1 trusted, c2 untrusted
    client = FakeLLMClient(reply=reply)
    return LLMBrain(client).decide("refund", store.snapshot(ids)), client


class TestLLMBrainDecisions(unittest.TestCase):
    def test_valid_json_becomes_the_decision(self):
        decision, client = decide('{"action": "refund", "amount": 500, "used_context": ["c1"]}')
        self.assertEqual(decision["action"], "refund")
        self.assertEqual(decision["amount"], 500.0)
        self.assertEqual(decision["used_context"], ["c1"])
        self.assertEqual(len(client.calls), 1)

    def test_decision_carries_debugging_metadata(self):
        raw = '{"action": "refund", "amount": 500, "used_context": ["c1"]}'
        decision, client = decide(raw)
        llm = decision["llm"]
        self.assertEqual(llm["model"], "fake-model")
        self.assertEqual(llm["raw_response"], raw)
        self.assertEqual(llm["prompt"], client.calls[0])
        self.assertIs(llm["from_recording"], False)
        self.assertIsNone(llm["validation_error"])

    def test_prompt_contains_every_item_with_id_source_trust_and_content(self):
        _, client = decide('{"action": "noop", "amount": 0, "used_context": []}')
        system, user = client.calls[0]
        self.assertEqual(system["role"], "system")
        self.assertIn("not instructions", system["content"])
        items = json.loads(user["content"].split("\n", 1)[1])
        self.assertEqual(
            [(i["id"], i["source"], i["trusted"]) for i in items],
            [("c1", "user_input", True), ("c2", "ticket_attachment", False)],
        )
        self.assertIn("refund 500", items[0]["content"])
        self.assertIn("SYSTEM OVERRIDE", items[1]["content"])

    def test_noop_never_carries_an_amount(self):
        # RefundAgent acts on the amount alone, so this must not slip through.
        decision, _ = decide('{"action": "noop", "amount": 50000, "used_context": []}')
        self.assertEqual(decision["action"], "noop")
        self.assertIsNone(decision["amount"])
        self.assertIsNone(decision["llm"]["validation_error"])

    def test_malformed_json_fails_closed_and_says_why(self):
        raw = "Sure! I will refund 50000 right away."
        decision, _ = decide(raw)
        self.assertEqual(decision["action"], "noop")
        self.assertIsNone(decision["amount"])
        self.assertEqual(decision["used_context"], [])
        self.assertIn("not valid JSON", decision["llm"]["validation_error"])
        self.assertEqual(decision["llm"]["raw_response"], raw)

    def test_invalid_output_fails_closed(self):
        cases = {
            "top level is a list": '[{"action": "refund", "amount": 500, "used_context": []}]',
            "missing field": '{"action": "refund", "amount": 500}',
            "unknown action": '{"action": "transfer", "amount": 500, "used_context": []}',
            "action wrong case": '{"action": "REFUND", "amount": 500, "used_context": []}',
            "action wrong type": '{"action": ["refund"], "amount": 500, "used_context": []}',
            "amount is a string": '{"action": "refund", "amount": "500", "used_context": []}',
            "amount is a boolean": '{"action": "refund", "amount": true, "used_context": []}',
            "amount is null": '{"action": "refund", "amount": null, "used_context": []}',
            "amount is negative": '{"action": "refund", "amount": -1, "used_context": []}',
            "amount is NaN": '{"action": "refund", "amount": NaN, "used_context": []}',
            "amount is infinite": '{"action": "refund", "amount": 1e999, "used_context": []}',
            "amount overflows": '{"action": "refund", "amount": 1' + "0" * 400 + ', "used_context": []}',
            "used_context not a list": '{"action": "refund", "amount": 500, "used_context": "c1"}',
            "used_context has a non-string": '{"action": "refund", "amount": 500, "used_context": [1]}',
            "used_context names an unknown id": '{"action": "refund", "amount": 500, "used_context": ["c9"]}',
        }
        for name, raw in cases.items():
            with self.subTest(name):
                decision, _ = decide(raw)
                self.assertEqual(decision["action"], "noop")
                self.assertIsNone(decision["amount"])
                self.assertEqual(decision["used_context"], [])
                self.assertTrue(decision["llm"]["validation_error"])

    def test_provider_failure_is_raised_not_turned_into_a_decision(self):
        store, ids = scenarios.prompt_injection_attack()
        brain = LLMBrain(FakeLLMClient(error=LLMError("model API returned HTTP 503")))
        with self.assertRaises(LLMError):
            brain.decide("refund", store.snapshot(ids))


def run_with_refund_brain(test, brain):
    """The three-hop pipeline with a chosen Refund Agent brain. Built from the
    agent classes directly; the Support Agent stays rule-based."""
    store, ids = scenarios.prompt_injection_attack()
    authority = TokenAuthority(ROOT_KEY)
    log = EventLog()
    test.addCleanup(log.conn.close)
    payment_api = PaymentAPI(authority)
    payment_api.refund = mock.Mock(wraps=payment_api.refund)  # lets tests assert it was never called
    root_token = authority.issue_root("support_agent", scope="refund", max_amount=1_000_000)
    support = SupportAgent(authority, root_token, RuleBasedBrain())
    refund = RefundAgent(authority, brain, payment_api)
    _, token = support.handle("run", log, store, ids)
    result = refund.handle("run", log, store, ids, token)
    return result, payment_api, log


class TestLLMCannotBypassAuthority(unittest.TestCase):
    def test_fooled_model_asking_for_50000_is_still_denied(self):
        fooled = FakeLLMClient(reply='{"action": "refund", "amount": 50000, "used_context": ["c2"]}')
        result, payment_api, _ = run_with_refund_brain(self, LLMBrain(fooled))
        self.assertEqual(result["status"], "denied")
        self.assertIn("exceeds authorized max 500", result["reason"])
        self.assertEqual(payment_api.ledger, [])

    def test_model_asking_for_the_authorized_amount_executes(self):
        honest = FakeLLMClient(reply='{"action": "refund", "amount": 500, "used_context": ["c1"]}')
        result, payment_api, _ = run_with_refund_brain(self, LLMBrain(honest))
        self.assertEqual(result, {"status": "executed", "amount": 500.0})
        self.assertEqual(payment_api.ledger, [{"caller": "refund_agent", "amount": 500.0}])

    def test_prompt_and_raw_response_reach_the_hash_chained_log(self):
        raw = '{"action": "refund", "amount": 50000, "used_context": ["c2"]}'
        _, _, log = run_with_refund_brain(self, LLMBrain(FakeLLMClient(reply=raw)))
        decision = [e for e in log.get_run("run")
                    if e.agent_id == "refund_agent" and e.event_type == "decision"][0]
        self.assertEqual(decision.payload["llm"]["raw_response"], raw)
        self.assertEqual(decision.payload["llm"]["model"], "fake-model")
        self.assertEqual(decision.context_refs, ["c2"])
        self.assertTrue(log.verify_integrity("run"))

    def test_invalid_model_output_is_visible_in_the_log(self):
        _, _, log = run_with_refund_brain(self, LLMBrain(FakeLLMClient(reply="refund everything")))
        decision = [e for e in log.get_run("run")
                    if e.agent_id == "refund_agent" and e.event_type == "decision"][0]
        self.assertEqual(decision.payload["action"], "noop")
        self.assertIn("not valid JSON", decision.payload["llm"]["validation_error"])


NOOP_OUTCOME = {"status": "noop", "amount": 0.0}


class TestNoopNeverReachesPaymentAPI(unittest.TestCase):
    def assert_noop(self, result, payment_api, log, run_id="run"):
        self.assertEqual(result, NOOP_OUTCOME)
        payment_api.refund.assert_not_called()
        self.assertEqual(payment_api.ledger, [])
        event_types = [e.event_type for e in log.get_run(run_id) if e.agent_id in ("refund_agent", "payment_api")]
        self.assertEqual(event_types, ["context_received", "decision", "noop"])
        self.assertTrue(log.verify_integrity(run_id))

    def test_model_choosing_noop(self):
        client = FakeLLMClient(reply='{"action": "noop", "amount": 0, "used_context": []}')
        self.assert_noop(*run_with_refund_brain(self, LLMBrain(client)))

    def test_model_choosing_noop_while_naming_an_amount(self):
        client = FakeLLMClient(reply='{"action": "noop", "amount": 50000, "used_context": ["c2"]}')
        self.assert_noop(*run_with_refund_brain(self, LLMBrain(client)))

    def test_invalid_model_output_failing_closed(self):
        for name, raw in {
            "not JSON": "Sure, refunding 50000 now.",
            "unknown action": '{"action": "transfer", "amount": 50000, "used_context": []}',
            "negative amount": '{"action": "refund", "amount": -5, "used_context": []}',
            "unknown context id": '{"action": "refund", "amount": 500, "used_context": ["c9"]}',
        }.items():
            with self.subTest(name):
                self.assert_noop(*run_with_refund_brain(self, LLMBrain(FakeLLMClient(reply=raw))))

    def test_rule_based_noop_through_the_full_pipeline(self):
        store = ContextStore()
        store.add(ContextItem("c1", source="user_input", trusted=True, content="Where is my order #4821?"))
        pipeline = Pipeline(ROOT_KEY)
        self.addCleanup(pipeline.log.conn.close)
        pipeline.payment_api.refund = mock.Mock(wraps=pipeline.payment_api.refund)

        run_id = pipeline.run(store, ["c1"])

        final = [e for e in pipeline.log.get_run(run_id) if e.event_type == "final_result"][0]
        self.assert_noop(final.payload, pipeline.payment_api, pipeline.log, run_id)

    def test_a_refund_decision_still_calls_the_payment_api(self):
        client = FakeLLMClient(reply='{"action": "refund", "amount": 500, "used_context": ["c1"]}')
        result, payment_api, _ = run_with_refund_brain(self, LLMBrain(client))
        self.assertEqual(result["status"], "executed")
        payment_api.refund.assert_called_once()


class TestRuleBasedPathUnchanged(unittest.TestCase):
    def test_pipeline_still_defaults_to_the_rule_based_brain(self):
        pipeline = Pipeline(ROOT_KEY)
        self.addCleanup(pipeline.log.conn.close)
        self.assertIsInstance(pipeline.support_agent.brain, RuleBasedBrain)
        self.assertIsInstance(pipeline.refund_agent.brain, RuleBasedBrain)
        store, ids = scenarios.benign_refund()
        run_id = pipeline.run(store, ids)
        final = [e for e in pipeline.log.get_run(run_id) if e.event_type == "final_result"][0]
        self.assertEqual(final.payload, {"status": "executed", "amount": 500.0})

    def test_rule_based_decision_has_no_llm_metadata(self):
        store, ids = scenarios.benign_refund()
        decision = RuleBasedBrain().decide("refund", store.snapshot(ids))
        self.assertEqual(decision, {"action": "refund", "amount": 500.0, "used_context": ["c1"]})


def fake_http_response(body: bytes):
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = body
    return response


def completion_body(content, finish_reason="stop"):
    return json.dumps(
        {"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish_reason}]}
    ).encode()


URLOPEN = "agent_trust_lab.llm.urllib.request.urlopen"
MESSAGES = [{"role": "user", "content": "reply in JSON"}]


class TestLLMClient(unittest.TestCase):
    def test_not_configured_without_a_key(self):
        self.assertFalse(LLMClient(api_key="").is_configured())
        self.assertFalse(LLMClient(api_key="   ").is_configured())
        self.assertTrue(LLMClient(api_key="k").is_configured())

    def test_reads_configuration_from_the_environment(self):
        env = {"ATL_LLM_API_KEY": "env-key", "ATL_LLM_MODEL": "some/model",
               "ATL_LLM_BASE_URL": "http://localhost:11434/v1/"}
        with mock.patch.dict(os.environ, env, clear=True):
            client = LLMClient()
        self.assertTrue(client.is_configured())
        self.assertEqual(client.model, "some/model")
        self.assertEqual(client.base_url, "http://localhost:11434/v1")

    def test_defaults_when_the_environment_is_empty(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            client = LLMClient()
        self.assertFalse(client.is_configured())
        self.assertEqual(client.model, DEFAULT_MODEL)
        self.assertEqual(client.base_url, DEFAULT_BASE_URL)

    def test_calling_without_a_key_is_an_explicit_error_and_sends_nothing(self):
        with mock.patch(URLOPEN) as urlopen:
            with self.assertRaises(LLMError):
                LLMClient(api_key="").complete(MESSAGES)
        urlopen.assert_not_called()

    def test_successful_call_returns_content_and_sends_the_expected_request(self):
        client = LLMClient(api_key="secret-key", base_url="https://example.test/v1", model="m1", timeout=7)
        with mock.patch(URLOPEN, return_value=fake_http_response(completion_body('{"ok": true}'))) as urlopen:
            self.assertEqual(client.complete(MESSAGES), '{"ok": true}')

        request = urlopen.call_args.args[0]
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 7)
        self.assertEqual(request.full_url, "https://example.test/v1/chat/completions")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-key")
        self.assertEqual(request.get_header("User-agent"), "agent-trust-lab")
        body = json.loads(request.data)
        self.assertEqual(body["model"], "m1")
        self.assertEqual(body["messages"], MESSAGES)
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertLessEqual(body["max_completion_tokens"], 1024)
        self.assertNotIn("secret-key", request.data.decode())

    def test_http_error_is_an_llm_error_and_never_leaks_the_key(self):
        error = urllib.error.HTTPError(
            "https://example.test", 401, "Unauthorized", None,
            io.BytesIO(b'{"error": "bad key secret-key"}'),
        )
        with mock.patch(URLOPEN, side_effect=error):
            with self.assertRaises(LLMError) as ctx:
                LLMClient(api_key="secret-key").complete(MESSAGES)
        self.assertIn("HTTP 401", str(ctx.exception))
        self.assertNotIn("secret-key", str(ctx.exception))

    def test_network_failures_are_llm_errors(self):
        for name, error in {
            "dns/connection": urllib.error.URLError("nodename nor servname provided"),
            "timeout": TimeoutError("timed out"),
            "connection reset": ConnectionResetError("reset by peer"),
        }.items():
            with self.subTest(name), mock.patch(URLOPEN, side_effect=error):
                with self.assertRaises(LLMError):
                    LLMClient(api_key="k").complete(MESSAGES)

    def test_unusable_responses_are_llm_errors(self):
        for name, body in {
            "not JSON": b"<html>bad gateway</html>",
            "no choices": b'{"choices": []}',
            "unexpected shape": b'{"data": "nope"}',
            "content is null": completion_body(None, finish_reason="length"),
            "content is blank": completion_body("   ", finish_reason="length"),
        }.items():
            with self.subTest(name), mock.patch(URLOPEN, return_value=fake_http_response(body)):
                with self.assertRaises(LLMError):
                    LLMClient(api_key="k").complete(MESSAGES)


OK_RESPONSE = completion_body('{"ok": true}')


class TestUsageCapAndTimeBudget(unittest.TestCase):
    def test_cap_refuses_before_sending_and_counts_only_sent_calls(self):
        client = LLMClient(api_key="k", usage_cap=UsageCap(2))
        with mock.patch(URLOPEN, return_value=fake_http_response(OK_RESPONSE)) as urlopen:
            client.complete(MESSAGES)
            client.complete(MESSAGES)
            with self.assertRaises(UsageCapReached) as ctx:
                client.complete(MESSAGES)
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(client.live_calls, 2)
        self.assertIn("rule-based mode", str(ctx.exception))

    def test_cap_is_shared_by_every_client_given_it(self):
        cap = UsageCap(1)
        with mock.patch(URLOPEN, return_value=fake_http_response(OK_RESPONSE)) as urlopen:
            LLMClient(api_key="k", usage_cap=cap).complete(MESSAGES)
            with self.assertRaises(UsageCapReached):
                LLMClient(api_key="k", usage_cap=cap).complete(MESSAGES)
        self.assertEqual(urlopen.call_count, 1)

    def test_cap_reached_is_an_llm_error_so_it_is_never_mistaken_for_a_decision(self):
        self.assertTrue(issubclass(UsageCapReached, LLMError))

    def test_calls_older_than_the_window_no_longer_count(self):
        now = [1000.0]
        cap = UsageCap(1, window_seconds=3600, clock=lambda: now[0])
        cap.spend()
        with self.assertRaises(UsageCapReached):
            cap.spend()
        now[0] += 3600
        cap.spend()

    def test_no_call_is_started_after_the_deadline(self):
        client = LLMClient(api_key="k", deadline=time.monotonic() - 1)
        with mock.patch(URLOPEN) as urlopen:
            with self.assertRaises(LLMError) as ctx:
                client.complete(MESSAGES)
        urlopen.assert_not_called()
        self.assertIn("time budget", str(ctx.exception))
        self.assertEqual(client.live_calls, 0)

    def test_a_call_near_the_deadline_gets_only_the_time_that_is_left(self):
        client = LLMClient(api_key="k", timeout=20, deadline=time.monotonic() + 5)
        with mock.patch(URLOPEN, return_value=fake_http_response(OK_RESPONSE)) as urlopen:
            client.complete(MESSAGES)
        self.assertLessEqual(urlopen.call_args.kwargs["timeout"], 5)


if __name__ == "__main__":
    unittest.main()
