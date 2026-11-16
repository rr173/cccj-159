"""End-to-end checks that reuse is real, not a relabelled full scan.

Each test builds the same six-row dataset and checks a property that a
"correct answer, but always full scan" implementation would fail.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import fixtures  # noqa: E402
from matview import api  # noqa: E402

M1 = {"name": "M1", "predicate": {"op": "range", "col": "d", "lo": 1, "hi": 3},
      "group_cols": ["d"], "measures": ["amount"]}
M2 = {"name": "M2", "predicate": {"op": "range", "col": "d", "lo": 3, "hi": 5},
      "group_cols": ["d"], "measures": ["amount"]}
M_ALL = {"name": "ALL", "predicate": {"op": "and", "args": []},
         "group_cols": ["d"], "measures": ["amount"]}
GLOBAL = {"group_by": [], "measures": [
    {"kind": "count_star", "as": "cnt"},
    {"kind": "count", "col": "amount", "as": "nn"},
    {"kind": "sum", "col": "amount", "as": "total"},
    {"kind": "avg", "col": "amount", "as": "mean"},
]}


class ReuseIsReal(unittest.TestCase):
    def setUp(self):
        self.ws = fixtures.Workspace()

    def test_global_query_credits_five_groups_from_files(self):
        sid = self.ws.build([M1, M2])
        e = self.ws.explain(sid, GLOBAL)
        credited = [s for s in e["segments"] if s["kind"] == "materialisation"]
        self.assertEqual(len(credited), 5)
        self.assertEqual(sum(s["rows_credited"] for s in credited), 5)
        self.assertEqual(e["evidence"]["base_rows_scanned"], 1)

    def test_full_materialisation_scans_zero_base_rows(self):
        sid = self.ws.build([M_ALL])
        e = self.ws.explain(sid, GLOBAL)
        self.assertEqual(e["evidence"]["base_rows_scanned"], 0)
        # ALL is grouped by d, so the NULL-date row is still a base residual:
        # its two worlds (d<=0, d>=6) examine zero rows because no fact matches
        for read in e["evidence"]["fact_table"]["reads"]:
            self.assertEqual(read["rows_examined"], 0)
        self.assertEqual(e["result"][0]["cnt"], 6)

    def test_single_materialisation_query_touches_one_file_once(self):
        sid = self.ws.build([M1])
        e = self.ws.explain(sid, {
            "group_by": ["d"],
            "where": {"op": "range", "col": "d", "lo": 1, "hi": 3},
            "measures": [{"kind": "sum", "col": "amount", "as": "s"}],
        })
        reads = e["evidence"]["materialisation_reads"]
        self.assertEqual(len(reads), 3)  # three groups, each fetched once
        self.assertEqual(e["evidence"]["base_rows_scanned"], 0)

    def test_no_materialisation_forces_full_scan(self):
        sid = self.ws.build([M1], register=False)
        e = self.ws.explain(sid, GLOBAL)
        self.assertEqual(e["evidence"]["base_rows_scanned"], 6)
        self.assertTrue(e["evidence"]["fact_table"]["queries"] >= 1)

    def test_materialisation_files_are_truly_opened(self):
        sid = self.ws.build([M1])
        e = self.ws.explain(sid, GLOBAL)
        for read in e["evidence"]["materialisation_reads"]:
            self.assertTrue(os.path.exists(read["data_file"]))
            with open(read["data_file"], "rb") as f:
                self.assertEqual(f.read(16), b"SQLite format 3\x00")


if __name__ == "__main__":
    unittest.main()
