"""
Generic user-table API — PostgREST/Supabase-compatible CRUD over the tenant's
own application tables.

MaluDB is offerable as a hosted DBaaS: clients talk to their own tables over
HTTP with no app server in between. This router reflects ``{table}`` from the
tenant's schema (app/helpers/reflect.py — base tables only, ``maludb_*``/
``malu$*`` rejected) and serves the PostgREST grammar through the shared parser
(app/helpers/query.py). Unlike the hand-written memory routers, the SQL here is
assembled from catalog-reflected identifiers — user tables can't have
hand-written handlers by definition.

One implementation, two mounts:

- ``/rest/v1/{table}``   — wire-compatible with PostgREST, so supabase-js /
  supabase-py clients work unchanged: bare-array responses, PostgREST error
  bodies (``{"code","message","details","hint"}``), ``Prefer:`` headers,
  ``Accept: application/vnd.pgrst.object+json`` for ``.single()``.
- ``/v1/tables/{table}`` — house style: enveloped responses (``{"rows": …}``,
  ``{"inserted": n}``) and the standard ``{"error":{code,message}}`` shape.

Supported surface:

    GET/HEAD  filtering (?col=op.value, or=(), and=(), not.), ?select= (with *
              and aliases), ?order=, limit/offset, Prefer: count=exact|planned|
              estimated + Content-Range
    POST      single object or bulk array; ?columns=; upsert via
              Prefer: resolution=merge-duplicates|ignore-duplicates
              (+ ?on_conflict=, default: primary key); Prefer: return=
    PATCH     body = column:value object; filtered by the same grammar
    DELETE    filtered by the same grammar

Unknown query-param keys are **rejected** (400) rather than ignored — on a
generic write surface, a typo'd filter (``?idd=eq.5``) silently matching every
row would be a data-loss footgun. (``debug`` stays allowed for the SQL trace.)

Not implemented (documented gaps): resource embedding (``select=rel(*)``),
``/rpc/{fn}``, JSON-path operators, array operators (cs/cd/ov), ``Range``
headers, CSV bodies.
"""

from __future__ import annotations

import re

import psycopg
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb

from app.auth import Auth
from app.database import db_exec, db_query
from app.errors import APIError, _pg_error_message, classify_database_error, json_error
from app.helpers.query import (
    ParsedQuery,
    content_range,
    parse_query,
    resolve_total,
    wants_count,
)
from app.helpers.reflect import TableInfo, quote_ident, resolve_table
from app.helpers.writes import as_items

router_rest = APIRouter(prefix="/rest/v1", tags=["rest"])
router_tables = APIRouter(prefix="/v1/tables", tags=["tables"])


# ---------------------------------------------------------------------------
# Prefer / Accept header parsing
# ---------------------------------------------------------------------------

_RETURN_RE = re.compile(r"return=(representation|minimal|headers-only)")
_RESOLUTION_RE = re.compile(r"resolution=(merge-duplicates|ignore-duplicates)")
_OBJECT_ACCEPT = "application/vnd.pgrst.object+json"


def wants_return(request: Request) -> str | None:
    """The ``Prefer: return=…`` preference, or None (PostgREST default: minimal)."""
    m = _RETURN_RE.search(request.headers.get("prefer", ""))
    return m.group(1) if m else None


def wants_resolution(request: Request) -> str | None:
    """The ``Prefer: resolution=…`` upsert preference, or None (plain insert)."""
    m = _RESOLUTION_RE.search(request.headers.get("prefer", ""))
    return m.group(1) if m else None


def wants_object(request: Request) -> bool:
    """True when the client asked for a single JSON object (supabase ``.single()``)."""
    return _OBJECT_ACCEPT in request.headers.get("accept", "")


# ---------------------------------------------------------------------------
# Strict query-key validation
# ---------------------------------------------------------------------------

# Keys the grammar itself consumes, plus `debug` (the ?debug=1 SQL trace).
_GRAMMAR_KEYS = frozenset({"select", "order", "limit", "offset", "or", "and", "debug"})


