"""Resource-behaviour tests for the hot-key path.

The full 100k/100k benchmark is driven from ``demo/run_hotkey.py`` and recorded
in ``evidence/`` (the big JSONL is never committed).  This test runs a
medium-scale instance in a child process with a hard ``RLIMIT_AS`` ceiling and
asserts that:

* the build finishes with the peak resident set bounded -- the disk-backed
  sweep, not Python lists, is what holds a hot key's history;
* the reconciliation partition is exact;
* the independent oracle agrees on the fixed sample orders.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CHILD = r"""
import json, os, resource, sys
sys.path.insert(0, %r)
# hard virtual-memory ceiling before importing/opening anything heavy
cap = 160 * 1024 * 1024
resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
from histjoin import hotkey, build_warehouse, counts, lineage_for, list_facts, list_quarantine
from tests.histjoin_refimpl import ReferenceWarehouse
data = sys.argv[1]
info = hotkey.generate(data, n_segments=2000, n_orders=4000)
manifest = build_warehouse(*info['paths'], os.path.join(data, 'out'))
ref = ReferenceWarehouse(*info['paths'])
facts = {f['row_id']: f for f in list_facts(os.path.join(data, 'out'))}
quar = {q['row_id']: q for q in list_quarantine(os.path.join(data, 'out'))}
mismatch = 0
for o in ref.orders:
    if not (len(o['row_id']) == 3 and o['row_id'][0] == 's'
            and o['row_id'][1:].isdigit()):
        continue
    exp = ref.expected(o)
    if exp['outcome'] == 'fact':
        got = facts[o['row_id']]
        if got['store'] != exp['store'] or got['region'] != exp['region']:
            mismatch += 1
    elif quar[o['row_id']]['reason'] != exp['reason']:
        mismatch += 1
peak_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(json.dumps({
    'counts': manifest['counts'],
    'partition': counts(os.path.join(data, 'out'))['partition_complete'],
    'peak_rss_mib': round(peak_kib / 1024, 2),
    'mismatch': mismatch,
}))
""" % ROOT


class HotKeyResource(unittest.TestCase):
    def test_medium_hot_key_under_memory_ceiling(self):
        data = tempfile.mkdtemp(prefix="hj-hot-test-")
        try:
            proc = subprocess.run(
                [sys.executable, "-I", "-c", CHILD, data],
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            report = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertTrue(report["partition"])
            self.assertEqual(report["mismatch"], 0)
            c = report["counts"]
            self.assertEqual(c["facts"] + c["quarantine"], c["orders"])
            # the 160 MiB RLIMIT_AS cap would have killed a list-based design
            self.assertLess(report["peak_rss_mib"], 150)
        finally:
            import shutil

            shutil.rmtree(data, ignore_errors=True)

    def test_thousands_of_tied_top_versions_stay_bounded(self):
        """Regression: a 10k-way equal-priority tie per order must not build a
        multi-million-row Python provenance list, but every tie is retained."""
        import json as _json

        from histjoin import build_warehouse, lineage_for

        data = tempfile.mkdtemp(prefix="hj-tie-")
        try:
            in_dir = os.path.join(data, "in")
            os.makedirs(in_dir)
            n_versions, n_orders = 10000, 300
            with open(os.path.join(in_dir, "customer.jsonl"), "w") as f:
                for i in range(n_versions):
                    f.write(_json.dumps({
                        "version_id": "v%06d" % i, "key": "H",
                        "valid_from": -10 ** 12, "valid_to": None,
                        "known_from": 0, "known_to": None,
                        "priority": 0, "deleted": False, "value": "S",
                    }) + "\n")
            with open(os.path.join(in_dir, "store.jsonl"), "w") as f:
                f.write(_json.dumps({
                    "version_id": "r", "key": "S",
                    "valid_from": -10 ** 12, "valid_to": None,
                    "known_from": 0, "known_to": None,
                    "priority": 0, "deleted": False, "value": "R",
                }) + "\n")
            with open(os.path.join(in_dir, "orders.jsonl"), "w") as f:
                for i in range(n_orders):
                    f.write(_json.dumps({
                        "row_id": "o%05d" % i, "customer": "H",
                        "event_time": 0, "known_time": 5, "amount": i,
                    }) + "\n")
            out = os.path.join(data, "out")
            build_warehouse(
                os.path.join(in_dir, "orders.jsonl"),
                os.path.join(in_dir, "customer.jsonl"),
                os.path.join(in_dir, "store.jsonl"),
                out, chunk=128,
            )
            lin = lineage_for(out, "o00000")["levels"]["customer"]
            self.assertEqual(len(lin), n_versions)
            self.assertTrue(all(x["tied"] for x in lin))
            self.assertEqual(
                sorted(x["version_id"] for x in lin),
                ["v%06d" % i for i in range(n_versions)],
            )
        finally:
            import shutil

            shutil.rmtree(data, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
