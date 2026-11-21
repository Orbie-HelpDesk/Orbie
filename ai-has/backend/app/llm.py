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
- CHAT_MODEL_FALLBACK (optional): if the main model fails with 404, 429 (also a daily quota), 500 to
  504, a timeout or a connection error, even after its retries, that model gets one attempt. It gets
  at least FALLBACK_MIN_SECONDS even if the budget is used up, so total time is at most the budget
  plus one short attempt. It is not tried after 400, 401 or 403, which a different model would not
  fix. Embeddings have no fallback.
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
FALLBACK_KINDS = RETRYABLE | {"not_found", "quota"}
FALLBACK_MIN_SECONDS = 10.0
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


def _run(call, model: str, timeout: float, deadline: float, retry: bool = True) -> object:
    """Calls call(model, timeout), with retries unless retry is False. Raises _Failed when it gives up."""
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
        if not retry or kind not in RETRYABLE or attempt >= len(RETRY_DELAYS):
            raise failure from None
        delay = RETRY_DELAYS[attempt]
        if _clock() + delay >= deadline:
            raise failure from None  # no time left for another attempt
        _sleep(delay)
        attempt += 1


def chat(client, *, single_attempt: bool = False, **kwargs):
    """chat.completions.create with the configured model, timeout, retries and budget.

    single_attempt=True is for optional calls that have their own plan B: one try on the main
    model, no retries and no fallback model.
    """
    deadline = _clock() + settings.llm_call_budget_seconds

    def call(model, timeout):
        return client.chat.completions.create(model=model, timeout=timeout, **kwargs)

    model = settings.chat_model
    try:
        response = _run(call, model, settings.chat_timeout_seconds, deadline, retry=not single_attempt)
    except _Failed as failed:
        fallback = settings.chat_model_fallback.strip()
        if single_attempt or not fallback or fallback == model or failed.kind not in FALLBACK_KINDS:
            raise ProviderUnavailable(failed.kind, failed.status) from None
        logger.warning("Main model failed (kind=%s), trying fallback model=%s once", failed.kind, fallback)
        timeout = min(settings.chat_timeout_seconds, max(deadline - _clock(), FALLBACK_MIN_SECONDS))
        try:
            response = _run(call, fallback, timeout, _clock() + timeout, retry=False)
        except _Failed:
            raise ProviderUnavailable(failed.kind, failed.status) from None
        model = fallback
    logger.info("chat answered by model=%s", model)
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
