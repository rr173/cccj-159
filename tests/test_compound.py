"""Compound coverage: text partitions, boolean regions, competing granularities."""

import os
import sys
import unittest
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import refimpl, compare  # noqa: E402
from matview import api  # noqa: E402

SCHEMA = {
    "row_id": "row_id",
    "fields": {"d": "int", "amount": "int", "region": "text"},
}
ROWS = [
    {"row_id": 1, "d": 1, "amount": 10, "region": "a"},
    {"row_id": 2, "d": 2, "amount": None, "region": "a"},
    {"row_id": 3, "d": 3, "amount": 30, "region": "b"},
    {"row_id": 4, "d": 4, "amount": 40, "region": "b"},
    {"row_id": 5, "d": 5, "amount": 50, "region": "c"},
    {"row_id": 6, "d": None, "amount": 60, "region": "a"},
    {"row_id": 7, "d": 6, "amount": 70, "region": None},
    {"row_id": 8, "d": 7, "amount": 80, "region": "c"},
]
ALL4 = [
    {"kind": "count_star", "as": "c"},
    {"kind": "count", "col": "amount", "as": "nn"},
    {"kind": "sum", "col": "amount", "as": "s"},
    {"kind": "avg", "col": "amount", "as": "a"},
]


def mat(name, predicate, group_cols, **kw):
    spec = {"name": name, "predicate": predicate,
            "group_cols": list(group_cols), "measures": ["amount"]}
    spec.update(kw)
    return spec


def build_and_match(test, specs, query, register=True):
    import tempfile

    workdir = tempfile.mkdtemp(prefix="matview-compound-")
    fact = os.path.join(workdir, "facts.sqlite")
    cat = os.path.join(workdir, "catalogue")
    api.create_fact_db(fact, SCHEMA, ROWS)
    sid = api.build_snapshot(cat, fact, specs, SCHEMA, register=register)
    prod = api.execute(cat, sid, query)
    ref = refimpl.reference_answer(fact, SCHEMA["fields"], query)
    problems = compare.compare(prod["rows"], ref, query)
    test.assertEqual(problems, [], msg="\n".join(problems))
    return prod, cat, sid


class TextPartitions(unittest.TestCase):
    def test_region_partition_materialisation(self):
        # M2 covers exactly regions a and b; query asks a, b and c
        specs = [
            mat(
                "AB",
                {"op": "or", "args": [
                    {"op": "eq", "col": "region", "val": "a"},
                    {"op": "eq", "col": "region", "val": "b"},
                ]},
                ["region"],
            )
        ]
        q = {"group_by": ["region"], "measures": ALL4,
             "where": {"op": "not",
                       "arg": {"op": "is_null", "col": "region"}}}
        prod, cat, sid = build_and_match(self, specs, q)
        got = {r["group"]["region"]: r for r in prod["rows"]}
        self.assertEqual(sorted(got), ["a", "b", "c"])
        self.assertEqual(got["a"]["c"], 3)
        self.assertEqual(got["a"]["nn"], 2)
        self.assertEqual(got["a"]["s"], 70)
        self.assertEqual(got["c"]["c"], 2)
        names = {s["name"] for s in prod["segments"]
                 if s["kind"] == "materialisation"}
        self.assertEqual(names, {"AB"})
        # only region c and NULL-region rows are left for base
        self.assertEqual(prod["execution"]["base_rows_admitted"], 2)

    def test_not_text_eq_residual(self):
        specs = [mat("A", {"op": "eq", "col": "region", "val": "a"}, ["region"])]
        q = {"group_by": ["region"], "measures": ALL4,
             "where": {"op": "not",
                       "arg": {"op": "eq", "col": "region", "val": "a"}}}
        prod, cat, sid = build_and_match(self, specs, q)
        got = {r["group"]["region"]: r for r in prod["rows"]}
        # NOT('a' = 'a') -> FALSE for a; for NULL region -> NULL (excluded)
        self.assertEqual(sorted(got), ["b", "c"])
        self.assertNotIn("a", got)
        self.assertNotIn(None, got)
        # the materialisation is wholly outside the query here
        self.assertFalse([s for s in prod["segments"]
                          if s["kind"] == "materialisation"])

    def test_text_partition_complement_with_null(self):
        # cover a and b; query NOT(region IN (a,b)) -> TRUE only for 'c',
        # NULL for the NULL-region row
        specs = [
            mat("AB", {"op": "or", "args": [
                {"op": "eq", "col": "region", "val": "a"},
                {"op": "eq", "col": "region", "val": "b"}]}, ["region"])
        ]
        q = {"group_by": ["region"], "measures": [{"kind": "count_star", "as": "c"}],
             "where": {"op": "not", "arg": {"op": "or", "args": [
                 {"op": "eq", "col": "region", "val": "a"},
                 {"op": "eq", "col": "region", "val": "b"}]}}}
        prod, cat, sid = build_and_match(self, specs, q)
        self.assertEqual([r["group"]["region"] for r in prod["rows"]], ["c"])
        self.assertEqual(prod["rows"][0]["c"], 2)


