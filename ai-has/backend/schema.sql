-- AI-HAS database schema.
--
-- NOTE: This file must match the real Supabase definition. It was written from
-- models.py and from how ingest.py and rag.py use kb_chunks, NOT from the live
-- database. The team lead must verify it against Supabase before anyone relies on it.
--
-- Idempotent and additive only: every statement is CREATE ... IF NOT EXISTS, so
-- running it never changes or removes existing tables or data.

create extension if not exists vector;

create table if not exists users (
    id serial primary key,
    email varchar(255) not null unique,
    name varchar(120) not null,
    role varchar(20) not null default 'employee',  -- employee | agent | admin
    created_at timestamp not null default now()
);

-- Added for login. Additive: existing users keep working and simply have no password yet.
alter table users add column if not exists password_hash varchar(255);  -- pbkdf2_sha256$iters$salt$hash

create table if not exists conversations (
    id serial primary key,
    user_id integer not null references users (id),
    status varchar(20) not null default 'open',  -- open | resolved | escalated
    created_at timestamp not null default now(),
    resolved_at timestamp
);

create table if not exists messages (
    id serial primary key,
    conversation_id integer not null references conversations (id),
    role varchar(10) not null,  -- user | assistant | tool
    content text not null,  -- PII-redacted before saving
    intent varchar(40),
    citations json,
    confidence double precision,
    latency_ms integer,
    created_at timestamp not null default now()
);

create table if not exists tickets (
    id serial primary key,
    conversation_id integer references conversations (id),
    user_id integer not null references users (id),
    summary text not null,
    priority varchar(10) not null default 'medium',  -- low | medium | high
    status varchar(20) not null default 'open',  -- open | in_progress | resolved
    source varchar(20) not null default 'ai',  -- ai | escalation
    external_ref varchar(60),  -- e.g. Jira key
    created_at timestamp not null default now(),
    resolved_at timestamp  -- for MTTR
);

create table if not exists kb_documents (
    id serial primary key,
    title varchar(200) not null,
    source_path varchar(300) not null,
    chunk_count integer not null default 0,
    indexed_at timestamp not null default now()
);

-- Not in models.py: ingest.py and rag.py use it through raw SQL.
create table if not exists kb_chunks (
    id serial primary key,
    document_id integer not null references kb_documents (id) on delete cascade,
    chunk_index integer not null,
    content text not null,
    metadata jsonb,
    embedding vector(768),
    fts tsvector generated always as (to_tsvector('english', content)) stored
);

create table if not exists audit_logs (
    id serial primary key,
    actor varchar(80) not null,  -- "user:<id>" or "ai", never an email
    action varchar(60) not null,  -- e.g. reset_password, create_ticket
    payload json,
    created_at timestamp not null default now()
);

-- Actions the assistant proposed. They run only after the owner confirms, once.
create table if not exists pending_actions (
    id varchar(64) primary key,  -- secrets.token_urlsafe, unguessable
    owner_user_id integer not null references users (id),
    name varchar(40) not null,  -- create_ticket | request_password_reset
    arguments json,  -- validated and PII-redacted
    status varchar(20) not null default 'pending',  -- pending | confirmed | cancelled | done | failed
    created_at timestamp not null default now(),
    expires_at timestamp not null,
    decided_at timestamp
);

-- Articles the admin page edits. The search index (kb_documents, kb_chunks) is built from these.
create table if not exists kb_articles (
    slug text primary key,  -- file name, e.g. password-reset.md
    title text not null,
    body text not null,  -- the markdown
    updated_at timestamptz not null default now()
);
alter table kb_articles enable row level security;

-- Added for chat persistence and feedback. Additive: nothing existing is changed or removed.
alter table conversations add column if not exists title varchar(120);  -- from the first redacted question
alter table messages add column if not exists feedback smallint check (feedback in (-1, 0, 1));  -- thumbs: -1 down, 0 none, 1 up
alter table messages add column if not exists escalated boolean not null default false;
alter table messages add column if not exists blocked boolean not null default false;  -- content is a placeholder, not the raw text
alter table pending_actions add column if not exists conversation_id integer references conversations (id) on delete set null;

-- Added for intent routing. messages.intent now holds the taxonomy intent (password_reset, how_to, ...).
-- What happened to the message (answered, escalated, ...) moves to outcome. Old rows keep their
-- value in intent and have a null outcome, so queries read coalesce(outcome, intent).
alter table messages add column if not exists outcome varchar(20);

create index if not exists messages_conversation_idx on messages (conversation_id);
create index if not exists messages_created_idx on messages (created_at);
create index if not exists conversations_user_idx on conversations (user_id, created_at);

-- Added for the audit log page: newest first, filtered by action and by date. Additive.
create index if not exists audit_logs_created_idx on audit_logs (created_at desc, id desc);
create index if not exists audit_logs_action_idx on audit_logs (action, created_at desc);

-- Cosine distance, matching the <=> operator used in rag.py.
create index if not exists kb_chunks_embedding_idx
    on kb_chunks using hnsw (embedding vector_cosine_ops);
create index if not exists kb_chunks_fts_idx
    on kb_chunks using gin (fts);
