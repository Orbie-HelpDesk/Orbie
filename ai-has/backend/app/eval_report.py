"""Builds the evaluation report text from result records. Pure: no API calls, easy to test."""
import statistics

from .eval_metrics import (
    average,
    coverage,
    expected_list,
    intent_summary,
    is_hit,
    markdown_table,
    output_summary,
    pct,
    percentile,
    replace_section,
    sweep_table,
    worst_n,
)

HOW_TO_READ = """\
- **Retrieval accuracy**: of the in-scope questions, how many were answered and cited at least one expected article.
- **Ticket deflection**: in-scope questions answered without escalating to a human.
- **Out-of-scope correctly escalated**: questions the knowledge base cannot answer that got the "not found" reply.
- **Guardrails**: injection attempts blocked, PII redacted, and harmless messages that were not blocked by mistake.
- **Faithfulness** (0 to 1): is every claim in the answer supported by the retrieved article? **Relevance** (0 to 1): does the answer address the question? Both are scored by the same Gemini model that wrote the answers, which tends to favour its own wording, so treat them as a rough signal.
- **Intent routing**: the intent the router gives each retrieval question (rules only by default, so no Gemini call), compared with hand-written labels. The labels were written from the question text before the router was run on the set, and the rules are not tuned to them.
- **Output guardrail**: what the answer check did. "May have been wrongly blocked" counts questions with an expected article that was retrieved, where the check still sent the answer to not found. Those are the ones to read by hand.
- **Threshold sweep**: uses retrieval distances only (no chat model). "Wrongly escalated" counts in-scope questions whose best match is farther than the threshold. "Wrongly answered" counts out-of-scope questions whose best match is at or inside it. The recommendation is the middle of the flat stretch with the fewest errors. It is advice, not a setting that was applied.
- The generated set was written from the articles themselves, so it is easy. The hard set was written by hand before any results were seen and is not tuned. Misses are listed as they are.
- The sets are small, so one question moves a percentage a lot. Latency includes the network round trip to Gemini."""


def rag_summary(records: list[dict]) -> dict:
    ok = [r for r in records if not r.get("error")]
    in_scope = [r for r in ok if r["in_scope"]]
    out_scope = [r for r in ok if not r["in_scope"]]
    answered = [r for r in in_scope if not r["escalate"]]
    hits = [r for r in answered if is_hit(r["expected_source"], r["citations"])]
    multi = [r for r in in_scope if len(expected_list(r["expected_source"])) > 1]
    return {
        "failed": [r for r in records if r.get("error")],
        "in_scope": in_scope,
        "out_scope": out_scope,
        "answered": answered,
        "hits": hits,
        "missed": [r for r in in_scope if r not in hits],
        "correct_escalations": [r for r in out_scope if r["escalate"]],
        "wrongly_answered": [r for r in out_scope if not r["escalate"]],
        "multi": multi,
        "multi_full": [
            r for r in multi if not r["escalate"] and coverage(r["expected_source"], r["citations"]) == 1.0
        ],
        "latencies": [r["latency"] for r in ok if r.get("latency") is not None],
    }


def guardrail_summary(records: list[dict]) -> dict[str, tuple[int, int]]:
    """category -> (passed, total)"""
    out: dict[str, tuple[int, int]] = {}
    for r in records:
        passed, total = out.get(r["category"], (0, 0))
        out[r["category"]] = (passed + (1 if r["passed"] else 0), total + 1)
    return out


def _ratio(n, d):
    return f"{pct(n, d)} ({n}/{d})"


def metrics_table(rag: dict, guard: dict[str, tuple[int, int]]) -> str:
    rows = []
    if rag["in_scope"] or rag["out_scope"]:
        n_in, n_out = len(rag["in_scope"]), len(rag["out_scope"])
        rows += [
            ["Retrieval accuracy (right article cited)", _ratio(len(rag["hits"]), n_in)],
            ["Ticket deflection rate (answered without escalation)", _ratio(len(rag["answered"]), n_in)],
            ["Out-of-scope correctly escalated", _ratio(len(rag["correct_escalations"]), n_out)],
        ]
    if rag["multi"]:
        rows.append(
            ["Two-problem questions with every article cited", _ratio(len(rag["multi_full"]), len(rag["multi"]))]
        )
    labels = {
        "injection": "Injection attempts blocked",
        "pii": "PII messages redacted",
        "benign_not_blocked": "Harmless messages not blocked",
    }
    for key, label in labels.items():
        if key in guard:
            rows.append([label, _ratio(*guard[key])])
    lat = rag["latencies"]
    if lat:
        rows += [
            ["Latency average", f"{statistics.mean(lat):.1f}s"],
            ["Latency median", f"{statistics.median(lat):.1f}s"],
            ["Latency p95", f"{percentile(lat, 95):.1f}s"],
        ]
    return markdown_table(["Metric", "Result"], rows)


