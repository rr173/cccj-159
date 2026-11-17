# Offline two-level bi-temporal history join

The materialised layer previously only queried already-processed facts.  This
package (`histjoin`) adds the missing offline warehouse step: it joins each
order against two levels of bi-temporal dimension history (customer→store,
store→region) and emits the fixed fact table that the existing
`matview` query layer consumes, together with a per-order lineage trail and an
isolation list.  It changes neither the materialisation selection algorithm
nor any existing entry point, and it does no real-time subscription.

## Inputs

Three unordered JSONL files (file/line order never affects the result):

| file | record shape |
|------|--------------|
| orders | `row_id` (unique non-empty text), `customer` (nullable non-empty text), `event_time`, `known_time` (int64), `amount` (nullable int64) |
| customer history | `version_id`, `key`, `valid_from`, `valid_to`, `known_from`, `known_to`, `priority` (int64), `deleted` (bool), `value` (nullable text) |
| store history | identical history shape |

Intervals are half-open `[from, to)`; a `to` of `null` means positive
infinity and empty intervals (`from >= to`) are whole-input errors.
`deleted=true` carries no value; `deleted=false` with `value=null` is a valid
**empty attribute**, distinct from a deletion.  All time arithmetic is exact
Python/SQLite integer comparison — no float index, no end-point `+1` — so the
full int64 range is handled precisely.

## The adjudication rule, exactly

For an order point `(event_time, known_time)` and a history `key`, candidates
are the versions whose business interval contains `event_time` **and** whose
knowledge interval contains `known_time` (equality on a right end point is an
exclusion).  Only the maximum `priority` among candidates is inspected:

* no candidates → `gap` (the candidate list is empty);
* top tier all `deleted` → `deleted`;
* top tier all non-deleted with `value=null` → `null_value`;
* top tier identical on `(deleted, value)` with a non-null value → `ok`, and
  **every** tied `version_id` is kept as an equally-valid source;
* top tier differs on `(deleted, value)` → `conflict`.  No side is chosen by
  file order, version id or recency.

A lower-priority old value can never fill a deletion, an empty attribute or a
conflict.  The second level runs only after level 1 yields a non-null store,
and it reuses the **order's own** `(event_time, known_time)` — never the start
of the customer version.  Both levels apply the same rule.

Orders with `customer = null` are isolated as `missing_key` (not `gap`).  Each
input order lands in exactly one of the fact table or the isolation list.

## Outputs (a self-describing result directory)

```
<out>/WAREHOUSE.json      marker, input digests/counts, manifest
<out>/schema.json         fact schema for matview.build_snapshot
<out>/facts.sqlite        facts in the exact matview physical shape
<out>/warehouse.sqlite    facts_ref, quarantine, provenance, reconcile
<out>/quarantine.jsonl    isolation list (row_id codepoint order)
<out>/lineage.jsonl       full per-order provenance (row_id codepoint order)
```

Facts keep `row_id, customer, store, region, event_time, known_time, amount`;
distinct row ids with identical business values stay distinct rows.
Quarantine rows name the failed level, the reason, the already-resolved
upstream versions and all top-tier versions at the failure.  A fresh process
needs only the directory to answer `lineage`, `quarantine`, `facts` and
`counts`.

## Why the disk-backed sweep satisfies both exactness and the resource bound

A direct point query (`valid_from <= t AND (valid_to IS NULL OR t < valid_to)`)
against 100k versions of one hot key, repeated for 100k orders, examines on the
order of 5×10⁹ rows — far too slow, and loading a hot key into a Python list
breaks the memory cap.  The engine instead merges two indexed streams **per
key**, ordered by business start:

1. versions are admitted into a scratch `active` table as their
   `valid_from` reaches the current event time, and expired when
   `valid_to <= event_time` (half-open);
2. one indexed aggregate returns the max knowledge-qualified priority, and a
   second bounded, batched query returns the (usually few) top-tier rows;
3. outcomes and provenance are streamed to disk in fixed-size chunks through a
   bounded buffer — even a pathological 10 000-way equal-priority tie per
   order never becomes a Python list.

The scratch database uses a capped page cache and file-backed temp storage, so
a hot key's whole history and all orders live on disk, not in Python memory.
Every ordering decision is an explicit SQL `ORDER BY` on stable columns
(`row_id`/`version_id` text in Unicode code-point order; SQLite's BINARY
collation on TEXT is code-point order), which is why shuffling the inputs or
changing the read chunk size changes neither facts nor provenance.

## Atomicity and whole-input failures

Type errors, duplicate `row_id`/`version_id` (reported with both locations)
and invalid intervals abort the whole run with the file name and 1-based line
number; no row is silently dropped and no partial fact database is published.
The result directory is assembled in a sibling `.warehouse-build-*` tree and
moved into place with a single rename, so a failed build leaves a previously
published complete result fully queryable, and every file of a successful
publish comes from the same input run.

## Commands

```bash
# build (chunk/cache are resource knobs only; output is identical)
python -m histjoin.cli build ORDERS CUSTOMER_HIST STORE_HIST OUT_DIR
python -m histjoin.cli lineage    OUT_DIR ROW_ID     # per-row provenance
python -m histjoin.cli quarantine OUT_DIR             # isolation list
python -m histjoin.cli facts      OUT_DIR             # warehoused facts
python -m histjoin.cli counts     OUT_DIR             # input reconciliation
```

## Scale contract (reproducible)

`histjoin.hotkey` generates one hot customer with 100 000 disjoint business
intervals, interleaved knowledge intervals and late higher-priority
corrections, ten hot stores, and 100 000 shuffled orders (including null
customers, unknown keys and int64 end points).  Run under
`taskset -c 0` with a 256 MiB `RLIMIT_AS` cap via `demo/run_hotkey.py`, the
two-level join completes in well under 180 s at ~64 MiB peak RSS; the recorded
numbers are in `evidence/hotkey-report.json`, and 40 fixed sample orders are
checked there against the independent brute-force enumerator
(`tests/histjoin_refimpl.py`), which imports no product resolution code.
Generated large files are git-ignored.
