# maludb-python-simple

A Python FastAPI rewrite of [maludb-lamp-api-server](https://github.com/maludb/maludb-lamp-api-server.git) — the JSON API server for [MaluDB](https://maludb.com), a knowledge graph database.

This project serves as both a **working API server** (targeting maludb_core 0.96.0) and a **learning-friendly codebase** — every route handler contains its SQL inline, making the path from URL to query visible in one file.

## Quick Start

```bash
# Install
pip install -e ".[dev]"

# Run the dev server
uvicorn app.main:app --reload --port 8000

# Health check
curl http://localhost:8000/health
```

## Requirements

- Python 3.12+
- PostgreSQL 14+ (with maludb_core 0.96.0 installed)
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

## Architecture

- **16 router modules** in `app/routers/` covering 106 route-method combinations
- **Raw SQL** via psycopg v3 — no ORM, no query builder
- **SQLite** for auth routing (token → Postgres credentials)
- **PostgreSQL** for all tenant data via maludb_core facade views
- **FastAPI dependency injection** for per-request auth + DB connections

See [CLAUDE.md](CLAUDE.md) for detailed architecture documentation.

## Testing

```bash
pytest                             # full suite (243 tests)
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