def category_table(rag: dict) -> str:
    cats: dict[str, dict] = {}
    for r in rag["in_scope"] + rag["out_scope"]:
        c = cats.setdefault(r["category"], {"good": 0, "total": 0, "kinds": set()})
        c["total"] += 1
        c["kinds"].add(r["in_scope"])
        c["good"] += 1 if (r in rag["hits"] or (not r["in_scope"] and r["escalate"])) else 0
    rows = []
    for name, c in cats.items():
        if c["kinds"] == {True}:
            what = "right article cited"
        elif c["kinds"] == {False}:
            what = "correctly escalated"
        else:
            what = "correct"
        rows.append([name, c["total"], f"{pct(c['good'], c['total'])} ({c['good']}/{c['total']}) {what}"])
    return markdown_table(["Category", "Questions", "Result"], rows)


def misses_lines(rag: dict, guard_records: list[dict]) -> list[str]:
    lines = []
    for r in rag["missed"]:
        got = r["citations"] or "escalated"
        lines.append(
            f"- [{r['category']}] \"{r['question']}\" expected `{', '.join(expected_list(r['expected_source']))}`, got {got}"
        )
    for r in rag["wrongly_answered"]:
        lines.append(
            f"- [{r['category']}] \"{r['question']}\" should have been escalated, "
            f"but was answered citing {r['citations']}"
        )
    for r in guard_records:
        if not r["passed"]:
            lines.append(f"- [{r['category']}] \"{r['question']}\" guardrail expectation failed: {r['detail']}")
    for r in rag["failed"]:
        lines.append(f"- [{r['category']}] \"{r['question']}\" could not be evaluated: {r['error']}")
    return lines or ["None."]


def set_section(title: str, rag_records: list[dict], guard_records: list[dict], note: str = "") -> str:
    rag = rag_summary(rag_records)
    guard = guardrail_summary(guard_records)
    heading = f"## {title} ({len(rag['in_scope'])} in-scope, {len(rag['out_scope'])} out-of-scope)"
    parts = [heading, ""]
    if note:
        parts += [note, ""]
    parts += [metrics_table(rag, guard), ""]
    if len({r["category"] for r in rag["in_scope"] + rag["out_scope"]}) > 1:
        parts += ["### By category", "", category_table(rag), ""]
    parts += ["### Missed questions", ""] + misses_lines(rag, guard_records) + [""]
    return "\n".join(parts)


def judge_section(records: list[dict], enabled: bool) -> str:
    parts = ["## Faithfulness and relevance", ""]
    if not enabled:
        return "\n".join(parts + ["Not run (`--no-judge`).", ""])
    answered = [r for r in records if not r.get("error") and not r["escalate"]]
    faith = [r["faithfulness"]["score"] for r in answered if r.get("faithfulness")]
    rel = [r["relevance"]["score"] for r in answered if r.get("relevance")]
    errors = [e for r in answered for e in r.get("judge_errors", [])]
    f_avg, r_avg = average(faith), average(rel)
    parts += [
        markdown_table(
            ["Score", "Average", "Judged"],
            [
                ["Faithfulness", "n/a" if f_avg is None else f"{f_avg:.2f}", f"{len(faith)}/{len(answered)}"],
                ["Answer relevance", "n/a" if r_avg is None else f"{r_avg:.2f}", f"{len(rel)}/{len(answered)}"],
            ],
        ),
        "",
        f"Answered questions judged: {len(answered)}. Judge calls that failed or gave unusable JSON "
        f"(left out of the averages): {len(errors)}.",
        "",
        "### Worst five",
        "",
    ]
    worst = worst_n(answered, 5)
    if not worst:
        parts.append("None scored.")
    else:
        rows = []
        for r in worst:
            f = r["faithfulness"]["score"] if r.get("faithfulness") else None
            v = r["relevance"]["score"] if r.get("relevance") else None
            reason = ""
            for s in (r.get("faithfulness"), r.get("relevance")):
                if s and s["score"] == min(x for x in (f, v) if x is not None):
                    reason = s["reason"]
                    break
            rows.append(
                [
                    r["question"].replace("|", "/"),
                    "n/a" if f is None else f"{f:.2f}",
                    "n/a" if v is None else f"{v:.2f}",
                    reason.replace("|", "/"),
                ]
            )
        parts.append(markdown_table(["Question", "Faithfulness", "Relevance", "Judge's reason"], rows))
    return "\n".join(parts + [""])


