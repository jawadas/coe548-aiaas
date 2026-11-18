-- Portal schema, version 1.
-- Runs once, inside a transaction, as the owner of the "portal" database.
-- gen_random_uuid() is built into PostgreSQL 13+, so no extension is needed.

CREATE TABLE users (
    id                   uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    email                text        NOT NULL,
    name                 text        NOT NULL,
    password_hash        text        NOT NULL,
    role                 text        NOT NULL DEFAULT 'user'
                                     CHECK (role IN ('user', 'admin')),
    status               text        NOT NULL DEFAULT 'active'
                                     CHECK (status IN ('active', 'disabled')),
    -- The matching user on the gateway: 'portal-<id>'.
    gateway_user_id      text        NOT NULL UNIQUE,
    -- The playground's own gateway key: encrypted secret and its gateway id.
    playground_key_enc   text,
    playground_key_token text,
    -- Per-user overrides. NULL means "use the configured default".
    rpm_limit            integer,
    tpm_limit            integer,
    max_keys             integer,
    -- Gateway keys the portal blocked when it disabled this account, so that
    -- enabling it again unblocks exactly those and not keys the user had paused.
    disabled_key_tokens  text[]      NOT NULL DEFAULT '{}',
    created_at           timestamptz NOT NULL DEFAULT now(),
    last_login_at        timestamptz
);

CREATE UNIQUE INDEX users_email_lower_key ON users (lower(email));

CREATE TABLE sessions (
    -- sha256 of the cookie value; the token itself is never stored.
    token_hash   text        PRIMARY KEY,
    user_id      uuid        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    csrf_token   text        NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    ip           text,
    user_agent   text
);

CREATE INDEX sessions_user_id_idx ON sessions (user_id);
CREATE INDEX sessions_expires_at_idx ON sessions (expires_at);

CREATE TABLE conversations (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       uuid        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    title         text        NOT NULL DEFAULT 'New chat',
    model         text        NOT NULL,
    system_prompt text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX conversations_user_updated_idx ON conversations (user_id, updated_at DESC);

CREATE TABLE messages (
    id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id   uuid        NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    role              text        NOT NULL CHECK (role IN ('user', 'assistant')),
    content           text        NOT NULL DEFAULT '',
    reasoning         text,
    status            text        NOT NULL DEFAULT 'complete'
                                  CHECK (status IN ('complete', 'stopped', 'error')),
    prompt_tokens     integer,
    completion_tokens integer,
    reasoning_tokens  integer,
    latency_ms        integer,
    ttft_ms           integer,
    error             text,
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX messages_conversation_created_idx ON messages (conversation_id, created_at);

CREATE TABLE audit_log (
    id            bigserial   PRIMARY KEY,
    at            timestamptz NOT NULL DEFAULT now(),
    -- Deliberately not a foreign key: the trail outlives the account.
    actor_user_id uuid,
    action        text        NOT NULL,
    target        text,
    detail        jsonb,
    ip            text
);
