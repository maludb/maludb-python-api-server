"""
Error handling — APIError exception, json_error() helper, and exception-to-JSON-response
handlers for FastAPI.

Ports PHP's json_error(), handle_uncaught(), and pg_error_message() from config/response.php.
Standard JSON shape: {"error": {"code": "...", "message": "..."}}
"""

from __future__ import annotations

import re

from fastapi import Request
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# APIError — the application-level exception raised by json_error()
# ---------------------------------------------------------------------------

class APIError(Exception):
    """Raised to abort the request with a structured JSON error response."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def json_error(code: str, message: str, status: int = 400) -> None:
    """Raise an APIError — mirrors PHP's json_error() which calls exit()."""
    raise APIError(code, message, status)


# ---------------------------------------------------------------------------
# Exception → JSON response handlers (registered on the FastAPI app)
# ---------------------------------------------------------------------------

async def api_error_handler(_request: Request, exc: APIError) -> JSONResponse:
    """Handle APIError → standard JSON error response."""
    return JSONResponse(
        status_code=exc.status,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


def _pg_error_message(exc: Exception) -> str:
    """Pull the human-readable 'ERROR: ...' line out of a psycopg/Postgres message."""
    msg = str(exc)
    m = re.search(r"ERROR:\s*(.+?)(\n|$)", msg, re.DOTALL)
    if m:
        return m.group(1).strip()
    return msg


async def database_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """
    Handle psycopg.errors.DatabaseError → map SQLSTATE to HTTP status.

    Mirrors PHP's handle_uncaught() PDOException branch:
      23505 → 409 conflict
      42501 → 403 insufficient_privilege
      23502, 23503, 23514, 22000, 22023, 22P02, P0001 → 422 validation_failed
      else  → 500 internal_error
    """
    # psycopg v3 exceptions carry sqlstate as exc.sqlstate (or via diag)
    sqlstate: str | None = getattr(exc, "sqlstate", None)
    if sqlstate is None:
        diag = getattr(exc, "diag", None)
        if diag is not None:
            sqlstate = getattr(diag, "sqlstate", None)

    status = 500
    code = "internal_error"
    message = "An unexpected error occurred."

    if sqlstate == "23505":
        status, code, message = 409, "conflict", _pg_error_message(exc)
    elif sqlstate == "42501":
        status, code, message = 403, "insufficient_privilege", _pg_error_message(exc)
    elif sqlstate in ("23502", "23503", "23514", "22000", "22023", "22P02", "P0001"):
        status, code, message = 422, "validation_failed", _pg_error_message(exc)

    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
    )


async def tenant_db_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """
    Handle TenantDatabaseError → 502 (auth failure) or 503 (unavailable).

    Mirrors PHP's handle_uncaught() TenantDatabaseException branch.
    """
    from app.database import TenantDatabaseError

    assert isinstance(exc, TenantDatabaseError)  # noqa: S101
    if exc.is_auth_failure:
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "code": "tenant_db_auth_failed",
                    "message": "The database credentials stored for this API token were rejected by Postgres.",
                }
            },
        )
    return JSONResponse(
        status_code=503,
        content={
            "error": {
                "code": "tenant_db_unavailable",
                "message": "The tenant database is currently unavailable.",
            }
        },
    )
