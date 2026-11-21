"""Fill the LOCAL database with synthetic demo data, for screenshots and the live defense.

Run with (the URL is the local database, see the README):
    docker compose run --rm -e DATABASE_URL=postgresql+psycopg2://aihas:aihas-local@db:5432/aihas backend python -m app.seed_demo [--reset]

THIS IS SYNTHETIC DEMO DATA. None of it is real usage: the people, problems, answers, ratings and
times are made up for the demo. Do not use it to measure anything or to claim real results.

What it adds (all owned by the demo employee, employee@example.com):
- 10 tickets over the last 14 days, in all three statuses and priorities, some with HELP-nnnn refs
- 20 saved questions and answers over the last 7 days (answered, escalated, one proposed action),
  with citations to real knowledge base article names, confidence and 2 to 9 second latencies,
  6 thumbs up and 1 thumbs down

Rules
- It refuses to run unless the database host is db or localhost. There is NO --allow-remote option
  for this script, and the option is rejected if given.
- Without --reset it only adds data, and a second run adds nothing (it skips what is already there).
- --reset first asks you to type "reset", then deletes the rows of tickets, conversations, messages,
  pending_actions and audit_logs. It never deletes users or the knowledge base.
- It never calls Gemini and never reads or prints a password, a key or the database URL.
"""
import argparse
import json
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from . import audit
from .chatlog import title_from
from .config import settings
from .db import engine
from .safety import SAFE_TO_SHOW, _hosts, is_local_db

DEMO_EMPLOYEE = "employee@example.com"  # the demo user that init_db creates
CONFIRM_WORD = "reset"

# Tables --reset empties, in an order that respects the foreign keys. Never users, never kb_*.
RESET_TABLES = ("pending_actions", "tickets", "messages", "conversations", "audit_logs")

# Texts the app itself uses, so the demo rows look like the real ones
NOT_FOUND_MESSAGE = (
    "I couldn't find this in our IT knowledge base. "
    "Would you like me to escalate this to a human agent?"
)
ACTION_MESSAGE = "I can do that for you. Please review it and confirm below. Nothing happens until you do."

# Only article names that are in the repository's kb/ folder in every checkout
VPN = "vpn-troubleshooting.md"
PASSWORD = "password-reset.md"
WIFI = "wifi-troubleshooting.md"
PRINTER = "printer-troubleshooting.md"
EMAIL = "email-setup.md"
SLOW_PC = "computer-restart-and-slow-pc.md"
ACCESS = "software-access-request.md"
CITED_ARTICLES = frozenset({VPN, PASSWORD, WIFI, PRINTER, EMAIL, SLOW_PC, ACCESS})


# ---------- tickets ----------
# days_ago, hour and minute (UTC: 01 to 09 is the working day in Singapore), priority, status,
# source, summary, Jira-style key, hours until it was resolved, and the conversation it came from.
TICKETS = [
    (13, 2, 10, "high", "resolved", "ai", "Account locked out after too many password attempts", "HELP-4101", 1.5, None),
    (12, 3, 40, "medium", "resolved", "escalation", "VPN disconnects every few minutes when working from home", "HELP-4102", 26, None),
    (10, 6, 5, "low", "resolved", "ai", "Printer on floor 2 shows a paper jam but nothing is stuck", None, 5, None),
    (9, 1, 50, "medium", "resolved", "ai", "Need access to the Figma workspace for the new design project", "HELP-4105", 20, None),
    (7, 7, 30, "low", "in_progress", "escalation", "Laptop battery drains from 100 to 20 percent in under two hours", "HELP-4108", None, None),
    (5, 2, 25, "high", "in_progress", "ai", "Cannot open the shared Finance drive, access denied", "HELP-4110", None, None),
    (4, None, None, "medium", "resolved", "escalation", "Cracked laptop screen needs a replacement", "HELP-4111", 30, "cracked-screen"),
    (3, None, None, "low", "open", "escalation", "Request for a second monitor stand", None, None, "monitor-stand"),
    (2, 5, 45, "high", "open", "ai", "Zoom meeting audio cuts out for everyone on the team", "HELP-4113", None, None),
    (1, 1, 35, "medium", "in_progress", "ai", "Wi-Fi is very slow in the meeting room on the 3rd floor", None, None, None),
]
# A ticket made from a chat is created a few minutes after the last answer of that conversation
TICKET_AFTER_CHAT = timedelta(minutes=3)


