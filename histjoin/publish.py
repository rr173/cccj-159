"""Atomic publication of a result directory.

A published warehouse is self-describing: a fresh process needs nothing but
the directory path to query facts, quarantine rows, per-row lineage and the
input-count reconciliation.

Layout (all files of one publish come from the *same* input run)::

    <out_dir>/
        WAREHOUSE.json      format marker, input digests/counts, run manifest
        schema.json         fact schema readable by matview.build_snapshot
        facts.sqlite        fact table in the exact matview physical shape
        warehouse.sqlite    quarantine, provenance, reconciliation, row index
        quarantine.jsonl    isolation list, row_id codepoint order
        lineage.jsonl       full per-order lineage, row_id codepoint order

The whole directory is assembled under a sibling ``.warehouse-build-*`` temp
tree and moved into place with one ``os.rename``.  A failure at any point
removes the temp tree, so a previously published complete result stays
queryable and can never be replaced by a half product.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
import uuid

from . import stable
from .factdb import FACT_SCHEMA, write_fact_db

WAREHOUSE_FORMAT = "histjoin-warehouse/1"

WAREHOUSE_DDL = """
CREATE TABLE facts_ref (
    rid        INTEGER PRIMARY KEY,
    row_id     TEXT NOT NULL UNIQUE,
    customer   TEXT NOT NULL,
    store      TEXT NOT NULL,
    region     TEXT NOT NULL,
    event_time INTEGER NOT NULL,
    known_time INTEGER NOT NULL,
    amount     INTEGER
);
CREATE TABLE quarantine (
    rid        INTEGER PRIMARY KEY,
    row_id     TEXT NOT NULL UNIQUE,
    customer   TEXT,
    event_time INTEGER NOT NULL,
    known_time INTEGER NOT NULL,
    amount     INTEGER,
    failed_level TEXT NOT NULL,
    reason     TEXT NOT NULL,
    upstream_versions TEXT NOT NULL,
    top_versions      TEXT NOT NULL
);
CREATE TABLE provenance (
    rid        INTEGER NOT NULL,
    row_id     TEXT NOT NULL,
    level      TEXT NOT NULL,
    key        TEXT,
    status     TEXT NOT NULL,
    value      TEXT,
    version_id TEXT NOT NULL,
    tied       INTEGER NOT NULL,
    ord        INTEGER NOT NULL
);
CREATE INDEX ix_prov_row ON provenance(row_id, level, ord);
CREATE TABLE reconcile (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""


def _fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_json(path, obj):
    tmp = path + ".tmp-" + uuid.uuid4().hex
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, sort_keys=True, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_dir(os.path.dirname(path))


def publish(work_con, out_dir, inputs, counts_in, chunk=2000):
    """Assemble and atomically publish the warehouse.

    ``inputs`` maps role -> {"path", "sha256", "records"} for the three
    JSONL files; ``counts_in`` carries the order/history record totals.
    Returns the manifest dict.
    """
    out_dir = os.path.abspath(out_dir)
    parent = os.path.dirname(out_dir) or "."
    os.makedirs(parent, exist_ok=True)
    tmp = os.path.join(parent, ".warehouse-build-" + uuid.uuid4().hex)
    os.makedirs(tmp)
    try:
        manifest = _build_all(work_con, tmp, inputs, counts_in, chunk)
        _verify(tmp, manifest)
        # swap: back an existing result out of the way, never overwrite in
        # place; the rename of a whole directory is one atomic namespace op
        backup = None
        if os.path.exists(out_dir):
            backup = os.path.join(parent, ".warehouse-old-" + uuid.uuid4().hex)
            os.rename(out_dir, backup)
        try:
            os.rename(tmp, out_dir)
            _fsync_dir(parent)
        except BaseException:
            # restore the previous result if the final rename failed
            if backup is not None and os.path.exists(backup):
                os.rename(backup, out_dir)
            raise
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return manifest


def _build_all(work_con, tmp, inputs, counts_in, chunk):
    n_facts, n_quar = _copy_tables(work_con, tmp, chunk)
    fact_path = os.path.join(tmp, "facts.sqlite")
    n_fact_rows = write_fact_db(work_con, fact_path, chunk=chunk)
    if n_fact_rows != n_facts:
        raise RuntimeError("fact row count mismatch during publish")
    _write_json(os.path.join(tmp, "schema.json"), FACT_SCHEMA)
    _export_jsonl(work_con, tmp, chunk)
    total_orders = counts_in["orders"]
    if n_facts + n_quar != total_orders:
        raise RuntimeError(
            "partition violation: facts (%d) + quarantine (%d) != orders (%d)"
            % (n_facts, n_quar, total_orders)
        )
    manifest = {
        "format": WAREHOUSE_FORMAT,
        "created_at": time.time(),
        "inputs": inputs,
        "counts": {
            "orders": total_orders,
            "customer_history": counts_in["customer_history"],
            "store_history": counts_in["store_history"],
            "facts": n_facts,
            "quarantine": n_quar,
        },
        "schema": FACT_SCHEMA,
    }
    _write_json(os.path.join(tmp, "WAREHOUSE.json"), manifest)
    # the file inventory is recorded only once every artefact, including the
    # manifest itself, exists on disk
    manifest["files"] = sorted(os.listdir(tmp))
    _write_json(os.path.join(tmp, "WAREHOUSE.json"), manifest)
    _fsync_dir(tmp)
    return manifest


def _copy_tables(work_con, tmp, chunk):
    wh = sqlite3.connect(os.path.join(tmp, "warehouse.sqlite"))
    try:
        wh.execute("PRAGMA journal_mode=DELETE")
        wh.execute("PRAGMA synchronous=FULL")
        wh.executescript(WAREHOUSE_DDL)
        n_facts = 0
        cur = work_con.execute(
            "SELECT rid, row_id, customer, store, region, event_time, known_time,"
            " amount FROM facts_stage ORDER BY row_id"
        )
        while True:
            rows = cur.fetchmany(chunk)
            if not rows:
                break
            wh.executemany(
                "INSERT INTO facts_ref (rid, row_id, customer, store, region,"
                " event_time, known_time, amount) VALUES (?,?,?,?,?,?,?,?)",
                rows,
            )
            n_facts += len(rows)
        n_quar = 0
        cur = work_con.execute(
            "SELECT rid, row_id, customer, event_time, known_time, amount,"
            " failed_level, reason, upstream_versions, top_versions"
            " FROM quarantine ORDER BY row_id"
        )
        while True:
            rows = cur.fetchmany(chunk)
            if not rows:
                break
            wh.executemany(
                "INSERT INTO quarantine (rid, row_id, customer, event_time,"
                " known_time, amount, failed_level, reason, upstream_versions,"
                " top_versions) VALUES (?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            n_quar += len(rows)
        cur = work_con.execute(
            "SELECT p.rid, o.row_id, p.level, p.key, p.status, p.value,"
            " p.version_id, p.tied, p.ord FROM provenance p"
            " JOIN orders o ON o.rid=p.rid"
            " ORDER BY o.row_id, p.level, p.ord"
        )
        while True:
            rows = cur.fetchmany(chunk)
            if not rows:
                break
            wh.executemany(
                "INSERT INTO provenance (rid, row_id, level, key, status, value,"
                " version_id, tied, ord) VALUES (?,?,?,?,?,?,?,?,?)",
                rows,
            )
        n_orders = work_con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        n_customer = work_con.execute(
            "SELECT COUNT(*) FROM hist WHERE kind='customer'"
        ).fetchone()[0]
        n_store = work_con.execute(
            "SELECT COUNT(*) FROM hist WHERE kind='store'"
        ).fetchone()[0]
        wh.executemany(
            "INSERT INTO reconcile (name, value) VALUES (?,?)",
            [
                ("orders", n_orders),
                ("customer_history", n_customer),
                ("store_history", n_store),
                ("facts", n_facts),
                ("quarantine", n_quar),
            ],
        )
        wh.commit()
    finally:
        wh.close()
    return n_facts, n_quar


def _export_jsonl(work_con, tmp, chunk):
    # Quarantine and lineage as readable JSONL, both in row_id codepoint
    # order (SQLite BINARY collation on TEXT == Unicode code point order).
    qpath = os.path.join(tmp, "quarantine.jsonl")
    with open(qpath, "w", encoding="utf-8") as f:
        cur = work_con.execute(
            "SELECT q.row_id, q.customer, q.event_time, q.known_time, q.amount,"
            " q.failed_level, q.reason, q.upstream_versions, q.top_versions"
            " FROM quarantine q ORDER BY q.row_id"
        )
        while True:
            rows = cur.fetchmany(chunk)
            if not rows:
                break
            for r in rows:
                obj = {
                    "row_id": r[0],
                    "customer": r[1],
                    "event_time": r[2],
                    "known_time": r[3],
                    "amount": r[4],
                    "failed_level": r[5],
                    "reason": r[6],
                    "upstream_versions": json.loads(r[7]),
                    "top_versions": json.loads(r[8]),
                }
                f.write(json.dumps(obj, sort_keys=True, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    lpath = os.path.join(tmp, "lineage.jsonl")
    with open(lpath, "w", encoding="utf-8") as f:
        cur = work_con.execute(
            "SELECT o.row_id, p.level, p.key, p.status, p.value, p.version_id,"
            " p.tied, p.ord FROM provenance p JOIN orders o ON o.rid=p.rid"
            " ORDER BY o.row_id, p.level, p.ord"
        )
        current = None
        record = None
        while True:
            rows = cur.fetchmany(chunk)
            if not rows:
                break
            for r in rows:
                row_id, level, key, status, value, vid, tied, ord_no = r
                if row_id != current:
                    if record is not None:
                        f.write(json.dumps(record, sort_keys=True,
                                          ensure_ascii=False) + "\n")
                    current = row_id
                    record = {"row_id": row_id, "levels": {}}
                bucket = record["levels"].setdefault(level, [])
                bucket.append({
                    "key": key,
                    "status": status,
                    "value": value,
                    "version_id": None if vid == "-" else vid,
                    "tied": bool(tied),
                    "ord": ord_no,
                })
        if record is not None:
            f.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _verify(tmp, manifest):
    """Re-open every durable artefact from disk and check internal totals."""
    wh = sqlite3.connect(os.path.join(tmp, "warehouse.sqlite"))
    try:
        rec = dict(wh.execute("SELECT name, value FROM reconcile").fetchall())
        if rec["facts"] != wh.execute(
            "SELECT COUNT(*) FROM facts_ref"
        ).fetchone()[0]:
            raise RuntimeError("warehouse facts_ref count disagrees with reconcile")
        if rec["quarantine"] != wh.execute(
            "SELECT COUNT(*) FROM quarantine"
        ).fetchone()[0]:
            raise RuntimeError("warehouse quarantine count disagrees with reconcile")
        if rec["facts"] + rec["quarantine"] != rec["orders"]:
            raise RuntimeError("warehouse partition does not cover every order")
        # every order has lineage, and no lineage exists without an order
        n_lin_rows = wh.execute(
            "SELECT COUNT(DISTINCT row_id) FROM provenance"
        ).fetchone()[0]
        if n_lin_rows != rec["orders"]:
            raise RuntimeError("lineage covers %d of %d orders" % (n_lin_rows, rec["orders"]))
    finally:
        wh.close()
    for name in manifest["files"]:
        p = os.path.join(tmp, name)
        if not os.path.isfile(p):
            raise RuntimeError("declared file missing in build tree: %s" % name)
    # reading the fact file back end to end also proves it is a valid SQLite db
    stable.sha256_file(os.path.join(tmp, "facts.sqlite"))
