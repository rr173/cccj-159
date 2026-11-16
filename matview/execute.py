"""Execute a plan: materialised contributions first, then the base residual.

Execution is deliberately dumb; all the proof lives in `plan.py`.

* materialisation segments are credited from their stored state — the executor
  fetches only the selected gids from the data file (real file reads, recorded)
  and never touches the base fact table;
* the base segment is answered by running each residual world as a narrow
  SQL condition on the fact table, with a probe UDF counting every fact row
  SQLite actually examines.

Base-row filtering is done *both* by exact SQL world conditions and by a
re-evaluation of the canonical three-valued residual predicate on each scanned
row.  The two are independent checks of the same row set; a disagreement would
be a bug, and the executor records the world queries so a test can inspect
them.
"""

from __future__ import annotations

from . import pred, region
from .query import GroupAcc, result_rows
from .userdb import FactTable, group_by_gid


class Execution:
    def __init__(self, catalogue, plan):
        self.catalogue = catalogue
        self.plan = plan
        self.accs = {}
        self.segments_info = []
        self.fact = None
        self.base_rows_scanned = 0
        self.base_rows_admitted = 0
        self.materialisation_reads = []
        self.world_queries = []
        self.contribution_keys = []
        self.errors = []

    # -- main --------------------------------------------------------------

    def run(self):
        config = self.plan.query
        measures = config["measures"]
        schema = self.catalogue.schema()
        for seg in self.plan.segments:
            if seg.kind == "materialisation":
                self._run_materialisation(seg, config)
            elif seg.kind == "base":
                self._run_base(seg, config, schema)
        if self.fact is not None:
            self.fact.close()
            self.base_rows_scanned = self.fact.rows_examined
            self.fact_evidence = self.fact.evidence()
        else:
            self.fact_evidence = {
                "fact_path": self.plan.fact_path,
                "total_facts": None,
                "rows_examined": 0,
                "queries": 0,
                "reads": [],
            }
        rows = result_rows(self.accs, config)
        return {
            "snapshot_id": self.plan.snapshot_id,
            "query": {
                "group_by": list(config["group_cols"]),
                "where": config["predicate_json"],
                "measures": [m.as_json() for m in config["measures"]],
                "fingerprint": config["fingerprint"],
            },
            "rows": rows,
            "segments": self.segments_info,
            "execution": {
                "snapshot_id": self.plan.snapshot_id,
                "query_fingerprint": config["fingerprint"],
                "materialisation_reads": self.materialisation_reads,
                "fact_table": self.fact_evidence,
                "base_rows_scanned": self.base_rows_scanned,
                "base_rows_admitted": self.base_rows_admitted,
                "base_full_scan": any(
                    s.get("kind") == "base" and s.get("full_base_scan")
                    for s in self.segments_info
                ),
                "materialisation_contributions": len(
                    [s for s in self.segments_info if s["kind"] == "materialisation"]
                ),
                "world_queries": self.world_queries,
            },
            "coverage": self.plan.coverage,
        }

    # -- materialisation branch -------------------------------------------

    def _acc(self, key):
        acc = self.accs.get(key)
        if acc is None:
            acc = GroupAcc(self.plan.query["measures"])
            self.accs[key] = acc
        return acc

    def _run_materialisation(self, seg, config):
        meta = self.catalogue.mat_meta(self.plan.snapshot_id, seg.mat_id)
        cols = list(meta.get("state_cols") or [])
        rows = group_by_gid(seg.data_file, [seg.gid], cols)
        if len(rows) != 1:
            self.errors.append(
                {"gid": seg.gid, "error": "materialisation group disappeared at execution"}
            )
            raise RuntimeConfirmError(
                "group %s of %s vanished between planning and execution"
                % (seg.gid, seg.mat_id)
            )
        stored = rows[0]
        acc = self._acc(tuple(seg.output_key))
        acc.add_state(seg.state, config)
        self.materialisation_reads.append(
            {
                "mat_id": seg.mat_id,
                "name": seg.name,
                "gid": seg.gid,
                "data_file": seg.data_file,
                "groups_fetched": 1,
                "rows_credited": seg.state.get("n", 0),
            }
        )
        self.segments_info.append(
            {
                "kind": "materialisation",
                "mat_id": seg.mat_id,
                "name": seg.name,
                "gid": seg.gid,
                "group_key": list(seg.key),
                "output_group": list(seg.output_key),
                "rows_credited": seg.state.get("n", 0),
                "state": {k: v for k, v in sorted(seg.state.items()) if k != "key"},
                "region": region.describe_region(seg.worlds),
            }
        )

    # -- base branch -------------------------------------------------------

    def _run_base(self, seg, config, schema):
        if seg.worlds is None:
            self._full_scan(seg, config, schema)
            return
        if not seg.worlds:
            self.segments_info.append(
                {
                    "kind": "base",
                    "reason": seg.reason,
                    "worlds": [],
                    "rows_scanned": 0,
                }
            )
            return
        ft = self._fact_table(schema)
        residual_pred = seg.residual_pred
        total_scanned = 0
        admitted = 0
        for w in seg.worlds:
            sql, params = region.world_sql(w)
            got = ft.scan_world(w, sql, params, purpose="base residual world")
            total_scanned += ft.log[-1]["rows_examined"]
            for row in got:
                if pred.admitted(residual_pred, row):
                    admitted += 1
                    self._credit_base_row(row, config)
            self.world_queries.append(
                {"sql": sql, "params": params, "rows_examined": ft.log[-1]["rows_examined"]}
            )
        self.base_rows_admitted += admitted
        self.segments_info.append(
            {
                "kind": "base",
                "reason": seg.reason,
                "worlds": region.describe_region(seg.worlds),
                "world_count": len(seg.worlds),
                "rows_scanned": total_scanned,
                "rows_admitted": admitted,
                "residual": pred.to_json(residual_pred),
            }
        )

    def _full_scan(self, seg, config, schema):
        ft = self._fact_table(schema)
        rows = ft.scan_all("full base scan")
        admitted = 0
        residual_pred = seg.residual_pred
        for row in rows:
            if pred.admitted(residual_pred, row):
                admitted += 1
                self._credit_base_row(row, config)
        self.base_rows_admitted += admitted
        self.segments_info.append(
            {
                "kind": "base",
                "reason": seg.reason,
                "worlds": None,
                "full_base_scan": True,
                "rows_scanned": ft.log[-1]["rows_examined"],
                "rows_admitted": admitted,
                "residual": pred.to_json(residual_pred),
            }
        )

    def _credit_base_row(self, row, config):
        key = tuple(row.get(c) for c in config["group_cols"])
        acc = self._acc(key)
        acc.add_row(row, config)

    def _fact_table(self, schema):
        if self.fact is None:
            self.fact = FactTable(self.plan.fact_path, schema)
        return self.fact


class RuntimeConfirmError(RuntimeError):
    """A plan invariant failed while reading stored state."""
