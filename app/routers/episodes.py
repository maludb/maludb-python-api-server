"""
Episode endpoints — list, create, detail, update, delete, event-scoped statements.

Ports PHP's episodes.php, episodes_id.php, episodes_id_statements.php.

Live-schema mapping (DB column -> API field):
    episode_id     -> id
    episode_kind   -> kind
    payload_jsonb  -> payload
    subject_id     -> subject_id  (0.94.0+ backing subject)
    canonical_name -> canonical_name (server-minted dated name)
Source view: maludb_episode (writable).
Default kind: 'activity'. Default sensitivity: 'internal'. Default provenance: 'provided'.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.auth import Auth
from app.database import db_exec, db_one, db_query, db_tx_core
from app.errors import json_error
from app.helpers.attributes import attach_attributes
from app.helpers.statements import STATEMENT_COLS, shape_statement, svpor_create_statement

router = APIRouter()

# ---------------------------------------------------------------------------
# Column list and row shaper
# ---------------------------------------------------------------------------

EPISODE_COLS = """episode_id AS id, episode_kind AS kind, title, summary,
                  payload_jsonb AS payload, occurred_at, occurred_until, recorded_at,
                  sensitivity, lifecycle_state, provenance, created_at,
                  subject_id, canonical_name"""


def shape_episode(row: dict[str, Any]) -> None:
    """Normalize scalar types on an episode row *in place*.

    Mirrors PHP's shape_episode(): cast id and subject_id to int,
    decode payload from JSON string (if still a string).
    """
    row["id"] = int(row["id"])
    row["subject_id"] = int(row["subject_id"]) if row["subject_id"] is not None else None
    # Decode payload — psycopg v3 may auto-decode jsonb, so handle both cases.
    payload = row.get("payload")
    if payload is None:
        row["payload"] = None
    elif isinstance(payload, str):
        row["payload"] = json.loads(payload)
    # else: already decoded by psycopg (dict) — leave as-is


# ===========================================================================
# GET /v1/episodes — list episodes
# ===========================================================================


@router.get("/v1/episodes")
def list_episodes(
    auth: Auth,
    q: str | None = Query(default=None, max_length=200),
    kind: str | None = Query(default=None, max_length=120),
    provenance: str | None = Query(default=None, max_length=40),
    limit: int = Query(default=50, le=200),
    with_: str | None = Query(default=None, alias="with", max_length=40),
):
    def _query(conn):
        clauses: list[str] = []
        params: list = []
        if kind:
            clauses.append("episode_kind = %s")
            params.append(kind)
        if provenance:
            clauses.append("provenance = %s")
            params.append(provenance)
        if q:
            clauses.append("(title ILIKE %s OR summary ILIKE %s)")
            params.extend([f"%{q}%", f"%{q}%"])

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        sql = f"""SELECT {EPISODE_COLS}
                    FROM maludb_episode
                    {where}
                   ORDER BY occurred_at DESC NULLS LAST, episode_id DESC
                   LIMIT %s"""
        params.append(limit)

        rows = db_query(conn, sql, params)
        for r in rows:
            shape_episode(r)

        if with_ == "attributes":
            attach_attributes(conn, rows, "maludb_episode_with_attributes", "episode_id")

        return rows

    rows = db_tx_core(auth.conn, _query)
    return {"episodes": rows}


# ===========================================================================
# POST /v1/episodes — create an episode
# ===========================================================================


@router.post("/v1/episodes")
async def create_episode(auth: Auth, request: Request):
    body = await request.json()

    title = (body.get("title") or "").strip() if isinstance(body.get("title"), str) else ""
    if not title:
        json_error("missing_field", 'Field "title" is required.', 400)

    kind = (
        str(body["kind"])
        if "kind" in body and body["kind"] is not None and str(body["kind"]).strip()
        else "activity"
    )
    summary = str(body["summary"]) if body.get("summary") is not None else None
    occurred_at = str(body["occurred_at"]) if body.get("occurred_at") is not None else None
    occurred_until = str(body["occurred_until"]) if body.get("occurred_until") is not None else None
    sensitivity = (
        str(body["sensitivity"])
        if "sensitivity" in body and body["sensitivity"] is not None and str(body["sensitivity"]).strip()
        else "internal"
    )
    provenance = (
        str(body["provenance"])
        if "provenance" in body and body["provenance"] is not None and str(body["provenance"]).strip()
        else "provided"
    )
    payload_json = (
        json.dumps(body["payload"])
        if "payload" in body and isinstance(body["payload"], dict)
        else "{}"
    )

    def _create(conn):
        row = db_one(
            conn,
            """SELECT maludb_register_episode(
                        p_episode_kind   => %s,
                        p_title          => %s,
                        p_summary        => %s,
                        p_payload_jsonb  => %s::jsonb,
                        p_occurred_at    => %s::timestamptz,
                        p_occurred_until => %s::timestamptz,
                        p_sensitivity    => %s,
                        p_provenance     => %s
                    ) AS id""",
            [kind, title, summary, payload_json, occurred_at, occurred_until, sensitivity, provenance],
        )
        return db_one(
            conn,
            f"SELECT {EPISODE_COLS} FROM maludb_episode WHERE episode_id = %s",
            [int(row["id"])],
        )

    episode = db_tx_core(auth.conn, _create)
    shape_episode(episode)

    return JSONResponse(status_code=201, content={"episode": episode})


# ===========================================================================
# GET /v1/episodes/{id} — assembled event via maludb_episode_get()
# ===========================================================================


@router.get("/v1/episodes/{episode_id}")
def get_episode(episode_id: int, auth: Auth):
    def _get(conn):
        row = db_one(conn, "SELECT maludb_episode_get(%s) AS j", [episode_id])
        if row is None or row["j"] is None:
            return None
        j = row["j"]
        if isinstance(j, str):
            return json.loads(j)
        return j  # psycopg may auto-decode jsonb

    event = db_tx_core(auth.conn, _get)
    if event is None:
        json_error("not_found", "Episode not found.", 404)
    return event


# ===========================================================================
# PATCH /v1/episodes/{id} — update episode fields
# ===========================================================================


@router.patch("/v1/episodes/{episode_id}")
async def update_episode(episode_id: int, auth: Auth, request: Request):
    body = await request.json()

    # Map request fields -> (column, value, placeholder-with-optional-cast).
    fields: list[str] = []
    params: list = []

    if "title" in body:
        t = str(body["title"]).strip() if body["title"] is not None else ""
        if not t:
            json_error("validation_failed", 'Field "title" cannot be empty.', 422)
        fields.append("title = %s")
        params.append(t)
    if "summary" in body:
        fields.append("summary = %s")
        params.append(None if body["summary"] is None else str(body["summary"]))
    if "kind" in body:
        fields.append("episode_kind = %s")
        params.append(str(body["kind"]))
    if "payload" in body:
        fields.append("payload_jsonb = %s::jsonb")
        params.append(None if body["payload"] is None else json.dumps(body["payload"]))
    if "occurred_at" in body:
        fields.append("occurred_at = %s::timestamptz")
        params.append(None if body["occurred_at"] is None else str(body["occurred_at"]))
    if "occurred_until" in body:
        fields.append("occurred_until = %s::timestamptz")
        params.append(None if body["occurred_until"] is None else str(body["occurred_until"]))
    if "sensitivity" in body:
        fields.append("sensitivity = %s")
        params.append(str(body["sensitivity"]))
    if "provenance" in body:
        fields.append("provenance = %s")
        params.append(str(body["provenance"]))
    if "lifecycle_state" in body:
        fields.append("lifecycle_state = %s")
        params.append(str(body["lifecycle_state"]))

    if not fields:
        json_error(
            "bad_request",
            "No updatable fields provided"
            " (title, summary, kind, payload, occurred_at, occurred_until,"
            " sensitivity, provenance, lifecycle_state).",
            400,
        )

    params.append(episode_id)

    def _update(conn):
        n = db_exec(
            conn,
            f"UPDATE maludb_episode SET {', '.join(fields)} WHERE episode_id = %s",
            params,
        )
        if n == 0:
            return None
        # Re-load the assembled event via maludb_episode_get.
        row = db_one(conn, "SELECT maludb_episode_get(%s) AS j", [episode_id])
        if row is None or row["j"] is None:
            return None
        j = row["j"]
        if isinstance(j, str):
            return json.loads(j)
        return j

    event = db_tx_core(auth.conn, _update)
    if event is None:
        json_error("not_found", "Episode not found.", 404)
    return event


# ===========================================================================
# DELETE /v1/episodes/{id} — delete an episode
# ===========================================================================


@router.delete("/v1/episodes/{episode_id}")
def delete_episode(episode_id: int, auth: Auth):
    def _delete(conn):
        return db_exec(conn, "DELETE FROM maludb_episode WHERE episode_id = %s", [episode_id])

    n = db_tx_core(auth.conn, _delete)
    if n == 0:
        json_error("not_found", "Episode not found.", 404)
    return {"deleted": True, "id": episode_id}


# ===========================================================================
# GET /v1/episodes/{id}/statements — event-scoped statements
# ===========================================================================


@router.get("/v1/episodes/{episode_id}/statements")
def list_episode_statements(episode_id: int, auth: Auth):
    def _query(conn):
        if db_one(conn, "SELECT 1 FROM maludb_episode WHERE episode_id = %s", [episode_id]) is None:
            return None
        rows = db_query(
            conn,
            f"""SELECT {STATEMENT_COLS}
                  FROM maludb_svpor_statement
                 WHERE object_kind = 'episode_object' AND object_id = %s
                 ORDER BY statement_id DESC""",
            [episode_id],
        )
        for r in rows:
            shape_statement(r)
        return rows

    result = db_tx_core(auth.conn, _query)
    if result is None:
        json_error("not_found", "Episode not found.", 404)
    return {"statements": result}


# ===========================================================================
# POST /v1/episodes/{id}/statements — add link to event
# ===========================================================================


@router.post("/v1/episodes/{episode_id}/statements")
async def create_episode_statement(episode_id: int, auth: Auth, request: Request):
    body = await request.json()

    def _create(conn):
        if db_one(conn, "SELECT 1 FROM maludb_episode WHERE episode_id = %s", [episode_id]) is None:
            return None
        return svpor_create_statement(
            conn, body, force_object={"kind": "episode_object", "id": episode_id}
        )

    stmt = db_tx_core(auth.conn, _create)
    if stmt is None:
        json_error("not_found", "Episode not found.", 404)
    return JSONResponse(status_code=201, content={"statement": stmt})
