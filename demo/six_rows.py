#!/usr/bin/env python3
"""Six-row worked example from the brief, run end to end.

    python3 demo/six_rows.py [OUTPUT_DIR]

Builds a catalogue from real fact rows, registers two date-range
materialisations, then explains and executes the global and per-date queries,
printing the answer and the execution evidence (base rows actually read,
materialisation files actually opened).
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from matview import api  # noqa: E402

SCHEMA = {"row_id": "row_id", "fields": {"d": "int", "amount": "int"}}
FACTS = [
    {"row_id": 1, "d": 1, "amount": 10},
    {"row_id": 2, "d": 2, "amount": None},
    {"row_id": 3, "d": 3, "amount": 30},
    {"row_id": 4, "d": 4, "amount": 40},
    {"row_id": 5, "d": 5, "amount": 50},
    {"row_id": 6, "d": None, "amount": 60},
]
MEASURES = [
    {"kind": "count_star", "as": "cnt"},
    {"kind": "count", "col": "amount", "as": "nn"},
    {"kind": "sum", "col": "amount", "as": "total"},
    {"kind": "avg", "col": "amount", "as": "mean"},
]
SPECS = [
    {
        "name": "M1",
        "predicate": {"op": "range", "col": "d", "lo": 1, "hi": 3},
        "group_cols": ["d"],
        "measures": ["amount"],
    },
    {
        "name": "M2",
        "predicate": {"op": "range", "col": "d", "lo": 3, "hi": 5},
        "group_cols": ["d"],
        "measures": ["amount"],
    },
]


def main(outdir=None):
    outdir = outdir or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "_run"
    )
    os.makedirs(outdir, exist_ok=True)
    fact_path = os.path.join(outdir, "facts.sqlite")
    catalogue = os.path.join(outdir, "catalogue")
    import shutil
    for stale in (catalogue,):
        if os.path.exists(stale):
            shutil.rmtree(stale)
    if os.path.exists(fact_path):
        os.remove(fact_path)
    api.create_fact_db(fact_path, SCHEMA, FACTS)
    sid = api.build_snapshot(
        catalogue, fact_path, SPECS, SCHEMA, label="six-row demo",
        snapshot_id="demo-main",
    )

    global_q = {"group_by": [], "measures": MEASURES}
    per_date_q = {"group_by": ["d"], "measures": MEASURES}

    print("snapshot:", sid)
    print("\n== global answer (all dates) ==")
    g = api.explain(catalogue, sid, global_q)
    (row,) = g["result"]
    print("count(*)=%d  count(amount)=%d  sum=%s  avg=%s/%s" % (
        row["cnt"], row["nn"], row["total"],
        row["mean"]["num"], row["mean"]["den"],
    ))
    print("base facts read:", g["evidence"]["base_rows_scanned"], "of",
          g["evidence"]["fact_table"]["total_facts"])
    print("materialisation groups credited:",
          g["coverage"]["materialisation_groups"])
    for s in g["segments"]:
        if s["kind"] == "materialisation":
            print("  from %-3s group d=%-4r rows=%d" % (
                s["name"], s["group_key"][0], s["rows_credited"]))
        else:
            print("  base residual worlds:", s.get("worlds"),
                  "-> rows read", s.get("rows_scanned"),
                  "admitted", s.get("rows_admitted"))

    print("\n== per-date answer ==")
    p = api.explain(catalogue, sid, per_date_q)
    for r in p["result"]:
        avg = r["mean"]
        print("  d=%-5r cnt=%d nn=%d sum=%-5r avg=%s" % (
            r["group"]["d"], r["cnt"], r["nn"], r["total"],
            "%d/%d" % (avg["num"], avg["den"]) if avg else None,
        ))

    print("\n== coarse M3 (no date grouping) asked for dates 2..4 ==")
    # A second fact file (same rows) carries the coarse M3 snapshot; this is
    # how the protocol keeps every materialisation tied to one base snapshot.
    fact2 = os.path.join(outdir, "facts2.sqlite")
    if os.path.exists(fact2):
        os.remove(fact2)
    api.create_fact_db(fact2, SCHEMA, FACTS)
    sid3 = api.build_snapshot(
        catalogue,
        fact2,
        [{
            "name": "M3",
            "predicate": {"op": "range", "col": "d", "lo": 1, "hi": 3},
            "group_cols": [],
            "measures": ["amount"],
        }],
        SCHEMA,
        snapshot_id="demo-coarse",
    )
    e3 = api.explain(catalogue, sid3, {
        "group_by": ["d"],
        "where": {"op": "range", "col": "d", "lo": 2, "hi": 4},
        "measures": MEASURES,
    })
    rep = e3["materialisations"][0]
    print("M3 usable:", rep["usable"], "reason:", rep["reason"])
    for r in e3["result"]:
        print("  d=%-5r cnt=%d sum=%r" % (r["group"]["d"], r["cnt"], r["total"]))
    print("residual worlds:", [s.get("worlds") for s in e3["segments"]
                              if s["kind"] == "base"][:1])


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
