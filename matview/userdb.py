"""File-level reads with honest instrumentation.

Materialisation reads
    `read_materialisation` decodes every group (planning),
    `group_by_gid` fetches only chosen groups (execution),
    `iter_groups` streams a whole file (finer->coarser fold).
    Each returns/exposes real file evidence (path, size, sha256).

Base fact reads
    `FactTable` is the *only* door to base rows.  Every candidate row that
    SQLite actually examines is counted by a probe UDF evaluated inside the
    query, so "we only read the NULL region" is measured, not asserted.  If a
    query condition makes SQLite fall back to a table scan, the probe counts
    every row and the evidence exposes it.
"""

from __future__ import annotations

import json
import os
import sqlite3
from urllib.parse import quote

from . import pred, stable
from .errors import MaterialisationUnusable

PROBE = "matview_scan_probe"


def connect_ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect("file:%s?mode=ro" % quote(os.path.abspath(path)), uri=True)


def q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# ---------------------------------------------------------------------------
# materialisation decoding
# ---------------------------------------------------------------------------

def read_materialisation(mdir: str, meta: dict, schema: pred.Schema) -> dict:
    """Decode a materialisation file completely.

    Raises MaterialisationUnusable with a stable reason code when the file
    cannot honour its own definition: never a silent skip and never a guess.
    """
    from . import build as buildmod

    data = os.path.join(mdir, meta.get("data_file", "data.sqlite"))
    if not os.path.exists(data):
        raise MaterialisationUnusable("data file missing: %s" % data, "data_file_missing")
    if meta.get("state_version") != buildmod.STATE_VERSION:
        raise MaterialisationUnusable(
            "unsupported state_version %r" % (meta.get("state_version"),),
            "unsupported_state_version",
        )
    if meta.get("field_digest") != schema.field_digest:
        raise MaterialisationUnusable(
            "materialisation was built under a different field schema",
            "schema_mismatch",
        )
    cols = list(meta.get("state_cols") or [])
    if not cols:
        raise MaterialisationUnusable("no declared aggregate state", "missing_aggregate_state")
    if "n" not in cols:
        raise MaterialisationUnusable(
            "no count(*) was saved for this materialisation", "missing_aggregate_state"
        )
    if meta.get("data_sha256") and stable.sha256_file(data) != meta["data_sha256"]:
        raise MaterialisationUnusable(
            "data file changed on disk since it was built", "data_file_corrupt"
        )
    con = connect_ro(data)
    try:
        have = [r[1] for r in con.execute("PRAGMA table_info(groups)")]
        have_set = set(have)
        missing = [c for c in ["key_json"] + cols if c not in have_set]
        if missing:
            raise MaterialisationUnusable(
                "materialisation file lacks required state columns %r" % (missing,),
                "missing_aggregate_state",
            )
        ret = sorted(c[len("retained:"):] for c in have if c.startswith("retained:"))
        extra_present = "extra_json" in have_set
        sel = ", ".join(q(c) for c in ["gid", "key_json"] + cols + ["retained:" + c for c in ret])
        if extra_present:
            sel += ", " + q("extra_json")
        rows = list(con.execute("SELECT %s FROM groups ORDER BY gid" % sel))
    finally:
        con.close()

    nkey = 2
    groups = {}
    state = {}
    retained = {}
    for row in rows:
        gid = row[0]
        key = json.loads(row[1])
        vals = dict(zip(cols, row[nkey:nkey + len(cols)]))
        n = vals.get("n")
        if n is None:
            raise MaterialisationUnusable(
                "stored group lacks count(*)", "missing_aggregate_state"
            )
        st = {"key": key, "n": n}
        for m in meta.get("measures", []):
            if "nn:" + m in vals:
                if "sum:" + m not in vals:
                    raise MaterialisationUnusable(
                        "stored group has a non-null count but no integer sum for %r" % (m,),
                        "missing_aggregate_state",
                    )
                if vals["nn:" + m] < 0 or vals["nn:" + m] > n:
                    raise MaterialisationUnusable(
                        "stored group state is inconsistent for %r" % (m,), "data_file_corrupt"
                    )
            # Absent components are kept as explicit None: the definition
            # deliberately did not save them, and planning must see the gap.
            if "nn:" + m in vals:
                st["nn:" + m] = vals["nn:" + m]
            if "sum:" + m in vals:
                st["sum:" + m] = vals["sum:" + m]
        kd = stable.key_digest(meta["group_cols"], key)
        if kd in state:
            raise MaterialisationUnusable(
                "duplicate group key inside materialisation", "data_file_corrupt"
            )
        state[kd] = st
        rv = {c: json.loads(row[nkey + len(cols) + i]) for i, c in enumerate(ret)}
        retained[kd] = rv
        extras = {}
        if extra_present and row[nkey + len(cols) + len(ret)] is not None:
            extras = json.loads(row[nkey + len(cols) + len(ret)])
        groups[gid] = {"gid": gid, "key": key, "state": st, "retained": rv,
                       "extras": extras}
    return {
        "groups": groups,
        "state": state,
        "retained": retained,
        "physical_rows": len(rows),
        "file": data,
        "size": os.path.getsize(data),
    }


