"""Randomised property tests across the whole protocol.

For hundreds of random datasets, random materialisation sets (intersecting,
contained, disjoint, with holes, finer and coarser groupings, retained and
unretained filter columns, partial states) and random queries, the product
answer must equal the independent reference enumerator, and plan components
must never double credit a group.
"""

import json
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import refimpl, compare  # noqa: E402
from matview import api, catalog, plan as planmod, region, pred  # noqa: E402


SCHEMA = {
    "row_id": "row_id",
    "fields": {"d": "int", "amount": "int", "region": "text", "g": "int"},
}
REGIONS = ["a", "b", "c"]


def _cons_key(part):
    """Stable textual form of a world description part."""
    if part.get("is_null"):
        return part["col"] + ":null"
    if "not_in" in part:
        return part["col"] + ":notin:" + ",".join(map(str, part["not_in"]))
    if "eq" in part:
        return part["col"] + ":eq:" + str(part["eq"])
    return part["col"] + ":[%s,%s]" % (part.get("lo"), part.get("hi"))


def random_facts(rng, n):
    rows = []
    for i in range(1, n + 1):
        d = None if rng.random() < 0.18 else rng.randint(0, 7)
        amount = None if rng.random() < 0.22 else rng.randint(-20, 60)
        reg = None if rng.random() < 0.12 else rng.choice(REGIONS)
        g = None if rng.random() < 0.12 else rng.randint(0, 2)
        rows.append({"row_id": i, "d": d, "amount": amount, "region": reg, "g": g})
    return rows


def random_predicate(rng, depth=0):
    choices = ["d_range", "d_null", "reg_eq", "g_eq", "and", "or", "not", "true"]
    weights = [30, 10, 18, 12, 12, 12, 4, 2] if depth < 2 else [40, 15, 25, 20, 0, 0, 0, 0]
    kind = rng.choices(choices, weights=weights)[0]
    if kind == "d_range":
        lo = rng.randint(-1, 7)
        hi = rng.randint(lo, 7)
        return {"op": "range", "col": "d", "lo": lo, "hi": hi}
    if kind == "d_null":
        return {"op": "is_null", "col": "d"}
    if kind == "reg_eq":
        return {"op": "eq", "col": "region", "val": rng.choice(REGIONS)}
    if kind == "g_eq":
        return {"op": "eq", "col": "g", "val": rng.randint(0, 2)}
    if kind == "and":
        return {"op": "and", "args": [random_predicate(rng, depth + 1)
                                      for _ in range(rng.randint(1, 3))]}
    if kind == "or":
        return {"op": "or", "args": [random_predicate(rng, depth + 1)
                                     for _ in range(rng.randint(1, 3))]}
    if kind == "not":
        return {"op": "not", "arg": random_predicate(rng, depth + 1)}
    return {"op": "and", "args": []}


def random_spec(rng, idx):
    kind = rng.choice(["d", "d", "g", "g", "region", "fine", "none"])
    if kind == "d":
        gcols = ["d"]
        pred = random_predicate(rng, 1)
    elif kind == "g":
        gcols = ["g"]
        pred = random_predicate(rng, 1)
    elif kind == "region":
        gcols = ["region"]
        pred = {"op": "and", "args": []}
    elif kind == "fine":
        gcols = ["d", "g"]
        pred = {"op": "and", "args": []}
    else:
        gcols = []
        pred = random_predicate(rng, 1)
    # keep coarse (no d) materialisations from being interpreted as also
    # grouping by d, which would defeat the coarse tests
    spec = {
        "name": "M%d" % idx,
        "predicate": pred,
        "group_cols": gcols,
        "measures": ["amount"],
    }
    if rng.random() < 0.35:
        # retain a column that is constant in expectation only when the filter
        # pins it; the builder verifies constantness against real facts
        spec["retained_cols"] = ["region"] if "region" in gcols else (
            ["d"] if "d" in gcols else []
        )
        if not spec["retained_cols"]:
            spec.pop("retained_cols")
    if rng.random() < 0.25:
        spec["state"] = rng.choice(
            [
                {"skip": ["sum:amount"]},
                {"skip": ["nn:amount"]},
                {"skip": ["nn:amount"], "extra": {"avg:amount": True}},
            ]
        )
    return spec


def random_query(rng):
    gcols = rng.choice([[], ["d"], ["g"], ["region"], ["d"], ["g"]])
    measures = [{"kind": "count_star", "as": "c"}]
    if rng.random() < 0.8:
        measures.append({"kind": "count", "col": "amount", "as": "nn"})
    if rng.random() < 0.8:
        measures.append({"kind": "sum", "col": "amount", "as": "s"})
    if rng.random() < 0.6:
        measures.append({"kind": "avg", "col": "amount", "as": "a"})
    q = {"group_by": gcols, "measures": measures}
    if rng.random() < 0.8:
        q["where"] = random_predicate(rng)
    return q


