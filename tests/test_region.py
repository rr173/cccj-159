"""Exhaustive and randomised checks of the exact region algebra.

`region.py` is the engine that proves coverage, so it gets the strictest
testing: for every predicate in a random family and every point of a small
finite domain, the true set, false set and complement must classify the point
exactly, and `disjointify` must produce a genuine partition.
"""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import refimpl  # noqa: E402
from matview import pred, region  # noqa: E402

SCHEMA = pred.Schema("row_id", {"d": "int", "t": "text", "n": "null"})
D_VALUES = [None, -1, 0, 1, 2, 3, 4, 5]
T_VALUES = [None, "a", "b", "c"]
DOMAIN = [{"d": d, "t": t, "n": None} for d in D_VALUES for t in T_VALUES]


def in_world(row, w):
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
        elif c[0] == "tnotin":
            if v is None or v in c[1]:
                return False
        else:  # pragma: no cover
            raise AssertionError("bad constraint %r" % (c,))
    return True


def in_region(row, worlds):
    return any(in_world(row, w) for w in worlds)


def random_predicate(rng, depth=0):
    opts = ["dr", "te", "tn", "dn", "and", "or", "not"]
    weights = [28, 20, 14, 10, 12, 12, 4] if depth < 2 else [35, 30, 25, 10, 0, 0, 0]
    k = rng.choices(opts, weights=weights)[0]
    if k == "dr":
        lo = rng.randint(-1, 4)
        hi = rng.randint(lo, 5)
        return {"op": "range", "col": "d", "lo": lo, "hi": hi}
    if k == "te":
        return {"op": "eq", "col": "t", "val": rng.choice(["a", "b", "c"])}
    if k == "tn":
        return {"op": "is_null", "col": "t"}
    if k == "dn":
        return {"op": "is_null", "col": "n"}
    if k == "and":
        return {"op": "and", "args": [random_predicate(rng, depth + 1)
                                     for _ in range(rng.randint(1, 3))]}
    if k == "or":
        return {"op": "or", "args": [random_predicate(rng, depth + 1)
                                    for _ in range(rng.randint(1, 3))]}
    return {"op": "not", "arg": random_predicate(rng, depth + 1)}


class ExhaustiveRegionAlgebra(unittest.TestCase):
    N = 400

    def test_true_false_complement_classify_every_point(self):
        rng = random.Random(99)
        for _ in range(self.N):
            pj = random_predicate(rng)
            p = pred.parse(pj, SCHEMA)
            tset = region.true_set(p, SCHEMA)
            fset = region.false_set(p, SCHEMA)
            comp = region.region_subtract([{}], tset, SCHEMA.fields)
            for row in DOMAIN:
                v = refimpl.truth(pj, row)
                self.assertEqual(in_region(row, tset), v is True, msg=(pj, row))
                self.assertEqual(in_region(row, fset), v is False, msg=(pj, row))
                self.assertFalse(
                    in_region(row, comp) and v is True,
                    msg="complement overlaps true set: %r %r" % (pj, row),
                )
                self.assertEqual(
                    in_region(row, comp) or v is True, True,
                    msg="complement misses a non-true point: %r %r" % (pj, row),
                )

    def test_disjointify_is_a_partition(self):
        rng = random.Random(123)
        for _ in range(self.N):
            pj = random_predicate(rng)
            p = pred.parse(pj, SCHEMA)
            union = region.true_set(p, SCHEMA) + region.false_set(p, SCHEMA)
            d = region.disjointify(union, SCHEMA.fields)
            for row in DOMAIN:
                hits = sum(1 for w in d if in_world(row, w))
                expected = refimpl.truth(pj, row) is not None
                self.assertLessEqual(hits, 1, msg=(pj, row, d))
                self.assertEqual(hits == 1, expected, msg=(pj, row, d))

    def test_arbitrary_union_of_predicates_disjointified(self):
        rng = random.Random(321)
        for _ in range(120):
            pjs = [random_predicate(rng, 1) for _ in range(3)]
            worlds = []
            for pj in pjs:
                worlds.extend(region.true_set(pred.parse(pj, SCHEMA), SCHEMA))
            d = region.disjointify(worlds, SCHEMA.fields)
            for row in DOMAIN:
                hits = sum(1 for w in d if in_world(row, w))
                expected = any(refimpl.truth(pj, row) is True for pj in pjs)
                self.assertLessEqual(hits, 1, msg=(pjs, row, d))
                self.assertEqual(hits == 1, expected, msg=(pjs, row, d))

    def test_difference_from_world_is_exact(self):
        rng = random.Random(77)
        for _ in range(200):
            a = random_predicate(rng, 1)
            b = random_predicate(rng, 1)
            wa = region.true_set(pred.parse(a, SCHEMA), SCHEMA)
            wb = region.true_set(pred.parse(b, SCHEMA), SCHEMA)
            diff = region.region_subtract(wa, wb, SCHEMA.fields)
            for row in DOMAIN:
                expected = (
                    refimpl.truth(a, row) is True
                    and refimpl.truth(b, row) is not True
                )
                self.assertEqual(in_region(row, diff), expected, msg=(a, b, row))

    def test_world_predicate_round_trip(self):
        """A world described in JSON must denote exactly the same point set."""
        rng = random.Random(5)
        for _ in range(100):
            pj = random_predicate(rng)
            worlds = region.true_set(pred.parse(pj, SCHEMA), SCHEMA)
            back = pred.parse(region.predicate_json(worlds), SCHEMA)
            for row in DOMAIN:
                self.assertEqual(
                    pred.admitted(back, row),
                    in_region(row, worlds),
                    msg=(pj, worlds, row),
                )


if __name__ == "__main__":
    unittest.main()
