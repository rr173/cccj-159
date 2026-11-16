"""Shared fixtures: fact tables, catalogues, materialisation specs."""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from matview import api  # noqa: E402

DEMO_SCHEMA = {
    "row_id": "row_id",
    "fields": {"d": "int", "amount": "int"},
}

# The six-row demo from the brief: dates 1,2,3,4,5,NULL with amounts
# 10, NULL, 30, 40, 50, 60.
DEMO_ROWS = [
    {"row_id": 1, "d": 1, "amount": 10},
    {"row_id": 2, "d": 2, "amount": None},
    {"row_id": 3, "d": 3, "amount": 30},
    {"row_id": 4, "d": 4, "amount": 40},
    {"row_id": 5, "d": 5, "amount": 50},
    {"row_id": 6, "d": None, "amount": 60},
]

# Duplicate business values under distinct row_ids: three rows with d=7,
# amount=40 (two of them with an identical (d, amount) pair), plus a row whose
# amount repeats an earlier one under a different date.
DUP_SCHEMA = {
    "row_id": "row_id",
    "fields": {"d": "int", "amount": "int"},
}
DUP_ROWS = [
    {"row_id": 1, "d": 1, "amount": 10},
    {"row_id": 2, "d": 2, "amount": None},
    {"row_id": 3, "d": 3, "amount": 30},
    {"row_id": 4, "d": 4, "amount": 40},
    {"row_id": 5, "d": 5, "amount": 50},
    {"row_id": 6, "d": None, "amount": 60},
    {"row_id": 7, "d": 7, "amount": 40},
    {"row_id": 8, "d": 7, "amount": 40},
    {"row_id": 9, "d": 7, "amount": 40},
]

ALL_MEASURES = [
    {"kind": "count_star", "as": "cnt"},
    {"kind": "count", "col": "amount", "as": "nn"},
    {"kind": "sum", "col": "amount", "as": "total"},
    {"kind": "avg", "col": "amount", "as": "mean"},
]


class Workspace:
    def __init__(self, schema=None, rows=None):
        self.dir = tempfile.mkdtemp(prefix="matview-test-")
        self.fact_path = os.path.join(self.dir, "facts.sqlite")
        self.catalogue = os.path.join(self.dir, "catalogue")
        self.schema = schema or DEMO_SCHEMA
        self.rows = rows if rows is not None else DEMO_ROWS
        api.create_fact_db(self.fact_path, self.schema, self.rows)

    def build(self, specs, snapshot_id=None, register=True):
        return api.build_snapshot(
            self.catalogue, self.fact_path, specs, self.schema,
            snapshot_id=snapshot_id, register=register,
        )

    def explain(self, sid, query, budget=None):
        kw = {} if budget is None else {"budget": budget}
        return api.explain(self.catalogue, sid, query, **kw)

    def execute(self, sid, query, budget=None):
        kw = {} if budget is None else {"budget": budget}
        return api.execute(self.catalogue, sid, query, **kw)

    def plan(self, sid, query, budget=None):
        kw = {} if budget is None else {"budget": budget}
        return api.plan(self.catalogue, sid, query, **kw)

    def fields(self):
        return dict(self.schema["fields"])