class BooleanRegion(unittest.TestCase):
    def test_null_or_range_materialisation(self):
        specs = [
            mat("NULLORLOW", {"op": "or", "args": [
                {"op": "is_null", "col": "d"},
                {"op": "range", "col": "d", "lo": 1, "hi": 2}]}, ["d"])
        ]
        q = {"group_by": ["d"], "measures": ALL4}
        prod, cat, sid = build_and_match(self, specs, q)
        got = {r["group"]["d"]: r for r in prod["rows"]}
        self.assertEqual(got[None]["s"], 60)
        self.assertEqual(got[1]["s"], 10)
        self.assertEqual(sorted(got, key=lambda v: (v is None, v)),
                         [1, 2, 3, 4, 5, 6, 7, None])
        names = {s["name"] for s in prod["segments"]
                 if s["kind"] == "materialisation"}
        self.assertEqual(names, {"NULLORLOW"})

    def test_range_with_null_hole_in_middle(self):
        # cover d=1..4 except d=3, via a NOT
        specs = [
            mat("HOLE", {"op": "and", "args": [
                {"op": "range", "col": "d", "lo": 1, "hi": 4},
                {"op": "not", "arg": {"op": "eq", "col": "d", "val": 3}},
            ]}, ["d"])
        ]
        q = {"group_by": ["d"], "measures": [{"kind": "count_star", "as": "c"},
                                             {"kind": "sum", "col": "amount", "as": "s"}]}
        prod, cat, sid = build_and_match(self, specs, q)
        got = {r["group"]["d"]: r for r in prod["rows"]}
        self.assertEqual(got[3]["s"], 30)  # the hole, from base
        base = [s for s in prod["segments"] if s["kind"] == "base"][0]
        self.assertEqual(base["rows_admitted"], 5)  # 3, 5, 6, 7 and the NULL date


class CompetingGranularities(unittest.TestCase):
    def test_finer_and_coarser_groups_do_not_double_count(self):
        for order in ("coarse_first", "fine_first"):
            specs = [
                mat("COARSE", {"op": "and", "args": []}, ["region"]),
                mat("FINE", {"op": "and", "args": []}, ["region", "d"]),
            ]
            if order == "fine_first":
                specs.reverse()
            q = {"group_by": ["region"], "measures": ALL4,
                 "where": {"op": "not",
                           "arg": {"op": "is_null", "col": "region"}}}
            prod, cat, sid = build_and_match(self, specs, q)
            gids = [(s["mat_id"], s["gid"]) for s in prod["segments"]
                    if s["kind"] == "materialisation"]
            self.assertEqual(len(gids), len(set(gids)))
            got = {r["group"]["region"]: r for r in prod["rows"]}
            self.assertEqual(got["a"]["c"], 3)
            self.assertEqual(got["a"]["s"], 70)
            # exactly one granularity should have been used for these rows
            used = {s["name"] for s in prod["segments"]
                    if s["kind"] == "materialisation"}
            self.assertEqual(used, {"COARSE"} if order == "coarse_first" else {"FINE"})

    def test_fine_groups_fold_up_when_coarse_unavailable(self):
        specs = [mat("FINE", {"op": "and", "args": []}, ["region", "d"])]
        q = {"group_by": ["region"], "measures": ALL4,
             "where": {"op": "not", "arg": {"op": "is_null", "col": "region"}}}
        prod, cat, sid = build_and_match(self, specs, q)
        # every eligible (region, d) group is credited and folded
        self.assertGreater(len(
            [s for s in prod["segments"] if s["kind"] == "materialisation"]), 3)
        got = {r["group"]["region"]: r for r in prod["rows"]}
        self.assertEqual(got["b"]["s"], 70)
        self.assertEqual(Fraction(**{}) if False else got["c"]["c"], 2)

    def test_coarse_group_takes_precedence_in_registration_order(self):
        specs = [
            mat("COARSE", {"op": "and", "args": []}, ["region"]),
            mat("FINE", {"op": "and", "args": []}, ["region", "d"]),
        ]
        q = {"group_by": ["region"], "measures": [{"kind": "count_star", "as": "c"}]}
        prod, cat, sid = build_and_match(self, specs, q)
        fine = [s for s in prod["segments"]
                if s["kind"] == "materialisation" and s["name"] == "FINE"]
        # FINE's groups are all already credited by COARSE (which is registered
        # first), so none of them may contribute
        self.assertEqual(fine, [])


class PartialCoverageAcrossMaterialisations(unittest.TestCase):
    def test_three_materialisations_union_exactly(self):
        specs = [
            mat("R1", {"op": "range", "col": "d", "lo": 1, "hi": 2}, ["d"]),
            mat("R2", {"op": "range", "col": "d", "lo": 2, "hi": 4}, ["d"]),
            mat("R3", {"op": "range", "col": "d", "lo": 4, "hi": 7}, ["d"]),
        ]
        q = {"group_by": [], "measures": ALL4}
        prod, cat, sid = build_and_match(self, specs, q)
        (row,) = prod["rows"]
        self.assertEqual(row["c"], 8)
        self.assertEqual(row["nn"], 7)
        self.assertEqual(row["s"], 340)
        # every d=1..7 row is covered; only the NULL-date row is left.  The
        # row with a NULL region has d=6, which R3 covers, so it stays covered.
        self.assertEqual(prod["execution"]["base_rows_admitted"], 1)
        gids = [(s["mat_id"], s["gid"]) for s in prod["segments"]
                if s["kind"] == "materialisation"]
        self.assertEqual(len(gids), len(set(gids)))


if __name__ == "__main__":
    unittest.main()
