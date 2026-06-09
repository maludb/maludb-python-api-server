"""
SQLite auth store — replaces MySQL from the PHP version.

Ports PHP's LocalDatabase class (config/local-database.php): token resolution,
model prompt lookup, and next_user_id.  Schema lives in config/auth_store.sql.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Schema file path (relative to project root)
_SCHEMA_FILE = Path(__file__).resolve().parent.parent / "config" / "auth_store.sql"


class AuthStore:
    """SQLite-backed auth/routing store.

    One instance per process (the FastAPI app keeps a singleton).  All reads
    are synchronous — SQLite is fast enough for a lookup table, and the auth
    check runs once per request.
    """

    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    # ------------------------------------------------------------------
    # Schema bootstrap
    # ------------------------------------------------------------------

    def init_db(self) -> None:
        """Read and execute config/auth_store.sql to create tables if needed."""
        schema = _SCHEMA_FILE.read_text()
        self._conn.executescript(schema)

    # ------------------------------------------------------------------
    # Token resolution (called by require_auth)
    # ------------------------------------------------------------------

    def resolve_token(self, token_hash: str) -> dict | None:
        """Look up a user row by sha256 token hash; returns None if unknown or expired."""
        cursor = self._conn.execute(
            """
            SELECT user_id, role, pg_dbname, pg_user, pg_password
              FROM users
             WHERE token_hash = ?
               AND (expires_at IS NULL OR expires_at > strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
             LIMIT 1
            """,
            (token_hash,),
        )
        row = cursor.fetchone()
        return dict(row) if row is not None else None

    # ------------------------------------------------------------------
    # Next user ID (for token-create when no user_id is supplied)
    # ------------------------------------------------------------------

    def next_user_id(self) -> int:
        """Return MAX(user_id) + 1, or 1 if the table is empty."""
        cursor = self._conn.execute("SELECT COALESCE(MAX(user_id), 0) + 1 AS n FROM users")
        row = cursor.fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------
    # Model prompt lookup (for LLM pipeline)
    # ------------------------------------------------------------------

    def model_prompt(self, model_name: str) -> dict | None:
        """Load the per-model extraction prompt + LLM connection, or None."""
        cursor = self._conn.execute(
            """
            SELECT model_name, model_identifier, api_format, system_prompt,
                   base_url, api_key, max_tokens, generation_params
              FROM model_prompts
             WHERE model_name = ?
             LIMIT 1
            """,
            (model_name,),
        )
        row = cursor.fetchone()
        return dict(row) if row is not None else None

    # ------------------------------------------------------------------
    # Raw connection (for testing / admin endpoints)
    # ------------------------------------------------------------------

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn
