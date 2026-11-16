#!/usr/bin/env python3
"""Record execution evidence to a JSON file.

    python3 demo/record_evidence.py [OUTPUT_JSON]

This script does not assert correctness from plan metadata: it starts from a
fresh fact database, builds materialisations through the product, runs a set of
scenarios, and records for each:

* the answer;
* `base_rows_scanned` (distinct fact rows physically examined by the probe
  UDF inside the query, so the number reflects the access path actually used);
* the residual worlds / SQL that were executed against the fact table;
* the materialisation data files that were opened, with size and SHA-256, and
  an OS-level stat taken after the run;
* whether any full base scan happened.

Run the test suite first if you want a from-scratch proof: the tests compare
these answers against an independent enumerator.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from matview import api, catalog  # noqa: E402
from matview import stable  # noqa: E402
from matview.region import describe_region  # noqa: E402

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
    {"name": "M1", "predicate": {"op": "range", "col": "d", "lo": 1, "hi": 3},
     "group_cols": ["d"], "measures": ["amount"]},
    {"name": "M2", "predicate": {"op": "range", "col": "d", "lo": 3, "hi": 5},
     "group_cols": ["d"], "measures": ["amount"]},
]
SCENARIOS = {
    "global_all_dates": {"group_by": [], "measures": MEASURES},
    "per_date": {"group_by": ["d"], "measures": MEASURES},
    "null_dates_only": {
        "group_by": ["d"], "measures": MEASURES,
        "where": {"op": "is_null", "col": "d"},
    },
    "dates_2_to_4": {
        "group_by": ["d"], "measures": MEASURES,
        "where": {"op": "range", "col": "d", "lo": 2, "hi": 4},
    },
    "not_equal_3": {
        "group_by": ["d"], "measures": MEASURES,
        "where": {"op": "and", "args": [
            {"op": "not", "arg": {"op": "is_null", "col": "d"}},
            {"op": "not", "arg": {"op": "eq", "col": "d", "val": 3}},
        ]},
    },
}


def file_probe(path):
    st = os.stat(path)
    return {"path": os.path.abspath(path), "size": st.st_size,
            "sha256": stable.sha256_file(path), "mtime": st.st_mtime}


def main(out_path):
    import tempfile

    work = tempfile.mkdtemp(prefix="matview-evidence-")
    fact = os.path.join(work, "facts.sqlite")
    catalogue = os.path.join(work, "catalogue")
    api.create_fact_db(fact, SCHEMA, FACTS)
    sid = api.build_snapshot(catalogue, fact, SPECS, SCHEMA, label="evidence")
    cat = catalog.Catalogue(catalogue)
    snap = cat.snapshot(sid)

    product_files = {}
    for mid in snap["materialisations"]:
        d = cat.mat_dir(sid, mid)
        product_files[snap["names"][mid]] = {
            "mat_id": mid,
            "meta": file_probe(os.path.join(d, "MAT.json")),
            "data": file_probe(os.path.join(d, "data.sqlite")),
        }

    report = {
        "catalogue": os.path.abspath(catalogue),
        "snapshot_id": sid,
        "snapshot_fact_digest": snap["fact_digest"],
        "n_facts": snap["n_facts"],
        "product_files_at_build": product_files,
        "scenarios": {},
        "checks": {},
    }

    for name, q in SCENARIOS.items():
        e = api.explain(catalogue, sid, q)
        reads = e["evidence"]["materialisation_reads"]
        opened = {}
        for r in reads:
            opened.setdefault(r["mat_id"], r["data_file"])
        report["scenarios"][name] = {
            "answer": e["result"],
            "base_rows_scanned": e["evidence"]["base_rows_scanned"],
            "base_rows_admitted": e["evidence"]["base_rows_admitted"],
            "base_full_scan": e["evidence"]["base_full_scan"],
            "total_facts": e["evidence"]["fact_table"]["total_facts"],
            "materialisation_segments": [
                {
                    "name": s["name"], "gid": s["gid"],
                    "group_key": s["group_key"],
                    "rows_credited": s["rows_credited"],
                    "region": describe_region_escaped(s),
                }
                for s in e["segments"] if s["kind"] == "materialisation"
            ],
            "base_segments": [
                {k: s.get(k) for k in ("reason", "worlds", "rows_scanned",
                                       "rows_admitted", "full_base_scan")}
                for s in e["segments"] if s["kind"] == "base"
            ],
            "world_queries": e["evidence"]["world_queries"],
            "mat_files_opened": {
                mid: file_probe(path) for mid, path in opened.items()
            },
        }

    # independent checks on the evidence itself
    g = report["scenarios"]["global_all_dates"]
    report["checks"]["global_base_read_only_null_region"] = (
        g["base_rows_scanned"] == 1 and not g["base_full_scan"]
    )
    credits = [
        (s["name"], s["gid"]) for s in g["materialisation_segments"]
    ]
    report["checks"]["no_group_credited_twice"] = len(credits) == len(set(credits))
    report["checks"]["global_totals"] = (
        g["answer"][0]["cnt"] == 6
        and g["answer"][0]["nn"] == 5
        and g["answer"][0]["total"] == 190
        and g["answer"][0]["mean"]["num"] == 38
        and g["answer"][0]["mean"]["den"] == 1
    )
    nd = report["scenarios"]["null_dates_only"]
    # neither M1 nor M2 covers a NULL date, so this region must come from base
    # -- but only the single NULL-date row, never a table scan
    report["checks"]["null_dates_read_one_row_no_scan"] = (
        nd["base_rows_scanned"] == 1
        and not nd["base_full_scan"]
        and nd["answer"][0]["total"] == 60
    )

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, sort_keys=True)
    print("wrote", out_path)
    for k, v in report["checks"].items():
        print("  %-45s %s" % (k, v))
    return report


def describe_region_escaped(segment):
    return segment.get("region")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "evidence", "evidence.json"
    )
    report = main(out)
    sys.exit(0 if all(report["checks"].values()) else 1)
