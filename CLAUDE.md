# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

maludb-python-simple is a Python FastAPI rewrite of [maludb-lamp-api-server](https://github.com/maludb/maludb-lamp-api-server.git) — the PHP LAMP API server for MaluDB. It targets maludb_core 0.96.0 and serves as both a working API server and a learning-friendly codebase.

The PHP source is the authoritative reference for all endpoint behavior, error codes, SQL queries, and response shapes. See `docs/plans/2026-06-08-php-to-fastapi-rewrite.md` for the complete rewrite plan.

## Tech Stack

- **Python 3.12+**, **FastAPI**, **Uvicorn**
- **psycopg v3** (PostgreSQL adapter) — raw SQL, no ORM
- **SQLite** (auth/routing store) — replaces MySQL from PHP version
- **Pydantic v2** (request validation only)
- **httpx** (outbound LLM calls)
- **python-multipart** (file uploads)

## Build & Run

```bash
pip install -e ".[dev]"           # install with dev dependencies
uvicorn app.main:app --reload     # dev server on port 8000
```

## Testing

```bash
pytest                             # run full test suite
pytest tests/test_subjects.py -v   # run one test file
pytest -k "test_create" -v         # run tests matching a pattern
ruff check app/ tests/             # lint
ruff format app/ tests/            # format
```

## Architecture

### Design Principles (carried from PHP)

1. **SQL traceability.** Every SQL query is a literal string in the route handler. Given a URL, find the router file, read the SQL. No query builder, no ORM.
2. **One router per domain.** Each file in `app/routers/` is self-contained — all queries, models, and handlers for that resource live together.
3. **Minimal dependencies.** Only what's needed. No framework-on-top-of-framework.
4. **Learning-friendly.** Type hints everywhere. Each router reads top-to-bottom. No inheritance, no generic CRUD base.
5. **Contract-compatible.** Same URLs, JSON shapes, error codes, and HTTP status codes as the PHP server.

### Structure

```
app/
├── main.py              # FastAPI app, exception handlers, router mounting
├── config.py            # Settings from environment variables
├── database.py          # PostgreSQL per-tenant connection + db_query/db_exec/db_one/db_tx_core
├── auth_store.py        # SQLite auth store (replaces MySQL)
├── auth.py              # FastAPI dependency: Bearer token → tenant DB creds
├── llm_catalog.py       # Seeded default_prompts catalog (models × tasks)
├── errors.py            # APIError, exception-to-response mapping
├── sql_log.py           # SQL tracing (sql.log + ?debug=1 buffer)
├── helpers/
│   ├── statements.py    # SVPO statement create/shape helpers
│   ├── attributes.py    # Typed attribute create/shape/attach helpers
│   ├── documents.py     # Document↔graph link/unlink helpers
│   ├── llm.py           # LLM chat/extract/embed, chunking
│   └── llm_resolve.py   # Effective per-user task → model config resolution
└── routers/
    ├── subjects.py      # /v1/subjects and sub-resources
    ├── verbs.py         # /v1/verbs and sub-resources
    ├── projects.py      # /v1/projects and sub-resources
    ├── pools.py         # /v1/pools
    ├── skills.py        # /v1/skills
    ├── notes.py         # /v1/notes + issue workflow
    ├── episodes.py      # /v1/episodes + event-scoped statements
    ├── statements.py    # /v1/statements (SVO edges)
    ├── documents.py     # /v1/documents + file upload + graph
    ├── attributes.py    # /v1/attributes + templates + check
    ├── objects.py       # /v1/objects/{kind} (atomic create)
    ├── graph.py         # /v1/edges, /v1/graph/neighbors, /v1/graph/walk
    ├── memory.py        # /v1/memory/* (LLM + vector pipeline)
    ├── mcp.py           # POST /mcp — MCP server (stateless Streamable HTTP, 8 tools)
    ├── llm_config.py    # /v1/llm/* (catalog, provider keys, task→model choices)
    ├── tokens.py        # /v1/tokens (self-service auth)
    ├── model_prompts.py # /v1/model-prompts
    └── types.py         # Type picker lists (subject/verb/document/episode types)
```

### Request Flow

```
HTTPS Request
  → FastAPI router (URL → router file → handler function)
  → require_auth dependency
    → Extract Bearer token from Authorization header
    → sha256(token_body) → SQLite lookup → Postgres creds + user_id + role
    → psycopg.connect(creds) → per-request connection
  → Route handler
    → db_query/db_exec/db_one (raw SQL, logged to sql.log)
    → db_tx_core for maludb_core facades (SET LOCAL search_path TO public, maludb_core)
  → JSON response (+ meta.debug if ?debug=1 and MALUDB_DEBUG=1)
```

### Dual-Database Architecture

- **PostgreSQL** (tenant data): maludb_core facade views + functions. Per-request connection configured by the resolved token's Postgres credentials. One API fronts many tenants.
- **SQLite** (auth routing): `users` table (token_hash → pg_dbname/pg_user/pg_password) + `model_prompts` table (LLM config). Schema in `config/auth_store.sql`.

### Error Handling

Standard JSON shape: `{"error": {"code": "string_code", "message": "..."}}`

PostgreSQL SQLSTATE mapping (mirrors PHP):
- `23505` → 409 conflict
- `23502/23503/23514/22000/22023/22P02/P0001` → 422 validation_failed
- `42501` → 403 insufficient_privilege
- Auth failures → 502 tenant_db_auth_failed / 503 tenant_db_unavailable

### Key Database Patterns

- `maludb_subject`/`maludb_verb` are updatable VIEWS (triggers enforce types)
- `subject_id`/`verb_id` have no sequences — derived as `MAX(id) + 1`
- API `label` → DB `canonical_name` (aliased in SELECT)
- Episodes/statements/attributes run in `db_tx_core()` with `SET LOCAL search_path TO public, maludb_core`
- Statements/attributes are idempotent (upsert on key fields)
- Provenance workflow: provided → suggested → accepted/rejected

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `MALUDB_PG_HOST` | `localhost` | PostgreSQL host (fixed for all tenants) |
| `MALUDB_PG_PORT` | `5432` | PostgreSQL port |
| `MALUDB_AUTH_STORE` | `data/auth.db` | SQLite auth database path |
| `MALUDB_DEBUG` | (unset) | Set to `1` to enable `?debug=1` SQL trace in responses |
| `MALUDB_LOG_DIR` | `/var/log/maludb` | SQL and API log directory |
| `MALUDB_LLM_TOKEN` | (unset) | Fallback LLM API key (if not in DB secret store) |
| `MALUDB_EMBED_DIM` | `1536` | Embedding vector dimension |

## Workflow Conventions

1. Read the plan in `docs/plans/2026-06-08-php-to-fastapi-rewrite.md` before starting work.
2. When implementing a router, refer to the corresponding PHP endpoint file(s) in the source repo for exact SQL queries, validation logic, and response shapes.
3. Keep SQL literal in each route handler — do not abstract queries into a shared layer.
4. Use `db_tx_core()` for any endpoint that touches maludb_core facades (episodes, statements, attributes, objects, graph, memory).
5. Test each endpoint with pytest. Curl test scripts in `tests/curl/` mirror the PHP test suite for integration testing.
