# User-Table REST API (Supabase / PostgREST compatible)

MaluDB tenants own their Postgres schema and can create their own application
tables in it (e.g. `orders`, `products`) alongside the `maludb_*` memory
facades. This API serves those tables generically — no app server or SQL driver
needed — by reflecting the table from the catalog per request
(`app/helpers/reflect.py`) and running the shared PostgREST-style grammar
(`app/helpers/query.py`) over it.

Only **base tables in the tenant's own schema** are served. `maludb_*` and
`malu$*` names are rejected; the memory structures keep their hand-written
routers (see the endpoint docs) — those routers speak the same filter grammar
but stay curated.

## Two mounts, one implementation

| Mount | Shape | For |
|---|---|---|
| `/rest/v1/{table}` | PostgREST wire format: bare-array responses, PostgREST error bodies (`{"code","message","details","hint"}`), `Prefer:` headers, `Accept: application/vnd.pgrst.object+json` | supabase-js / supabase-py / any PostgREST client, pointed at this server unchanged |
| `/v1/tables/{table}` | House style: `{"rows": …}` / `{"inserted": n}` envelopes and the standard `{"error":{code,message}}` shape | Clients already speaking the MaluDB API |

Auth is the standard MaluDB Bearer token on both mounts. Supabase clients work
out of the box because `create_client(url, key)` sends the key as
`Authorization: Bearer <key>` — pass your `malu_…` token as the key:

```python
from supabase import create_client
sb = create_client("https://api.example.org", "malu_…")
rows = sb.table("products").select("*").gt("price", 50).order("price").execute().data
```

Note: *authentication* failures (401/502/503) are raised before the table
router runs and use the house error envelope on both mounts.

## Supported surface

**Read** — `GET`/`HEAD /{table}`
- Filters: `?col=op.value` with `eq neq gt gte lt lte like ilike match imatch
  in is fts plfts phfts wfts`, negation `not.`, repeated params (AND),
  `or=(…)` / `and=(…)` groups
- Projection: `?select=col,alias:col,*`
- Ordering: `?order=col.desc.nullslast,…`
- Pagination: `limit` / `offset` (default and max **1000** rows)
- Counts: `Prefer: count=exact|planned|estimated` → total in `Content-Range`
- `.single()`: `Accept: application/vnd.pgrst.object+json` returns one object
  (406 `PGRST116` unless exactly one row) — `/rest/v1` only

**Insert** — `POST /{table}`
- Body: one JSON object or an array (bulk; one atomic statement). Missing keys
  insert as column `DEFAULT`s; `?columns=` restricts/fixes the insert columns
- Upsert: `Prefer: resolution=merge-duplicates|ignore-duplicates`, conflict
  target `?on_conflict=col,…` (defaults to the primary key)
- `Prefer: return=representation` echoes the inserted rows (honoring
  `?select=`); default is minimal (`201`, empty body on `/rest/v1`)

**Update** — `PATCH /{table}?<filters>` with a `{"col": value}` body
**Delete** — `DELETE /{table}?<filters>`
- Both honor `Prefer: return=representation` (else `204` on `/rest/v1`,
  `{"updated"/"deleted": n}` on `/v1/tables`)
- An unfiltered PATCH/DELETE affects the whole table (PostgREST parity) — but
  see strictness below

## Strictness (deliberate divergence from the memory routers)

Unknown query-param keys are **rejected with 400** instead of ignored. On a
generic write surface, a typo'd filter (`?idd=eq.5`) that was silently ignored
would turn a targeted DELETE into a full-table DELETE. `debug` remains allowed
(`?debug=1` SQL trace), plus `columns` / `on_conflict` on POST.

## Error shapes

`/rest/v1` returns PostgREST-style bodies. Postgres errors pass the SQLSTATE
through as `code` (e.g. `23505` duplicate key → HTTP 409); grammar/valdation
errors use `PGRST1xx`/`PGRST2xx` codes (`PGRST205` unknown table, `PGRST204`
unknown column, `PGRST116` object mismatch). `/v1/tables` uses the standard
MaluDB error envelope and SQLSTATE mapping.

## Not implemented (yet)

Resource embedding (`select=rel(*)`), `/rpc/{fn}`, JSON-path and array
operators (`->`, `cs`, `cd`, `ov`), `Range` headers, CSV bodies, `PUT` upsert.
Requests using them fail with an explicit 400 rather than misbehaving.

## Verification

`tests/test_rest.py` covers the SQL builders and strict-key logic. The
end-to-end suite `tests/test_rest_e2e.py` runs against a real tenant DB when
`MALUDB_E2E_TOKEN` + `MALUDB_E2E_DSN` are set (see its docstring), and the
surface has been verified against the real `supabase-py` client (v2.31).
