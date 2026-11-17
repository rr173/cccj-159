"""Subprocess CLI tests for histjoin (independent process, result dir only)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from histjoin import demo_data as walk  # noqa: E402
from tests import hj_util  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOOT = (
    "import sys; sys.path.insert(0, %r); "
    "from histjoin.cli import main; raise SystemExit(main(sys.argv[1:]))" % ROOT
)


class HistJoinCli(unittest.TestCase):
    def setUp(self):
        self.d = hj_util.JsonlDir(prefix="hj-cli-")
        self.d.write(walk.ORDERS, walk.CUSTOMER_HISTORY, walk.STORE_HISTORY)

    def tearDown(self):
        self.d.cleanup()

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, "-I", "-c", BOOT] + list(args),
            capture_output=True, text=True,
        )

    def test_build_lineage_quarantine_counts_facts(self):
        out = self._run(
            "build", self.d.orders_path, self.d.cust_path,
            self.d.store_path, self.d.out_path, "--chunk", "2",
        )
        self.assertEqual(out.returncode, 0, msg=out.stderr)
        manifest = json.loads(out.stdout)
        self.assertEqual(manifest["counts"]["facts"], 4)
        self.assertEqual(manifest["counts"]["quarantine"], 2)

        counts = json.loads(self._run("counts", self.d.out_path).stdout)
        self.assertTrue(counts["partition_complete"])

        facts = json.loads(self._run("facts", self.d.out_path).stdout)
        self.assertEqual(sorted(f["row_id"] for f in facts),
                         ["O1", "O2", "O3", "O4"])

        quar = json.loads(self._run("quarantine", self.d.out_path).stdout)
        self.assertEqual({q["row_id"] for q in quar}, {"O5", "O6"})

        lin = json.loads(self._run("lineage", self.d.out_path, "O6").stdout)
        self.assertEqual(lin["outcome"], "quarantine")
        self.assertEqual(
            lin["quarantine"]["top_versions"], ["s2-north", "s2-south"]
        )

    def test_input_error_exits_nonzero_with_file_and_line(self):
        with open(self.d.orders_path, "a") as f:
            f.write(json.dumps(dict(walk.ORDERS[0])) + "\n")  # duplicate row_id
        out = self._run(
            "build", self.d.orders_path, self.d.cust_path,
            self.d.store_path, self.d.out_path,
        )
        self.assertEqual(out.returncode, 1)
        payload = json.loads(out.stderr.strip().splitlines()[-1])
        self.assertEqual(payload["reason"], "duplicate_id")
        self.assertTrue(payload["file"].endswith("orders.jsonl"))
        self.assertEqual(payload["line"], len(walk.ORDERS) + 1)
        self.assertEqual(payload["first_line"], 1)
        self.assertFalse(os.path.exists(self.d.out_path))

    def test_unknown_row_id_is_a_query_error(self):
        self._run(
            "build", self.d.orders_path, self.d.cust_path,
            self.d.store_path, self.d.out_path,
        )
        out = self._run("lineage", self.d.out_path, "NOPE")
        self.assertEqual(out.returncode, 1)
        payload = json.loads(out.stderr.strip().splitlines()[-1])
        self.assertEqual(payload["reason"], "lineage_not_found")


if __name__ == "__main__":
    unittest.main()
