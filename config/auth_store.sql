-- SQLite auth/routing store for the MaluDB Python API.
--
-- Replaces the MySQL store from the PHP version (config/local-database.sql).
-- One row per API token: it carries the user's role and the Postgres connection
-- (pg_dbname / pg_user / pg_password) that requests authenticated by that token
-- connect with.  The token itself is stored only as a sha256 hash (of the token
-- after the `malu_` prefix).

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash      TEXT    NOT NULL UNIQUE,
    token_prefix    TEXT    NOT NULL,
    user_id         INTEGER NOT NULL,
    role            TEXT    NOT NULL DEFAULT 'executor',
    pg_dbname       TEXT    NOT NULL,
    pg_user         TEXT    NOT NULL,
    pg_password     TEXT    NOT NULL,
    expires_at      TEXT,
    device_name     TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Per-model extraction prompts + LLM connection.  The system prompt may differ
-- per model; api_format selects the request shape ('openai' | 'anthropic').
CREATE TABLE IF NOT EXISTS model_prompts (
    model_name       TEXT PRIMARY KEY,
    model_identifier TEXT,
    api_format       TEXT NOT NULL DEFAULT 'openai',
    system_prompt    TEXT,
    base_url         TEXT,
    api_key          TEXT,
    max_tokens       INTEGER DEFAULT 2048,
    generation_params TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
