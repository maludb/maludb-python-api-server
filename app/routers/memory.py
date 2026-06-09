"""
Memory pipeline endpoints — config, documents, search, ingest.

Ports PHP's memory_config.php, memory_documents.php, memory_search.php,
memory_ingest.php.  The API is the model worker: it chunks text, calls the
LLM (extraction) and the embedding model, then writes results back via the
maludb_* facades.

Endpoints:
    GET  /v1/memory/config     — read namespace config
    POST /v1/memory/config     — full config setup (secret + provider + alias + bind)
    PUT  /v1/memory/config     — same as POST
    POST /v1/memory/documents  — upload + chunk + extract + embed + ingest
    POST /v1/memory/search     — embed query + vector search
    POST /v1/memory/ingest     — text -> LLM pipeline (per-model prompt)
"""

from __future__ import annotations

import json
import os
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.auth import Auth, get_auth_store
from app.database import db_one, db_query, db_tx_core
from app.errors import json_error
from app.helpers.llm import (
    llm_complete,
    llm_json_from_text,
    mem_chunk,
    mem_embed,
    mem_extract,
    mem_resolve_token,
    mem_vector_literal,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# GET /v1/memory/config — read namespace config
# ---------------------------------------------------------------------------


@router.get("/v1/memory/config")
def get_memory_config(auth: Auth, namespace: str = "default"):
    row = db_tx_core(auth.conn, lambda c: db_one(c, "SELECT maludb_memory_model_config(%s) AS cfg", [namespace]))
    cfg = None
    if row and row["cfg"] is not None:
        cfg = row["cfg"] if isinstance(row["cfg"], dict) else json.loads(row["cfg"])
    return {"namespace": namespace, "config": cfg}


# ---------------------------------------------------------------------------
# POST/PUT /v1/memory/config — full config setup
# ---------------------------------------------------------------------------


@router.post("/v1/memory/config")
@router.put("/v1/memory/config")
async def set_memory_config(auth: Auth, request: Request):
    body = await request.json()

    namespace = str(body.get("namespace") or "default").strip() or "default"
    _sn = body.get("secret_name")
    secret_name = str(_sn).strip() if _sn and str(_sn).strip() else None
    token = str(body["token"]) if "token" in body and body["token"] is not None else None

    provider = body.get("provider") if isinstance(body.get("provider"), dict) else {}
    alias = body.get("alias") if isinstance(body.get("alias"), dict) else {}

    prov_name = str(provider.get("name", "")).strip()
    prov_kind = str(provider.get("kind", "")).strip()
    prov_adapter = str(provider["adapter_name"]) if provider.get("adapter_name") is not None else None
    prov_sens = str(provider.get("data_sensitivity", "")).strip() or "internal"

    alias_name = str(alias.get("name", "")).strip()
    alias_model = str(alias.get("model_identifier", "")).strip()
    alias_ctx = int(alias["context_length"]) if alias.get("context_length") is not None else None
    base_url = str(alias.get("base_url", "")).strip()

    embedding_model = str(body.get("embedding_model", "")).strip()
    _pt = body.get("prompt_template")
    prompt_template = str(_pt) if "prompt_template" in body and _pt is not None else None
    gen_params = json.dumps(body["generation_params"]) if isinstance(body.get("generation_params"), dict) else "{}"
    default_subject = str(body.get("default_subject_type", "")).strip() or "other"
    default_prov = str(body.get("default_provenance", "")).strip() or "suggested"

    # Shape validation
    if not prov_name or not prov_kind:
        json_error("missing_field", "provider.name and provider.kind are required.", 400)
    if not alias_name or not alias_model:
        json_error("missing_field", "alias.name and alias.model_identifier are required.", 400)
    if not base_url:
        json_error("missing_field", "alias.base_url is required.", 400)
    if not embedding_model:
        json_error("missing_field", '"embedding_model" is required.', 400)
    if prompt_template is not None and "{{chunk}}" not in prompt_template:
        json_error("validation_failed", "prompt_template must contain the {{chunk}} placeholder.", 422)
    if token is not None and secret_name is None:
        json_error("missing_field", '"secret_name" is required when a token is provided.', 400)

    def _setup(conn):
        # 1. Store the token encrypted (redacted from logs).
        if token is not None:
            db_one(
                conn,
                "SELECT secret_id FROM maludb_core.secret_set(p_name => %s, p_kind => 'provider', p_value => %s)",
                [secret_name, token],
            )
        # 2. Register the provider.
        db_one(
            conn,
            """SELECT maludb_register_model_provider(
                        p_name => %s, p_kind => %s, p_adapter_name => %s,
                        p_secret_ref => %s, p_data_sensitivity => %s) AS id""",
            [prov_name, prov_kind, prov_adapter, secret_name, prov_sens],
        )
        # 3. Register the alias.
        db_one(
            conn,
            """SELECT maludb_register_model_alias(
                        p_alias => %s, p_provider => %s, p_model_identifier => %s,
                        p_context_length => %s, p_runtime_params => jsonb_build_object('base_url', %s::text)) AS id""",
            [alias_name, prov_name, alias_model, alias_ctx, base_url],
        )
        # 4. Bind alias + prompt + embedding + defaults.
        db_one(
            conn,
            """SELECT maludb_memory_set_model_config(
                        p_extraction_alias     => %s,
                        p_prompt_template      => %s,
                        p_embedding_model      => %s,
                        p_namespace            => %s,
                        p_generation_params    => %s::jsonb,
                        p_default_subject_type => %s,
                        p_default_provenance   => %s) AS cfg""",
            [alias_name, prompt_template, embedding_model, namespace, gen_params, default_subject, default_prov],
        )
        # 5. Read it back.
        row = db_one(conn, "SELECT maludb_memory_model_config(%s) AS cfg", [namespace])
        if row and row["cfg"] is not None:
            return row["cfg"] if isinstance(row["cfg"], dict) else json.loads(row["cfg"])
        return None

    cfg = db_tx_core(auth.conn, _setup)
    return {"namespace": namespace, "config": cfg}


# ---------------------------------------------------------------------------
# POST /v1/memory/documents — upload + chunk + extract + embed + ingest
# ---------------------------------------------------------------------------


def _to_text_array(v: Any) -> list[str]:
    """Normalize a list value to a list of trimmed, non-empty strings."""
    if not isinstance(v, list):
        return []
    return [s.strip() for s in v if isinstance(s, str) and s.strip()]


def _pg_text_array(items: list[str]) -> str:
    """Format a Python list as a Postgres text[] literal."""
    escaped = ['"' + s.replace('"', '\\"') + '"' for s in items]
    return "{" + ",".join(escaped) + "}"


@router.post("/v1/memory/documents")
async def memory_documents(auth: Auth, request: Request):
    body = await request.json()

    title = str(body.get("title", "")).strip()
    text = str(body.get("text", ""))
    if not title:
        json_error("missing_field", 'Field "title" is required.', 400)
    if not text.strip():
        json_error("missing_field", 'Field "text" is required.', 400)

    namespace = str(body.get("namespace") or "default").strip() or "default"
    source_type = str(body.get("source_type") or "document").strip() or "document"
    media_type = str(body["media_type"]) if body.get("media_type") is not None else None
    _dt = body.get("document_type")
    doc_type = str(_dt).strip() if _dt and str(_dt).strip() else None
    metadata = json.dumps(body["metadata"]) if isinstance(body.get("metadata"), dict) else "{}"

    projects = _to_text_array(body.get("projects"))
    subjects = _to_text_array(body.get("subjects"))
    verbs = _to_text_array(body.get("verbs"))
    events = _to_text_array(body.get("events"))

    chunk_cfg = body.get("chunk") if isinstance(body.get("chunk"), dict) else {}
    chunk_max = max(200, int(chunk_cfg.get("max", 2000)))
    chunk_overlap = max(0, int(chunk_cfg.get("overlap", 200)))

    # Config from DB
    row = db_tx_core(auth.conn, lambda c: db_one(c, "SELECT maludb_memory_model_config(%s) AS cfg", [namespace]))
    cfg_raw = {}
    if row and row["cfg"] is not None:
        cfg_raw = row["cfg"] if isinstance(row["cfg"], dict) else json.loads(row["cfg"])
        if not isinstance(cfg_raw, dict):
            cfg_raw = {}

    embedding_model = (
        str(body["embedding_model"]).strip()
        if body.get("embedding_model") and str(body["embedding_model"]).strip()
        else cfg_raw.get("embedding_model") or os.environ.get("MALUDB_EMBED_MODEL", "maludb-local-dev")
    )
    default_subject = cfg_raw.get("default_subject_type", "other")
    default_prov = cfg_raw.get("default_provenance", "suggested")
    model_id = cfg_raw.get("model_identifier", "")

    # Extraction config for the LLM call
    extract_cfg = {
        "base_url": cfg_raw.get("base_url", ""),
        "model_identifier": model_id,
        "prompt_template": cfg_raw.get("prompt_template"),
        "generation_params": cfg_raw.get("generation_params", {}),
        "token": mem_resolve_token(auth.conn, cfg_raw.get("secret_ref")),
    }
    # Embedding config
    embed_cfg: dict[str, str] = {"embedding_model": embedding_model}

    # 1. Obtain candidate edges: caller-supplied (bypass) OR LLM extraction per chunk
    provided = body.get("edges") if isinstance(body.get("edges"), list) else None
    chunks = mem_chunk(text, chunk_max, chunk_overlap)

    edges: list[dict] = []
    extractor = "provided"
    if provided is not None:
        for e in provided:
            if isinstance(e, dict):
                edges.append(e)
    else:
        extractor = "llm"
        for chunk in chunks:
            for e in mem_extract(chunk, extract_cfg):
                if isinstance(e, dict):
                    if not e.get("source_span") or not str(e["source_span"]).strip():
                        e["source_span"] = chunk
                    edges.append(e)

    if not edges:
        json_error("no_edges", 'No SVPO edges to ingest (supply "edges" or configure an extraction model).', 422)

    # 2. Embed each edge
    for e in edges:
        span = str(e.get("source_span", "")).strip()
        if not span:
            span = (str(e.get("subject_text", "")) + " " + str(e.get("verb_text", ""))).strip()
        e["__vector"] = mem_vector_literal(mem_embed(span, embed_cfg))
        e["source_span"] = span

    # 3. One transaction per document: upload, then ingest every edge.
    def _ingest(conn):
        doc = db_one(
            conn,
            """SELECT maludb_upload_document(
                        p_title => %s, p_content_text => %s, p_source_type => %s,
                        p_media_type => %s, p_document_type => %s,
                        p_projects => %s::text[], p_subjects => %s::text[],
                        p_verbs => %s::text[], p_events => %s::text[],
                        p_metadata_jsonb => %s::jsonb) AS id""",
            [
                title, text, source_type, media_type, doc_type,
                _pg_text_array(projects), _pg_text_array(subjects),
                _pg_text_array(verbs), _pg_text_array(events),
                metadata,
            ],
        )
        document_id = int(doc["id"])

        out: list[dict] = []
        for e in edges:
            subject_text = str(e.get("subject_text", "")).strip()
            verb_text = str(e.get("verb_text", "")).strip()
            if not subject_text or not verb_text:
                json_error("validation_failed", "Each edge needs subject_text and verb_text.", 422)

            predicate = json.dumps(e["predicate"]) if isinstance(e.get("predicate"), list) else "[]"
            subject_ty = str(e.get("subject_type", "")).strip() or default_subject
            confidence = str(e["confidence"]) if "confidence" in e and e["confidence"] is not None else None
            provenance = str(e.get("provenance", "")).strip() or default_prov
            extr_model = model_id if model_id else extractor

            st = db_one(
                conn,
                """SELECT maludb_memory_ingest_edge(
                            p_source_kind      => 'document', p_source_id => %s,
                            p_subject_text     => %s, p_verb_text => %s,
                            p_predicate        => %s::jsonb,
                            p_embedding        => %s::maludb_core.malu_vector,
                            p_embedding_model  => %s,
                            p_subject_type     => %s,
                            p_source_span      => %s,
                            p_confidence       => %s::numeric,
                            p_provenance       => %s,
                            p_extraction_model => %s,
                            p_namespace        => %s,
                            p_document_id      => %s) AS statement_id""",
                [
                    document_id, subject_text, verb_text, predicate, e["__vector"],
                    embedding_model, subject_ty, str(e["source_span"]), confidence,
                    provenance, extr_model, namespace, document_id,
                ],
            )
            out.append({
                "statement_id": int(st["statement_id"]),
                "subject_text": subject_text,
                "verb_text": verb_text,
                "subject_type": subject_ty,
                "provenance": provenance,
            })
        return {"document_id": document_id, "edges": out}

    result = db_tx_core(auth.conn, _ingest)

    return JSONResponse(
        status_code=201,
        content={
            "document_id": result["document_id"],
            "namespace": namespace,
            "embedding_model": embedding_model,
            "extractor": extractor,
            "chunk_count": len(chunks),
            "edges": result["edges"],
        },
    )


# ---------------------------------------------------------------------------
# POST /v1/memory/search — embed query, vector search
# ---------------------------------------------------------------------------


@router.post("/v1/memory/search")
async def memory_search(auth: Auth, request: Request):
    body = await request.json()

    query = str(body.get("query", ""))
    if not query.strip():
        json_error("missing_field", 'Field "query" is required.', 400)

    namespace = str(body.get("namespace") or "default").strip() or "default"
    subject = str(body["subject"]).strip() if body.get("subject") and str(body["subject"]).strip() else None
    verb = str(body["verb"]).strip() if body.get("verb") and str(body["verb"]).strip() else None

    if subject is None and verb is None:
        json_error("missing_field", 'Provide "subject" and/or "verb" — the compartment pre-filter is required.', 400)

    limit = max(1, min(200, int(body.get("limit", 20))))
    metric = str(body.get("metric") or "cosine").strip() or "cosine"

    # Same embedding model as ingest
    row = db_tx_core(auth.conn, lambda c: db_one(c, "SELECT maludb_memory_model_config(%s) AS cfg", [namespace]))
    cfg_raw = {}
    if row and row["cfg"] is not None:
        cfg_raw = row["cfg"] if isinstance(row["cfg"], dict) else json.loads(row["cfg"])
        if not isinstance(cfg_raw, dict):
            cfg_raw = {}

    embedding_model = (
        str(body["embedding_model"]).strip()
        if body.get("embedding_model") and str(body["embedding_model"]).strip()
        else cfg_raw.get("embedding_model") or os.environ.get("MALUDB_EMBED_MODEL", "maludb-local-dev")
    )

    vector = mem_vector_literal(mem_embed(query, {"embedding_model": embedding_model}))

    rows = db_tx_core(
        auth.conn,
        lambda c: db_query(
            c,
            """SELECT chunk_id, statement_id, document_id, source_text, distance, similarity,
                    rank_no, subject_name, verb_name
               FROM maludb_memory_search(
                        p_query_embedding => %s::maludb_core.malu_vector,
                        p_subject         => %s,
                        p_verb            => %s,
                        p_namespace       => %s,
                        p_limit           => %s,
                        p_metric          => %s)""",
            [vector, subject, verb, namespace, limit, metric],
        ),
    )
    for r in rows:
        for k in ("chunk_id", "statement_id", "document_id", "rank_no"):
            r[k] = int(r[k]) if r[k] is not None else None
        for k in ("distance", "similarity"):
            r[k] = float(r[k]) if r[k] is not None else None

    return {
        "namespace": namespace,
        "embedding_model": embedding_model,
        "results": rows,
    }


# ---------------------------------------------------------------------------
# POST /v1/memory/ingest — text -> LLM pipeline (per-model prompt)
# ---------------------------------------------------------------------------


@router.post("/v1/memory/ingest")
async def memory_ingest(auth: Auth, request: Request):
    body = await request.json()

    text = str(body.get("text", ""))
    if not text.strip():
        json_error("missing_field", 'Field "text" is required.', 400)

    model = str(body.get("model") or "chatgpt-4o").strip() or "chatgpt-4o"
    namespace = str(body.get("namespace") or "default").strip() or "default"
    preview = bool(body.get("preview"))

    # Hints
    hints_raw = body.get("hints")
    if isinstance(hints_raw, list):
        hints_json = json.dumps(hints_raw)
    elif isinstance(hints_raw, str) and hints_raw.strip():
        decoded = None
        try:
            decoded = json.loads(hints_raw)
        except (json.JSONDecodeError, ValueError):
            pass
        if isinstance(decoded, list):
            hints_json = json.dumps(decoded)
        else:
            hints_json = json.dumps([{"subject-type": "note", "subject-name": hints_raw}])
    else:
        hints_json = "[]"

    # Per-model prompt + LLM connection from SQLite
    store = get_auth_store()
    pr = store.model_prompt(model)
    if pr is None:
        msg = f'No prompt configured for model "{model}". Set one via POST /v1/model-prompts.'
        json_error("model_not_configured", msg, 422)

    # Known subjects / verbs from Postgres
    subj_rows = db_query(
        auth.conn,
        "SELECT canonical_name AS name, subject_type AS type FROM maludb_subject ORDER BY canonical_name",
    )
    verb_rows = db_query(auth.conn, "SELECT canonical_name FROM maludb_verb ORDER BY canonical_name")
    known_subjects_json = json.dumps([{"name": r["name"], "type": r["type"]} for r in subj_rows], ensure_ascii=False)
    known_verbs_json = json.dumps([r["canonical_name"] for r in verb_rows], ensure_ascii=False)

    # Subject type catalog (0.96.0)
    try:
        type_rows = db_query(
            auth.conn,
            "SELECT category, subject_type, description FROM maludb_subject_type ORDER BY category, sort_order",
        )
    except Exception:
        type_rows = db_query(
            auth.conn,
            "SELECT category, subject_type, description"
            " FROM maludb_core.malu$svpor_subject_type ORDER BY category, sort_order",
        )

    entity_lines: list[str] = []
    event_lines: list[str] = []
    for r in type_rows:
        desc = " — " + r["description"] if r.get("description") and str(r["description"]).strip() else ""
        line = f"  - {r['subject_type']}{desc}"
        if (r.get("category") or "entity") == "event":
            event_lines.append(line)
        else:
            entity_lines.append(line)

    entity_block = "\n".join(entity_lines) if entity_lines else "  - other"
    event_block = "\n".join(event_lines) if event_lines else "  - task"

    # Build the messages
    system_prompt = str(pr.get("system_prompt", ""))
    system = system_prompt.replace("{{ENTITY_TYPES}}", entity_block).replace("{{EVENT_KINDS}}", event_block)
    user_msg = (
        f"TEXT:\n{text}\n\nHINTS:\n{hints_json}\n\n"
        f"KNOWN_SUBJECTS:\n{known_subjects_json}\n\nKNOWN_VERBS:\n{known_verbs_json}\n"
    )

    if preview:
        return {
            "model": model,
            "api_format": pr.get("api_format", "openai"),
            "system_prompt": system,
            "user_message": user_msg,
            "counts": {
                "known_subjects": len(subj_rows),
                "known_verbs": len(verb_rows),
                "entity_types": len(entity_lines),
                "event_kinds": len(event_lines),
            },
        }

    if not pr.get("api_key"):
        msg = f'No API key set for model "{model}". Set it via POST /v1/model-prompts.'
        json_error("model_api_key_missing", msg, 409)

    # Verify maludb_memory_ingest_extraction is available
    has_facade = db_one(
        auth.conn,
        "SELECT EXISTS(SELECT 1 FROM pg_proc WHERE proname = 'maludb_memory_ingest_extraction') AS ok",
    )
    if not has_facade or not has_facade["ok"]:
        json_error(
            "ingest_unavailable",
            "maludb_memory_ingest_extraction is not available in this database (requires maludb_core 0.92.0).",
            501,
        )

    # Call the LLM
    llm_cfg: dict[str, Any] = {
        "api_format": pr.get("api_format", "openai"),
        "base_url": pr.get("base_url", ""),
        "model_identifier": pr.get("model_identifier") or model,
        "token": pr["api_key"],
        "max_tokens": int(pr.get("max_tokens", 2048)),
        "generation_params": json.loads(pr["generation_params"]) if pr.get("generation_params") else {},
    }
    content = llm_complete(llm_cfg, system, user_msg)
    extraction = llm_json_from_text(content)
    if extraction is None:
        json_error("upstream_error", "LLM output was not a JSON object.", 502)

    # Upload text + ingest extraction (one transaction)
    def _ingest(conn):
        doc = db_one(
            conn,
            "SELECT maludb_upload_document(p_title => %s, p_content_text => %s, p_source_type => 'document') AS id",
            [text[:80].strip(), text],
        )
        document_id = int(doc["id"])
        row = db_one(
            conn,
            """SELECT maludb_memory_ingest_extraction(
                        p_extraction => %s::jsonb, p_source_kind => 'document',
                        p_source_id => %s, p_provenance => 'suggested') AS result""",
            [json.dumps(extraction), document_id],
        )
        if row and row["result"] and isinstance(row["result"], str):
            result_val = json.loads(row["result"])
        else:
            result_val = row["result"] if row else None
        return {"document_id": document_id, "result": result_val}

    result = db_tx_core(auth.conn, _ingest)

    return JSONResponse(
        status_code=201,
        content={
            "document_id": result["document_id"],
            "model": model,
            "api_format": pr.get("api_format", "openai"),
            "namespace": namespace,
            "result": result["result"],
        },
    )
