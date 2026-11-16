"""The catalogue directory and snapshot lifecycle.

Layout
------
    catalogue/
        CATALOGUE.json              format marker
        FIELDS.json                 the fixed fact schema
        index.sqlite                registrations (rebuildable from disk)
        snapshots/
            <sid>/
                SNAPSHOT.json       written LAST, inside a temp dir
                manifest.json       name -> materialisation id
                mat/<mid>/
                    MAT.json
                    data.sqlite

Lifecycle guarantees
--------------------
* **Build**: a snapshot is assembled in ``snapshots/snapshot-build-*`` and only
  appears under its final name by a single ``os.rename`` after every file has
  been written, re-read and verified.  A crash at any earlier point leaves only
  a temp directory (garbage-collected on next open), never something that can
  be registered or discovered by a fresh process.
* **Register** is a separate, explicit step that consults the disk (the
  product scans real fact rows itself).  User-supplied row counts are never
  read; `build_snapshot` derives every stored number from real facts.
* **Reopen**: any process opening the directory rebuilds the index from
  finished snapshot directories, then executes plans against them.  Index and
  disk are cross-checked; a mismatch is a hard error, not a silent reuse.
* **Staleness**: every query re-derives the base fact file digest and refuses
  a snapshot whose facts have changed (``snapshot_mismatch``).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import time
import uuid

from . import build, pred, stable, userdb
from .errors import (
    CatalogueError,
    MaterialisationBuildError,
    SnapshotMismatchError,
)

FORMAT = "matview-catalogue/1"
FIELDS_FORMAT = "matview-fields/1"
SNAPSHOT_FORMAT = "matview-snapshot/1"


# ---------------------------------------------------------------------------
# tiny durable-file helpers
# ---------------------------------------------------------------------------

def write_json_atomic(path: str, obj) -> None:
    tmp = path + ".tmp-" + uuid.uuid4().hex
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, sort_keys=True, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fsync_dir(os.path.dirname(path))


def read_json(path: str):
    with open(path, "rb") as f:
        return json.loads(f.read().decode("utf-8"))


def fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:  # pragma: no cover
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover
        pass
    finally:
        os.close(fd)


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# ---------------------------------------------------------------------------
# facts: the product creates the real fact database
# ---------------------------------------------------------------------------

def create_fact_db(path: str, schema: pred.Schema, rows) -> None:
    """Create the fixed fact table from real rows supplied by the product.

    The layer's own code writes every row to disk here; later all metadata
    (row counts, digests) is re-derived by scanning, never trusted from input.
    """
    path = os.path.abspath(path)
    if os.path.exists(path):
        raise CatalogueError("fact database already exists: %s" % path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    names = [schema.row_id] + sorted(schema.fields)
    ddl = []
    for n in names:
        typ = "INTEGER" if (n == schema.row_id or schema.fields.get(n) == "int") else "TEXT"
        ddl.append("%s %s" % (quote_ident(n), typ))
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("PRAGMA synchronous=FULL")
        con.execute("CREATE TABLE facts (%s)" % ", ".join(ddl))
        con.execute(
            "CREATE UNIQUE INDEX ux_facts_row_id ON facts (%s)"
            % quote_ident(schema.row_id)
        )
        # Access paths for the residual.  Indexes change no fact value; they
        # let a proven narrow residual (e.g. the NULL branch) read only the
        # rows that branch can possibly admit.
        for fname in sorted(schema.fields):
            con.execute(
                "CREATE INDEX ix_facts_%s ON facts (%s)"
                % (uuid.uuid4().hex[:8], quote_ident(fname))
            )
        payload = []
        seen = set()
        for row in rows:
            rid = row.get(schema.row_id)
            if rid is None:
                raise CatalogueError("every fact needs a row_id")
            if rid in seen:
                raise CatalogueError("duplicate row_id %r (row_id must be unique)" % (rid,))
            seen.add(rid)
            vals = []
            for n in names:
                v = row.get(n)
                if n != schema.row_id:
                    ft = schema.fields[n]
                    if v is not None:
                        if ft == "int" and (isinstance(v, bool) or not isinstance(v, int)):
                            raise CatalogueError(
                                "field %r is int, got %r" % (n, type(v).__name__)
                            )
                        if ft == "text" and not isinstance(v, str):
                            raise CatalogueError(
                                "field %r is text, got %r" % (n, type(v).__name__)
                            )
                    else:
                        if ft == "int" and False:
                            pass
                vals.append(v)
            payload.append(tuple(vals))
        con.executemany(
            "INSERT INTO facts (%s) VALUES (%s)"
            % (", ".join(quote_ident(n) for n in names), ",".join("?" * len(names))),
            payload,
        )
        con.commit()
    finally:
        con.close()
    fsync_dir(os.path.dirname(path))


def read_facts(path: str, schema: pred.Schema) -> dict:
    """Read the whole fact table as {row_id: {field: value}} (product side)."""
    con = userdb.connect_ro(path)
    try:
        names = sorted(schema.fields)
        cols = [schema.row_id] + names
        out = {}
        for vals in con.execute(
            "SELECT %s FROM facts ORDER BY %s"
            % (", ".join(quote_ident(c) for c in cols), quote_ident(schema.row_id))
        ):
            d = dict(zip(cols, vals))
            out[d[schema.row_id]] = {c: d[c] for c in names}
        return out
    finally:
        con.close()


def facts_digest(leaves: dict, schema: pred.Schema) -> str:
    payload = [
        [rid, [leaves[rid][c] for c in sorted(schema.fields)]]
        for rid in sorted(leaves, key=lambda r: (0, r) if isinstance(r, int) else (1, str(r)))
    ]
    return stable.sha256_hex(stable.stable_json(payload))


# ---------------------------------------------------------------------------
# the catalogue
# ---------------------------------------------------------------------------

class Catalogue:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        os.makedirs(self.path, exist_ok=True)
        self._init_root()
        self._init_index()
        self._collect_garbage()
        self.resync_from_disk()

    # -- root / index ------------------------------------------------------

    def _init_root(self):
        marker = os.path.join(self.path, "CATALOGUE.json")
        if not os.path.exists(marker):
            write_json_atomic(marker, {"format": FORMAT, "created_at": time.time()})
        else:
            meta = read_json(marker)
            if meta.get("format") != FORMAT:
                raise CatalogueError(
                    "not a %s directory (found %r)" % (FORMAT, meta.get("format"))
                )
        os.makedirs(os.path.join(self.path, "snapshots"), exist_ok=True)

    def _index_path(self):
        return os.path.join(self.path, "index.sqlite")

    def _init_index(self):
        con = sqlite3.connect(self._index_path())
        try:
            con.execute("PRAGMA journal_mode=DELETE")
            con.execute(
                """CREATE TABLE IF NOT EXISTS snapshots (
                     snapshot_id TEXT PRIMARY KEY,
                     fact_path TEXT NOT NULL,
                     schema_json TEXT NOT NULL,
                     n_facts INTEGER NOT NULL,
                     fact_digest TEXT NOT NULL,
                     created_at REAL NOT NULL,
                     label TEXT)"""
            )
            con.execute(
                """CREATE TABLE IF NOT EXISTS registrations (
                     snapshot_id TEXT NOT NULL,
                     mat_id TEXT NOT NULL,
                     registered_at REAL NOT NULL,
                     PRIMARY KEY (snapshot_id, mat_id))"""
            )
            con.commit()
        finally:
            con.close()

    def _con(self):
        con = sqlite3.connect(self._index_path())
        con.row_factory = sqlite3.Row
        return con

    def _collect_garbage(self):
        """Remove temp build directories left by a killed process."""
        base = os.path.join(self.path, "snapshots")
        for name in os.listdir(base):
            if name.startswith("snapshot-build-"):
                shutil.rmtree(os.path.join(base, name), ignore_errors=True)

    # -- schema ------------------------------------------------------------

    @property
    def _fields_path(self):
        return os.path.join(self.path, "FIELDS.json")

    def schema(self) -> pred.Schema:
        if not os.path.exists(self._fields_path):
            raise CatalogueError("catalogue has no fixed fact schema yet")
        obj = read_json(self._fields_path)
        if obj.get("format") != FIELDS_FORMAT:
            raise CatalogueError("bad fields file format")
        return pred.Schema(obj["row_id"], obj["fields"])

    def ensure_schema(self, schema: pred.Schema) -> None:
        if not os.path.exists(self._fields_path):
            write_json_atomic(
                self._fields_path,
                {
                    "format": FIELDS_FORMAT,
                    "row_id": schema.row_id,
                    "fields": schema.fields,
                    "field_digest": schema.field_digest,
                },
            )
            return
        stored = self.schema()
        if stored.row_id != schema.row_id or stored.fields != schema.fields:
            raise SchemaMismatchError(
                "catalogue schema %r/%r differs from %r/%r"
                % (stored.row_id, stored.fields, schema.row_id, schema.fields)
            )

    # -- snapshot paths ----------------------------------------------------

    def _snapshots_dir(self):
        return os.path.join(self.path, "snapshots")

    def snapshot_dir(self, sid: str) -> str:
        return os.path.join(self._snapshots_dir(), sid)

    def mat_dir(self, sid: str, mid: str) -> str:
        return os.path.join(self.snapshot_dir(sid), "mat", mid)

    def mat_meta(self, sid: str, mid: str) -> dict:
        d = self.mat_dir(sid, mid)
        p = os.path.join(d, "MAT.json")
        if not os.path.exists(p):
            raise CatalogueError("no materialisation %s/%s" % (sid, mid))
        return read_json(p)

    # -- resync ------------------------------------------------------------

    def resync_from_disk(self) -> dict:
        """Make the index match finished snapshots on disk (idempotent)."""
        con = self._con()
        problems = []
        try:
            seen = {}
            for name in sorted(os.listdir(self._snapshots_dir())):
                d = os.path.join(self._snapshots_dir(), name)
                if name.startswith("snapshot-build-") or not os.path.isdir(d):
                    continue
                sp = os.path.join(d, "SNAPSHOT.json")
                if not os.path.exists(sp):
                    continue
                try:
                    info = read_json(sp)
                except (OSError, ValueError) as exc:
                    problems.append({"snapshot_id": name, "reason": "unreadable_snapshot",
                                     "message": str(exc)})
                    continue
                if info.get("format") != SNAPSHOT_FORMAT or info.get("snapshot_id") != name:
                    problems.append({"snapshot_id": name, "reason": "invalid_snapshot"})
                    continue
                if not self._snapshot_complete(d, info):
                    problems.append({"snapshot_id": name, "reason": "incomplete_snapshot"})
                    continue
                seen[name] = info
                row = con.execute(
                    "SELECT * FROM snapshots WHERE snapshot_id=?", (name,)
                ).fetchone()
                if row is None:
                    con.execute(
                        "INSERT INTO snapshots VALUES (?,?,?,?,?,?,?)",
                        (
                            name,
                            info["fact_path"],
                            pred.json_dumps(info["schema"]),
                            info["n_facts"],
                            info["fact_digest"],
                            info["created_at"],
                            info.get("label", ""),
                        ),
                    )
                else:
                    if (
                        row["fact_path"] != info["fact_path"]
                        or row["fact_digest"] != info["fact_digest"]
                        or row["n_facts"] != info["n_facts"]
                        or row["schema_json"] != pred.json_dumps(info["schema"])
                    ):
                        raise SnapshotMismatchError(
                            "index entry for %s disagrees with the finished snapshot on disk"
                            % name
                        )
                registered = {r[0] for r in con.execute(
                    "SELECT mat_id FROM registrations WHERE snapshot_id=?", (name,))}
                for mid in info["materialisations"]:
                    if info["registration"][mid].get("by_default") and mid not in registered:
                        con.execute(
                            "INSERT OR IGNORE INTO registrations VALUES (?,?,?)",
                            (name, mid, info["registration"][mid].get("registered_at", time.time())),
                        )
            for row in con.execute("SELECT snapshot_id FROM snapshots").fetchall():
                if row["snapshot_id"] not in seen:
                    con.execute(
                        "DELETE FROM registrations WHERE snapshot_id=?", (row["snapshot_id"],)
                    )
                    con.execute(
                        "DELETE FROM snapshots WHERE snapshot_id=?", (row["snapshot_id"],)
                    )
            con.commit()
        finally:
            con.close()
        return {"snapshots": len(seen), "problems": problems}

    def _snapshot_complete(self, d: str, info: dict) -> bool:
        if not os.path.exists(info.get("fact_path", "")):
            return False
        for mid in info.get("materialisations", []):
            if not os.path.exists(os.path.join(d, "mat", mid, "MAT.json")):
                return False
            if not os.path.exists(os.path.join(d, "mat", mid, "data.sqlite")):
                return False
        for mid, reg in info.get("registration", {}).items():
            if reg.get("by_default") and mid not in info.get("materialisations", []):
                return False
        return True

    # -- lookups -----------------------------------------------------------

    def snapshot(self, sid: str) -> dict:
        con = self._con()
        try:
            row = con.execute("SELECT * FROM snapshots WHERE snapshot_id=?", (sid,)).fetchone()
        finally:
            con.close()
        if row is None:
            raise SnapshotMismatchError("unknown snapshot %r" % (sid,))
        info = read_json(os.path.join(self.snapshot_dir(sid), "SNAPSHOT.json"))
        info["index_row"] = dict(row)
        return info

    def list_snapshots(self) -> list:
        con = self._con()
        try:
            return [dict(r) for r in con.execute("SELECT * FROM snapshots ORDER BY created_at")]
        finally:
            con.close()

    def snapshot_for_facts(self, fact_path: str) -> list:
        p = os.path.abspath(fact_path)
        con = self._con()
        try:
            return [
                dict(r)
                for r in con.execute("SELECT * FROM snapshots WHERE fact_path=?", (p,))
            ]
        finally:
            con.close()

    def is_registered(self, sid: str, mid: str) -> bool:
        con = self._con()
        try:
            return con.execute(
                "SELECT 1 FROM registrations WHERE snapshot_id=? AND mat_id=?", (sid, mid)
            ).fetchone() is not None
        finally:
            con.close()

    def registrations(self, sid: str) -> list:
        con = self._con()
        try:
            return [
                dict(r)
                for r in con.execute(
                    "SELECT * FROM registrations WHERE snapshot_id=? ORDER BY registered_at, mat_id",
                    (sid,),
                )
            ]
        finally:
            con.close()

    def register(self, sid: str, mid: str) -> None:
        """Explicitly register an existing, complete materialisation."""
        self.resync_from_disk()
        info = self.snapshot(sid)
        if mid not in info["materialisations"]:
            raise CatalogueError("snapshot %s has no materialisation %s" % (sid, mid))
        if not os.path.exists(os.path.join(self.mat_dir(sid, mid), "MAT.json")):
            raise CatalogueError("materialisation files missing for %s/%s" % (sid, mid))
        con = self._con()
        try:
            con.execute(
                "INSERT OR IGNORE INTO registrations VALUES (?,?,?)",
                (sid, mid, time.time()),
            )
            con.commit()
        finally:
            con.close()

    # -- build -------------------------------------------------------------

    def build_snapshot(
        self,
        fact_path: str,
        specs: list,
        schema: pred.Schema,
        snapshot_id: str = None,
        label: str = "",
        register: bool = True,
    ) -> str:
        """Derive materialisations from real fact rows; publish atomically.

        Each spec::

            {"name": "M1",
             "predicate": <user JSON predicate>,
             "group_cols": ["d"],
             "measures": ["amount"],
             "retained_cols": ["region"]}

        Every aggregate is computed by the product scanning real facts; the
        written file is re-read and re-verified before publication.  Nothing is
        trusted from the caller.  On failure the temp tree is removed.
        """
        self.resync_from_disk()
        self.ensure_schema(schema)
        fact_path = os.path.abspath(fact_path)
        for snap in self.snapshot_for_facts(fact_path):
            raise SnapshotMismatchError(
                "fact file %s is already the base of snapshot %s; build a new "
                "fact snapshot instead of mutating one" % (fact_path, snap["snapshot_id"])
            )
        leaves = read_facts(fact_path, schema)
        digest = facts_digest(leaves, schema)
        n_facts = len(leaves)
        built = []
        tmp = tempfile.mkdtemp(prefix="snapshot-build-", dir=self._snapshots_dir())
        try:
            os.makedirs(os.path.join(tmp, "mat"))
            registration = {}
            for spec in specs:
                name = spec.get("name")
                if not isinstance(name, str) or not name:
                    raise CatalogueError("each materialisation spec needs a name")
                if name in registration:
                    raise CatalogueError("duplicate materialisation name %r" % (name,))
                info = self._build_one(tmp, spec, leaves, schema, fact_path)
                built.append(info)
                registration[info["mat_id"]] = {
                    "name": name,
                    "by_default": bool(spec.get("registered", True)),
                    "registered_at": time.time(),
                }
            sid = snapshot_id or ("snap-" + uuid.uuid4().hex[:16])
            if os.path.exists(self.snapshot_dir(sid)):
                raise CatalogueError("snapshot id already exists: %s" % sid)
            manifest = {
                "format": "matview-snapshot-manifest/1",
                "snapshot_id": sid,
                "mats": {i["mat_id"]: i["name"] for i in built},
            }
            info = {
                "format": SNAPSHOT_FORMAT,
                "snapshot_id": sid,
                "fact_path": fact_path,
                "fact_digest": digest,
                "n_facts": n_facts,
                "schema": schema.as_json(),
                "field_digest": schema.field_digest,
                "materialisations": [i["mat_id"] for i in built],
                "names": {i["mat_id"]: i["name"] for i in built},
                "registration": registration,
                "label": label,
                "created_at": time.time(),
            }
            self._verify_built(tmp, info, leaves, schema)
            write_json_atomic(os.path.join(tmp, "manifest.json"), manifest)
            write_json_atomic(os.path.join(tmp, "SNAPSHOT.json"), info)
            fsync_dir(tmp)
            final = self.snapshot_dir(sid)
            os.rename(tmp, final)
            fsync_dir(self._snapshots_dir())
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        con = self._con()
        try:
            con.execute(
                "INSERT INTO snapshots VALUES (?,?,?,?,?,?,?)",
                (
                    sid,
                    fact_path,
                    pred.json_dumps(info["schema"]),
                    n_facts,
                    digest,
                    info["created_at"],
                    label,
                ),
            )
            if register:
                for mid, reg in registration.items():
                    if reg["by_default"]:
                        con.execute(
                            "INSERT OR IGNORE INTO registrations VALUES (?,?,?)",
                            (sid, mid, reg["registered_at"]),
                        )
            else:
                # built but deliberately not registered: record that decision in
                # the snapshot so a resync does not silently register it later
                info["registration"] = {
                    mid: dict(reg, by_default=False) for mid, reg in registration.items()
                }
                write_json_atomic(
                    os.path.join(self.snapshot_dir(sid), "SNAPSHOT.json"), info
                )
            con.commit()
        finally:
            con.close()
        return sid

    def _build_one(self, tmp, spec, leaves, schema, fact_path):
        name = spec["name"]
        mid = "mat-" + uuid.uuid4().hex[:16]
        mdir = os.path.join(tmp, "mat", mid)
        os.makedirs(mdir)
        p = pred.parse(spec.get("predicate", {"op": "and", "args": []}), schema)
        gcols = list(spec.get("group_cols", []))
        for c in gcols:
            schema.require(c)
        if len(set(gcols)) != len(gcols):
            raise CatalogueError("duplicate group column in %r" % (name,))
        measures = list(spec.get("measures", []))
        for m in measures:
            if not schema.is_measure(m):
                raise CatalogueError(
                    "measure %r in %r must be an int field" % (m, name)
                )
        retained = list(spec.get("retained_cols", []))
        for c in retained:
            schema.require(c)
            if c in gcols:
                raise CatalogueError(
                    "retained column %r in %r is already a group column" % (c, name)
                )
        state_spec = spec.get("state", "full")
        state, retained_map = build.aggregate_leaves(
            leaves, p, gcols, measures, retained, state_spec=state_spec
        )
        base = {
            "snapshot_id": None,
            "mat_id": mid,
            "predicate_json": pred.to_json(p),
            "pred_sig": pred.signature(p),
            "group_cols": gcols,
            "measures": measures,
            "retained_cols": retained,
            "state_spec": state_spec,
            "extra_avg": sorted(
                k[4:] for k, v in (state_spec.get("extra") or {}).items()
                if v and k.startswith("avg:")
            ) if isinstance(state_spec, dict) else [],
        }
        meta = build.write_materialisation(mdir, base, state, retained_map, schema, name)
        meta["snapshot_id"] = None
        meta["fact_digest"] = None
        return {"mat_id": mid, "name": name, "dir": mdir, "meta": meta}

    def _verify_built(self, tmp, info, leaves, schema):
        """Re-read every written file from disk and recompute from real facts."""
        for mid in info["materialisations"]:
            mdir = os.path.join(tmp, "mat", mid)
            meta = read_json(os.path.join(mdir, "MAT.json"))
            got = userdb.read_materialisation(mdir, meta, schema)
            got_state = {}
            for _gid, g in got["groups"].items():
                st = dict(g["state"])
                st.update(g.get("extras") or {})
                got_state[stable.key_digest(meta["group_cols"], g["key"])] = st
            p = pred.parse(meta["predicate_json"], schema)
            expected, exp_ret = build.aggregate_leaves(
                leaves, p, meta["group_cols"], meta["measures"],
                meta["retained_cols"], state_spec=meta.get("state_spec", "full"),
            )
            if build._canon(got_state) != build._canon(expected):
                raise MaterialisationBuildError(
                    "materialisation %r does not reproduce expected state" % mid
                )
            if got["retained"] != exp_ret:
                raise MaterialisationBuildError(
                    "materialisation %r retained columns do not reproduce" % mid
                )
            if got["physical_rows"] != len(expected):
                raise MaterialisationBuildError(
                    "materialisation %r group count mismatch" % mid
                )
