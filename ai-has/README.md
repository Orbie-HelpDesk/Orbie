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
- **History and feedback:** every chat is saved for its owner (redacted text only). The history panel loads from the server, and the thumbs up and down are saved. See "What is stored, and for how long".
- **Admin dashboard** (`/admin`, agent or admin): ticket KPIs, a ticket list with a status filter, status changes (open, in progress, resolved), and an Analytics section with questions per day, deflection, response times, thumbs and top lists.
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
| POST | `/chat` | Login required. Body `{message, conversation_id?}`. Returns 503 with `Retry-After` when the AI provider is down (see "When the AI provider fails"). Guardrails, then hybrid search and answer. Returns `answer`, `citations`, `escalate`, `distances`, `guardrails`, `proposed_action` (null, or `{id, name, arguments, label}`), and `conversation_id` and `message_id` (null if saving is off or failed). 404 if the conversation is not yours |
| GET | `/conversations` | Your conversations, most recently active first (`?limit=`, 1 to 100) |
| GET | `/conversations/{id}` | The messages of a conversation. The owner, or an agent or admin (audited). Anyone else gets 404 |
| DELETE | `/conversations/{id}` | Owner only. Deletes the messages and the conversation, and unlinks its tickets (audited) |
| PATCH | `/messages/{id}/feedback` | Body `{value}` with -1, 0 or 1. Only on assistant messages in your own conversations, else 404 |
| POST | `/actions/confirm` | Login required. Body `{action_id, confirm}`. Runs (true) or drops (false) a proposed action. Only the owner can decide it, once. 403 other owner, 404 unknown, 409 already handled, 410 expired |
| POST | `/tickets` | Login required. Create a ticket owned by you from `summary` (3 to 500 chars), `priority` and an optional `conversation_id` of your own chat |
| GET | `/tickets` | Agent or admin. List tickets, optional `?status=`. Each ticket has `ref` (`TCK-0007`), `external_ref` (the Jira-style key such as `HELP-4821`, or `null` until n8n has returned one), `created_at` and `resolved_at` |
| PATCH | `/tickets/{id}` | Agent or admin. Change status (audited) |
| GET | `/stats` | Agent or admin. Ticket totals, by priority, average resolution hours. `?tz=` (an IANA name such as `Asia/Singapore`) makes "created today" follow that zone's day |
| GET | `/stats/analytics` | Agent or admin. `?days=` 1 to 365 (default 30) and `?tz=`. Chat analytics, see below |
| GET | `/admin/kb` and `/kb`, `/kb/info`, `/kb/{source}`, `/kb/test` | **Admin only.** The knowledge base page and its API. PUT and DELETE on `/kb/{source}` change it. See "Knowledge base page (admins only)" |
| GET | `/health` | App and database status (public) |
| GET | `/docs` | API docs (public) |

Pages and static files are sent with `Cache-Control: no-cache`, so a rebuilt frontend is not hidden by an old cached copy.

**Timestamps and time zones.** Every timestamp the API sends (tickets, conversations, messages) is ISO 8601 in UTC with its offset, for example `2026-11-21T03:13:40+00:00`. The pages turn it into the viewer's local time in the browser. The database columns are `timestamp without time zone` and are read as UTC, which is what `now()` writes on a UTC server. That holds on the local database (`Etc/UTC`). **The team lead should confirm it on Supabase with `show timezone;`**, and if it is not UTC the stored times, and so the displayed ones, are off by that difference.

`?tz=` on `/stats` and `/stats/analytics` is checked against `pg_timezone_names` and is only ever sent to the database as a bound parameter, never added to the SQL text. An unknown name gets 422, and no `tz` means UTC. The dashboard sends the browser's zone, so a Singapore viewer's days start at midnight Singapore time: the "Created today" tile and each bar of the questions-per-day chart follow their day instead of the UTC day.

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
| `agent`, `admin` | Everything an employee can, plus the dashboard (`/admin`), `GET /tickets`, `PATCH /tickets/{id}`, `GET /stats` and `GET /stats/analytics` |
| `admin` only | Everything an agent can, plus the knowledge base page (`/admin/kb`) and its API (`/kb`, `/kb/{source}`, `/kb/test`). Agents and employees get 403 |

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

**Sample users (local database only).** For the team demo, run the command above with `DEMO_PASSWORD='MyOrbie2026!'` and all three users share that password. Log in at http://localhost:8000/login.

| Email | Role | What it can do | Password |
|---|---|---|---|
| `employee@example.com` | employee | Chat, create tickets, see its own history | `MyOrbie2026!` |
| `agent@example.com` | agent | Everything an employee can, plus the admin page, tickets and analytics | `MyOrbie2026!` |
| `admin@example.com` | admin | Everything an agent can, plus the knowledge base page | `MyOrbie2026!` |

