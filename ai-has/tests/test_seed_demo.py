import ast
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app import main, rag, seed_demo
from app.config import settings
from fake_seed_db import FakeSeedDb

LOCAL = "postgresql+psycopg2://aihas:SECRETPW@db:5432/aihas"
NOW = datetime(2026, 11, 22, 9, 0, 0)  # a naive UTC time, like the database
KB_FOLDER = Path(__file__).resolve().parents[1] / "kb"


@pytest.fixture
def env(monkeypatch):
    db = FakeSeedDb()
    monkeypatch.setattr(seed_demo, "engine", db)
    monkeypatch.setattr(settings, "database_url", LOCAL)
    return db


def run(argv=(), answer="reset", terminal=True, now=NOW):
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return answer

    code = seed_demo.main(list(argv), ask=ask, is_terminal=terminal, now=now)
    return code, asked


# ---------- the guard ----------
@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg2://u:SECRETPW@db.supabase.co:5432/postgres",
        "postgresql+psycopg2://u:SECRETPW@aws-0-eu.pooler.supabase.com:6543/postgres",
        "postgresql+psycopg2://u:SECRETPW@10.0.0.5:5432/x",
        "postgresql+psycopg2://u:SECRETPW@/x",  # no host: libpq would pick one from the environment
        "postgresql+psycopg2://u:SECRETPW@db:5432/x?host=evil.example.com",
        "not a url",
        "",
    ],
)
def test_it_refuses_any_database_that_is_not_local_and_touches_nothing(env, monkeypatch, url):
    monkeypatch.setattr(settings, "database_url", url)
    with pytest.raises(SystemExit) as stop:
        run()
    assert "Refusing to run" in str(stop.value) and "no option to override" in str(stop.value)
    assert env.statements == [] and env.commits == 0


def test_the_refusal_never_shows_the_url_or_the_password(env, monkeypatch):
    monkeypatch.setattr(settings, "database_url", "postgresql+psycopg2://u:SECRETPW@db.supabase.co:5432/postgres")
    with pytest.raises(SystemExit) as stop:
        run()
    assert "SECRETPW" not in str(stop.value) and "postgresql" not in str(stop.value)


@pytest.mark.parametrize("url", [LOCAL, "postgresql+psycopg2://u:p@localhost:5433/x"])
def test_a_local_host_is_allowed(env, monkeypatch, url):
    monkeypatch.setattr(settings, "database_url", url)
    assert run()[0] == 0


@pytest.mark.parametrize("option", ["--allow-remote", "--yes", "--force", "-y"])
def test_there_is_no_override_option_at_all(env, monkeypatch, option, capsys):
    monkeypatch.setattr(settings, "database_url", "postgresql+psycopg2://u:p@db.supabase.co:5432/postgres")
    with pytest.raises(SystemExit) as stop:
        run([option])
    assert stop.value.code == 2  # rejected by the argument parser before anything else
    assert env.statements == [] and "unrecognized arguments" in capsys.readouterr().err


def test_the_override_flag_is_rejected_even_on_a_local_database(env, capsys):
    with pytest.raises(SystemExit) as stop:
        run(["--allow-remote"])
    assert stop.value.code == 2 and env.statements == []


# ---------- --reset ----------
def test_reset_asks_for_the_typed_word_and_only_then_deletes(env):
    code, asked = run(["--reset"], answer="reset")
    assert code == 0 and len(asked) == 1 and '"reset"' in asked[0]
    assert all(s.get("summary") not in ("as", "Linked ticket") for s in env.tickets)
    assert [c["title"] for c in env.conversations if c["id"] == 90] == []
    assert env.pending_actions == []
    assert all(a["action"] != "login" for a in env.audit_logs)


@pytest.mark.parametrize("answer", ["", " ", "no", "RESET", "Reset", "reset now", "y", "yes", "resett", '"reset"'])
def test_anything_but_the_exact_word_cancels_and_changes_nothing(env, answer, capsys):
    before = env.snapshot()
    code, _ = run(["--reset"], answer=answer)
    assert code == 1 and env.snapshot() == before and env.statements == [] and env.commits == 0
    assert "Nothing was changed" in capsys.readouterr().out


def test_the_word_may_have_spaces_around_it(env):
    assert run(["--reset"], answer="  reset \n")[0] == 0


def test_reset_needs_a_terminal_so_a_pipe_cannot_confirm_it(env, capsys):
    before = env.snapshot()
    code, asked = run(["--reset"], terminal=False)
    assert code == 2 and asked == [] and env.snapshot() == before and env.statements == []
    assert "interactive terminal" in capsys.readouterr().out


