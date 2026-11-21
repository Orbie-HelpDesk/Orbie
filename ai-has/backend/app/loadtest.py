"""Load test for POST /chat. Standard library plus httpx.

    python -m app.loadtest                                  # mock mode: no Gemini, no database
    python -m app.loadtest --mode retrieval-only -n 50 -c 5 --yes
    LOADTEST_PASSWORD=... python -m app.loadtest --mode full --url http://localhost:8000 --user EMAIL -n 30 -c 3 --yes

Modes (the first lines of the output always say which one ran):
  mock            runs the app in-process with the answer step replaced by a stub. No Gemini calls and
                  no database. It measures the app's own overhead (guardrails, middleware, JSON) only.
  retrieval-only  runs the app in-process with the real embedding and database search but no chat
                  model. Spends one embedding call per request. Needs DATABASE_URL.
  full            sends real requests to a running server (--url). Spends one embedding call and up to
                  one chat call per request. Start the server with RATE_LIMIT_ENABLED=false, or the
                  rate limit will answer 429 (counted separately below). Needs a login: --user EMAIL
                  and the password in the LOADTEST_PASSWORD environment variable.

In-process modes switch the rate limit off, because the test would otherwise measure the limiter.
Modes that spend Gemini quota refuse to run without --yes. In-process modes never write conversations.
In full mode start the server with PERSIST_CHATS=false, or every request is saved as a conversation.
"""
import argparse
import asyncio
import datetime
import json
import os
import statistics
import time
from pathlib import Path

import httpx

from .eval_metrics import percentile, replace_section

DATA = Path(__file__).parent / "eval_data"
DEFAULT_REPORT = DATA / "report.md"
MODES = ("mock", "retrieval-only", "full")
# Who the in-process modes act as. It never touches the database.
LOADTEST_USER = {"id": 0, "email": "loadtest@example.com", "name": "Load test", "role": "employee"}


def load_questions(which: str = "all") -> list[str]:
    """Questions from the evaluation sets (guardrail test messages are left out)."""
    questions: list[str] = []
    if which in ("generated", "all"):
        questions += [c["question"] for c in json.loads((DATA / "questions.json").read_text(encoding="utf-8"))]
    if which in ("hard", "all"):
        hard = json.loads((DATA / "hard_questions.json").read_text(encoding="utf-8"))
        questions += [c["question"] for c in hard if "guardrail" not in c]
    return questions


def build_plan(n: int, questions: list[str]) -> list[str]:
    """n questions, cycling through the list."""
    return [questions[i % len(questions)] for i in range(n)]


def banner(mode: str, n: int, url: str) -> str:
    if mode == "mock":
        return (
            "MODE: MOCK. No Gemini calls and no database. The answer step is a stub, so this measures "
            "the app's own overhead only, not real answer speed."
        )
    if mode == "retrieval-only":
        return (
            f"MODE: RETRIEVAL-ONLY. Real embedding and database search, no chat model. "
            f"Spends {n} Gemini embedding calls."
        )
    return (
        f"MODE: FULL against {url}. Real answers. Spends up to {n} embedding calls and {n} chat calls "
        f"({2 * n} in total)."
    )


def summarize(results: list[tuple[int | None, float]], duration: float) -> dict:
    """results: (HTTP status or None for a connection error, latency in seconds) per request."""
    n = len(results)
    ok = [lat for status, lat in results if status == 200]
    limited = sum(1 for status, _ in results if status == 429)
    errors = sum(1 for status, _ in results if status != 200 and status != 429)
    return {
        "requests": n,
        "ok": len(ok),
        "rate_limited": limited,
        "errors": errors,
        "error_rate": (n - len(ok)) / n if n else 0.0,
        "duration": duration,
        "rps": n / duration if duration > 0 else 0.0,
        "median": statistics.median(ok) if ok else None,
        "p95": percentile(ok, 95),
        "p99": percentile(ok, 99),
    }


def _seconds(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 1000:.0f} ms" if value < 1 else f"{value:.2f} s"


def format_summary(s: dict) -> str:
    return "\n".join(
        [
            f"Requests: {s['requests']} in {s['duration']:.2f}s",
            f"Requests per second: {s['rps']:.1f}",
            f"Error rate: {100 * s['error_rate']:.1f}% (200 OK: {s['ok']}, 429 rate limited: "
            f"{s['rate_limited']}, other errors: {s['errors']})",
            f"Latency of successful requests: median {_seconds(s['median'])}, "
            f"p95 {_seconds(s['p95'])}, p99 {_seconds(s['p99'])}",
        ]
    )


def report_section(mode: str, banner_text: str, s: dict, n: int, c: int, date: str) -> str:
    return "\n".join(
        [
            "## Load test",
            "",
            f"Date: {date} | Mode: **{mode}** | Requests: {n} | Concurrent workers: {c}",
            "",
            banner_text,
            "",
            "| Metric | Result |",
            "|---|---|",
            f"| Requests per second | {s['rps']:.1f} |",
            f"| Error rate | {100 * s['error_rate']:.1f}% (200 OK {s['ok']}, 429 {s['rate_limited']}, other {s['errors']}) |",
            f"| Latency median | {_seconds(s['median'])} |",
            f"| Latency p95 | {_seconds(s['p95'])} |",
            f"| Latency p99 | {_seconds(s['p99'])} |",
            "",
            "Latencies are for successful requests only. A mock run says nothing about Gemini speed.",
        ]
    )