# ---------- saved questions and answers ----------
# Each conversation: key, days_ago, hour, minute (UTC), and its exchanges. An exchange is
# (question, answer, intent, outcome, sources, confidence, latency_ms, feedback).
def _ex(question, answer, intent, outcome, sources, confidence, latency_ms, feedback=None):
    return {"q": question, "a": answer, "intent": intent, "outcome": outcome, "sources": sources,
            "confidence": confidence, "latency_ms": latency_ms, "feedback": feedback}


CONVERSATIONS = [
    ("forgot-password", 6, 5, 5, [
        _ex("I forgot my password and cannot log in", ACTION_MESSAGE, "password_reset", "action_proposed", [], None, 2300),
    ]),
    ("vpn-drops", 6, 6, 30, [
        _ex("My VPN keeps disconnecting every few minutes",
            "1. Check that your internet connection is stable.\n2. Close the VPN client completely and open it again.\n"
            "3. Choose a different VPN server if the client offers one.\n4. Restart your laptop and connect again.",
            "vpn_issue", "answered", [VPN], 0.86, 4200, 1),
        _ex("it still doesn't work after restarting",
            "1. Make sure the VPN client is the latest version.\n2. Try another network, such as a phone hotspot.\n"
            "3. If it still fails, create a ticket so the IT team can check your account.",
            "vpn_issue", "answered", [VPN], 0.78, 5100),
    ]),
    ("office-wifi", 5, 6, 20, [
        _ex("How do I connect to the office Wi-Fi?",
            "1. Open the Wi-Fi list on your laptop.\n2. Choose the company network.\n3. Sign in with your work account.\n"
            "4. If it asks for a certificate, accept it.",
            "how_to", "answered", [WIFI], 0.74, 3600, 1),
    ]),
    ("printer-jam", 5, 7, 45, [
        _ex("my printer says paper jam",
            "1. Turn the printer off and open all the covers.\n2. Pull the jammed sheet out slowly in the direction of the paper path.\n"
            "3. Close the covers, turn the printer on and print a test page.",
            "how_to", "answered", [PRINTER], 0.81, 4800),
    ]),
    ("cracked-screen", 4, 1, 40, [
        _ex("My laptop screen is cracked, can I get a replacement?", NOT_FOUND_MESSAGE, "how_to", "escalated", [], 0.0, 2600),
    ]),
    ("slow-laptop", 4, 5, 10, [
        _ex("My laptop is very slow after the update",
            "1. Restart the laptop. A restart finishes updates and clears memory.\n2. Close programs you are not using.\n"
            "3. Check that at least 10 percent of the disk is free.",
            "how_to", "answered", [SLOW_PC], 0.69, 6900),
        _ex("how do I restart it properly",
            "1. Save your work.\n2. Choose Start, then Power, then Restart (not Shut down).\n3. Wait until the sign-in screen appears.",
            "how_to", "answered", [SLOW_PC], 0.71, 4400),
    ]),
    ("slack-access", 3, 2, 55, [
        _ex("How do I request access to Slack?",
            "1. Open the software access request form.\n2. Choose Slack and add your manager as the approver.\n"
            "3. Submit it. You get an email when access is approved.",
            "how_to", "answered", [ACCESS], 0.88, 3900, 1),
    ]),
    ("monitor-stand", 3, 8, 30, [
        _ex("Can I get a second monitor stand for my desk?", NOT_FOUND_MESSAGE, "how_to", "escalated", [], 0.05, 3000),
    ]),
    ("work-email-phone", 2, 1, 20, [
        _ex("How do I set up my work email on my phone?",
            "1. Open the mail app on your phone and add an account.\n2. Enter your work email address.\n"
            "3. Sign in with your work account and approve the sign-in request.",
            "how_to", "answered", [EMAIL], 0.91, 3100, 1),
    ]),
    ("vpn-auth-failed", 2, 6, 40, [
        _ex("My VPN says authentication failed",
            "1. Check that you typed your work email and password correctly.\n2. If you changed your password recently, sign out and sign in again.\n"
            "3. If it still fails, ask for a password reset.",
            "vpn_issue", "answered", [VPN, PASSWORD], 0.52, 8700, -1),
    ]),
    ("meeting-room-wifi", 1, 3, 15, [
        _ex("wifi keeps dropping in the meeting room",
            "1. Forget the network and connect to it again.\n2. Move closer to the access point and check the signal.\n"
            "3. Turn the Wi-Fi off and on again.",
            "how_to", "answered", [WIFI], 0.74, 3800),
        _ex("it only happens in that room",
            "1. Note which room and what time it happens.\n2. Try the other company network if the room shows two.\n"
            "3. Create a ticket with the room name so the IT team can check the access point.",
            "how_to", "answered", [WIFI], 0.61, 5200),
    ]),
    ("linux-laptop", 1, 8, 5, [
        _ex("Can IT install Linux on my work laptop?", NOT_FOUND_MESSAGE, "how_to", "escalated", [], 0.0, 2900),
    ]),
    ("reset-password-how", 0, 2, 10, [
        _ex("How do I reset my password?",
            "1. Open the sign-in page and choose Forgot password.\n2. Enter your work email address.\n"
            "3. Follow the link in the email to choose a new password.",
            "how_to", "answered", [PASSWORD], 0.93, 2900, 1),
    ]),
    ("printer-offline", 0, 3, 25, [
        _ex("my printer is offline",
            "1. Check that the printer is on and connected to the network.\n2. Restart the printer.\n"
            "3. On your laptop, remove the printer and add it again.",
            "how_to", "answered", [PRINTER], 0.77, 5600),
    ]),
    ("photoshop-license", 0, 4, 50, [
        _ex("How can I get a license for Photoshop?",
            "1. Open the software access request form.\n2. Choose Photoshop and explain what you need it for.\n"
            "3. Your manager approves it, then IT installs it.",
            "how_to", "answered", [ACCESS], 0.84, 4100, 1),
    ]),
    ("outlook-sync", 0, 5, 30, [
        _ex("Outlook does not sync on my phone",
            "1. Check that your phone has internet access.\n2. Remove the work account from the mail app and add it again.\n"
            "3. Sign in and approve the sign-in request.",
            "how_to", "answered", [EMAIL], 0.72, 7400),
    ]),
    ("usb-hub", 0, 6, 5, [
        _ex("Where can I buy a USB-C hub for my laptop?", NOT_FOUND_MESSAGE, "how_to", "escalated", [], 0.1, 2700),
    ]),
]
EXCHANGE_GAP = timedelta(minutes=5)  # between a question and the follow-up in the same conversation


