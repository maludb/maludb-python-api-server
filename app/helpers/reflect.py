"""
Catalog reflection for the generic user-table API (/rest/v1 and /v1/tables).

MaluDB tenants own their schema (named after the connecting role) and may create
their own application tables in it, alongside the maludb_* facade views. This
module resolves a URL's ``{table}`` segment against ``information_schema`` for
the **tenant's own schema only** (the schema named after the connecting role),
so the generic router can build a ``QuerySpec`` for
the shared PostgREST-style parser (app/helpers/query.py) without any hand-written
per-table code.

Safety properties:

- Only BASE TABLEs in the tenant role's own schema are visible — the maludb_* facades
  are views and the malu$* storage lives in other schemas, so neither reflects;
  both prefixes are additionally rejected by name before touching the catalog.
- Every SQL identifier the router splices comes from the catalog rows returned
  here (quoted via ``quote_ident``), never from client input. Client-supplied
  column names are resolved through ``TableInfo.columns`` or rejected.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from app.database import db_query
from app.errors import APIError
from app.helpers.query import Col, QuerySpec

# PostgREST has no default page size; Supabase caps at max-rows=1000. We follow
# the cap and default to it (an unbounded default invites accidental full scans).
REST_DEFAULT_LIMIT = 1000
REST_MAX_LIMIT = 1000

# Prefixes reserved for MaluDB memory structures — never served generically.
_RESERVED_PREFIXES = ("maludb_", "malu$")

# information_schema.columns.data_type → Python type for filter-value coercion.
# Only exact coercions: ints and booleans. Everything else (numeric, floats,
# timestamps, json, …) passes through as text and Postgres casts it — coercing
# numeric through Python float would silently lose precision past ~15 digits.
_TYPE_MAP: dict[str, type] = {
    "smallint": int,
    "integer": int,
    "bigint": int,
    "boolean": bool,
}


def quote_ident(name: str) -> str:
    """Double-quote a SQL identifier (doubling embedded quotes)."""
    return '"' + name.replace('"', '""') + '"'


@dataclass(frozen=True)
class TableInfo:
    """A reflected user table: identifiers, column types, PK, and its QuerySpec."""

    name: str  # catalog name, as stored
    ident: str  # quoted identifier for splicing into SQL
    columns: dict[str, type]  # column name → Python type (insertion order = ordinal)
    data_types: dict[str, str]  # column name → information_schema data_type ('ARRAY', 'jsonb', …)
    pk: list[str]  # primary-key column names ([] if none)
    spec: QuerySpec  # allowlist for parse_query


def table_not_found(table: str) -> APIError:
    return APIError(
        "table_not_found",
        f"Table '{table}' does not exist in your schema.",
        404,
    )


def resolve_table(conn: psycopg.Connection, table: str) -> TableInfo:
    """Reflect ``table`` from the tenant's own schema, or raise 404.

    The tenant schema is *named after the connecting role* (maludb_core's
    onboarding contract), so scoping is on ``current_user`` — not
    ``current_schema()``, which could resolve to a shared schema (e.g.
    maludb_core or public) for a role without its own schema and re-open the
    cross-tenant access the table-scope decision excluded.

    Reflection runs per request (two catalog lookups) so DDL is visible
    immediately — no cache-staleness window after a CREATE/ALTER TABLE.
    """
    if not table or table.startswith(_RESERVED_PREFIXES):
        raise table_not_found(table)

    cols = db_query(
        conn,
        """
        SELECT c.column_name, c.data_type
          FROM information_schema.columns c
          JOIN information_schema.tables t
            ON t.table_schema = c.table_schema AND t.table_name = c.table_name
         WHERE c.table_schema = current_user
           AND t.table_type = 'BASE TABLE'
           AND c.table_name = %s
         ORDER BY c.ordinal_position
        """,
        [table],
    )
    if not cols:
        raise table_not_found(table)

    # Primary-key KEY columns only: indkey also lists INCLUDE (non-key) columns,
    # so walk just the first indnkeyatts entries.
    pk_rows = db_query(
        conn,
        """
        SELECT a.attname
          FROM pg_index i
          JOIN pg_class cl ON cl.oid = i.indrelid
          JOIN pg_namespace n ON n.oid = cl.relnamespace
          CROSS JOIN LATERAL generate_series(0, i.indnkeyatts - 1) AS k(ord)
          JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[k.ord]
         WHERE n.nspname = current_user
           AND cl.relname = %s
           AND i.indisprimary
         ORDER BY k.ord
        """,
        [table],
    )

    columns = {r["column_name"]: _TYPE_MAP.get(r["data_type"], str) for r in cols}
    data_types = {r["column_name"]: r["data_type"] for r in cols}
    spec = QuerySpec(
        columns={name: Col(quote_ident(name), typ) for name, typ in columns.items()},
        default_order=[],  # PostgREST applies no default ORDER BY
        default_limit=REST_DEFAULT_LIMIT,
        max_limit=REST_MAX_LIMIT,
    )
    return TableInfo(
        name=table,
        ident=quote_ident(table),
        columns=columns,
        data_types=data_types,
        pk=[r["attname"] for r in pk_rows],
        spec=spec,
    )
