"""Randomised property tests against the independent brute-force oracle.

The generator deliberately produces the awkward shapes a point query must get
exactly right: nested and overlapping business intervals for the same key,
interleaved/overlapping knowledge intervals, equal-priority divergence,
deletions and empty attributes mixed with real values, late corrections,
multiple keys, null customers, and int64 boundary points.  The product's
facts, quarantine rows and full lineage are then compared field-for-field
with ``tests.histjoin_refimpl``, which scans raw histories and imports no
product resolution code.
"""

from __future__ import annotations

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from histjoin import build_warehouse, lineage_for, list_facts, list_quarantine  # noqa: E402
from tests import hj_util  # noqa: E402
from tests.histjoin_refimpl import ReferenceWarehouse  # noqa: E402

MIN_I64 = hj_util.MIN_I64
MAX_I64 = hj_util.MAX_I64
EDGE_TIMES = [MIN_I64, MIN_I64 + 1, -10, -1, 0, 1, 9, 10, 11, 99, 100,
              MAX_I64 - 1, MAX_I64]


def _interval(rng, lo, hi):
    a = rng.randint(lo, hi)
    if rng.random() < 0.2:
        return a, None  # open right end
    b = rng.randint(lo, hi)
    if a == b:
        b = a + 1 if a < MAX_I64 else a - 1
    return min(a, b), max(a, b)


def _gen_history(rng, keys, vid_prefix, n_records):
    rows = []
    for i in range(n_records):
        key = rng.choice(keys)
        vf, vt = _interval(rng, 0, 100)
        kf, kt = _interval(rng, 0, 100)
        kind = rng.random()
        if kind < 0.15:
            deleted, value = True, None
        elif kind < 0.3:
            deleted, value = False, None  # valid empty attribute
        else:
            deleted, value = False, "%s-val-%d" % (key, rng.randint(0, 4))
        rows.append(hj_util.h(
            "%s-%04d" % (vid_prefix, i), key, vf, vt, kf, kt,
            priority=rng.randint(0, 4), deleted=deleted, value=value,
        ))
    return rows


def _gen_orders(rng, customer_keys, n_orders):
    rows = []
    for i in range(n_orders):
        if rng.random() < 0.08:
            customer = None
        else:
            customer = rng.choice(customer_keys + ["GHOST"])
        et = rng.choice(EDGE_TIMES) if rng.random() < 0.3 else rng.randint(0, 100)
        kt = rng.choice(EDGE_TIMES) if rng.random() < 0.3 else rng.randint(0, 100)
        amount = rng.choice([None, rng.randint(-50, 50)])
        rows.append(hj_util.o("r%05d" % i, customer, et, kt, amount))
    return rows


class RandomisedOracleEquivalence(unittest.TestCase):
    def _compare_one(self, seed):
        rng = random.Random(seed)
        customers = ["C%d" % i for i in range(rng.randint(1, 5))]
        stores = ["S%d" % i for i in range(rng.randint(1, 6))]
        cust_hist = _gen_history(rng, customers, "cv", rng.randint(20, 80))
        # point customer values at the stores the second level knows about
        for rec in cust_hist:
            if not rec["deleted"] and rec["value"] is not None:
                rec["value"] = rng.choice(stores + ["UNKNOWN-STORE"])
        store_hist = _gen_history(rng, stores, "sv", rng.randint(20, 80))
        orders = _gen_orders(rng, customers, rng.randint(30, 100))
        d = hj_util.JsonlDir(prefix="hj-prop-%d-" % seed)
        try:
            d.write(orders, cust_hist, store_hist)
            chunk = rng.choice([1, 5, 64, 2000])
            build_warehouse(**d.build_kwargs(), chunk=chunk)
            ref = ReferenceWarehouse(d.orders_path, d.cust_path, d.store_path)
            facts = {f["row_id"]: f for f in list_facts(d.out_path)}
            quar = {q["row_id"]: q for q in list_quarantine(d.out_path)}
            self.assertEqual(
                set(facts) | set(quar), {o["row_id"] for o in orders}
            )
            self.assertFalse(set(facts) & set(quar))
            for exp in ref.all_expected():
                rid = exp["row_id"]
                lin = lineage_for(d.out_path, rid)
                if exp["outcome"] == "fact":
                    self.assertIn(rid, facts, msg=(seed, rid))
                    f = facts[rid]
                    self.assertEqual(f["store"], exp["store"], msg=(seed, rid))
                    self.assertEqual(f["region"], exp["region"], msg=(seed, rid))
                    self._check_tier(lin, "customer", exp["customer_top"], seed, rid)
                    self._check_tier(lin, "store", exp["store_top"], seed, rid)
                else:
                    self.assertIn(rid, quar, msg=(seed, rid))
                    q = quar[rid]
                    self.assertEqual(q["reason"], exp["reason"], msg=(seed, rid))
                    self.assertEqual(q["failed_level"], exp["failed_level"],
                                     msg=(seed, rid))
                    self.assertEqual(q["top_versions"],
                                     sorted(exp["store_top"] if exp["failed_level"] == "store"
                                            else exp["customer_top"]),
                                     msg=(seed, rid))
                    if exp["reason"] == "gap" or exp["reason"] == "missing_key":
                        self.assertEqual(q["top_versions"], [], msg=(seed, rid))
        finally:
            d.cleanup()

    @staticmethod
    def _check_tier(lineage, level, expected_versions, seed, rid):
        got = [
            x["version_id"] for x in lineage["levels"][level]
            if x["version_id"] is not None
        ]
        assert sorted(got) == sorted(expected_versions), (seed, rid, level, got)

    def test_random_runs_match_oracle(self):
        for seed in range(60):
            with self.subTest(seed=seed):
                self._compare_one(seed)


if __name__ == "__main__":
    unittest.main()