# ---------- the guard ----------
def ensure_local(database_url: str) -> None:
    """Exit unless every database host is db or localhost. There is no override."""
    if is_local_db(database_url):
        return
    hosts = _hosts(database_url)
    bad = next((h for h in hosts if h not in ("db", "localhost")), None)
    shown = bad if bad and SAFE_TO_SHOW.match(bad) else "(missing, invalid or not a plain hostname)"
    raise SystemExit(
        f"Refusing to run: DATABASE_URL host is {shown}, not db or localhost.\n"
        "This script only fills a local database with demo data. It has no option to override that."
    )


# ---------- building the rows ----------
HOME_UTC_OFFSET = timedelta(hours=8)  # the demo viewer is in Singapore, so "N days ago" counts Singapore days


def _when(now: datetime, days_ago: int, hour: int, minute: int) -> datetime:
    """A UTC time (naive, like the database) N Singapore days ago. Never later than five minutes ago.

    Counting days in Singapore keeps the oldest saved question inside the dashboard's "last 7 days"
    for a viewer in Singapore and for one in UTC.
    """
    home = now + HOME_UTC_OFFSET
    moment = datetime(home.year, home.month, home.day, hour, minute) - timedelta(days=days_ago)
    if moment > now - timedelta(minutes=5):
        moment -= timedelta(days=1)
    return moment


