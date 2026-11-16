"""Six-row worked example from the brief, end to end."""

import os
import sys
import unittest
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import fixtures, refimpl, compare  # noqa: E402
from matview import api  # noqa: E402

M1 = {
    "name": "M1",
    "predicate": {"op": "range", "col": "d", "lo": 1, "hi": 3},
    "group_cols": ["d"],
    "measures": ["amount"],
}
M2 = {
    "name": "M2",
    "predicate": {"op": "range", "col": "d", "lo": 3, "hi": 5},
    "group_cols": ["d"],
    "measures": ["amount"],
}


class SixRowDemo(unittest.TestCase):
    def setUp(self):
        self.ws = fixtures.Workspace()
        self.sid = self.ws.build([M1, M2])
        self.query = {
            "group_by": ["d"],
            "measures": fixtures.ALL_MEASURES,
        }

    def test_global_totals_from_materialisations_plus_null_region(self):
        q = {"group_by": [], "measures": fixtures.ALL_MEASURES}
        e = self.ws.explain(self.sid, q)
        (row,) = e["result"]
        self.assertEqual(row["cnt"], 6)
        self.assertEqual(row["nn"], 5)
        self.assertEqual(row["total"], 190)
        self.assertEqual(Fraction(row["mean"]["num"], row["mean"]["den"]), Fraction(38))
        # date 3 appears in both M1 and M2 but is credited once
        mat_gids = [
            (s["mat_id"], s["gid"])
            for s in e["segments"]
            if s["kind"] == "materialisation"
        ]
        self.assertEqual(len(mat_gids), len(set(mat_gids)))
        credits = [
            s for s in e["segments"] if s["kind"] == "materialisation"
        ]
        self.assertEqual(sum(s["rows_credited"] for s in credits), 5)
        # only the NULL-date region is read from base facts
        base = [s for s in e["segments"] if s["kind"] == "base"][0]
        self.assertFalse(base.get("full_base_scan"))
        self.assertEqual(base["rows_admitted"], 1)
        self.assertEqual(base["rows_scanned"], 1)
        self.assertEqual(
            [w["and"] for w in base["worlds"]],
            [
                [{"col": "d", "lo": 6, "hi": None}],
                [{"col": "d", "lo": None, "hi": 0}],
                [{"col": "d", "is_null": True}],
            ],
        )

    def test_null_date_not_lost(self):
        e = self.ws.explain(self.sid, self.query)
        got = {r["group"]["d"]: r for r in e["result"]}
        self.assertIn(None, got)
        self.assertEqual(got[None]["cnt"], 1)
        self.assertEqual(got[None]["total"], 60)
        self.assertEqual(sorted(x for x in got if x is not None), [1, 2, 3, 4, 5])

    def test_date3_not_double_counted(self):
        e = self.ws.explain(self.sid, self.query)
        got = {r["group"]["d"]: r for r in e["result"]}
        self.assertEqual(got[3]["cnt"], 1)
        self.assertEqual(got[3]["total"], 30)
        self.assertEqual(sum(r["cnt"] for r in e["result"]), 6)
        self.assertEqual(sum(r["total"] or 0 for r in e["result"]), 190)

    def test_matches_independent_reference(self):
        for q in [
            self.query,
            {"group_by": [], "measures": fixtures.ALL_MEASURES},
            {"group_by": ["d"], "measures": [{"kind": "count_star", "as": "c"}]},
        ]:
            prod = self.ws.execute(self.sid, q)["rows"]
            ref = refimpl.reference_answer(
                self.ws.fact_path, self.ws.fields(), q
            )
            problems = compare.compare(prod, ref, q)
            self.assertEqual(problems, [], msg="\n".join(problems))


if __name__ == "__main__":
    unittest.main()
