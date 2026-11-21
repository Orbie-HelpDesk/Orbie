import logging
import traceback
from types import SimpleNamespace

import httpx
import openai
import pytest
from fastapi.testclient import TestClient

from app import ingest, llm, main, rag
from app.config import settings
from app.ratelimit import limiter

SECRET = "AIzaSyFAKEFAKEFAKEFAKEFAKEFAKE123456"
BODY = "projects/123 SECRET-BODY-TEXT user asked about payroll"


def request():
    return httpx.Request("POST", f"https://generativelanguage.example.invalid/v1/chat?key={SECRET}")


def status_error(cls, code, text=BODY):
    response = httpx.Response(code, request=request(), text=text)
    return cls(f"Error code: {code} - {text} ({SECRET})", response=response, body={"error": {"message": text}})


def rate_limit(text=BODY):
    return status_error(openai.RateLimitError, 429, text)


def unavailable(code=503):
    return status_error(openai.InternalServerError, code)


def not_found():
    return status_error(openai.NotFoundError, 404)


def timeout():
    return openai.APITimeoutError(request=request())


def connection():
    return openai.APIConnectionError(request=request())


OK = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))])


class FakeClient:
    """Plays a list of outcomes, one per call: an exception to raise or a value to return.

    A tuple (outcome, seconds) also moves the fake clock forward, like a slow call.
    """

    def __init__(self, *outcomes, clock=None):
        self.outcomes, self.calls, self.clock = list(outcomes), [], clock
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.embeddings = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, tuple):
            outcome, seconds = outcome
            self.clock["t"] += seconds
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def clock(monkeypatch):
    """A fake clock. Sleeping moves it forward and is recorded, so no test really waits."""
    state = {"t": 1000.0, "sleeps": []}
    monkeypatch.setattr(llm, "_clock", lambda: state["t"])

    def sleep(seconds):
        state["sleeps"].append(seconds)
        state["t"] += seconds

    monkeypatch.setattr(llm, "_sleep", sleep)
    monkeypatch.setattr(settings, "chat_model", "main-model")
    monkeypatch.setattr(settings, "chat_timeout_seconds", 30.0)
    monkeypatch.setattr(settings, "embed_timeout_seconds", 20.0)
    monkeypatch.setattr(settings, "llm_call_budget_seconds", 45.0)
    return state


def ask(client, **kwargs):
    return llm.chat(client, messages=[{"role": "user", "content": "hi"}], **kwargs)


# ---------- success and timeouts ----------
def test_a_good_call_is_made_once_with_the_model_and_the_timeout(clock):
    client = FakeClient(OK)
    assert ask(client) is OK
    (call,) = client.calls
    assert call["model"] == "main-model" and call["timeout"] == 30.0
    assert clock["sleeps"] == []


def test_embeddings_use_the_embedding_timeout(clock, monkeypatch):
    monkeypatch.setattr(settings, "embed_model", "embed-model")
    client = FakeClient(OK)
    llm.embeddings(client, input=["x"])
    assert client.calls[0]["model"] == "embed-model" and client.calls[0]["timeout"] == 20.0


def test_the_client_has_no_hidden_sdk_retries_and_a_short_timeout():
    assert ingest.client.max_retries == 0
    # never the SDK default of 600 s (the evaluation runner may set its own, up to 90 s)
    assert float(ingest.client.timeout) <= 90


# ---------- retries ----------
def test_a_429_is_retried_and_the_second_try_can_succeed(clock):
    client = FakeClient(rate_limit(), OK)
    assert ask(client) is OK
    assert len(client.calls) == 2 and clock["sleeps"] == [1.0]


@pytest.mark.parametrize("failure", [unavailable(500), unavailable(502), unavailable(503), unavailable(504), timeout(), connection(), rate_limit()])
def test_retryable_errors_are_tried_three_times_with_1s_then_3s(clock, failure):
    client = FakeClient(failure, failure, failure)
    with pytest.raises(llm.ProviderUnavailable):
        ask(client)
    assert len(client.calls) == 3
    assert clock["sleeps"] == [1.0, 3.0]


def test_a_third_failure_is_final_there_is_no_fourth_try(clock):
    client = FakeClient(unavailable(), unavailable(), unavailable(), OK)
    with pytest.raises(llm.ProviderUnavailable) as stop:
        ask(client)
    assert len(client.calls) == 3 and (stop.value.kind, stop.value.status) == ("server", 503)


@pytest.mark.parametrize(
    "failure, kind",
    [
        (not_found(), "not_found"),
        (status_error(openai.BadRequestError, 400), "bad_request"),
        (status_error(openai.AuthenticationError, 401), "auth"),
        (status_error(openai.PermissionDeniedError, 403), "auth"),
        (ValueError("boom"), "other"),
    ],
)
def test_other_errors_are_never_retried_on_the_same_model(clock, failure, kind):
    client = FakeClient(failure, OK)
    with pytest.raises(llm.ProviderUnavailable) as stop:
        ask(client)
    assert len(client.calls) == 1 and clock["sleeps"] == [] and stop.value.kind == kind


@pytest.mark.parametrize("text", ["Quota exceeded for metric ... PerDay ...", "You exceeded your daily quota", "requests per day limit reached"])
def test_a_daily_quota_429_is_not_retried(clock, text):
    client = FakeClient(rate_limit(text), OK)
    with pytest.raises(llm.ProviderUnavailable) as stop:
        ask(client)
    assert len(client.calls) == 1 and clock["sleeps"] == [] and stop.value.kind == "quota"


def test_a_per_minute_429_is_not_mistaken_for_a_daily_quota(clock):
    client = FakeClient(rate_limit("Rate limit reached for requests per minute"), OK)
    assert ask(client) is OK and len(client.calls) == 2


