"""
Verb endpoints — list, create, detail, update, delete, and linked subjects.

Ports PHP's /v1/verbs (verbs.php), /v1/verbs/{id} (verbs_id.php),
and /v1/verbs/{id}/subjects (verbs_id_subjects.php).

Live-schema mapping (DB column -> API field):
  verb_id        -> id
  verb_type      -> type
  canonical_name, description, classifier_md -> same

Subject links live in maludb_subject_verb keyed by verb_name (= canonical_name).
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.auth import Auth
from app.database import db_exec, db_one, db_query
from app.errors import json_error

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_verb_detail(auth: Auth, verb_id: int) -> dict | None:
    """Fetch a verb with its embedded subjects[], or None if not found."""
    verb = db_one(
        auth.conn,
        """SELECT verb_id        AS id,
                  canonical_name AS canonical_name,
                  verb_type      AS type,
                  description,
                  classifier_md
             FROM maludb_verb
            WHERE verb_id = %s""",
        [verb_id],
    )
    if verb is None:
        return None
    verb["id"] = int(verb["id"])

    subjects = db_query(
        auth.conn,
        """SELECT s.subject_id     AS id,
                  s.canonical_name AS label,
                  s.subject_type   AS type
             FROM maludb_subject_verb sv
             JOIN maludb_subject s ON s.canonical_name = sv.subject_name
            WHERE sv.verb_name = %s
            ORDER BY s.canonical_name""",
        [verb["canonical_name"]],
    )
    for s in subjects:
        s["id"] = int(s["id"])
    verb["subjects"] = subjects

    return verb


# ---------------------------------------------------------------------------
# GET /v1/verbs — list verbs
# ---------------------------------------------------------------------------


@router.get("/v1/verbs")
async def list_verbs(
    auth: Auth,
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, le=200),
) -> JSONResponse:
    where = ""
    params: list = []
    if q:
        where = "WHERE v.canonical_name ILIKE %s OR v.description ILIKE %s"
        params = [f"%{q}%", f"%{q}%"]

    sql = f"""SELECT v.verb_id        AS id,
                     v.canonical_name AS canonical_name,
                     v.verb_type      AS type,
                     v.description,
                     v.classifier_md,
                     (SELECT count(*) FROM maludb_subject_verb sv
                        WHERE sv.verb_name = v.canonical_name) AS linked_subjects
                FROM maludb_verb v
                {where}
               ORDER BY v.canonical_name
               LIMIT {limit}"""

    rows = db_query(auth.conn, sql, params)
    for r in rows:
        r["id"] = int(r["id"])
        r["linked_subjects"] = int(r["linked_subjects"])

    return JSONResponse(content={"verbs": rows})


# ---------------------------------------------------------------------------
# POST /v1/verbs — create a verb
# ---------------------------------------------------------------------------


@router.post("/v1/verbs")
async def create_verb(auth: Auth, request: Request) -> JSONResponse:
    body = await request.json()

    name = str(body.get("canonical_name") or "").strip()
    if not name:
        json_error("missing_field", 'Field "canonical_name" is required.', 400)

    vtype = str(body["type"]) if body.get("type") is not None else None
    description = str(body["description"]) if body.get("description") is not None else None
    classifier_md = str(body["classifier_md"]) if body.get("classifier_md") is not None else None

    created = db_one(
        auth.conn,
        """INSERT INTO maludb_verb
               (verb_id, canonical_name, verb_type, description, classifier_md, created_at)
           SELECT COALESCE(MAX(verb_id), 0) + 1, %s, %s, %s, %s, now()
             FROM maludb_verb
           RETURNING verb_id        AS id,
                     canonical_name AS canonical_name,
                     verb_type      AS type,
                     description,
                     classifier_md""",
        [name, vtype, description, classifier_md],
    )

    created["id"] = int(created["id"])
    created["linked_subjects"] = 0

    return JSONResponse(status_code=201, content={"verb": created})


# ---------------------------------------------------------------------------
# GET /v1/verbs/{id} — verb detail with embedded subjects
# ---------------------------------------------------------------------------


@router.get("/v1/verbs/{verb_id}")
async def get_verb(auth: Auth, verb_id: int) -> JSONResponse:
    verb = _load_verb_detail(auth, verb_id)
    if verb is None:
        json_error("not_found", "Verb not found.", 404)

    return JSONResponse(content={"verb": verb})


# ---------------------------------------------------------------------------
# PATCH /v1/verbs/{id} — update a verb
# ---------------------------------------------------------------------------


@router.patch("/v1/verbs/{verb_id}")
async def update_verb(auth: Auth, verb_id: int, request: Request) -> JSONResponse:
    if db_one(auth.conn, "SELECT 1 FROM maludb_verb WHERE verb_id = %s", [verb_id]) is None:
        json_error("not_found", "Verb not found.", 404)

    body = await request.json()
    fields: list[str] = []
    params: list = []

    if "canonical_name" in body:
        name = str(body["canonical_name"]).strip()
        if not name:
            json_error("validation_failed", 'Field "canonical_name" cannot be empty.', 422)
        fields.append("canonical_name = %s")
        params.append(name)

    if "type" in body:
        fields.append("verb_type = %s")
        params.append(None if body["type"] is None else str(body["type"]))

    if "description" in body:
        fields.append("description = %s")
        params.append(None if body["description"] is None else str(body["description"]))

    if "classifier_md" in body:
        fields.append("classifier_md = %s")
        params.append(None if body["classifier_md"] is None else str(body["classifier_md"]))

    if not fields:
        json_error(
            "bad_request",
            "No updatable fields provided (canonical_name, type, description, classifier_md).",
            400,
        )

    params.append(verb_id)
    db_exec(
        auth.conn,
        f"UPDATE maludb_verb SET {', '.join(fields)} WHERE verb_id = %s",
        params,
    )

    return JSONResponse(content={"verb": _load_verb_detail(auth, verb_id)})


# ---------------------------------------------------------------------------
# DELETE /v1/verbs/{id} — delete a verb
# ---------------------------------------------------------------------------


@router.delete("/v1/verbs/{verb_id}")
async def delete_verb(auth: Auth, verb_id: int) -> JSONResponse:
    n = db_exec(auth.conn, "DELETE FROM maludb_verb WHERE verb_id = %s", [verb_id])
    if n == 0:
        json_error("not_found", "Verb not found.", 404)

    return JSONResponse(content={"deleted": True, "id": verb_id})


# ---------------------------------------------------------------------------
# GET /v1/verbs/{id}/subjects — linked subjects for a verb
# ---------------------------------------------------------------------------


@router.get("/v1/verbs/{verb_id}/subjects")
async def verb_subjects(auth: Auth, verb_id: int) -> JSONResponse:
    verb = db_one(auth.conn, "SELECT canonical_name FROM maludb_verb WHERE verb_id = %s", [verb_id])
    if verb is None:
        json_error("not_found", "Verb not found.", 404)

    subjects = db_query(
        auth.conn,
        """SELECT s.subject_id     AS id,
                  s.canonical_name AS label,
                  s.subject_type   AS type
             FROM maludb_subject_verb sv
             JOIN maludb_subject s ON s.canonical_name = sv.subject_name
            WHERE sv.verb_name = %s
            ORDER BY s.canonical_name""",
        [verb["canonical_name"]],
    )
    for s in subjects:
        s["id"] = int(s["id"])

    return JSONResponse(content={"subjects": subjects})
