# Design notes

## Why the algebra is exact, not heuristic

The brief permits a bounded decision procedure (integer ranges, NULL branches,
finite text equality partitions) but forbids guessing.  Two ideas make every
decision provable:

1. **A materialised group is a lemma.** Its predicate, group key and retained
   columns are all exact facts about base rows, so the set of base rows it
   covers is a finite union of *worlds* (conjunctions of per-column
   constraints).  `region.true_set` derives that union from the definition; it
   never consults the data.

2. **Coverage is set algebra.** A group is reused only if its world union is
   (a) a subset of the query's true set and (b) disjoint from all credited
   groups.  Subset and disjointness are decided by exact world subtraction, not
   by comparing counts or sampling.

Text partitions stay finite because the predicate grammar only has finite text
equality (`eq`); `NOT IN S` complements enumerate S.  Integer complements
enumerate at most two interval pieces.  A world budget (`budget_worlds`, default
8192) bounds pathological input; exceeding it is treated as *cannot prove*, and
the planner falls back to a full three-valued base scan — **withdrawing any
already-selected groups** so rows are never counted twice.

## Three-valued logic, concretely

Internal predicates include `is_true(p)` and `not_true(p)`.  `not_true(p)` is
TRUE iff `p` is FALSE **or NULL**.  Residuals are

```
Q AND NOT_TRUE(cover)
```

so a NULL on the covering predicate cannot drop rows.  Example: cover is
`d BETWEEN 1 AND 5`; for the NULL-date row it evaluates to NULL.  SQL
`NOT(cover)` would be NULL and the row would vanish.  `NOT_TRUE(cover)` is TRUE,
the row survives into the base branch, and it is admitted exactly once because
it was never credited.

Base-row filtering is performed twice, independently: once by the exact SQL
world condition and once by re-evaluating the canonical residual predicate on
each returned row.  They must agree; disagreement would be a bug.

## Residual shape

`Q` and the cover are both converted to disjoint worlds.  Each world becomes a
narrow SQL condition (`d IS NULL`, `d >= ?`, `region = ?`, `region IN (...)`).
The facts DB indexes every business column, so a world that pins one column is
an index search, not a scan.  A probe UDF counts the distinct fact rows SQLite
actually feeds through, which is the number reported as `base_rows_scanned`.

## Finer → coarser

A finer group (e.g. `region + d`) folds into a coarser query grouping
(`region`) because its stored `n`, non-null counts and integer sums are exactly
the per-group state that must be added.  The reverse — splitting a coarse group
— is never attempted.  When both a coarse and a fine materialisation could
supply the same rows, the first registered wins; the *answer* is identical and
only the credited basis changes, which `explain` reports per group.

## Why aggregates are stored as (n, null-count, integer sum)

`AVG` and `COUNT(x)` are not additive across groups.  Storing `n`, `nn(x)` and
`sum(x)` makes every query measure derivable:

| measure   | from stored state |
|-----------|-------------------|
| count(*)  | `n`               |
| count(x)  | `nn:x`            |
| sum(x)    | `sum:x`           |
| avg(x)    | `sum:x / nn:x` (exact rational) |

A definition that saved only an average (`state.extra.avg:x`) is not mergeable:
planning rejects it when the query needs `sum`/`avg`/`count(x)`, while
`count(*)`-only queries can still use it.

## Failure atomicity

The snapshot is built under `snapshots/snapshot-build-*`; each materialisation
writes `data.tmp-*`, fsyncs, renames to `data.sqlite`, then the writer re-reads
the file and compares against the intended state.  After all files, a second
pass re-scans real facts and recomputes every stored group.  Only then is
`SNAPSHOT.json` written and the directory renamed.  `resolve` removes build
temp dirs on each open, so a crashed build never becomes visible, and the index
never points at an unfinished snapshot.

## Staleness

Each snapshot records the SHA-256 content digest of the fact file.  Planning
recomputes it and raises `snapshot_mismatch` if the file changed, so a
materialisation can never silently answer from rows that no longer exist.
Materialisations also store their `fact_digest`; a definition copied into a
different snapshot is rejected with the same reason.

## What the tests guarantee

`tests/refimpl.py` is a deliberately independent enumerator: its own SQL read,
its own three-valued evaluator, its own accumulator.  The production planner is
never used to build expected answers.  `test_region.py` additionally proves the
region algebra against a finite domain by brute-force membership.  The
randomised suite (`test_property.py`) exercises overlapping, contained,
disjoint, holed and finer/coarser materialisations over random predicates with
NULLs.
