"""Minimal client for an OpenAI-compatible chat-completions endpoint.

Standard library only, so the engine keeps zero runtime dependencies. The
provider is configuration, not code: any service that speaks this API shape
can be used by setting the three ATL_LLM_* environment variables.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Optional

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
# Checked against the provider's public model and rate-limit pages on
# 2026-10-10: a production model that has free-plan limits. Model line-ups
# change (the previous free-tier defaults were shut down in Aug 2026), so
# override with ATL_LLM_MODEL rather than editing this when that happens.
DEFAULT_MODEL = "openai/gpt-oss-20b"

TEMPERATURE = 0
# The default model reasons before answering and that reasoning counts against
# this limit, so it cannot be sized to the ~40-token JSON reply alone.
MAX_COMPLETION_TOKENS = 1024
TIMEOUT_SECONDS = 20


class LLMError(Exception):
    """The model call itself failed: missing key, network, timeout, HTTP error
    or an unusable response envelope.

    Deliberately separate from "the model answered, but its decision was
    invalid", which LLMBrain handles by failing closed. Callers must be able
    to tell "the model said no" from "we never got an answer"."""


class UsageCapReached(LLMError):
    """The live-call budget is spent. No request was sent."""


class UsageCap:
    """A budget of live model calls per rolling window, for the public demo.

    Best effort only. The count lives in this process's memory: it resets on
    restart, and separate server instances each keep their own. It limits
    casual overuse of a shared free-tier key; it is not a spending guarantee.
    """

    def __init__(self, limit: int, window_seconds: float = 3600, clock=time.monotonic):
        self.limit = limit
        self._window = window_seconds
        self._clock = clock
        self._sent = deque()
        self._lock = threading.Lock()

    def spend(self) -> None:
        with self._lock:
            now = self._clock()
            while self._sent and now - self._sent[0] >= self._window:
                self._sent.popleft()
            if len(self._sent) >= self.limit:
                raise UsageCapReached(
                    f"LLM demo limit reached ({self.limit} live model calls per hour). "
                    "Use rule-based mode, or try again later."
                )
            self._sent.append(now)


class LLMClient:
    def __init__(self, api_key=None, base_url=None, model=None, timeout=TIMEOUT_SECONDS,
                 usage_cap: Optional[UsageCap] = None, deadline: Optional[float] = None):
        env = os.environ
        self._api_key = (env.get("ATL_LLM_API_KEY", "") if api_key is None else api_key).strip()
        self.base_url = (base_url or env.get("ATL_LLM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or env.get("ATL_LLM_MODEL") or DEFAULT_MODEL
        self.timeout = timeout
        self._usage_cap = usage_cap
        # A time.monotonic() value after which no further call is started, so
        # work that needs several calls cannot outlive the request it serves.
        self._deadline = deadline
        self.live_calls = 0

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def _body(self, messages: list) -> dict:
        return {
            "model": self.model,
            "messages": messages,
            "temperature": TEMPERATURE,
            "max_completion_tokens": MAX_COMPLETION_TOKENS,
            "response_format": {"type": "json_object"},
        }

    def request_identity(self, messages: list) -> dict:
        """Everything that determines the model's answer, and nothing secret:
        where the request goes plus the exact body `complete` would send.
        Recordings are keyed on this, so a recorded response is only reused
        for a request that is identical in every one of these settings."""
        return {"base_url": self.base_url, **self._body(messages)}

    def complete(self, messages: list) -> str:
        """Send chat messages, return the model's raw text reply."""
        if not self.is_configured():
            raise LLMError("no API key configured: set ATL_LLM_API_KEY")

        timeout = self.timeout
        if self._deadline is not None:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise LLMError("the time budget for this request is used up; no further model call was sent")
            timeout = min(timeout, remaining)
        # Checked here, at the one place a live request is sent: answers
        # served from recordings never reach this point and are not counted.
        if self._usage_cap is not None:
            self._usage_cap.spend()
        self.live_calls += 1

        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(self._body(messages)).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
                # The provider's edge answers urllib's default User-Agent
                # with HTTP 403 before the request reaches the API.
                "User-Agent": "agent-trust-lab",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            with e:
                detail = e.read().decode("utf-8", "replace")[:300].replace(self._api_key, "[redacted]")
            raise LLMError(f"model API returned HTTP {e.code}: {detail}") from e
        except OSError as e:  # URLError, timeouts, connection failures
            raise LLMError(f"could not reach model API: {e}") from e
        except ValueError as e:
            raise LLMError("model API returned a response that is not JSON") from e

        try:
            choice = payload["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError("model API response had an unexpected shape") from e

        # Happens when the reasoning budget is exhausted before any answer is
        # written. There is no decision to validate, so this is a failed call.
        if not isinstance(content, str) or not content.strip():
            raise LLMError(f"model returned no content (finish_reason={choice.get('finish_reason')!r})")
        return content
