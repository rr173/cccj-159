# Materialised-result reuse layer

A Python/SQLite layer that answers structured queries over a fixed fact table
using previously materialised aggregate results, reusing them **only where it
can prove they apply exactly**, and filling the remainder from base fact rows
with full three-valued (SQL) semantics.

It is not a recommender and not a metric catalogue: it builds materialisation
files, plans a query against them, executes the plan, and explains every
decision and every physical read.

## Install / run

Pure standard library (Python 3.9+, SQLite 3.35+).  No dependencies.

```bash
python3 demo/six_rows.py              # the six-row worked example
python3 -m unittest discover -s tests -t . -p 'test_*.py'
python3 demo/record_evidence.py       # writes evidence/evidence.json
```

The CLI mirrors the API:

```bash
python3 -m matview.cli build   CAT DIR/facts.sqlite schema.json specs.json
python3 -m matview.cli register CAT SNAPSHOT MAT_ID
python3 -m matview.cli plan    CAT SNAPSHOT query.json
python3 -m matview.cli execute CAT SNAPSHOT query.json
python3 -m matview.cli explain CAT SNAPSHOT query.json
```

## Protocol

### Facts

One fixed fact table per snapshot, created by the product from real rows.

* declared fields are `int`, `text` or `null`;
* a unique `row_id` identifies a *fact row*, not a business key: two rows with
  identical business values are two facts and are counted twice.

### Query (JSON, never SQL text)

```json
{
  "group_by": ["d"],
  "where": {"op": "or", "args": [
    {"op": "range", "col": "d", "lo": 1, "hi": 3},
    {"op": "is_null", "col": "d"}
  ]},
  "measures": [
    {"kind": "count_star", "as": "cnt"},
    {"kind": "count", "col": "amount", "as": "nn"},
    {"kind": "sum", "col": "amount", "as": "total"},
    {"kind": "avg", "col": "amount", "as": "mean"}
  ]
}
```

Predicates: `eq` (type-consistent), `range` (integer closed interval, `null`
bound = open end), `is_null`, `and`, `or`, `not`.  Evaluation is three-valued;
**only `TRUE` enters a result**, `FALSE` and `NULL` do not.

Measures: `count(*)`; `count(x)` counts non-null `x`; `sum(x)` is exact
integer; `avg(x)` is an exact reduced fraction `sum/count`.  `sum`/`avg` are
`NULL` when no non-null value contributes.  With no `group_by`, exactly one
row is returned even for empty input (`count = 0`, `sum`/`avg = NULL`); with
`group_by`, only non-empty groups appear.

### Materialisation definition (JSON)

```json
{
  "name": "M1",
  "predicate": {"op": "range", "col": "d", "lo": 1, "hi": 3},
  "group_cols": ["d"],
  "measures": ["amount"],
  "retained_cols": ["region"],
  "state": "full"
}
```

* The product **scans real fact rows** to derive every stored value; a
  caller-supplied row count is never read or trusted.
* Every group always stores `n` = `count(*)`.  For each measure column it
  stores the non-null count and the exact integer sum.  An AVG is *never*
  stored as a mergeable state; `state: {"skip": [...], "extra":
  {"avg:amount": true}}` models a definition that only kept a final average,
  and planning rejects it for any query that needs to merge.
* `retained_cols` may keep extra columns (e.g. the text column filtered on)
  but only when constant over every base row of the group; the builder
  verifies this and refuses otherwise.
* Distinct row ids with identical business values remain distinct facts.

### Planning: what may be reused, and why

A materialised group is a *lemma*: "the rows of group `g` are exactly the base
rows admitted by the definition's predicate whose group-column values are
`g.key`, with each retained column pinned to its constant value".  Its true
region is computed exactly (`region.true_set`) and must be

1. **wholly inside** the query region — groups are never split, and a group
   that straddles the boundary is skipped with the reason
   `group_crosses_boundary_unsplittable`.  This covers a coarser materialisation
   (e.g. no date grouping) being asked for a sub-range: its internal share is
   not proactively estimated;
2. **disjoint from every region already credited** — so intersecting
   materialisations are never summed twice (`region_already_covered`);
3. **answerable from stored state** — a missing sum or non-null count yields
   `missing_aggregate_state`, and `count(x)` is never substituted by
   `count(*)`.

The result is a pairwise-disjoint contribution partition.  Finer groups may be
folded up to a coarser grouping (`region + d` → `region`), never the reverse.

Uncovered rows are computed as a **three-valued complement**:

```
residual = Q AND NOT_TRUE(cover)
```

`NOT_TRUE(p)` is TRUE iff `p` is FALSE *or* NULL, so a NULL on a covering
predicate cannot silently drop rows the way a SQL `NOT(...)` would.  The
remainder is expressed as a finite union of exact worlds (integer ranges,
NULL, text `=` / `NOT IN`), each run as a narrow query against the facts, with
an independent re-evaluation of the canonical residual on every returned row.
If the region exceeds the world budget it falls back to a full three-valued
base scan, and says so.

Rejections always carry a stable reason: `different_snapshot`,
`snapshot_mismatch`, `schema_mismatch`, `missing_aggregate_state`,
`coarser_group_cols`, `unknown_field`, `invalid_definition`,
`definition_digest_mismatch`, `data_file_corrupt`, `data_file_missing`,
`not_registered`, `unreadable_materialisation`.

### Snapshot identity

Every snapshot records the base fact file's content digest.  A query
re-derives it and refuses (`snapshot_mismatch`) if facts changed after the
build.  A materialisation is only ever used inside its own snapshot; another
snapshot's materialisation is reported as `different_snapshot`.

### Durable build and registration

A snapshot is assembled in `snapshots/snapshot-build-*`, every file is
written, fsync'd, re-read and compared against a fresh scan of real facts, and
only then renamed into its final directory.  `SNAPSHOT.json` is written last.
A failure at any point removes the tree, so nothing half-built can be
discovered or registered.  Leftover temp trees from a kill are collected on
the next open.  Registration is a separate explicit step stored in
`index.sqlite`; any new process opening the directory rebuilds the index from
finished snapshots and can execute plans with only the directory path.

### Evidence

Execution reports, per scenario:

* `base_rows_scanned`: **distinct** fact rows physically examined, counted by
  a probe UDF evaluated inside the SQL query (so it reflects the access path
  chosen, index search or table scan);
* the exact SQL and parameters of each residual world query;
* the materialisation data files opened, with size and SHA-256;
* `base_full_scan`: whether any branch fell back to a whole-table scan.

## Worked example (the brief's six rows)

Facts: dates `1,2,3,4,5,NULL` with amounts `10,NULL,30,40,50,60`.  `M1` covers
dates 1–3 and `M2` covers 3–5, both grouped by date.  Global query:

```
count(*)=6  count(amount)=5  sum=190  avg=38/1
```

Five groups are credited from M1/M2 (date 3 exactly once); the single NULL-date
row is the only base read, and the residual is exactly
`d IS NULL OR d <= 0 OR d >= 6`.  A coarse `M3` without date grouping is
refused for a `d=2..4` query (`coarser_group_cols`), and an AVG-only
materialisation is refused for a merge (`missing_aggregate_state`).

## Tests

`tests/` contains an **independent** reference enumerator (`refimpl.py`,
its own SQL reading, its own three-valued evaluator, its own accumulator) used
to compute expected answers; product results are compared against it.  Plan
derivation code is never used to compute the expectation.  Additional
randomised property tests cover boolean predicates with NULLs, overlapping
materialisations, and partial saved states.
