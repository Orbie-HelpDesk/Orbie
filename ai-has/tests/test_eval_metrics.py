import pytest

from app import eval_metrics as m


def test_pct_and_percentile():
    assert m.pct(21, 21) == "100%"
    assert m.pct(0, 0) == "n/a"
    values = list(range(1, 101))  # 1..100
    assert m.percentile(values, 50) == 51
    assert m.percentile(values, 95) == 96
    assert m.percentile(values, 99) == 100
    assert m.percentile([], 95) is None
    assert m.percentile([7.0], 99) == 7.0


def test_with_retry_backs_off_then_succeeds():
    calls, waits = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("429")
        return "ok"

    assert m.with_retry(flaky, tries=4, base_delay=2, sleep=waits.append) == "ok"
    assert waits == [2, 4]


def test_with_retry_raises_after_last_try():
    waits = []

    def always_fails():
        raise ValueError("down")

    with pytest.raises(ValueError):
        m.with_retry(always_fails, tries=3, base_delay=1, sleep=waits.append)
    assert waits == [1, 2]  # no wait after the final failure


def test_hit_and_coverage_for_single_and_multiple_expected_articles():
    assert m.is_hit("vpn.md", ["vpn.md"]) is True
    assert m.is_hit("vpn.md", ["wifi.md"]) is False
    assert m.is_hit(["vpn.md", "printer.md"], ["printer.md"]) is True
    assert m.is_hit(None, ["vpn.md"]) is False
    assert m.coverage(["vpn.md", "printer.md"], ["printer.md"]) == 0.5
    assert m.coverage(["vpn.md", "printer.md"], ["printer.md", "vpn.md"]) == 1.0
    assert m.coverage("vpn.md", []) == 0.0


def test_average_skips_failed_judgments():
    assert m.average([1.0, None, 0.5]) == 0.75
    assert m.average([None, None]) is None
    assert m.average([]) is None


def test_sample_evenly_is_deterministic_and_spread_out():
    items = list(range(37))
    picked = m.sample_evenly(items, 10)
    assert picked == m.sample_evenly(items, 10)
    assert len(picked) == 10
    assert picked[0] == 0 and picked[-1] > 30
    assert m.sample_evenly(items, None) == items
    assert m.sample_evenly(items, 100) == items


def rec(q, faith=None, rel=None):
    return {
        "question": q,
        "faithfulness": None if faith is None else {"score": faith, "reason": ""},
        "relevance": None if rel is None else {"score": rel, "reason": ""},
    }


def test_worst_n_orders_by_lowest_score_and_skips_unscored():
    records = [rec("a", 1.0, 0.9), rec("b", 0.2, 1.0), rec("c", 0.8, 0.4), rec("none"), rec("d", None, 0.1)]
    assert [r["question"] for r in m.worst_n(records, 3)] == ["d", "b", "c"]
    assert [r["question"] for r in m.worst_n(records, 10)] == ["d", "b", "c", "a"]


FAKE_ROWS = [
    {"in_scope": True, "best_distance": 0.25},
    {"in_scope": True, "best_distance": 0.42},
    {"in_scope": True, "best_distance": 0.55},
    {"in_scope": True, "best_distance": None},  # retrieval found nothing
    {"in_scope": False, "best_distance": 0.58},
    {"in_scope": False, "best_distance": 0.66},
    {"in_scope": False, "best_distance": None},
]


def test_thresholds_run_from_030_to_070_in_steps_of_005():
    assert m.THRESHOLDS[0] == 0.30 and m.THRESHOLDS[-1] == 0.70
    assert len(m.THRESHOLDS) == 9


def test_sweep_counts_each_kind_of_mistake():
    by_t = {r["threshold"]: r for r in m.sweep(FAKE_ROWS)}
    # at 0.30 only the 0.25 question is answered: 3 in-scope escalated, no out-of-scope answered
    assert by_t[0.30] == {"threshold": 0.30, "wrongly_escalated": 3, "wrongly_answered": 0, "total_errors": 3}
    # at 0.50: 0.55 and None escalated (2); no out-of-scope under 0.50
    assert (by_t[0.50]["wrongly_escalated"], by_t[0.50]["wrongly_answered"]) == (2, 0)
    # at 0.60: only None is escalated (1); 0.58 answered by mistake (1)
    assert (by_t[0.60]["wrongly_escalated"], by_t[0.60]["wrongly_answered"]) == (1, 1)
    # at 0.70: both out-of-scope questions with a distance are answered
    assert (by_t[0.70]["wrongly_escalated"], by_t[0.70]["wrongly_answered"]) == (1, 2)


def test_recommend_picks_the_middle_of_the_best_flat_range():
    rows = [
        {"threshold": t, "total_errors": e}
        for t, e in zip(m.THRESHOLDS, [5, 3, 1, 1, 1, 1, 1, 4, 6])
    ]
    result = m.recommend(rows)
    assert result["errors"] == 1
    assert result["range"] == (0.40, 0.60)
    assert result["threshold"] == 0.50  # middle of 0.40..0.60


def test_recommend_even_range_takes_the_lower_middle_and_longest_run_wins():
    rows = [
        {"threshold": t, "total_errors": e}
        for t, e in zip(m.THRESHOLDS, [1, 2, 1, 1, 1, 1, 2, 3, 3])
    ]
    result = m.recommend(rows)
    assert result["range"] == (0.40, 0.55)  # longer than the lone 0.30
    assert result["threshold"] == 0.45  # lower of the two middle values
    assert m.recommend([]) is None


def test_sweep_table_marks_the_current_value():
    table = m.sweep_table(m.sweep(FAKE_ROWS), current=0.5)
    assert "| 0.50 (current) |" in table
    assert "In-scope wrongly escalated" in table
    assert table.count("\n") == 1 + len(m.THRESHOLDS)  # header, separator, nine rows


def test_marked_sections_can_be_replaced_and_kept():
    report = "# Report\n\nbody\n"
    with_section = m.replace_section(report, "loadtest", "old results")
    assert m.extract_section(with_section, "loadtest") == "old results"
    updated = m.replace_section(with_section, "loadtest", "new results")
    assert m.extract_section(updated, "loadtest") == "new results"
    assert "old results" not in updated
    assert updated.startswith("# Report")
    assert m.extract_section(report, "loadtest") is None


def test_with_retry_does_not_retry_an_error_listed_in_no_retry():
    waits, calls = [], []

    def down():
        calls.append(1)
        raise ConnectionError("already retried inside")

    with pytest.raises(ConnectionError):
        m.with_retry(down, tries=4, base_delay=1, sleep=waits.append, no_retry=(ConnectionError,))
    assert len(calls) == 1 and waits == []


def test_with_retry_still_retries_other_errors_when_no_retry_is_given():
    calls, waits = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 2:
            raise ValueError("x")
        return "ok"

    assert m.with_retry(flaky, tries=3, base_delay=1, sleep=waits.append, no_retry=(ConnectionError,)) == "ok"
    assert waits == [1]
