"""
Graph endpoints — edges, neighbors, walk, path, stats.

Ports PHP's edges.php, graph_neighbors.php, and graph_walk.php.

- GET /v1/edges        — unified edge view over maludb_edge
- GET /v1/graph/neighbors — one-hop neighbors via maludb_graph_neighbors()
- GET /v1/graph/walk      — multi-hop BFS via maludb_graph_walk()
- GET /v1/graph/path      — source→target paths via maludb_graph_path() (core ≥0.101.0)
- GET /v1/graph/stats     — node/edge/rel/store aggregates over maludb_edge

All queries run inside db_tx_core() so the maludb_core facade views resolve.
"""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, Query, Request

from app.auth import Auth
from app.database import db_query, db_tx_core
from app.errors import json_error

router = APIRouter()

_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")

# Graphify tags every extracted relationship EXTRACTED / INFERRED /
# AMBIGUOUS; SVO statements store numeric confidence. The mapping is
# reversible (three distinct values).
_CONFIDENCE_MAP = {"EXTRACTED": 1.0, "INFERRED": 0.7, "AMBIGUOUS": 0.4}

_MAX_NODES = 50_000
_MAX_LINKS = 200_000
_MAX_SKIPPED_REPORTED = 200


# ===========================================================================
# GET /v1/edges — unified edge view
# ===========================================================================


@router.get("/v1/edges")
def list_edges(
    auth: Auth,
    source_kind: str | None = Query(default=None, max_length=40),
    source_id: int | None = Query(default=None),
    target_kind: str | None = Query(default=None, max_length=40),
    target_id: int | None = Query(default=None),
    rel: str | None = Query(default=None, max_length=120),
    edge_store: str | None = Query(default=None, max_length=40),
    limit: int = Query(default=200, le=500),
):
    def _query(conn):
        clauses: list[str] = []
        params: list = []
        if source_kind:
            clauses.append("source_kind = %s")
            params.append(source_kind)
        if source_id is not None:
            clauses.append("source_id = %s")
            params.append(source_id)
        if target_kind:
            clauses.append("target_kind = %s")
            params.append(target_kind)
        if target_id is not None:
            clauses.append("target_id = %s")
            params.append(target_id)
        if rel:
            clauses.append("rel = %s")
            params.append(rel)
        if edge_store:
            clauses.append("edge_store = %s")
            params.append(edge_store)

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        sql = f"""SELECT edge_store, edge_id, source_kind, source_id, rel,
                         target_kind, target_id, confidence, provenance
                    FROM maludb_edge
                    {where}
                   ORDER BY edge_store, edge_id DESC
                   LIMIT %s"""
        params.append(limit)

        rows = db_query(conn, sql, params)
        for r in rows:
            r["edge_id"] = int(r["edge_id"]) if r["edge_id"] is not None else None
            r["source_id"] = int(r["source_id"])
            r["target_id"] = int(r["target_id"])
            r["confidence"] = float(r["confidence"]) if r["confidence"] is not None else None
        return rows

    rows = db_tx_core(auth.conn, _query)
    return {"edges": rows}


# ===========================================================================
# GET /v1/graph/neighbors — one-hop neighbors
# ===========================================================================


@router.get("/v1/graph/neighbors")
def graph_neighbors(
    auth: Auth,
    kind: str = Query(max_length=40),
    id: int = Query(),
    direction: str = Query(default="both", max_length=20),
    rel: str | None = Query(default=None, max_length=400),
):
    if not kind:
        json_error("missing_field", 'Query param "kind" is required.', 400)

    def _query(conn):
        if rel:
            rel_list = [r.strip() for r in rel.split(",") if r.strip()]
            sql = """SELECT neighbor_kind, neighbor_id, rel, edge_store,
                            confidence, provenance, label
                       FROM maludb_graph_neighbors(%s, %s, %s, %s::text[])"""
            params = [kind, id, direction, rel_list]
        else:
            sql = """SELECT neighbor_kind, neighbor_id, rel, edge_store,
                            confidence, provenance, label
                       FROM maludb_graph_neighbors(%s, %s, %s)"""
            params = [kind, id, direction]

        rows = db_query(conn, sql, params)
        for r in rows:
            r["neighbor_id"] = int(r["neighbor_id"])
            r["confidence"] = float(r["confidence"]) if r["confidence"] is not None else None
        return rows

    rows = db_tx_core(auth.conn, _query)
    return {"kind": kind, "id": id, "direction": direction, "neighbors": rows}


# ===========================================================================
# GET /v1/graph/walk — multi-hop BFS
# ===========================================================================


