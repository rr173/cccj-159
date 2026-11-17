"""Read-only queries over a published warehouse.

A new process needs only the result directory: every lookup opens
``warehouse.sqlite`` through a read-only ``file:`` URI and never touches the
scratch database or the original JSONL inputs.
"""

from __future__ import annotations

import json
import os
import sqlite3
from urllib.parse import quote

from .errors import LineageNotFound, WarehouseError

WAREHOUSE_DB = "warehouse.sqlite"
MARKER = "WAREHOUSE.json"
FACT_DB = "facts.sqlite"
SCHEMA = "schema.json"


def _connect(out_dir):
    path = os.path.join(os.path.abspath(out_dir), WAREHOUSE_DB)
    if not os.path.exists(os.path.join(out_dir, MARKER)):
        raise WarehouseError(
            "not a histjoin warehouse (missing %s): %s" % (MARKER, out_dir)
        )
    if not os.path.exists(path):
        raise WarehouseError("warehouse database missing: %s" % path)
    return sqlite3.connect("file:%s?mode=ro" % quote(os.path.abspath(path)), uri=True)


def lineage_for(out_dir, row_id):
    """Full provenance trail for one order, keyed by row_id.

    Returns a dict with the order outcome and, per level, the exact
    adjudication plus every top-tier version (sorted).  ``missing_key``
    orders report an empty customer candidate list.
    """
    con = _connect(out_dir)
    try:
        fact = con.execute(
            "SELECT row_id, customer, store, region, event_time, known_time, amount"
            " FROM facts_ref WHERE row_id=?",
            (row_id,),
        ).fetchone()
        quar = con.execute(
            "SELECT row_id, customer, event_time, known_time, amount, failed_level,"
            " reason, upstream_versions, top_versions"
            " FROM quarantine WHERE row_id=?",
            (row_id,),
        ).fetchone()
        if fact is None and quar is None:
            raise LineageNotFound("no order with row_id %r" % (row_id,))
        levels = {}
        for r in con.execute(
            "SELECT level, key, status, value, version_id, tied, ord"
            " FROM provenance WHERE row_id=? ORDER BY level, ord",
            (row_id,),
        ):
            level, key, status, value, vid, tied, ord_no = r
            levels.setdefault(level, []).append({
                "key": key,
                "status": status,
                "value": value,
                "version_id": None if vid == "-" else vid,
                "tied": bool(tied),
                "ord": ord_no,
            })
        if fact is not None:
            return {
                "row_id": row_id,
                "outcome": "fact",
                "fact": {
                    "row_id": fact[0], "customer": fact[1], "store": fact[2],
                    "region": fact[3], "event_time": fact[4],
                    "known_time": fact[5], "amount": fact[6],
                },
                "levels": levels,
            }
        return {
            "row_id": row_id,
            "outcome": "quarantine",
            "quarantine": {
                "row_id": quar[0], "customer": quar[1], "event_time": quar[2],
                "known_time": quar[3], "amount": quar[4],
                "failed_level": quar[5], "reason": quar[6],
                "upstream_versions": json.loads(quar[7]),
                "top_versions": json.loads(quar[8]),
            },
            "levels": levels,
        }
    finally:
        con.close()


def list_quarantine(out_dir):
    """All isolated orders in row_id codepoint order."""
    con = _connect(out_dir)
    try:
        out = []
        for r in con.execute(
            "SELECT row_id, customer, event_time, known_time, amount, failed_level,"
            " reason, upstream_versions, top_versions"
            " FROM quarantine ORDER BY row_id"
        ):
            out.append({
                "row_id": r[0], "customer": r[1], "event_time": r[2],
                "known_time": r[3], "amount": r[4], "failed_level": r[5],
                "reason": r[6],
                "upstream_versions": json.loads(r[7]),
                "top_versions": json.loads(r[8]),
            })
        return out
    finally:
        con.close()


def list_facts(out_dir):
    """All warehoused facts in row_id codepoint order."""
    con = _connect(out_dir)
    try:
        cols = ("row_id", "customer", "store", "region",
                "event_time", "known_time", "amount")
        return [
            dict(zip(cols, r))
            for r in con.execute(
                "SELECT row_id, customer, store, region, event_time, known_time,"
                " amount FROM facts_ref ORDER BY row_id"
            )
        ]
    finally:
        con.close()


def counts(out_dir):
    """Input/output reconciliation numbers stored at publish time."""
    con = _connect(out_dir)
    try:
        rec = dict(con.execute("SELECT name, value FROM reconcile").fetchall())
    finally:
        con.close()
    with open(os.path.join(out_dir, MARKER), encoding="utf-8") as f:
        marker = json.load(f)
    return {
        "reconcile": rec,
        "inputs": marker.get("inputs", {}),
        "counts": marker.get("counts", {}),
        "partition_complete": rec.get("facts", 0) + rec.get("quarantine", 0)
        == rec.get("orders", -1),
    }
