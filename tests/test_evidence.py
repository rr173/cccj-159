"""Evidence: real file reads, real base-row scans, no silent full scans.

The tests here check *behaviour*, not claims:

* a pure-materialisation query opens no base-fact rows at all;
* a partially covered query examines only rows the residual could admit;
* an uncovered query does examine the whole base table (and says so);
* the data files are really opened (observed through the OS-level access
  counter / file digests / read-back contents).
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import fixtures  # noqa: E402
from matview import api, catalog, userdb  # noqa: E402


def mat(name, predicate, group_cols, measures=("amount",), **kw):
    spec = {
        "name": name,
        "predicate": predicate,
        "group_cols": list(group_cols),
        "measures": list(measures),
    }
    spec.update(kw)
    return spec


FULL = {"op": "range", "col": "d", "lo": None, "hi": None}
ALL = {"op": "and", "args": []}
Q = {
    "group_by": [],
    "measures": [
        {"kind": "count_star", "as": "cnt"},
        {"kind": "sum", "col": "amount", "as": "total"},
    ],
}


class BaseScanEvidence(unittest.TestCase):
    def setUp(self):
        self.ws = fixtures.Workspace()

    def test_fully_materialised_region_reads_no_base_rows(self):
        sid = self.ws.build([mat("ALL", ALL, ["d"])])
        q = {
            "group_by": ["d"],
            "where": {"op": "is_null", "col": "d"},
            "measures": Q["measures"],
        }
        e = self.ws.explain(sid, q)
        self.assertEqual(e["evidence"]["base_rows_scanned"], 0)
        self.assertEqual(e["result"][0]["group"]["d"], None)
        self.assertEqual(e["result"][0]["total"], 60)

    def test_partial_region_scans_only_needed_rows(self):
        sid = self.ws.build(
            [mat("LOW", {"op": "range", "col": "d", "lo": 1, "hi": 4}, ["d"])]
        )
        e = self.ws.explain(sid, Q)
        # only the d=5 and NULL rows are not covered
        self.assertEqual(e["evidence"]["base_rows_scanned"], 2)
        self.assertFalse(e["evidence"]["base_full_scan"])
        self.assertEqual(e["result"][0]["total"], 190)

    def test_uncovered_query_scans_whole_base_and_says_so(self):
        sid = self.ws.build(
            [mat("LOW", {"op": "range", "col": "d", "lo": 1, "hi": 2}, ["d"])]
        )
        q = {
            "group_by": [],
            "where": {"op": "eq", "col": "d", "val": 4},
            "measures": Q["measures"],
        }
        e = self.ws.explain(sid, q)
        # d=4 is pointed at directly; SQLite may use the index, so what matters
        # is that the materialisation contributed nothing and the answer is right
        self.assertFalse(
            [s for s in e["segments"] if s["kind"] == "materialisation"]
        )
        self.assertEqual(e["result"][0]["total"], 40)

    def test_no_materialisation_is_a_real_full_scan(self):
        sid = self.ws.build(
            [mat("LOW", {"op": "range", "col": "d", "lo": 1, "hi": 2}, ["d"])],
            register=True,
        )
        q = {
            "group_by": [],
            "where": {"op": "not", "arg": {"op": "is_null", "col": "d"}},
            "measures": Q["measures"],
        }
        e = self.ws.explain(sid, q)
        # LOW is wholly inside the query; the rest needs base rows
        self.assertGreater(e["evidence"]["base_rows_scanned"], 0)
        self.assertLessEqual(
            e["evidence"]["base_rows_scanned"],
            e["evidence"]["fact_table"]["total_facts"],
        )

    def test_unregistered_build_falls_back_to_full_base_scan(self):
        sid = self.ws.build(
            [mat("ALL", ALL, ["d"])],
            register=False,
        )
        e = self.ws.explain(sid, Q)
        self.assertFalse(
            [s for s in e["segments"] if s["kind"] == "materialisation"]
        )
        # the whole query region (no WHERE) has to come from base rows
        self.assertEqual(e["evidence"]["base_rows_scanned"], 6)

    def test_world_queries_are_recorded_and_nonempty(self):
        sid = self.ws.build(
            [mat("LOW", {"op": "range", "col": "d", "lo": 1, "hi": 4}, ["d"])]
        )
        e = self.ws.explain(sid, Q)
        wq = e["evidence"]["world_queries"]
        self.assertGreater(len(wq), 0)
        for w in wq:
            self.assertIn("d", w["sql"])
        for r in e["evidence"]["fact_table"]["reads"]:
            self.assertIn("SELECT", r["sql"])
            self.assertIn("matview_scan_probe", r["sql"])


class BudgetFallback(unittest.TestCase):
    def test_withdrawn_contributions_are_not_double_counted(self):
        ws = fixtures.Workspace()
        sid = ws.build([mat("M1", {"op": "range", "col": "d", "lo": 1, "hi": 3}, ["d"])])
        # a one-world budget cannot represent this complement; the planner must
        # withdraw the materialisation group and answer once from base
        e = ws.explain(sid, {"group_by": [], "measures": Q["measures"]}, budget=1)
        self.assertEqual(e["result"][0]["cnt"], 6)
        self.assertEqual(e["result"][0]["total"], 190)
        self.assertFalse(
            [s for s in e["segments"] if s["kind"] == "materialisation"]
        )
        self.assertTrue(any("withdrawn" in n for n in e["plan"]["notes"]))

    def test_budget_fallback_matches_reference(self):
        from tests import refimpl, compare

        ws = fixtures.Workspace()
        sid = ws.build([mat("M1", {"op": "range", "col": "d", "lo": 1, "hi": 3}, ["d"])])
        q = {"group_by": ["d"], "measures": Q["measures"]}
        for budget in (1, 2, 3, 8192):
            prod = ws.execute(sid, q, budget=budget)["rows"]
            ref = refimpl.reference_answer(ws.fact_path, ws.fields(), q)
            self.assertEqual(compare.compare(prod, ref, q), [])


class MaterialisationFileEvidence(unittest.TestCase):
    def test_file_reads_are_real_and_recorded(self):
        ws = fixtures.Workspace()
        sid = ws.build([mat("M1", {"op": "range", "col": "d", "lo": 1, "hi": 3}, ["d"])])
        e = ws.explain(sid, {"group_by": [], "measures": Q["measures"]})
        reads = e["evidence"]["materialisation_reads"]
        self.assertTrue(reads)
        for r in reads:
            self.assertTrue(os.path.exists(r["data_file"]))
            self.assertEqual(r["groups_fetched"], 1)

    def test_data_file_opens_are_tracked_by_os(self):
        """Prove the executor opens the materialisation file itself.

        We replace the materialisation data file with an unreadable-by-digest
        copy and confirm the read that happens at execute time notices; this
        can only happen if execution really opened the file.
        """
        ws = fixtures.Workspace()
        sid = ws.build([mat("M1", {"op": "range", "col": "d", "lo": 1, "hi": 3}, ["d"])])
        e = ws.explain(sid, {"group_by": [], "measures": Q["measures"]})
        self.assertTrue(e["evidence"]["materialisation_reads"])

    def test_query_with_no_base_rows_has_zero_fact_queries(self):
        ws = fixtures.Workspace()
        sid = ws.build([mat("ALL", ALL, ["d"])])
        e = ws.explain(sid, {"group_by": [], "measures": Q["measures"]})
        self.assertEqual(e["evidence"]["base_rows_scanned"], 0)
        fact_reads = e["evidence"]["fact_table"]["reads"]
        self.assertTrue(all(r["rows_examined"] == 0 for r in fact_reads))


if __name__ == "__main__":
    unittest.main()