@router.get("/v1/graph/walk")
def graph_walk(
    auth: Auth,
    kind: str = Query(max_length=40),
    id: int = Query(),
    max_depth: int = Query(default=4, le=20),
    direction: str = Query(default="both", max_length=20),
    rel: str | None = Query(default=None, max_length=400),
):
    if not kind:
        json_error("missing_field", 'Query param "kind" is required.', 400)

    def _query(conn):
        if rel:
            rel_list = [r.strip() for r in rel.split(",") if r.strip()]
            sql = """SELECT object_kind, object_id, depth, rel, edge_store, label, path
                       FROM maludb_graph_walk(%s, %s, %s, %s, %s::text[])"""
            params = [kind, id, max_depth, direction, rel_list]
        else:
            sql = """SELECT object_kind, object_id, depth, rel, edge_store, label, path
                       FROM maludb_graph_walk(%s, %s, %s, %s)"""
            params = [kind, id, max_depth, direction]

        rows = db_query(conn, sql, params)
        for r in rows:
            r["object_id"] = int(r["object_id"])
            r["depth"] = int(r["depth"])
            # psycopg v3 auto-converts Postgres text[] to Python list;
            # ensure None/empty becomes [].
            if r["path"] is None:
                r["path"] = []
        return rows

    rows = db_tx_core(auth.conn, _query)
    return {
        "kind": kind,
        "id": id,
        "max_depth": max_depth,
        "direction": direction,
        "walk": rows,
    }


# ===========================================================================
# GET /v1/graph/path — source→target paths, shortest first
# ===========================================================================


@router.get("/v1/graph/path")
def graph_path(
    auth: Auth,
    source_kind: str = Query(max_length=40),
    source_id: int = Query(),
    target_kind: str = Query(max_length=40),
    target_id: int = Query(),
    max_depth: int = Query(default=6, ge=1, le=32),
    direction: str = Query(default="both", max_length=20),
    rel: str | None = Query(default=None, max_length=400),
):
    if not source_kind:
        json_error("missing_field", 'Query param "source_kind" is required.', 400)
    if not target_kind:
        json_error("missing_field", 'Query param "target_kind" is required.', 400)

    def _query(conn):
        if rel:
            rel_list = [r.strip() for r in rel.split(",") if r.strip()]
            sql = """SELECT depth, path
                       FROM maludb_graph_path(%s, %s, %s, %s, %s, %s, %s::text[])"""
            params = [source_kind, source_id, target_kind, target_id, max_depth, direction, rel_list]
        else:
            sql = """SELECT depth, path
                       FROM maludb_graph_path(%s, %s, %s, %s, %s, %s)"""
            params = [source_kind, source_id, target_kind, target_id, max_depth, direction]

        rows = db_query(conn, sql, params)
        for r in rows:
            r["depth"] = int(r["depth"])
            if r["path"] is None:
                r["path"] = []
        return rows

    rows = db_tx_core(auth.conn, _query)
    return {
        "source_kind": source_kind,
        "source_id": source_id,
        "target_kind": target_kind,
        "target_id": target_id,
        "max_depth": max_depth,
        "direction": direction,
        "paths": rows,
    }


# ===========================================================================
# GET /v1/graph/stats — aggregates over the unified edge view
# ===========================================================================


@router.get("/v1/graph/stats")
def graph_stats(
    auth: Auth,
    top_rels: int = Query(default=25, ge=1, le=100),
):
    def _query(conn):
        totals = db_query(
            conn,
            "SELECT count(*) AS edges FROM maludb_edge",
        )[0]

        nodes = db_query(
            conn,
            """SELECT count(*) AS nodes
                 FROM (SELECT source_kind AS kind, source_id AS id FROM maludb_edge
                       UNION
                       SELECT target_kind, target_id FROM maludb_edge) endpoints""",
        )[0]

        by_store = db_query(
            conn,
            """SELECT edge_store, count(*) AS edges
                 FROM maludb_edge
                GROUP BY edge_store
                ORDER BY edges DESC, edge_store""",
        )

        by_rel = db_query(
            conn,
            """SELECT rel, count(*) AS edges
                 FROM maludb_edge
                GROUP BY rel
                ORDER BY edges DESC, rel NULLS LAST
                LIMIT %s""",
            [top_rels],
        )

        return {
            "edges": int(totals["edges"]),
            "nodes": int(nodes["nodes"]),
            "by_store": {r["edge_store"]: int(r["edges"]) for r in by_store},
            "top_rels": [
                {"rel": r["rel"], "edges": int(r["edges"])} for r in by_rel
            ],
        }

    stats = db_tx_core(auth.conn, _query)
    return {"stats": stats}