def test_without_reset_nothing_is_deleted_and_nothing_is_asked(env):
    code, asked = run()
    assert code == 0 and asked == []
    assert not any(s.startswith("delete") for s in env.statements)
    assert {t["summary"] for t in env.tickets} >= {"as", "Linked ticket"}  # the leftovers stay
    assert env.pending_actions == [{"id": "abc"}] and any(a["action"] == "login" for a in env.audit_logs)


def test_reset_deletes_exactly_the_five_tables_in_a_foreign_key_safe_order(env):
    run(["--reset"])
    deletes = [s for s in env.statements if s.startswith("delete")]
    assert deletes == [f"delete from {t}" for t in ("pending_actions", "tickets", "messages", "conversations", "audit_logs")]


def test_users_and_the_knowledge_base_are_never_deleted_or_changed(env):
    users_before = dict(env.users)
    kb_before = (list(env.kb_documents), list(env.kb_chunks))
    run(["--reset"])
    run(["--reset"])
    run()
    assert env.users == users_before and (env.kb_documents, env.kb_chunks) == kb_before
    for sql in env.statements:
        lowered = sql.lower()
        assert not re.search(r"(delete from|truncate|drop|alter|update)\s+(table\s+)?(users|kb_documents|kb_chunks)\b", lowered)
        assert "truncate" not in lowered and "drop " not in lowered
        assert "password" not in lowered  # it never reads or writes a password
        if lowered.startswith("insert"):
            assert not lowered.startswith(("insert into users", "insert into kb_"))


def test_the_fake_refuses_a_delete_from_any_other_table(env):
    """A check on the check: if the script ever tried it, the test would fail loudly."""
    with pytest.raises(AssertionError):
        with env.begin() as conn:
            conn.execute(__import__("sqlalchemy").text("delete from users"))
    with pytest.raises(AssertionError):
        with env.begin() as conn:
            conn.execute(__import__("sqlalchemy").text("delete from kb_chunks"))


def test_the_reset_and_the_new_rows_are_one_transaction(env):
    before = env.snapshot()
    env.fail_on = "insert into tickets"
    with pytest.raises(RuntimeError):
        run(["--reset"])
    assert env.snapshot() == before and env.commits == 0  # nothing deleted, nothing half added


def test_the_output_says_what_was_deleted(env, capsys):
    run(["--reset"])
    out = capsys.readouterr().out
    assert "Deleted: 1 pending_actions, 2 tickets, 1 messages, 1 conversations, 1 audit_logs" in out
    assert "synthetic demo data" in out


# ---------- the data ----------
def seeded(env, now=NOW):
    run(["--reset"], now=now)
    return env


def test_ten_tickets_in_every_status_and_priority_owned_by_the_demo_employee(env):
    seeded(env)
    tickets = env.tickets
    assert len(tickets) == 10 and {t["user_id"] for t in tickets} == {1}
    assert {t["status"] for t in tickets} == {"open", "in_progress", "resolved"}
    assert {t["priority"] for t in tickets} == {"low", "medium", "high"}
    assert {t["source"] for t in tickets} == {"ai", "escalation"}
    for status in ("open", "in_progress", "resolved"):
        assert sum(1 for t in tickets if t["status"] == status) >= 2
    assert len({t["summary"] for t in tickets}) == 10


def test_some_tickets_have_jira_style_keys_and_some_do_not(env):
    seeded(env)
    refs = [t["external_ref"] for t in env.tickets]
    keys = [r for r in refs if r]
    assert 4 <= len(keys) < 10 and all(re.fullmatch(r"HELP-\d{4}", k) for k in keys) and len(set(keys)) == len(keys)
    assert any(r is None for r in refs)


def test_the_tickets_cover_the_topics_the_defense_needs(env):
    seeded(env)
    text = " ".join(t["summary"].lower() for t in env.tickets)
    for word in ("password", "vpn", "printer", "access", "battery", "shared", "wi-fi", "zoom"):
        assert word in text, word


def test_ticket_times_are_utc_over_the_last_14_days_and_make_sense(env):
    seeded(env)
    for t in env.tickets:
        assert t["created_at"].tzinfo is None  # naive UTC, like every column
        assert NOW - timedelta(days=14) <= t["created_at"] < NOW
        if t["status"] == "resolved":
            assert t["created_at"] < t["resolved_at"] < NOW
            assert timedelta(hours=1) <= t["resolved_at"] - t["created_at"] <= timedelta(days=2)
        else:
            assert t["resolved_at"] is None
    assert max(NOW - t["created_at"] for t in env.tickets) > timedelta(days=10)  # spread, not all on one day
    assert len({t["created_at"].date() for t in env.tickets}) >= 8


