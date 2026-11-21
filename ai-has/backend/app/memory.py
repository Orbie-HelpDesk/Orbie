"""Conversation memory: the last few turns, and a standalone question for follow-ups.

The history comes from the saved conversation on the server (already PII-redacted, and only the
caller's own conversation, which the route has checked). Nothing the browser sends is trusted.
Without a saved conversation (saving off, or a new chat) there is no memory.

A follow-up like "it still doesn't work" is useless to a search on its own, so the search uses a
short standalone question built by rules: the earlier question plus the follow-up. No model call.
"""
import re

from .chatlog import PURGED_TEXT
from .config import settings
from .intent import TOPIC

MAX_TURN_CHARS = 500  # each earlier message is cut to this before it goes to the model
MAX_STANDALONE = 240
SHORT_MESSAGE_WORDS = 4

# Words that point back at something said earlier (English and Taglish)
REFERENCE = re.compile(
    r"\b(it|its|this|that|these|those|they|them|there|same|again|still|also|too|another|other one|the other|"
    r"yan|iyan|ito|yun|yon|niyan|nito|pa rin|pa din|ulit|din)\b",
    re.IGNORECASE,
)


def needs_rewrite(message: str) -> bool:
    """True for a short message or one with a reference like "it" or "still", unless it already names an IT topic."""
    text = message.strip()
    if TOPIC.search(text):
        return False  # "my VPN still fails" says what it is about
    return len(text.split()) <= SHORT_MESSAGE_WORDS or REFERENCE.search(text) is not None


def _usable(turn: dict) -> bool:
    return bool(turn["content"]) and turn["content"] != PURGED_TEXT


def standalone_question(message: str, turns: list[dict]) -> str:
    """The message on its own if it makes sense alone, otherwise the earlier question plus the follow-up."""
    if not needs_rewrite(message):
        return message
    earlier = [t["content"] for t in turns if t["role"] == "user" and _usable(t)]
    if not earlier:
        return message
    # the most recent earlier question that names a topic, else the most recent one
    previous = next((q for q in reversed(earlier) if TOPIC.search(q)), earlier[-1])
    room = MAX_STANDALONE - len(message) - 1
    if room <= 0:
        return message[:MAX_STANDALONE]
    return f"{previous[:room].rstrip()} {message}"


def history_messages(turns: list[dict]) -> list[dict]:
    """The earlier turns as chat messages for the model, each cut to MAX_TURN_CHARS."""
    return [
        {"role": t["role"], "content": t["content"][:MAX_TURN_CHARS]}
        for t in turns
        if t["role"] in ("user", "assistant") and _usable(t)
    ]


def max_turns() -> int:
    return max(0, settings.history_turns)
