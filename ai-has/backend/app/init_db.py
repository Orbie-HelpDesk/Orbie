"""Create the schema and seed demo users. Safe to run many times.

Run with: python -m app.init_db
Refuses to run unless DATABASE_URL points at db or localhost (override: --allow-remote).

Only runs CREATE ... IF NOT EXISTS and INSERT ... ON CONFLICT DO NOTHING, so it
never drops, alters or overwrites existing data.

The demo users get the password from DEMO_PASSWORD, only on a local database and only
if they have no password yet. The password is never printed.
"""
from pathlib import Path

from sqlalchemy import text

from .config import settings
from .db import engine
from .passwords import check_password_rules, hash_password
from .safety import is_local_db, require_local_db

SCHEMA_FILE = Path(__file__).resolve().parent.parent / "schema.sql"

DEMO_USERS = [
    {"email": "employee@example.com", "name": "Demo Employee", "role": "employee"},
    {"email": "agent@example.com", "name": "Demo Agent", "role": "agent"},
    {"email": "admin@example.com", "name": "Demo Admin", "role": "admin"},
]


def set_demo_passwords(conn) -> str:
    """Sets DEMO_PASSWORD on demo users that have none. Returns a line to print."""
    if not is_local_db(settings.database_url):
        return "Demo passwords skipped: the database is not local."
    if not settings.demo_password:
        return "Demo passwords not set: DEMO_PASSWORD is empty, so the demo users cannot log in yet."
    problem = check_password_rules(settings.demo_password)
    if problem:
        return f"Demo passwords not set: DEMO_PASSWORD is not acceptable. {problem}"
    changed = 0
    for user in DEMO_USERS:
        result = conn.execute(
            text("update users set password_hash = :h where email = :e and password_hash is null"),
            {"h": hash_password(settings.demo_password), "e": user["email"]},
        )
        changed += result.rowcount
    return f"Demo passwords set for {changed} user(s) that had none."


def init_db():
    schema = SCHEMA_FILE.read_text(encoding="utf-8")
    with engine.begin() as conn:
        # exec_driver_sql runs the whole file as is (psycopg2 accepts several statements)
        conn.exec_driver_sql(schema)
        for user in DEMO_USERS:
            conn.execute(
                text(
                    "insert into users (email, name, role) values (:email, :name, :role) "
                    "on conflict (email) do nothing"
                ),
                user,
            )
        password_note = set_demo_passwords(conn)
    print(f"Schema applied. Demo users ensured: {', '.join(u['email'] for u in DEMO_USERS)}")
    print(password_note)


if __name__ == "__main__":
    require_local_db(settings.database_url)
    init_db()