def materialisation_evidence(mdir: str, meta: dict) -> dict:
    data = os.path.join(mdir, meta.get("data_file", "data.sqlite"))
    return {
        "snapshot_id": meta.get("snapshot_id"),
        "mat_id": meta.get("mat_id"),
        "name": meta.get("name"),
        "data_file": data,
        "size": os.path.getsize(data) if os.path.exists(data) else None,
        "sha256": meta.get("data_sha256"),
        "n_groups": meta.get("n_groups"),
        "n_rows_covered": meta.get("n_rows_covered"),
    }


def group_by_gid(data_file: str, gids, cols) -> list:
    """Fetch whole stored rows for the named gid list only."""
    if not gids:
        return []
    con = connect_ro(data_file)
    try:
        sel = ", ".join(q(c) for c in ["gid"] + list(cols))
        sql = "SELECT %s FROM groups WHERE gid IN (%s)" % (
            sel, ",".join("?" * len(gids)),
        )
        return [dict(zip(["gid"] + list(cols), r)) for r in con.execute(sql, tuple(gids))]
    finally:
        con.close()


def iter_groups(data_file: str, cols, batch: int = 256):
    con = connect_ro(data_file)
    try:
        sel = ", ".join(q(c) for c in cols)
        cur = con.execute("SELECT %s FROM groups ORDER BY gid" % sel)
        while True:
            rows = cur.fetchmany(batch)
            if not rows:
                return
            for r in rows:
                yield dict(zip(cols, r))
    finally:
        con.close()


# ---------------------------------------------------------------------------
# base facts
# ---------------------------------------------------------------------------

class FactTable:
    """The instrumented base-table reader.

    `rows_examined` increments for every fact row SQLite feeds to the probe.
    Because the probe is inside the SQL filter, its count reflects the access
    path actually chosen (index search or table scan).
    """

    def __init__(self, path: str, schema: pred.Schema):
        self.path = os.path.abspath(path)
        self.schema = schema
        self.con = connect_ro(path)
        self.names = sorted(schema.fields)
        self.rid = schema.row_id
        self.cols = [self.rid] + self.names
        self.rows_examined = 0
        self.queries = 0
        self.log = []
        self._probe_calls = 0
        self._examined_ids = set()
        self._current_ids = None
        self.con.create_function(PROBE, 1, self._probe)
        self.total_facts = self.con.execute("SELECT COUNT(*) FROM facts").fetchone()[0]

    def _probe(self, rid):
        self._probe_calls += 1
        if self._current_ids is not None:
            self._current_ids.add(rid)
        return 1

    def _row(self, values):
        return dict(zip(self.cols, values))

    def _select_list(self):
        return ", ".join(q(c) for c in self.cols)

    def _run(self, sql, params, purpose, kind):
        """Execute one probe-instrumented query against the fact table.

        `rows_examined` counts **distinct** fact rows SQLite fed to the probe,
        deduplicated across every world query in this execution.  That makes
        it the number of base rows whose values were physically touched, even
        when residual worlds are executed as separate queries.
        """
        self.queries += 1
        self._current_ids = set()
        out = []
        try:
            for vals in self.con.execute(sql, params):
                out.append(self._row(vals))
        finally:
            ids = self._current_ids
            self._current_ids = None
        examined = len(ids)
        self._examined_ids |= ids
        self.rows_examined = len(self._examined_ids)
        self.log.append(
            {
                "purpose": purpose,
                "kind": kind,
                "rows_examined": examined,
                "cumulative_distinct_rows": self.rows_examined,
                "sql": sql,
                "params": list(params) if params is not None else [],
            }
        )
        return out

    def scan_world(self, world, sql, params, purpose):
        """Run exactly one world's narrow query, counting examined rows."""
        # No ORDER BY: ordering would forbid SQLite from using a covering
        # index for a one-sided range and silently turn the read into a scan.
        full = "SELECT %s FROM facts WHERE %s(%s)=1 AND (%s)" % (
            self._select_list(),
            PROBE,
            q(self.rid),
            sql,
        )
        return self._run(full, params, purpose, "world_lookup")

    def scan_all(self, purpose="full_base_scan"):
        """Full base-table scan, every row counted exactly once."""
        sql = "SELECT %s FROM facts WHERE %s(%s)=1" % (
            self._select_list(), PROBE, q(self.rid),
        )
        return self._run(sql, [], purpose, "full_scan")

    def close(self):
        self.con.close()

    def evidence(self) -> dict:
        return {
            "fact_path": self.path,
            "total_facts": self.total_facts,
            "rows_examined": self.rows_examined,
            "queries": self.queries,
            "reads": list(self.log),
        }
