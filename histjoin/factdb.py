"""Streaming fact-table writer.

The produced ``facts.sqlite`` has the exact physical shape that
``matview.catalog.create_fact_db`` writes (a ``facts`` table, a unique index
on ``row_id``, one index per business field), so the existing
``build_snapshot`` entry point consumes it without any change.  Unlike the
in-memory helper in the matview package, rows are streamed from the work
database in fixed chunks, so the full fact set is never held in Python.
"""

from __future__ import annotations

import os
import sqlite3

FACT_SCHEMA = {
    "row_id": "row_id",
    "fields": {
        "customer": "text",
        "store": "text",
        "region": "text",
        "event_time": "int",
        "known_time": "int",
        "amount": "int",
    },
}

_FACT_COLS = (
    "row_id", "customer", "store", "region",
    "event_time", "known_time", "amount",
)


def write_fact_db(work_con, dst_path, chunk=2000):
    """Stream ``facts_stage`` (ordered by row_id) into a fresh fact database."""
    if os.path.exists(dst_path):
        raise RuntimeError("fact destination already exists: %s" % dst_path)
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    con = sqlite3.connect(dst_path)
    try:
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("PRAGMA synchronous=FULL")
        con.execute(
            "CREATE TABLE facts ("
            "row_id TEXT, customer TEXT, store TEXT, region TEXT,"
            " event_time INTEGER, known_time INTEGER, amount INTEGER)"
        )
        con.execute("CREATE UNIQUE INDEX ux_facts_row_id ON facts (row_id)")
        # deterministic index names; indexes never influence fact values,
        # only the access paths the query layer may choose
        for col in ("customer", "store", "region", "event_time", "known_time", "amount"):
            con.execute(
                "CREATE INDEX ix_facts_%s ON facts (%s)" % (col, col)
            )
        cur = work_con.execute(
            "SELECT row_id, customer, store, region, event_time, known_time, amount"
            " FROM facts_stage ORDER BY row_id"
        )
        total = 0
        while True:
            rows = cur.fetchmany(chunk)
            if not rows:
                break
            con.executemany(
                "INSERT INTO facts (row_id, customer, store, region, event_time,"
                " known_time, amount) VALUES (?,?,?,?,?,?,?)",
                rows,
            )
            total += len(rows)
        con.commit()
    finally:
        con.close()
    _fsync_dir(os.path.dirname(dst_path))
    return total


def read_fact_count(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    finally:
        con.close()


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
