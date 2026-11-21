# AI-HAS: AI-Powered Enterprise Helpdesk & Workflow Automation System

![CI](https://github.com/Orbie-HelpDesk/ITPE-FinalSum/actions/workflows/ci.yml/badge.svg)

The CI check (ruff, pytest, docker build) is advisory unless branch protection is enabled on `main`. The team lead should check whether the repo plan allows branch protection.

Capstone project: Applied Generative AI for IT Solution Development.

## What it does
- **Chat** (`/`): employees describe an IT problem and get an answer from **Orbie**, the assistant. Answers are grounded in the knowledge base in `kb/`, with citations. AI-HAS is the product name.
- **Guardrails:** every message is checked for prompt injection and length before it reaches the AI. Personal data (emails, phone numbers, card numbers, API keys, passwords) is redacted first.
- **Tickets:** when the answer is "not found", the "Create a ticket" button creates a real ticket. Ticket text is also PII-redacted.
- **Actions with confirmation:** Orbie can propose opening a ticket (with a mock Jira key from n8n) or sending a password reset link (mock Active Directory). It only proposes. A card with Confirm and Cancel appears, and nothing runs until you confirm. Every decision is written to `audit_logs`. See "Actions from chat" below.
- **Login and roles:** everyone logs in. Employees can chat and create tickets. Only agents and admins see the dashboard. See "Login and roles" below.
- **Admin dashboard** (`/admin`, agent or admin): ticket KPIs, a ticket list with a status filter, and status changes (open, in progress, resolved).
- **Evaluation:** scripts that measure retrieval accuracy, deflection, escalation, guardrails and latency.

If the best match is too far away (distance above 0.5) or the model replies NO_ANSWER, the API returns a "not found" message with `escalate=true`.

## Stack
FastAPI (Python 3.12), SQLAlchemy 2, Google Gemini through its OpenAI-compatible endpoint (openai SDK), PostgreSQL with pgvector on Supabase, and a plain HTML, CSS and JavaScript frontend. No frameworks and no build step.

## Endpoints
| Method | Path | Purpose |
|---|---|---|
| GET | `/login` | Login page (public) |
| POST | `/auth/login` | JSON `{email, password}`. Sets the session cookie. 401 wrong login, 429 too many failures, 503 if `SESSION_SECRET` is not set |
| POST | `/auth/logout` | Clears the session cookie |
| GET | `/auth/me` | The logged-in user `{id, email, name, role}`, or 401 |
| GET | `/` | Chat page (any logged-in user, else redirect to `/login`) |
| GET | `/admin` | Admin dashboard (agent or admin; employee gets 403, logged out is redirected) |
| POST | `/chat` | Login required. Guardrails, then hybrid search and answer. Returns `answer`, `citations`, `escalate`, `distances`, `guardrails`, `proposed_action` (null, or `{id, name, arguments, label}`) |
| POST | `/actions/confirm` | Login required. Body `{action_id, confirm}`. Runs (true) or drops (false) a proposed action. Only the owner can decide it, once. 403 other owner, 404 unknown, 409 already handled, 410 expired |
| POST | `/tickets` | Login required. Create a ticket owned by you from `summary` (3 to 500 chars) and `priority` |
| GET | `/tickets` | Agent or admin. List tickets, optional `?status=` |
| PATCH | `/tickets/{id}` | Agent or admin. Change status (audited) |
| GET | `/stats` | Agent or admin. Ticket totals, by priority, average resolution hours |
| GET | `/health` | App and database status (public) |
| GET | `/docs` | API docs (public) |

Pages and static files are sent with `Cache-Control: no-cache`, so a rebuilt frontend is not hidden by an old cached copy.

## Security basics
- **Rate limits** per client IP, in memory: `POST /chat` 20 per minute, `POST /tickets` 10 per minute and `POST /actions/confirm` 20 per minute. Over the limit the API returns 429 with `Retry-After`. Set `RATE_LIMIT_ENABLED`, `RATE_LIMIT_CHAT_PER_MIN`, `RATE_LIMIT_TICKETS_PER_MIN` and `RATE_LIMIT_ACTIONS_PER_MIN` in `.env` (names and defaults are in `.env.example`). `RATE_LIMIT_ENABLED=false` switches all limits off, for example for a load test. The counters are per process and reset on restart. The limit uses the direct client IP, so behind a reverse proxy all users share one IP.
- **Security headers** on every response: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`.
- **Safe errors:** an unexpected error returns `{"detail": "Something went wrong. Please try again."}` with status 500. The details only go to the server log, so no stack trace, database URL or key reaches the client. Normal 400, 404, 429 and 422 responses are unchanged.

## Login and roles
Everyone logs in at `/login`. Three roles exist (`users.role`): `employee`, `agent` and `admin`. Agent and admin have the same rights for now.

| Who | Can do |
|---|---|
| Logged out | Open `/login` and `/health`. Everything else answers 401, and the pages `/` and `/admin` redirect to `/login` |
| `employee` | The chat (`/`, `POST /chat`), `POST /tickets` and `POST /actions/confirm`. `/admin`, `GET /tickets`, `PATCH /tickets/{id}` and `GET /stats` answer 403 |
| `agent`, `admin` | Everything an employee can, plus the dashboard (`/admin`), `GET /tickets`, `PATCH /tickets/{id}` and `GET /stats` |

Tickets and proposed actions belong to the logged-in user. The chat and admin headers show the name, the role and a Log out button. Chat history in the browser is stored per user id.

**How it works**
- **Passwords** are hashed with PBKDF2-HMAC-SHA256 from the standard library (600000 iterations, random salt), so there is no new dependency. A wrong email takes as long as a wrong password, and both give the same message.
- **Session:** `POST /auth/login` (JSON `{email, password}`) sets a signed token in the cookie `aihas_session`: `HttpOnly`, `SameSite=Lax`, `Path=/`, expiring after `SESSION_HOURS` (default 4). `GET /auth/me` returns the user. `POST /auth/logout` clears the cookie. The token holds only the user id, so the role is read from the database on every request and a role change or removed user takes effect at once.
- **`SESSION_SECRET`** signs the token. It must be at least 32 characters. While it is empty or short, login answers 503. Make one with `python -c "import secrets; print(secrets.token_urlsafe(48))"` and put it in `.env`. Changing it logs everyone out.
- **`COOKIE_SECURE` should be `true` behind https**, so the browser only sends the cookie over https. It is `false` by default because local development uses plain http.
- **Logout only clears the cookie.** The signed token is not stored on the server, so a copy of it stays valid until it expires. Keep `SESSION_HOURS` short. Revoking tokens would need a sessions table, which is not built.
- **Cross-site requests:** every POST, PATCH and DELETE must be JSON (`Content-Type: application/json`), otherwise 415, so a plain HTML form cannot send one. If the browser sends an `Origin` header it must match the site, otherwise 403. There is no CORS.
- **Failed logins** are limited in memory: 5 per 10 minutes per email, and 20 per 10 minutes per client IP (`LOGIN_MAX_FAILURES_PER_EMAIL`, `LOGIN_MAX_FAILURES_PER_IP`, `LOGIN_WINDOW_MINUTES`). Over the limit the answer is 429 with `Retry-After`, even for the right password. A good login clears the email counter. The counters reset on restart. A known email can be locked for a few minutes by someone failing on purpose, which is the price of this protection.
- **`/docs` is public.** The API docs and `/openapi.json` need no login. They show the route names and fields but no data.

**Audit.** `audit_logs` gets a row for `login`, `login_failed`, `logout`, every `ticket_status_change` (ticket id, old and new status, written in the same transaction as the change), `password_set` (from the helper below), and each confirmed or cancelled action. The actor is `user:<id>`, or `anonymous` for a failed login with an unknown email. No row holds an email, a name, a password or ticket text.

**Demo users.** `init_db` gives the three demo users the password in `DEMO_PASSWORD` (at least 8 characters), only where they have none yet and only on a local database. It never overwrites a password, never prints it, and skips this step on any non-local host even with `--allow-remote`. `DEMO_PASSWORD` and `SESSION_SECRET` have placeholders in `.env.example`, and the real values belong only in your own `.env`.
```bash
docker compose run --rm -e DATABASE_URL=postgresql+psycopg2://aihas:aihas-local@db:5432/aihas -e DEMO_PASSWORD=choose-one backend python -m app.init_db
```

**Real accounts.** Existing users, for example on Supabase, have no password until one is set:
```bash
docker compose run --rm -e DATABASE_URL=postgresql+psycopg2://aihas:aihas-local@db:5432/aihas backend python -m app.set_password someone@example.com
```
The password is typed at a hidden prompt (twice). It is never taken from an argument, an environment variable or a pipe, and the command needs an interactive terminal. It only changes an existing user and creates nobody. It refuses a database that is not `db` or `localhost` unless `--allow-remote` is given, and that flag is only for when the team lead has said to change passwords on Supabase.

**Before this reaches Supabase:** the team lead reviews the new column in `schema.sql`, applies it, and sets real passwords with `set_password`. Until then nobody there can log in.

**Not built:** password reset by email, changing your own password in the UI, token revocation, per-role differences between agent and admin, and two-factor login.

## Quick start (Supabase)
```bash
cp .env.example .env      # fill in GEMINI_API_KEY and DATABASE_URL (ask the team lead), and SESSION_SECRET (see Login and roles)
docker compose up --build
```
- Chat page: http://localhost:8000
- Login: http://localhost:8000/login
- Admin dashboard: http://localhost:8000/admin (agent or admin)
- API docs: http://localhost:8000/docs
- Health: http://localhost:8000/health

Valid model names depend on your key. To list them:
```bash
docker compose run --rm backend python -m app.list_models
```

## Try it locally without touching Supabase
The `local-db` profile starts a throwaway Postgres with pgvector on host port 5433. Use the URL below only for these commands. Do not put it in a shared `.env`.

```bash
# 1. start the local database
docker compose --profile local-db up -d db

# 2. create the tables and seed demo users
docker compose run --rm -e DATABASE_URL=postgresql+psycopg2://aihas:aihas-local@db:5432/aihas \
  backend python -m app.init_db

# 3. index the knowledge base
docker compose run --rm -e DATABASE_URL=postgresql+psycopg2://aihas:aihas-local@db:5432/aihas \
  backend python -m app.ingest

# 4. run the app against it (same command as the normal service, with the URL overridden)
docker compose run --rm --service-ports \
  -e DATABASE_URL=postgresql+psycopg2://aihas:aihas-local@db:5432/aihas backend
```
`-e` overrides the value in `.env`. To look at the data: `docker compose exec db psql -U aihas -d aihas -c "select id, summary, priority, status from tickets order by id desc limit 5;"`. To reset the local data: `docker compose --profile local-db down -v`.

Ingest needs a valid `GEMINI_API_KEY` in `.env`, because it creates embeddings.

### Safety guard for scripts that write data
`init_db` and `ingest` refuse to run unless the `DATABASE_URL` host is `db` or `localhost`. The check reads the URL the same way SQLAlchemy does, so it also looks at `?host=` and `?hostaddr=`, and it refuses a missing or empty host (libpq would fall back to `PGHOST`). Its message never contains the URL or the password.

Passing `--allow-remote` overrides the guard, for example `python -m app.init_db --allow-remote`. Only use it when the team lead has said to write to Supabase.

## Database schema
`backend/schema.sql` creates every table with `CREATE ... IF NOT EXISTS`, so it never changes existing data. `python -m app.init_db` applies it and seeds three demo users (`employee@example.com`, `agent@example.com`, `admin@example.com`), with the password from `DEMO_PASSWORD` on a local database.

**Note:** `schema.sql` was written from `models.py` and from how `ingest.py` and `rag.py` use `kb_chunks`. It must match the real Supabase definition, which the team lead needs to verify. Do not run `init_db` against Supabase until that is done.

**New table:** `pending_actions` (additive, `create table if not exists`) holds proposed actions. It exists only in the local database so far. The team lead should review it in `schema.sql` before it is applied to Supabase. Until then, on Supabase, a proposal cannot be stored, so `/chat` logs the error and gives the normal answer. `audit_logs.actor` now holds `user:<id>`, not an email.

**New column:** `users.password_hash` (`alter table users add column if not exists`, additive). Same rule: review it first. Existing users keep working in the database but cannot log in until a password is set.

Tickets are owned by the logged-in user. The old default, `employee@company.com` (or the first `employee` user with a logged warning), is only used when code creates a ticket without an email, for example in tests.

## Evaluation
Two question sets:
- **Generated** (`eval_data/questions.json`): written by Gemini from the articles, so it is easy.
- **Hard** (`eval_data/hard_questions.json`): 51 hand-written cases. Typos and slang, Taglish, one-word messages, two problems in one message, questions that need a different article, near-miss out-of-scope questions (HR, payroll, building access), prompt injections, PII, and harmless messages that must not be blocked. It was written before any results were seen and is not tuned to them.

`eval_run` reports, per set: retrieval accuracy, deflection, out-of-scope escalation, guardrail results and latency. It also scores each answered question for **faithfulness** (is every claim supported by the retrieved article) and **answer relevance** using the Gemini chat model as judge, lists the worst five, and runs a **threshold sweep** of `MAX_DISTANCE` from 0.30 to 0.70 with a recommendation. The recommendation is advice and is never applied automatically.

These commands call the Gemini API and use your quota. Run them against the local database (see above).
```bash
# see what would run and the most calls it could use, without calling anything
docker compose run --rm -e DATABASE_URL=postgresql+psycopg2://aihas:aihas-local@db:5432/aihas   backend python -m app.eval_run --set hard --limit 10 --dry-run

docker compose run --rm -e DATABASE_URL=... backend python -m app.eval_run --set generated
docker compose run --rm -e DATABASE_URL=... backend python -m app.eval_run --set hard --limit 10
docker compose run --rm -e DATABASE_URL=... backend python -m app.eval_run --set all
```
Options: `--set generated|hard|all` (default `generated`), `--limit N` (evenly spaced sample of retrieval questions; guardrail checks cost nothing and always run), `--no-judge` (skip the two judge calls per answer), `--pause` (seconds between calls, default 1), `--timeout` (seconds per call, default 90), `--resume`, `--dry-run`, `--report PATH`.

Each call is retried with exponential backoff, and every result is saved to `eval_data/.cache` (git-ignored) as soon as it is known. If a run stops, `--resume` reuses the saved results and only repeats questions that failed. Calls per question: 1 embedding, 1 chat (only if the best match is close enough) and 2 judge calls (only for answered questions). The report is written to `eval_data/report.md` with the date and a "How to read this" section.

`python -m app.eval_generate` regenerates the generated set from the knowledge base, and `python -m app.tune` prints distances for a few questions.

### Load test
```bash
docker compose run --rm -e DATABASE_URL=... backend python -m app.loadtest                       # mock mode, no Gemini, no database
docker compose run --rm -e DATABASE_URL=... backend python -m app.loadtest --mode retrieval-only -n 50 -c 5 --yes
LOADTEST_PASSWORD=... python -m app.loadtest --mode full --url http://localhost:8000 --user employee@example.com -n 30 -c 3 --yes   # against a running server, after logging in
```
It sends `-n` requests to `POST /chat` with `-c` concurrent workers and reports requests per second, error rate, and median, p95 and p99 latency. The first lines of the output say which mode ran:
- **mock** (default): the app runs in-process as a fixed test user, with a stubbed answer step and no action step. No Gemini calls and no database. It measures the app's own overhead only.
- **retrieval-only**: real embedding and database search, no chat model. Spends one embedding call per request.
- **full**: real requests to a running server, logged in as `--user` with the password from the `LOADTEST_PASSWORD` environment variable (login is required now). Spends up to one embedding and one chat call per request. Start that server with `RATE_LIMIT_ENABLED=false`, or the rate limit answers 429 (counted separately).

The modes that spend quota refuse to run without `--yes`. The result is written as a marked "Load test" section of `report.md`, and `eval_run` keeps that section when it rewrites the report (`--no-report` skips it).

## n8n workflows (mock Jira and Active Directory)
Two workflows in `n8n/` back the actions the assistant can propose. Nothing real is called: the ticket workflow returns a made-up Jira key (`HELP-1234`) and the password workflow returns `{"status": "reset_link_sent"}`. No email is sent.

| File | Webhook path | Receives | Returns |
|---|---|---|---|
| `n8n/create-ticket.json` | `/webhook/create-ticket` | `{summary, priority, user_email}` | `{"key": "HELP-<number>"}` |
| `n8n/password-reset.json` | `/webhook/password-reset` | `{user_email}` | `{"status": "reset_link_sent"}` |

n8n is optional and only starts with its compose profile. It is published on `127.0.0.1:5678` only, so it is not reachable from other machines.

```bash
docker compose --profile n8n up -d n8n
```
1. Open http://localhost:5678 and create the local owner account (first start only).
2. Import: **Workflows > Add workflow > the ... menu > Import from file**, and pick `n8n/create-ticket.json`. Repeat for `n8n/password-reset.json`. (The `./n8n` folder is also mounted read-only at `/workflows` in the container.)
3. Open each workflow and switch **Active** on. Only active workflows answer the production URL `/webhook/...`.
4. Check it from your machine (the optional secret header is only needed if you set `N8N_WEBHOOK_SECRET`):
   ```bash
   curl -X POST http://localhost:5678/webhook/create-ticket -H "Content-Type: application/json" -d '{"summary":"Test","priority":"low","user_email":"a@b.com"}'
   ```

**Backend URL.** Inside the backend container `localhost` is the container itself, so use the compose service name: `N8N_WEBHOOK_URL=http://n8n:5678/webhook` (already in `.env.example`). The default in `config.py`, `http://localhost:5678/webhook`, is only right when the backend runs outside Docker.

**Settings** (names and placeholder values are in `.env.example`; never commit real values):
- `N8N_WEBHOOK_URL`: base URL of the webhooks.
- `N8N_WEBHOOK_SECRET`: optional. When set, the backend sends it in the `X-Webhook-Secret` header and both workflows answer 401 to any request without it. Set the same value for the `n8n` service and the backend (both read it from `.env`). Empty means no check.
- `N8N_ENCRYPTION_KEY`: optional key n8n uses for its stored credentials.
- `N8N_IMAGE_TAG`: optional n8n image version (default `1.70.3`).

Without the UI, import and activate from the command line (each file holds one workflow, so use `--separate` with the folder), then restart n8n:
```bash
docker compose exec n8n n8n import:workflow --separate --input=/workflows
docker compose exec n8n n8n update:workflow --all --active=true
docker compose --profile n8n restart n8n
```

Workflow data lives in the `n8ndata` volume. Re-export a changed workflow from the n8n editor (**...** menu > **Download**) into `n8n/`.

## Actions from chat
Flow: the user asks for something, Orbie proposes, the user confirms, the backend runs it through n8n.

1. `POST /chat` runs the usual guardrails and PII redaction. If the cleaned message looks like a request for an action (words such as ticket, reset my password, locked out), `rag.propose_action` sends it to the model with two tools, `create_ticket` and `request_password_reset`, using the `tools` parameter of the OpenAI-compatible Gemini endpoint. **This is one extra Gemini chat call for those messages only.** Other messages cost nothing extra. If the model calls a tool, `/chat` skips the knowledge base answer and returns `proposed_action`. If it does not (for example "how do I reset my password?"), the normal answer is returned, so that message used two chat calls in total.
2. The proposal is validated (known tool, summary length, priority, PII redacted, injection check) and stored in `pending_actions` with an id from `secrets.token_urlsafe`, its owner and a 10 minute expiry. The model never chooses the account: a password reset always applies to the owner.
3. The chat page shows a card with Confirm and Cancel. `POST /actions/confirm` receives only the id. What runs is what was stored, so a request cannot change it.
4. Deciding is one atomic `UPDATE ... WHERE id AND owner AND status = 'pending' AND not expired`, and the row count says if this call won. A double click, a replay or two tabs cannot run an action twice, and another user is rejected with 403.
5. Confirming a ticket creates it in our database first, then asks n8n for the Jira key and saves it in `tickets.external_ref`. If n8n is down the ticket still exists and the card says the Jira sync is pending. A password reset needs n8n, so it fails with a clear message if n8n is down. The proposal is used up either way, so the user asks again.
6. Confirm and cancel each write an `audit_logs` row: `actor` is `user:<id>` (never an email), `action` is `create_ticket` or `reset_password`, and `payload` has the decision, the outcome, the ticket id and priority. It holds no email, name or ticket text.

**If tool calling is unreliable.** If the tool call raises an error or comes back malformed (bad JSON, unknown tool), `propose_action` falls back to a small rule: a message that clearly asks to create or open a ticket, or to reset or unlock a password, and is not a "how, what, why..." question. The same confirm step still applies. This was checked end to end with a rejected model key, where the rule path produced the proposals. It has not been checked against a live Gemini tool call, so try it with a real key (see below).

**Owner.** A proposal belongs to the logged-in user, and only that user can confirm or cancel it.

**Try it locally** (local database, n8n imported and active as described above):
```bash
docker compose --profile local-db up -d db
docker compose --profile n8n up -d n8n
docker compose run --rm -e DATABASE_URL=postgresql+psycopg2://aihas:aihas-local@db:5432/aihas backend python -m app.init_db
docker compose run --rm --service-ports -e N8N_WEBHOOK_URL=http://n8n:5678/webhook -e DATABASE_URL=postgresql+psycopg2://aihas:aihas-local@db:5432/aihas backend
```
Then open http://localhost:8000, log in (for example as `employee@example.com` with your `DEMO_PASSWORD`; `SESSION_SECRET` must be set in `.env` or passed with `-e`), and type "please reset my password" or "create a ticket, my monitor flickers". Check the result with `docker compose exec db psql -U aihas -d aihas -c "select actor, action, payload from audit_logs order by id desc limit 5;"`. If you set `N8N_WEBHOOK_SECRET`, pass it to the backend with `-e N8N_WEBHOOK_SECRET=...` too.

## Tests
No Gemini calls and no real database are needed. Tests stub the database engine.

```bash
# local
pip install -r backend/requirements.txt
pytest

# or in Docker
docker compose run --rm -v "${PWD}/tests:/app/tests" backend sh -c "pip install -q pytest httpx && python -m pytest tests -q"
```
Covered: password hashing and demo passwords, signed tokens (tampered, expired, wrong secret), login and failure, the cookie flags, the lockout per email and per IP, each role (logged out, employee, agent, admin) against every protected endpoint and page, the JSON-only and Origin checks, the audit rows, the `set_password` helper (hidden prompt only, local database only), the n8n actions with a fake n8n (up, down, wrong secret, bad answers), proposals and `/actions/confirm` with an in-memory fake of the new tables (owner check, double confirm runs once, expiry, cancel, audit without personal data), tool calling with a fake model, `/health`, the models import, the local-db guard, ticket creation and its user fallback, the no-cache headers, the rate limits, the security headers and the safe 500.

### Lint and CI
```bash
pip install ruff==0.17.6
ruff check backend tests
```
GitHub Actions (`.github/workflows/ci.yml`) runs ruff, pytest and a `docker build` of `backend/` on every pull request and on pushes to `main`. It uses no secrets. The ruff version is pinned in `ruff.toml` and in the workflow, so change both together.

## Structure
- `backend/app/`: `main.py` (routes), `rag.py` (search and answer), `guardrails.py`, `tickets.py`, `auth.py` (current user, roles), `sessions.py` (signed token), `passwords.py`, `loginlimit.py`, `audit.py`, `actions.py` (n8n calls), `proposals.py` (propose, confirm, audit), `ingest.py`, `init_db.py`, `set_password.py`, `safety.py`, `eval_*.py`, `tune.py`
- `backend/schema.sql`: idempotent database schema
- `frontend/chat/`, `frontend/admin/` and `frontend/login/`: the three pages (`chat/session.js` is shared by chat and admin)
- `kb/`: IT knowledge base SOPs (markdown)
- `docs/`: architecture, prompt taxonomy
- `tests/`: pytest tests
- `n8n/`: exported workflows (mock Jira ticket, mock password reset)

## Not built yet
- Intent classification beyond the keyword check that decides when to offer tools
- Real Jira and Active Directory (the workflows are mocks)
- Saving conversations and messages to the database (chat history lives in the browser)
- Password reset by email, token revocation and two-factor login (see Login and roles)

## Progress
- [x] Week 1–2: architecture, schema, Docker, taxonomy
- [x] Week 3–4: RAG pipeline
- [x] Week 5–6: ticket creation and admin dashboard
- [x] Week 7: guardrails and evaluation
- [ ] Week 8: docs + defense
