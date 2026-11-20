from types import SimpleNamespace

from app import rag


def chunk(source, content, distance):
    return {"content": content, "metadata": {"source": source}, "distance": distance, "score": 0.0}


class FakeClient:
    """Records what would be sent to the chat model. No Gemini call."""

    def __init__(self, reply="1. Do the thing."):
        self.reply = reply
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        message = SimpleNamespace(content=self.reply)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def setup_fakes(monkeypatch, retrieved, reply="1. Do the thing."):
    fake = FakeClient(reply)
    searches = []
    monkeypatch.setattr(rag, "client", fake)
    monkeypatch.setattr(rag, "retrieve", lambda q, k=4: searches.append(q) or retrieved)
    monkeypatch.setattr(
        rag,
        "article_chunks",
        lambda source: [
            {"content": f"{source} step 1", "metadata": {"source": source}},
            {"content": f"{source} step 2", "metadata": {"source": source}},
        ],
    )
    return fake, searches


RETRIEVED = [
    chunk("vpn.md", "vpn chunk", 0.20),
    chunk("wifi.md", "wifi chunk", 0.23),  # within 0.05 of the best: included
    chunk("printer.md", "printer chunk", 0.40),  # too far: left out
]


def test_context_sent_to_the_model_is_unchanged(monkeypatch):
    fake, _ = setup_fakes(monkeypatch, RETRIEVED)
    result = rag.answer("vpn broken")

    expected_context = (
        "[Source: vpn.md]\nvpn.md step 1\n\n---\n\n"
        "[Source: vpn.md]\nvpn.md step 2\n\n---\n\n"
        "[Source: wifi.md]\nwifi chunk"
    )
    (call,) = fake.calls
    assert call["messages"][0] == {"role": "system", "content": rag.SYSTEM_PROMPT}
    assert call["messages"][1] == {
        "role": "user",
        "content": f"CONTEXT:\n{expected_context}\n\nQUESTION: vpn broken",
    }
    assert result == {
        "answer": "1. Do the thing.",
        "citations": ["vpn.md", "wifi.md"],
        "escalate": False,
        "distances": [0.2, 0.23, 0.4],
    }


def test_select_chunks_and_build_context_match_what_answer_uses(monkeypatch):
    setup_fakes(monkeypatch, RETRIEVED)
    selected = rag.select_chunks(RETRIEVED)
    assert [c["content"] for c in selected] == ["vpn.md step 1", "vpn.md step 2", "wifi chunk"]
    assert rag.build_context(selected).startswith("[Source: vpn.md]\nvpn.md step 1")


def test_falls_back_to_best_chunk_when_article_is_not_found(monkeypatch):
    setup_fakes(monkeypatch, RETRIEVED)
    monkeypatch.setattr(rag, "article_chunks", lambda source: [])
    selected = rag.select_chunks(RETRIEVED)
    assert selected[0]["content"] == "vpn chunk"


def test_answer_searches_once_unless_chunks_are_given(monkeypatch):
    fake, searches = setup_fakes(monkeypatch, RETRIEVED)
    rag.answer("one")
    assert searches == ["one"]
    rag.answer("two", chunks=RETRIEVED)
    assert searches == ["one"]  # the second call reused the chunks
    assert len(fake.calls) == 2


def test_far_match_escalates_without_calling_the_model(monkeypatch):
    far = [chunk("vpn.md", "x", rag.MAX_DISTANCE + 0.1)]
    fake, _ = setup_fakes(monkeypatch, far)
    result = rag.answer("pizza")
    assert result["escalate"] is True
    assert result["answer"] == rag.NOT_FOUND_MESSAGE
    assert fake.calls == []


def test_no_answer_reply_escalates(monkeypatch):
    setup_fakes(monkeypatch, RETRIEVED, reply="NO_ANSWER")
    result = rag.answer("vpn broken")
    assert result["escalate"] is True
    assert result["citations"] == []
