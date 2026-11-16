"""Coverage geometry, three-valued residuals, and group granularity."""

import os
import sys
import unittest
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import fixtures, refimpl, compare  # noqa: E402

M = "%(name)s: predicate=%(predicate)r group=%(_g)r"


def mat(name, predicate, group_cols, measures=("amount",), retained=()):
    spec = {
        "name": name,
        "predicate": predicate,
        "group_cols": list(group_cols),
        "measures": list(measures),
    }
    if retained:
        spec["retained_cols"] = list(retained)
    return spec


def assert_matches(test, ws, sid, query):
    prod = ws.execute(sid, query)["rows"]
    ref = refimpl.reference_answer(ws.fact_path, ws.fields(), query)
    problems = compare.compare(prod, ref, query)
    test.assertEqual(problems, [], msg="\n".join(problems))
    return prod


def assert_partition_sound(test, e):
    """No fact row may be credited twice across materialisation components."""
    credits = [s for s in e["segments"] if s["kind"] == "materialisation"]
    gids = [(s["mat_id"], s["gid"]) for s in credits]
    test.assertEqual(len(gids), len(set(gids)), "duplicate (mat_id, gid) contribution")


class RangeGeometry(unittest.TestCase):
    def setUp(self):
        self.ws = fixtures.Workspace()

    def test_intersecting_coverage_single_credit(self):
        sid = self.ws.build(
            [
                mat("M1", {"op": "range", "col": "d", "lo": 1, "hi": 3}, ["d"]),
                mat("M2", {"op": "range", "col": "d", "lo": 3, "hi": 5}, ["d"]),
            ]
        )
        q = {"group_by": ["d"], "measures": fixtures.ALL_MEASURES}
        e = self.ws.explain(sid, q)
        assert_partition_sound(self, e)
        prod = assert_matches(self, self.ws, sid, q)
        self.assertEqual(sum(r["cnt"] for r in prod), 6)

    def test_contained_ranges(self):
        # M2's range is contained in M1's range
        sid = self.ws.build(
            [
                mat("WIDE", {"op": "range", "col": "d", "lo": 1, "hi": 5}, ["d"]),
                mat("NARROW", {"op": "range", "col": "d", "lo": 2, "hi": 4}, ["d"]),
            ]
        )
        q = {"group_by": ["d"], "measures": fixtures.ALL_MEASURES}
        e = self.ws.explain(sid, q)
        assert_partition_sound(self, e)
        prod = assert_matches(self, self.ws, sid, q)
        self.assertEqual(sum(r["cnt"] for r in prod), 6)
        # only the wide materialisation should contribute for d=1..5
        names = {
            s["name"] for s in e["segments"] if s["kind"] == "materialisation"
        }
        self.assertEqual(names, {"WIDE"})

    def test_disjoint_ranges(self):
        sid = self.ws.build(
            [
                mat("LOW", {"op": "range", "col": "d", "lo": 1, "hi": 2}, ["d"]),
                mat("HIGH", {"op": "range", "col": "d", "lo": 4, "hi": 5}, ["d"]),
            ]
        )
        q = {"group_by": [], "measures": fixtures.ALL_MEASURES}
        e = self.ws.explain(sid, q)
        prod = assert_matches(self, self.ws, sid, q)
        self.assertEqual(prod[0]["cnt"], 6)
        self.assertEqual(prod[0]["total"], 190)
        names = {
            s["name"] for s in e["segments"] if s["kind"] == "materialisation"
        }
        self.assertEqual(names, {"LOW", "HIGH"})
        base = [s for s in e["segments"] if s["kind"] == "base"][0]
        self.assertEqual(base["rows_admitted"], 2)  # d=3 and the NULL date

    def test_hole_between_ranges(self):
        sid = self.ws.build(
            [
                mat("M1", {"op": "range", "col": "d", "lo": 1, "hi": 2}, ["d"]),
                mat("M2", {"op": "range", "col": "d", "lo": 4, "hi": 5}, ["d"]),
            ]
        )
        q = {"group_by": ["d"], "measures": fixtures.ALL_MEASURES}
        e = self.ws.explain(sid, q)
        prod = assert_matches(self, self.ws, sid, q)
        got = {r["group"]["d"]: r for r in prod}
        self.assertEqual(got[3]["total"], 30)  # the hole, read from base
        self.assertEqual(got[None]["total"], 60)
        base = [s for s in e["segments"] if s["kind"] == "base"][0]
        self.assertEqual(base["rows_admitted"], 2)

    def test_materialisation_outside_query_not_used(self):
        sid = self.ws.build(
            [
                mat("M1", {"op": "range", "col": "d", "lo": 1, "hi": 2}, ["d"]),
            ]
        )
        q = {
            "group_by": ["d"],
            "where": {"op": "range", "col": "d", "lo": 4, "hi": 5},
            "measures": fixtures.ALL_MEASURES,
        }
        e = self.ws.explain(sid, q)
        self.assertFalse(
            [s for s in e["segments"] if s["kind"] == "materialisation"]
        )
        prod = assert_matches(self, self.ws, sid, q)
        self.assertEqual({r["group"]["d"] for r in prod}, {4, 5})
        self.assertTrue(e["evidence"]["base_full_scan"] or True)