def test_times_fall_in_a_working_day_for_a_viewer_in_singapore(env):
    seeded(env)
    for t in env.tickets:
        local_hour = (t["created_at"] + timedelta(hours=8)).hour
        assert 8 <= local_hour <= 18, t["summary"]


def test_twenty_exchanges_over_seven_days(env):
    seeded(env)
    users = [m for m in env.messages if m["role"] == "user"]
    bots = [m for m in env.messages if m["role"] == "assistant"]
    assert len(users) == len(bots) == 20 and len(env.conversations) == 17
    for m in env.messages:
        assert NOW - timedelta(days=7) < m["created_at"] < NOW
    assert len({m["created_at"].date() for m in env.messages}) == 7


def test_a_mix_of_answered_and_escalated_with_one_proposed_action(env):
    seeded(env)
    bots = [m for m in env.messages if m["role"] == "assistant"]
    outcomes = {o: sum(1 for m in bots if m["outcome"] == o) for o in {m["outcome"] for m in bots}}
    assert outcomes == {"answered": 15, "escalated": 4, "action_proposed": 1}
    for m in bots:
        assert m["escalated"] is (m["outcome"] == "escalated")
        if m["outcome"] == "escalated":
            assert m["content"] == rag.NOT_FOUND_MESSAGE and m["citations"] == [] and m["confidence"] < 0.2
        if m["outcome"] == "action_proposed":
            assert m["content"] == main.ACTION_MESSAGE and m["intent"] == "password_reset" and m["confidence"] is None
    escalated_conversations = {c["id"] for c in env.conversations if c["status"] == "escalated"}
    assert len(escalated_conversations) == 4
    deflection = outcomes["answered"] / (outcomes["answered"] + outcomes["escalated"])
    assert 0.7 < deflection < 0.9  # believable, not perfect


def test_answers_cite_real_knowledge_base_articles_with_a_confidence(env):
    seeded(env)
    answered = [m for m in env.messages if m["role"] == "assistant" and m["outcome"] == "answered"]
    for m in answered:
        assert m["citations"] and set(m["citations"]) <= seed_demo.CITED_ARTICLES
        assert 0.4 <= m["confidence"] <= 1.0 and m["intent"] in ("how_to", "vpn_issue")
        assert (KB_FOLDER / m["citations"][0]).is_file(), m["citations"]  # a file that exists in kb/ in every checkout
    assert {c for m in answered for c in m["citations"]} >= {"vpn-troubleshooting.md", "password-reset.md", "wifi-troubleshooting.md", "printer-troubleshooting.md"}


def test_latencies_are_two_to_nine_seconds(env):
    seeded(env)
    latencies = [m["latency_ms"] for m in env.messages if m["role"] == "assistant"]
    assert len(latencies) == 20 and all(2000 <= v <= 9000 for v in latencies)
    assert len(set(latencies)) > 10  # varied, not one number
    user_rows = [m for m in env.messages if m["role"] == "user"]
    assert all(m["latency_ms"] is None and m["citations"] is None for m in user_rows)


def test_a_few_thumbs_up_and_one_thumbs_down_only_on_answered_messages(env):
    seeded(env)
    rated = [m for m in env.messages if m["feedback"] is not None]
    ups = [m for m in rated if m["feedback"] == 1]
    downs = [m for m in rated if m["feedback"] == -1]
    assert 3 <= len(ups) <= 6 and len(downs) == 1
    assert all(m["role"] == "assistant" and m["outcome"] == "answered" for m in rated)


def test_answers_have_steps_and_never_ask_for_or_show_a_password_or_personal_data(env):
    seeded(env)
    for m in env.messages:
        assert not re.search(r"[\w.]+@[\w.]+\.\w+", m["content"]) and not re.search(r"\d{3}[ -]\d{3}[ -]\d{4}", m["content"])
        assert "your password is" not in m["content"].lower()
    answered = [m for m in env.messages if m["role"] == "assistant" and m["outcome"] == "answered"]
    assert all(m["content"].startswith("1. ") for m in answered)


