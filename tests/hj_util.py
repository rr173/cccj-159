"""Shared helpers for the histjoin test suite."""

from __future__ import annotations

import json
import os
import shutil
import tempfile

MIN_I64 = -(2 ** 63)
MAX_I64 = 2 ** 63 - 1


class JsonlDir:
    """A scratch directory holding the three JSONL inputs and build outputs."""

    def __init__(self, prefix="histjoin-test-"):
        self.dir = tempfile.mkdtemp(prefix=prefix)
        self.orders_path = os.path.join(self.dir, "orders.jsonl")
        self.cust_path = os.path.join(self.dir, "customer.jsonl")
        self.store_path = os.path.join(self.dir, "store.jsonl")
        self.out_path = os.path.join(self.dir, "warehouse")

    def write(self, orders, customer, store):
        _dump(self.orders_path, orders)
        _dump(self.cust_path, customer)
        _dump(self.store_path, store)
        return self

    def build_kwargs(self):
        return dict(
            orders_path=self.orders_path,
            customer_hist_path=self.cust_path,
            store_hist_path=self.store_path,
            out_dir=self.out_path,
        )

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def _dump(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def rewrite_permuted(path, rows, seed=0):
    """Rewrite one JSONL with a deterministic non-input order permutation."""
    import random

    rng = random.Random(seed)
    order = list(range(len(rows)))
    rng.shuffle(order)
    with open(path, "w", encoding="utf-8") as f:
        for i in order:
            f.write(json.dumps(rows[i], ensure_ascii=False) + "\n")


def h(version_id, key, valid_from, valid_to, known_from, known_to,
      priority, deleted=False, value=None):
    """Compact history-record constructor."""
    return {
        "version_id": version_id, "key": key,
        "valid_from": valid_from, "valid_to": valid_to,
        "known_from": known_from, "known_to": known_to,
        "priority": priority, "deleted": deleted, "value": value,
    }


def o(row_id, customer, event_time, known_time, amount=None):
    return {
        "row_id": row_id, "customer": customer,
        "event_time": event_time, "known_time": known_time, "amount": amount,
    }
