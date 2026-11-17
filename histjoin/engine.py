"""Exact two-level bi-temporal adjudication, executed as disk-backed sweeps.

The decision rule for one order at one level
--------------------------------------------
For point ``(event_time, known_time)`` and history ``key`` the candidates are
exactly the versions whose *business* interval contains ``event_time`` AND
whose *knowledge* interval contains ``known_time`` (both intervals are
half-open, so an end-point equality is an exclusion):

    valid_from <= event_time < valid_to(or +inf)
    known_from <= known_time < known_to(or +inf)

Only the **maximum priority** among the candidates matters.  Let the set of
candidates holding that priority be T:

* T empty                          -> ``gap``          (version list empty)
* all T have deleted=1             -> ``deleted``
* all T have deleted=0, value=None -> ``null_value``   (valid empty attribute)
* all T identical on (deleted,value) with a non-null value -> ``ok``;
  every tied version_id is retained as an equally-valid provenance entry
* T differs in (deleted, value)    -> ``conflict``; no side may be picked
  based on file order, version id ordering or recency.

A lower-priority old value must never fill a deletion, an empty attribute or
a conflict -- the rule looks at the top tier alone.

Why a sweep, and why it fits the resource budget
-------------------------------------------------
A naive point query (``valid_from <= t AND (valid_to IS NULL OR t < valid_to)``
against 100k versions of one hot key, times 100k orders) makes SQLite examine
~5e9 rows.  Instead, per key we merge two already-indexed streams ordered by
business start: the key's demands (orders) and the key's versions.  A
disk-backed ``active`` scratch table holds versions whose business interval
has started but not yet ended at the current event time; at each demand a
single indexed SQL query returns the max priority of knowledge-qualified
active rows and a second bounded query returns the (usually very few) top-tier
rows.  Everything stays on disk: cursors use ``fetchmany``, result batches are
flushed in fixed-size chunks, and no hot key's versions or orders are ever
materialised as a Python list.  Integer comparisons only -- there is no float
index and no end-point ``+1`` anywhere, so int64 extremes stay exact.
"""

from __future__ import annotations

# outcome codes (also the stable reason codes written to quarantine rows)
GAP = "gap"
DELETED = "deleted"
NULL_VALUE = "null_value"
CONFLICT = "conflict"
OK = "ok"
MISSING_KEY = "missing_key"

ACTIVE_DDL = """
CREATE TEMP TABLE active (
    version_id TEXT NOT NULL,
    valid_from INTEGER NOT NULL,
    valid_to   INTEGER,
    known_from INTEGER NOT NULL,
    known_to   INTEGER,
    priority   INTEGER NOT NULL,
    deleted    INTEGER NOT NULL,
    value      TEXT
);
CREATE INDEX ix_active_known ON active(known_from, known_to, priority);
CREATE INDEX ix_active_validto ON active(valid_to);
"""


