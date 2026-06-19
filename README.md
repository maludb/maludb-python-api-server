# maludb-python-simple

This serves as both a **working API server** (targeting maludb_core 0.100.0) and a **learning-friendly codebase** — every route handler contains its SQL inline, making the path from URL to query visible in one file.

## Quick Start

These steps take a **fresh Ubuntu 24.04 LTS** machine — nothing installed beyond the base OS — to a running API server. Ubuntu 24.04 already ships Python 3.12, which is the only interpreter version this project needs.

### 1. Install system packages

```bash
sudo apt update
sudo apt install -y git curl python3-venv python3-pip postgresql postgresql-contrib
```

### 2. Create the tenant PostgreSQL database

The API is multi-tenant: every token maps to a PostgreSQL database that holds the actual knowledge graph. Create a login role and a database for your tenant. Maludb-core should be installed with a database, user account, and schema already created.  If it is not, you can create a new database named 'maludu', with a user and schema both named 'app' with the instructions below.

DO NOT
DO NOT - DO NOT UPGRADE POSTGRES TO VERSION 18
DO NOT
```bash
sudo -u postgres createdb maludb
sudo -u postgres psql -d maludb -c "CREATE EXTENSION maludb_core CASCADE"
sudo -u postgres psql -d maludb -tAc "SELECT maludb_core.maludb_core_version()"
sudo -u postgres psql -d maludb -c "CREATE USER app"
sudo -u postgres psql -d maludb -c "CREATE SCHEMA app AUTHORIZATION app"
sudo -u postgres psql -d maludb -c "SELECT * FROM maludb_core.enable_memory_schema('app')"
sudo -u postgres psql -d maludb -c "SET ROLE app; SET search_path TO app, maludb_core, public; SELECT * FROM maludb_subject"
sudo -u postgres psql -c "ALTER USER app PASSWORD '#change_on_install#'"
```

