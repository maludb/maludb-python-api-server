"""
Document endpoints — list, upload, detail, link/unlink, delete, backfill.

Ports PHP's documents.php, documents_id.php, documents-backfill.php.

Documents are first-class graph nodes (maludb_core 0.87.0): each project/subject
name is wired into the unified graph (document→subject edge + soft tag) via
document_link_subject(), and primary_project_id is set from the first project.
Bytes are stored in maludb_source_package.content_bytes (bytea); maludb_document
holds the metadata and links to the package. Binary download is out of v1 —
GET returns metadata only.
"""

from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse

from app.auth import Auth
from app.database import db_exec, db_one, db_query, db_tx_core
from app.errors import json_error
from app.helpers.attributes import attach_attributes
from app.helpers.documents import document_link_subject, document_unlink_subject

router = APIRouter()


# ---------------------------------------------------------------------------
# Helper — parse comma-separated names, de-duplicate, preserve order
# ---------------------------------------------------------------------------

def _parse_names(s: str | None) -> list[str]:
    """Split a comma-separated string into unique, trimmed, non-empty names."""
    if not s:
        return []
    seen: dict[str, str] = {}
    for n in s.split(","):
        n = n.strip()
        if n and n not in seen:
            seen[n] = n
    return list(seen.values())


# ---------------------------------------------------------------------------
# Helper — load full document detail with tags[]
# ---------------------------------------------------------------------------

def _load_document_detail(auth: Auth, document_id: int) -> dict | None:
    """Fetch a document with its tags[], or None if not found.

    Mirrors PHP's load_document_detail() from documents_id.php.
    """
    doc = db_one(
        auth.conn,
        """SELECT d.document_id              AS id,
                  d.title,
                  d.source_type,
                  d.media_type,
                  d.document_type,
                  d.primary_project_id,
                  d.metadata_jsonb->>'description' AS description,
                  sp.content_size,
                  sp.content_hash,
                  d.created_at,
                  d.updated_at
             FROM maludb_document d
             LEFT JOIN maludb_source_package sp ON sp.source_package_id = d.source_package_id
            WHERE d.document_id = %s""",
        [document_id],
    )
    if doc is None:
        return None
    doc["id"] = int(doc["id"])
    doc["content_size"] = int(doc["content_size"]) if doc["content_size"] is not None else None
    doc["primary_project_id"] = int(doc["primary_project_id"]) if doc["primary_project_id"] is not None else None

    # Soft tags carry the resolved graph object (tag_object_type/tag_object_id).
    tags = db_query(
        auth.conn,
        """SELECT tag_id, tag_kind, tag_value, tag_object_type, tag_object_id, provenance, confidence
           FROM maludb_document_tag
          WHERE document_id = %s
          ORDER BY tag_kind, tag_value, tag_id""",
        [document_id],
    )
    for t in tags:
        t["tag_id"] = int(t["tag_id"])
        t["tag_object_id"] = int(t["tag_object_id"]) if t["tag_object_id"] is not None else None
        t["confidence"] = float(t["confidence"]) if t["confidence"] is not None else None
    doc["tags"] = tags

    return doc


# ===========================================================================
# GET /v1/documents — list documents
# ===========================================================================


@router.get("/v1/documents")
def list_documents(
    auth: Auth,
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, le=200),
    with_: str | None = Query(default=None, alias="with", max_length=40),
):
    where = ""
    params: list = []
    if q:
        where = "WHERE d.title ILIKE %s"
        params.append(f"%{q}%")

    sql = f"""SELECT d.document_id              AS id,
                     d.title,
                     d.source_type,
                     d.media_type,
                     d.document_type,
                     d.primary_project_id,
                     d.metadata_jsonb->>'description' AS description,
                     sp.content_size,
                     d.created_at
                FROM maludb_document d
                LEFT JOIN maludb_source_package sp ON sp.source_package_id = d.source_package_id
                {where}
               ORDER BY d.created_at DESC NULLS LAST, d.document_id DESC
               LIMIT %s"""
    params.append(limit)

    rows = db_query(auth.conn, sql, params)
    for r in rows:
        r["id"] = int(r["id"])
        r["content_size"] = int(r["content_size"]) if r["content_size"] is not None else None
        r["primary_project_id"] = int(r["primary_project_id"]) if r["primary_project_id"] is not None else None

    if with_ == "attributes":
        attach_attributes(auth.conn, rows, "maludb_document_with_attributes", "document_id")

    return {"documents": rows}


