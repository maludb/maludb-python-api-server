"""
PostgREST-compatible query parsing for list endpoints.

Supabase exposes every table through PostgREST's query grammar (``?col=eq.x``,
``?select=…``, ``?order=…``, ``limit``/``offset``).  This helper brings the same
ergonomics to our hand-written routers **without** abandoning the SQL-traceability
principle: the router keeps writing the literal ``SELECT … FROM …`` text, and this
module only builds the *column projection*, the ``WHERE`` clause, the ``ORDER BY``,
and ``LIMIT``/``OFFSET`` — each constrained to a per-router allowlist (``QuerySpec``)
so **client input never becomes a SQL identifier**.  Values are always bound as
``%s`` placeholders; only operators and (allowlisted) column expressions are
spliced into the SQL text.

Supported grammar (a pragmatic subset of PostgREST):

    filtering   ?col=op.value         op ∈ eq neq gt gte lt lte
                                          like ilike match imatch in is
                                          fts plfts phfts wfts
                negation              ?col=not.op.value
                repeated (AND)        ?age=gte.18&age=lte.65
                OR groups             ?or=(col.op.value,col.op.value)
    selection   ?select=col,alias:col
    ordering    ?order=col[.asc|.desc][.nullsfirst|.nullslast],...
    pagination  ?limit=N&offset=M

Not yet supported (raise a clear 400 ``bad_request``): array/range operators
(cs/cd/ov), JSON-path access, quoted values inside ``in.()``/``or()``, and nested
``and``/``or`` groups.

Malformed values, unknown columns, and unknown operators raise
``APIError("bad_request", …, 400)`` so the failure matches the standard JSON error
shape instead of leaking a Postgres error.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from app.errors import APIError

# ---------------------------------------------------------------------------
# Spec types — each router declares one of these (DB expression ↔ API field)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Col:
    """One allowlisted, queryable column.

    ``expr`` is the raw SQL expression (e.g. ``"s.subject_id"`` or a correlated
    sub-select) spliced verbatim into the SQL — it is router-authored, never
    client input.  ``type`` drives value coercion for filters (``int``/``float``/
    ``bool``/``str``) so e.g. ``id=eq.abc`` fails with a clean 400.
    """

    expr: str
    type: type = str


@dataclass(frozen=True)
class QuerySpec:
    """The per-resource allowlist + defaults a router passes to ``parse_query``."""

    columns: Mapping[str, Col]
    default_order: list[tuple[str, str]] = field(default_factory=list)
    default_select: list[str] | None = None  # None → all columns, in declared order
    default_limit: int = 50
    max_limit: int = 200


@dataclass(frozen=True)
class ParsedQuery:
    """Assembled SQL fragments + bound params for a list query.

    The router splices these into its literal statement::

        SELECT {select_list} FROM … {where_sql} {order_sql} {limit_sql}

    passing ``where_params + limit_params`` (in that order) as the parameters.
    """

    select_list: str
    selected: list[str]
    where_sql: str
    where_clause: str  # conditions joined by AND, WITHOUT the "WHERE " prefix
    where_params: list
    order_sql: str
    limit_sql: str
    limit_params: list
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Operator tables
# ---------------------------------------------------------------------------

_SIMPLE_OPS: dict[str, str] = {
    "eq": "=",
    "neq": "<>",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
    "like": "LIKE",
    "ilike": "ILIKE",
    "match": "~",
    "imatch": "~*",
}

# Full-text-search operators → the tsquery constructor they map to.
_FTS_OPS: dict[str, str] = {
    "fts": "to_tsquery",
    "plfts": "plainto_tsquery",
    "phfts": "phraseto_tsquery",
    "wfts": "websearch_to_tsquery",
}

_IS_VALUES = {"null", "true", "false", "unknown"}

# Operators known to the grammar. A value that does NOT begin with one of these
# (as ``op.value``) is treated as an implicit ``eq`` exact match — so bare legacy
# params (``?type=note``) and dotted literals (timestamps) still work.
_KNOWN_OPS = frozenset(_SIMPLE_OPS) | frozenset(_FTS_OPS) | {"in", "is"}
# Operators that only make sense on a text column (pattern / regex / full-text).
_TEXT_OPS = frozenset({"like", "ilike", "match", "imatch"}) | frozenset(_FTS_OPS)

_RESERVED_KEYS = frozenset({"select", "order", "limit", "offset", "or", "and"})

_LANG_RE = re.compile(r"^([a-z]+)\(([a-z_]+)\)$")
_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _bad(msg: str) -> APIError:
    return APIError("bad_request", msg, 400)


def _invalid(msg: str) -> APIError:
    """422 for well-formed grammar whose value is out of the allowed range/type
    (limit/offset) — matches the status the prior FastAPI ``Query`` validation used."""
    return APIError("validation_failed", msg, 422)


# ---------------------------------------------------------------------------
# Value coercion
# ---------------------------------------------------------------------------


def _coerce(api_name: str, col: Col, value: str):
    """Coerce a raw string to the column's Python type (for cleaner errors)."""
    t = col.type
    try:
        if t is int:
            return int(value)
        if t is float:
            return float(value)
        if t is bool:
            lv = value.lower()
            if lv in ("true", "t", "1"):
                return True
            if lv in ("false", "f", "0"):
                return False
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise _bad(f"Invalid value '{value}' for column '{api_name}'.") from exc
    return value


