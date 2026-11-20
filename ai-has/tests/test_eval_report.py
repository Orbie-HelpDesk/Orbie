from app import eval_metrics as m
from app import eval_report as r


def rag(id_, category, in_scope, expected, escalate, citations=(), best=0.3, latency=1.0, faith=None, rel=None):
    return {
        "set": "hard", "id": id_, "category": category, "question": f"question {id_}",
        "in_scope": in_scope, "expected_source": expected, "distances": [best], "best_distance": best,
        "escalate": escalate, "citations": list(citations), "answer": "an answer", "latency": latency,
        "faithfulness": None if faith is None else {"score": faith, "reason": "faith reason"},
        "relevance": None if rel is None else {"score": rel, "reason": "rel reason"},
        "judge_errors": [], "error": None,
    }


def guard(id_, category, passed, detail="detail"):
    return {"set": "hard", "id": id_, "category": category, "question": f"guard {id_}", "passed": passed, "detail": detail}


RECORDS = [
    rag("a", "typo", True, "vpn.md", False, ["vpn.md"], 0.2, 2.0, 1.0, 1.0),
    rag("b", "typo", True, "wifi.md", False, ["vpn.md"], 0.3, 4.0, 0.3, 0.9),  # answered with the wrong article
    rag("c", "two", True, ["vpn.md", "printer.md"], False, ["printer.md"], 0.35, 6.0, 0.8, 0.5),
    rag("d", "two", True, ["vpn.md", "printer.md"], False, ["vpn.md", "printer.md"], 0.3, 8.0, 1.0, 1.0),
    rag("e", "oos", False, None, True, [], 0.7, 1.0),
    rag("f", "oos", False, None, False, ["vpn.md"], 0.45, 3.0, 0.6, 0.2),  # should have been escalated
    {**rag("g", "typo", True, "vpn.md", None), "error": "RateLimitError", "best_distance": 0.25, "latency": None},
]
GUARD = [
    guard("i1", "injection", True),
    guard("i2", "injection", False, "expected blocked, was not blocked"),
    guard("p1", "pii", True),
    guard("b1", "benign_not_blocked", True),
]


def test_rag_summary_counts():
    s = r.rag_summary(RECORDS)
    assert len(s["in_scope"]) == 4 and len(s["out_scope"]) == 2  # the failed one is left out
    assert [x["id"] for x in s["hits"]] == ["a", "c", "d"]
    assert [x["id"] for x in s["missed"]] == ["b"]
    assert [x["id"] for x in s["correct_escalations"]] == ["e"]
    assert [x["id"] for x in s["wrongly_answered"]] == ["f"]
    assert [x["id"] for x in s["multi_full"]] == ["d"] and len(s["multi"]) == 2
    assert [x["id"] for x in s["failed"]] == ["g"]


def test_metrics_table_keeps_the_existing_metric_names():
    s = r.rag_summary(RECORDS)
    table = r.metrics_table(s, r.guardrail_summary(GUARD))
    for label in (
        "Retrieval accuracy (right article cited) | 75% (3/4)",
        "Ticket deflection rate (answered without escalation) | 100% (4/4)",
        "Out-of-scope correctly escalated | 50% (1/2)",
        "Two-problem questions with every article cited | 50% (1/2)",
        "Injection attempts blocked | 50% (1/2)",
        "PII messages redacted | 100% (1/1)",
        "Harmless messages not blocked | 100% (1/1)",
        "Latency average",
        "Latency median",
        "Latency p95",
    ):
        assert label in table, label


def test_misses_are_listed_as_they_are():
    s = r.rag_summary(RECORDS)
    text = "\n".join(r.misses_lines(s, GUARD))
    assert '"question b" expected `wifi.md`, got [\'vpn.md\']' in text
    assert '"question f" should have been escalated' in text
    assert 'guardrail expectation failed: expected blocked, was not blocked' in text
    assert '"question g" could not be evaluated: RateLimitError' in text
    assert r.misses_lines(r.rag_summary([]), []) == ["None."]


def test_judge_section_has_averages_worst_five_and_error_count():
    records = [dict(x) for x in RECORDS]
    records[0] = {**records[0], "judge_errors": ["relevance: judge reply was not valid JSON"], "relevance": None}
    text = r.judge_section(records, enabled=True)
    assert "Faithfulness | 0.74 | 5/5" in text  # (1.0 + 0.3 + 0.8 + 1.0 + 0.6) / 5
    assert "Answer relevance | 0.65 | 4/5" in text  # (0.9 + 0.5 + 1.0 + 0.2) / 4, one judge call failed
    assert "left out of the averages): 1" in text
    assert "### Worst five" in text
    # the lowest score is question f (relevance 0.2), then b (faithfulness 0.3)
    assert text.index("question f") < text.index("question b")
    assert "Not run" in r.judge_section(records, enabled=False)


def test_sweep_section_shows_table_recommendation_and_current_value():
    rows = m.sweep([{"in_scope": True, "best_distance": 0.4}, {"in_scope": False, "best_distance": 0.65}])
    text = r.sweep_section(rows, m.recommend(rows), 0.5, 2)
    assert "| 0.50 (current) |" in text
    assert "**Recommendation: 0.50.**" in text
    assert "Nothing was changed in the code." in text


def test_full_report_has_every_section_and_keeps_the_load_test():
    rows = m.sweep([{"in_scope": x["in_scope"], "best_distance": x["best_distance"]} for x in RECORDS])
    report = r.build_report(
        date="2026-11-21",
        header="Sets run: hard",
        sets=[("Hard set", RECORDS, GUARD, "Sample: 7 of 37.")],
        judge_records=RECORDS,
        judge_enabled=True,
        sweep_rows=rows,
        recommendation=m.recommend(rows),
        current_threshold=0.5,
        sweep_count=len(RECORDS),
        loadtest_body="## Load test\n\nmock mode",
    )
    for heading in ("# AI-HAS Evaluation Report", "Date: 2026-11-21", "## How to read this", "## Hard set (4 in-scope, 2 out-of-scope)",
                    "Sample: 7 of 37.", "### By category", "### Missed questions", "## Faithfulness and relevance",
                    "## Threshold sweep", "<!-- loadtest:start -->", "mock mode"):
        assert heading in report, heading
    assert report.endswith("\n")
