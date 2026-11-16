"""Persistence, registration, crash-safety and snapshot identity."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import fixtures, refimpl, compare  # noqa: E402
from matview import api, catalog, pred  # noqa: E402
from matview.errors import (  # noqa: E402
    CatalogueError,
    MaterialisationBuildError,
    SnapshotMismatchError,
)

M1 = {
    "name": "M1",
    "predicate": {"op": "range", "col": "d", "lo": 1, "hi": 3},
    "group_cols": ["d"],
    "measures": ["amount"],
}

GLOBAL = {
    "group_by": [],
    "measures": [
        {"kind": "count_star", "as": "cnt"},
        {"kind": "count", "col": "amount", "as": "nn"},
        {"kind": "sum", "col": "amount", "as": "total"},
        {"kind": "avg", "col": "amount", "as": "mean"},
    ],
}


class Reopen(unittest.TestCase):
    def test_fresh_catalogue_handle_executes(self):
        ws = fixtures.Workspace()
        sid = ws.build([M1])
        # a brand-new process opens only the directory path
        out = subprocess.run(
            [
                sys.executable,
                "-I",
                os.path.join(os.path.dirname(__file__), "reopen_child.py"),
                ws.catalogue,
                sid,
                json.dumps(GLOBAL),
            ],
            capture_output=True,
            text=True,
            env={"PYTHONPATH": os.path.dirname(os.path.dirname(__file__))},
        )
        self.assertEqual(out.returncode, 0, msg=out.stderr)
        payload = json.loads(out.stdout)
        self.assertEqual(payload["result"][0]["cnt"], 6)
        self.assertEqual(payload["result"][0]["total"], 190)
        self.assertEqual(payload["snapshots"], 1)

    def test_registration_survives_new_catalogue_object(self):
        ws = fixtures.Workspace()
        sid = ws.build([M1])
        cat2 = catalog.Catalogue(ws.catalogue)
        regs = cat2.registrations(sid)
        self.assertEqual(len(regs), 1)
        e = api.explain(ws.catalogue, sid, GLOBAL)
        self.assertEqual(e["result"][0]["cnt"], 6)


class Registration(unittest.TestCase):
    def test_unregistered_materialisation_is_not_used(self):
        ws = fixtures.Workspace()
        sid = ws.build([M1], register=False)
        cat = catalog.Catalogue(ws.catalogue)
        mid = cat.snapshot(sid)["materialisations"][0]
        self.assertFalse(cat.is_registered(sid, mid))
        e = api.explain(ws.catalogue, sid, GLOBAL)
        reasons = {r["mat_id"]: r["reason"] for r in e["rejections"]}
        self.assertEqual(reasons[mid], "not_registered")
        # correct answer, but with no materialisation credit
        self.assertEqual(e["result"][0]["cnt"], 6)
        self.assertFalse(
            [s for s in e["segments"] if s["kind"] == "materialisation"]
        )
        # explicit registration makes it usable
        api.register(ws.catalogue, sid, mid)
        e2 = api.explain(ws.catalogue, sid, GLOBAL)
        self.assertTrue(
            [s for s in e2["segments"] if s["kind"] == "materialisation"]
        )
        self.assertEqual(e2["result"][0]["cnt"], 6)

    def test_registration_is_idempotent(self):
        ws = fixtures.Workspace()
        sid = ws.build([M1])
        cat = catalog.Catalogue(ws.catalogue)
        mid = cat.snapshot(sid)["materialisations"][0]
        api.register(ws.catalogue, sid, mid)
        api.register(ws.catalogue, sid, mid)
        self.assertEqual(len(catalog.Catalogue(ws.catalogue).registrations(sid)), 1)


class BuildFailureLeavesNothing(unittest.TestCase):
    def test_failed_build_has_no_snapshot_and_no_temp(self):
        ws = fixtures.Workspace()
        bad = dict(M1)
        bad["measures"] = ["not_a_field"]
        bad["name"] = "BAD"
        with self.assertRaises(Exception):
            ws.build([bad])
        snaps = os.path.join(ws.catalogue, "snapshots")
        self.assertEqual(os.listdir(snaps), [])
        cat = catalog.Catalogue(ws.catalogue)
        self.assertEqual(cat.list_snapshots(), [])

    def test_build_interrupted_midway_leaves_nothing(self):
        """Simulate a crash after one materialisation was written."""
        ws = fixtures.Workspace()
        orig = catalog.build.write_materialisation
        calls = {"n": 0}

        def explode(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated crash")
            return orig(*a, **kw)

        catalog.build.write_materialisation = explode
        try:
            with self.assertRaises(RuntimeError):
                ws.build(
                    [
                        M1,
                        dict(M1, name="M2", predicate={
                            "op": "range", "col": "d", "lo": 3, "hi": 5}),
                    ]
                )
        finally:
            catalog.build.write_materialisation = orig
        snaps = os.path.join(ws.catalogue, "snapshots")
        self.assertEqual(os.listdir(snaps), [])
        self.assertEqual(catalog.Catalogue(ws.catalogue).list_snapshots(), [])

    def test_leftover_temp_dir_is_collected_and_never_indexed(self):
        ws = fixtures.Workspace()
        sid = ws.build([M1])
        stray = os.path.join(ws.catalogue, "snapshots", "snapshot-build-deadbeef")
        os.makedirs(stray)
        with open(os.path.join(stray, "SNAPSHOT.json"), "w") as f:
            f.write(json.dumps({"format": "matview-snapshot/1", "snapshot_id": "x"}))
        cat = catalog.Catalogue(ws.catalogue)
        self.assertFalse(os.path.exists(stray))
        self.assertEqual([s["snapshot_id"] for s in cat.list_snapshots()], [sid])


class SnapshotIdentity(unittest.TestCase):
    def test_fact_change_after_build_is_rejected(self):
        ws = fixtures.Workspace()
        sid = ws.build([M1])
        conn = sqlite3.connect(ws.fact_path)
        conn.execute("UPDATE facts SET amount=999 WHERE row_id=1")
        conn.commit()
        conn.close()
        with self.assertRaises(SnapshotMismatchError):
            api.plan(ws.catalogue, sid, GLOBAL)

    def test_second_snapshot_of_same_fact_file_refused(self):
        ws = fixtures.Workspace()
        ws.build([M1])
        with self.assertRaises(SnapshotMismatchError):
            ws.build([M1], snapshot_id="another")

    def test_materialisation_from_other_snapshot_reported(self):
        ws = fixtures.Workspace()
        sid1 = ws.build([M1], snapshot_id="s1")
        # a second fact file with different facts and its own snapshot
        ws2 = fixtures.WorkspaceSameCatalogue(ws) if False else None
        # tamper with M1's metadata to point at a foreign snapshot
        cat = catalog.Catalogue(ws.catalogue)
        mid = cat.snapshot(sid1)["materialisations"][0]
        mpath = os.path.join(cat.mat_dir(sid1, mid), "MAT.json")
        with open(mpath) as f:
            meta = json.load(f)
        meta["snapshot_id"] = "some-other-snapshot"
        with open(mpath, "w") as f:
            json.dump(meta, f)
        e = api.explain(ws.catalogue, sid1, GLOBAL)
        rep = e["materialisations"][0]
        self.assertFalse(rep["usable"])
        self.assertEqual(rep["reason"], "different_snapshot")
        self.assertEqual(e["result"][0]["cnt"], 6)


class TamperedFiles(unittest.TestCase):
    def _mat(self, ws, sid):
        cat = catalog.Catalogue(ws.catalogue)
        mid = cat.snapshot(sid)["materialisations"][0]
        return cat, mid

    def test_changed_data_file_rejected(self):
        ws = fixtures.Workspace()
        sid = ws.build([M1])
        cat, mid = self._mat(ws, sid)
        data = os.path.join(cat.mat_dir(sid, mid), "data.sqlite")
        conn = sqlite3.connect(data)
        conn.execute('UPDATE groups SET "sum:amount"=999')
        conn.commit()
        conn.close()
        e = api.explain(ws.catalogue, sid, GLOBAL)
        rep = e["materialisations"][0]
        self.assertFalse(rep["usable"])
        self.assertEqual(rep["reason"], "data_file_corrupt")
        self.assertEqual(e["result"][0]["total"], 190)

    def test_missing_state_column_rejected(self):
        ws = fixtures.Workspace()
        sid = ws.build([M1])
        cat, mid = self._mat(ws, sid)
        data = os.path.join(cat.mat_dir(sid, mid), "data.sqlite")
        # remove the saved sum column and update the digest so we reach the
        # structural check rather than the checksum check
        conn = sqlite3.connect(data)
        conn.execute("ALTER TABLE groups DROP COLUMN \"sum:amount\"")
        conn.commit()
        conn.close()
        mpath = os.path.join(cat.mat_dir(sid, mid), "MAT.json")
        with open(mpath) as f:
            meta = json.load(f)
        meta["data_sha256"] = None
        with open(mpath, "w") as f:
            json.dump(meta, f)
        e = api.explain(ws.catalogue, sid, GLOBAL)
        rep = e["materialisations"][0]
        self.assertFalse(rep["usable"])
        self.assertEqual(rep["reason"], "missing_aggregate_state")
        self.assertEqual(e["result"][0]["total"], 190)


if __name__ == "__main__":
    unittest.main()