def build_conversations(now: datetime) -> list[dict]:
    """The conversations with their exchanges and the time of each message."""
    built = []
    for key, days_ago, hour, minute, exchanges in CONVERSATIONS:
        start = _when(now, days_ago, hour, minute)
        items = []
        for i, ex in enumerate(exchanges):
            asked = start + i * EXCHANGE_GAP
            items.append({**ex, "asked_at": asked, "answered_at": asked + timedelta(milliseconds=ex["latency_ms"])})
        built.append({"key": key, "title": title_from(exchanges[0]["q"]), "started": start, "exchanges": items})
    return built


def build_tickets(now: datetime, conversations: list[dict]) -> list[dict]:
    last_answer = {c["key"]: c["exchanges"][-1]["answered_at"] for c in conversations}
    built = []
    for days_ago, hour, minute, priority, status, source, summary, ref, hours, chat in TICKETS:
        if chat:
            created = min(last_answer[chat] + TICKET_AFTER_CHAT, now - timedelta(minutes=1))
        else:
            created = _when(now, days_ago, hour, minute)
        resolved = min(created + timedelta(hours=hours), now - timedelta(minutes=1)) if status == "resolved" else None
        built.append({
            "summary": summary, "priority": priority, "status": status, "source": source,
            "external_ref": ref, "created_at": created, "resolved_at": resolved, "chat": chat,
        })
    return built


# ---------- writing ----------
def _employee_id(conn) -> int:
    user_id = conn.execute(text("select id from users where email = :e"), {"e": DEMO_EMPLOYEE}).scalar()
    if user_id is None:
        raise SystemExit(f"The demo employee {DEMO_EMPLOYEE} does not exist. Run python -m app.init_db first.")
    return user_id


def reset(conn) -> dict[str, int]:
    """Deletes the rows of the demo tables. Never users, never the knowledge base."""
    counts = {}
    for table in RESET_TABLES:
        counts[table] = conn.execute(text(f"delete from {table}")).rowcount  # the names are the constants above
    return counts


def add_conversation(conn, user_id: int, conv: dict) -> tuple[int, int | None]:
    """Adds one conversation with its messages. Returns (id, number of messages), or (id, None) if it was there."""
    found = conn.execute(
        text("select id from conversations where user_id = :u and title = :t"), {"u": user_id, "t": conv["title"]}
    ).scalar()
    if found is not None:
        return found, None
    escalated = any(ex["outcome"] == "escalated" for ex in conv["exchanges"])
    conv_id = conn.execute(
        text(
            "insert into conversations (user_id, status, title, created_at) "
            "values (:u, :s, :t, :c) returning id"
        ),
        {"u": user_id, "s": "escalated" if escalated else "open", "t": conv["title"], "c": conv["started"]},
    ).scalar()
    insert = text(
        "insert into messages (conversation_id, role, content, intent, outcome, citations, confidence, "
        "latency_ms, feedback, escalated, blocked, created_at) "
        "values (:c, :r, :t, :i, :o, cast(:ci as json), :cf, :l, :f, :e, false, :at)"
    )
    count = 0
    for ex in conv["exchanges"]:
        conn.execute(insert, {
            "c": conv_id, "r": "user", "t": ex["q"], "i": None, "o": None, "ci": None, "cf": None,
            "l": None, "f": None, "e": False, "at": ex["asked_at"],
        })
        conn.execute(insert, {
            "c": conv_id, "r": "assistant", "t": ex["a"], "i": ex["intent"], "o": ex["outcome"],
            "ci": json.dumps(ex["sources"]), "cf": ex["confidence"], "l": ex["latency_ms"], "f": ex["feedback"],
            "e": ex["outcome"] == "escalated", "at": ex["answered_at"],
        })
        count += 2
    return conv_id, count


