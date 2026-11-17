"""Deterministic hot-key stress generator.

Builds the worst-case shape for a naive point query:

* one hot customer with 100 000 **disjoint** business intervals, so at any
  event time exactly one base version is open -- but a naive product still
  has to wade through every ``valid_from <= t`` row;
* knowledge intervals are interleaved and a late higher-priority correction
  shadows part of every base interval, so the top tier is not always the
  obvious row and backtracking/conflict/deletion paths get exercised;
* ten stores, each with 10 000 disjoint business intervals (the second level
  is hot too), plus late same-priority corrections that create a small,
  predictable number of level-2 conflicts;
* 100 000 orders, deliberately written in a shuffled on-disk order, including
  null customers, unknown keys and orders pinned to the int64 end points.

Nothing here is random at run time: a fixed seed makes the byte content (and
therefore every expected sample) reproducible.  Generated files are large and
must never be committed; only the small run report is kept.
"""

from __future__ import annotations

import json
import os
import random

from .validate import MAX_I64, MIN_I64

SEED = 20261116
N_SEGMENTS = 100_000
N_STORES = 10
N_ORDERS = 100_000

# reserved, hand-checkable sample orders (see expected_samples())
N_SAMPLES = 40
# orders outside the hot key (each is still a real input order)
N_NULL_CUSTOMER = 500
N_GHOST = 500
EDGE_ROW_IDS = ("z-edge-min", "z-edge-max")


