"""LLM-as-judge scoring for answered questions.

Two separate calls per answer:
  - faithfulness: is every claim in the answer supported by the retrieved context?
  - relevance: does the answer address what the user asked?

The judge is the same chat model that wrote the answers, which tends to favour its own
wording. Read the scores as a rough signal, not as ground truth.
"""
import json
import re

from .config import settings
from .eval_metrics import with_retry

FAITHFULNESS_PROMPT = """You are a strict evaluator of an IT helpdesk assistant.
Judge ONLY faithfulness: is every claim in the ANSWER supported by the CONTEXT?

Rules:
- Treat the CONTEXT as the only source of truth. Ignore your own knowledge.
- Score 1.0 if every factual claim (steps, numbers, names, conditions, advice) is supported by the CONTEXT.
- Lower the score for each claim that is unsupported or contradicted. Score 0.0 if most claims are unsupported.
- Generic politeness is fine and does not count as a claim.

Reply with JSON only, no markdown and no extra text:
{{"score": <number from 0 to 1>, "reason": "<one short sentence>"}}

CONTEXT:
{context}

ANSWER:
{answer}"""

RELEVANCE_PROMPT = """You are a strict evaluator of an IT helpdesk assistant.
Judge ONLY answer relevance: does the ANSWER address what the user asked in the QUESTION?

Rules:
- Do not check facts. Only check whether the answer is about the user's problem and helps with it.
- The question may contain typos, slang or Filipino words. Judge what the user meant.
- Score 1.0 if it fully addresses the question, about 0.5 if it only partly does or covers just one of two problems,
  and 0.0 if it is off topic or only says it cannot help.

Reply with JSON only, no markdown and no extra text:
{{"score": <number from 0 to 1>, "reason": "<one short sentence>"}}

QUESTION:
{question}

ANSWER:
{answer}"""

MAX_REASON = 200


def parse_judge_json(raw) -> dict | None:
    """Turn the judge's reply into {"score": 0..1, "reason": str}, or None if it is unusable."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    # The reply may come wrapped in ```json fences or have text around the object.
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    score = data.get("score")
    if isinstance(score, bool):
        return None
    if isinstance(score, str):
        try:
            score = float(score.strip())
        except ValueError:
            return None
    if not isinstance(score, (int, float)) or score != score:  # also rejects NaN
        return None

    reason = data.get("reason")
    reason = reason.strip()[:MAX_REASON] if isinstance(reason, str) else ""
    return {"score": min(1.0, max(0.0, float(score))), "reason": reason}


def _ask(client, prompt: str) -> str:
    res = client.chat.completions.create(
        model=settings.chat_model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
    )
    return res.choices[0].message.content or ""


def judge(client, prompt: str, tries: int = 4, sleep=None) -> dict:
    """One judge call. Never raises: returns {"score", "reason"} or {"error": "..."}.

    A failed call or an unusable reply gives an error entry, which the report counts and
    leaves out of the averages.
    """
    kwargs = {} if sleep is None else {"sleep": sleep}
    try:
        raw = with_retry(lambda: _ask(client, prompt), tries=tries, **kwargs)
    except Exception as e:
        return {"error": f"judge call failed: {type(e).__name__}"}
    parsed = parse_judge_json(raw)
    if parsed is None:
        return {"error": "judge reply was not valid JSON"}
    return parsed


def judge_faithfulness(client, context: str, answer: str, **kwargs) -> dict:
    return judge(client, FAITHFULNESS_PROMPT.format(context=context, answer=answer), **kwargs)


def judge_relevance(client, question: str, answer: str, **kwargs) -> dict:
    return judge(client, RELEVANCE_PROMPT.format(question=question, answer=answer), **kwargs)
