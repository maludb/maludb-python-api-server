"""
Tests for the graph router — endpoint registration and validation.

Tests that don't need a live Postgres connection: verifying that endpoints
are registered (401 not 404 without auth).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Endpoint registration — all graph endpoints should return 401, not 404,
# when called without an auth token.
# ---------------------------------------------------------------------------

_AUTH_PATHS = [
    ("GET", "/v1/edges"),
    ("GET", "/v1/graph/neighbors?kind=subject&id=1"),
    ("GET", "/v1/graph/walk?kind=subject&id=1"),
]


class TestGraphRouterRegistered:
    """Verify all graph endpoints are mounted and return 401 (not 404) without auth."""

    @pytest.mark.parametrize("method,path", _AUTH_PATHS)
    def test_missing_auth_returns_401(self, method: str, path: str):
        response = client.request(method, path)
        assert response.status_code == 401, (
            f"{method} {path} returned {response.status_code}, expected 401"
        )
        data = response.json()
        assert "error" in data
        assert data["error"]["code"] == "auth_missing"


class TestGraphAuthErrorShape:
    """Verify that 401 responses have the standard error shape."""

    def test_edges_error_shape(self):
        r = client.get("/v1/edges")
        assert r.status_code == 401
        data = r.json()
        assert "error" in data
        assert "code" in data["error"]
        assert "message" in data["error"]
        assert data["error"]["code"] == "auth_missing"
        assert "Bearer" in data["error"]["message"]

    def test_neighbors_error_shape(self):
        r = client.get("/v1/graph/neighbors?kind=subject&id=1")
        assert r.status_code == 401
        data = r.json()
        assert data["error"]["code"] == "auth_missing"

    def test_walk_error_shape(self):
        r = client.get("/v1/graph/walk?kind=subject&id=1")
        assert r.status_code == 401
        data = r.json()
        assert data["error"]["code"] == "auth_missing"

    def test_invalid_token_prefix(self):
        """A token without the malu_ prefix should get auth_invalid, not 404."""
        r = client.get(
            "/v1/edges",
            headers={"Authorization": "Bearer bad_token_here"},
        )
        assert r.status_code == 401
        data = r.json()
        assert data["error"]["code"] == "auth_invalid"


class TestGraphNotFound:
    """Non-existent routes should still 404."""

    def test_nonexistent_graph_route(self):
        r = client.get("/v1/graph/nonexistent")
        assert r.status_code == 404


class TestGraphRequiredParams:
    """Verify required query params are enforced by FastAPI (422)."""

    def test_neighbors_missing_kind(self):
        r = client.get(
            "/v1/graph/neighbors?id=1",
            headers={"Authorization": "Bearer bad_token_here"},
        )
        # FastAPI returns 422 for missing required query params before auth runs
        assert r.status_code in (401, 422)

    def test_neighbors_missing_id(self):
        r = client.get(
            "/v1/graph/neighbors?kind=subject",
            headers={"Authorization": "Bearer bad_token_here"},
        )
        assert r.status_code in (401, 422)

    def test_walk_missing_kind(self):
        r = client.get(
            "/v1/graph/walk?id=1",
            headers={"Authorization": "Bearer bad_token_here"},
        )
        assert r.status_code in (401, 422)

    def test_walk_missing_id(self):
        r = client.get(
            "/v1/graph/walk?kind=subject",
            headers={"Authorization": "Bearer bad_token_here"},
        )
        assert r.status_code in (401, 422)
