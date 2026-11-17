"""Whole-input validation, duplicate detection and failure atomicity.

A malformed line, a duplicate row_id/version_id or an invalid interval is an
error of the *whole run*: the error names file and 1-based line number, no
partial fact database is published, and a previously published complete
result stays queryable afterwards.
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from histjoin import build_warehouse, counts, list_facts  # noqa: E402
from histjoin.errors import (  # noqa: E402
    DuplicateIdError,
    InputValidationError,
    InvalidIntervalError,
)
from histjoin import demo_data as walk  # noqa: E402
from tests import hj_util  # noqa: E402


def _append(path, obj):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")


class ValidationErrors(unittest.TestCase):
    def setUp(self):
        self.d = hj_util.JsonlDir()
        self.d.write(walk.ORDERS, walk.CUSTOMER_HISTORY, walk.STORE_HISTORY)

    def tearDown(self):
        self.d.cleanup()

    def _expect(self, exc_type, file_suffix, line):
        with self.assertRaises(exc_type) as ctx:
            build_warehouse(**self.d.build_kwargs())
        err = ctx.exception
        self.assertTrue(err.file.endswith(file_suffix), err.file)
        self.assertEqual(err.line, line)

    def test_malformed_json_names_file_and_line(self):
        with open(self.d.orders_path, "a") as f:
            f.write("{not json}\n")
        self._expect(InputValidationError, "orders.jsonl", len(walk.ORDERS) + 1)

    def test_blank_line_is_rejected_not_skipped(self):
        with open(self.d.orders_path, "a") as f:
            f.write("\n")
        self._expect(InputValidationError, "orders.jsonl", len(walk.ORDERS) + 1)

    def test_wrong_type_int_field(self):
        bad = dict(walk.ORDERS[0])
        bad["row_id"] = "bad-type"
        bad["event_time"] = 7.5
        _append(self.d.orders_path, bad)
        self._expect(InputValidationError, "orders.jsonl", len(walk.ORDERS) + 1)

    def test_bool_is_not_an_integer(self):
        bad = dict(walk.ORDERS[0])
        bad["row_id"] = "bad-bool"
        bad["amount"] = True
        _append(self.d.orders_path, bad)
        self._expect(InputValidationError, "orders.jsonl", len(walk.ORDERS) + 1)

    def test_integer_outside_int64(self):
        bad = dict(walk.ORDERS[0])
        bad["row_id"] = "big"
        bad["known_time"] = 2 ** 63
        _append(self.d.orders_path, bad)
        self._expect(InputValidationError, "orders.jsonl", len(walk.ORDERS) + 1)

    def test_empty_row_id_rejected(self):
        bad = dict(walk.ORDERS[0])
        bad["row_id"] = ""
        _append(self.d.orders_path, bad)
        self._expect(InputValidationError, "orders.jsonl", len(walk.ORDERS) + 1)

    def test_unknown_field_rejected(self):
        bad = dict(walk.ORDERS[0])
        bad["row_id"] = "extra"
        bad["surprise"] = 1
        _append(self.d.orders_path, bad)
        self._expect(InputValidationError, "orders.jsonl", len(walk.ORDERS) + 1)

    def test_empty_business_interval(self):
        bad = hj_util.h("bad", "C", 5, 5, 0, None, 0, value="S")
        _append(self.d.cust_path, bad)
        self._expect(InvalidIntervalError, "customer.jsonl",
                     len(walk.CUSTOMER_HISTORY) + 1)

    def test_reversed_business_interval(self):
        bad = hj_util.h("bad", "C", 9, 8, 0, None, 0, value="S")
        _append(self.d.cust_path, bad)
        self._expect(InvalidIntervalError, "customer.jsonl",
                     len(walk.CUSTOMER_HISTORY) + 1)

    def test_empty_knowledge_interval(self):
        bad = hj_util.h("bad", "C", 0, 10, 3, 3, 0, value="S")
        _append(self.d.cust_path, bad)
        self._expect(InvalidIntervalError, "customer.jsonl",
                     len(walk.CUSTOMER_HISTORY) + 1)

    def test_deleted_with_value_rejected(self):
        bad = hj_util.h("bad", "C", 0, 10, 0, None, 0, deleted=True, value="S")
        _append(self.d.cust_path, bad)
        self._expect(InputValidationError, "customer.jsonl",
                     len(walk.CUSTOMER_HISTORY) + 1)

    def test_non_boolean_deleted_rejected(self):
        bad = hj_util.h("bad", "C", 0, 10, 0, None, 0, deleted=1, value=None)
        _append(self.d.cust_path, bad)
        self._expect(InputValidationError, "customer.jsonl",
                     len(walk.CUSTOMER_HISTORY) + 1)

    def test_non_string_value_rejected(self):
        bad = hj_util.h("bad", "C", 0, 10, 0, None, 0, value=9)
        _append(self.d.cust_path, bad)
        self._expect(InputValidationError, "customer.jsonl",
                     len(walk.CUSTOMER_HISTORY) + 1)


class DuplicateIds(unittest.TestCase):
    def setUp(self):
        self.d = hj_util.JsonlDir()
        self.d.write(walk.ORDERS, walk.CUSTOMER_HISTORY, walk.STORE_HISTORY)

    def tearDown(self):
        self.d.cleanup()

    def test_duplicate_row_id_reports_both_locations(self):
        dup = dict(walk.ORDERS[0])
        _append(self.d.orders_path, dup)
        with self.assertRaises(DuplicateIdError) as ctx:
            build_warehouse(**self.d.build_kwargs())
        err = ctx.exception
        self.assertEqual(err.line, len(walk.ORDERS) + 1)
        self.assertEqual(err.first_line, 1)
        self.assertTrue(err.file.endswith("orders.jsonl"))

    def test_duplicate_version_id_within_one_file(self):
        dup = dict(walk.CUSTOMER_HISTORY[0])  # reuses version_id c-v1
        dup["key"] = "OTHER"
        _append(self.d.cust_path, dup)
        with self.assertRaises(DuplicateIdError) as ctx:
            build_warehouse(**self.d.build_kwargs())
        self.assertEqual(ctx.exception.first_line, 1)
        self.assertEqual(ctx.exception.line, len(walk.CUSTOMER_HISTORY) + 1)

    def test_duplicate_version_id_across_the_two_history_files(self):
        # version ids are globally unique across customer and store history
        cross = dict(walk.STORE_HISTORY[0])
        cross["version_id"] = "c-v1"  # collides with a customer version id
        _append(self.d.store_path, cross)
        with self.assertRaises(DuplicateIdError) as ctx:
            build_warehouse(**self.d.build_kwargs())
        self.assertTrue(ctx.exception.file.endswith("store.jsonl"))


class FailureAtomicity(unittest.TestCase):
    def test_failed_build_keeps_previous_complete_result(self):
        d = hj_util.JsonlDir()
        d.write(walk.ORDERS, walk.CUSTOMER_HISTORY, walk.STORE_HISTORY)
        try:
            build_warehouse(**d.build_kwargs())
            before = counts(d.out_path)
            self.assertTrue(before["partition_complete"])
            # corrupt the input with a duplicate and rebuild into the same dir
            _append(d.orders_path, dict(walk.ORDERS[0]))
            with self.assertRaises(DuplicateIdError):
                build_warehouse(**d.build_kwargs())
            # the old, complete result is still fully queryable
            after = counts(d.out_path)
            self.assertEqual(after["reconcile"], before["reconcile"])
            self.assertEqual(len(list_facts(d.out_path)), 4)
            # no leftover temp build trees in the parent
            leftovers = [
                n for n in os.listdir(d.dir)
                if n.startswith(".warehouse-build-") or n.startswith(".warehouse-old-")
            ]
            self.assertEqual(leftovers, [])
        finally:
            d.cleanup()

    def test_no_partial_output_on_first_build_failure(self):
        d = hj_util.JsonlDir()
        d.write(walk.ORDERS, walk.CUSTOMER_HISTORY, walk.STORE_HISTORY)
        try:
            _append(d.orders_path, dict(walk.ORDERS[0]))  # duplicate
            with self.assertRaises(DuplicateIdError):
                build_warehouse(**d.build_kwargs())
            self.assertFalse(os.path.exists(d.out_path))
        finally:
            d.cleanup()


class Determinism(unittest.TestCase):
    """File permutation and read chunk size cannot change the result."""

    def _build(self, chunk, seed):
        d = hj_util.JsonlDir()
        d.write(walk.ORDERS, walk.CUSTOMER_HISTORY, walk.STORE_HISTORY)
        # shuffle every input independently into a different on-disk order
        hj_util.rewrite_permuted(d.orders_path, walk.ORDERS, seed=seed)
        hj_util.rewrite_permuted(d.cust_path, walk.CUSTOMER_HISTORY, seed=seed + 1)
        hj_util.rewrite_permuted(d.store_path, walk.STORE_HISTORY, seed=seed + 2)
        build_warehouse(**d.build_kwargs(), chunk=chunk)
        facts = list_facts(d.out_path)
        quar = []
        from histjoin import list_quarantine, lineage_for
        quar = list_quarantine(d.out_path)
        lineage = {
            o["row_id"]: lineage_for(d.out_path, o["row_id"]) for o in walk.ORDERS
        }
        return d, facts, quar, lineage

    def test_permutation_and_chunk_invariance(self):
        baseline = None
        for chunk in (1, 7, 2000):
            for seed in (0, 42, 99):
                d, facts, quar, lineage = self._build(chunk, seed)
                try:
                    snapshot = json.dumps(
                        {"facts": facts, "quar": quar, "lineage": lineage},
                        sort_keys=True, ensure_ascii=False,
                    )
                    if baseline is None:
                        baseline = snapshot
                    else:
                        self.assertEqual(snapshot, baseline)
                finally:
                    d.cleanup()

    def test_output_order_is_row_id_codepoint_order(self):
        d = hj_util.JsonlDir()
        # include row ids whose codepoint order differs from naive/insert order
        orders = [
            hj_util.o("B2", "C", 10, 5),
            hj_util.o("a1", "C", 10, 5),
            hj_util.o("A10", "C", 10, 5),
            hj_util.o("é1", "C", 10, 5),
        ]
        cust = [hj_util.h("s", "C", 0, 100, 0, None, 0, value="S")]
        store = [hj_util.h("r", "S", 0, 100, 0, None, 0, value="R")]
        d.write(orders, cust, store)
        try:
            build_warehouse(**d.build_kwargs())
            ids = [f["row_id"] for f in list_facts(d.out_path)]
            self.assertEqual(ids, sorted(ids))
            self.assertEqual(ids, ["A10", "B2", "a1", "é1"])
        finally:
            d.cleanup()


if __name__ == "__main__":
    unittest.main()