def _check_known_keys(query_params, spec, extra: frozenset[str] = frozenset()) -> None:
    """Reject query keys that are neither grammar keys nor spec columns."""
    for key in query_params.keys():
        if key in _GRAMMAR_KEYS or key in extra:
            continue
        if key not in spec.columns:
            json_error("unknown_column", f"Unknown query parameter or column '{key}'.", 400)


def _known_column(ti: TableInfo, name: str, where: str) -> str:
    """Validate a client-supplied column name against the reflected table."""
    if name not in ti.columns:
        json_error("unknown_column", f"Could not find the '{name}' column of '{ti.name}' in {where}.", 400)
    return name


# ---------------------------------------------------------------------------
# SQL builders (unit-testable, no I/O)
# ---------------------------------------------------------------------------


def _unquote(name: str) -> str:
    """Strip PostgREST-style double quotes from a client column name
    (supabase clients send ``?columns="sku","name"``)."""
    if len(name) >= 2 and name.startswith('"') and name.endswith('"'):
        return name[1:-1].replace('""', '"')
    return name


def _adapt(value):
    """Bind dict/list values as jsonb so JSON columns accept object bodies."""
    if isinstance(value, (dict, list)):
        return Jsonb(value)
    return value


def _jsonable(data):
    """JSON-encode DB values (Decimal, datetime, UUID, …) for a direct JSONResponse."""
    from fastapi.encoders import jsonable_encoder

    return jsonable_encoder(data)


def build_insert_sql(
    ti: TableInfo,
    items: list[dict],
    columns_param: str | None,
    on_conflict_param: str | None,
    resolution: str | None,
    returning: str | None,
) -> tuple[str, list]:
    """Assemble one multi-row ``INSERT`` (optionally ``ON CONFLICT``) statement.

    Insert columns come from ``?columns=`` when given, else the union of the
    items' keys (missing keys per row insert as ``DEFAULT``). All column names
    are validated against the reflected table and quoted from catalog names.
    """
    if columns_param is not None:
        cols = [_unquote(c.strip()) for c in columns_param.split(",") if c.strip()]
        if not cols:
            json_error("bad_request", "Empty 'columns' list.", 400)
    else:
        cols = []
        for item in items:
            for key in item:
                if key not in cols:
                    cols.append(key)
    for c in cols:
        _known_column(ti, c, "the insert body")

    params: list = []
    if not cols:
        # All items are {} — Postgres only supports DEFAULT VALUES for one row.
        if len(items) != 1:
            json_error("bad_request", "Bulk insert of empty objects is not supported.", 400)
        sql = f"INSERT INTO {ti.ident} DEFAULT VALUES"
    else:
        value_rows: list[str] = []
        for item in items:
            placeholders: list[str] = []
            for c in cols:
                if c in item:
                    placeholders.append("%s")
                    params.append(_adapt(item[c]))
                else:
                    placeholders.append("DEFAULT")
            value_rows.append("(" + ", ".join(placeholders) + ")")
        col_list = ", ".join(quote_ident(c) for c in cols)
        sql = f"INSERT INTO {ti.ident} ({col_list}) VALUES {', '.join(value_rows)}"

    if resolution:
        if on_conflict_param:
            target = [
                _known_column(ti, _unquote(c.strip()), "'on_conflict'")
                for c in on_conflict_param.split(",")
                if c.strip()
            ]
        else:
            target = ti.pk
        if not target:
            json_error(
                "bad_request",
                f"Upsert on '{ti.name}' needs '?on_conflict=' — the table has no primary key.",
                400,
            )
        target_sql = ", ".join(quote_ident(c) for c in target)
        update_cols = [c for c in cols if c not in target]
        if resolution == "ignore-duplicates" or not update_cols:
            sql += f" ON CONFLICT ({target_sql}) DO NOTHING"
        else:
            set_sql = ", ".join(f"{quote_ident(c)} = EXCLUDED.{quote_ident(c)}" for c in update_cols)
            sql += f" ON CONFLICT ({target_sql}) DO UPDATE SET {set_sql}"

    if returning:
        sql += f" RETURNING {returning}"
    return sql, params