def _span(n_segments=N_SEGMENTS):
    # disjoint integer segments covering a wide but strictly int64-bounded
    # range; base + n*width remains well inside [-2^63, 2^63)
    total = 2_000_000_000_000  # 2e12, leaving huge headroom from the extremes
    width = max(1, total // n_segments)
    return total, width


def generate(out_dir, n_segments=N_SEGMENTS, n_orders=N_ORDERS, seed=SEED):
    """Write the three JSONL inputs into ``out_dir``; return file paths."""
    os.makedirs(out_dir, exist_ok=True)
    rng = random.Random(seed)
    total, width = _span(n_segments)
    base = MIN_I64 // 2
    # fixed sub-counts at the contractual 100k scale; scaled down proportionally
    # (never below the reserved samples) for small smoke runs
    scale = n_orders / N_ORDERS
    n_samples = min(N_SAMPLES, max(4, n_orders // 10))
    n_null = max(0, int(round(N_NULL_CUSTOMER * scale)))
    n_ghost = max(0, int(round(N_GHOST * scale)))

    customer_hist = []
    store_of_segment = []
    for i in range(n_segments):
        vf = base + i * width
        vt = base + (i + 1) * width
        store = "S%d" % (i % N_STORES)
        store_of_segment.append(store)
        # base version: known from 0, priority 0
        customer_hist.append({
            "version_id": "c-base-%07d" % i, "key": "HOT",
            "valid_from": vf, "valid_to": vt,
            "known_from": 0, "known_to": None,
            "priority": 0, "deleted": False, "value": store,
        })
        # late correction on the inner half of the segment, known later,
        # alternating between a different store and a deletion, priority 1
        mid = vf + width // 2
        if i % 7 == 0:
            correction = {
                "version_id": "c-del-%07d" % i, "key": "HOT",
                "valid_from": mid, "valid_to": vt,
                "known_from": 500, "known_to": None,
                "priority": 1, "deleted": True, "value": None,
            }
        else:
            other = "S%d" % ((i + 1) % N_STORES)
            correction = {
                "version_id": "c-fix-%07d" % i, "key": "HOT",
                "valid_from": mid, "valid_to": vt,
                "known_from": 500, "known_to": None,
                "priority": 1, "deleted": False, "value": other,
            }
        customer_hist.append(correction)

    # second level: each store gets n_segments/N_STORES disjoint intervals
    store_hist = []
    per_store = n_segments // N_STORES
    for s in range(N_STORES):
        store = "S%d" % s
        for j in range(per_store):
            # same global segment numbering as the customer level: segment
            # (j*N_STORES + s) maps to store s, so its interval lines up
            seg = j * N_STORES + s
            vf = base + seg * width
            vt = vf + width
            region = "R%d" % (j % 5)
            store_hist.append({
                "version_id": "s-base-%02d-%07d" % (s, j),
                "key": store,
                "valid_from": vf, "valid_to": vt,
                "known_from": 0, "known_to": None,
                "priority": 0, "deleted": False, "value": region,
            })
            # a late same-priority divergence on a thin inner slice creates
            # a deterministic set of level-2 conflicts (no recency tie-break)
            if j % 13 == 0:
                inner_from = vf + width // 4
                inner_to = inner_from + max(1, width // 8)
                store_hist.append({
                    "version_id": "s-conf-%02d-%07d" % (s, j),
                    "key": store,
                    "valid_from": inner_from, "valid_to": inner_to,
                    "known_from": 900, "known_to": None,
                    "priority": 0, "deleted": False,
                    "value": "R-CONFLICT",
                })

    # orders: reserve the first n_samples row ids with fixed coordinates
    orders, samples = _build_orders(
        rng, n_orders, n_segments, base, width, store_of_segment,
        n_samples, n_null, n_ghost,
    )

    paths = (
        os.path.join(out_dir, "orders.jsonl"),
        os.path.join(out_dir, "customer.jsonl"),
        os.path.join(out_dir, "store.jsonl"),
    )
    _write_shuffled(paths[0], orders, rng)
    _write_shuffled(paths[1], customer_hist, rng)
    _write_shuffled(paths[2], store_hist, rng)
    return {
        "paths": paths,
        "n_orders": len(orders),
        "n_customer_history": len(customer_hist),
        "n_store_history": len(store_hist),
        "sample_row_ids": [s["row_id"] for s in samples],
        "base": base,
        "width": width,
    }


def _build_orders(rng, n_orders, n_segments, base, width, store_of_segment,
                  n_samples, n_null, n_ghost):
    orders = []
    samples = []
    # fixed samples: spread over segments, half before/after correction kt
    for k in range(n_samples):
        seg = (k * (n_segments // n_samples)) % n_segments
        vf = base + seg * width
        in_inner = (k % 2 == 0)
        event_time = vf + (width // 4 if not in_inner else (3 * width) // 4)
        known_time = 600 if in_inner else 100
        row_id = "s%02d" % k
        orders.append({
            "row_id": row_id, "customer": "HOT",
            "event_time": event_time, "known_time": known_time,
            "amount": 1000 + k,
        })
        samples.append(orders[-1])

    regular = n_orders - n_samples - n_null - n_ghost - len(EDGE_ROW_IDS)
    for i in range(regular):
        seg = rng.randrange(n_segments)
        vf = base + seg * width
        event_time = rng.randint(vf, vf + width - 1)
        known_time = rng.choice([0, 100, 499, 500, 501, 900, 1000])
        orders.append({
            "row_id": "o%07d" % i, "customer": "HOT",
            "event_time": event_time, "known_time": known_time,
            "amount": rng.choice([None, rng.randint(-1000, 1000)]),
        })
    for i in range(n_null):
        orders.append({
            "row_id": "n%04d" % i, "customer": None,
            "event_time": rng.randint(0, 1_000_000),
            "known_time": rng.randint(0, 1_000_000), "amount": rng.randint(1, 9),
        })
    for i in range(n_ghost):
        orders.append({
            "row_id": "g%04d" % i, "customer": "NO-SUCH-CUSTOMER",
            "event_time": rng.randint(0, 1_000_000),
            "known_time": rng.randint(0, 1_000_000), "amount": None,
        })
    orders.append({
        "row_id": EDGE_ROW_IDS[0], "customer": "HOT",
        "event_time": MIN_I64, "known_time": 0, "amount": 1,
    })
    orders.append({
        "row_id": EDGE_ROW_IDS[1], "customer": "HOT",
        "event_time": MAX_I64, "known_time": 1000, "amount": 2,
    })
    return orders, samples


def _write_shuffled(path, rows, rng):
    rng.shuffle(rows)  # on-disk order must not drive any decision
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
