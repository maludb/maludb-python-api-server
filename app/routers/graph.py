"""
Graph endpoints — edges, neighbors, walk.

Ports PHP's edges.php, graph_neighbors.php, and graph_walk.php.

- GET /v1/edges        — unified edge view over maludb_edge
- GET /v1/graph/neighbors — one-hop neighbors via maludb_graph_neighbors()
- GET /v1/graph/walk      — multi-hop BFS via maludb_graph_walk()

All queries run inside db_tx_core() so the maludb_core facade views resolve.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.auth import Auth
from app.database import db_query, db_tx_core
from app.errors import json_error

router = APIRouter()


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