# ---------------------------------------------------------------------------
# Filter condition builder
# ---------------------------------------------------------------------------


def _parse_op(raw: str) -> tuple[bool, str, str | None, str]:
    """Split a raw filter value into ``(negate, op, lang, value)``.

    Recognized forms: ``op.value``, ``op(lang).value``, and ``not.<op>…``. A value
    that does NOT begin with a known operator token is treated as an implicit
    ``eq`` (exact match) over the whole string — so bare legacy params
    (``?type=note``) and dotted literals (e.g. timestamps) work unambiguously.
    """
    negate = False
    body = raw
    if body.startswith("not."):
        candidate = body[4:]
        tok = candidate.partition(".")[0]
        m = _LANG_RE.match(tok)
        base = m.group(1) if m else tok
        if base in _KNOWN_OPS:
            negate = True
            body = candidate

    op_tok, dot, value = body.partition(".")
    m = _LANG_RE.match(op_tok)
    base = m.group(1) if m else op_tok
    if dot and base in _KNOWN_OPS:
        lang = m.group(2) if m else None
        return negate, base, lang, value

    # No recognized operator prefix → implicit eq over the entire value.
    return negate, "eq", None, body


def _build_condition(api_name: str, col: Col, raw: str) -> tuple[str, list]:
    """Build one ``WHERE`` fragment + its params for ``col`` and a raw value."""
    negate, op, lang, value = _parse_op(raw)

    if op in _TEXT_OPS and col.type is not str:
        raise _bad(f"Operator '{op}' requires a text column, but '{api_name}' is not text.")

    if op in _SIMPLE_OPS:
        if op in ("like", "ilike"):
            params: list = [value.replace("*", "%")]
        else:
            params = [_coerce(api_name, col, value)]
        frag = f"{col.expr} {_SIMPLE_OPS[op]} %s"

    elif op == "in":
        inner = value.strip()
        if inner.startswith("(") and inner.endswith(")"):
            inner = inner[1:-1]
        items = [x.strip() for x in inner.split(",") if x.strip() != ""]
        if not items:
            raise _bad(f"Empty 'in' list for column '{api_name}'.")
        params = [_coerce(api_name, col, x) for x in items]
        placeholders = ", ".join(["%s"] * len(params))
        frag = f"{col.expr} IN ({placeholders})"

    elif op == "is":
        kw = value.lower()
        if kw not in _IS_VALUES:
            raise _bad(f"'is' filter for '{api_name}' must be one of {sorted(_IS_VALUES)}.")
        params = []
        frag = f"{col.expr} IS {kw.upper()}"

    elif op in _FTS_OPS:
        fn = _FTS_OPS[op]
        if lang:
            params = [lang, lang, value]
            frag = f"to_tsvector(%s, {col.expr}) @@ {fn}(%s, %s)"
        else:
            params = [value]
            frag = f"to_tsvector({col.expr}) @@ {fn}(%s)"

    else:
        raise _bad(f"Unknown operator '{op}' for column '{api_name}'.")

    if negate:
        frag = f"NOT ({frag})"
    return frag, params


def _split_top(s: str) -> list[str]:
    """Split on top-level commas, respecting parentheses (for ``or`` groups)."""
    parts: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in s:
        if ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


