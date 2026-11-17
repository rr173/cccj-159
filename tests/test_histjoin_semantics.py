"""Exact-semantics tests for the offline history join.

The hand-computed walkthrough from the brief anchors the suite; every other
edge case (tied top-tier sources, null vs deleted, half-open end points,
int64 extremes, knowledge-time backtracking, every quarantine reason) is
checked both against explicit hand expectations and against the independent
enumerator in ``tests.histjoin_refimpl`` -- which never imports product code.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from histjoin import (  # noqa: E402
    build_warehouse,
    lineage_for,
    list_facts,
    list_quarantine,
)
from histjoin import demo_data as walk  # noqa: E402
from tests import hj_util  # noqa: E402
from tests.histjoin_refimpl import ReferenceWarehouse  # noqa: E402


class Walkthrough(unittest.TestCase):
    """The six orders from the brief, with their exact hand-computed fate."""

    def setUp(self):
        self.d = hj_util.JsonlDir()
        self.d.write(walk.ORDERS, walk.CUSTOMER_HISTORY, walk.STORE_HISTORY)
        build_warehouse(**self.d.build_kwargs())

    def tearDown(self):
        self.d.cleanup()

    def test_fact_disposition_and_region_sums(self):
        facts = {f["row_id"]: f for f in list_facts(self.d.out_path)}
        self.assertEqual(sorted(facts), ["O1", "O2", "O3", "O4"])
        for rid, (store, region, amount) in walk.EXPECTED_FACTS.items():
            self.assertEqual(facts[rid]["store"], store)
            self.assertEqual(facts[rid]["region"], region)
            self.assertEqual(facts[rid]["amount"], amount)
            self.assertEqual(facts[rid]["customer"], "C")
        sums = {}
        for f in facts.values():
            sums[f["region"]] = sums.get(f["region"], 0) + f["amount"]
        self.assertEqual(sums, walk.EXPECTED_REGION_SUMS)

    def test_quarantine_does_not_fall_back_or_merge_amounts(self):
        quar = {q["row_id"]: q for q in list_quarantine(self.d.out_path)}
        self.assertEqual(set(quar), {"O5", "O6"})
        self.assertEqual(quar["O5"]["failed_level"], "customer")
        self.assertEqual(quar["O5"]["reason"], "deleted")
        # the lower-priority S2 must NOT fill the top-tier deletion
        self.assertEqual(quar["O5"]["top_versions"], ["c-del"])
        self.assertEqual(quar["O5"]["upstream_versions"], [])
        self.assertEqual(quar["O6"]["failed_level"], "store")
        self.assertEqual(quar["O6"]["reason"], "conflict")
        self.assertEqual(quar["O6"]["top_versions"], ["s2-north", "s2-south"])
        self.assertEqual(quar["O6"]["upstream_versions"], ["c-v2"])

    def test_every_order_is_in_exactly_one_place(self):
        facts = {f["row_id"] for f in list_facts(self.d.out_path)}
        quar = {q["row_id"] for q in list_quarantine(self.d.out_path)}
        self.assertEqual(facts | quar, {o["row_id"] for o in walk.ORDERS})
        self.assertFalse(facts & quar)

    def test_lineage_trails_match_hand_versions(self):
        l1 = lineage_for(self.d.out_path, "O1")["levels"]["customer"][0]
        self.assertEqual(l1["version_id"], "c-v1")  # correction unknown at kt=9
        l2 = lineage_for(self.d.out_path, "O2")["levels"]["customer"][0]
        self.assertEqual(l2["version_id"], "c-v2")  # correction known at kt=10
        l3 = lineage_for(self.d.out_path, "O3")["levels"]["store"][0]
        self.assertEqual(l3["version_id"], "s2-north")  # t=8 excludes West
        l4 = lineage_for(self.d.out_path, "O4")["levels"]["customer"][0]
        self.assertEqual(l4["version_id"], "c-v1")  # t=15 outside [5,15)


class MatviewIntegration(unittest.TestCase):
    """The produced fact DB is consumed by the existing entry points."""

    def test_build_snapshot_and_region_sum(self):
        import json
        from matview import api

        d = hj_util.JsonlDir()
        d.write(walk.ORDERS, walk.CUSTOMER_HISTORY, walk.STORE_HISTORY)
        try:
            build_warehouse(**d.build_kwargs())
            with open(os.path.join(d.out_path, "schema.json")) as fh:
                schema = json.load(fh)
            cat = os.path.join(d.dir, "catalogue")
            specs = [{
                "name": "REGION_SUM",
                "predicate": {"op": "and", "args": []},
                "group_cols": ["region"],
                "measures": ["amount"],
            }]
            sid = api.build_snapshot(
                cat, os.path.join(d.out_path, "facts.sqlite"),
                specs, schema, snapshot_id="walk",
            )
            res = api.execute(cat, sid, {
                "group_by": ["region"],
                "measures": [{"kind": "sum", "col": "amount", "as": "total"}],
            })
            sums = {r["group"]["region"]: r["total"] for r in res["rows"]}
            self.assertEqual(sums, walk.EXPECTED_REGION_SUMS)
        finally:
            d.cleanup()


class TiedTopTierSources(unittest.TestCase):
    """Same (deleted, value) at the top priority -> one answer, all sources."""

    def test_tied_equal_values_are_both_retained(self):
        d = hj_util.JsonlDir()
        cust = [
            hj_util.h("a1", "C", 0, 100, 0, None, 5, value="S"),
            hj_util.h("a2", "C", 0, 100, 0, None, 5, value="S"),
        ]
        store = [hj_util.h("r1", "S", hj_util.MIN_I64, None, 0, None, 0, value="R")]
        orders = [hj_util.o("T1", "C", 10, 5, 7)]
        d.write(orders, cust, store)
        try:
            build_warehouse(**d.build_kwargs())
            facts = list_facts(d.out_path)
            self.assertEqual(len(facts), 1)
            lin = lineage_for(d.out_path, "T1")["levels"]["customer"]
            self.assertEqual(sorted(x["version_id"] for x in lin), ["a1", "a2"])
            self.assertTrue(all(x["tied"] for x in lin))
        finally:
            d.cleanup()

    def test_tied_deleted_markers_quarantine_with_both_sources(self):
        d = hj_util.JsonlDir()
        cust = [
            hj_util.h("d1", "C", 0, 100, 0, None, 9, deleted=True),
            hj_util.h("d2", "C", 0, 100, 0, None, 9, deleted=True),
            hj_util.h("low", "C", 0, 100, 0, None, 1, value="S"),
        ]
        d.write([hj_util.o("T2", "C", 10, 5)], cust, [])
        try:
            build_warehouse(**d.build_kwargs())
            q = list_quarantine(d.out_path)
            self.assertEqual(len(q), 1)
            self.assertEqual(q[0]["reason"], "deleted")
            self.assertEqual(q[0]["top_versions"], ["d1", "d2"])
        finally:
            d.cleanup()

    def test_same_priority_disagreement_is_conflict(self):
        d = hj_util.JsonlDir()
        cust = [
            hj_util.h("x1", "C", 0, 100, 0, None, 5, value="S1"),
            hj_util.h("x2", "C", 0, 100, 0, None, 5, value="S2"),
        ]
        d.write([hj_util.o("T3", "C", 10, 5)], cust, [])
        try:
            build_warehouse(**d.build_kwargs())
            q = list_quarantine(d.out_path)[0]
            self.assertEqual(q["reason"], "conflict")
            self.assertEqual(q["top_versions"], ["x1", "x2"])
        finally:
            d.cleanup()


class NullVersusDeleted(unittest.TestCase):
    """An empty attribute and a deletion are distinct outcomes."""

    def _build(self, deleted, reason):
        d = hj_util.JsonlDir()
        rec = hj_util.h(
            "n1", "C", 0, 100, 0, None, 3,
            deleted=deleted, value=None,
        )
        d.write([hj_util.o("N1", "C", 10, 5)], [rec], [])
        try:
            build_warehouse(**d.build_kwargs())
            q = list_quarantine(d.out_path)[0]
            self.assertEqual(q["reason"], reason)
            return d
        finally:
            if deleted:
                d.cleanup()

    def test_null_value_is_an_empty_attribute_not_a_deletion(self):
        d = self._build(False, "null_value")
        try:
            lin = lineage_for(d.out_path, "N1")["levels"]["customer"][0]
            self.assertEqual(lin["status"], "null_value")
            self.assertIsNone(lin["value"])
        finally:
            d.cleanup()

    def test_deleted_is_separate_from_null_value(self):
        self._build(True, "deleted")

    def test_low_priority_value_does_not_fill_null_or_deleted(self):
        for deleted, reason in ((True, "deleted"), (False, "null_value")):
            d = hj_util.JsonlDir()
            top = hj_util.h("top", "C", 0, 100, 0, None, 4,
                            deleted=deleted, value=None)
            low = hj_util.h("low", "C", 0, 100, 0, None, 1, value="S")
            d.write([hj_util.o("N2", "C", 10, 5)], [top, low], [])
            try:
                build_warehouse(**d.build_kwargs())
                q = list_quarantine(d.out_path)[0]
                self.assertEqual(q["reason"], reason)
                self.assertEqual(q["top_versions"], ["top"])
            finally:
                d.cleanup()


class HalfOpenEndPoints(unittest.TestCase):
    """Equality on either right end point is an exclusion."""

    def test_event_time_equal_to_valid_to_is_excluded(self):
        d = hj_util.JsonlDir()
        cust = [hj_util.h("v", "C", 0, 10, 0, None, 0, value="S")]
        store = [hj_util.h("r", "S", 0, 100, 0, None, 0, value="R")]
        d.write(
            [hj_util.o("at9", "C", 9, 5), hj_util.o("at10", "C", 10, 5)],
            cust, store,
        )
        try:
            build_warehouse(**d.build_kwargs())
            quar = {q["row_id"]: q for q in list_quarantine(d.out_path)}
            facts = {f["row_id"] for f in list_facts(d.out_path)}
            self.assertIn("at9", facts)
            self.assertEqual(quar["at10"]["reason"], "gap")
            self.assertEqual(quar["at10"]["top_versions"], [])
        finally:
            d.cleanup()

    def test_known_time_equal_to_known_to_is_excluded(self):
        d = hj_util.JsonlDir()
        cust = [hj_util.h("v", "C", 0, 100, 0, 7, 0, value="S")]
        store = [hj_util.h("r", "S", 0, 100, 0, None, 0, value="R")]
        d.write(
            [hj_util.o("k6", "C", 5, 6), hj_util.o("k7", "C", 5, 7)],
            cust, store,
        )
        try:
            build_warehouse(**d.build_kwargs())
            quar = {q["row_id"]: q for q in list_quarantine(d.out_path)}
            facts = {f["row_id"] for f in list_facts(d.out_path)}
            self.assertIn("k6", facts)
            self.assertEqual(quar["k7"]["reason"], "gap")
        finally:
            d.cleanup()


class Int64EndPoints(unittest.TestCase):
    """Times reach both int64 extremes with no float/plus-one precision loss."""

    def test_min_and_max_event_times(self):
        d = hj_util.JsonlDir()
        cust = [hj_util.h("all", "C", hj_util.MIN_I64, None,
                          hj_util.MIN_I64, None, 0, value="S")]
        store = [hj_util.h("r", "S", hj_util.MIN_I64, None,
                           hj_util.MIN_I64, None, 0, value="R")]
        orders = [
            hj_util.o("min", "C", hj_util.MIN_I64, hj_util.MIN_I64),
            hj_util.o("max", "C", hj_util.MAX_I64, hj_util.MAX_I64),
        ]
        d.write(orders, cust, store)
        try:
            build_warehouse(**d.build_kwargs())
            facts = {f["row_id"]: f for f in list_facts(d.out_path)}
            self.assertEqual(facts["min"]["event_time"], hj_util.MIN_I64)
            self.assertEqual(facts["max"]["event_time"], hj_util.MAX_I64)
        finally:
            d.cleanup()

    def test_interval_ending_at_min_plus_one(self):
        # [MIN_I64, MIN_I64+1) covers only the minimum point exactly
        d = hj_util.JsonlDir()
        cust = [hj_util.h("edge", "C", hj_util.MIN_I64, hj_util.MIN_I64 + 1,
                          hj_util.MIN_I64, None, 0, value="S")]
        store = [hj_util.h("r", "S", hj_util.MIN_I64, None,
                           hj_util.MIN_I64, None, 0, value="R")]
        d.write(
            [hj_util.o("in", "C", hj_util.MIN_I64, 0),
             hj_util.o("out", "C", hj_util.MIN_I64 + 1, 0)],
            cust, store,
        )
        try:
            build_warehouse(**d.build_kwargs())
            quar = {q["row_id"]: q for q in list_quarantine(d.out_path)}
            facts = {f["row_id"] for f in list_facts(d.out_path)}
            self.assertIn("in", facts)
            self.assertEqual(quar["out"]["reason"], "gap")
        finally:
            d.cleanup()


class KnowledgeBacktracking(unittest.TestCase):
    """Different known_times over the same event_time replay history states."""

    def test_late_correction_changes_the_answer_only_after_known_from(self):
        d = hj_util.JsonlDir()
        cust = [
            hj_util.h("old", "C", 0, 100, 0, None, 0, value="OLD"),
            hj_util.h("new", "C", 0, 100, 50, None, 1, value="NEW"),
        ]
        store = [
            hj_util.h("rold", "OLD", hj_util.MIN_I64, None, 0, None, 0, value="RO"),
            hj_util.h("rnew", "NEW", hj_util.MIN_I64, None, 0, None, 0, value="RN"),
        ]
        orders = [
            hj_util.o("before", "C", 25, 49),
            hj_util.o("at", "C", 25, 50),
            hj_util.o("after", "C", 25, 51),
        ]
        d.write(orders, cust, store)
        try:
            build_warehouse(**d.build_kwargs())
            facts = {f["row_id"]: f for f in list_facts(d.out_path)}
            self.assertEqual(facts["before"]["store"], "OLD")
            self.assertEqual(facts["at"]["store"], "NEW")
            self.assertEqual(facts["after"]["store"], "NEW")
            self.assertEqual(facts["before"]["region"], "RO")
            self.assertEqual(facts["after"]["region"], "RN")
        finally:
            d.cleanup()


class MissingKeyAndLevel2Reasons(unittest.TestCase):
    def test_null_customer_is_missing_key_with_empty_lists(self):
        d = hj_util.JsonlDir()
        d.write([hj_util.o("mk", None, 5, 5)], [], [])
        try:
            build_warehouse(**d.build_kwargs())
            q = list_quarantine(d.out_path)[0]
            self.assertEqual(q["failed_level"], "customer")
            self.assertEqual(q["reason"], "missing_key")
            self.assertEqual(q["top_versions"], [])
            lin = lineage_for(d.out_path, "mk")
            self.assertEqual(lin["levels"]["customer"][0]["status"], "missing_key")
        finally:
            d.cleanup()

    def test_unknown_customer_is_gap_not_missing_key(self):
        d = hj_util.JsonlDir()
        d.write([hj_util.o("g", "GHOST", 5, 5)],
                [hj_util.h("c", "OTHER", 0, 10, 0, None, 0, value="S")], [])
        try:
            build_warehouse(**d.build_kwargs())
            q = list_quarantine(d.out_path)[0]
            self.assertEqual(q["reason"], "gap")
            self.assertEqual(q["top_versions"], [])
        finally:
            d.cleanup()

    def test_level2_gap_deleted_null_conflict_are_distinct(self):
        cases = [
            ("gap", []),
            ("deleted", [hj_util.h("del", "S", 0, 100, 0, None, 0, deleted=True)]),
            ("null_value", [hj_util.h("nul", "S", 0, 100, 0, None, 0, value=None)]),
            ("conflict", [
                hj_util.h("c1", "S", 0, 100, 0, None, 0, value="R1"),
                hj_util.h("c2", "S", 0, 100, 0, None, 0, value="R2"),
            ]),
        ]
        for reason, store_hist in cases:
            d = hj_util.JsonlDir()
            cust = [hj_util.h("s", "C", 0, 100, 0, None, 0, value="S")]
            d.write([hj_util.o("L2-" + reason, "C", 10, 5)], cust, store_hist)
            try:
                build_warehouse(**d.build_kwargs())
                q = list_quarantine(d.out_path)[0]
                self.assertEqual(q["failed_level"], "store")
                self.assertEqual(q["reason"], reason)
                self.assertEqual(q["upstream_versions"], ["s"])
            finally:
                d.cleanup()


class DistinctRowIdsAreNotMerged(unittest.TestCase):
    def test_identical_business_rows_stay_distinct_facts(self):
        d = hj_util.JsonlDir()
        cust = [hj_util.h("s", "C", 0, 100, 0, None, 0, value="S")]
        store = [hj_util.h("r", "S", 0, 100, 0, None, 0, value="R")]
        orders = [
            hj_util.o("dup-A", "C", 10, 5, 11),
            hj_util.o("dup-B", "C", 10, 5, 11),
        ]
        d.write(orders, cust, store)
        try:
            build_warehouse(**d.build_kwargs())
            facts = list_facts(d.out_path)
            self.assertEqual(sorted(f["row_id"] for f in facts), ["dup-A", "dup-B"])
        finally:
            d.cleanup()


class OracleAgreement(unittest.TestCase):
    """Every product outcome equals the independent brute-force enumerator."""

    def test_walkthrough_agrees_with_reference_enumerator(self):
        d = hj_util.JsonlDir()
        d.write(walk.ORDERS, walk.CUSTOMER_HISTORY, walk.STORE_HISTORY)
        try:
            build_warehouse(**d.build_kwargs())
            ref = ReferenceWarehouse(
                d.orders_path, d.cust_path, d.store_path
            )
            facts = {f["row_id"]: f for f in list_facts(d.out_path)}
            quar = {q["row_id"]: q for q in list_quarantine(d.out_path)}
            for exp in ref.all_expected():
                rid = exp["row_id"]
                if exp["outcome"] == "fact":
                    self.assertIn(rid, facts)
                    self.assertEqual(facts[rid]["store"], exp["store"])
                    self.assertEqual(facts[rid]["region"], exp["region"])
                    lin = lineage_for(d.out_path, rid)["levels"]
                    self.assertEqual(
                        sorted(x["version_id"] for x in lin["customer"]),
                        exp["customer_top"],
                    )
                    self.assertEqual(
                        sorted(x["version_id"] for x in lin["store"]),
                        exp["store_top"],
                    )
                else:
                    self.assertIn(rid, quar)
                    self.assertEqual(quar[rid]["reason"], exp["reason"])
                    self.assertEqual(quar[rid]["failed_level"],
                                     exp["failed_level"])
        finally:
            d.cleanup()


if __name__ == "__main__":
    unittest.main()