async def run_requests(client: httpx.AsyncClient, plan: list[str], concurrency: int):
    """Send every question with at most `concurrency` requests in flight."""
    gate = asyncio.Semaphore(concurrency)
    results: list[tuple[int | None, float]] = []

    async def one(question: str):
        async with gate:
            t0 = time.perf_counter()
            try:
                res = await client.post("/chat", json={"message": question})
                status = res.status_code
            except httpx.HTTPError:
                status = None
            results.append((status, time.perf_counter() - t0))

    started = time.perf_counter()
    await asyncio.gather(*(one(q) for q in plan))
    return results, time.perf_counter() - started


def _stub_answer(mock_latency_ms: float):
    def answer(question: str, chunks=None) -> dict:
        if mock_latency_ms:
            time.sleep(mock_latency_ms / 1000)
        return {"answer": "mock answer", "citations": [], "escalate": False, "distances": []}

    return answer


def _retrieval_only_answer():
    from .rag import MAX_DISTANCE, NOT_FOUND_MESSAGE, retrieve

    def answer(question: str, chunks=None) -> dict:
        found = retrieve(question)
        best = min((c["distance"] for c in found), default=None)
        escalate = best is None or best > MAX_DISTANCE
        return {
            "answer": NOT_FOUND_MESSAGE if escalate else "retrieval only, no chat model",
            "citations": [] if escalate else sorted({c["metadata"].get("source") for c in found}),
            "escalate": escalate,
            "distances": [round(c["distance"], 3) for c in found],
        }

    return answer


async def run_in_process(mode: str, plan: list[str], concurrency: int, mock_latency_ms: float):
    """Run the real app through an ASGI transport, with the answer step replaced."""
    from . import main as app_main
    from .auth import current_user
    from .config import settings

    saved = (app_main.answer, app_main.propose_action, settings.rate_limit_enabled, settings.persist_chats)
    app_main.answer = _stub_answer(mock_latency_ms) if mode == "mock" else _retrieval_only_answer()
    # Both in-process modes promise no chat model call, so the action step is off too.
    app_main.propose_action = lambda question: None
    settings.rate_limit_enabled = False
    settings.persist_chats = False  # the test must not write conversations
    # The route needs a logged-in user. Here it is a fixed one, so no database is used.
    app_main.app.dependency_overrides[current_user] = lambda: LOADTEST_USER
    try:
        transport = httpx.ASGITransport(app=app_main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://loadtest", timeout=120) as client:
            return await run_requests(client, plan, concurrency)
    finally:
        app_main.answer, app_main.propose_action, settings.rate_limit_enabled, settings.persist_chats = saved
        app_main.app.dependency_overrides.pop(current_user, None)


async def run_full(url: str, plan: list[str], concurrency: int, timeout: float, user: str, password: str):
    """Logs in once (the client keeps the session cookie), then sends the requests."""
    async with httpx.AsyncClient(base_url=url, timeout=timeout) as client:
        res = await client.post("/auth/login", json={"email": user, "password": password})
        if res.status_code != 200:
            raise SystemExit(f"Login failed with status {res.status_code}. Check --user and LOADTEST_PASSWORD.")
        return await run_requests(client, plan, concurrency)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="AI-HAS load test for POST /chat")
    p.add_argument("--mode", choices=MODES, default="mock")
    p.add_argument("-n", "--requests", type=int, default=100)
    p.add_argument("-c", "--concurrency", type=int, default=10)
    p.add_argument("--url", default="http://localhost:8000", help="server for --mode full")
    p.add_argument("--set", choices=["generated", "hard", "all"], default="all", dest="which")
    p.add_argument("--mock-latency-ms", type=float, default=0.0, help="fake answer delay in mock mode")
    p.add_argument("--timeout", type=float, default=120.0, help="seconds per request in full mode")
    p.add_argument("--user", help="email to log in with in full mode (the password comes from LOADTEST_PASSWORD)")
    p.add_argument("--yes", action="store_true", help="confirm that Gemini quota may be spent")
    p.add_argument("--no-report", action="store_true", help="do not write the load test section of the report")
    p.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = p.parse_args(argv)

    if args.requests < 1 or args.concurrency < 1:
        p.error("-n and -c must be at least 1")

    text = banner(args.mode, args.requests, args.url)
    print(text)
    print(f"Requests: {args.requests}, concurrent workers: {args.concurrency}, questions: {args.which}")
    if args.mode != "mock" and not args.yes:
        print("Nothing was sent. Add --yes to confirm that Gemini quota may be spent.")
        return 2

    password = os.environ.get("LOADTEST_PASSWORD", "")
    if args.mode == "full" and not (args.user and password):
        print("Full mode needs a login now: pass --user EMAIL and set the LOADTEST_PASSWORD environment variable.")
        return 2

    plan = build_plan(args.requests, load_questions(args.which))
    if args.mode == "full":
        results, duration = asyncio.run(
            run_full(args.url, plan, args.concurrency, args.timeout, args.user, password)
        )
    else:
        results, duration = asyncio.run(run_in_process(args.mode, plan, args.concurrency, args.mock_latency_ms))

    summary = summarize(results, duration)
    print(format_summary(summary))

    if not args.no_report:
        existing = args.report.read_text(encoding="utf-8") if args.report.exists() else "# AI-HAS Evaluation Report\n"
        body = report_section(
            args.mode, text, summary, args.requests, args.concurrency, datetime.date.today().isoformat()
        )
        args.report.write_text(replace_section(existing, "loadtest", body), encoding="utf-8")
        print(f"Load test section written to {args.report}")
    return 0 if summary["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
