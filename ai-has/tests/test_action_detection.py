import json
import logging
from types import SimpleNamespace

import pytest

from app import llm, rag
from app.config import settings
from test_llm import SECRET, FakeClient, not_found, timeout, unavailable


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(llm, "_sleep", lambda s: pytest.fail("a single attempt must never wait"))
    monkeypatch.setattr(settings, "chat_model", "main-model")
    monkeypatch.setattr(settings, "chat_model_fallback", "fallback-model")


def tool_call(name, arguments="{}"):
    call = SimpleNamespace(function=SimpleNamespace(name=name, arguments=arguments))
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[call]))])


def use(monkeypatch, *outcomes):
    client = FakeClient(*outcomes)
    monkeypatch.setattr(rag, "client", client)
    return client


# ---------- informational questions are skipped ----------
@pytest.mark.parametrize(
    "message",
    [
        "How do I reset my password?",
        "how can I open a ticket",
        "What is a ticket?",
        "What's the status of my ticket",
        "Why can't I create a ticket",
        "WHY is my account locked out",
        "  how do I get unlocked? I am locked out",
        '"What" does escalate mean',
        "...how do I contact a human agent",
    ],
)
def test_a_question_that_starts_with_how_what_or_why_costs_no_model_call(monkeypatch, message):
    client = use(monkeypatch, tool_call("request_password_reset"))
    assert rag.propose_action(message) is None
    assert client.calls == []


@pytest.mark.parametrize(
    "message",
    [
        "Please reset my password",
        "Can you open a ticket for my monitor",
        "Howdy, please create a ticket",  # "howdy" is not "how"
        "However, I need a ticket for the printer",  # "however" is not "how"
        "Whatever, just reset my password please",  # "whatever" is not "what"
        "I forgot my password, how do I reset it?",  # how is not the first word
    ],
)
def test_other_messages_still_get_the_model_call(monkeypatch, message):
    client = use(monkeypatch, tool_call("request_password_reset"))
    assert rag.propose_action(message) == {"name": "request_password_reset", "arguments": {}}
    assert len(client.calls) == 1


def test_the_skip_applies_before_anything_else_even_with_an_action_word(monkeypatch):
    client = use(monkeypatch)
    assert rag.propose_action("How do I create a ticket and escalate to a human agent?") is None
    assert client.calls == []


# ---------- one attempt, then the rule ----------
@pytest.mark.parametrize("failure", [unavailable(503), unavailable(500), timeout(), not_found()])
def test_a_failing_provider_gets_one_attempt_and_no_fallback_model(monkeypatch, failure):
    client = use(monkeypatch, failure, tool_call("request_password_reset"))
    result = rag.propose_action("Please reset my password")
    assert len(client.calls) == 1 and client.calls[0]["model"] == "main-model"
    assert result == {"name": "request_password_reset", "arguments": {}}  # the rule-based trigger answered


def test_the_rule_still_proposes_a_ticket_when_the_model_call_fails(monkeypatch):
    use(monkeypatch, unavailable())
    result = rag.propose_action("Create a ticket: the printer on floor 2 is jammed")
    assert result["name"] == "create_ticket" and result["arguments"]["summary"].startswith("Create a ticket")


def test_a_failing_call_with_nothing_clear_proposes_nothing(monkeypatch):
    use(monkeypatch, unavailable())
    assert rag.propose_action("My ticket is mentioned in this sentence") is None


def test_a_malformed_tool_call_still_falls_back_to_the_rule(monkeypatch):
    use(monkeypatch, tool_call("create_ticket", "{not json"))
    assert rag.propose_action("Please open a ticket, my keyboard is broken") is not None


def test_the_failure_is_logged_by_type_only(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    use(monkeypatch, unavailable())
    rag.propose_action("Please reset my password")
    assert "Tool calling failed (ProviderUnavailable)" in caplog.text
    for leaked in (SECRET, "SECRET-BODY-TEXT", "https://"):
        assert leaked not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_the_normal_answer_still_retries_and_uses_the_fallback(monkeypatch):
    """The single attempt is only for action detection. The answer keeps its retries and fallback."""
    monkeypatch.setattr(llm, "_sleep", lambda s: None)
    reply = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="1. Restart.", tool_calls=None))])
    client = use(monkeypatch, unavailable(), unavailable(), unavailable(), reply)
    monkeypatch.setattr(rag, "article_chunks", lambda source: [])
    chunks = [{"content": "c", "metadata": {"source": "vpn.md"}, "distance": 0.2, "score": 1}]
    result = rag.answer("My VPN keeps dropping", chunks)
    assert result["answer"] == "1. Restart."
    assert [c["model"] for c in client.calls] == ["main-model"] * 3 + ["fallback-model"]
    assert json.dumps(result)  # plain data
