"""Pure helpers for the evaluation: scoring, threshold sweep and report tables.

Nothing here calls Gemini or the database, so it is easy to test with fake data.
"""
import time

THRESHOLDS = [round(0.30 + 0.05 * i, 2) for i in range(9)]  # 0.30 to 0.70


def pct(n, d):
    return f"{(100 * n / d):.0f}%" if d else "n/a"


def percentile(values: list[float], p: float) -> float | None:
    """Value at percentile p (0 to 100), nearest-rank style. None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p / 100))]


def with_retry(fn, tries: int = 4, base_delay: float = 2.0, sleep=time.sleep, no_retry: tuple = ()):
    """Call fn(); on any error wait 2s, 4s, 8s ... and try again. Raises the last error.

    An error of a type in no_retry is raised at once (the call already retried inside).
    """
    for attempt in range(tries):
        try:
            return fn()
        except Exception as e:
            if attempt == tries - 1 or (no_retry and isinstance(e, no_retry)):
                raise
            sleep(base_delay * 2**attempt)


def expected_list(expected) -> list[str]:
    if expected is None:
        return []
    return list(expected) if isinstance(expected, (list, tuple)) else [expected]


def is_hit(expected, citations: list[str]) -> bool:
    """True if at least one expected article is cited."""
    return any(e in citations for e in expected_list(expected))


def coverage(expected, citations: list[str]) -> float:
    """Share of the expected articles that were cited (1.0 when all of them were)."""
    wanted = expected_list(expected)
    return sum(1 for e in wanted if e in citations) / len(wanted) if wanted else 0.0


def average(values) -> float | None:
    """Mean that skips None (a judge call that failed). None if nothing is left."""
    kept = [v for v in values if v is not None]
    return sum(kept) / len(kept) if kept else None


def sample_evenly(items: list, limit: int | None) -> list:
    """Deterministic spread-out sample, so --limit covers every part of the list."""
    if limit is None or limit >= len(items):
        return list(items)
    return [items[int(i * len(items) / limit)] for i in range(limit)]


def worst_n(records: list[dict], n: int = 5) -> list[dict]:
    """Answered records with the lowest judge score (the lower of faithfulness and relevance)."""

    def lowest(r):
        scores = [s["score"] for s in (r.get("faithfulness"), r.get("relevance")) if s]
        return min(scores) if scores else None

    scored = [(lowest(r), r) for r in records]
    scored = [(s, r) for s, r in scored if s is not None]
    scored.sort(key=lambda pair: pair[0])
    return [r for _, r in scored[:n]]


def sweep(rows: list[dict], thresholds: list[float] = THRESHOLDS) -> list[dict]:
    """For each MAX_DISTANCE value, count the mistakes the retrieval step alone would make.

    rows: {"in_scope": bool, "best_distance": float or None}. In-scope questions are
    wrongly escalated when their best distance is above the threshold. Out-of-scope
    questions are wrongly answered when their best distance is at or below it.
    """
    result = []
    for t in thresholds:
        escalated = sum(
            1 for r in rows
            if r["in_scope"] and (r["best_distance"] is None or r["best_distance"] > t)
        )
        answered = sum(
            1 for r in rows
            if not r["in_scope"] and r["best_distance"] is not None and r["best_distance"] <= t
        )
        result.append(
            {
                "threshold": t,
                "wrongly_escalated": escalated,
                "wrongly_answered": answered,
                "total_errors": escalated + answered,
            }
        )
    return result


def recommend(sweep_rows: list[dict]) -> dict | None:
    """Middle of the longest run of thresholds with the fewest errors.

    A value in the middle of a flat stretch is safer than one at its edge. With an even
    number of values the lower one is chosen (stricter, so fewer wrong answers).
    """
    if not sweep_rows:
        return None
    best = min(r["total_errors"] for r in sweep_rows)
    runs, current = [], []
    for r in sweep_rows:
        if r["total_errors"] == best:
            current.append(r["threshold"])
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    longest = max(runs, key=len)  # first one wins on a tie
    return {
        "threshold": longest[(len(longest) - 1) // 2],
        "range": (longest[0], longest[-1]),
        "errors": best,
    }


def markdown_table(headers: list[str], rows: list[list]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def sweep_table(sweep_rows: list[dict], current: float | None = None) -> str:
    rows = []
    for r in sweep_rows:
        mark = " (current)" if current is not None and abs(r["threshold"] - current) < 1e-9 else ""
        rows.append(
            [f"{r['threshold']:.2f}{mark}", r["wrongly_escalated"], r["wrongly_answered"], r["total_errors"]]
        )
    return markdown_table(
        ["MAX_DISTANCE", "In-scope wrongly escalated", "Out-of-scope wrongly answered", "Total errors"],
        rows,
    )


# ---------- Marked sections, so the load test can live in the same report ----------
def _markers(name: str) -> tuple[str, str]:
    return f"<!-- {name}:start -->", f"<!-- {name}:end -->"


def extract_section(report: str, name: str) -> str | None:
    start, end = _markers(name)
    if start in report and end in report:
        return report.split(start, 1)[1].split(end, 1)[0].strip("\n")
    return None


def replace_section(report: str, name: str, body: str) -> str:
    """Put body between the markers, replacing what was there or appending at the end."""
    start, end = _markers(name)
    block = f"{start}\n{body.strip(chr(10))}\n{end}"
    if start in report and end in report:
        before, rest = report.split(start, 1)
        after = rest.split(end, 1)[1]
        return before + block + after
    return report.rstrip("\n") + "\n\n" + block + "\n"
