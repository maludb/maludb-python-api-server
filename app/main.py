"""
FastAPI application — entry point.

Creates the app, registers exception handlers, adds per-request middleware
(SqlTracer setup, connection cleanup, debug injection), and mounts the
health endpoint.
"""

from __future__ import annotations

import json

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app import config
from app.database import TenantDatabaseError
from app.errors import APIError, api_error_handler, database_error_handler, tenant_db_error_handler
from app.routers import attributes, episodes, notes, pools, projects, skills, statements, subjects, tokens, types, verbs
from app.sql_log import SqlTracer, set_tracer

# ---------------------------------------------------------------------------
# Create the FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="MaluDB API")

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(attributes.router)
app.include_router(episodes.router)
app.include_router(notes.router)
app.include_router(pools.router)
app.include_router(projects.router)
app.include_router(skills.router)
app.include_router(statements.router)
app.include_router(subjects.router)
app.include_router(tokens.router)
app.include_router(verbs.router)
app.include_router(types.router)

# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------

app.add_exception_handler(APIError, api_error_handler)  # type: ignore[arg-type]
app.add_exception_handler(psycopg.errors.DatabaseError, database_error_handler)  # type: ignore[arg-type]
app.add_exception_handler(TenantDatabaseError, tenant_db_error_handler)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Per-request middleware — SqlTracer + connection cleanup + debug injection
# ---------------------------------------------------------------------------

class TracerMiddleware(BaseHTTPMiddleware):
    """Middleware that creates a fresh SqlTracer per request, sets endpoint
    metadata, and optionally injects debug SQL trace into JSON responses."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Create and install a fresh tracer for this request
        tracer = SqlTracer()
        tracer.method = request.method
        tracer.uri = str(request.url.path)
        if request.url.query:
            tracer.uri += f"?{request.url.query}"
        # Derive an endpoint name from the path (similar to PHP's basename of SCRIPT_FILENAME)
        tracer.endpoint = request.url.path.rstrip("/").rsplit("/", 1)[-1] or "root"
        set_tracer(tracer)

        response = await call_next(request)

        # Debug injection: if DEBUG_ENABLED and ?debug=1, inject meta.debug into JSON responses
        if (
            config.DEBUG_ENABLED
            and request.query_params.get("debug") == "1"
            and response.headers.get("content-type", "").startswith("application/json")
        ):
            # Read the response body
            body_chunks = []
            async for chunk in response.body_iterator:
                if isinstance(chunk, bytes):
                    body_chunks.append(chunk)
                else:
                    body_chunks.append(chunk.encode("utf-8"))
            body_bytes = b"".join(body_chunks)

            try:
                data = json.loads(body_bytes)
                if isinstance(data, dict):
                    if "meta" not in data:
                        data["meta"] = {}
                    data["meta"]["debug"] = {
                        "endpoint": tracer.endpoint,
                        "queries": tracer.queries,
                    }
                    return JSONResponse(
                        content=data,
                        status_code=response.status_code,
                    )
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

            # If we couldn't parse/modify, return original body
            return Response(
                content=body_bytes,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
            )

        return response


app.add_middleware(TracerMiddleware)


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict:
    """Health check — always returns 200."""
    return {"status": "ok"}