def test_two_tickets_come_from_chats_and_follow_their_conversation(env):
    seeded(env)
    linked = [t for t in env.tickets if t["conversation_id"] is not None]
    assert len(linked) == 2
    for t in linked:
        conversation = next(c for c in env.conversations if c["id"] == t["conversation_id"])
        assert conversation["status"] == "escalated"
        last_message = max(m["created_at"] for m in env.messages if m["conversation_id"] == conversation["id"])
        assert last_message < t["created_at"] <= last_message + timedelta(minutes=10)


def test_conversation_titles_come_from_the_first_question_like_in_the_app(env):
    from app.chatlog import title_from

    seeded(env)
    first_questions = {m["content"] for m in env.messages if m["role"] == "user"}
    for c in env.conversations:
        assert any(title_from(q) == c["title"] for q in first_questions)
    assert len({c["title"] for c in env.conversations}) == 17


def test_one_audit_row_marks_it_as_synthetic_with_counts_only(env):
    seeded(env)
    (row,) = env.audit_logs
    assert row == {"actor": "cli", "action": "demo_seeded", "payload": {"synthetic": True, "tickets": 10, "conversations": 17, "messages": 40}}


# ---------- adding without a reset ----------
def test_a_second_run_adds_nothing_and_says_so(env, capsys):
    seeded(env)
    first = env.snapshot()
    capsys.readouterr()
    assert run(now=NOW + timedelta(days=2))[0] == 0
    assert env.snapshot() == first
    assert "Added 0 ticket(s), 0 conversation(s) and 0 message(s). Skipped 27 that were already there." in capsys.readouterr().out


def test_without_reset_it_adds_to_what_is_there_and_leaves_the_rest(env):
    run()
    assert {"as", "Linked ticket"} <= {t["summary"] for t in env.tickets} and len(env.tickets) == 12
    assert len(env.conversations) == 18 and len(env.audit_logs) == 2  # the old login row and the demo marker


def test_a_demo_ticket_that_is_already_there_is_not_duplicated(env):
    env.tickets.append({"id": 5, "summary": "Request for a second monitor stand", "user_id": 1})
    run()
    assert sum(1 for t in env.tickets if t["summary"] == "Request for a second monitor stand") == 1


def test_it_stops_with_a_clear_message_if_the_demo_employee_is_missing(monkeypatch):
    empty = FakeSeedDb(with_employee=False)
    monkeypatch.setattr(seed_demo, "engine", empty)
    monkeypatch.setattr(settings, "database_url", LOCAL)
    before = empty.snapshot()
    with pytest.raises(SystemExit) as stop:
        run(["--reset"])
    assert "employee@example.com" in str(stop.value) and "init_db" in str(stop.value)
    assert empty.snapshot() == before  # nothing was deleted either


@pytest.mark.parametrize("offset_hours", [0, 8, -5])
@pytest.mark.parametrize("hour", [0, 1, 3, 9, 12, 16, 17, 23])
def test_every_saved_question_is_inside_the_last_7_calendar_days_for_a_viewer_in_utc_singapore_or_new_york(env, hour, offset_hours):
    """The dashboard counts days in the viewer's zone. The oldest question must never fall just outside."""
    now = datetime(2026, 11, 22, hour, 30)
    seeded(env, now=now)
    offset = timedelta(hours=offset_hours)
    today = (now + offset).date()
    for m in env.messages:
        local_day = (m["created_at"] + offset).date()
        assert today - timedelta(days=6) <= local_day <= today, (m["content"][:30], local_day, today)


@pytest.mark.parametrize("hour", [0, 1, 3, 12, 23])
def test_no_row_is_ever_dated_in_the_future_whatever_time_it_runs(env, hour):
    now = datetime(2026, 11, 22, hour, 30)
    seeded(env, now=now)
    for t in env.tickets:
        assert t["created_at"] < now and (t["resolved_at"] is None or t["resolved_at"] < now)
    assert all(m["created_at"] < now for m in env.messages)
    assert all(m["created_at"] > now - timedelta(days=8) for m in env.messages)


# ---------- never Gemini, never secrets ----------
def test_the_script_imports_nothing_that_can_call_gemini():
    source = (Path(seed_demo.__file__)).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        if isinstance(node, ast.ImportFrom):
            imported |= {(node.module or "").split(".")[0]} | {a.name for a in node.names}
    assert not imported & {"openai", "llm", "ingest", "rag", "intent", "memory", "kb", "httpx", "requests"}


def test_the_output_never_contains_the_url_a_password_or_a_key(env, capsys):
    run(["--reset"])
    out = capsys.readouterr().out
    for leaked in ("SECRETPW", "postgresql", "aihas:", "AIza", "password_hash"):
        assert leaked not in out
