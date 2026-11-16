"""Group granularity, aggregation-state sufficiency, and duplicate facts."""

import os
import sys
import unittest
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import fixtures, refimpl, compare  # noqa: E402


def mat(name, predicate, group_cols, measures=("amount",), **kw):
    spec = {
        "name": name,
        "predicate": predicate,
        "group_cols": list(group_cols),
        "measures": list(measures),
    }
    spec.update(kw)
    return spec


def assert_matches(test, ws, sid, query):
    prod = ws.execute(sid, query)["rows"]
    ref = refimpl.reference_answer(ws.fact_path, ws.fields(), query)
    problems = compare.compare(prod, ref, query)
    test.assertEqual(problems, [], msg="\n".join(problems))
    return prod


class FinerToCoarser(unittest.TestCase):
    def setUp(self):
        self.ws = fixtures.Workspace()
        self.sid = self.ws.build(
            [
                mat("FINE", {"op": "range", "col": "d", "lo": 1, "hi": 5}, ["d"]),
            ]
        )

    def test_fold_date_groups_to_global(self):
        q = {"group_by": [], "measures": fixtures.ALL_MEASURES}
        e = self.ws.explain(self.sid, q)
        prod = assert_matches(self, self.ws, self.sid, q)
        # five finer date groups (d=1..5) fold into the global row; the NULL
        # date comes from the base residual
        self.assertEqual(prod[0]["cnt"], 6)
        self.assertEqual(prod[0]["nn"], 5)
        self.assertEqual(prod[0]["total"], 190)
        self.assertEqual(Fraction(prod[0]["mean"]["num"], prod[0]["mean"]["den"]),
                         Fraction(38))
        self.assertEqual(e["coverage"]["materialisation_groups"], 5)
        # 5 finer groups credited, no base scan
        self.assertEqual(e["evidence"]["base_rows_scanned"], 1)  # only NULL date

    def test_cannot_fold_partial_region(self):
        # query restricted to d=2..3: the d=2 and d=3 groups are inside, but
        # d=1/4/5 groups are outside, so only two groups may be used.
        q = {
            "group_by": [],
            "where": {"op": "range", "col": "d", "lo": 2, "hi": 3},
            "measures": fixtures.ALL_MEASURES,
        }
        e = self.ws.explain(self.sid, q)
        prod = assert_matches(self, self.ws, self.sid, q)
        self.assertEqual(prod[0]["cnt"], 2)
        self.assertEqual(prod[0]["total"], 30)
        self.assertEqual(
            Fraction(prod[0]["mean"]["num"], prod[0]["mean"]["den"]), Fraction(30)
        )


class AggregateStateRequirements(unittest.TestCase):
    def setUp(self):
        self.ws = fixtures.Workspace()
        # A materialisation that saved only count(*) and a final average.
        self.sid = self.ws.build(
            [
                mat(
                    "AVGONLY",
                    {"op": "range", "col": "d", "lo": 1, "hi": 5},
                    ["d"],
                    state={
                        "skip": ["sum:amount", "nn:amount"],
                        "extra": {"avg:amount": True},
                    },
                )
            ]
        )

    def test_stored_average_not_mergeable(self):
        q = {"group_by": [], "measures": [{"kind": "avg", "col": "amount", "as": "mean"}]}
        e = self.ws.explain(self.sid, q)
        rep = e["materialisations"][0]
        self.assertFalse(rep["usable"])
        self.assertEqual(rep["reason"], "missing_aggregate_state")
        prod = assert_matches(self, self.ws, self.sid, q)
        # correct global average, computed without the stored per-day averages
        self.assertEqual(
            Fraction(prod[0]["mean"]["num"], prod[0]["mean"]["den"]), Fraction(38)
        )

    def test_average_of_averages_is_not_global_average(self):
        # days have different non-null counts, so mean(day means) != global mean
        q = {"group_by": ["d"], "measures": [{"kind": "avg", "col": "amount", "as": "mean"}]}
        prod = assert_matches(self, self.ws, self.sid, q)
        got = {r["group"]["d"]: r for r in prod}
        self.assertEqual(
            Fraction(got[1]["mean"]["num"], got[1]["mean"]["den"]), Fraction(10)
        )

    def test_count_star_still_available(self):
        q = {"group_by": [], "measures": [{"kind": "count_star", "as": "c"}]}
        e = self.ws.explain(self.sid, q)
        rep = e["materialisations"][0]
        self.assertTrue(rep["usable"])
        prod = assert_matches(self, self.ws, self.sid, q)
        # the whole d=1..5 region is covered, only the NULL date is from base
        self.assertEqual(prod[0]["c"], 6)
        self.assertEqual(e["evidence"]["base_rows_scanned"], 1)

    def test_count_x_is_not_count_star(self):
        q = {
            "group_by": [],
            "measures": [
                {"kind": "count_star", "as": "c"},
                {"kind": "count", "col": "amount", "as": "nn"},
            ],
        }
        e = self.ws.explain(self.sid, q)
        rep = e["materialisations"][0]
        # non-null counts were not saved, so count(amount) must come from base
        self.assertFalse(rep["usable"])
        self.assertEqual(rep["reason"], "missing_aggregate_state")
        prod = assert_matches(self, self.ws, self.sid, q)
        self.assertEqual(prod[0]["c"], 6)
        self.assertEqual(prod[0]["nn"], 5)