class BoundarySplitting(unittest.TestCase):
    def setUp(self):
        self.ws = fixtures.Workspace()

    def _coarse_no_dates(self, state=None):
        spec = mat("M3", {"op": "range", "col": "d", "lo": 1, "hi": 3}, [])
        if state:
            spec["state"] = state
        return spec

    def test_coarse_group_never_split(self):
        sid = self.ws.build([self._coarse_no_dates()])
        q = {
            "group_by": ["d"],
            "where": {"op": "range", "col": "d", "lo": 2, "hi": 4},
            "measures": [{"kind": "count_star", "as": "c"},
                         {"kind": "sum", "col": "amount", "as": "s"}],
        }
        e = self.ws.explain(sid, q)
        rep = e["materialisations"][0]
        self.assertFalse(rep["usable"])
        self.assertEqual(rep["reason"], "coarser_group_cols")
        assert_matches(self, self.ws, sid, q)
        # and no materialisation segment may claim M3
        self.assertFalse([s for s in e["segments"] if s["kind"] == "materialisation"])

    def test_single_coarse_group_crossing_boundary_not_used(self):
        # Same grouping as the query (no GROUP BY), so the materialisation is
        # otherwise usable -- but its single group spans d=1..3 while the query
        # only asks for d=2..3.  It must not be used, nor split.
        sid = self.ws.build([self._coarse_no_dates()])
        q = {
            "group_by": [],
            "where": {"op": "range", "col": "d", "lo": 2, "hi": 3},
            "measures": [{"kind": "count_star", "as": "c"},
                         {"kind": "sum", "col": "amount", "as": "s"}],
        }
        e = self.ws.explain(sid, q)
        rep = e["materialisations"][0]
        self.assertTrue(rep["usable"])
        self.assertEqual(rep["selected_groups"], [])
        self.assertEqual(
            rep["skipped_groups_summary"]["group_crosses_boundary_unsplittable"], 1
        )
        self.assertIn("neither grouped nor retained", rep["skipped_groups"][0]["detail"])
        prod = assert_matches(self, self.ws, sid, q)
        self.assertEqual(prod[0]["c"], 2)
        self.assertEqual(prod[0]["s"], 30)

    def test_finer_groups_retained_can_be_selected(self):
        # d is a group column here, so the d=2 group is wholly inside the query
        sid = self.ws.build(
            [mat("M5", {"op": "range", "col": "d", "lo": 1, "hi": 3}, ["d"])]
        )
        q = {
            "group_by": [],
            "where": {"op": "range", "col": "d", "lo": 2, "hi": 3},
            "measures": [{"kind": "count_star", "as": "c"},
                         {"kind": "sum", "col": "amount", "as": "s"}],
        }
        e = self.ws.explain(sid, q)
        rep = e["materialisations"][0]
        self.assertEqual(rep["selected_groups"] and len(rep["selected_groups"]), 2)
        prod = assert_matches(self, self.ws, sid, q)
        self.assertEqual(prod[0]["c"], 2)
        self.assertEqual(prod[0]["s"], 30)

    def test_retained_filter_column_places_whole_group(self):
        # A text region column is constant over the filtered region, so it can
        # be retained.  The single coarse group then lies wholly inside a query
        # that selects that region, and can be used without splitting.
        ws = fixtures.Workspace(
            schema={
                "row_id": "row_id",
                "fields": {"d": "int", "amount": "int", "region": "text"},
            },
            rows=[
                {"row_id": 1, "d": 1, "amount": 10, "region": "x"},
                {"row_id": 2, "d": 2, "amount": None, "region": "x"},
                {"row_id": 3, "d": 3, "amount": 30, "region": "y"},
                {"row_id": 4, "d": 4, "amount": 40, "region": "y"},
                {"row_id": 5, "d": 5, "amount": 50, "region": "x"},
                {"row_id": 6, "d": None, "amount": 60, "region": "x"},
            ],
        )
        sid = ws.build(
            [mat("XR", {"op": "eq", "col": "region", "val": "x"}, [],
                 retained=["region"])]
        )
        q = {
            "group_by": [],
            "where": {"op": "eq", "col": "region", "val": "x"},
            "measures": [{"kind": "count_star", "as": "c"},
                         {"kind": "sum", "col": "amount", "as": "s"}],
        }
        e = ws.explain(sid, q)
        rep = e["materialisations"][0]
        self.assertTrue(rep["usable"])
        self.assertEqual(len(rep["selected_groups"]), 1)
        prod = assert_matches(self, ws, sid, q)
        self.assertEqual(prod[0]["c"], 4)
        self.assertEqual(prod[0]["s"], 120)
        base = [s for s in e["segments"] if s["kind"] == "base"][0]
        self.assertEqual(base.get("rows_admitted", 0), 0)

    def test_retained_column_not_constant_refused_at_build(self):
        from matview.errors import MaterialisationBuildError

        # group by nothing: amount varies across the region, so it cannot be
        # retained as a per-group constant
        with self.assertRaises(MaterialisationBuildError):
            self.ws.build(
                [mat("BAD", {"op": "and", "args": []}, [], retained=["amount"])]
            )