# ===========================================================================
# POST /v1/documents — multipart file upload
# ===========================================================================


@router.post("/v1/documents")
async def create_document(
    auth: Auth,
    file: UploadFile = File(...),
    filename: str | None = Form(default=None),
    mime_type: str | None = Form(default=None),
    description: str | None = Form(default=None),
    document_type: str | None = Form(default=None),
    projects: str | None = Form(default=None),
    subjects: str | None = Form(default=None),
):
    # Read the uploaded file bytes.
    file_bytes = await file.read()
    if not file_bytes:
        json_error("bad_request", "Could not read the uploaded file.", 400)

    fname = (filename or "").strip() if filename else ""
    if not fname:
        fname = (file.filename or "upload").strip() or "upload"

    mime = (mime_type or "").strip() if mime_type else ""
    if not mime:
        mime = (file.content_type or "application/octet-stream").strip() or "application/octet-stream"

    doc_type = document_type.strip() if document_type and document_type.strip() else None
    size = len(file_bytes)
    content_hash = hashlib.sha256(file_bytes).hexdigest()

    # Insert source package — psycopg v3 handles bytes natively for bytea.
    sp_row = db_one(
        auth.conn,
        """INSERT INTO maludb_source_package
               (source_type, content_bytes, media_type, content_size, content_hash, ingested_at)
           VALUES ('document', %s, %s, %s, %s, now()) RETURNING source_package_id""",
        [file_bytes, mime, size, content_hash],
    )
    spid = int(sp_row["source_package_id"])  # type: ignore[index]

    metadata_json = json.dumps({"description": description, "filename": fname})

    doc = db_one(
        auth.conn,
        """INSERT INTO maludb_document
               (source_package_id, title, source_type, media_type, document_type, metadata_jsonb, created_at)
           VALUES (%s, %s, 'document', %s, %s, %s, now())
           RETURNING document_id AS id, title, source_type, media_type, document_type, created_at""",
        [spid, fname, mime, doc_type, metadata_json],
    )
    doc["id"] = int(doc["id"])
    doc["description"] = description
    doc["content_size"] = size

    # Graph wiring (0.87.0): optional comma-separated projects/subjects →
    # document→subject edges + soft tags; primary_project_id from the first project.
    project_names = _parse_names(projects)
    subject_names = _parse_names(subjects)

    primary = None
    if project_names or subject_names:
        def _wire_graph(conn):
            first = None
            for p in project_names:
                sid = document_link_subject(conn, doc["id"], "project", p)
                if first is None and sid is not None:
                    first = sid
            for s in subject_names:
                document_link_subject(conn, doc["id"], "subject", s)
            if first is not None:
                db_exec(
                    conn,
                    "UPDATE maludb_document SET primary_project_id = %s"
                    " WHERE document_id = %s AND primary_project_id IS NULL",
                    [first, doc["id"]],
                )
            return first

        primary = db_tx_core(auth.conn, _wire_graph)

    doc["primary_project_id"] = primary

    return JSONResponse(status_code=201, content={"document": doc})


# ===========================================================================
# GET /v1/documents/{id} — document detail
# ===========================================================================


@router.get("/v1/documents/{document_id}")
def get_document(document_id: int, auth: Auth):
    doc = _load_document_detail(auth, document_id)
    if doc is None:
        json_error("not_found", "Document not found.", 404)
    return {"document": doc}


