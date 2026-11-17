#!/usr/bin/env python3
"""End-to-end demonstration of the offline history warehouse.

    python3 demo/history_warehouse.py [OUTPUT_DIR]

Runs the brief's canonical scenario (customer C, the S1/S2 correction, the
late deletion and the same-priority region conflict), builds the warehouse,
prints the facts, the isolation list and per-row lineage, then hands the
produced facts to the *existing* matview entry points and checks the regional
sums: East=50, West=20, North=30 with neither quarantined amount included.
"""

import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from histjoin import (  # noqa: E402
    build_warehouse,
    counts,
    lineage_for,
    list_facts,
    list_quarantine,
)
from histjoin import demo_data as walk  # noqa: E402


def _write(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main(outdir=None):
    outdir = outdir or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "_run", "history"
    )
    if os.path.exists(outdir):
        shutil.rmtree(outdir)
    os.makedirs(outdir)
    orders_p = os.path.join(outdir, "orders.jsonl")
    cust_p = os.path.join(outdir, "customer.jsonl")
    store_p = os.path.join(outdir, "store.jsonl")
    _write(orders_p, walk.ORDERS)
    _write(cust_p, walk.CUSTOMER_HISTORY)
    _write(store_p, walk.STORE_HISTORY)
    warehouse = os.path.join(outdir, "warehouse")

    manifest = build_warehouse(orders_p, cust_p, store_p, warehouse)
    print("== build manifest counts ==")
    print(json.dumps(manifest["counts"], sort_keys=True))

    print("\n== facts (row_id: store / region / amount) ==")
    for f in list_facts(warehouse):
        print("  %s: %s / %s / %s" % (
            f["row_id"], f["store"], f["region"], f["amount"]))

    print("\n== isolation list ==")
    for q in list_quarantine(warehouse):
        print("  %s: level=%s reason=%s top=%s upstream=%s" % (
            q["row_id"], q["failed_level"], q["reason"],
            q["top_versions"], q["upstream_versions"]))

    print("\n== lineage for the two quarantined orders ==")
    for rid in ("O5", "O6"):
        print(" ", rid, json.dumps(lineage_for(warehouse, rid),
                                   ensure_ascii=False, sort_keys=True))

    print("\n== reconciliation ==")
    rec = counts(warehouse)
    print(" ", rec["reconcile"], "partition_complete =",
          rec["partition_complete"])

    # hand the published facts to the existing matview query layer
    from matview import api

    schema = json.load(open(os.path.join(warehouse, "schema.json")))
    catalogue = os.path.join(outdir, "catalogue")
    specs = [{
        "name": "REGION_SUM",
        "predicate": {"op": "and", "args": []},
        "group_cols": ["region"],
        "measures": ["amount"],
    }]
    sid = api.build_snapshot(
        catalogue, os.path.join(warehouse, "facts.sqlite"),
        specs, schema, snapshot_id="history-demo",
    )
    result = api.execute(catalogue, sid, {
        "group_by": ["region"],
        "measures": [
            {"kind": "count_star", "as": "cnt"},
            {"kind": "sum", "col": "amount", "as": "total"},
        ],
    })
    sums = {r["group"]["region"]: r["total"] for r in result["rows"]}
    print("\n== existing query layer: sum(amount) by region ==")
    for region in sorted(sums):
        print("  %s = %s" % (region, sums[region]))
    assert sums == walk.EXPECTED_REGION_SUMS, sums
    print("\nOK: matches hand-computed", walk.EXPECTED_REGION_SUMS,
          "(quarantined amounts 50 and 60 excluded)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