def build_update_sql(ti: TableInfo, body: dict, qp: ParsedQuery, returning: str | None) -> tuple[str, list]:
    """Assemble ``UPDATE … SET … [WHERE …]`` from a column:value body + parsed filters."""
    if not body:
        json_error("bad_request", "PATCH body must contain at least one column.", 400)
    set_parts: list[str] = []
    params: list = []
    for key, value in body.items():
        _known_column(ti, key, "the update body")
        set_parts.append(f"{quote_ident(key)} = %s")
        params.append(_adapt(value))
    sql = f"UPDATE {ti.ident} SET {', '.join(set_parts)} {qp.where_sql}"
    params.extend(qp.where_params)
    if returning:
        sql += f" RETURNING {returning}"
    return sql, params


def build_delete_sql(ti: TableInfo, qp: ParsedQuery, returning: str | None) -> tuple[str, list]:
    """Assemble ``DELETE FROM … [WHERE …]`` from parsed filters."""
    sql = f"DELETE FROM {ti.ident} {qp.where_sql}"
    if returning:
        sql += f" RETURNING {returning}"
    return sql, list(qp.where_params)


# ---------------------------------------------------------------------------
# Core operations (flavor-independent)
# ---------------------------------------------------------------------------


def _single_object(request: Request, rows: list[dict]):
    """Apply the ``.single()`` Accept header: exactly one row or 406."""
    if len(rows) != 1:
        json_error(
            "object_mismatch",
            f"JSON object requested, multiple (or no) rows returned ({len(rows)} rows).",
            406,
        )
    return rows[0]


def _get_core(auth, request: Request, response: Response, table: str):
    """Shared read path: reflect, parse, select, count. Returns the row list."""
    ti = resolve_table(auth.conn, table)
    _check_known_keys(request.query_params, ti.spec)
    qp = parse_query(request.query_params, ti.spec)
    sql = f"SELECT {qp.select_list} FROM {ti.ident} {qp.where_sql} {qp.order_sql} {qp.limit_sql}"
    rows = db_query(auth.conn, sql, qp.where_params + qp.limit_params)
    total = resolve_total(auth.conn, wants_count(request), ti.ident, qp.where_sql, qp.where_params)
    response.headers["Content-Range"] = content_range(qp.offset, len(rows), total)
    return rows


async def _insert_core(auth, request: Request, table: str) -> tuple[list[dict] | None, int]:
    """Shared insert/upsert path. Returns ``(rows|None, affected)`` — rows only
    when ``Prefer: return=representation``."""
    ti = resolve_table(auth.conn, table)
    _check_known_keys(request.query_params, ti.spec, frozenset({"columns", "on_conflict"}))
    items, _ = as_items(await request.json())
    representation = wants_return(request) == "representation"
    if not items:
        return ([] if representation else None), 0

    qp = parse_query(request.query_params, ti.spec)  # select list for RETURNING
    sql, params = build_insert_sql(
        ti,
        items,
        request.query_params.get("columns"),
        request.query_params.get("on_conflict"),
        wants_resolution(request),
        qp.select_list if representation else None,
    )
    if representation:
        rows = db_query(auth.conn, sql, params)
        return rows, len(rows)
    return None, db_exec(auth.conn, sql, params)


async def _update_core(auth, request: Request, table: str) -> tuple[list[dict] | None, int]:
    """Shared update path. Returns ``(rows|None, affected)``."""
    ti = resolve_table(auth.conn, table)
    _check_known_keys(request.query_params, ti.spec)
    body = await request.json()
    if not isinstance(body, dict):
        json_error("validation_failed", "PATCH body must be a JSON object of column:value pairs.", 422)
    qp = parse_query(request.query_params, ti.spec)
    representation = wants_return(request) == "representation"
    sql, params = build_update_sql(ti, body, qp, qp.select_list if representation else None)
    if representation:
        rows = db_query(auth.conn, sql, params)
        return rows, len(rows)
    return None, db_exec(auth.conn, sql, params)


def _delete_core(auth, request: Request, table: str) -> tuple[list[dict] | None, int]:
    """Shared delete path. Returns ``(rows|None, affected)``."""
    ti = resolve_table(auth.conn, table)
    _check_known_keys(request.query_params, ti.spec)
    qp = parse_query(request.query_params, ti.spec)
    representation = wants_return(request) == "representation"
    sql, params = build_delete_sql(ti, qp, qp.select_list if representation else None)
    if representation:
        rows = db_query(auth.conn, sql, params)
        return rows, len(rows)
    return None, db_exec(auth.conn, sql, params)


