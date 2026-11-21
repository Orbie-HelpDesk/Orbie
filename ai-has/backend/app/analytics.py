"""Helpdesk analytics from the saved conversations. Staff only (see the route in main.py).

Definitions
- question: a user message that was not blocked.
- answered / escalated / actions proposed / blocked: the outcome saved on the assistant message
  (messages.outcome, or messages.intent on rows saved before outcome existed).
- deflection rate: answered / (answered + escalated). Blocked messages and proposed actions are
  left out, because they are neither a solved question nor a failed search.
- thumbs up share: up / (up + down), over the messages that were rated.
- latency: whole request time in ms, over assistant messages that were not blocked.
- the escalated questions are the saved, PII-redacted texts. Redaction is pattern based, so a
  few personal details can still get through. Messages removed by purge_old are left out.
- days: calendar days in the viewer's time zone (the `tz` parameter, UTC by default), today included.
"""
from datetime import date, timedelta

from sqlalchemy import text

from .chatlog import PURGED_TEXT
from .db import engine

TOP_N = 10
# Times are stored in UTC. :tz is a bound parameter holding a name checked against pg_timezone_names
# (see timeutil.check_timezone). It is never put into the SQL text.
LOCAL = "(created_at at time zone 'UTC') at time zone :tz"  # the stored time as the viewer's wall clock
SINCE = (  # the start of the viewer's day, :d - 1 days ago, as a UTC time to compare with created_at
    "((date_trunc('day', now() at time zone :tz) - make_interval(days => :d - 1)) at time zone :tz) at time zone 'UTC'"
)

DAILY_SQL = text(
    f"select date_trunc('day', {LOCAL})::date as day, count(*) as n from messages "
    f"where role = 'user' and not blocked and created_at >= {SINCE} group by 1 order by 1"
)

TOTALS_SQL = text(
    f"""
    select (now() at time zone :tz)::date as today,
           count(*) filter (where coalesce(outcome, intent) = 'answered') as answered,
           count(*) filter (where coalesce(outcome, intent) = 'escalated') as escalated,
           count(*) filter (where coalesce(outcome, intent) = 'action_proposed') as actions,
           count(*) filter (where blocked) as blocked,
           percentile_cont(0.5) within group (order by latency_ms)
               filter (where latency_ms is not null and not blocked) as median_ms,
           percentile_cont(0.95) within group (order by latency_ms)
               filter (where latency_ms is not null and not blocked) as p95_ms,
           count(*) filter (where feedback = 1) as up,
           count(*) filter (where feedback = -1) as down
    from messages where role = 'assistant' and created_at >= {SINCE}
    """
)

ARTICLES_SQL = text(
    f"""
    select a.source, count(*) as n
    from messages m, json_array_elements_text(m.citations) as a(source)
    where m.role = 'assistant' and m.citations is not null and m.created_at >= {SINCE}
    group by a.source order by n desc, a.source limit {TOP_N}
    """
)

ESCALATED_SQL = text(
    f"""
    select min(trim(q.content)) as question, count(*) as n
    from messages a
    join lateral (
        select content from messages u
        where u.conversation_id = a.conversation_id and u.role = 'user' and u.id < a.id
        order by u.id desc limit 1
    ) q on true
    where a.role = 'assistant' and a.escalated and not a.blocked and a.created_at >= {SINCE}
      and q.content <> :purged
    group by lower(trim(q.content)) order by n desc, question limit {TOP_N}
    """
)


def _share(part: int, others: int) -> float | None:
    total = part + others
    return round(part / total, 3) if total else None


def build(days: int, today: date, daily_rows, totals, article_rows, escalated_rows) -> dict:
    """Turns the query rows into the answer. Pure, so it can be tested without a database."""
    counts = {r["day"]: r["n"] for r in daily_rows}
    series = []
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        series.append({"day": day.isoformat(), "count": int(counts.get(day, 0))})

    answered, escalated = int(totals["answered"]), int(totals["escalated"])
    up, down = int(totals["up"]), int(totals["down"])
    median, p95 = totals["median_ms"], totals["p95_ms"]
    return {
        "days": days,
        "questions_per_day": series,
        "totals": {
            "questions": sum(p["count"] for p in series),
            "answered": answered,
            "escalated": escalated,
            "actions_proposed": int(totals["actions"]),
            "blocked": int(totals["blocked"]),
        },
        "deflection_rate": _share(answered, escalated),
        "latency_ms": {
            "median": round(float(median)) if median is not None else None,
            "p95": round(float(p95)) if p95 is not None else None,
        },
        "feedback": {"up": up, "down": down, "up_share": _share(up, down)},
        "top_articles": [{"source": r["source"], "count": int(r["n"])} for r in article_rows],
        "top_escalated_questions": [{"question": r["question"], "count": int(r["n"])} for r in escalated_rows],
    }


def compute(days: int, tz: str = "UTC") -> dict:
    """The analytics for the last `days` days. Days begin and end in the time zone `tz`."""
    params = {"d": days, "tz": tz}
    with engine.connect() as conn:
        daily = conn.execute(DAILY_SQL, params).mappings().all()
        totals = conn.execute(TOTALS_SQL, params).mappings().one()
        articles = conn.execute(ARTICLES_SQL, params).mappings().all()
        escalated = conn.execute(ESCALATED_SQL, {**params, "purged": PURGED_TEXT}).mappings().all()
    return build(days, totals["today"], daily, totals, articles, escalated)
