"""Set or change the password of one existing user.

Run with: docker compose run --rm backend python -m app.set_password EMAIL

The password is typed at a hidden prompt (getpass), twice. It is never read from an argument,
an environment variable or a pipe, so it does not end up in shell history or process lists.
It needs an interactive terminal. The user must already exist, and nothing is created.

Refuses to run unless DATABASE_URL points at db or localhost (override: --allow-remote, only
when the team lead has said to change passwords on Supabase).
"""
import argparse
import getpass
import sys

from sqlalchemy import text

from . import audit
from .config import settings
from .db import engine
from .passwords import check_password_rules, hash_password
from .safety import ALLOW_FLAG, require_local_db


def set_password(email: str, password: str) -> int | None:
    """Stores the hash for this user. Returns the user id, or None if there is no such user."""
    with engine.begin() as conn:
        user_id = conn.execute(
            text("update users set password_hash = :h where email = :e returning id"),
            {"h": hash_password(password), "e": email.strip().lower()},
        ).scalar()
        if user_id is not None:
            audit.record("cli", "password_set", {"user_id": user_id}, conn=conn)
    return user_id


def main(argv: list[str] | None = None, ask=getpass.getpass, is_terminal=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(description="Set the password of an existing user (typed at a hidden prompt).")
    parser.add_argument("email", help="email of the user")
    parser.add_argument(ALLOW_FLAG, action="store_true", help="allow a database that is not db or localhost")
    args = parser.parse_args(argv)

    require_local_db(settings.database_url, argv)

    if is_terminal is None:
        is_terminal = sys.stdin.isatty()
    if not is_terminal:
        print("This needs an interactive terminal, so the password can be typed hidden. Nothing was changed.")
        return 2

    password = ask("New password: ")
    problem = check_password_rules(password)
    if problem:
        print(f"{problem} Nothing was changed.")
        return 1
    if ask("Repeat the password: ") != password:
        print("The two passwords do not match. Nothing was changed.")
        return 1

    user_id = set_password(args.email, password)
    if user_id is None:
        print("No user with that email. Nothing was changed.")
        return 1
    print(f"Password updated for user id {user_id}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
