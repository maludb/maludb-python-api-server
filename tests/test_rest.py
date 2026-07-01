"""
Tests for the generic user-table router (app/routers/rest.py) and its
reflection helper — registration, strict key checking, and the SQL builders
(no real Postgres DB; the write builders are pure functions).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from starlette.datastructures import QueryParams

from app.errors import APIError
from app.helpers.query import Col, QuerySpec, parse_query
from app.helpers.reflect import REST_DEFAULT_LIMIT, REST_MAX_LIMIT, TableInfo, quote_ident
from app.main import app
from app.routers.rest import (
    _check_insert_keys,
    _check_known_keys,
    _split_columns,
    build_delete_sql,
    build_insert_sql,
    build_update_sql,
)

client = TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Fixture — a fake reflected table (what resolve_table would return)
# ---------------------------------------------------------------------------


def make_table(name: str = "todos", pk: list[str] | None = None) -> TableInfo:
    columns = {"id": int, "title": str, "done": bool, "meta": str, "tags": str}
    data_types = {"id": "bigint", "title": "text", "done": "boolean", "meta": "jsonb", "tags": "ARRAY"}
    spec = QuerySpec(
        columns={n: Col(quote_ident(n), t) for n, t in columns.items()},
        default_order=[],
        default_limit=REST_DEFAULT_LIMIT,
        max_limit=REST_MAX_LIMIT,
        clamp_limit=True,
    )
    return TableInfo(
        name=name,
        ident=quote_ident(name),
        columns=columns,
        data_types=data_types,
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
        assert qp.selected == ["id", "title", "done", "meta", "tags"]

    def test_star_mixed_with_alias(self):
        ti = make_table()
        qp = parse_query(QueryParams("select=*,name:title"), ti.spec)
        assert qp.selected == ["id", "title", "done", "meta", "tags", "name"]

    def test_aliases_are_quoted(self):
        """Reflected column names can be mixed-case or contain spaces — the SQL
        alias must be quoted or Postgres case-folds / errors."""
        spec = QuerySpec(columns={"userId": Col('"userId"', int), "user id": Col('"user id"', str)})
        qp = parse_query(QueryParams("select=*"), spec)
        assert qp.select_list == '"userId" AS "userId", "user id" AS "user id"'
        qp = parse_query(QueryParams(""), spec)  # default select
        assert qp.select_list == '"userId" AS "userId", "user id" AS "user id"'


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

    def test_windowed_delete_applies_order_and_limit(self):
        """?limit= on DELETE must window via ctid — never silently over-delete."""
        ti = make_table()
        qp = parsed("done=is.true&order=id&limit=1", ti)
        sql, params = build_delete_sql(ti, qp, None, window=(True, False))
        assert sql == (
            'DELETE FROM "todos" WHERE ctid IN '
            '(SELECT ctid FROM "todos" WHERE "done" IS TRUE ORDER BY "id" ASC LIMIT %s)'
        )
        assert params == [1]

    def test_windowed_update_param_order(self):
        ti = make_table()
        qp = parsed("id=gte.10&limit=2&offset=1", ti)
        sql, params = build_update_sql(ti, {"done": True}, qp, None, window=(True, True))
        assert sql == (
            'UPDATE "todos" SET "done" = %s WHERE ctid IN '
            '(SELECT ctid FROM "todos" WHERE "id" >= %s  LIMIT %s OFFSET %s)'
        )
        assert params == [True, 10, 2, 1]

    def test_offset_only_window_has_no_limit(self):
        """?offset= without ?limit= must NOT drag the default LIMIT 1000 into
        the write — that would silently cap a bulk delete."""
        ti = make_table()
        qp = parsed("done=is.true&offset=5", ti)
        sql, params = build_delete_sql(ti, qp, None, window=(False, True))
        assert "LIMIT" not in sql
        assert sql.endswith("OFFSET %s)")
        assert params == [5]


# ---------------------------------------------------------------------------
# Value adaptation + insert-key strictness
# ---------------------------------------------------------------------------


class TestAdaptAndInsertKeys:
    def test_list_into_array_column_stays_native(self):
        """A Python list bound to an ARRAY column must use psycopg's native
        list→array adaptation, not a Jsonb wrapper."""
        ti = make_table()
        _, params = build_insert_sql(ti, [{"tags": ["a", "b"]}], None, None, None, None)
        assert params == [["a", "b"]]

    def test_list_into_jsonb_column_wrapped(self):
        ti = make_table()
        _, params = build_insert_sql(ti, [{"meta": [1, 2]}], None, None, None, None)
        assert isinstance(params[0], Jsonb)

    def test_dict_always_wrapped(self):
        ti = make_table()
        _, params = build_insert_sql(ti, [{"meta": {"k": 1}}], None, None, None, None)
        assert isinstance(params[0], Jsonb)

    def test_insert_rejects_filter_params(self):
        with pytest.raises(APIError) as exc:
            _check_insert_keys(QueryParams("id=eq.5"))
        assert exc.value.status == 400

    def test_insert_rejects_limit(self):
        with pytest.raises(APIError):
            _check_insert_keys(QueryParams("limit=5"))

    def test_insert_allows_its_keys(self):
        _check_insert_keys(QueryParams("select=id&columns=title&on_conflict=id&debug=1"))  # no raise

    def test_filter_shaped_debug_rejected(self):
        """?debug=eq.true must not be silently dropped (it would unfilter a
        write on a table with a `debug` column) — reject and point at and=()."""
        ti = make_table()
        with pytest.raises(APIError) as exc:
            _check_known_keys(QueryParams("debug=eq.true"), ti.spec)
        assert exc.value.status == 400
        with pytest.raises(APIError):
            _check_insert_keys(QueryParams("debug=eq.true"))
        _check_known_keys(QueryParams("debug=1"), ti.spec)  # trace flag still fine


class TestSplitColumns:
    def test_plain_and_quoted(self):
        assert _split_columns("a,b") == ["a", "b"]
        assert _split_columns('"sku","name"') == ["sku", "name"]

    def test_quoted_name_containing_comma(self):
        assert _split_columns('"a,b","title"') == ["a,b", "title"]

    def test_doubled_quotes_unescaped(self):
        assert _split_columns('"we""ird"') == ['we"ird']


class TestLimitClamp:
    def test_over_max_clamps_when_flagged(self):
        ti = make_table()  # reflect specs set clamp_limit=True
        qp = parse_query(QueryParams("limit=2000"), ti.spec)
        assert qp.limit == REST_MAX_LIMIT

    def test_over_max_still_422_without_flag(self):
        spec = QuerySpec(columns={"id": Col('"id"', int)}, max_limit=10)
        with pytest.raises(APIError) as exc:
            parse_query(QueryParams("limit=11"), spec)
        assert exc.value.status == 422


class TestHeterogeneousUpsert:
    def test_merge_upsert_requires_matching_keys(self):
        """A merge upsert with heterogeneous items would overwrite unmentioned
        columns with DEFAULT — reject like PostgREST does."""
        ti = make_table()
        items = [{"id": 1, "title": "a"}, {"id": 2, "done": True}]
        with pytest.raises(APIError) as exc:
            build_insert_sql(ti, items, None, None, "merge-duplicates", None)
        assert exc.value.status == 400
        # plain insert (no resolution) keeps the DEFAULT-fill behavior
        sql, _ = build_insert_sql(ti, items, None, None, None, None)
        assert "DEFAULT" in sql
        # ignore-duplicates never updates, so heterogeneous is safe too
        sql, _ = build_insert_sql(ti, items, None, None, "ignore-duplicates", None)
        assert sql.endswith("DO NOTHING")
