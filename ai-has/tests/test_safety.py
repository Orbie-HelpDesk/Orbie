import pytest

from app.safety import require_local_db

REMOTE = "postgresql+psycopg2://user:pw@db.abcdefgh.supabase.co:5432/postgres"


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg2://aihas:x@db:5432/aihas",
        "postgresql+psycopg2://aihas:x@localhost:5433/aihas",
    ],
)
def test_local_hosts_are_allowed(url):
    require_local_db(url, argv=[])


def test_remote_host_is_refused_with_clear_message():
    with pytest.raises(SystemExit) as exc:
        require_local_db(REMOTE, argv=[])
    message = str(exc.value)
    assert "db.abcdefgh.supabase.co" in message
    assert "--allow-remote" in message
    assert "pw" not in message  # never print the password


def test_allow_remote_flag_overrides():
    require_local_db(REMOTE, argv=["--allow-remote"])


@pytest.mark.parametrize("url", ["", "ask-the-team-lead-for-this"])
def test_missing_or_invalid_url_is_refused(url):
    with pytest.raises(SystemExit):
        require_local_db(url, argv=[])