This password is for the local demo database only. Never set it on Supabase or any shared or real environment, and use `set_password` there instead. If a user already has a different password, `init_db` leaves it alone.

**Real accounts.** Existing users, for example on Supabase, have no password until one is set:
```bash
docker compose run --rm -e DATABASE_URL=postgresql+psycopg2://aihas:aihas-local@db:5432/aihas backend python -m app.set_password someone@example.com
```
The password is typed at a hidden prompt (twice). It is never taken from an argument, an environment variable or a pipe, and the command needs an interactive terminal. It only changes an existing user and creates nobody. It refuses a database that is not `db` or `localhost` unless `--allow-remote` is given, and that flag is only for when the team lead has said to change passwords on Supabase.

**Before this reaches Supabase:** the team lead reviews the new column in `schema.sql`, applies it, and sets real passwords with `set_password`. Until then nobody there can log in.

**Not built:** password reset by email, changing your own password in the UI, token revocation, per-role differences between agent and admin, and two-factor login.

## What is stored, and for how long
Every `POST /chat` is saved, so the history survives a new browser and the dashboard can show analytics.

| What | Where | Notes |
|---|---|---|
| Conversation | `conversations` | The owner, a status (`open` or `escalated`) and a title made from the first question, already redacted |
| Your question | `messages` (role `user`) | The PII-redacted text that the pipeline used, never the raw message |
| Orbie's answer | `messages` (role `assistant`) | The text, the cited articles, a confidence (0 to 1, from the best match distance, same scale as the gauge in the chat), the whole request time in ms, the taxonomy intent (`how_to`, `vpn_issue`, `password_reset`, `software_access`, `incident`, `out_of_scope`, `unknown`, or `blocked:<reason>`), an outcome (`answered`, `escalated`, `action_proposed`, `refused`, `clarify` or `blocked`) and the `escalated` flag |
| Thumbs up or down | `messages.feedback` | -1, 0 or 1, set only by the owner of the conversation |
| A blocked message | `messages` | Saved as the placeholder `[blocked message]` with `blocked = true`. The raw text is never stored |
| Tickets | `tickets.conversation_id` | A ticket made from the chat is linked to its conversation |
| Who did what | `audit_logs` | Logins, ticket status changes, confirmed actions, `kb_create`, `kb_update` and `kb_delete` (file name only), `conversation_read` (staff reading someone else's chat), `conversation_deleted`, `messages_purged`. Conversation ids and counts only, never text |

**Not stored:** the raw text of blocked messages, the id of a proposed action (it works like a one-time key, so a reloaded chat shows the text of a proposal but not its Confirm card), the match distances (only the confidence), passwords, and anything the redaction replaced.

**Who can read it.** You can read, rate and delete your own conversations. Agents and admins can read any conversation by id, and each such read writes an audit row. Nobody can write into or delete someone else's conversation, and nobody but the owner can rate a message. Agents and admins also see the analytics, which show the redacted text of the most escalated questions. The redaction is pattern based and can miss a personal detail, so treat that list as internal.

**If saving fails** (for example the new tables are not on the database yet) the chat still answers, and `conversation_id` in the response is null. Set `PERSIST_CHATS=false` to switch saving off completely. The in-process load test does this itself. For `--mode full`, start the server with `PERSIST_CHATS=false` or every request becomes a conversation. The evaluation runner calls the answer code directly, so it never logs in and never saves anything.

**History in the chat page.** When logged in, the history panel shows the saved conversations from the server. The list is loaded when the page opens, and refreshed when the panel is opened, after every saved answer and after a delete. A failed answer (for example a 503) is not saved, so it is not listed. If the server cannot be reached (network error, 401 or 5xx) the list that is already shown stays as it is, with no error message, and the history kept in this browser (stored per user) is used. Chats from the old browser-only history are not copied to the server. History loaded from the server has no match gauge, because the distances are not stored. The delete buttons delete the conversation on the server (its tickets stay, no longer linked to it).

**Retention.** Nothing is removed automatically. To remove old text:
```bash
docker compose run --rm -e DATABASE_URL=postgresql+psycopg2://aihas:aihas-local@db:5432/aihas backend python -m app.purge_old --days 90 --dry-run
docker compose run --rm -e DATABASE_URL=postgresql+psycopg2://aihas:aihas-local@db:5432/aihas backend python -m app.purge_old --days 90
```
For messages older than N days it replaces the text with `[purged]`, and for every conversation that has such a message it replaces the title too (the title comes from the first question). Rows, times, citations, confidence, latency, thumbs and the escalated flag stay, so the counts in the analytics survive, but the escalated question list drops purged questions. `--dry-run` only counts. It writes one `messages_purged` audit row with counts, refuses a database that is not `db` or `localhost` unless `--allow-remote` is given (only when the team lead has said to change data on Supabase), and prints counts only. It does not touch tickets, whose summaries are redacted but are business records, so they need their own retention decision.

**Analytics.** `GET /stats/analytics?days=30&tz=Asia/Singapore` (agent or admin, 1 to 365 days, days counted in `tz`, UTC by default) feeds the Analytics section of the dashboard: questions per day, the deflection rate (`answered / (answered + escalated)`, with blocked messages and proposed actions left out), median and 95th percentile response time, the thumbs-up share (`up / (up + down)`), the most cited articles and the most escalated questions.

## When the AI provider fails
Every chat and embedding call goes through `backend/app/llm.py`, so a slow or failing Gemini cannot hang a request or show a raw error. The OpenAI client is built with `max_retries=0`. The SDK default is a 600 second timeout and silent retries, and retrying is done only in `llm.py`, where the waits are limited and logged.

| Setting | Default | Meaning |
|---|---|---|
| `CHAT_TIMEOUT_SECONDS` | 30 | One chat attempt. **A slow model may need a higher value**, for example 60. Raise `LLM_CALL_BUDGET_SECONDS` with it, because an attempt never runs longer than the budget that is left |
| `EMBED_TIMEOUT_SECONDS` | 20 | One embedding attempt |
| `LLM_CALL_BUDGET_SECONDS` | 45 | Total time for one call, all retries included |
| `CHAT_MODEL_FALLBACK` | empty | Optional second chat model. Use `docker compose run --rm backend python -m app.list_models` to see the names. Names come from `.env`, never from the code |

**Retries.** A timeout, a connection error, 429 and 500 to 504 are retried up to 2 times, after 1 s and then 3 s, as long as the budget allows. 400, 401, 403, 404 and any other error are never retried on the same model. A 429 whose text says the daily quota is used up is not retried either. That check is a text match on "per day" or "daily", so if Gemini words it differently the cost is two extra retries of about 4 seconds.

**Fallback model.** If `CHAT_MODEL_FALLBACK` is set and the main model fails with 404, 429 (including a daily quota), 500 to 504, a timeout or a connection error, even after its retries, the fallback model gets one attempt and no retries. It gets at least 10 seconds even if the budget is used up, so one call takes at most the budget plus one short attempt. It is not tried after 400, 401 or 403, which another model would not fix. Embeddings have no fallback. The log says which model answered, by name only.

**When everything fails**, `POST /chat` returns HTTP 503 with `Retry-After: 10` and exactly:
```json
{"detail": "The AI service is busy right now. Please try again in a moment."}
```
The question is not saved in that case. The chat page shows that message with a Try again button, which stays disabled for the `Retry-After` seconds and then sends the same question again. Any other failed answer (rate limit, network, a generic 500) gets the same button.

**What is logged.** One line per failed attempt: the model name, a kind (`timeout`, `rate_limit`, `quota`, `server`, `not_found`, `bad_request`, `auth`, `connection`), the status code, the error class name and the attempt number. The error text, the response body, the URL and any key are never logged, and the original error is not attached to the one that is raised, so a traceback cannot print them either.

**The extra calls are single attempts.** The intent classifier and the action tool call (see "How a message is routed") make one attempt on the main model, with no retries and no fallback model. If one fails, the rules decide (the intent) or the proposal is built by rule (the action), so a slow provider cannot hold up the chat twice.

**Evaluation runner.** `eval_run` does not retry a search or answer that already retried inside `llm.py`. After `--max-provider-failures` questions in a row fail because the provider answered 429, 503 or timed out (default 3, `0` never stops), it stops with a message that says how many results are saved and which command to run next, and it writes no report. The saved results stay in `eval_data/.cache`, so the same command with `--resume` carries on. `--timeout` is also the timeout and total time of the search and answer calls during the run.

## How a message is routed
After the input guardrails and PII redaction, every message goes through the same steps (`POST /chat`):

1. **Memory.** The last 4 exchanges of the saved conversation (up to 8 messages, each cut to 500 characters, already redacted, blocked ones left out) are read from the server. The browser sends no history, so none can be forged. A message that is short (4 words or fewer) or points back ("it", "still", "again", "yan", "pa rin") and names no IT topic gets a **standalone question** by rules: the earlier question plus the follow-up, at most 240 characters. There is no Gemini call for this. Without a saved conversation (saving off, a new chat) there is no memory. `HISTORY_TURNS=0` turns it off.
2. **Intent** (`intent.py`, the taxonomy in `docs/prompt-taxonomy.md`). Rules in English and Taglish decide first. Only when they are not confident (below `INTENT_CONFIDENCE_THRESHOLD`, default 0.75, or two different intents fit) is one small model call made: strict JSON, `temperature 0`, a few tokens, a single attempt with no retries and no fallback model. If it fails or its reply is not valid, the rules' guess is used, and a provider problem never becomes an action, a refusal or a question: it falls back to the knowledge base. `INTENT_USE_MODEL=false` uses the rules only.
3. **Route.**

| Intent | What happens |
|---|---|
| `how_to`, `vpn_issue` | Hybrid search with the standalone question, then the answer from the knowledge base, with the memory in the prompt |
| `password_reset`, `software_access`, `incident` | An action is offered through the tool-calling path (below), with the Confirm and Cancel card. `incident` is always a high priority ticket |
| `out_of_scope` | A polite refusal. No search and no model call |
| `unknown` | One clarifying question. If the next message is still unclear, it escalates (`escalate=true`, the ticket button). This needs a saved conversation to remember that it already asked |

A question that starts with how, what, why or paano is asked for information, so "How do I reset my password?" goes to the knowledge base and "I forgot my password" offers the reset. The response carries `intent: {name, confidence, source}` (`rules`, `model` or `fallback`), and `messages.intent` stores the name. What happened to the message (answered, escalated, action_proposed, refused, clarify, blocked) is in `messages.outcome`, and the analytics read `coalesce(outcome, intent)` so rows saved before `outcome` existed still count.

**Function calling.** For an action intent, `rag.propose_action` asks the model, through the OpenAI-compatible `tools` parameter, to fill in the action's arguments (a short ticket summary and a priority). Only the tool for that intent is offered, and it is a single attempt through `llm.py`. If the call fails or the tool call is malformed (bad JSON, the wrong tool, no summary), the proposal is built by rule from the message instead. If the model answers without calling the tool, it has decided not to propose anything and the knowledge base answers. The model can only propose. Nothing runs until the user confirms, and `proposals.py` validates, redacts and stores the proposal as before.

**Output guardrails** (`guardrails.check_output`, run on the model's answer before it is returned):
- **Replaced** (a fixed message, `escalate=false`) if the answer contains an email or phone number, a card number, an API key, a password, or a run of 8 words copied from one of our own prompts. An email or phone number is allowed when it is written exactly like that in the retrieved article, because the knowledge base is company text. A password, a key or a card number never is.
- **Not found** (`escalate=true`) if the answer cites no source, is empty, or none of its steps are supported by the retrieved text. A step counts as supported when about a third of its longer words (compared by stem) appear in the context, and one supported step is enough. Steps with fewer than two such words are not judged. The citations are the articles the server gave the model, not something the model writes, so "no source" is only a safety net and the step check does the real work.
- The `guardrails` field of the response says what happened: `{"blocked": false, "reason": null, "redacted": [], "output": {"action": "pass" | "replaced" | "not_found", "reasons": [...]}}`, with `output` null on paths that have no model answer. The log has the reason names only, never the answer text.
**Measured leniency.** On the hard question set with a real model, 14 of the 37 retrieval questions could be answered before the provider's daily quota ran out (the rest got a 429, and `eval_run` stopped with its clear message). All 14 answers passed the output check, so **0 good answers were wrongly blocked**, and in every one at least one step was fully supported by the retrieved article (1.00, against a threshold of 0.35). This is a small sample. Run `eval_run --set hard --no-judge` again when the quota is back to cover all 37: the report's "Output guardrail" section lists any answer that may have been wrongly blocked.

### Gemini calls per chat message
| Case | Before | Now |
|---|---|---|
| The common case: a normal question the rules are sure about (this includes the rewrite, the memory and the output checks) | 2 (embedding, answer) | **2, so +0** |
| An action intent, rules sure | 1 (the tool call) | **1** (the tool call, as before) |
| Out of scope | 2 | **0** |
| `unknown` (a greeting, or the model is not sure) | 2 | 1 (the classifier) or 0 when the rules recognise a greeting |
| The rules are not sure, and the question goes to the knowledge base | 2 | **3**: one classifier call, then the embedding and the answer |
| The rules are not sure, and it is an action | 1 | **2**: the classifier, then the tool call |
| Worst case | 3 | **3** (classifier, embedding, answer), and 2 for an action. Retries and the fallback model in `llm.py` can add attempts to the embedding and the answer, but never to the classifier or the tool call, which are single attempts |

The rule that decides how often the classifier is needed is measured by `eval_run` ("Decided by the rules").

## Knowledge base page (admins only)
The articles Orbie answers from are the markdown files in `kb/`. Until now they were indexed only by running `python -m app.ingest`. Admins can now manage them at **`/admin/kb`**, linked from the dashboard header (only admins see the link). **Agents and employees get 403, and a logged-out visitor is sent to the login page.** The same applies to the API below.

| Method | Path | Purpose |
|---|---|---|
| GET | `/kb` | Every article with title, file, chunk count, `indexed_at` (UTC) and size, and a status: `indexed`, `not_indexed` (a file with no index rows, for example after a `git pull`) or `file_missing` (index rows whose file is gone) |
| GET | `/kb/info` | The limits and whether changes are allowed on this database (`editable`) |
| GET | `/kb/{source}` | The markdown of one article |
| PUT | `/kb/{source}` | Body `{content}`. Creates (201) or updates (200) the article and re-indexes **only that article** |
| DELETE | `/kb/{source}` | Removes the file and its index rows |
| POST | `/kb/test` | Body `{question, source?}`. One question through the same search the chat uses. Returns the top chunks with source, distance and a snippet, whether the best one is under the 0.5 limit, and whether the article being edited is among them. **No answer is written.** |

On the page: a list of articles with status badges, a markdown editor with a byte counter, Save and re-index, Delete with a confirmation, and a Test retrieval box with distance bars and the limit marked. It uses the same look as the rest of the app, in dark and light mode, and works on a phone.

**These edits change real files.** `kb/` is a read-write bind mount (`./kb:/kb` in `docker-compose.yml`), so saving an article in the page writes the file in your local `kb/` folder, and deleting removes it. They show up in `git status` like any other edit: a new article is untracked, a change is modified, a delete is deleted. Commit them, or undo them with git, like other changes. The page does not touch git.

**`KB_EDIT_ALLOW_REMOTE` (default `false`).** Saving and deleting change the index tables (`kb_documents` and `kb_chunks`) and the files, so they are refused with a 403 unless the database host is `db` or `localhost`, or `KB_EDIT_ALLOW_REMOTE=true`. Listing, reading and the test box always work. **Only the team lead should switch it on, and only for a shared database such as Supabase.** The reason is that the `kb/` files live in each developer's own checkout while the index is shared: an edit through the page would change the shared index but not anyone else's files, and the next `python -m app.ingest` from another checkout would put the old text back. With the switch off the page shows a read-only banner.

**Rules the server enforces**
- **File names** are lowercase letters, digits and dashes, ending in `.md` (at most 84 characters), and not a Windows reserved name such as `con` or `nul`. Lowercase only, so two names can never differ just by case on a case-insensitive disk.
- **Paths:** the resolved path must sit directly inside the KB folder (`KB_DIR`, default `/kb`), a symlink is never followed, and `../` or a slash in a name is refused. Nothing is ever written outside `kb/`.
- **Size and count:** at most `KB_MAX_BYTES` (default 60000) per article, counted in bytes, and at most `KB_MAX_ARTICLES` (default 200) articles. An obviously huge request is refused before it is read.
- **Content:** UTF-8 text, line endings and a BOM are cleaned, and the first line must be a `# Title` heading because it becomes the title.
- **Rate limit:** saving, deleting and testing share `RATE_LIMIT_KB_PER_MIN` (default 20), because each is an embedding call. Reading the list is not limited.

**How a save works.** The article is chunked and embedded first (through `llm.py`, in batches of 50, so a provider failure is the usual clear 503 and **nothing has changed**). Then a temp file is written, the index rows and the audit row are committed in one transaction, and only then the file is moved into place. If the database step fails the temp file is removed and the old article is untouched. A delete removes the index rows first and then the file. Both use the same `ingest.prepare_article` and `ingest.store_article` as the script, so the script and the page index an article identically.

**Audit.** Every create, update and delete writes `kb_create`, `kb_update` or `kb_delete` to `audit_logs` in the same transaction as the change. The actor is `user:<id>` and the payload is the file name only, never the article text.

The page can only be tested here without Gemini. Saving an article makes one embedding call per 50 chunks (usually one call), and the test box makes one.

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

### Demo data for screenshots and the defense
`python -m app.seed_demo` fills the **local** database with demo data, so the dashboard and the analytics page look populated.

> **This is synthetic demo data.** The tickets, questions, answers, ratings, confidence values, latencies and times are made up for the demo. None of it is real usage. Do not use it to measure anything, and do not present it as real results.

```bash
# add the demo data (safe to run again: it skips what is already there)
docker compose run --rm -e DATABASE_URL=postgresql+psycopg2://aihas:aihas-local@db:5432/aihas backend python -m app.seed_demo

# first clear the development leftovers, then add the demo data (asks you to type "reset")
docker compose run --rm -e DATABASE_URL=postgresql+psycopg2://aihas:aihas-local@db:5432/aihas backend python -m app.seed_demo --reset
```
Run `python -m app.init_db` first if the demo users do not exist yet. All the data belongs to the demo employee, `employee@example.com`.

**What it adds**
- **10 tickets** over the last 14 days: 5 resolved (with resolved times), 3 in progress and 2 open, in all three priorities. A password lockout, a VPN problem, a printer, software access, a laptop battery, a shared drive and similar. Seven have `HELP-nnnn` keys, three do not (a sync still pending). Two of them were created from a chat and are linked to it.
- **20 saved questions and answers** in 17 conversations over the last 7 days: 15 answered, 4 escalated (questions the knowledge base does not cover, such as a replacement screen) and 1 proposed action. They cite real article names from `kb/` (`vpn-troubleshooting.md`, `password-reset.md`, `wifi-troubleshooting.md`, `printer-troubleshooting.md`, `email-setup.md`, `computer-restart-and-slow-pc.md`, `software-access-request.md`), with confidence values, response times of 2 to 9 seconds, 6 thumbs up and 1 thumbs down. The deflection rate comes out near 79%, not 100%.
- One `audit_logs` row, `demo_seeded`, with `synthetic: true` and the counts, so the rows can be told apart later.
- Times are UTC. "N days ago" counts days in Singapore, so the oldest question stays inside the dashboard's last 7 days for a viewer in Singapore or in UTC. Nothing is dated in the future, whatever time you run it.

**Safety**
- It refuses to run unless the database host is `db` or `localhost` (the same host check as the other scripts). It has **no `--allow-remote` option at all**, and an unknown option is an error, so it cannot be pointed at Supabase.
- Without `--reset` it only adds data, and a second run adds nothing.
- `--reset` asks you to type exactly `reset` (it needs an interactive terminal, so a pipe cannot confirm it) and then deletes **all** rows of `tickets`, `conversations`, `messages`, `pending_actions` and `audit_logs`, in the same transaction as the new rows, so a failure leaves nothing half done. It never deletes users or the knowledge base. That includes your own test tickets and chats, so do not use it on a database you want to keep.
- It never calls Gemini and never reads or prints a password, a key or the database URL. The demo users keep whatever passwords they have.

### Safety guard for scripts that write data
`init_db` and `ingest` refuse to run unless the `DATABASE_URL` host is `db` or `localhost`. The check reads the URL the same way SQLAlchemy does, so it also looks at `?host=` and `?hostaddr=`, and it refuses a missing or empty host (libpq would fall back to `PGHOST`). Its message never contains the URL or the password.

Passing `--allow-remote` overrides the guard, for example `python -m app.init_db --allow-remote`. Only use it when the team lead has said to write to Supabase.

## Database schema
`backend/schema.sql` creates every table with `CREATE ... IF NOT EXISTS`, so it never changes existing data. `python -m app.init_db` applies it and seeds three demo users (`employee@example.com`, `agent@example.com`, `admin@example.com`), with the password from `DEMO_PASSWORD` on a local database.

**Note:** `schema.sql` was written from `models.py` and from how `ingest.py` and `rag.py` use `kb_chunks`. It must match the real Supabase definition, which the team lead needs to verify. Do not run `init_db` against Supabase until that is done.

Every change made after the first version (`pending_actions`, `users.password_hash`, and the chat persistence columns and indexes) is listed, with its exact statement, in "Schema changes not yet applied to Supabase" below. They exist only in the local database so far, and `audit_logs.actor` now holds `user:<id>`, `anonymous` or `cli`, never an email.

Tickets are owned by the logged-in user. The old default, `employee@company.com` (or the first `employee` user with a logged warning), is only used when code creates a ticket without an email, for example in tests.

## Schema changes not yet applied to Supabase
Everything below is in `backend/schema.sql`, is additive and idempotent (`if not exists`), and has only been run on the local database. The team lead should review it and apply it once. Nothing changes or removes existing tables, columns or data.

**Apply it with `psql`, not with `init_db`.** `init_db --allow-remote` would also insert the three demo users. This applies only the schema, and it can be run again safely:
```bash
psql "postgresql://USER:PASSWORD@HOST:5432/postgres" -f backend/schema.sql   # a plain postgresql:// URL: psql does not accept the +psycopg2 part of DATABASE_URL
```
Run it as one file, because the statements depend on each other in order (`pending_actions` must exist before the column added to it). The file also repeats the older `create table if not exists` statements, which do nothing where the tables exist. It was written from the code, so compare the older tables with the real Supabase definition first (see the note under "Database schema").

| From | Change | Statement |
|---|---|---|
| Login (PR 10) | Password hash for each user. Existing users cannot log in until `python -m app.set_password` sets one | `alter table users add column if not exists password_hash varchar(255);` |
| Actions with confirmation (PR 9) | New table for proposed actions | see below |
| Chat persistence (this PR) | Title of a conversation | `alter table conversations add column if not exists title varchar(120);` |
| This PR | Thumbs up or down | `alter table messages add column if not exists feedback smallint check (feedback in (-1, 0, 1));` |
| This PR | Escalated flag | `alter table messages add column if not exists escalated boolean not null default false;` |
| Intent routing (this PR) | What happened to a message. `messages.intent` now holds the taxonomy intent, and analytics read `coalesce(outcome, intent)` | `alter table messages add column if not exists outcome varchar(20);` |
| This PR | Blocked flag | `alter table messages add column if not exists blocked boolean not null default false;` |
| This PR | Link from a proposed action to its conversation | `alter table pending_actions add column if not exists conversation_id integer references conversations (id) on delete set null;` |
| This PR | Indexes for the history list and the analytics | `create index if not exists messages_conversation_idx on messages (conversation_id);` `create index if not exists messages_created_idx on messages (created_at);` `create index if not exists conversations_user_idx on conversations (user_id, created_at);` |

The `pending_actions` table (PR 9):
```sql
create table if not exists pending_actions (
    id varchar(64) primary key,
    owner_user_id integer not null references users (id),
    name varchar(40) not null,
    arguments json,
    status varchar(20) not null default 'pending',
    created_at timestamp not null default now(),
    expires_at timestamp not null,
    decided_at timestamp
);
```
Other things to know when applying it:
- `audit_logs.actor` has no schema change, but its meaning did: new rows hold `user:<id>`, `anonymous` or `cli`, never an email. Old rows are not rewritten.
- `conversations`, `messages`, `audit_logs` and `tickets.external_ref` already exist in the file from the first schema, so they are expected to exist on Supabase. If any of them is missing there, running the whole file creates it.
- Until it is applied, the app keeps working on Supabase: saving a chat fails quietly (the chat still answers), proposals cannot be stored, and nobody can log in, because there are no passwords. After applying it, set passwords with `set_password --allow-remote`, one user at a time.

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
Options: `--set generated|hard|all` (default `generated`), `--limit N` (evenly spaced sample of retrieval questions; guardrail checks cost nothing and always run), `--no-judge` (skip the two judge calls per answer), `--pause` (seconds between calls, default 1), `--timeout` (seconds per call, default 90, also the limit for search and answer calls in the run), `--intent-model` (let the classifier model decide the questions the rules are not sure about, one extra call each; the default uses the rules only), `--max-provider-failures N` (stop after N questions in a row fail because the AI service is busy or down, default 3, 0 never stops), `--resume`, `--dry-run`, `--report PATH`.

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

The in-process modes never save conversations. For full mode start the server with `PERSIST_CHATS=false`, or every request is saved as a conversation. The modes that spend quota refuse to run without `--yes`. The result is written as a marked "Load test" section of `report.md`, and `eval_run` keeps that section when it rewrites the report (`--no-report` skips it).

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

1. `POST /chat` runs the usual guardrails and PII redaction, then the intent router (see "How a message is routed"). For `password_reset`, `software_access` and `incident`, `rag.propose_action` sends the message to the model with the one tool for that intent (`request_password_reset` or `create_ticket`), using the `tools` parameter of the OpenAI-compatible Gemini endpoint. **This is one Gemini chat call, the same as before.** If the model calls the tool, `/chat` skips the knowledge base answer and returns `proposed_action`. If it does not, the normal answer is returned.
2. The proposal is validated (known tool, summary length, priority, PII redacted, injection check) and stored in `pending_actions` with an id from `secrets.token_urlsafe`, its owner and a 10 minute expiry. The model never chooses the account: a password reset always applies to the owner.
3. The chat page shows a card with Confirm and Cancel. `POST /actions/confirm` receives only the id. What runs is what was stored, so a request cannot change it.
4. Deciding is one atomic `UPDATE ... WHERE id AND owner AND status = 'pending' AND not expired`, and the row count says if this call won. A double click, a replay or two tabs cannot run an action twice, and another user is rejected with 403.
5. Confirming a ticket creates it in our database first, then asks n8n for the Jira key and saves it in `tickets.external_ref`. If n8n is down the ticket still exists and the card says the Jira sync is pending. A password reset needs n8n, so it fails with a clear message if n8n is down. The proposal is used up either way, so the user asks again.
6. Confirm and cancel each write an `audit_logs` row: `actor` is `user:<id>` (never an email), `action` is `create_ticket` or `reset_password`, and `payload` has the decision, the outcome, the ticket id and priority. It holds no email, name or ticket text.

**If tool calling is unreliable.** If the tool call raises an error or comes back malformed (bad JSON, the wrong tool, no summary), the proposal is built by rule from the intent: a reset for `password_reset`, a medium priority ticket for `software_access` and a high priority ticket for `incident`, with the message as the summary. The same confirm step still applies. This was checked end to end with a rejected model key, where the rule path produced the proposals. It has not been checked against a live Gemini tool call, so try it with a real key (see below).

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
Covered: `seed_demo` with a stubbed database (the local-only guard with no override, the typed `reset` confirmation, that users and the knowledge base are never deleted, the shape and honesty of the data, repeat runs, no row dated in the future), the knowledge base API and page with a temporary folder, a fake index and a fake embedder (roles, file names, path traversal, the size limit, create and update, failures that change nothing, delete, audit without text, the shared-database switch, the test box), the intent rules (English and Taglish, ambiguity, question forms), the classifier call with a fake model (valid, fenced, invalid, low confidence, failing, single attempt), each route of `/chat`, the follow-up rewrite and the memory, the output checks, the tool-calling path with fake replies, the evaluation's intent and output sections, the AI provider wrapper with a fake client that raises 404, 429, 503, timeouts and connection errors (retry counts and waits, no retry on 404 or 400, the fallback model, the time budget, the 503 body and `Retry-After`, no secret or response body in the log or the response), the how, what and why skip, the evaluation runner stopping and resuming, saving conversations and messages (redaction, blocked placeholder, confidence, ownership, failures, `PERSIST_CHATS=false`), the conversation, feedback and delete endpoints with owner checks and audit rows, the analytics numbers and who may read them, `purge_old` (guard, dry run, only text and titles), password hashing and demo passwords, signed tokens (tampered, expired, wrong secret), login and failure, the cookie flags, the lockout per email and per IP, each role (logged out, employee, agent, admin) against every protected endpoint and page, the JSON-only and Origin checks, the audit rows, the `set_password` helper (hidden prompt only, local database only), the n8n actions with a fake n8n (up, down, wrong secret, bad answers), proposals and `/actions/confirm` with an in-memory fake of the new tables (owner check, double confirm runs once, expiry, cancel, audit without personal data), tool calling with a fake model, `/health`, the models import, the local-db guard, ticket creation and its user fallback, the no-cache headers, the rate limits, the security headers and the safe 500.

### Lint and CI
```bash
pip install ruff==0.17.6
ruff check backend tests
```
GitHub Actions (`.github/workflows/ci.yml`) runs ruff, pytest and a `docker build` of `backend/` on every pull request and on pushes to `main`. It uses no secrets. The ruff version is pinned in `ruff.toml` and in the workflow, so change both together.

## Structure
- `backend/app/`: `main.py` (routes), `rag.py` (search and answer), `guardrails.py`, `llm.py` (provider calls, retries, fallback), `kb.py` (the knowledge base page's logic), `seed_demo.py` (synthetic demo data, local database only), `ingest.py` (indexing, shared by the script and the page), `intent.py` (intent rules and classifier), `memory.py` (history and the follow-up rewrite), `tickets.py`, `chatlog.py` (saving and reading chats), `analytics.py`, `purge_old.py`, `auth.py` (current user, roles), `sessions.py` (signed token), `passwords.py`, `loginlimit.py`, `audit.py`, `actions.py` (n8n calls), `proposals.py` (propose, confirm, audit), `ingest.py`, `init_db.py`, `set_password.py`, `safety.py`, `eval_*.py`, `tune.py`
- `backend/schema.sql`: idempotent database schema
- `frontend/chat/`, `frontend/admin/`, `frontend/kb/` and `frontend/login/`: the pages (`chat/session.js` is shared by chat, admin and the knowledge base page)
- `kb/`: IT knowledge base SOPs (markdown)
- `docs/`: architecture, prompt taxonomy
- `tests/`: pytest tests
- `n8n/`: exported workflows (mock Jira ticket, mock password reset)

## Not built yet
- Intent rules for more languages than English and Taglish, and a model-written rewrite for follow-ups the rules cannot make standalone
- Real Jira and Active Directory (the workflows are mocks)
- Copying the old browser-only history to the server, a list of everyone's conversations for staff, and automatic retention (`purge_old` is run by hand)
- Password reset by email, token revocation and two-factor login (see Login and roles)

## Progress
- [x] Week 1–2: architecture, schema, Docker, taxonomy
- [x] Week 3–4: RAG pipeline
- [x] Week 5–6: ticket creation and admin dashboard
- [x] Week 7: guardrails and evaluation
- [ ] Week 8: docs + defense
