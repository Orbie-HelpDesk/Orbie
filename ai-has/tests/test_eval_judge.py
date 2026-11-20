from types import SimpleNamespace

import pytest

from app import eval_judge as j


@pytest.mark.parametrize(
    "raw, score",
    [
        ('{"score": 0.8, "reason": "ok"}', 0.8),
        ('```json\n{"score": 1, "reason": "all supported"}\n```', 1.0),
        ('Here is my verdict: {"score": 0.25, "reason": "one claim unsupported"} Thanks!', 0.25),
        ('{"score": "0.6", "reason": "string score"}', 0.6),
        ('{"score": 1.7, "reason": "too high"}', 1.0),
        ('{"score": -2, "reason": "too low"}', 0.0),
        ('{"score": 0.5}', 0.5),
    ],
)
def test_parse_accepts_and_clamps(raw, score):
    parsed = j.parse_judge_json(raw)
    assert parsed is not None
    assert parsed["score"] == score


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "I cannot score this.",
        "{not json}",
        '{"reason": "no score"}',
        '{"score": null}',
        '{"score": true}',
        '{"score": "high"}',
        '{"score": NaN}',
        '["score", 1]',
        None,
        42,
    ],
)
def test_parse_returns_none_for_unusable_replies(raw):
    assert j.parse_judge_json(raw) is None


def test_long_reason_is_trimmed():
    parsed = j.parse_judge_json('{"score": 1, "reason": "' + "x" * 500 + '"}')
    assert len(parsed["reason"]) == j.MAX_REASON


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.prompts.append(kwargs["messages"][0]["content"])
        assert kwargs["temperature"] == 0
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])


def test_judge_scores_with_the_right_prompts():
    client = FakeClient(['{"score": 0.9, "reason": "a"}', '{"score": 0.4, "reason": "b"}'])
    faith = j.judge_faithfulness(client, context="CTX TEXT", answer="ANS TEXT")
    rel = j.judge_relevance(client, question="Q TEXT", answer="ANS TEXT")
    assert faith == {"score": 0.9, "reason": "a"}
    assert rel == {"score": 0.4, "reason": "b"}
    assert "CTX TEXT" in client.prompts[0] and "faithfulness" in client.prompts[0]
    assert "Q TEXT" in client.prompts[1] and "CTX TEXT" not in client.prompts[1]  # relevance never sees the context
    assert all("JSON only" in p for p in client.prompts)


def test_bad_json_becomes_an_error_entry_not_a_crash():
    client = FakeClient(["Sorry, I can't do that."])
    assert j.judge_relevance(client, "q", "a") == {"error": "judge reply was not valid JSON"}


def test_failed_call_is_retried_then_reported_without_raising():
    waits = []
    client = FakeClient([RuntimeError("429"), RuntimeError("429"), '{"score": 1, "reason": "fine"}'])
    assert j.judge_relevance(client, "q", "a", tries=4, sleep=waits.append)["score"] == 1.0
    assert waits == [2.0, 4.0]

    dead = FakeClient([RuntimeError("down")] * 3)
    result = j.judge_relevance(dead, "q", "a", tries=3, sleep=lambda s: None)
    assert result == {"error": "judge call failed: RuntimeError"}


def test_prompts_ask_for_strict_json_and_keep_braces_valid():
    for prompt in (j.FAITHFULNESS_PROMPT, j.RELEVANCE_PROMPT):
        assert '{{"score"' in prompt  # doubled braces, so .format() leaves a literal JSON example
    filled = j.RELEVANCE_PROMPT.format(question="q", answer="a")
    assert '{"score": <number from 0 to 1>' in filled
