import json

import pytest
from fastapi.testclient import TestClient

from app import audit, chatlog, main
from app.ratelimit import limiter
from fake_chat_db import FakeChatDb


@pytest.fixture
def env(monkeypatch, login_as):
    """The conversation routes with a fake table. Users: 1 employee, 2 agent, 3 admin."""
    monkeypatch.setattr(main.settings, "rate_limit_enabled", False)
    limiter.reset()
    db = FakeChatDb()
    monkeypatch.setattr(chatlog, "engine", db)
    monkeypatch.setattr(audit, "engine", db)
    chatlog_save = chatlog.save_exchange

    def talk(user_id, question, conversation_id=None):
        return chatlog_save(user_id, conversation_id, question, f"Answer to {question}",
                            intent="answered", citations=["vpn.md"], confidence=0.8, latency_ms=5)

    state = {"db": db, "talk": talk, "login_as": login_as, "client": TestClient(main.app)}
    yield state
    limiter.reset()


def as_role(env, role):
    env["login_as"](role)
    return env["client"]


def call(client, method, url, **kwargs):
    kwargs.setdefault("json", {})  # every state-changing request must be JSON
    return client.request(method, url, **kwargs)


# ---------- listing ----------
def test_my_conversations_are_mine_only_and_newest_first(env):
    a = env["talk"](1, "first question")["conversation_id"]
    env["talk"](2, "an agent's question")
    b = env["talk"](1, "second question")["conversation_id"]
    env["talk"](1, "a follow up", a)  # the first chat was active most recently
    rows = as_role(env, "employee").get("/conversations").json()
    assert [r["id"] for r in rows] == [a, b]
    assert rows[0]["title"] == "first question" and rows[0]["questions"] == 2


def test_the_list_is_limited_and_validated(env):
    for i in range(3):
        env["talk"](1, f"question {i}")
    client = as_role(env, "employee")
    assert len(client.get("/conversations?limit=2").json()) == 2
    assert client.get("/conversations?limit=0").status_code == 422
    assert client.get("/conversations?limit=101").status_code == 422


def test_staff_list_only_their_own_conversations_too(env):
    env["talk"](1, "an employee's question")
    assert as_role(env, "agent").get("/conversations").json() == []


# ---------- reading ----------
def test_the_owner_reads_the_messages(env):
    cid = env["talk"](1, "My VPN keeps dropping")["conversation_id"]
    body = as_role(env, "employee").get(f"/conversations/{cid}").json()
    assert body["title"] == "My VPN keeps dropping"
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    bot = body["messages"][1]
    assert bot["content"] == "Answer to My VPN keeps dropping" and bot["citations"] == ["vpn.md"]
    assert bot["confidence"] == 0.8 and bot["feedback"] is None
    assert env["db"].audit == []  # reading your own chat is not audited


def test_another_employee_gets_404(env):
    cid = env["talk"](2, "someone else's question")["conversation_id"]  # user 2 owns it
    res = as_role(env, "employee").get(f"/conversations/{cid}")
    assert res.status_code == 404 and res.json() == {"detail": "Conversation not found."}
    assert env["db"].audit == []


def test_an_unknown_conversation_is_404_for_everyone(env):
    assert as_role(env, "employee").get("/conversations/999").status_code == 404
    assert as_role(env, "admin").get("/conversations/999").status_code == 404


@pytest.mark.parametrize("role, actor", [("agent", "user:2"), ("admin", "user:3")])
def test_staff_can_read_anyones_conversation_and_it_is_audited(env, role, actor):
    cid = env["talk"](1, "an employee's private question")["conversation_id"]
    res = as_role(env, role).get(f"/conversations/{cid}")
    assert res.status_code == 200 and res.json()["messages"][0]["content"] == "an employee's private question"
    assert env["db"].audit == [{"actor": actor, "action": "conversation_read", "payload": {"conversation_id": cid}}]
    assert "private question" not in json.dumps(env["db"].audit)


