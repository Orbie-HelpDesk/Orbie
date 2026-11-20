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


# Password made of / ? # @ and letters, URL-encoded as it should be written
ENCODED_PW = "p%2F%3F%23%40w"
# The same characters written raw, which is a malformed URL
RAW_PW = "p/?#@w"


def test_host_is_read_correctly_with_url_encoded_special_chars():
    require_local_db(f"postgresql+psycopg2://u:{ENCODED_PW}@db:5432/x", argv=[])
    require_local_db(f"postgresql+psycopg2://u:{ENCODED_PW}@localhost:5432/x", argv=[])
    with pytest.raises(SystemExit) as exc:
        require_local_db(f"postgresql+psycopg2://u:{ENCODED_PW}@db.abc.supabase.co:5432/x", argv=[])
    assert "db.abc.supabase.co" in str(exc.value)


@pytest.mark.parametrize("host", ["db", "localhost", "db.abc.supabase.co"])
def test_unencoded_special_chars_are_never_allowed_through(host):
    # SQLAlchemy reads a host like "w@db" here, which is the host the engine would use,
    # so the guard refuses it instead of guessing what was meant.
    with pytest.raises(SystemExit):
        require_local_db(f"postgresql+psycopg2://u:{RAW_PW}@{host}:5432/x", argv=[])


def test_unencoded_slash_question_hash_without_at_sign_still_reads_host():
    require_local_db("postgresql+psycopg2://u:p/?#w@db:5432/x", argv=[])


@pytest.mark.parametrize(
    "url",
    [
        f"postgresql+psycopg2://u:{ENCODED_PW}@db.abc.supabase.co:5432/x",
        f"postgresql+psycopg2://u:{RAW_PW}@db.abc.supabase.co:5432/x",
        f"postgresql+psycopg2://u:{RAW_PW}@db:5432/x",
        f"postgresql+psycopg2://u:{ENCODED_PW}@db:5432/x?host=evil.example.com",
        "not a url, secret-pw",
    ],
)
def test_error_message_never_contains_url_or_password(url):
    with pytest.raises(SystemExit) as exc:
        require_local_db(url, argv=[])
    message = str(exc.value)
    assert "postgresql" not in message
    assert "://" not in message
    for secret in (ENCODED_PW, RAW_PW, "p/?#", "@w", "w@", "secret-pw"):
        assert secret not in message


@pytest.mark.parametrize("key", ["host", "hostaddr"])
def test_query_string_host_override_is_checked(key):
    # psycopg2 connects to ?host=..., not the host in the URL, so the guard must check it too
    with pytest.raises(SystemExit) as exc:
        require_local_db(f"postgresql+psycopg2://u:pw@db:5432/x?{key}=evil.example.com", argv=[])
    assert "evil.example.com" in str(exc.value)
    require_local_db(f"postgresql+psycopg2://u:pw@db:5432/x?{key}=localhost", argv=[])
    with pytest.raises(SystemExit):
        require_local_db(f"postgresql+psycopg2://u:pw@db:5432/x?{key}=db&{key}=evil.example.com", argv=[])


@pytest.mark.parametrize(
    "url",
    [
        "postgresql:///dbname",
        "postgresql://u:secret-pw@/dbname",
        "postgresql://u:secret-pw@:5432/dbname",
        "postgresql+psycopg2://u:secret-pw@/x?host=localhost",
    ],
)
def test_empty_or_missing_host_is_refused_without_echoing_url(url):
    # libpq would fall back to PGHOST here, so the real target is unknown
    with pytest.raises(SystemExit) as exc:
        require_local_db(url, argv=[])
    message = str(exc.value)
    assert "empty host" in message
    assert "PGHOST" in message
    assert "postgresql" not in message
    assert "://" not in message
    assert "secret-pw" not in message
    assert "dbname" not in message


def test_blank_query_host_is_ignored_by_the_engine_so_url_host_decides():
    # make_url drops blank query values, so the engine connects to the URL host (db)
    require_local_db("postgresql+psycopg2://u:pw@db:5432/x?host=", argv=[])


def test_allow_remote_flag_still_overrides_empty_host():
    require_local_db("postgresql:///dbname", argv=["--allow-remote"])
