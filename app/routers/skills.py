"""
Skill endpoints — list, create, detail, update, delete, duplicate (fork).

Ports PHP's skills.php, skills_id.php, skills_id_duplicate.php.

Live-schema mapping (DB column -> API field):
    skill_id   -> id
    skill_name -> name
Source table: maludb_skill (skill_id from sequence).
Defaults: version '1.0.0', visibility 'private', packaging_kind 'system_prompt', enabled true.
DB enforces visibility/packaging_kind value sets (-> 422).
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.auth import Auth
from app.database import db_exec, db_one, db_query, db_tx_core
from app.errors import json_error

router = APIRouter()


# ---------------------------------------------------------------------------
# Helper — load a single skill
# ---------------------------------------------------------------------------


def _load_skill(auth: Auth, skill_id: int) -> dict | None:
    """Fetch a single skill, or None if not found."""
    skill = db_one(
        auth.conn,
        """SELECT skill_id AS id, skill_name AS name, description, markdown, version,
                  visibility, packaging_kind, enabled, created_at, updated_at
             FROM maludb_skill
            WHERE skill_id = %s""",
        [skill_id],
    )
    if skill is None:
        return None
    skill["id"] = int(skill["id"])
    skill["enabled"] = None if skill["enabled"] is None else bool(skill["enabled"])
    return skill


# ===========================================================================
# GET /v1/skills — list skills
# ===========================================================================


@router.get("/v1/skills")
def list_skills(
    auth: Auth,
    visibility: str | None = Query(default=None, max_length=40),
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, le=200),
):
    clauses: list[str] = []
    params: list = []
    if visibility:
        clauses.append("visibility = %s")
        params.append(visibility)
    if q:
        clauses.append("(skill_name ILIKE %s OR description ILIKE %s)")
        params.extend([f"%{q}%", f"%{q}%"])

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    sql = f"""SELECT skill_id AS id, skill_name AS name, description, version,
                     visibility, packaging_kind, enabled, created_at
                FROM maludb_skill
                {where}
               ORDER BY skill_name
               LIMIT %s"""
    params.append(limit)

    rows = db_query(auth.conn, sql, params)
    for r in rows:
        r["id"] = int(r["id"])
        r["enabled"] = None if r["enabled"] is None else bool(r["enabled"])

    return {"skills": rows}


# ===========================================================================
# POST /v1/skills — create a skill
# ===========================================================================


@router.post("/v1/skills")
async def create_skill(auth: Auth, request: Request):
    body = await request.json()

    name = (body.get("name") or "").strip() if isinstance(body.get("name"), str) else ""
    if not name:
        json_error("missing_field", 'Field "name" is required.', 400)

    cols = ["skill_name"]
    placeholders = ["%s"]
    params: list = [name]

    for f in ("description", "markdown", "version", "visibility", "packaging_kind"):
        if f in body and body[f] is not None:
            cols.append(f)
            placeholders.append("%s")
            params.append(str(body[f]))

    if "enabled" in body:
        cols.append("enabled")
        placeholders.append("%s")
        params.append("true" if body["enabled"] else "false")

    created = db_one(
        auth.conn,
        f"""INSERT INTO maludb_skill ({', '.join(cols)})
           VALUES ({', '.join(placeholders)})
           RETURNING skill_id AS id, skill_name AS name, description, markdown, version,
                     visibility, packaging_kind, enabled, created_at""",
        params,
    )
    created["id"] = int(created["id"])
    created["enabled"] = None if created["enabled"] is None else bool(created["enabled"])

    return JSONResponse(status_code=201, content={"skill": created})


# ===========================================================================
# GET /v1/skills/{id} — skill detail
# ===========================================================================


@router.get("/v1/skills/{skill_id}")
def get_skill(skill_id: int, auth: Auth):
    skill = _load_skill(auth, skill_id)
    if skill is None:
        json_error("not_found", "Skill not found.", 404)
    return {"skill": skill}


# ===========================================================================
# PATCH /v1/skills/{id} — update a skill
# ===========================================================================


@router.patch("/v1/skills/{skill_id}")
async def update_skill(skill_id: int, auth: Auth, request: Request):
    if db_one(auth.conn, "SELECT 1 FROM maludb_skill WHERE skill_id = %s", [skill_id]) is None:
        json_error("not_found", "Skill not found.", 404)

    body = await request.json()
    fields: list[str] = []
    params: list = []

    if "name" in body:
        name = str(body["name"]).strip() if body["name"] is not None else ""
        if not name:
            json_error("validation_failed", 'Field "name" cannot be empty.', 422)
        fields.append("skill_name = %s")
        params.append(name)

    for f in ("description", "markdown", "version", "visibility", "packaging_kind"):
        if f in body:
            fields.append(f"{f} = %s")
            params.append(None if body[f] is None else str(body[f]))

    if "enabled" in body:
        fields.append("enabled = %s")
        params.append("true" if body["enabled"] else "false")

    if not fields:
        json_error(
            "bad_request",
            "No updatable fields provided (name, description, version, visibility, packaging_kind, enabled).",
            400,
        )

    fields.append("updated_at = now()")
    params.append(skill_id)
    db_exec(
        auth.conn,
        f"UPDATE maludb_skill SET {', '.join(fields)} WHERE skill_id = %s",
        params,
    )

    return {"skill": _load_skill(auth, skill_id)}


# ===========================================================================
# DELETE /v1/skills/{id} — delete a skill
# ===========================================================================


@router.delete("/v1/skills/{skill_id}")
def delete_skill(skill_id: int, auth: Auth):
    n = db_exec(auth.conn, "DELETE FROM maludb_skill WHERE skill_id = %s", [skill_id])
    if n == 0:
        json_error("not_found", "Skill not found.", 404)
    return {"deleted": True, "id": skill_id}


# ===========================================================================
# POST /v1/skills/{id}/duplicate — fork a skill
# ===========================================================================


@router.post("/v1/skills/{skill_id}/duplicate")
async def duplicate_skill(skill_id: int, auth: Auth, request: Request):
    src = db_one(
        auth.conn,
        """SELECT skill_id, skill_name, COALESCE(owner_schema, current_schema()) AS owner_schema
             FROM maludb_skill WHERE skill_id = %s""",
        [skill_id],
    )
    if src is None:
        json_error("not_found", "Skill not found.", 404)

    body = await request.json()
    new_name = (
        str(body["name"]) if "name" in body and body["name"] is not None and str(body["name"]).strip() else None
    )
    new_version = (
        str(body["version"])
        if "version" in body and body["version"] is not None and str(body["version"]).strip()
        else "1.0.0"
    )

    def do_fork(conn):  # noqa: ANN001, ANN202
        return db_one(
            conn,
            "SELECT maludb_skill_fork(%s, %s, %s, %s) AS id",
            [src["owner_schema"], skill_id, new_name, new_version],
        )

    row = db_tx_core(auth.conn, do_fork)

    new_id = int(row["id"])
    skill = db_one(
        auth.conn,
        """SELECT skill_id AS id, skill_name AS name, description, version,
                  visibility, packaging_kind, enabled, source_skill_id, created_at
             FROM maludb_skill WHERE skill_id = %s""",
        [new_id],
    )
    skill["id"] = int(skill["id"])
    skill["source_skill_id"] = None if skill["source_skill_id"] is None else int(skill["source_skill_id"])
    skill["enabled"] = None if skill["enabled"] is None else bool(skill["enabled"])

    return JSONResponse(status_code=201, content={"skill": skill})