def _build_bool_group(raw: str, spec: QuerySpec, kw: str) -> tuple[str, list]:
    """Build a parenthesised ``(a <kw> b …)`` fragment from ``or=(…)`` / ``and=(…)``."""
    label = kw.lower()
    s = raw.strip()
    if s.startswith("(") and s.endswith(")"):
        s = s[1:-1]
    conds = _split_top(s)
    parts: list[str] = []
    params: list = []
    for cond in conds:
        name, dot, rest = cond.strip().partition(".")
        if not dot:
            raise _bad(f"Malformed '{label}' condition '{cond}'.")
        col = spec.columns.get(name)
        if col is None:
            raise _bad(f"Unknown column '{name}' in '{label}' group.")
        frag, p = _build_condition(name, col, rest)
        parts.append(frag)
        params.extend(p)
    if not parts:
        raise _bad(f"Empty '{label}' group.")
    return "(" + f" {kw} ".join(parts) + ")", params


def _build_or_group(raw: str, spec: QuerySpec) -> tuple[str, list]:
    return _build_bool_group(raw, spec, "OR")


def _build_and_group(raw: str, spec: QuerySpec) -> tuple[str, list]:
    return _build_bool_group(raw, spec, "AND")


# ---------------------------------------------------------------------------
# select / order / pagination builders
# ---------------------------------------------------------------------------


def _build_select(value: str, spec: QuerySpec) -> tuple[str, list[str]]:
    pieces: list[str] = []
    selected: list[str] = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            alias, name = item.split(":", 1)
        else:
            alias = name = item
        if not _IDENT_RE.match(alias):
            raise _bad(f"Invalid column alias '{alias}'.")
        col = spec.columns.get(name)
        if col is None:
            raise _bad(f"Unknown column '{name}' in select.")
        pieces.append(f"{col.expr} AS {alias}")
        selected.append(alias)
    if not pieces:
        raise _bad("Empty select list.")
    return ", ".join(pieces), selected


def _default_select(spec: QuerySpec) -> tuple[str, list[str]]:
    names = spec.default_select if spec.default_select is not None else list(spec.columns.keys())
    pieces = [f"{spec.columns[n].expr} AS {n}" for n in names]
    return ", ".join(pieces), list(names)


def _build_order(value: str, spec: QuerySpec) -> str:
    terms: list[str] = []
    for term in value.split(","):
        term = term.strip()
        if not term:
            continue
        parts = term.split(".")
        name = parts[0]
        col = spec.columns.get(name)
        if col is None:
            raise _bad(f"Unknown column '{name}' in order.")
        direction = "ASC"
        nulls = ""
        for mod in parts[1:]:
            ml = mod.lower()
            if ml == "asc":
                direction = "ASC"
            elif ml == "desc":
                direction = "DESC"
            elif ml == "nullsfirst":
                nulls = " NULLS FIRST"
            elif ml == "nullslast":
                nulls = " NULLS LAST"
            else:
                raise _bad(f"Invalid order modifier '{mod}' for '{name}'.")
        terms.append(f"{col.expr} {direction}{nulls}")
    if not terms:
        raise _bad("Empty order clause.")
    return "ORDER BY " + ", ".join(terms)


