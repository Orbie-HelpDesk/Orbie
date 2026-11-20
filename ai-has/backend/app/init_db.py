"""Create the schema and seed demo users. Safe to run many times.

Run with: python -m app.init_db
Refuses to run unless DATABASE_URL points at db or localhost (override: --allow-remote).

Only runs CREATE ... IF NOT EXISTS and INSERT ... ON CONFLICT DO NOTHING, so it
never drops, alters or overwrites existing data.
"""
from pathlib import Path

from sqlalchemy import text

from .config import settings
from .db import engine
from .safety import require_local_db

SCHEMA_FILE = Path(__file__).resolve().parent.parent / "schema.sql"

DEMO_USERS = [
    {"email": "employee@example.com", "name": "Demo Employee", "role": "employee"},
    {"email": "agent@example.com", "name": "Demo Agent", "role": "agent"},
    {"email": "admin@example.com", "name": "Demo Admin", "role": "admin"},
]


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
    print(f"Schema applied. Demo users ensured: {', '.join(u['email'] for u in DEMO_USERS)}")


if __name__ == "__main__":
    require_local_db(settings.database_url)
    init_db()