# ===========================================================================
# POST /v1/graph/import — bulk import of a Graphify node-link graph
# ===========================================================================


def _clean_text(value, max_len: int) -> str:
    """Strip control characters and cap length."""
    return _CONTROL_CHARS_RE.sub("", str(value)).strip()[:max_len]


def _link_confidence(raw) -> float | None:
    """Map Graphify's EXTRACTED/INFERRED/AMBIGUOUS (or a numeric) to [0,1]."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return max(0.0, min(1.0, float(raw)))
    return _CONFIDENCE_MAP.get(str(raw).strip().upper())


@router.post("/v1/graph/import")
async def graph_import(auth: Auth, request: Request):
    """
    Import a Graphify graph (NetworkX node-link JSON) into the tenant graph.

    Body: {
      "namespace": "my-repo",                  # required; prefixes canonical names
      "provenance": "graphify-0.9.6",          # optional free text on the namespace subject
      "graph": {"nodes": [...], "links": [...]}  # "edges" accepted as alias of "links"
      "options": {"chunk_size": 500}           # optional
    }

    Nodes become subjects (canonical_name = "<namespace>/<node id>", label as
    alias + attribute); links become SVO statements (relation as verb, with
    numeric confidence). Idempotent: subjects upsert by canonical name and
    statements by (subject, verb, object) identity, so re-importing the same
    graph updates rather than duplicates. Runs in a single transaction.

    Note: the core ingest counts statement upserts as "created", so
    edges.created on a re-import reports the upserted total even though no
    new rows were inserted; nodes.created/resolved distinguish correctly.
    """
    body = await request.json()
    if not isinstance(body, dict):
        json_error("validation_failed", "Body must be a JSON object.", 422)

    namespace = str(body.get("namespace") or "").strip()
    if not namespace:
        json_error("missing_field", 'Field "namespace" is required.', 400)
    if not _NAMESPACE_RE.match(namespace):
        json_error(
            "validation_failed",
            '"namespace" must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$.',
            422,
        )

    graph = body.get("graph")
    if not isinstance(graph, dict):
        json_error("missing_field", 'Field "graph" (node-link object) is required.', 400)

    nodes = graph.get("nodes")
    links = graph.get("links", graph.get("edges", []))
    if not isinstance(nodes, list) or not nodes:
        json_error("validation_failed", '"graph.nodes" must be a non-empty array.', 422)
    if not isinstance(links, list):
        json_error("validation_failed", '"graph.links" must be an array.', 422)
    if len(nodes) > _MAX_NODES:
        json_error("validation_failed", f'"graph.nodes" exceeds the {_MAX_NODES} node cap.', 422)
    if len(links) > _MAX_LINKS:
        json_error("validation_failed", f'"graph.links" exceeds the {_MAX_LINKS} link cap.', 422)

    options = body.get("options") or {}
    chunk_size = options.get("chunk_size", 500)
    if not isinstance(chunk_size, int) or not (50 <= chunk_size <= 5000):
        json_error("validation_failed", '"options.chunk_size" must be an integer in [50, 5000].', 422)

    provenance = _clean_text(body.get("provenance") or "graphify", 200)

    # ---- normalize nodes -------------------------------------------------
    skipped: list[dict] = []
    subjects_by_id: dict[str, dict] = {}
    for i, n in enumerate(nodes):
        if not isinstance(n, dict):
            skipped.append({"section": "nodes", "index": i, "reason": "not an object"})
            continue
        node_id = _clean_text(n.get("id") or "", 512)
        if not node_id:
            skipped.append({"section": "nodes", "index": i, "reason": "missing id"})
            continue
        if node_id in subjects_by_id:
            skipped.append({"section": "nodes", "index": i, "reason": f"duplicate id {node_id!r}"})
            continue

        label = _clean_text(n.get("label") or "", 256)
        node_type = _clean_text(n.get("file_type") or n.get("type") or "concept", 60) or "concept"
        attributes = []
        for attr in ("label", "source_file", "source_location", "community", "file_type"):
            if n.get(attr) is not None and str(n.get(attr)).strip() != "":
                attributes.append(
                    {"attr_name": f"graphify_{attr}", "value_text": _clean_text(n[attr], 2000)}
                )
        subject = {
            "key": node_id,
            "name": f"{namespace}/{node_id}",
            "type": node_type,
        }
        if label and label != node_id:
            subject["aliases"] = [label]
        if attributes:
            subject["attributes"] = attributes
        subjects_by_id[node_id] = subject

    # ---- normalize links -------------------------------------------------
    edges: list[dict] = []
    for i, l in enumerate(links):
        if not isinstance(l, dict):
            skipped.append({"section": "links", "index": i, "reason": "not an object"})
            continue
        src = _clean_text(l.get("source") or "", 512)
        tgt = _clean_text(l.get("target") or "", 512)
        if src not in subjects_by_id or tgt not in subjects_by_id:
            skipped.append({"section": "links", "index": i, "reason": "unknown source/target node id"})
            continue
        relation = _clean_text(l.get("relation") or "related_to", 120) or "related_to"
        edge = {"subject": src, "verb": relation, "object": tgt}
        confidence = _link_confidence(l.get("confidence"))
        if confidence is not None:
            edge["confidence"] = confidence
        edges.append(edge)

    subject_list = list(subjects_by_id.values())

    # ---- namespace root subject (namespaces become discoverable) ----------
    root_subject = {
        "key": "$namespace",
        "name": namespace,
        "type": "graph_namespace",
        "attributes": [{"attr_name": "provenance", "value_text": provenance}],
    }

    # ---- chunked ingest in one transaction --------------------------------
    def _import(conn):
        # Subject types are a curated per-tenant catalog (no tenant-writable
        # registration facade yet), so unseen node types fall back to a seeded
        # generic; the declared type is preserved in the graphify_file_type /
        # graphify_type attributes.
        catalog = {
            r["subject_type"]
            for r in db_query(conn, "SELECT subject_type FROM maludb_subject_type")
        }
        fallback = next((t for t in ("concept", "other") if t in catalog), None)
        if fallback is None:
            json_error(
                "validation_failed",
                "Tenant subject-type catalog has no generic type "
                "('concept' or 'other') to map graph nodes onto.",
                422,
            )
        for subject in [root_subject, *subject_list]:
            if subject["type"] not in catalog:
                subject.setdefault("attributes", []).append(
                    {"attr_name": "graphify_type", "value_text": subject["type"]}
                )
                subject["type"] = fallback

        totals = {
            "subjects_created": 0,
            "subjects_resolved": 0,
            "verbs_created": 0,
            "edges_created": 0,
            "chunks": 0,
        }

        def _ingest(payload: dict, count_subjects: bool = False):
            rows = db_query(
                conn,
                "SELECT maludb_memory_ingest_extraction(%s::jsonb) AS report",
                [json.dumps(payload)],
            )
            report = rows[0]["report"]
            created = report.get("created", {})
            resolved = report.get("resolved", {})
            # Pass 2 re-lists edge endpoints to satisfy the ingest contract's
            # same-payload key resolution; only pass 1 counts subjects, so
            # those re-resolves don't inflate the report.
            if count_subjects:
                totals["subjects_created"] += int(created.get("subjects", 0))
                totals["subjects_resolved"] += int(resolved.get("subjects", 0))
            totals["verbs_created"] += int(created.get("verbs", 0))
            totals["edges_created"] += int(created.get("edges", 0))
            totals["chunks"] += 1
            for item in report.get("skipped", []):
                if len(skipped) < _MAX_SKIPPED_REPORTED:
                    skipped.append(item)

        # Pass 1: subjects (root first so the namespace exists even for
        # a nodes-only import).
        _ingest({"subjects": [root_subject]}, count_subjects=True)
        for i in range(0, len(subject_list), chunk_size):
            _ingest({"subjects": subject_list[i : i + chunk_size]}, count_subjects=True)

        # Pass 2: edges. Each chunk re-lists its endpoint subjects (name-only
        # entries resolve idempotently to the existing rows) so chunks are
        # self-contained — the ingest contract resolves from/to against keys
        # in the same payload.
        for i in range(0, len(edges), chunk_size):
            chunk = edges[i : i + chunk_size]
            endpoint_ids = {e["subject"] for e in chunk} | {e["object"] for e in chunk}
            resolve_subjects = [
                {
                    "key": nid,
                    "name": subjects_by_id[nid]["name"],
                    "type": subjects_by_id[nid]["type"],
                }
                for nid in sorted(endpoint_ids)
            ]
            _ingest({"subjects": resolve_subjects, "edges": chunk})

        return totals

    totals = db_tx_core(auth.conn, _import)

    return {
        "namespace": namespace,
        "nodes": {
            "received": len(nodes),
            "imported": len(subject_list),
            "created": totals["subjects_created"],
            "resolved": totals["subjects_resolved"],
        },
        "edges": {
            "received": len(links),
            "imported": len(edges),
            "created": totals["edges_created"],
        },
        "verbs_created": totals["verbs_created"],
        "chunks": totals["chunks"],
        "skipped": skipped[:_MAX_SKIPPED_REPORTED],
    }