def _parse_int(value: str | None, default: int, name: str) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (ValueError, TypeError) as exc:
        raise _invalid(f"'{name}' must be an integer.") from exc


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse_query(query_params, spec: QuerySpec, *, reserved: tuple[str, ...] = ()) -> ParsedQuery:
    """Parse PostgREST-style query params into SQL fragments against ``spec``.

    ``query_params`` is a Starlette ``QueryParams`` (``request.query_params``).
    ``reserved`` lists param keys the *caller* consumes itself (e.g. a legacy
    ``q`` shortcut) so they are not mistaken for column filters.
    """
    ignored = _RESERVED_KEYS | set(reserved)

    where_parts: list[str] = []
    where_params: list = []

    for key, raw in query_params.multi_items():
        if key in ignored:
            continue
        col = spec.columns.get(key)
        if col is None:
            # Unknown keys are ignored, not rejected — matches the prior lenient
            # contract (e.g. the ?debug=1 SQL trace, cache-busters, tracking params).
            continue
        frag, p = _build_condition(key, col, raw)
        where_parts.append(frag)
        where_params.extend(p)

    for raw in query_params.getlist("or"):
        frag, p = _build_or_group(raw, spec)
        where_parts.append(frag)
        where_params.extend(p)

    for raw in query_params.getlist("and"):
        frag, p = _build_and_group(raw, spec)
        where_parts.append(frag)
        where_params.extend(p)

    where_clause = " AND ".join(where_parts)
    where_sql = ("WHERE " + where_clause) if where_parts else ""

    sel = query_params.get("select")
    if sel:
        select_list, selected = _build_select(sel, spec)
    else:
        select_list, selected = _default_select(spec)

    ordv = query_params.get("order")
    if ordv:
        order_sql = _build_order(ordv, spec)
    elif spec.default_order:
        terms = [f"{spec.columns[n].expr} {d.upper()}" for n, d in spec.default_order]
        order_sql = "ORDER BY " + ", ".join(terms)
    else:
        order_sql = ""

    limit = _parse_int(query_params.get("limit"), spec.default_limit, "limit")
    if limit < 0:
        raise _invalid("'limit' must be >= 0.")
    if limit > spec.max_limit:
        raise _invalid(f"'limit' must be <= {spec.max_limit}.")

    offset = _parse_int(query_params.get("offset"), 0, "offset")
    if offset < 0:
        raise _invalid("'offset' must be >= 0.")

    limit_sql = "LIMIT %s"
    limit_params: list = [limit]
    if offset:
        limit_sql += " OFFSET %s"
        limit_params.append(offset)

    return ParsedQuery(
        select_list=select_list,
        selected=selected,
        where_sql=where_sql,
        where_clause=where_clause,
        where_params=where_params,
        order_sql=order_sql,
        limit_sql=limit_sql,
        limit_params=limit_params,
        limit=limit,
        offset=offset,
    )


def build_where(*clauses: str) -> str:
    """Join non-empty condition fragments into a ``WHERE …`` clause (or "").

    Lets a handler merge the parser's ``where_clause`` with its own base/legacy
    conditions while keeping param order under the handler's control::

        where_sql = build_where(BASE, qp.where_clause, legacy_clause)
        params = qp.where_params + legacy_params  # same order as the clauses
    """
    real = [c for c in clauses if c]
    return ("WHERE " + " AND ".join(real)) if real else ""


# ---------------------------------------------------------------------------
# Counting + Content-Range (PostgREST-style pagination metadata)
# ---------------------------------------------------------------------------

_COUNT_RE = re.compile(r"count=(exact|planned|estimated)")


def wants_count(request) -> str | None:
    """Return the requested count strategy from a ``Prefer: count=…`` header.

    One of ``exact`` / ``planned`` / ``estimated``, or None when absent.
    """
    m = _COUNT_RE.search(request.headers.get("prefer", ""))
    return m.group(1) if m else None


def content_range(offset: int, returned: int, total: int | None) -> str:
    """Build a ``Content-Range`` header value: ``<first>-<last>/<total|*>``.

    An empty page renders the range as ``*`` (e.g. ``*/0``); an unknown total
    (no count requested) renders as ``*`` (e.g. ``0-9/*``) — matching PostgREST.
    """
    span = f"{offset}-{offset + returned - 1}" if returned > 0 else "*"
    return f"{span}/{'*' if total is None else total}"


def resolve_total(conn, count_kind: str | None, from_sql: str, where_sql: str, where_params) -> int | None:
    """Compute the total matching row count for a list query, or None.

    ``exact`` runs ``COUNT(*)``; ``planned``/``estimated`` read the planner's
    row estimate via ``EXPLAIN`` (no execution). ``from_sql`` is the FROM body
    (e.g. ``"maludb_subject s"`` or a table + JOINs), ``where_sql`` the full
    ``WHERE …`` clause (or "") with its ``where_params`` — the same filtered set
    as the list query, so the count matches what a limit-less fetch would return.
    """
    if not count_kind:
        return None
    from app.database import db_one  # deferred import to avoid a module cycle

    if count_kind == "exact":
        row = db_one(conn, f"SELECT count(*) AS n FROM {from_sql} {where_sql}", where_params)
        return int(row["n"]) if row and row["n"] is not None else 0

    # planned / estimated → the planner's estimate (EXPLAIN does not execute).
    row = db_one(conn, f"EXPLAIN (FORMAT JSON) SELECT 1 FROM {from_sql} {where_sql}", where_params)
    plan = row["QUERY PLAN"]
    if isinstance(plan, str):
        plan = json.loads(plan)
    return int(plan[0]["Plan"]["Plan Rows"])
