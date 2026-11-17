"""Independent reference enumerator for the history-join semantics.

This module is deliberately separate from the product: it reads the three
JSONL inputs itself, keeps the raw histories in plain Python lists, and for
every order scans *every* version of the matching key to compute the expected
adjudication by direct application of the protocol.  It imports none of
``histjoin``'s index, sweep or adjudication code, so a property/randomised
test that agrees with both is checking the product against an independent
oracle rather than re-deriving expectations from the code under test.

The algorithm is intentionally the obvious O(orders * versions) one:

1. candidates = versions with matching key whose business half-open interval
   contains event_time AND whose knowledge half-open interval contains
   known_time (integer comparisons only);
2. no candidates -> gap; otherwise keep only max-priority candidates;
3. the top tier is accepted only when every row agrees on (deleted, value):
   deleted -> "deleted", deleted=false+value=null -> "null_value", otherwise
   the shared non-null value; disagreement -> "conflict".
"""

from __future__ import annotations

import json

MIN_I64 = -(2 ** 63)
MAX_I64 = 2 ** 63 - 1

GAP = "gap"
DELETED = "deleted"
NULL_VALUE = "null_value"
CONFLICT = "conflict"
OK = "ok"
MISSING_KEY = "missing_key"


def _read_jsonl(path):
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                raise ValueError("blank line %s:%d" % (path, line_no))
            rows.append(json.loads(line))
    return rows


def _contains(vfrom, vto, t):
    # half-open [from, to); null to = positive infinity; exact integer compare
    return vfrom <= t and (vto is None or t < vto)


def _candidates(history, key, event_time, known_time):
    out = []
    for h in history:
        if h["key"] != key:
            continue
        if _contains(h["valid_from"], h["valid_to"], event_time) and \
                _contains(h["known_from"], h["known_to"], known_time):
            out.append(h)
    return out


def _adjudicate(candidates):
    """Return (status, value, sorted_top_version_ids)."""
    if not candidates:
        return GAP, None, []
    top_priority = max(h["priority"] for h in candidates)
    top = [h for h in candidates if h["priority"] == top_priority]
    signatures = {(bool(h["deleted"]), h["value"]) for h in top}
    versions = sorted(h["version_id"] for h in top)
    if len(signatures) != 1:
        return CONFLICT, None, versions
    deleted, value = next(iter(signatures))
    if deleted:
        return DELETED, None, versions
    if value is None:
        return NULL_VALUE, None, versions
    return OK, value, versions


class ReferenceWarehouse:
    """Brute-force expected outcome for every order in the three inputs."""

    def __init__(self, orders_path, customer_path, store_path):
        self.orders = _read_jsonl(orders_path)
        self.customer = _read_jsonl(customer_path)
        self.store = _read_jsonl(store_path)

    def expected(self, order):
        """Full expected disposition for one order dict."""
        if order["customer"] is None:
            return {
                "row_id": order["row_id"],
                "outcome": "quarantine",
                "failed_level": "customer",
                "reason": MISSING_KEY,
                "store": None,
                "region": None,
                "customer_top": [],
                "store_top": [],
                "upstream": [],
            }
        c_cand = _candidates(self.customer, order["customer"],
                             order["event_time"], order["known_time"])
        c_status, store, c_top = _adjudicate(c_cand)
        if c_status != OK:
            return {
                "row_id": order["row_id"],
                "outcome": "quarantine",
                "failed_level": "customer",
                "reason": c_status,
                "store": None,
                "region": None,
                "customer_top": c_top,
                "store_top": [],
                "upstream": [],
            }
        s_cand = _candidates(self.store, store,
                             order["event_time"], order["known_time"])
        s_status, region, s_top = _adjudicate(s_cand)
        if s_status != OK:
            return {
                "row_id": order["row_id"],
                "outcome": "quarantine",
                "failed_level": "store",
                "reason": s_status,
                "store": store,
                "region": None,
                "customer_top": c_top,
                "store_top": s_top,
                "upstream": c_top,
            }
        return {
            "row_id": order["row_id"],
            "outcome": "fact",
            "failed_level": None,
            "reason": None,
            "store": store,
            "region": region,
            "customer_top": c_top,
            "store_top": s_top,
            "upstream": c_top,
        }

    def all_expected(self):
        return [self.expected(o) for o in self.orders]
