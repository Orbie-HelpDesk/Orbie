import os

# Dummy settings so the app can be imported without a real .env.
# The URL is never connected to: tests stub the database engine.
os.environ["DATABASE_URL"] = "postgresql+psycopg2://test:test@localhost:1/test"
os.environ["GEMINI_API_KEY"] = "test-key"


import pytest  # noqa: E402

USERS = {
    "employee": {"id": 1, "email": "employee@example.com", "name": "Demo Employee", "role": "employee"},
    "agent": {"id": 2, "email": "agent@example.com", "name": "Demo Agent", "role": "agent"},
    "admin": {"id": 3, "email": "admin@example.com", "name": "Demo Admin", "role": "admin"},
}


@pytest.fixture
def login_as():
    """login_as("agent") makes every request in the test come from that user. The role rules still apply."""
    from app import main
    from app.auth import current_user

    def _set(role="employee"):
        main.app.dependency_overrides[current_user] = lambda: USERS[role]
        return USERS[role]

    yield _set
    main.app.dependency_overrides.pop(current_user, None)


@pytest.fixture(autouse=True)
def no_chat_saving_by_default(monkeypatch):
    """Tests never reach a database. The tests about saving switch this on and stub the database."""
    from app.config import settings

    monkeypatch.setattr(settings, "persist_chats", False)