class SweepEngine:
    """Runs both levels against one work database, writing resolution rows."""

    def __init__(self, con, chunk=2000):
        self.con = con
        self.chunk = chunk
        self._active_ready = False
        # provenance is streamed into this bounded buffer and flushed to disk;
        # a pathological key with thousands of tied top-tier versions per
        # demand can therefore never accumulate a Python-sized result list
        self._prov_buf = []

    # -- output table setup ------------------------------------------------

    def prepare_outputs(self):
        self.con.executescript(
            """
            CREATE TABLE level1_res (
                rid        INTEGER PRIMARY KEY,
                row_id     TEXT NOT NULL,
                customer   TEXT,
                store      TEXT,
                status     TEXT NOT NULL,
                event_time INTEGER NOT NULL,
                known_time INTEGER NOT NULL,
                amount     INTEGER
            );
            CREATE TABLE demands2 (
                rid        INTEGER PRIMARY KEY,
                key        TEXT NOT NULL,
                event_time INTEGER NOT NULL,
                known_time INTEGER NOT NULL
            );
            CREATE INDEX ix_demands2_key ON demands2(key, event_time, rid);
            CREATE TABLE level2_res (
                rid        INTEGER PRIMARY KEY,
                region     TEXT,
                status     TEXT NOT NULL
            );
            CREATE TABLE provenance (
                rid        INTEGER NOT NULL,
                level      TEXT NOT NULL CHECK (level IN ('customer','store')),
                key        TEXT,
                status     TEXT NOT NULL,
                value      TEXT,
                version_id TEXT NOT NULL,
                tied       INTEGER NOT NULL CHECK (tied IN (0,1)),
                ord        INTEGER NOT NULL
            );
            CREATE INDEX ix_prov_rid ON provenance(rid, level, ord);
            CREATE TABLE quarantine (
                rid        INTEGER PRIMARY KEY,
                row_id     TEXT NOT NULL,
                customer   TEXT,
                event_time INTEGER NOT NULL,
                known_time INTEGER NOT NULL,
                amount     INTEGER,
                failed_level TEXT NOT NULL,
                reason     TEXT NOT NULL,
                upstream_versions TEXT NOT NULL,
                top_versions      TEXT NOT NULL
            );
            CREATE TABLE facts_stage (
                rid        INTEGER PRIMARY KEY,
                row_id     TEXT NOT NULL,
                customer   TEXT NOT NULL,
                store      TEXT NOT NULL,
                region     TEXT NOT NULL,
                event_time INTEGER NOT NULL,
                known_time INTEGER NOT NULL,
                amount     INTEGER
            );
            """
        )
        self.con.commit()

    def _ensure_active(self):
        if not self._active_ready:
            self.con.executescript(ACTIVE_DDL)
            self._active_ready = True

    # -- provenance helper: bounded buffer, always flushed to disk ---------

    def _emit_prov(self, rows):
        self._prov_buf.extend(rows)
        if len(self._prov_buf) >= self.chunk:
            self._flush_prov()

    def _flush_prov(self):
        if self._prov_buf:
            self.con.executemany(
                "INSERT INTO provenance (rid, level, key, status, value,"
                " version_id, tied, ord) VALUES (?,?,?,?,?,?,?,?)",
                self._prov_buf,
            )
            self._prov_buf.clear()

    # -- level 1: order -> store via customer history ----------------------

    def resolve_level1(self):
        self._ensure_active()
        con = self.con
        keys = [r[0] for r in con.execute(
            "SELECT DISTINCT customer FROM orders WHERE customer IS NOT NULL ORDER BY 1"
        )]
        # orders with customer = NULL never enter a sweep
        missing_rids = [r[0] for r in con.execute(
            "SELECT rid FROM orders WHERE customer IS NULL ORDER BY rid"
        )]
        con.execute(
            "INSERT INTO level1_res (rid, row_id, customer, store, status,"
            " event_time, known_time, amount)"
            " SELECT rid, row_id, NULL, NULL, ?, event_time, known_time, amount"
            " FROM orders WHERE customer IS NULL",
            (MISSING_KEY,),
        )
        self._emit_prov(
            [(rid, "customer", None, MISSING_KEY, None, "-", 0, 0)
             for rid in missing_rids]
        )
        res_batch = []
        for key in keys:
            for outcome in self._sweep_key(
                kind="customer", key=key,
                demand_sql=(
                    "SELECT rid, event_time, known_time FROM orders"
                    " WHERE customer=? ORDER BY event_time, rid"
                ),
                params=(key,), level="customer",
            ):
                rid, event_time, known_time, status, store = outcome
                res_batch.append((rid, key, store, status, event_time, known_time))
                if len(res_batch) >= self.chunk:
                    self._flush_level1(res_batch)
                    res_batch.clear()
        if res_batch:
            self._flush_level1(res_batch)
        self._flush_prov()
        con.commit()
        return len(missing_rids)

    def _flush_level1(self, batch):
        # row_id and amount are joined back from orders so the sweep itself
        # only carries rid plus the adjudication, not whole order rows.
        self.con.executemany(
            "INSERT INTO level1_res (rid, row_id, customer, store, status,"
            " event_time, known_time, amount)"
            " SELECT o.rid, o.row_id, ?, ?, ?, ?, ?, o.amount"
            " FROM orders o WHERE o.rid=?",
            [
                # SELECT order: customer, store, status, event_time, known_time
                (customer, store, status, event_time, known_time, rid)
                for (rid, customer, store, status, event_time, known_time) in batch
            ],
        )

    # -- level 2: store -> region, reusing the order's own times -----------

    def build_level2_demands(self):
        # Only rows that resolved to a non-null store get a second-level
        # resolution; the original order times are carried, never the start
        # of the customer version that produced the store.
        self.con.execute(
            "INSERT INTO demands2 (rid, key, event_time, known_time)"
            " SELECT l.rid, l.store, o.event_time, o.known_time"
            " FROM level1_res l JOIN orders o ON o.rid=l.rid"
            " WHERE l.status=? ORDER BY l.store, o.event_time, l.rid",
            (OK,),
        )
        self.con.commit()

    def resolve_level2(self):
        self._ensure_active()
        con = self.con
        keys = [r[0] for r in con.execute(
            "SELECT DISTINCT key FROM demands2 ORDER BY 1"
        )]
        res_batch = []
        for key in keys:
            for outcome in self._sweep_key(
                kind="store", key=key,
                demand_sql=(
                    "SELECT rid, event_time, known_time FROM demands2"
                    " WHERE key=? ORDER BY event_time, rid"
                ),
                params=(key,), level="store",
            ):
                rid, _et, _kt, status, region = outcome
                res_batch.append((rid, region if status == OK else None, status))
                if len(res_batch) >= self.chunk:
                    self._flush_level2(res_batch)
                    res_batch.clear()
        if res_batch:
            self._flush_level2(res_batch)
        self._flush_prov()
        con.commit()

    def _flush_level2(self, batch):
        self.con.executemany(
            "INSERT INTO level2_res (rid, region, status) VALUES (?,?,?)", batch
        )

    # -- the shared per-key merge sweep ------------------------------------

    def _sweep_key(self, kind, key, demand_sql, params, level):
        """Yield one outcome tuple per demand for a single history key.

        ``outcome`` = ``(rid, event_time, known_time, status, value)``.
        Provenance rows are streamed into the bounded provenance buffer inside
        :meth:`_adjudicate`, so a key with a huge tied top tier never produces
        a Python-sized list.  The scratch ``active`` table is emptied between
        keys; its contents for the current key always equal "business
        interval open at the current event time".
        """
        con = self.con
        con.execute("DELETE FROM active")
        demands = con.execute(demand_sql, params)
        versions = con.execute(
            "SELECT version_id, valid_from, valid_to, known_from, known_to,"
            " priority, deleted, value FROM hist"
            " WHERE kind=? AND key=? ORDER BY valid_from, hid",
            (kind, key),
        )
        next_ver = self._next_row(versions)
        while True:
            chunk = demands.fetchmany(self.chunk)
            if not chunk:
                break
            for rid, event_time, known_time in chunk:
                # admit every version whose business interval has started
                while next_ver is not None and next_ver[1] <= event_time:
                    con.execute(
                        "INSERT INTO active (version_id, valid_from, valid_to,"
                        " known_from, known_to, priority, deleted, value)"
                        " VALUES (?,?,?,?,?,?,?,?)",
                        next_ver,
                    )
                    next_ver = self._next_row(versions)
                # expire versions whose business interval ended before t
                # (half-open: valid_to == t means t is NOT covered)
                con.execute(
                    "DELETE FROM active WHERE valid_to IS NOT NULL AND valid_to <= ?",
                    (event_time,),
                )
                yield self._adjudicate(
                    rid, key, event_time, known_time, level
                )

    @staticmethod
    def _next_row(cur):
        rows = cur.fetchmany(1)
        return rows[0] if rows else None

    def _adjudicate(self, rid, key, event_time, known_time, level):
        """Apply the top-priority-only rule using the disk-backed active set.

        The top tier is read in bounded batches: classification only needs the
        distinct ``(deleted, value)`` signatures, while every top-tier row is
        streamed into the provenance buffer (never returned as a big list), so
        a key with thousands of tied versions stays inside the memory budget.
        """
        con = self.con
        top_priority = con.execute(
            "SELECT MAX(priority) FROM active"
            " WHERE known_from <= ? AND (known_to IS NULL OR ? < known_to)",
            (known_time, known_time),
        ).fetchone()[0]
        if top_priority is None:
            # explicit empty-candidate marker: lineage records that the
            # candidate list was empty (gap), not merely unrecorded
            self._emit_prov([(rid, level, key, GAP, None, "-", 0, 0)])
            return rid, event_time, known_time, GAP, None

        cur = con.execute(
            "SELECT version_id, deleted, value FROM active"
            " WHERE known_from <= ? AND (known_to IS NULL OR ? < known_to)"
            " AND priority=? ORDER BY version_id",
            (known_time, known_time, top_priority),
        )
        signatures = set()
        pending = []          # this one demand's top-tier provenance tuples
        top_count = 0
        while True:
            batch = cur.fetchmany(self.chunk)
            if not batch:
                break
            for vid, deleted, val in batch:
                signatures.add((deleted, val))
                pending.append((rid, level, key, None, val, vid, 0, top_count))
                top_count += 1
        status, value = self._classify_signatures(signatures)
        tied = 1 if top_count > 1 else 0
        self._emit_prov([
            (r[0], r[1], r[2], status, r[4], r[5], tied, r[7]) for r in pending
        ])
        return rid, event_time, known_time, status, value

    @staticmethod
    def _classify_signatures(signatures):
        if len(signatures) == 1:
            deleted, val = next(iter(signatures))
            if deleted:
                return DELETED, None
            if val is None:
                return NULL_VALUE, None
            return OK, val
        return CONFLICT, None

    # -- finalisation: facts vs. quarantine, each order exactly once -------

    def finalise(self):
        con = self.con
        # facts: both levels OK with non-null values.  Distinct row_ids with
        # identical business values stay distinct rows (no merging).
        con.execute(
            "INSERT INTO facts_stage (rid, row_id, customer, store, region,"
            " event_time, known_time, amount)"
            " SELECT o.rid, o.row_id, o.customer, l1.store, l2.region,"
            " o.event_time, o.known_time, o.amount"
            " FROM orders o"
            " JOIN level1_res l1 ON l1.rid=o.rid"
            " JOIN level2_res l2 ON l2.rid=o.rid"
            " WHERE l1.status=? AND l2.status=?",
            (OK, OK),
        )
        # level-1 failures (including missing_key for customer = NULL)
        con.execute(
            "INSERT INTO quarantine (rid, row_id, customer, event_time, known_time,"
            " amount, failed_level, reason, upstream_versions, top_versions)"
            " SELECT o.rid, o.row_id, o.customer, o.event_time, o.known_time, o.amount,"
            " 'customer', l1.status, '[]',"
            " (" + self._top_versions_sql("customer") + ")"
            " FROM orders o JOIN level1_res l1 ON l1.rid=o.rid"
            " WHERE l1.status <> ?",
            (OK,),
        )
        # level-2 failures: carry the already-resolved customer versions
        con.execute(
            "INSERT INTO quarantine (rid, row_id, customer, event_time, known_time,"
            " amount, failed_level, reason, upstream_versions, top_versions)"
            " SELECT o.rid, o.row_id, o.customer, o.event_time, o.known_time, o.amount,"
            " 'store', l2.status,"
            " (" + self._top_versions_sql("customer") + "),"
            " (" + self._top_versions_sql("store") + ")"
            " FROM orders o"
            " JOIN level1_res l1 ON l1.rid=o.rid"
            " JOIN level2_res l2 ON l2.rid=o.rid"
            " WHERE l1.status=? AND l2.status <> ?",
            (OK, OK),
        )
        con.commit()
        return (
            con.execute("SELECT COUNT(*) FROM facts_stage").fetchone()[0],
            con.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0],
        )

    @staticmethod
    def _top_versions_sql(level):
        # sorted JSON array of the top-tier version ids for one level;
        # the '-' gap marker is deliberately excluded so gaps serialise as []
        return (
            "SELECT COALESCE(json_group_array(version_id), '[]') FROM ("
            " SELECT version_id FROM provenance p WHERE p.rid=o.rid"
            " AND p.level='%s' AND version_id <> '-' ORDER BY p.ord)" % level
        )