# ---------- the total budget ----------
def test_an_attempt_never_runs_longer_than_the_time_left(clock, monkeypatch):
    monkeypatch.setattr(settings, "llm_call_budget_seconds", 10.0)
    client = FakeClient((timeout(), 10.0), OK, clock=clock)
    client.clock = clock
    with pytest.raises(llm.ProviderUnavailable) as stop:
        ask(client)
    assert client.calls[0]["timeout"] == 10.0  # the 30 s setting was cut to the 10 s budget
    assert len(client.calls) == 1 and stop.value.kind == "timeout"


def test_no_retry_starts_when_the_backoff_would_use_up_the_budget(clock, monkeypatch):
    monkeypatch.setattr(settings, "llm_call_budget_seconds", 10.0)
    client = FakeClient((unavailable(), 9.5), OK, clock=clock)
    with pytest.raises(llm.ProviderUnavailable):
        ask(client)
    assert len(client.calls) == 1 and clock["sleeps"] == []


def test_each_retry_gets_only_the_time_that_is_left(clock, monkeypatch):
    monkeypatch.setattr(settings, "llm_call_budget_seconds", 45.0)
    client = FakeClient((timeout(), 30.0), (timeout(), 8.0), (timeout(), 1.0), clock=clock)
    with pytest.raises(llm.ProviderUnavailable):
        ask(client)
    # 45 s budget: 30 s used + 1 s backoff leaves 14, then 8 s used + 3 s backoff leaves 3
    assert [c["timeout"] for c in client.calls] == [30.0, 14.0, 3.0]


# ---------- nothing from the provider reaches users or logs ----------
def test_the_failure_has_a_fixed_message_and_no_attached_error(clock):
    with pytest.raises(llm.ProviderUnavailable) as stop:
        ask(FakeClient(unavailable(), unavailable(), unavailable()))
    error = stop.value
    assert str(error) == "The AI service is busy right now. Please try again in a moment."
    assert error.__cause__ is None and error.__suppress_context__ is True
    printed = "".join(traceback.format_exception(error))
    assert SECRET not in printed and "SECRET-BODY-TEXT" not in printed


def test_the_log_has_status_and_type_but_no_body_url_or_key(clock, caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(llm.ProviderUnavailable):
        ask(FakeClient(rate_limit(), unavailable(), unavailable()))
    log = caplog.text
    assert "kind=rate_limit" in log and "status=429" in log and "status=503" in log
    assert "error=RateLimitError" in log and "error=InternalServerError" in log and "model=main-model" in log
    for leaked in (SECRET, "SECRET-BODY-TEXT", "payroll", "generativelanguage", "https://", "key="):
        assert leaked not in log
    assert all(record.exc_info is None for record in caplog.records)


def test_a_success_logs_only_the_model_name(clock, caplog):
    caplog.set_level(logging.INFO)
    ask(FakeClient(rate_limit(), OK))
    assert "chat answered by model=main-model" in caplog.text and SECRET not in caplog.text


# ---------- the route ----------
@pytest.fixture
def route(monkeypatch, login_as, clock):
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    limiter.reset()
    login_as("employee")
    monkeypatch.setattr(main, "propose_action", lambda q: None)
    yield TestClient(main.app, raise_server_exceptions=False)
    limiter.reset()


def test_chat_returns_a_503_with_retry_after_and_the_fixed_body(route, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(rag, "retrieve", lambda q, k=4: [{"content": "c", "metadata": {"source": "vpn.md"}, "distance": 0.2, "score": 1}])
    monkeypatch.setattr(rag, "article_chunks", lambda source: [])
    monkeypatch.setattr(rag, "client", FakeClient(unavailable(), unavailable(), unavailable()))
    res = route.post("/chat", json={"message": "My VPN keeps dropping"})
    assert res.status_code == 503
    assert res.json() == {"detail": "The AI service is busy right now. Please try again in a moment."}
    assert res.headers["retry-after"] == "10"
    assert res.headers["x-content-type-options"] == "nosniff"  # still goes through the security middleware
    for leaked in (SECRET, "SECRET-BODY-TEXT", "generativelanguage"):
        assert leaked not in res.text and leaked not in caplog.text


def test_a_failing_embedding_also_gives_the_503(route, monkeypatch):
    monkeypatch.setattr(ingest, "client", FakeClient(timeout(), timeout(), timeout()))
    res = route.post("/chat", json={"message": "My VPN keeps dropping"})
    assert res.status_code == 503 and res.headers["retry-after"] == "10"


def test_a_404_for_the_model_is_a_503_for_the_user_without_the_details(route, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(rag, "retrieve", lambda q, k=4: [{"content": "c", "metadata": {"source": "vpn.md"}, "distance": 0.2, "score": 1}])
    monkeypatch.setattr(rag, "article_chunks", lambda source: [])
    client = FakeClient(not_found())
    monkeypatch.setattr(rag, "client", client)
    res = route.post("/chat", json={"message": "My VPN keeps dropping"})
    assert res.status_code == 503 and len(client.calls) == 1
    assert "kind=not_found" in caplog.text and "status=404" in caplog.text and SECRET not in caplog.text


def test_a_good_answer_is_unchanged(route, monkeypatch):
    monkeypatch.setattr(rag, "retrieve", lambda q, k=4: [{"content": "c", "metadata": {"source": "vpn.md"}, "distance": 0.2, "score": 1}])
    monkeypatch.setattr(rag, "article_chunks", lambda source: [])
    reply = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="1. Restart.", tool_calls=None))])
    monkeypatch.setattr(rag, "client", FakeClient(rate_limit(), reply))  # one retry inside, invisible to the user
    res = route.post("/chat", json={"message": "My VPN keeps dropping"})
    assert res.status_code == 200 and res.json()["answer"] == "1. Restart."
