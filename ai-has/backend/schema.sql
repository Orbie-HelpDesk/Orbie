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
    actor varchar(80) not null,  -- user email or "ai"
    action varchar(60) not null,  -- e.g. reset_password, create_ticket
    payload json,
    created_at timestamp not null default now()
);

-- Cosine distance, matching the <=> operator used in rag.py.
create index if not exists kb_chunks_embedding_idx
    on kb_chunks using hnsw (embedding vector_cosine_ops);
create index if not exists kb_chunks_fts_idx
    on kb_chunks using gin (fts);
