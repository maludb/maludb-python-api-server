"""
Subject endpoints — CRUD for subjects, verb links, related-subject relationships.

Ports PHP's subjects.php, subjects_id.php, subjects_id_verbs.php,
subjects_id_verbs_id.php, subjects_id_related-subjects.php,
subjects_id_related-subjects_id.php, and subject-relationships_id.php.

Live-schema mapping (DB column → API field):
    subject_id     → id
    canonical_name → label
    subject_type   → type
Verb links live in maludb_subject_verb keyed by subject_name (= canonical_name).
Relationships live in maludb_subject_relationship (from/to subject ids).
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.auth import Auth
from app.database import db_exec, db_one, db_query
from app.errors import json_error

router = APIRouter()


# ---------------------------------------------------------------------------
# Helper — load a full subject detail with embedded verbs[] and related_subjects[]
# ---------------------------------------------------------------------------


def _load_subject_detail(auth: Auth, subject_id: int) -> dict | None:
    """Fetch a subject with its embedded verbs[], related_subjects[], and documents[].

    Returns None if no subject with that id exists.
    Mirrors PHP's load_subject_detail() from subjects_id.php.
    """
    subject = db_one(
        auth.conn,
        """SELECT subject_id     AS id,
                  canonical_name AS label,
                  subject_type   AS type,
                  description,
                  classifier_md
             FROM maludb_subject
            WHERE subject_id = %s""",
        [subject_id],
    )
    if subject is None:
        return None
    subject["id"] = int(subject["id"])

    # Linked verbs — resolve verb details by name through the compartment table.
    verbs = db_query(
        auth.conn,
        """SELECT v.verb_id        AS id,
                  v.canonical_name AS canonical_name,
                  v.verb_type      AS type
             FROM maludb_subject_verb sv
             JOIN maludb_verb v ON v.canonical_name = sv.verb_name
            WHERE sv.subject_name = %s
            ORDER BY v.canonical_name""",
        [subject["label"]],
    )
    for v in verbs:
        v["id"] = int(v["id"])
    subject["verbs"] = verbs

    # Related subjects — either endpoint of a relationship; the "other" side is returned.
    rels = db_query(
        auth.conn,
        """SELECT relationship_id,
                  from_subject_id,
                  to_subject_id,
                  from_subject_label,
                  to_subject_label,
                  relationship_type,
                  label AS relationship_label,
                  valid_from,
                  valid_to
             FROM maludb_subject_relationship
            WHERE from_subject_id = %s OR to_subject_id = %s
            ORDER BY relationship_id""",
        [subject_id, subject_id],
    )
    subject["related_subjects"] = _map_related(rels, subject_id)

    # TODO: Documents linked through the unified graph (document_neighbors).
    # Document helpers are not built yet (Task 19) — return empty list for now.
    subject["documents"] = []

    return subject


def _map_related(rels: list[dict], subject_id: int) -> list[dict]:
    """Map raw relationship rows to the API shape, resolving the 'other' side.

    Mirrors PHP's map_related() from subjects_id_related-subjects.php.
    """
    out = []
    for r in rels:
        outgoing = int(r["from_subject_id"]) == subject_id
        out.append({
            "relationship_id": int(r["relationship_id"]),
            "id": int(r["to_subject_id"] if outgoing else r["from_subject_id"]),
            "label": r["to_subject_label"] if outgoing else r["from_subject_label"],
            "relationship_type": r["relationship_type"],
            "relationship_label": r["relationship_label"],
            "direction": "outgoing" if outgoing else "incoming",
            "valid_from": r["valid_from"],
            "valid_to": r["valid_to"],
        })
    return out


# ---------------------------------------------------------------------------
# Helper — load a single relationship row
# ---------------------------------------------------------------------------


def _load_relationship(auth: Auth, rel_id: int) -> dict | None:
    """Fetch a single subject-relationship row, or None.

    Mirrors PHP's load_relationship() from subject-relationships_id.php.
    """
    row = db_one(
        auth.conn,
        """SELECT relationship_id   AS id,
                  from_subject_id, to_subject_id,
                  from_subject_label, to_subject_label,
                  relationship_type,
                  label,
                  valid_from, valid_to,
                  created_at
             FROM maludb_subject_relationship
            WHERE relationship_id = %s""",
        [rel_id],
    )
    if row is None:
        return None
    row["id"] = int(row["id"])
    row["from_subject_id"] = int(row["from_subject_id"])
    row["to_subject_id"] = int(row["to_subject_id"])
    return row


# ===========================================================================
# GET /v1/subjects — list subjects
# ===========================================================================


@router.get("/v1/subjects")
def list_subjects(
    auth: Auth,
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, le=200),
    with_: str | None = Query(default=None, alias="with", max_length=40),
):
    where = ""
    params: list = []
    if q:
        where = "WHERE s.canonical_name ILIKE %s OR s.description ILIKE %s"
        params = [f"%{q}%", f"%{q}%"]

    sql = f"""SELECT s.subject_id     AS id,
                     s.canonical_name AS label,
                     s.subject_type   AS type,
                     s.description,
                     s.classifier_md,
                     (SELECT count(*) FROM maludb_subject_verb sv
                        WHERE sv.subject_name = s.canonical_name) AS linked_verbs,
                     (SELECT count(*) FROM maludb_subject_relationship r
                        WHERE r.from_subject_id = s.subject_id
                           OR r.to_subject_id   = s.subject_id) AS related_subjects
                FROM maludb_subject s
                {where}
               ORDER BY s.canonical_name
               LIMIT %s"""
    params.append(limit)

    rows = db_query(auth.conn, sql, params)
    for r in rows:
        r["id"] = int(r["id"])
        r["linked_verbs"] = int(r["linked_verbs"])
        r["related_subjects"] = int(r["related_subjects"])

    # ?with=attributes is accepted but not yet implemented (Task 19).

    return {"subjects": rows}


# ===========================================================================
# POST /v1/subjects — create a subject
# ===========================================================================


@router.post("/v1/subjects")
async def create_subject(auth: Auth, request: Request):
    body = await request.json()

    label = (body.get("label") or "").strip() if isinstance(body.get("label"), str) else ""
    if not label:
        json_error("missing_field", 'Field "label" is required.', 400)

    type_ = str(body["type"]) if "type" in body and body["type"] is not None else None
    description = str(body["description"]) if "description" in body and body["description"] is not None else None
    classifier_md = (
        str(body["classifier_md"]) if "classifier_md" in body and body["classifier_md"] is not None else None
    )

    # subject_id has no sequence — derive it inline (MAX + 1).
    created = db_one(
        auth.conn,
        """INSERT INTO maludb_subject
               (subject_id, canonical_name, subject_type, description, classifier_md, created_at)
           SELECT COALESCE(MAX(subject_id), 0) + 1, %s, %s, %s, %s, now()
             FROM maludb_subject
           RETURNING subject_id     AS id,
                     canonical_name AS label,
                     subject_type   AS type,
                     description,
                     classifier_md""",
        [label, type_, description, classifier_md],
    )

    created["id"] = int(created["id"])
    created["linked_verbs"] = 0

    return JSONResponse(status_code=201, content={"subject": created})


# ===========================================================================
# GET /v1/subjects/{id} — subject detail
# ===========================================================================


@router.get("/v1/subjects/{subject_id}")
def get_subject(subject_id: int, auth: Auth):
    subject = _load_subject_detail(auth, subject_id)
    if subject is None:
        json_error("not_found", "Subject not found.", 404)
    return {"subject": subject}


# ===========================================================================
# PATCH /v1/subjects/{id} — update a subject
# ===========================================================================


@router.patch("/v1/subjects/{subject_id}")
async def update_subject(subject_id: int, auth: Auth, request: Request):
    # Must exist before we attempt an update.
    if db_one(auth.conn, "SELECT 1 FROM maludb_subject WHERE subject_id = %s", [subject_id]) is None:
        json_error("not_found", "Subject not found.", 404)

    body = await request.json()
    fields: list[str] = []
    params: list = []

    if "label" in body:
        label = (str(body["label"]).strip()) if body["label"] is not None else ""
        if not label:
            json_error("validation_failed", 'Field "label" cannot be empty.', 422)
        fields.append("canonical_name = %s")
        params.append(label)
    if "type" in body:
        fields.append("subject_type = %s")
        params.append(None if body["type"] is None else str(body["type"]))
    if "description" in body:
        fields.append("description = %s")
        params.append(None if body["description"] is None else str(body["description"]))
    if "classifier_md" in body:
        fields.append("classifier_md = %s")
        params.append(None if body["classifier_md"] is None else str(body["classifier_md"]))

    if not fields:
        json_error("bad_request", "No updatable fields provided (label, type, description, classifier_md).", 400)

    params.append(subject_id)
    db_exec(
        auth.conn,
        f"UPDATE maludb_subject SET {', '.join(fields)} WHERE subject_id = %s",
        params,
    )

    return {"subject": _load_subject_detail(auth, subject_id)}


# ===========================================================================
# DELETE /v1/subjects/{id} — delete a subject
# ===========================================================================


@router.delete("/v1/subjects/{subject_id}")
def delete_subject(subject_id: int, auth: Auth):
    n = db_exec(auth.conn, "DELETE FROM maludb_subject WHERE subject_id = %s", [subject_id])
    if n == 0:
        json_error("not_found", "Subject not found.", 404)
    return {"deleted": True, "id": subject_id}


# ===========================================================================
# GET /v1/subjects/{id}/verbs — list verbs linked to a subject
# ===========================================================================


@router.get("/v1/subjects/{subject_id}/verbs")
def list_subject_verbs(subject_id: int, auth: Auth):
    subject = db_one(
        auth.conn,
        "SELECT canonical_name FROM maludb_subject WHERE subject_id = %s",
        [subject_id],
    )
    if subject is None:
        json_error("not_found", "Subject not found.", 404)

    verbs = db_query(
        auth.conn,
        """SELECT v.verb_id        AS id,
                  v.canonical_name AS canonical_name,
                  v.verb_type      AS type
             FROM maludb_subject_verb sv
             JOIN maludb_verb v ON v.canonical_name = sv.verb_name
            WHERE sv.subject_name = %s
            ORDER BY v.canonical_name""",
        [subject["canonical_name"]],
    )
    for v in verbs:
        v["id"] = int(v["id"])

    return {"verbs": verbs}


# ===========================================================================
# POST /v1/subjects/{id}/verbs — link a verb to a subject
# ===========================================================================


@router.post("/v1/subjects/{subject_id}/verbs")
async def link_verb(subject_id: int, auth: Auth, request: Request):
    subject = db_one(
        auth.conn,
        "SELECT canonical_name FROM maludb_subject WHERE subject_id = %s",
        [subject_id],
    )
    if subject is None:
        json_error("not_found", "Subject not found.", 404)

    body = await request.json()
    if "verb_id" not in body or not isinstance(body["verb_id"], int):
        json_error("missing_field", 'Field "verb_id" (integer) is required.', 400)
    verb_id = int(body["verb_id"])

    verb = db_one(
        auth.conn,
        "SELECT verb_id AS id, canonical_name, verb_type AS type FROM maludb_verb WHERE verb_id = %s",
        [verb_id],
    )
    if verb is None:
        json_error("validation_failed", "verb_id does not refer to an existing verb.", 422)

    # Already linked? maludb_subject_verb is keyed by name.
    exists = db_one(
        auth.conn,
        "SELECT 1 FROM maludb_subject_verb WHERE subject_name = %s AND verb_name = %s",
        [subject["canonical_name"], verb["canonical_name"]],
    )
    if exists is not None:
        json_error("conflict", "That verb is already linked to the subject.", 409)

    row = db_one(
        auth.conn,
        "SELECT maludb_subject_verb_link(%s, %s) AS compartment_id",
        [subject_id, verb_id],
    )
    verb["id"] = int(verb["id"])

    return JSONResponse(
        status_code=201,
        content={
            "verb": verb,
            "compartment_id": int(row["compartment_id"]),
        },
    )


# ===========================================================================
# DELETE /v1/subjects/{id}/verbs/{verb_id} — unlink a verb from a subject
# ===========================================================================


@router.delete("/v1/subjects/{subject_id}/verbs/{verb_id}")
def unlink_verb(subject_id: int, verb_id: int, auth: Auth):
    row = db_one(
        auth.conn,
        "SELECT maludb_subject_verb_unlink(%s, %s) AS removed",
        [subject_id, verb_id],
    )
    if int(row["removed"]) == 0:
        json_error("not_found", "That verb is not linked to the subject.", 404)
    return {"deleted": True, "id": subject_id, "verb_id": verb_id}


# ===========================================================================
# GET /v1/subjects/{id}/related-subjects — list related subjects
# ===========================================================================


@router.get("/v1/subjects/{subject_id}/related-subjects")
def list_related_subjects(subject_id: int, auth: Auth):
    subject = db_one(
        auth.conn,
        "SELECT subject_id FROM maludb_subject WHERE subject_id = %s",
        [subject_id],
    )
    if subject is None:
        json_error("not_found", "Subject not found.", 404)

    rels = db_query(
        auth.conn,
        """SELECT relationship_id, from_subject_id, to_subject_id,
                  from_subject_label, to_subject_label,
                  relationship_type, label AS relationship_label,
                  valid_from, valid_to
             FROM maludb_subject_relationship
            WHERE from_subject_id = %s OR to_subject_id = %s
            ORDER BY relationship_id""",
        [subject_id, subject_id],
    )
    return {"related_subjects": _map_related(rels, subject_id)}


# ===========================================================================
# POST /v1/subjects/{id}/related-subjects — create a relationship
# ===========================================================================


@router.post("/v1/subjects/{subject_id}/related-subjects")
async def create_related_subject(subject_id: int, auth: Auth, request: Request):
    me = db_one(
        auth.conn,
        "SELECT canonical_name FROM maludb_subject WHERE subject_id = %s",
        [subject_id],
    )
    if me is None:
        json_error("not_found", "Subject not found.", 404)

    body = await request.json()
    if "related_subject_id" not in body or not isinstance(body["related_subject_id"], int):
        json_error("missing_field", 'Field "related_subject_id" (integer) is required.', 400)
    other_id = int(body["related_subject_id"])

    if other_id == subject_id:
        json_error("validation_failed", "A subject cannot be related to itself.", 422)

    rtype = (
        str(body["relationship_type"])
        if "relationship_type" in body
        and body["relationship_type"] is not None
        and str(body["relationship_type"]).strip()
        else "related_to"
    )
    valid_from = (
        str(body["valid_from"])
        if "valid_from" in body and body["valid_from"] is not None and str(body["valid_from"]).strip()
        else None
    )
    valid_to = (
        str(body["valid_to"])
        if "valid_to" in body and body["valid_to"] is not None and str(body["valid_to"]).strip()
        else None
    )

    other = db_one(
        auth.conn,
        "SELECT canonical_name FROM maludb_subject WHERE subject_id = %s",
        [other_id],
    )
    if other is None:
        json_error("validation_failed", "related_subject_id does not refer to an existing subject.", 422)

    # Reject an exact duplicate (same direction + type).
    dup = db_one(
        auth.conn,
        """SELECT 1 FROM maludb_subject_relationship
            WHERE from_subject_id = %s AND to_subject_id = %s AND relationship_type = %s""",
        [subject_id, other_id, rtype],
    )
    if dup is not None:
        json_error("conflict", "That related-subject link already exists.", 409)

    created = db_one(
        auth.conn,
        """INSERT INTO maludb_subject_relationship
               (relationship_id, from_subject_id, to_subject_id,
                from_subject_label, to_subject_label, relationship_type, valid_from, valid_to, created_at)
           SELECT COALESCE(MAX(relationship_id), 0) + 1, %s, %s, %s, %s, %s, %s::timestamptz, %s::timestamptz, now()
             FROM maludb_subject_relationship
           RETURNING relationship_id, valid_from, valid_to""",
        [subject_id, other_id, me["canonical_name"], other["canonical_name"], rtype, valid_from, valid_to],
    )

    return JSONResponse(
        status_code=201,
        content={
            "related_subject": {
                "relationship_id": int(created["relationship_id"]),
                "id": other_id,
                "label": other["canonical_name"],
                "relationship_type": rtype,
                "relationship_label": None,
                "direction": "outgoing",
                "valid_from": created["valid_from"],
                "valid_to": created["valid_to"],
            },
        },
    )


# ===========================================================================
# DELETE /v1/subjects/{id}/related-subjects/{other_id} — unlink related subjects
# ===========================================================================


@router.delete("/v1/subjects/{subject_id}/related-subjects/{other_id}")
def delete_related_subject(subject_id: int, other_id: int, auth: Auth):
    n = db_exec(
        auth.conn,
        """DELETE FROM maludb_subject_relationship
            WHERE (from_subject_id = %s AND to_subject_id = %s)
               OR (from_subject_id = %s AND to_subject_id = %s)""",
        [subject_id, other_id, other_id, subject_id],
    )
    if n == 0:
        json_error("not_found", "No relationship between those subjects.", 404)
    return {"deleted": True, "id": subject_id, "related_subject_id": other_id, "removed": n}


# ===========================================================================
# GET /v1/subject-relationships/{rel_id} — fetch a single relationship
# ===========================================================================


@router.get("/v1/subject-relationships/{rel_id}")
def get_relationship(rel_id: int, auth: Auth):
    row = _load_relationship(auth, rel_id)
    if row is None:
        json_error("not_found", "Relationship not found.", 404)
    return {"relationship": row}


# ===========================================================================
# PATCH /v1/subject-relationships/{rel_id} — update a relationship
# ===========================================================================


@router.patch("/v1/subject-relationships/{rel_id}")
async def update_relationship(rel_id: int, auth: Auth, request: Request):
    if db_one(auth.conn, "SELECT 1 FROM maludb_subject_relationship WHERE relationship_id = %s", [rel_id]) is None:
        json_error("not_found", "Relationship not found.", 404)

    body = await request.json()
    fields: list[str] = []
    params: list = []

    if "relationship_type" in body:
        rt = str(body["relationship_type"]).strip() if body["relationship_type"] is not None else ""
        if not rt:
            json_error("validation_failed", 'Field "relationship_type" cannot be empty.', 422)
        fields.append("relationship_type = %s")
        params.append(rt)
    if "label" in body:
        fields.append("label = %s")
        params.append(None if body["label"] is None else str(body["label"]))
    if "valid_from" in body:
        fields.append("valid_from = %s::timestamptz")
        val = body["valid_from"]
        params.append(None if val is None or (isinstance(val, str) and not val.strip()) else str(val))
    if "valid_to" in body:
        fields.append("valid_to = %s::timestamptz")
        val = body["valid_to"]
        params.append(None if val is None or (isinstance(val, str) and not val.strip()) else str(val))

    if not fields:
        json_error("bad_request", "No updatable fields provided (relationship_type, label, valid_from, valid_to).", 400)

    params.append(rel_id)
    db_exec(
        auth.conn,
        f"UPDATE maludb_subject_relationship SET {', '.join(fields)} WHERE relationship_id = %s",
        params,
    )

    return {"relationship": _load_relationship(auth, rel_id)}


# ===========================================================================
# DELETE /v1/subject-relationships/{rel_id} — delete a relationship
# ===========================================================================


@router.delete("/v1/subject-relationships/{rel_id}")
def delete_relationship(rel_id: int, auth: Auth):
    n = db_exec(
        auth.conn,
        "DELETE FROM maludb_subject_relationship WHERE relationship_id = %s",
        [rel_id],
    )
    if n == 0:
        json_error("not_found", "Relationship not found.", 404)
    return {"deleted": True, "id": rel_id}
