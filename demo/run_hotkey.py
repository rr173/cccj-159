"""Run the hot-key build under the brief's resource contract.

Contract: one CPU (enforced with ``taskset`` when available), a 256 MiB hard
memory cap (``resource.setrlimit(RLIMIT_AS)`` inside the process), completed
within 180 s.  The runner measures wall time and peak RSS, checks the
reconciliation counts, and validates an independent set of fixed samples with
the brute-force oracle (which never uses product resolution code).

Usage::

    python demo/run_hotkey.py generate DIR      # create the big JSONL inputs
    python demo/run_hotkey.py build DIR         # constrained build + verify
    python demo/run_hotkey.py all DIR           # both, writing a small report
"""

from __future__ import annotations

import json
import os
import resource
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from histjoin import build_warehouse, counts, list_facts, list_quarantine  # noqa: E402
from histjoin import hotkey  # noqa: E402
from tests.histjoin_refimpl import ReferenceWarehouse  # noqa: E402

MEMORY_LIMIT_BYTES = 256 * 1024 * 1024
TIME_LIMIT_SECONDS = 180
# leave headroom under RLIMIT_AS for the interpreter and SQLite's own allocations
RLIMIT_HEADROOM = 48 * 1024 * 1024


def _set_limits():
    resource.setrlimit(
        resource.RLIMIT_AS,
        (MEMORY_LIMIT_BYTES - RLIMIT_HEADROOM, MEMORY_LIMIT_BYTES),
    )


def generate(data_dir):
    info = hotkey.generate(data_dir)
    print("generated:", json.dumps({
        k: v for k, v in info.items() if k != "paths"
    }, indent=2))
    return info


def build(data_dir, out_dir=None, chunk=2000, cache_kib=32768):
    _set_limits()
    paths = (
        os.path.join(data_dir, "orders.jsonl"),
        os.path.join(data_dir, "customer.jsonl"),
        os.path.join(data_dir, "store.jsonl"),
    )
    out_dir = out_dir or os.path.join(data_dir, "warehouse")
    start = time.monotonic()
    manifest = build_warehouse(
        paths[0], paths[1], paths[2], out_dir,
        chunk=chunk, cache_kib=cache_kib,
    )
    elapsed = time.monotonic() - start
    peak_rss_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    report = {
        "elapsed_seconds": round(elapsed, 3),
        "peak_rss_mib": round(peak_rss_kib / 1024, 2),
        "memory_limit_mib": 256,
        "time_limit_seconds": TIME_LIMIT_SECONDS,
        "within_time_limit": elapsed <= TIME_LIMIT_SECONDS,
        "chunk": chunk,
        "cache_kib": cache_kib,
        "counts": manifest["counts"],
    }
    rec = counts(out_dir)
    report["partition_complete"] = rec["partition_complete"]
    report["facts"] = len(list_facts(out_dir))
    report["quarantine"] = len(list_quarantine(out_dir))
    return report, out_dir


def _load_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            rows.append(json.loads(line))
    return rows


def verify_samples(data_dir, out_dir):
    """Independent oracle check of the fixed sample orders only.

    The brute-force enumerator intentionally keeps raw histories in Python
    lists, so it is not run under the build's memory cap and is fed a narrowed
    input: the sample orders plus every history version, but only after the
    product warehouse has already been built under the cap.  It still imports
    none of the product's resolution code.
    """
    orders_all = _load_jsonl(os.path.join(data_dir, "orders.jsonl"))
    sample_orders = [
        o for o in orders_all
        if len(o["row_id"]) == 3 and o["row_id"][0] == "s"
        and o["row_id"][1:].isdigit()
    ]
    narrow_dir = os.path.join(data_dir, "_oracle_narrow")
    os.makedirs(narrow_dir, exist_ok=True)
    op = os.path.join(narrow_dir, "orders.jsonl")
    cp = os.path.join(data_dir, "customer.jsonl")
    sp = os.path.join(data_dir, "store.jsonl")
    with open(op, "w", encoding="utf-8") as f:
        for o in sample_orders:
            f.write(json.dumps(o) + "\n")
    ref = ReferenceWarehouse(op, cp, sp)
    facts = {f["row_id"]: f for f in list_facts(out_dir)}
    quar = {q["row_id"]: q for q in list_quarantine(out_dir)}
    checked = 0
    sample_results = []
    for order in ref.orders:
        exp = ref.expected(order)
        rid = order["row_id"]
        if exp["outcome"] == "fact":
            got = facts[rid]
            ok = got["store"] == exp["store"] and got["region"] == exp["region"]
        else:
            got = quar[rid]
            ok = got["reason"] == exp["reason"]
        sample_results.append({
            "row_id": rid,
            "expected": exp["outcome"] + (
                "/" + exp["reason"] if exp["reason"] else ""
            ),
            "store": exp["store"],
            "region": exp["region"],
            "ok": ok,
        })
        checked += 1
    return checked, sample_results


def main(argv):
    cmd, data_dir = argv[1], argv[2]
    if cmd == "generate":
        generate(data_dir)
        return 0
    if cmd == "build":
        # the capped process only builds and measures; the brute-force oracle
        # is deliberately list-based and runs separately via "verify"
        report, out_dir = build(data_dir)
        report_path = os.path.join(data_dir, "hotkey-report.json")
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, sort_keys=True)
        print(json.dumps(report, indent=2, sort_keys=True))
        if not report["within_time_limit"]:
            return 3
        if not report["partition_complete"]:
            return 4
        return 0
    if cmd == "verify":
        out_dir = os.path.join(data_dir, "warehouse")
        checked, samples = verify_samples(data_dir, out_dir)
        result = {
            "samples_checked": checked,
            "all_samples_ok": all(s["ok"] for s in samples),
            "samples": samples,
        }
        with open(os.path.join(data_dir, "hotkey-samples.json"), "w",
                  encoding="utf-8") as f:
            json.dump(result, f, indent=2, sort_keys=True)
        print(json.dumps({"samples_checked": checked,
                          "all_samples_ok": result["all_samples_ok"]}, indent=2))
        return 0 if result["all_samples_ok"] else 5
    if cmd == "all":
        rc = main(["run", "generate", data_dir])
        if rc:
            return rc
        rc = main(["run", "build", data_dir])
        if rc:
            return rc
        return main(["run", "verify", data_dir])
    sys.stderr.write("unknown command %r\n" % cmd)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
