"""
End-to-end tests for the generic user-table API against a REAL tenant database.

Skipped unless both env vars are set:

    MALUDB_E2E_TOKEN  a malu_ API token resolvable by the local auth store
    MALUDB_E2E_DSN    a psycopg DSN for the SAME tenant DB/user the token maps
                      to (used only to CREATE/DROP the scratch table)

Example (local dev):

    MALUDB_E2E_TOKEN=malu_… \
    MALUDB_E2E_DSN='host=127.0.0.1 dbname=maludb user=app password=…' \
    pytest tests/test_rest_e2e.py -v

The suite creates a scratch table in the tenant schema, exercises both mounts
through the full app (real auth resolution, real Postgres), and drops it.
"""

from __future__ import annotations

import os

import pytest

TOKEN = os.environ.get("MALUDB_E2E_TOKEN")
DSN = os.environ.get("MALUDB_E2E_DSN")

pytestmark = pytest.mark.skipif(
    not (TOKEN and DSN),
    reason="e2e disabled — set MALUDB_E2E_TOKEN and MALUDB_E2E_DSN",
)

TABLE = "maludb_e2e_scratch_todos".replace("maludb_", "e2e_")  # never a reserved prefix


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        yield c


@pytest.fixture(scope="module", autouse=True)
def scratch_table():
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'DROP TABLE IF EXISTS "{TABLE}"')
        conn.execute(
            f'CREATE TABLE "{TABLE}" ('
            "  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,"
            "  title text NOT NULL,"
            "  done boolean NOT NULL DEFAULT false,"
            "  meta jsonb,"
            "  UNIQUE (title))"
        )
    yield
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'DROP TABLE IF EXISTS "{TABLE}"')


REPR = {"Prefer": "return=representation"}


class TestRestFlavorE2E:
    def test_insert_bulk_representation(self, client):
        r = client.post(
            f"/rest/v1/{TABLE}",
            json=[
                {"title": "walk dog", "meta": {"tags": ["pets"]}},
                {"title": "write docs", "done": True},
                {"title": "ship it"},
            ],
            headers=REPR,
        )
        assert r.status_code == 201, r.text
        rows = r.json()
        assert [row["title"] for row in rows] == ["walk dog", "write docs", "ship it"]
        assert rows[0]["meta"] == {"tags": ["pets"]}

    def test_filter_select_order(self, client):
        r = client.get(f"/rest/v1/{TABLE}?done=is.false&select=title&order=title.desc")
        assert r.status_code == 200
        assert r.json() == [{"title": "walk dog"}, {"title": "ship it"}]

    def test_count_and_content_range(self, client):
        r = client.get(f"/rest/v1/{TABLE}?limit=1", headers={"Prefer": "count=exact"})
        assert r.status_code == 200
        assert r.headers["content-range"] == "0-0/3"

    def test_single_object_accept(self, client):
        headers = {"Accept": "application/vnd.pgrst.object+json"}
        r = client.get(f"/rest/v1/{TABLE}?title=eq.ship it", headers=headers)
        assert r.status_code == 200
        assert r.json()["title"] == "ship it"

        r = client.get(f"/rest/v1/{TABLE}", headers=headers)  # 3 rows → 406
        assert r.status_code == 406
        assert r.json()["code"] == "PGRST116"

    def test_upsert_merge(self, client):
        r = client.post(
            f"/rest/v1/{TABLE}?on_conflict=title",
            json={"title": "ship it", "done": True},
            headers={"Prefer": "return=representation, resolution=merge-duplicates"},
        )
        assert r.status_code == 201, r.text
        assert r.json()[0]["done"] is True

    def test_patch_filtered(self, client):
        r = client.patch(f"/rest/v1/{TABLE}?title=eq.walk dog", json={"done": True}, headers=REPR)
        assert r.status_code == 200
        assert r.json()[0]["done"] is True

    def test_patch_minimal_is_204(self, client):
        r = client.patch(f"/rest/v1/{TABLE}?title=eq.walk dog", json={"done": True})
        assert r.status_code == 204
        assert r.content == b""

    def test_unknown_filter_key_rejected(self, client):
        r = client.delete(f"/rest/v1/{TABLE}?idd=eq.1")
        assert r.status_code == 400
        assert r.json()["code"] == "PGRST204"

    def test_db_error_shape(self, client):
        r = client.post(f"/rest/v1/{TABLE}", json={"title": "ship it"})  # unique violation
        assert r.status_code == 409
        body = r.json()
        assert body["code"] == "23505"
        assert "duplicate key" in body["message"]

    def test_memory_facades_not_served(self, client):
        r = client.get("/rest/v1/maludb_subject")
        assert r.status_code == 404
        assert r.json()["code"] == "PGRST205"

    def test_delete_filtered(self, client):
        r = client.delete(f"/rest/v1/{TABLE}?title=eq.write docs", headers=REPR)
        assert r.status_code == 200
        assert r.json()[0]["title"] == "write docs"


class TestTablesFlavorE2E:
    def test_envelopes_and_house_errors(self, client):
        r = client.get(f"/v1/tables/{TABLE}?select=title&order=title")
        assert r.status_code == 200
        assert "rows" in r.json()

        r = client.post(f"/v1/tables/{TABLE}", json={"title": "house row"})
        assert r.status_code == 201
        assert r.json() == {"inserted": 1}

        r = client.patch(f"/v1/tables/{TABLE}?title=eq.house row", json={"done": True})
        assert r.json() == {"updated": 1}

        r = client.delete(f"/v1/tables/{TABLE}?title=eq.house row")
        assert r.json() == {"deleted": 1}

        r = client.get("/v1/tables/nope")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "table_not_found"