def sweep_section(sweep_rows: list[dict], recommendation: dict | None, current: float, counted: int) -> str:
    parts = [
        "## Threshold sweep",
        "",
        f"Retrieval only, {counted} questions with a measured distance. Current MAX_DISTANCE is {current}.",
        "",
        sweep_table(sweep_rows, current),
        "",
    ]
    if recommendation:
        lo, hi = recommendation["range"]
        parts.append(
            f"**Recommendation: {recommendation['threshold']:.2f}.** The fewest errors ({recommendation['errors']}) "
            f"happen from {lo:.2f} to {hi:.2f}, and this is the middle of that range. "
            "Nothing was changed in the code."
        )
    else:
        parts.append("No recommendation: no questions with a measured distance.")
    return "\n".join(parts + [""])


def intent_section(records: list[dict]) -> str:
    """Intent accuracy. Left out when no question has an expected_intent label."""
    summary = intent_summary(records)
    if summary is None:
        return ""
    total = summary["total"]
    lines = [
        "## Intent routing",
        "",
        f"Intent accuracy: **{summary['correct']} of {total}** ({_ratio(summary['correct'], total)}). "
        "A question counts as correct when the intent is one of the accepted labels (two are accepted when both are fair).",
        "",
        f"Decided by the rules: {summary['sources']['rules']} of {total}. Not decided by the rules: "
        f"{summary['sources']['fallback'] + summary['sources']['model']} of {total} "
        "(these would cost one classifier call each in the app, or use the fallback here).",
        "",
        "| Category | Correct |",
        "|---|---|",
    ]
    for category, (hit, count) in sorted(summary["by_category"].items()):
        lines.append(f"| {category} | {hit} of {count} |")
    if summary["misses"]:
        lines += ["", "Wrong:"]
        for r in summary["misses"]:
            lines.append(
                f"- {r['id']} expected {', '.join(expected_list(r['expected_intent']))}, got {r['intent']} "
                f"({r['intent_source']}, {r['intent_confidence']})"
            )
    lines.append("")
    return "\n".join(lines)


def output_section(records: list[dict]) -> str:
    """What the output guardrail did, and answers it may have blocked wrongly."""
    summary = output_summary(records)
    if summary is None:
        return ""
    count, suspect = summary["count"], summary["suspect"]
    lines = [
        "## Output guardrail",
        "",
        f"Answers checked: {summary['checked']}. Passed: {count['pass']}. Replaced (PII, password, key or prompt text): "
        f"{count['replaced']}. Sent to not found (no source or not supported by the retrieved text): {count['not_found']}.",
        "",
        f"Questions with an expected article that was retrieved and answered: {summary['eligible']}. "
        f"Of those, answers that may have been wrongly blocked: **{len(suspect)}**.",
    ]
    for r in suspect + summary["replaced"]:
        lines.append(f"- {r['id']} {r['output_action']}: {', '.join(r['output_reasons'])}")
    lines.append("")
    return "\n".join(lines)


def build_report(
    date: str,
    header: str,
    sets: list[tuple[str, list[dict], list[dict], str]],
    judge_records: list[dict],
    judge_enabled: bool,
    sweep_rows: list[dict],
    recommendation: dict | None,
    current_threshold: float,
    sweep_count: int,
    loadtest_body: str | None = None,
) -> str:
    parts = [
        "# AI-HAS Evaluation Report",
        "",
        f"Date: {date}",
        "",
        header,
        "",
        "## How to read this",
        "",
        HOW_TO_READ,
        "",
    ]
    for title, rag_records, guard_records, note in sets:
        parts.append(set_section(title, rag_records, guard_records, note))
    for extra in (intent_section(judge_records), output_section(judge_records)):
        if extra:
            parts.append(extra)
    parts.append(judge_section(judge_records, judge_enabled))
    parts.append(sweep_section(sweep_rows, recommendation, current_threshold, sweep_count))
    report = "\n".join(parts).rstrip("\n") + "\n"
    if loadtest_body:
        report = replace_section(report, "loadtest", loadtest_body)
    return report
