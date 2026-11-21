import logging
from types import SimpleNamespace

import openai
import pytest
from fastapi.testclient import TestClient

from app import llm, main, rag
from app.config import settings
from app.ratelimit import limiter
from test_llm import (
    OK,
    SECRET,
    FakeClient,
    connection,
    not_found,
    rate_limit,
    status_error,
    timeout,
    unavailable,
)

FALLBACK_REPLY = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="from fallback", tool_calls=None))])


@pytest.fixture
def clock(monkeypatch):
    state = {"t": 1000.0, "sleeps": []}
    monkeypatch.setattr(llm, "_clock", lambda: state["t"])

    def sleep(seconds):
        state["sleeps"].append(seconds)
        state["t"] += seconds

    monkeypatch.setattr(llm, "_sleep", sleep)
    monkeypatch.setattr(settings, "chat_model", "main-model")
    monkeypatch.setattr(settings, "chat_model_fallback", "fallback-model")
    monkeypatch.setattr(settings, "chat_timeout_seconds", 30.0)
    monkeypatch.setattr(settings, "embed_timeout_seconds", 20.0)
    monkeypatch.setattr(settings, "llm_call_budget_seconds", 45.0)
    return state


def ask(client):
    return llm.chat(client, messages=[{"role": "user", "content": "hi"}])


def models(client):
    return [c["model"] for c in client.calls]


# ---------- when the fallback is used ----------
def test_a_404_on_the_main_model_goes_straight_to_the_fallback_once(clock):
    client = FakeClient(not_found(), FALLBACK_REPLY)
    assert ask(client) is FALLBACK_REPLY
    assert models(client) == ["main-model", "fallback-model"]  # no retry of the 404 on the main model
    assert clock["sleeps"] == []


@pytest.mark.parametrize("failure", [rate_limit(), unavailable(503), unavailable(500), timeout(), connection()])
def test_after_its_retries_the_main_model_hands_over_to_the_fallback(clock, failure):
    client = FakeClient(failure, failure, failure, FALLBACK_REPLY)
    assert ask(client) is FALLBACK_REPLY
    assert models(client) == ["main-model"] * 3 + ["fallback-model"]
    assert clock["sleeps"] == [1.0, 3.0]


def test_a_daily_quota_skips_the_retries_and_uses_the_fallback(clock):
    client = FakeClient(rate_limit("Quota exceeded ... PerDay ..."), FALLBACK_REPLY)
    assert ask(client) is FALLBACK_REPLY
    assert models(client) == ["main-model", "fallback-model"] and clock["sleeps"] == []


def test_the_fallback_is_tried_once_and_never_retried(clock):
    client = FakeClient(not_found(), unavailable(), FALLBACK_REPLY)
    with pytest.raises(llm.ProviderUnavailable) as stop:
        ask(client)
    assert models(client) == ["main-model", "fallback-model"]  # the third outcome was never used
    assert stop.value.kind == "not_found" and stop.value.status == 404  # the main model's reason is reported
    assert clock["sleeps"] == []


def test_a_good_main_model_never_touches_the_fallback(clock):
    client = FakeClient(OK, FALLBACK_REPLY)
    assert ask(client) is OK and models(client) == ["main-model"]


# ---------- when it is not used ----------
@pytest.mark.parametrize(
    "failure",
    [status_error(openai.BadRequestError, 400), status_error(openai.AuthenticationError, 401), status_error(openai.PermissionDeniedError, 403), ValueError("x")],
)
def test_it_is_not_used_after_errors_a_different_model_would_not_fix(clock, failure):
    client = FakeClient(failure, FALLBACK_REPLY)
    with pytest.raises(llm.ProviderUnavailable):
        ask(client)
    assert models(client) == ["main-model"]


@pytest.mark.parametrize("value", ["", "   ", "main-model"])
def test_no_fallback_when_it_is_empty_or_the_same_as_the_main_model(clock, monkeypatch, value):
    monkeypatch.setattr(settings, "chat_model_fallback", value)
    client = FakeClient(not_found(), FALLBACK_REPLY)
    with pytest.raises(llm.ProviderUnavailable):
        ask(client)
    assert models(client) == ["main-model"]


def test_embeddings_never_use_the_fallback(clock, monkeypatch):
    monkeypatch.setattr(settings, "embed_model", "embed-model")
    client = FakeClient(not_found(), FALLBACK_REPLY)
    with pytest.raises(llm.ProviderUnavailable):
        llm.embeddings(client, input=["x"])
    assert models(client) == ["embed-model"]


# ---------- time ----------
def test_the_fallback_still_gets_a_fair_attempt_when_the_budget_is_used_up(clock, monkeypatch):
    monkeypatch.setattr(settings, "llm_call_budget_seconds", 10.0)
    client = FakeClient((timeout(), 10.0), FALLBACK_REPLY, clock=clock)
    assert ask(client) is FALLBACK_REPLY
    assert client.calls[1]["model"] == "fallback-model" and client.calls[1]["timeout"] == llm.FALLBACK_MIN_SECONDS


def test_the_fallback_uses_the_remaining_time_when_there_is_more_than_the_minimum(clock):
    client = FakeClient((unavailable(), 5.0), (unavailable(), 5.0), (unavailable(), 5.0), FALLBACK_REPLY, clock=clock)
    assert ask(client) is FALLBACK_REPLY
    # 45 s budget, 15 s of calls and 4 s of backoff used: 26 s left, below the 30 s attempt timeout
    assert client.calls[3]["timeout"] == 26.0


# ---------- logging ----------
def test_the_log_names_the_models_and_nothing_else(clock, caplog):
    caplog.set_level(logging.DEBUG)
    ask(FakeClient(not_found(), FALLBACK_REPLY))
    log = caplog.text
    assert "trying fallback model=fallback-model" in log and "chat answered by model=fallback-model" in log
    assert "kind=not_found" in log and "status=404" in log
    for leaked in (SECRET, "SECRET-BODY-TEXT", "payroll", "https://", "key="):
        assert leaked not in log


def test_the_main_model_is_named_when_it_answers(clock, caplog):
    caplog.set_level(logging.INFO)
    ask(FakeClient(OK))
    assert "chat answered by model=main-model" in caplog.text and "fallback" not in caplog.text


# ---------- through the app ----------
@pytest.fixture
def route(monkeypatch, login_as, clock):
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    limiter.reset()
    login_as("employee")
    monkeypatch.setattr(main, "propose_action", lambda q: None)
    monkeypatch.setattr(rag, "retrieve", lambda q, k=4: [{"content": "c", "metadata": {"source": "vpn.md"}, "distance": 0.2, "score": 1}])
    monkeypatch.setattr(rag, "article_chunks", lambda source: [])
    yield TestClient(main.app, raise_server_exceptions=False)
    limiter.reset()


def test_the_user_gets_an_answer_when_the_main_model_is_missing(route, monkeypatch):
    client = FakeClient(not_found(), FALLBACK_REPLY)
    monkeypatch.setattr(rag, "client", client)
    res = route.post("/chat", json={"message": "My VPN keeps dropping"})
    assert res.status_code == 200 and res.json()["answer"] == "from fallback"
    assert models(client) == ["main-model", "fallback-model"]


def test_the_user_gets_the_503_when_both_models_fail(route, monkeypatch):
    monkeypatch.setattr(rag, "client", FakeClient(not_found(), unavailable()))
    res = route.post("/chat", json={"message": "My VPN keeps dropping"})
    assert res.status_code == 503 and res.headers["retry-after"] == "10"
    assert res.json() == {"detail": llm.BUSY_MESSAGE}