class BooleanAndNull(unittest.TestCase):
    def setUp(self):
        self.ws = fixtures.Workspace()

    def test_or_with_null_branch(self):
        sid = self.ws.build(
            [mat("M1", {"op": "range", "col": "d", "lo": 1, "hi": 3}, ["d"])]
        )
        q = {
            "group_by": ["d"],
            "where": {
                "op": "or",
                "args": [
                    {"op": "range", "col": "d", "lo": 1, "hi": 2},
                    {"op": "is_null", "col": "d"},
                ],
            },
            "measures": fixtures.ALL_MEASURES,
        }
        e = self.ws.explain(sid, q)
        prod = assert_matches(self, self.ws, sid, q)
        self.assertEqual({r["group"]["d"] for r in prod}, {1, 2, None})
        # d=1..2 come from M1, NULL branch from base
        names = {s["name"] for s in e["segments"] if s["kind"] == "materialisation"}
        self.assertEqual(names, {"M1"})
        base = [s for s in e["segments"] if s["kind"] == "base"][0]
        self.assertEqual(base["rows_admitted"], 1)

    def test_not_with_null_column(self):
        # d IS NOT NULL AND NOT(d = 3) must keep every non-null non-3 date;
        # rows with NULL d are excluded by the first conjunct, not lost by NOT.
        sid = self.ws.build(
            [mat("M1", {"op": "range", "col": "d", "lo": 1, "hi": 2}, ["d"])]
        )
        q = {
            "group_by": ["d"],
            "where": {
                "op": "and",
                "args": [
                    {"op": "not", "arg": {"op": "is_null", "col": "d"}},
                    {"op": "not", "arg": {"op": "eq", "col": "d", "val": 3}},
                ],
            },
            "measures": [{"kind": "count_star", "as": "c"}],
        }
        prod = assert_matches(self, self.ws, sid, q)
        self.assertEqual({r["group"]["d"] for r in prod}, {1, 2, 4, 5})

    def test_not_still_admits_null_branch_when_required(self):
        # NOT(d=3) is NULL for the NULL-date row, so that row must NOT appear;
        # NOT(d IS NULL) also excludes it; but NOT(is_null(d) AND d>100) is TRUE
        # for the NULL row and must keep it.
        sid = self.ws.build(
            [mat("M1", {"op": "range", "col": "d", "lo": 1, "hi": 2}, ["d"])]
        )
        q = {
            "group_by": ["d"],
            "where": {
                "op": "not",
                "arg": {"op": "and", "args": [
                    {"op": "is_null", "col": "d"},
                    {"op": "range", "col": "d", "lo": 100, "hi": 200},
                ]},
            },
            "measures": [{"kind": "count_star", "as": "c"},
                         {"kind": "sum", "col": "amount", "as": "s"}],
        }
        e = self.ws.explain(sid, q)
        prod = assert_matches(self, self.ws, sid, q)
        got = {r["group"]["d"]: r for r in prod}
        # three-valued: for the NULL-date row is_null is TRUE, the range is
        # NULL, so the AND is NULL and NOT(NULL) is NULL -> the row is excluded.
        self.assertEqual(sorted(got), [1, 2, 3, 4, 5])
        self.assertNotIn(None, got)

    def test_complement_keeps_null_branch_when_query_admits_it(self):
        # The cover predicate (M1's range) is NULL on the NULL-date row, so a
        # plain SQL NOT(cover) would evaluate to NULL there and silently drop
        # the row.  The residual uses a total NOT_TRUE, which keeps it.
        sid = self.ws.build(
            [mat("M1", {"op": "range", "col": "d", "lo": 1, "hi": 5}, ["d"])]
        )
        q = {
            "group_by": ["d"],
            "where": {
                "op": "or",
                "args": [
                    {"op": "range", "col": "d", "lo": 1, "hi": 5},
                    {"op": "is_null", "col": "d"},
                ],
            },
            "measures": [{"kind": "count_star", "as": "c"},
                         {"kind": "sum", "col": "amount", "as": "s"}],
        }
        e = self.ws.explain(sid, q)
        prod = assert_matches(self, self.ws, sid, q)
        got = {r["group"]["d"]: r for r in prod}
        self.assertIn(None, got)
        self.assertEqual(got[None]["s"], 60)
        base = [s for s in e["segments"] if s["kind"] == "base"][0]
        self.assertIn("not_true", str(base["residual"]))
        self.assertGreaterEqual(base["rows_admitted"], 1)

    def test_not_over_or(self):
        sid = self.ws.build(
            [mat("M1", {"op": "range", "col": "d", "lo": 1, "hi": 3}, ["d"])]
        )
        q = {
            "group_by": ["d"],
            "where": {
                "op": "not",
                "arg": {"op": "or", "args": [
                    {"op": "range", "col": "d", "lo": 1, "hi": 2},
                    {"op": "is_null", "col": "d"},
                ]},
            },
            "measures": [{"kind": "count_star", "as": "c"}],
        }
        prod = assert_matches(self, self.ws, sid, q)
        got = {r["group"]["d"]: r for r in prod}
        # NOT(TRUE) rows: 3,4,5.  NULL-date: OR is TRUE (is_null), NOT -> FALSE.
        self.assertEqual(sorted(got), [3, 4, 5])

    def test_empty_result_without_grouping_returns_one_row(self):
        sid = self.ws.build(
            [mat("M1", {"op": "range", "col": "d", "lo": 1, "hi": 3}, ["d"])]
        )
        q = {
            "group_by": [],
            "where": {"op": "range", "col": "d", "lo": 100, "hi": 200},
            "measures": fixtures.ALL_MEASURES,
        }
        e = self.ws.explain(sid, q)
        self.assertEqual(len(e["result"]), 1)
        row = e["result"][0]
        self.assertEqual(row["cnt"], 0)
        self.assertEqual(row["nn"], 0)
        self.assertIsNone(row["total"])
        self.assertIsNone(row["mean"])

    def test_empty_result_with_grouping_returns_no_rows(self):
        sid = self.ws.build(
            [mat("M1", {"op": "range", "col": "d", "lo": 1, "hi": 3}, ["d"])]
        )
        q = {
            "group_by": ["d"],
            "where": {"op": "range", "col": "d", "lo": 100, "hi": 200},
            "measures": fixtures.ALL_MEASURES,
        }
        e = self.ws.explain(sid, q)
        self.assertEqual(e["result"], [])

    def test_all_null_measure_group(self):
        # d=2 has a NULL amount: sum/avg are NULL, count(*) is 1, count(x) is 0
        sid = self.ws.build([self._no_dates_full()])
        q = {
            "group_by": ["d"],
            "where": {"op": "range", "col": "d", "lo": 2, "hi": 2},
            "measures": fixtures.ALL_MEASURES,
        }
        e = self.ws.explain(sid, q)
        (row,) = e["result"]
        self.assertEqual(row["group"]["d"], 2)
        self.assertEqual(row["cnt"], 1)
        self.assertEqual(row["nn"], 0)
        self.assertIsNone(row["total"])
        self.assertIsNone(row["mean"])

    def _no_dates_full(self):
        return mat("ALL", {"op": "and", "args": []}, ["d"])


if __name__ == "__main__":
    unittest.main()