def add_ticket(conn, user_id: int, ticket: dict, conversation_id: int | None) -> bool:
    """Adds one ticket unless one with the same summary is already there. Returns True if it was added."""
    found = conn.execute(
        text("select id from tickets where user_id = :u and summary = :s"), {"u": user_id, "s": ticket["summary"]}
    ).scalar()
    if found is not None:
        return False
    conn.execute(
        text(
            "insert into tickets (conversation_id, user_id, summary, priority, status, source, external_ref, "
            "created_at, resolved_at) values (:c, :u, :s, :p, :st, :src, :r, :ca, :ra)"
        ),
        {
            "c": conversation_id, "u": user_id, "s": ticket["summary"], "p": ticket["priority"],
            "st": ticket["status"], "src": ticket["source"], "r": ticket["external_ref"],
            "ca": ticket["created_at"], "ra": ticket["resolved_at"],
        },
    )
    return True


def seed(conn, now: datetime, do_reset: bool) -> dict:
    """Everything in the caller's transaction, so a failure leaves nothing half done."""
    user_id = _employee_id(conn)
    result = {"deleted": reset(conn) if do_reset else {}, "conversations": 0, "messages": 0, "tickets": 0, "skipped": 0}

    conversations = build_conversations(now)
    ids = {}
    for conv in conversations:
        conv_id, messages = add_conversation(conn, user_id, conv)
        ids[conv["key"]] = conv_id
        if messages is None:
            result["skipped"] += 1
        else:
            result["conversations"] += 1
            result["messages"] += messages
    for ticket in build_tickets(now, conversations):
        if add_ticket(conn, user_id, ticket, ids.get(ticket["chat"])):
            result["tickets"] += 1
        else:
            result["skipped"] += 1

    if result["tickets"] or result["conversations"]:
        audit.record(
            "cli", "demo_seeded",
            {"synthetic": True, "tickets": result["tickets"], "conversations": result["conversations"],
             "messages": result["messages"]},
            conn=conn,
        )
    return result


def main(argv: list[str] | None = None, ask=input, is_terminal=None, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description="Add synthetic demo data to the LOCAL database.")
    parser.add_argument("--reset", action="store_true", help='first delete tickets, conversations, messages, pending actions and audit logs (asks you to type "reset")')
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)  # any other option, such as --allow-remote, is an error

    ensure_local(settings.database_url)

    if args.reset:
        if is_terminal is None:
            is_terminal = sys.stdin.isatty()
        if not is_terminal:
            print("--reset needs an interactive terminal, so you can type the confirmation. Nothing was changed.")
            return 2
        print(
            "This deletes ALL rows of tickets, conversations, messages, pending_actions and audit_logs "
            "in the local database. Users and the knowledge base are not touched."
        )
        if ask(f'Type "{CONFIRM_WORD}" to continue: ').strip() != CONFIRM_WORD:
            print("Not confirmed. Nothing was changed.")
            return 1

    now = now or datetime.now(UTC).replace(tzinfo=None)
    with engine.begin() as conn:
        result = seed(conn, now, args.reset)

    if args.reset:
        print("Deleted: " + ", ".join(f"{n} {table}" for table, n in result["deleted"].items()))
    print(
        f"Added {result['tickets']} ticket(s), {result['conversations']} conversation(s) and "
        f"{result['messages']} message(s). Skipped {result['skipped']} that were already there."
    )
    print("This is synthetic demo data, not real usage.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
