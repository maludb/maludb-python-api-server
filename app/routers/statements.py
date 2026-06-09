"""
Statement endpoints — list, create, detail, update (provenance/close), delete.

Ports PHP's statements.php and statements_id.php.

A statement is (subject_kind, subject_id) --verb_id--> (object_kind, object_id).
Created via the idempotent maludb_svpor_statement_create(...) facade. Everything
runs inside db_tx_core() so the facade can resolve its malu$* base tables + RLS grants.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.auth import Auth
from app.database import db_one, db_query, db_tx_core
from app.errors import json_error
from app.helpers.statements import STATEMENT_COLS, shape_statement, svpor_create_statement

router = APIRouter()


# ---------------------------------------------------------------------------
# Helper — load a single shaped statement row
# ---------------------------------------------------------------------------


def _load_statement(conn, statement_id: int) -> dict | None:
    """Fetch a single statement, or None if not found."""
    row = db_one(
        conn,
        f"SELECT {STATEMENT_COLS} FROM maludb_svpor_statement WHERE statement_id = %s",
        [statement_id],
    )
    if row is None:
        return None
    shape_statement(row)
    return row


# ===========================================================================
# GET /v1/statements — list statements
# ===========================================================================


@router.get("/v1/statements")
def list_statements(
    auth: Auth,
    provenance: str | None = Query(default=None, max_length=40),
    object_kind: str | None = Query(default=None, max_length=40),
    subject_kind: str | None = Query(default=None, max_length=40),
    object_id: int | None = Query(default=None),
    subject_id: int | None = Query(default=None),
    verb_id: int | None = Query(default=None),
    limit: int = Query(default=50, le=200),
):
    def _query(conn):
        clauses: list[str] = []
        params: list = []
        if provenance:
            clauses.append("provenance = %s")
            params.append(provenance)
        if object_kind:
            clauses.append("object_kind = %s")
            params.append(object_kind)
        if subject_kind:
            clauses.append("subject_kind = %s")
            params.append(subject_kind)
        if object_id is not None:
            clauses.append("object_id = %s")
            params.append(object_id)
        if subject_id is not None:
            clauses.append("subject_id = %s")
            params.append(subject_id)
        if verb_id is not None:
            clauses.append("verb_id = %s")
            params.append(verb_id)

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        sql = f"""SELECT {STATEMENT_COLS}
                    FROM maludb_svpor_statement
                    {where}
                   ORDER BY statement_id DESC
                   LIMIT %s"""
        params.append(limit)

        rows = db_query(conn, sql, params)
        for r in rows:
            shape_statement(r)
        return rows

    rows = db_tx_core(auth.conn, _query)
    return {"statements": rows}


# ===========================================================================
# POST /v1/statements — create a statement
# ===========================================================================


@router.post("/v1/statements")
async def create_statement(auth: Auth, request: Request):
    body = await request.json()

    def _create(conn):
        return svpor_create_statement(conn, body)

    stmt = db_tx_core(auth.conn, _create)
    return JSONResponse(status_code=201, content={"statement": stmt})


# ===========================================================================
# GET /v1/statements/{id} — statement detail
# ===========================================================================


@router.get("/v1/statements/{statement_id}")
def get_statement(statement_id: int, auth: Auth):
    def _get(conn):
        return _load_statement(conn, statement_id)

    stmt = db_tx_core(auth.conn, _get)
    if stmt is None:
        json_error("not_found", "Statement not found.", 404)
    return {"statement": stmt}


# ===========================================================================
# PATCH /v1/statements/{id} — update provenance and/or close
# ===========================================================================


@router.patch("/v1/statements/{statement_id}")
async def update_statement(statement_id: int, auth: Auth, request: Request):
    body = await request.json()

    set_provenance = (
        "provenance" in body
        and body["provenance"] is not None
        and str(body["provenance"]).strip() != ""
    )
    do_close = (
        ("close" in body and body["close"] is True)
        or "valid_to" in body
    )

    if not set_provenance and not do_close:
        json_error(
            "bad_request",
            "No updatable fields provided (provenance, valid_to, close).",
            400,
        )

    def _update(conn):
        if db_one(conn, "SELECT 1 FROM maludb_svpor_statement WHERE statement_id = %s", [statement_id]) is None:
            return None
        if set_provenance:
            db_one(
                conn,
                "SELECT maludb_svpor_statement_set_provenance(%s, %s)",
                [statement_id, str(body["provenance"])],
            )
        if do_close:
            # close:true -> now(); explicit valid_to -> that timestamp (null also closes at now()).
            valid_to = (
                str(body["valid_to"])
                if "valid_to" in body and body["valid_to"] is not None
                else None
            )
            db_one(
                conn,
                "SELECT maludb_svpor_statement_close(%s, COALESCE(%s::timestamptz, now()))",
                [statement_id, valid_to],
            )
        return _load_statement(conn, statement_id)

    stmt = db_tx_core(auth.conn, _update)
    if stmt is None:
        json_error("not_found", "Statement not found.", 404)
    return {"statement": stmt}


# ===========================================================================
# DELETE /v1/statements/{id} — delete a statement
# ===========================================================================


@router.delete("/v1/statements/{statement_id}")
def delete_statement(statement_id: int, auth: Auth):
    def _delete(conn):
        if db_one(conn, "SELECT 1 FROM maludb_svpor_statement WHERE statement_id = %s", [statement_id]) is None:
            return False
        db_one(conn, "SELECT maludb_svpor_statement_delete(%s)", [statement_id])
        return True

    deleted = db_tx_core(auth.conn, _delete)
    if not deleted:
        json_error("not_found", "Statement not found.", 404)
    return {"deleted": True, "id": statement_id}
