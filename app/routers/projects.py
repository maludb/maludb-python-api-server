"""
Project endpoints — CRUD, archive/unarchive, subject links, verb links.

Ports PHP's projects.php, projects_id.php, projects_id_archive.php,
projects_id_unarchive.php, projects_id_subjects.php,
projects_id_subjects_id.php, projects_id_verbs.php, projects_id_verbs_id.php.

A "project" is a subject with subject_type='project'.  The maludb_project
view filters maludb_subject WHERE subject_type='project'.  Project id IS
subject_id.  The API field ``name`` maps to DB column ``canonical_name``.

Subject/verb links use the SVPOR relationship graph:
    maludb_svpor_relationship_create / maludb_svpor_relationship_delete
All link-mutation operations run inside db_tx_core() so that maludb_core
functions resolve correctly.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.auth import Auth
from app.database import db_exec, db_one, db_query, db_tx_core
from app.errors import json_error

router = APIRouter()


# ---------------------------------------------------------------------------
# Helper — load a full project detail with embedded subjects[], verbs[], documents[]
# ---------------------------------------------------------------------------


def _load_project_detail(auth: Auth, project_id: int) -> dict | None:
    """Fetch a project with its embedded subjects[], verbs[], and documents[].

    Returns None if no project with that id exists.
    Mirrors PHP's load_project_detail() from projects_id.php.
    """
    project = db_one(
        auth.conn,
        """SELECT subject_id     AS id,
                  canonical_name AS name,
                  description,
                  classifier_md,
                  archived_at
             FROM maludb_project
            WHERE subject_id = %s""",
        [project_id],
    )
    if project is None:
        return None
    project["id"] = int(project["id"])

    # Linked identifiers come from the SVPOR graph (source = this project subject).
    edges = db_query(
        auth.conn,
        """SELECT target_kind, target_id, target_name, relationship_type
             FROM maludb_svpor_relationship
            WHERE source_kind = 'subject' AND source_id = %s
            ORDER BY target_kind, target_name""",
        [project_id],
    )
    subjects = []
    verbs = []
    for e in edges:
        item = {
            "id": int(e["target_id"]),
            "name": e["target_name"],
            "relationship_type": e["relationship_type"],
        }
        if e["target_kind"] == "verb":
            verbs.append(item)
        else:
            subjects.append(item)
    project["subjects"] = subjects
    project["verbs"] = verbs

    # Documents linked through the unified graph — document helpers not built yet;
    # return empty list for now.
    project["documents"] = []

    return project


# ===========================================================================
# GET /v1/projects — list projects
# ===========================================================================


@router.get("/v1/projects")
def list_projects(
    auth: Auth,
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, le=200),
):
    where = ""
    params: list = []
    if q:
        where = "WHERE canonical_name ILIKE %s OR description ILIKE %s"
        params = [f"%{q}%", f"%{q}%"]

    sql = f"""SELECT subject_id     AS id,
                     canonical_name AS name,
                     description,
                     classifier_md,
                     archived_at
                FROM maludb_project
                {where}
               ORDER BY canonical_name
               LIMIT %s"""
    params.append(limit)

    rows = db_query(auth.conn, sql, params)
    for r in rows:
        r["id"] = int(r["id"])

    return {"projects": rows}


# ===========================================================================
# POST /v1/projects — create a project
# ===========================================================================


@router.post("/v1/projects")
async def create_project(auth: Auth, request: Request):
    body = await request.json()

    name = (body.get("name") or "").strip() if isinstance(body.get("name"), str) else ""
    if not name:
        json_error("missing_field", 'Field "name" is required.', 400)

    description = str(body["description"]) if "description" in body and body["description"] is not None else None
    classifier_md = (
        str(body["classifier_md"]) if "classifier_md" in body and body["classifier_md"] is not None else None
    )

    # A project is a subject of type 'project'; subject_id has no sequence.
    created = db_one(
        auth.conn,
        """INSERT INTO maludb_subject
               (subject_id, canonical_name, subject_type, description, classifier_md, created_at)
           SELECT COALESCE(MAX(subject_id), 0) + 1, %s, 'project', %s, %s, now()
             FROM maludb_subject
           RETURNING subject_id     AS id,
                     canonical_name AS name,
                     description,
                     classifier_md""",
        [name, description, classifier_md],
    )
    created["id"] = int(created["id"])

    return JSONResponse(status_code=201, content={"project": created})


# ===========================================================================
# GET /v1/projects/{id} — project detail
# ===========================================================================


@router.get("/v1/projects/{project_id}")
def get_project(project_id: int, auth: Auth):
    project = _load_project_detail(auth, project_id)
    if project is None:
        json_error("not_found", "Project not found.", 404)
    return {"project": project}


# ===========================================================================
# PATCH /v1/projects/{id} — update a project
# ===========================================================================


@router.patch("/v1/projects/{project_id}")
async def update_project(project_id: int, auth: Auth, request: Request):
    if db_one(auth.conn, "SELECT 1 FROM maludb_project WHERE subject_id = %s", [project_id]) is None:
        json_error("not_found", "Project not found.", 404)

    body = await request.json()
    fields: list[str] = []
    params: list = []

    if "name" in body:
        name = str(body["name"]).strip() if body["name"] is not None else ""
        if not name:
            json_error("validation_failed", 'Field "name" cannot be empty.', 422)
        fields.append("canonical_name = %s")
        params.append(name)
    if "description" in body:
        fields.append("description = %s")
        params.append(None if body["description"] is None else str(body["description"]))
    if "classifier_md" in body:
        fields.append("classifier_md = %s")
        params.append(None if body["classifier_md"] is None else str(body["classifier_md"]))

    if not fields:
        json_error("bad_request", "No updatable fields provided (name, description, classifier_md).", 400)

    params.append(project_id)
    db_exec(
        auth.conn,
        f"UPDATE maludb_subject SET {', '.join(fields)} WHERE subject_id = %s AND subject_type = 'project'",
        params,
    )

    return {"project": _load_project_detail(auth, project_id)}


# ===========================================================================
# DELETE /v1/projects/{id} — delete a project
# ===========================================================================


@router.delete("/v1/projects/{project_id}")
def delete_project(project_id: int, auth: Auth):
    n = db_exec(
        auth.conn,
        "DELETE FROM maludb_subject WHERE subject_id = %s AND subject_type = 'project'",
        [project_id],
    )
    if n == 0:
        json_error("not_found", "Project not found.", 404)
    return {"deleted": True, "id": project_id}


# ===========================================================================
# POST /v1/projects/{id}/archive — archive a project
# ===========================================================================


@router.post("/v1/projects/{project_id}/archive")
def archive_project(project_id: int, auth: Auth):
    project = db_one(
        auth.conn,
        "SELECT archived_at FROM maludb_project WHERE subject_id = %s",
        [project_id],
    )
    if project is None:
        json_error("not_found", "Project not found.", 404)
    if project["archived_at"] is not None:
        json_error("already_archived", "Project is already archived.", 409)

    db_one(auth.conn, "SELECT maludb_project_archive(%s)", [project_id])

    updated = db_one(
        auth.conn,
        """SELECT subject_id     AS id,
                  canonical_name AS name,
                  description,
                  classifier_md,
                  archived_at
             FROM maludb_project
            WHERE subject_id = %s""",
        [project_id],
    )
    updated["id"] = int(updated["id"])
    return {"project": updated}


# ===========================================================================
# POST /v1/projects/{id}/unarchive — unarchive a project
# ===========================================================================


@router.post("/v1/projects/{project_id}/unarchive")
def unarchive_project(project_id: int, auth: Auth):
    project = db_one(
        auth.conn,
        "SELECT archived_at FROM maludb_project WHERE subject_id = %s",
        [project_id],
    )
    if project is None:
        json_error("not_found", "Project not found.", 404)
    if project["archived_at"] is None:
        json_error("not_archived", "Project is not archived.", 409)

    db_one(auth.conn, "SELECT maludb_project_unarchive(%s)", [project_id])

    updated = db_one(
        auth.conn,
        """SELECT subject_id     AS id,
                  canonical_name AS name,
                  description,
                  classifier_md,
                  archived_at
             FROM maludb_project
            WHERE subject_id = %s""",
        [project_id],
    )
    updated["id"] = int(updated["id"])
    return {"project": updated}


# ===========================================================================
# POST /v1/projects/{id}/subjects — link a subject to a project
# ===========================================================================


@router.post("/v1/projects/{project_id}/subjects")
async def link_subject(project_id: int, auth: Auth, request: Request):
    if db_one(auth.conn, "SELECT 1 FROM maludb_project WHERE subject_id = %s", [project_id]) is None:
        json_error("not_found", "Project not found.", 404)

    body = await request.json()
    if "subject_id" not in body or not isinstance(body["subject_id"], int):
        json_error("missing_field", 'Field "subject_id" (integer) is required.', 400)
    sid = int(body["subject_id"])

    if sid == project_id:
        json_error("validation_failed", "A project cannot link to itself.", 422)

    subject = db_one(
        auth.conn,
        """SELECT subject_id AS id, canonical_name AS name, subject_type AS type
             FROM maludb_subject WHERE subject_id = %s""",
        [sid],
    )
    if subject is None:
        json_error("validation_failed", "subject_id does not refer to an existing subject.", 422)

    # The svpor create helper is not idempotent — dedupe here.
    dup = db_one(
        auth.conn,
        """SELECT 1 FROM maludb_svpor_relationship
            WHERE source_kind='subject' AND source_id=%s AND target_kind='subject'
              AND target_id=%s AND relationship_type='has_member'""",
        [project_id, sid],
    )
    if dup is not None:
        json_error("conflict", "That subject is already linked to the project.", 409)

    def _do_link(conn):
        return db_one(
            conn,
            """SELECT maludb_svpor_relationship_create(
                   'subject', %s, 'subject', %s, 'has_member', NULL, '{}'::jsonb, NULL
               ) AS edge_id""",
            [project_id, sid],
        )

    row = db_tx_core(auth.conn, _do_link)
    subject["id"] = int(subject["id"])

    return JSONResponse(
        status_code=201,
        content={"subject": subject, "edge_id": int(row["edge_id"])},
    )


# ===========================================================================
# PUT /v1/projects/{id}/subjects — replace full subject set
# ===========================================================================


@router.put("/v1/projects/{project_id}/subjects")
async def replace_subjects(project_id: int, auth: Auth, request: Request):
    if db_one(auth.conn, "SELECT 1 FROM maludb_project WHERE subject_id = %s", [project_id]) is None:
        json_error("not_found", "Project not found.", 404)

    body = await request.json()
    if "subject_ids" not in body or not isinstance(body["subject_ids"], list):
        json_error("missing_field", 'Field "subject_ids" (array of integers) is required.', 400)

    want: list[int] = []
    seen: set[int] = set()
    for v in body["subject_ids"]:
        if not isinstance(v, int):
            json_error("validation_failed", "subject_ids must be integers.", 422)
        if v == project_id:
            json_error("validation_failed", "A project cannot link to itself.", 422)
        if db_one(auth.conn, "SELECT 1 FROM maludb_subject WHERE subject_id = %s", [v]) is None:
            json_error("validation_failed", f"subject_id {v} does not refer to an existing subject.", 422)
        if v not in seen:
            want.append(v)
            seen.add(v)

    def _do_replace(conn):
        cur_rows = db_query(
            conn,
            """SELECT target_id FROM maludb_svpor_relationship
                WHERE source_kind='subject' AND source_id=%s AND target_kind='subject'
                  AND relationship_type='has_member'""",
            [project_id],
        )
        cur = [int(r["target_id"]) for r in cur_rows]

        for c in cur:
            if c not in seen:
                db_one(
                    conn,
                    "SELECT maludb_svpor_relationship_delete('subject', %s, 'subject', %s, 'has_member')",
                    [project_id, c],
                )
        for w in want:
            if w not in cur:
                db_one(
                    conn,
                    """SELECT maludb_svpor_relationship_create(
                           'subject', %s, 'subject', %s, 'has_member', NULL, '{}'::jsonb, NULL
                       )""",
                    [project_id, w],
                )

    db_tx_core(auth.conn, _do_replace)

    subjects = db_query(
        auth.conn,
        """SELECT s.subject_id AS id, s.canonical_name AS name, s.subject_type AS type
             FROM maludb_svpor_relationship r
             JOIN maludb_subject s ON s.subject_id = r.target_id
            WHERE r.source_kind='subject' AND r.source_id=%s AND r.target_kind='subject'
              AND r.relationship_type='has_member'
            ORDER BY s.canonical_name""",
        [project_id],
    )
    for x in subjects:
        x["id"] = int(x["id"])

    return {"subjects": subjects}


# ===========================================================================
# DELETE /v1/projects/{id}/subjects/{sid} — unlink a subject from a project
# ===========================================================================


@router.delete("/v1/projects/{project_id}/subjects/{subject_id}")
def unlink_subject(project_id: int, subject_id: int, auth: Auth):
    def _do_unlink(conn):
        return db_one(
            conn,
            "SELECT maludb_svpor_relationship_delete('subject', %s, 'subject', %s, 'has_member') AS removed",
            [project_id, subject_id],
        )

    row = db_tx_core(auth.conn, _do_unlink)
    if int(row["removed"]) == 0:
        json_error("not_found", "That subject is not linked to the project.", 404)
    return {"deleted": True, "id": project_id, "subject_id": subject_id}


# ===========================================================================
# POST /v1/projects/{id}/verbs — link a verb to a project
# ===========================================================================


@router.post("/v1/projects/{project_id}/verbs")
async def link_verb(project_id: int, auth: Auth, request: Request):
    if db_one(auth.conn, "SELECT 1 FROM maludb_project WHERE subject_id = %s", [project_id]) is None:
        json_error("not_found", "Project not found.", 404)

    body = await request.json()
    if "verb_id" not in body or not isinstance(body["verb_id"], int):
        json_error("missing_field", 'Field "verb_id" (integer) is required.', 400)
    vid = int(body["verb_id"])

    verb = db_one(
        auth.conn,
        "SELECT verb_id AS id, canonical_name AS name, verb_type AS type FROM maludb_verb WHERE verb_id = %s",
        [vid],
    )
    if verb is None:
        json_error("validation_failed", "verb_id does not refer to an existing verb.", 422)

    dup = db_one(
        auth.conn,
        """SELECT 1 FROM maludb_svpor_relationship
            WHERE source_kind='subject' AND source_id=%s AND target_kind='verb'
              AND target_id=%s AND relationship_type='has_member'""",
        [project_id, vid],
    )
    if dup is not None:
        json_error("conflict", "That verb is already linked to the project.", 409)

    def _do_link(conn):
        return db_one(
            conn,
            """SELECT maludb_svpor_relationship_create(
                   'subject', %s, 'verb', %s, 'has_member', NULL, '{}'::jsonb, NULL
               ) AS edge_id""",
            [project_id, vid],
        )

    row = db_tx_core(auth.conn, _do_link)
    verb["id"] = int(verb["id"])

    return JSONResponse(
        status_code=201,
        content={"verb": verb, "edge_id": int(row["edge_id"])},
    )


# ===========================================================================
# PUT /v1/projects/{id}/verbs — replace full verb set
# ===========================================================================


@router.put("/v1/projects/{project_id}/verbs")
async def replace_verbs(project_id: int, auth: Auth, request: Request):
    if db_one(auth.conn, "SELECT 1 FROM maludb_project WHERE subject_id = %s", [project_id]) is None:
        json_error("not_found", "Project not found.", 404)

    body = await request.json()
    if "verb_ids" not in body or not isinstance(body["verb_ids"], list):
        json_error("missing_field", 'Field "verb_ids" (array of integers) is required.', 400)

    want: list[int] = []
    seen: set[int] = set()
    for v in body["verb_ids"]:
        if not isinstance(v, int):
            json_error("validation_failed", "verb_ids must be integers.", 422)
        if db_one(auth.conn, "SELECT 1 FROM maludb_verb WHERE verb_id = %s", [v]) is None:
            json_error("validation_failed", f"verb_id {v} does not refer to an existing verb.", 422)
        if v not in seen:
            want.append(v)
            seen.add(v)

    def _do_replace(conn):
        cur_rows = db_query(
            conn,
            """SELECT target_id FROM maludb_svpor_relationship
                WHERE source_kind='subject' AND source_id=%s AND target_kind='verb'
                  AND relationship_type='has_member'""",
            [project_id],
        )
        cur = [int(r["target_id"]) for r in cur_rows]

        for c in cur:
            if c not in seen:
                db_one(
                    conn,
                    "SELECT maludb_svpor_relationship_delete('subject', %s, 'verb', %s, 'has_member')",
                    [project_id, c],
                )
        for w in want:
            if w not in cur:
                db_one(
                    conn,
                    """SELECT maludb_svpor_relationship_create(
                           'subject', %s, 'verb', %s, 'has_member', NULL, '{}'::jsonb, NULL
                       )""",
                    [project_id, w],
                )

    db_tx_core(auth.conn, _do_replace)

    verbs = db_query(
        auth.conn,
        """SELECT v.verb_id AS id, v.canonical_name AS name, v.verb_type AS type
             FROM maludb_svpor_relationship r
             JOIN maludb_verb v ON v.verb_id = r.target_id
            WHERE r.source_kind='subject' AND r.source_id=%s AND r.target_kind='verb'
              AND r.relationship_type='has_member'
            ORDER BY v.canonical_name""",
        [project_id],
    )
    for x in verbs:
        x["id"] = int(x["id"])

    return {"verbs": verbs}


# ===========================================================================
# DELETE /v1/projects/{id}/verbs/{vid} — unlink a verb from a project
# ===========================================================================


@router.delete("/v1/projects/{project_id}/verbs/{verb_id}")
def unlink_verb(project_id: int, verb_id: int, auth: Auth):
    def _do_unlink(conn):
        return db_one(
            conn,
            "SELECT maludb_svpor_relationship_delete('subject', %s, 'verb', %s, 'has_member') AS removed",
            [project_id, verb_id],
        )

    row = db_tx_core(auth.conn, _do_unlink)
    if int(row["removed"]) == 0:
        json_error("not_found", "That verb is not linked to the project.", 404)
    return {"deleted": True, "id": project_id, "verb_id": verb_id}
