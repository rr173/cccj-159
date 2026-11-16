"""CLI end to end, and structured-JSON rejection paths."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from matview import api, pred  # noqa: E402
from tests import fixtures  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA = fixtures.DEMO_SCHEMA
ROWS = fixtures.DEMO_ROWS
SPECS = [
    {"name": "M1", "predicate": {"op": "range", "col": "d", "lo": 1, "hi": 3},
     "group_cols": ["d"], "measures": ["amount"]},
]
QUERY = {"group_by": ["d"],
         "measures": [{"kind": "count_star", "as": "c"},
                      {"kind": "sum", "col": "amount", "as": "s"}]}


class Cli(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="matview-cli-")
        self.cat = os.path.join(self.dir, "catalogue")
        self.fact = os.path.join(self.dir, "facts.sqlite")
        api.create_fact_db(self.fact, SCHEMA, ROWS)
        self.schema_p = self._write("schema.json", SCHEMA)
        self.specs_p = self._write("specs.json", SPECS)
        self.query_p = self._write("query.json", QUERY)

    def _write(self, name, obj):
        p = os.path.join(self.dir, name)
        with open(p, "w") as f:
            json.dump(obj, f)
        return p

    def _run(self, *args, stdin=None):
        # -I ignores PYTHONPATH, so run with an explicit path bootstrap that
        # keeps the interpreter isolated from the cwd
        child = (
            "import sys; sys.path.insert(0, %r); "
            "from matview.cli import main; raise SystemExit(main(sys.argv[1:]))"
            % ROOT
        )
        return subprocess.run(
            [sys.executable, "-I", "-c", child] + list(args),
            cwd=os.path.dirname(self.dir),
            capture_output=True,
            text=True,
            input=stdin,
        )

    def test_build_plan_execute_explain(self):
        out = self._run("build", self.cat, self.fact, self.schema_p, self.specs_p)
        self.assertEqual(out.returncode, 0, msg=out.stderr)
        sid = json.loads(out.stdout)["snapshot_id"]
        for cmd in ("plan", "execute", "explain"):
            out = self._run(cmd, self.cat, sid, self.query_p)
            self.assertEqual(out.returncode, 0, msg=out.stderr)
            payload = json.loads(out.stdout)
            self.assertIn("snapshot_id", payload)
        ex = json.loads(self._run("execute", self.cat, sid, self.query_p).stdout)
        self.assertEqual(sum(r["c"] for r in ex["rows"]), 6)
        ins = json.loads(self._run("inspect", self.cat).stdout)
        self.assertEqual(len(ins["snapshots"]), 1)

    def test_query_from_stdin(self):
        out = self._run("build", self.cat, self.fact, self.schema_p, self.specs_p)
        sid = json.loads(out.stdout)["snapshot_id"]
        out = self._run("execute", self.cat, sid, "-", stdin=json.dumps(QUERY))
        self.assertEqual(out.returncode, 0, msg=out.stderr)
        self.assertEqual(sum(r["c"] for r in json.loads(out.stdout)["rows"]), 6)


class StructuredQueryValidation(unittest.TestCase):
    def setUp(self):
        self.schema = pred.Schema.from_json(SCHEMA)

    def test_type_mismatch_rejected(self):
        with self.assertRaises(pred.PredicateError):
            pred.parse({"op": "eq", "col": "d", "val": "one"}, self.schema)
        with self.assertRaises(pred.PredicateError):
            pred.parse({"op": "eq", "col": "d", "val": None}, self.schema)

    def test_range_on_text_rejected(self):
        s = pred.Schema("row_id", {"d": "int", "region": "text"})
        with self.assertRaises(pred.PredicateError):
            pred.parse({"op": "range", "col": "region", "lo": "a", "hi": "z"}, s)

    def test_unknown_field_rejected(self):
        with self.assertRaises(pred.SchemaError):
            pred.parse({"op": "eq", "col": "nope", "val": 1}, self.schema)

    def test_sum_on_text_rejected(self):
        from matview.query import parse_query
        from matview.errors import QueryError

        s = pred.Schema("row_id", {"d": "int", "region": "text"})
        with self.assertRaises(QueryError):
            parse_query(
                {"group_by": [], "measures": [{"kind": "sum", "col": "region", "as": "x"}]},
                s,
            )

    def test_sql_text_is_not_accepted(self):
        with self.assertRaises(pred.PredicateError):
            pred.parse({"op": "sql", "text": "d > 3"}, self.schema)
        with self.assertRaises(pred.PredicateError):
            pred.parse("d > 3", self.schema)


if __name__ == "__main__":
    unittest.main()