class DuplicateFacts(unittest.TestCase):
    def setUp(self):
        self.ws = fixtures.Workspace(
            schema=fixtures.DUP_SCHEMA, rows=fixtures.DUP_ROWS
        )

    def test_same_business_values_are_separate_rows(self):
        sid = self.ws.build(
            [
                mat("ALL", {"op": "and", "args": []}, ["d"]),
            ]
        )
        q = {"group_by": ["d"], "measures": fixtures.ALL_MEASURES}
        e = self.ws.explain(sid, q)
        prod = assert_matches(self, self.ws, sid, q)
        got = {r["group"]["d"]: r for r in prod}
        self.assertEqual(got[7]["cnt"], 3)
        self.assertEqual(got[7]["nn"], 3)
        self.assertEqual(got[7]["total"], 120)
        self.assertEqual(sum(r["cnt"] for r in prod), 9)
        self.assertEqual(sum(r["total"] or 0 for r in prod), 310)

    def test_row_id_identity_respected_under_materialisation(self):
        sid = self.ws.build(
            [
                mat("M1", {"op": "range", "col": "d", "lo": 1, "hi": 5}, ["d"]),
            ]
        )
        q = {"group_by": [], "measures": fixtures.ALL_MEASURES}
        prod = assert_matches(self, self.ws, sid, q)
        self.assertEqual(prod[0]["cnt"], 9)
        self.assertEqual(prod[0]["nn"], 8)
        self.assertEqual(prod[0]["total"], 310)


class GranularityRejections(unittest.TestCase):
    def test_missing_required_measure_state(self):
        ws = fixtures.Workspace()
        sid = ws.build(
            [
                mat(
                    "NO_SUM",
                    {"op": "range", "col": "d", "lo": 1, "hi": 5},
                    ["d"],
                    state={"skip": ["sum:amount"]},
                )
            ]
        )
        q = {"group_by": [], "measures": [{"kind": "sum", "col": "amount", "as": "s"}]}
        e = ws.explain(sid, q)
        rep = e["materialisations"][0]
        self.assertEqual(rep["reason"], "missing_aggregate_state")
        prod = assert_matches(self, ws, sid, q)
        self.assertEqual(prod[0]["s"], 190)

    def test_unknown_field_in_definition(self):
        ws = fixtures.Workspace()
        # tamper with a stored definition after the build to reference a field
        # that is not in the schema
        sid = ws.build(
            [mat("M1", {"op": "range", "col": "d", "lo": 1, "hi": 5}, ["d"])]
        )
        cat = __import__("matview.catalog", fromlist=["Catalogue"]).Catalogue(ws.catalogue)
        regs = cat.registrations(sid)
        mid = regs[0]["mat_id"]
        mpath = os.path.join(cat.mat_dir(sid, mid), "MAT.json")
        import json

        with open(mpath) as f:
            meta = json.load(f)
        meta["group_cols"] = ["d", "nosuch"]
        with open(mpath, "w") as f:
            json.dump(meta, f)
        e = ws.explain(sid, {"group_by": [], "measures": [{"kind": "count_star", "as": "c"}]})
        rep = e["materialisations"][0]
        self.assertFalse(rep["usable"])
        self.assertEqual(rep["reason"], "unknown_field")


if __name__ == "__main__":
    unittest.main()