> **maludb_core is a prerequisite, not part of this repo.** The data endpoints (and minting a token) require the **maludb_core 0.100.0** facade views and functions to already exist in the tenant database. Install them into the `maludb` database above by following the [MaluDB](https://maludb.com) project instructions before continuing past the health check. The server itself — and `/health` — starts without it.

### 3. Clone and install the API server

```bash
git clone https://github.com/maludb/maludb-python-api-server.git
cd maludb-python-api-server

# Create and activate a virtual environment (required on Ubuntu 24.04)
python3 -m venv .venv
source .venv/bin/activate

# Install the project with its dev dependencies
pip install -e ".[dev]"
```

> **`pip install` reports `Command 'pip' not found`?** You're not in the virtual environment. Ubuntu has no system-wide `pip` command — `apt install python3-pip` only provides `pip3`, never bare `pip`. The `pip` command exists *only* after `source .venv/bin/activate`, which also changes your shell prompt to start with `(.venv)`. Verify with `which pip` — it should point inside `.venv/bin/`.

### 4. Run the dev server

```bash
uvicorn app.main:app --reload --port 8000
```

The SQLite auth store (`data/auth.db`) is created and migrated automatically on first use — there is no manual database bootstrap step.

### 5. Verify

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

Once maludb_core is installed in your tenant database, continue to [Authentication](#authentication) to mint your first token.

## Requirements

- Python 3.12+
- PostgreSQL 17 (with maludb_core 0.100.0 installed)
- No other services required (SQLite replaces MySQL for auth)

## Configuration

All configuration is via environment variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `MALUDB_PG_HOST` | `localhost` | PostgreSQL host |
| `MALUDB_PG_PORT` | `5432` | PostgreSQL port |
| `MALUDB_AUTH_STORE` | `data/auth.db` | SQLite auth database path |
| `MALUDB_DEBUG` | (unset) | Set to `1` to enable `?debug=1` |
| `MALUDB_LOG_DIR` | `/var/log/maludb` | Log directory |

## Authentication

1. Mint a token by proving your Postgres login:
   ```bash
   curl -X POST http://localhost:8000/v1/tokens \
     -H 'Content-Type: application/json' \
     -d '{"pg_dbname":"mydb","pg_user":"myuser","pg_password":"mypass"}'
   ```

2. Use the returned token for all other requests:
   ```bash
   curl http://localhost:8000/v1/subjects \
     -H 'Authorization: Bearer malu_...'
   ```

## Configure an LLM provider and model

The memory pipeline (`/v1/memory/ingest`, `/v1/memory/documents`,
`/v1/memory/search`) extracts a knowledge graph with an LLM, so each user must
**store a provider API key** and **choose a model** before ingest works. Until
then, `/v1/memory/ingest` falls back to the legacy `chatgpt-4o` default — which
has no prompt configured — and returns:

```
model_not_configured: No prompt configured for model "chatgpt-4o" and no
model config for namespace "default".
```

These endpoints are Bearer-authenticated (use the token from
[Authentication](#authentication)) and keyed by your `user_id`, so every token a
user holds shares the same keys and model choices.

1. See the seeded catalog and your current key/choice state — each row reports
   `key_set` and `is_choice`:
   ```bash
   curl http://localhost:8000/v1/llm/catalog \
     -H 'Authorization: Bearer malu_...'
   ```

2. Store your provider API key. The `provider` must be one the catalog lists
   (e.g. `openai`, `anthropic`, `google`, `xai`, `deepseek`, `ollama`); the key
   is stored server-side and is never returned:
   ```bash
   curl -X PUT http://localhost:8000/v1/llm/providers/openai \
     -H 'Authorization: Bearer malu_...' \
     -H 'Content-Type: application/json' \
     -d '{"api_key":"sk-..."}'
   ```

3. Choose the extraction model (a `model_name` from the catalog). Tasks are
   `extract` (notes/documents), `embed` (search + document embeddings), and
   `skill_extract`:
   ```bash
   curl -X PUT http://localhost:8000/v1/llm/models/extract \
     -H 'Authorization: Bearer malu_...' \
     -H 'Content-Type: application/json' \
     -d '{"model_name":"gpt-4o"}'
   ```

If you choose a model whose provider has no stored key, the response includes a
`warning` naming the provider to set a key for. To enable search and document
push, repeat step 3 against `/v1/llm/models/embed` with an embedding model.

> The `maludb` CLI wraps these three calls as `maludb llm catalog`,
> `maludb llm set-key <provider>`, and `maludb llm use <model>`. The older
> `POST /v1/model-prompts` / `POST /v1/memory/config` paths still work but
> require raw Postgres credentials rather than a bearer token.

## Architecture

- **16 router modules** in `app/routers/` covering 106 route-method combinations
- **Raw SQL** via psycopg v3 — no ORM, no query builder
- **SQLite** for auth routing (token → Postgres credentials)
- **PostgreSQL** for all tenant data via maludb_core facade views
- **FastAPI dependency injection** for per-request auth + DB connections

### Agent skills (maludb_core 0.97.0)

`POST /v1/skills/ingest` registers a Claude Agent Skill bundle (SKILL.md +
scripts/references/assets) as an immutable skill version: canonical bundle-hash
dedupe, automatic parent detection, deterministic materiality screens (with an
optional LLM judge for body-only changes), discovery-tag extraction via the
configured model (`config/prompts/skill-extract.system.txt`; register it with
`POST /v1/model-prompts`) or a deterministic frontmatter fallback, divergent
fork lineage, and supersession of non-materially-different parents.
`GET /v1/skills/{id}/bundle` returns the full bundle (base64 content, per-file
hashes, executable bits) for client-side reconstruction; `GET /v1/skills`
gains `subject`/`verb` tag search through `maludb_skill_search`. Content
fields of a registered agent skill are immutable — PATCH answers 409.
The `malu` CLI front-end is `malu skill push / push-all / list / pull`.

### Note search (maludb_core 0.98.0)

`GET /v1/memory/notes` retrieves notes by the subjects/verbs of their
extracted edges — a thin wrapper over the `maludb_note_search` facade.
Structured search: `?subject_like=ubuntu&verb_like=installation` (fuzzy
verb: bidirectional containment, so `installation` finds `install`) or
`?subject_like=ubuntu&action=install` (exact canonical/alias verb match).
Free text: `?q=Install%20Ubuntu` — parsed deterministically against the
tenant verb catalog (`maludb_note_query_parse`); when no catalog verb
matches and the user has a `query_parse` model configured (the seeded
catalog task, prompt `config/prompts/query-parse.system.txt`), the server
falls back to an LLM parse constrained to the catalog. Defaults to
documents with `source_type='note'`; `all_sources=true` widens. The MCP
server exposes the same search as the `find_notes` tool. `POST
/v1/memory/ingest` now accepts optional `source_type` and `title` body
fields (the `maludb` CLI sends `source_type: "note"`); notes ingested
before that default to `source_type='document'` and only surface with
`all_sources=true`. The CLI front-end is `maludb get note`.

### Skill reindex (maludb_core 0.99.0)

`POST /v1/skills/reindex/run` runs one background reindex sweep for the
calling tenant: it claims the stalest skills via `maludb_skill_reindex_claim`
(never indexed, older than `?max_age=` — default `30 days`, or older than the
registry watermark), re-derives their discovery tags against the *current*
knowledge graph (the user's `skill_extract` model, or the deterministic
fallback), and applies a replace-`extracted` rewrite via
`maludb_skill_reindex_apply` — curator `manual` tags are preserved by the DB.
`?limit=` (default 32) caps the batch; one skill's failure is reported in
`errors` without aborting the sweep. Returns `501` until the 0.99.0 facades
are enabled (`enable_memory_schema`). This re-derivation is what links a skill
to subjects/verbs minted *after* it was first loaded, and what repairs a weak
initial extraction (find_skill's `+100`/`+80` facets).

Drive it on a schedule with the bundled systemd timer (the "background agent"):
`deploy/maludb-skill-reindex.{service,timer}` POST the endpoint periodically —
see the unit header for install. The sweep reindexes the configured token's
tenant; run one timer per tenant token for more. (DB-side protocol:
maludb_core `docs/skill-reindex.md`.)

### Document/note reindex (maludb_core 0.100.0)

`POST /v1/memory/reindex/run` runs one background reindex sweep over documents
and notes for the calling tenant — the SVPOR-graph analogue of skill reindex.
It claims the stalest documents via `maludb_memory_reindex_claim` (never
indexed, older than `?max_age=` — default `30 days`, or older than the registry
watermark; `?source_type=note` to scope), re-derives each one's SVPOR footprint
from its stored text with the user's `extract` model, and applies a
replace-footprint rewrite via `maludb_memory_reindex_apply` (delete the
`$source`-anchored statements, re-ingest idempotently; shared subjects/verbs
merge, entity-card embeddings refresh via the 0.95.0 dirty-queue triggers).
`?limit=` (default 32) caps the batch; a document whose extraction fails is in
`errors`, one with no `extract` model configured is in `skipped`, and neither
aborts the sweep. Returns `501` until the 0.100.0 facades are enabled. This is
what links an old document to subjects/verbs minted *after* it was ingested and
repairs a weak initial extraction (so `note_search` and graph traversal find it).
Unlike skills, document reindex **requires** an `extract` model — there is no
deterministic SVPOR fallback.

Drive it on a schedule with `deploy/maludb-memory-reindex.{service,timer}` (a
modest 6-hourly cadence, since it calls a model per document) — see the unit
header for install. (DB-side protocol: maludb_core `docs/document-reindex.md`.)

### Entity-card embeddings (maludb_core 0.95.0)

`POST /v1/memory/embeddings/run` drains the entity-card embedding queue — the
long-deferred consumer of maludb_core's 0.95.0 "semantic spine". Ingest and the
skill/document reindex sweeps mark subjects, verbs, and SVO statements *dirty*;
this worker claims them via `maludb_embedding_dirty_claim`, embeds each card's
text with the user's `embed` model, and stores the vector + refreshes semantic
neighbours via `maludb_embedding_complete` (the `bytea` is built in SQL from a
`real[]` so the wire format is the DB's own). `?kinds=subject,verb` scopes;
`?limit=` (default 64) caps the batch. It returns early when no real `embed`
model is configured (the deterministic fallback isn't worth persisting). This is
what powers the optional `similar_to` semantic jumps — for **documents and
skills alike** — so it complements both reindex workers.

Drive it on a schedule with `deploy/maludb-embedding-worker.{service,timer}` (a
brisker 15-minute cadence — cards are cheap and the claim auto-skips unchanged
ones, so empty runs are nearly free).

See [CLAUDE.md](CLAUDE.md) for detailed architecture documentation.

## Testing

```bash
pytest                             # full suite (262 tests)
pytest tests/test_subjects.py -v   # one file
ruff check app/ tests/             # lint
```

## Origin

This is a faithful port of the PHP LAMP version. The API contract (URLs, JSON shapes, error codes, status codes) is identical — the MaluDB desktop client works against either server.

Key differences from the PHP version:
- **SQLite** replaces MySQL for the local auth store
- **FastAPI routers** replace one-file-per-endpoint PHP scripts
- **psycopg v3** replaces PDO for PostgreSQL access
- **httpx** replaces cURL for outbound LLM calls

## License

MIT
