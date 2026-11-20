import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARD = ROOT / "backend" / "app" / "eval_data" / "hard_questions.json"
KB_FILES = {p.name for p in (ROOT / "kb").glob("*.md")}

CATEGORIES = {
    "typo_slang", "taglish", "very_short", "two_problems", "cross_topic",
    "near_miss_out_of_scope", "injection", "pii", "benign_not_blocked",
}


def load():
    return json.loads(HARD.read_text(encoding="utf-8"))


def test_hard_set_has_at_least_40_questions_in_every_category():
    cases = load()
    assert len(cases) >= 40
    assert {c["category"] for c in cases} == CATEGORIES


def test_ids_are_unique_and_questions_not_empty():
    cases = load()
    assert len({c["id"] for c in cases}) == len(cases)
    assert all(c["question"].strip() for c in cases)


def test_every_case_has_exactly_one_kind_of_expectation():
    for c in load():
        if "guardrail" in c:
            assert "in_scope" not in c
            assert set(c["guardrail"]) in ({"expect_blocked"}, {"expect_redacted"})
        elif c["in_scope"]:
            expected = c["expected_source"]
            sources = expected if isinstance(expected, list) else [expected]
            assert sources and set(sources) <= KB_FILES, c["id"]
        else:
            assert c["expected_source"] is None


def test_set_covers_the_requested_difficulty_types():
    cases = load()
    two = [c for c in cases if c["category"] == "two_problems"]
    assert all(isinstance(c["expected_source"], list) and len(c["expected_source"]) == 2 for c in two)
    assert any(len(c["question"].split()) == 1 for c in cases if c["category"] == "very_short")
    assert sum(1 for c in cases if c["category"] == "near_miss_out_of_scope") >= 5
