"""Calls to the AI provider, with timeouts, retries and a clear failure.

Every chat and embedding call goes through here, so a slow or failing provider can neither hang a
request nor show a raw error to the user. The OpenAI client is built with max_retries=0: retrying
is done here only, so the waits do not multiply.

Rules
- Each attempt has a timeout (CHAT_TIMEOUT_SECONDS or EMBED_TIMEOUT_SECONDS), and one logical call
  has a total budget (LLM_CALL_BUDGET_SECONDS). An attempt never runs longer than the time left.
- Timeouts, connection errors, 429 and 500 to 504 are retried up to 2 times, after 1 s and 3 s.
- 400, 401, 403, 404 and any other error are never retried on the same model.
- A 429 that says the daily quota is used up is not retried either: waiting a few seconds will not help.
- If everything fails, ProviderUnavailable is raised. Its message is fixed, so nothing from the
  provider can reach a user or a log.

Logging: only the model name, a kind (timeout, rate_limit, ...), the status code and the error class
name. Never the error text, the response body, the URL or any key. The exception is raised with
"from None" so the original error (whose text can hold the response body) is not printed in a
traceback either.
"""
import logging
import re
import time

import openai

from .config import settings

logger = logging.getLogger(__name__)

RETRY_DELAYS = (1.0, 3.0)  # seconds to wait before retry 1 and retry 2
RETRY_AFTER_SECONDS = 10
BUSY_MESSAGE = "The AI service is busy right now. Please try again in a moment."

RETRYABLE = {"timeout", "connection", "rate_limit", "server"}
# Best effort: Gemini words a daily limit with "per day" or "daily" (for example RequestsPerDay).
DAILY_QUOTA = re.compile(r"per[\s_-]*day|daily", re.IGNORECASE)

# Replaced in tests so no real waiting happens.
_sleep = time.sleep
_clock = time.monotonic


class ProviderUnavailable(Exception):
    """The AI provider could not answer. The message is safe to show to a user."""

    def __init__(self, kind: str, status: int | None = None):
        super().__init__(BUSY_MESSAGE)
        self.kind = kind
        self.status = status


class _Failed(Exception):
    """One model failed. Carries only a kind and a status code."""

    def __init__(self, kind: str, status: int | None):
        super().__init__(kind)
        self.kind = kind
        self.status = status


def _mentions_daily_quota(exc: Exception) -> bool:
    """Reads the error text only to decide whether to retry. The text itself is never logged."""
    return DAILY_QUOTA.search(f"{exc} {getattr(exc, 'body', '')}") is not None


def classify(exc: Exception) -> tuple[str, int | None]:
    """A kind and a status code for an error from the OpenAI-compatible client."""
    if isinstance(exc, openai.APITimeoutError):
        return "timeout", None
    if isinstance(exc, openai.APIConnectionError):
        return "connection", None
    if isinstance(exc, openai.APIStatusError):
        status = exc.status_code
        if status == 429:
            return ("quota" if _mentions_daily_quota(exc) else "rate_limit"), status
        if status == 404:
            return "not_found", status
        if status == 400:
            return "bad_request", status
        if status in (401, 403):
            return "auth", status
        if 500 <= status <= 504:
            return "server", status
        return "other", status
    return "other", None


def _log_failure(model: str, kind: str, status: int | None, exc: Exception, attempt: int) -> None:
    logger.warning(
        "AI call failed: model=%s kind=%s status=%s error=%s attempt=%d",
        model, kind, status, type(exc).__name__, attempt,
    )


def _run(call, model: str, timeout: float, deadline: float) -> object:
    """Calls call(model, timeout) with retries. Raises _Failed when it gives up."""
    attempt = 0
    while True:
        remaining = deadline - _clock()
        if remaining <= 0:
            raise _Failed("timeout", None)
        try:
            return call(model, min(timeout, remaining))
        except Exception as exc:
            kind, status = classify(exc)
            _log_failure(model, kind, status, exc, attempt + 1)
            failure = _Failed(kind, status)
        if kind not in RETRYABLE or attempt >= len(RETRY_DELAYS):
            raise failure from None
        delay = RETRY_DELAYS[attempt]
        if _clock() + delay >= deadline:
            raise failure from None  # no time left for another attempt
        _sleep(delay)
        attempt += 1


def chat(client, **kwargs):
    """chat.completions.create with the configured model, timeout, retries and budget."""
    deadline = _clock() + settings.llm_call_budget_seconds

    def call(model, timeout):
        return client.chat.completions.create(model=model, timeout=timeout, **kwargs)

    try:
        response = _run(call, settings.chat_model, settings.chat_timeout_seconds, deadline)
    except _Failed as failed:
        raise ProviderUnavailable(failed.kind, failed.status) from None
    logger.info("chat answered by model=%s", settings.chat_model)
    return response


def embeddings(client, **kwargs):
    """embeddings.create with the configured model, timeout, retries and budget."""
    deadline = _clock() + settings.llm_call_budget_seconds

    def call(model, timeout):
        return client.embeddings.create(model=model, timeout=timeout, **kwargs)

    try:
        return _run(call, settings.embed_model, settings.embed_timeout_seconds, deadline)
    except _Failed as failed:
        raise ProviderUnavailable(failed.kind, failed.status) from None