def test_logged_out_requests_get_401(env):
    env["login_as"]("employee")
    main.app.dependency_overrides.clear()  # no session at all
    client = TestClient(main.app)
    assert client.get("/conversations").status_code == 401
    assert client.get("/conversations/1").status_code == 401
    assert call(client, "DELETE", "/conversations/1").status_code == 401
    assert call(client, "PATCH", "/messages/1/feedback", json={"value": 1}).status_code == 401


# ---------- feedback ----------
def test_a_user_can_rate_and_clear_a_rating_on_their_own_answer(env):
    saved = env["talk"](1, "My VPN keeps dropping")
    client = as_role(env, "employee")
    for value in (1, -1, 0):
        res = call(client, "PATCH", f"/messages/{saved['message_id']}/feedback", json={"value": value})
        assert res.status_code == 200 and res.json() == {"message_id": saved["message_id"], "feedback": value}
        assert env["db"].messages[1]["feedback"] == value


@pytest.mark.parametrize("value", [2, -2, "up", None, 1.5])
def test_only_minus_one_zero_and_one_are_accepted(env, value):
    saved = env["talk"](1, "My VPN keeps dropping")
    res = call(as_role(env, "employee"), "PATCH", f"/messages/{saved['message_id']}/feedback", json={"value": value})
    assert res.status_code == 422 and env["db"].messages[1]["feedback"] is None


def test_a_user_cannot_rate_someone_elses_message(env):
    saved = env["talk"](2, "an agent's question")
    res = call(as_role(env, "employee"), "PATCH", f"/messages/{saved['message_id']}/feedback", json={"value": 1})
    assert res.status_code == 404 and env["db"].messages[1]["feedback"] is None


def test_staff_cannot_rate_other_peoples_messages_either(env):
    saved = env["talk"](1, "an employee's question")
    res = call(as_role(env, "admin"), "PATCH", f"/messages/{saved['message_id']}/feedback", json={"value": -1})
    assert res.status_code == 404 and env["db"].messages[1]["feedback"] is None


def test_a_question_cannot_be_rated_only_an_answer(env):
    env["talk"](1, "My VPN keeps dropping")
    user_message_id = env["db"].messages[0]["id"]
    assert call(as_role(env, "employee"), "PATCH", f"/messages/{user_message_id}/feedback", json={"value": 1}).status_code == 404


def test_rating_an_unknown_message_is_404(env):
    assert call(as_role(env, "employee"), "PATCH", "/messages/999/feedback", json={"value": 1}).status_code == 404


# ---------- deleting ----------
def test_the_owner_deletes_a_conversation_and_it_is_audited_without_text(env):
    cid = env["talk"](1, "My secret-ish question")["conversation_id"]
    env["db"].tickets = {10: cid, 11: None}
    res = call(as_role(env, "employee"), "DELETE", f"/conversations/{cid}")
    assert res.status_code == 200 and res.json() == {"status": "deleted"}
    assert cid not in env["db"].conversations and env["db"].messages == []
    assert env["db"].tickets == {10: None, 11: None}  # the ticket stays, unlinked
    assert env["db"].audit == [{"actor": "user:1", "action": "conversation_deleted", "payload": {"conversation_id": cid}}]
    assert "secret-ish" not in json.dumps(env["db"].audit)
    assert as_role(env, "employee").get(f"/conversations/{cid}").status_code == 404


@pytest.mark.parametrize("role", ["employee", "agent", "admin"])
def test_nobody_can_delete_someone_elses_conversation_not_even_an_admin(env, role):
    cid = env["talk"](99, "another user's question")["conversation_id"]
    res = call(as_role(env, role), "DELETE", f"/conversations/{cid}")
    assert res.status_code == 404
    assert cid in env["db"].conversations and len(env["db"].messages) == 2 and env["db"].audit == []


def test_deleting_an_unknown_conversation_is_404_and_not_audited(env):
    assert call(as_role(env, "employee"), "DELETE", "/conversations/999").status_code == 404
    assert env["db"].audit == []


def test_a_delete_must_be_json_like_every_state_change(env):
    cid = env["talk"](1, "q")["conversation_id"]
    as_role(env, "employee")
    assert env["client"].delete(f"/conversations/{cid}").status_code == 415
    assert cid in env["db"].conversations