# ---------------------------------------------------------------------------
# PostgREST error shape (rest flavor only — /v1/tables uses the global handlers)
# ---------------------------------------------------------------------------

# Our APIError codes → PostgREST error codes (the ones supabase clients key on).
_PGRST_CODES = {
    "table_not_found": "PGRST205",
    "unknown_column": "PGRST204",
    "object_mismatch": "PGRST116",
    "bad_request": "PGRST100",
    "validation_failed": "PGRST102",
}


def _pgrst_error(exc: Exception) -> JSONResponse:
    """Convert an APIError / psycopg DatabaseError to a PostgREST error body."""
    if isinstance(exc, APIError):
        return JSONResponse(
            status_code=exc.status,
            content={
                "code": _PGRST_CODES.get(exc.code, "PGRST100"),
                "message": exc.message,
                "details": None,
                "hint": None,
            },
        )
    # psycopg DatabaseError → PostgREST passes the SQLSTATE through as the code.
    status, _, sqlstate = classify_database_error(exc)
    diag = getattr(exc, "diag", None)
    return JSONResponse(
        status_code=status,
        content={
            "code": sqlstate or "XX000",
            "message": _pg_error_message(exc),
            "details": getattr(diag, "message_detail", None) if diag else None,
            "hint": getattr(diag, "message_hint", None) if diag else None,
        },
    )


_PGRST_ERRORS = (APIError, psycopg.errors.DatabaseError)


# ---------------------------------------------------------------------------
# /rest/v1 — PostgREST wire-compatible mount
# ---------------------------------------------------------------------------


@router_rest.api_route("/{table}", methods=["GET", "HEAD"])
def rest_select(table: str, auth: Auth, request: Request, response: Response):
    try:
        rows = _get_core(auth, request, response, table)
        return _single_object(request, rows) if wants_object(request) else rows
    except _PGRST_ERRORS as exc:
        return _pgrst_error(exc)


@router_rest.post("/{table}", status_code=201)
async def rest_insert(table: str, auth: Auth, request: Request):
    try:
        rows, _ = await _insert_core(auth, request, table)
        if rows is None:
            return Response(status_code=201)
        body = _single_object(request, rows) if wants_object(request) else rows
        return JSONResponse(status_code=201, content=_jsonable(body))
    except _PGRST_ERRORS as exc:
        return _pgrst_error(exc)


@router_rest.patch("/{table}")
async def rest_update(table: str, auth: Auth, request: Request):
    try:
        rows, _ = await _update_core(auth, request, table)
        if rows is None:
            return Response(status_code=204)
        return _single_object(request, rows) if wants_object(request) else rows
    except _PGRST_ERRORS as exc:
        return _pgrst_error(exc)


@router_rest.delete("/{table}")
def rest_delete(table: str, auth: Auth, request: Request):
    try:
        rows, _ = _delete_core(auth, request, table)
        if rows is None:
            return Response(status_code=204)
        return _single_object(request, rows) if wants_object(request) else rows
    except _PGRST_ERRORS as exc:
        return _pgrst_error(exc)


# ---------------------------------------------------------------------------
# /v1/tables — house-style mount (standard envelopes + global error handlers)
# ---------------------------------------------------------------------------


@router_tables.api_route("/{table}", methods=["GET", "HEAD"])
def tables_select(table: str, auth: Auth, request: Request, response: Response):
    return {"rows": _get_core(auth, request, response, table)}


@router_tables.post("/{table}", status_code=201)
async def tables_insert(table: str, auth: Auth, request: Request):
    rows, affected = await _insert_core(auth, request, table)
    return {"inserted": affected} if rows is None else {"rows": rows}


@router_tables.patch("/{table}")
async def tables_update(table: str, auth: Auth, request: Request):
    rows, affected = await _update_core(auth, request, table)
    return {"updated": affected} if rows is None else {"rows": rows}


@router_tables.delete("/{table}")
def tables_delete(table: str, auth: Auth, request: Request):
    rows, affected = _delete_core(auth, request, table)
    return {"deleted": affected} if rows is None else {"rows": rows}