# ===========================================================================
# PATCH /v1/documents/{id} — link/unlink projects & subjects
# ===========================================================================


@router.patch("/v1/documents/{document_id}")
async def update_document(document_id: int, auth: Auth, request: Request):
    if db_one(auth.conn, "SELECT 1 FROM maludb_document WHERE document_id = %s", [document_id]) is None:
        json_error("not_found", "Document not found.", 404)

    body = await request.json()

    # Pull a list of names for body[op][kind]; reject anything that is not a string array.
    def _names(op: str, kind: str) -> list[str]:
        lst = (body.get(op) or {}).get(kind)
        if lst is None:
            return []
        if not isinstance(lst, list):
            json_error("validation_failed", f'"{op}.{kind}" must be an array of names.', 422)
        out: dict[str, str] = {}
        for n in lst:
            if not isinstance(n, str):
                json_error("validation_failed", f'"{op}.{kind}" must contain only strings.', 422)
            n = n.strip()
            if n and n not in out:
                out[n] = n
        return list(out.values())

    link_projects = _names("link", "projects")
    link_subjects = _names("link", "subjects")
    unlink_projects = _names("unlink", "projects")
    unlink_subjects = _names("unlink", "subjects")

    if not link_projects and not link_subjects and not unlink_projects and not unlink_subjects:
        json_error("bad_request", "Provide link/unlink projects or subjects to change.", 400)

    def _patch(conn):
        # Unlink first so a re-link in the same request re-establishes the edge cleanly.
        for p in unlink_projects:
            document_unlink_subject(conn, document_id, "project", p)
        for s in unlink_subjects:
            document_unlink_subject(conn, document_id, "subject", s)

        first = None
        for p in link_projects:
            sid = document_link_subject(conn, document_id, "project", p)
            if first is None and sid is not None:
                first = sid
        for s in link_subjects:
            document_link_subject(conn, document_id, "subject", s)

        # Adopt a primary project when one isn't set yet (unlink may have just cleared it).
        if first is not None:
            db_exec(
                conn,
                "UPDATE maludb_document SET primary_project_id = %s"
                " WHERE document_id = %s AND primary_project_id IS NULL",
                [first, document_id],
            )

    db_tx_core(auth.conn, _patch)

    return {"document": _load_document_detail(auth, document_id)}


# ===========================================================================
# DELETE /v1/documents/{id} — delete document + source package + graph edges
# ===========================================================================


@router.delete("/v1/documents/{document_id}")
def delete_document(document_id: int, auth: Auth):
    row = db_one(
        auth.conn,
        "SELECT source_package_id FROM maludb_document WHERE document_id = %s",
        [document_id],
    )
    if row is None:
        json_error("not_found", "Document not found.", 404)

    # Remove the document's graph edges first — deleting the document cascades
    # its soft tags but NOT its document→subject svpor_statement edges (0.87.0),
    # which would otherwise dangle.
    db_tx_core(
        auth.conn,
        lambda conn: db_exec(
            conn,
            "DELETE FROM maludb_svpor_statement WHERE subject_kind = 'document' AND subject_id = %s",
            [document_id],
        ),
    )
    db_exec(auth.conn, "DELETE FROM maludb_document WHERE document_id = %s", [document_id])
    if row["source_package_id"] is not None:
        db_exec(
            auth.conn,
            "DELETE FROM maludb_source_package WHERE source_package_id = %s",
            [row["source_package_id"]],
        )

    return {"deleted": True, "id": document_id}


# ===========================================================================
# POST /v1/documents-backfill — run maludb_document_graph_backfill()
# ===========================================================================


@router.post("/v1/documents-backfill")
def backfill_documents(auth: Auth):
    def _backfill(conn):
        return db_one(conn, "SELECT maludb_document_graph_backfill() AS n")

    linked = db_tx_core(auth.conn, _backfill)
    return {"linked": int(linked["n"])}  # type: ignore[index]
