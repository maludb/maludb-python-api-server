"""
Tests for the generic user-table router (app/routers/rest.py) and its
reflection helper — registration, strict key checking, and the SQL builders
(no real Postgres DB; the write builders are pure functions).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import QueryParams

from app.errors import APIError
from app.helpers.query import Col, QuerySpec, parse_query
from app.helpers.reflect import REST_DEFAULT_LIMIT, REST_MAX_LIMIT, TableInfo, quote_ident
from app.main import app
from app.routers.rest import (
    _check_known_keys,
    build_delete_sql,
    build_insert_sql,
    build_update_sql,
)

client = TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Fixture — a fake reflected table (what resolve_table would return)
# ---------------------------------------------------------------------------


def make_table(name: str = "todos", pk: list[str] | None = None) -> TableInfo:
    columns = {"id": int, "title": str, "done": bool, "meta": str}
    spec = QuerySpec(
        columns={n: Col(quote_ident(n), t) for n, t in columns.items()},
        default_order=[],
        default_limit=REST_DEFAULT_LIMIT,
        max_limit=REST_MAX_LIMIT,
    )
    return TableInfo(
        name=name,
        ident=quote_ident(name),
        columns=columns,
        pk=pk if pk is not None else ["id"],
        spec=spec,
    )


# ---------------------------------------------------------------------------
# Registration — both mounts return 401 (not 404) without a token
# ---------------------------------------------------------------------------

_AUTH_PATHS = [
    ("GET", "/rest/v1/some_table"),
    ("POST", "/rest/v1/some_table"),
    ("PATCH", "/rest/v1/some_table"),
    ("DELETE", "/rest/v1/some_table"),
    ("GET", "/v1/tables/some_table"),
    ("POST", "/v1/tables/some_table"),
    ("PATCH", "/v1/tables/some_table"),
    ("DELETE", "/v1/tables/some_table"),
]


class TestRestRouterRegistered:
    @pytest.mark.parametrize("method,path", _AUTH_PATHS)
    def test_missing_auth_returns_401(self, method: str, path: str):
        response = client.request(method, path)
        assert response.status_code == 401, f"{method} {path} returned {response.status_code}, expected 401"
        assert response.json()["error"]["code"] == "auth_missing"

    def test_head_is_routed(self):
        assert client.head("/rest/v1/some_table").status_code == 401


# ---------------------------------------------------------------------------
# Strict key checking
# ---------------------------------------------------------------------------


class TestStrictKeys:
    def test_grammar_and_column_keys_pass(self):
        ti = make_table()
        params = QueryParams("select=*&order=id.desc&limit=5&offset=0&or=(id.eq.1)&debug=1&title=eq.x")
        _check_known_keys(params, ti.spec)  # no raise

    def test_unknown_key_rejected(self):
        ti = make_table()
        with pytest.raises(APIError) as exc:
            _check_known_keys(QueryParams("idd=eq.5"), ti.spec)
        assert exc.value.status == 400
        assert exc.value.code == "unknown_column"

    def test_extra_allowlist(self):
        ti = make_table()
        params = QueryParams("columns=title&on_conflict=id")
        with pytest.raises(APIError):
            _check_known_keys(params, ti.spec)
        _check_known_keys(params, ti.spec, frozenset({"columns", "on_conflict"}))  # no raise


# ---------------------------------------------------------------------------
# select=* wildcard (shared parser)
# ---------------------------------------------------------------------------


class TestSelectStar:
    def test_star_expands_all_columns(self):
        ti = make_table()
        qp = parse_query(QueryParams("select=*"), ti.spec)
        assert qp.selected == ["id", "title", "done", "meta"]

    def test_star_mixed_with_alias(self):
        ti = make_table()
        qp = parse_query(QueryParams("select=*,name:title"), ti.spec)
        assert qp.selected == ["id", "title", "done", "meta", "name"]


# ---------------------------------------------------------------------------
# INSERT builder
# ---------------------------------------------------------------------------


class TestBuildInsert:
    def test_single_row(self):
        sql, params = build_insert_sql(make_table(), [{"title": "a", "done": True}], None, None, None, None)
        assert sql == 'INSERT INTO "todos" ("title", "done") VALUES (%s, %s)'
        assert params == ["a", True]

    def test_bulk_union_of_keys_fills_default(self):
        items = [{"title": "a"}, {"title": "b", "done": True}]
        sql, params = build_insert_sql(make_table(), items, None, None, None, None)
        assert sql == 'INSERT INTO "todos" ("title", "done") VALUES (%s, DEFAULT), (%s, %s)'
        assert params == ["a", "b", True]

    def test_columns_param_restricts(self):
        items = [{"title": "a", "done": True}]
        sql, params = build_insert_sql(make_table(), items, "title", None, None, None)
        assert sql == 'INSERT INTO "todos" ("title") VALUES (%s)'
        assert params == ["a"]

    def test_unknown_column_rejected(self):
        with pytest.raises(APIError) as exc:
            build_insert_sql(make_table(), [{"nope": 1}], None, None, None, None)
        assert exc.value.status == 400
        assert exc.value.code == "unknown_column"

    def test_upsert_merge_defaults_to_pk(self):
        items = [{"id": 1, "title": "a"}]
        sql, _ = build_insert_sql(make_table(), items, None, None, "merge-duplicates", None)
        assert 'ON CONFLICT ("id") DO UPDATE SET "title" = EXCLUDED."title"' in sql

    def test_upsert_ignore_duplicates(self):
        items = [{"id": 1, "title": "a"}]
        sql, _ = build_insert_sql(make_table(), items, None, None, "ignore-duplicates", None)
        assert sql.endswith('ON CONFLICT ("id") DO NOTHING')

    def test_upsert_explicit_on_conflict(self):
        items = [{"title": "a", "done": False}]
        sql, _ = build_insert_sql(make_table(), items, None, "title", "merge-duplicates", None)
        assert 'ON CONFLICT ("title") DO UPDATE SET "done" = EXCLUDED."done"' in sql

    def test_upsert_without_pk_or_target_rejected(self):
        with pytest.raises(APIError) as exc:
            build_insert_sql(make_table(pk=[]), [{"title": "a"}], None, None, "merge-duplicates", None)
        assert exc.value.status == 400

    def test_upsert_all_cols_in_target_degrades_to_nothing(self):
        sql, _ = build_insert_sql(make_table(), [{"id": 1}], None, None, "merge-duplicates", None)
        assert sql.endswith('ON CONFLICT ("id") DO NOTHING')

    def test_default_values_single_empty_object(self):
        sql, params = build_insert_sql(make_table(), [{}], None, None, None, None)
        assert sql == 'INSERT INTO "todos" DEFAULT VALUES'
        assert params == []

    def test_bulk_empty_objects_rejected(self):
        with pytest.raises(APIError):
            build_insert_sql(make_table(), [{}, {}], None, None, None, None)

    def test_returning_appended(self):
        sql, _ = build_insert_sql(make_table(), [{"title": "a"}], None, None, None, '"id" AS id')
        assert sql.endswith('RETURNING "id" AS id')


# ---------------------------------------------------------------------------
# UPDATE / DELETE builders
# ---------------------------------------------------------------------------


def parsed(query: str, ti: TableInfo):
    return parse_query(QueryParams(query), ti.spec)


class TestBuildUpdateDelete:
    def test_update_with_filter(self):
        ti = make_table()
        qp = parsed("id=eq.5", ti)
        sql, params = build_update_sql(ti, {"done": True}, qp, None)
        assert sql == 'UPDATE "todos" SET "done" = %s WHERE "id" = %s'
        assert params == [True, 5]

    def test_update_empty_body_rejected(self):
        ti = make_table()
        with pytest.raises(APIError) as exc:
            build_update_sql(ti, {}, parsed("id=eq.5", ti), None)
        assert exc.value.status == 400

    def test_update_unknown_column_rejected(self):
        ti = make_table()
        with pytest.raises(APIError):
            build_update_sql(ti, {"nope": 1}, parsed("id=eq.5", ti), None)

    def test_delete_with_filter_and_returning(self):
        ti = make_table()
        qp = parsed("done=is.true", ti)
        sql, params = build_delete_sql(ti, qp, '"id" AS id')
        assert sql == 'DELETE FROM "todos" WHERE "done" IS TRUE RETURNING "id" AS id'
        assert params == []

    def test_delete_unfiltered_allowed(self):
        ti = make_table()
        sql, params = build_delete_sql(ti, parsed("", ti), None)
        assert sql == 'DELETE FROM "todos" '
        assert params == []