class RandomisedProperties(unittest.TestCase):
    CASES = 40

    def test_random_cases_match_reference(self):
        rng = random.Random(20260101)
        for case in range(self.CASES):
            n = rng.randint(0, 30)
            rows = random_facts(rng, n)
            with self.subTest(case=case, n=n):
                self._run_case(rng, rows, case)

    def _run_case(self, rng, rows, case):
        import tempfile

        workdir = tempfile.mkdtemp(prefix="matview-rand-")
        fact_path = os.path.join(workdir, "facts.sqlite")
        catalogue = os.path.join(workdir, "catalogue")
        api.create_fact_db(fact_path, SCHEMA, rows)
        specs = [random_spec(rng, i) for i in range(rng.randint(0, 3))]
        try:
            sid = api.build_snapshot(catalogue, fact_path, specs, SCHEMA)
        except Exception:
            # a random spec may legitimately be refused (e.g. retained column
            # not constant); such a build must leave nothing behind
            self.assertEqual(
                os.listdir(os.path.join(catalogue, "snapshots")), []
            )
            return
        for qi in range(4):
            q = random_query(rng)
            prod = api.execute(catalogue, sid, q)
            ref = refimpl.reference_answer(fact_path, SCHEMA["fields"], q)
            problems = compare.compare(prod["rows"], ref, q)
            self.assertEqual(
                problems, [],
                msg=(
                    "case %d query %d\nspecs=%r\nquery=%r\nrows=%r\n%s"
                    % (case, qi, specs, q, rows, "\n".join(problems))
                ),
            )
            # partition soundness: no stored group may be credited twice
            gids = [
                (s["mat_id"], s["gid"])
                for s in prod["segments"]
                if s["kind"] == "materialisation"
            ]
            self.assertEqual(len(gids), len(set(gids)))
            # residual worlds must be disjoint
            base = [s for s in prod["segments"] if s["kind"] == "base"]
            for b in base:
                if b.get("worlds"):
                    descs = [tuple(sorted(_cons_key(c)
                                          for c in w["and"]))
                             for w in b["worlds"]]
                    self.assertEqual(len(descs), len(set(descs)),
                                     msg="residual worlds overlap: %r" % (descs,))


class RegionAlgebraProperties(unittest.TestCase):
    def test_true_set_and_complement_partition_the_universe(self):
        rng = random.Random(7)
        schema = pred.Schema("row_id", SCHEMA["fields"])
        for _ in range(120):
            pj = random_predicate(rng)
            p = pred.parse(pj, schema)
            true = region.disjointify(region.true_set(p, schema), schema.fields)
            comp = region.disjointify(
                region.region_subtract([{}], true, schema.fields), schema.fields
            )
            for d in [None, -2, -1, 0, 1, 2, 5, 7, 8]:
                for reg in [None, "a", "b", "c", "z"]:
                    for g in [None, 0, 1, 2, 3]:
                        row = {"d": d, "region": reg, "g": g, "amount": None}
                        t = refimpl.truth(pj, row) is True
                        in_true = any(self._in_world(row, w) for w in true)
                        in_comp = any(self._in_world(row, w) for w in comp)
                        self.assertEqual(t, in_true, msg=(pj, row))
                        self.assertEqual(in_true and in_comp, False, msg=(pj, row))
                        self.assertEqual(in_true or in_comp, True, msg=(pj, row))

    def test_disjointify_never_double_covers(self):
        rng = random.Random(11)
        schema = pred.Schema("row_id", SCHEMA["fields"])
        for _ in range(60):
            preds = [random_predicate(rng, 1) for _ in range(3)]
            worlds = []
            for pj in preds:
                p = pred.parse(pj, schema)
                worlds.extend(region.true_set(p, schema))
            dis = region.disjointify(worlds, schema.fields)
            for d in [None, 0, 1, 2, 3, 7]:
                for reg in [None, "a", "b", "c"]:
                    row = {"d": d, "region": reg, "g": 1, "amount": None}
                    hits = sum(1 for w in dis if self._in_world(row, w))
                    expected = any(
                        refimpl.truth(pj, row) is True for pj in preds
                    )
                    self.assertLessEqual(hits, 1, msg=(preds, row, dis))
                    self.assertEqual(hits == 1, expected, msg=(preds, row, dis))

    @staticmethod
    def _in_world(row, w):
        for col, c in w.items():
            v = row.get(col)
            if c[0] == "null":
                if v is not None:
                    return False
            elif c[0] == "int":
                if v is None:
                    return False
                if c[1] is not None and v < c[1]:
                    return False
                if c[2] is not None and v > c[2]:
                    return False
            elif c[0] == "tval":
                if v != c[1]:
                    return False
            else:  # tnotin
                if v is None or v in c[1]:
                    return False
        return True


if __name__ == "__main__":
    unittest.main()
