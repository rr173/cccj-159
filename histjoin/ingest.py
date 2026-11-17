"""Disk-backed work database and streaming ingestion.

Every input row is validated as it is read and inserted straight into a
temporary SQLite database; nothing for a hot key (all 100k of its versions, or
all of its orders) is ever accumulated in a Python list.  Unique indexes on
``row_id`` and ``version_id`` remember the *first* occurrence on disk, so a
duplicate can be reported with both locations.

The work DB uses a small, bounded page cache (``cache_size`` in KiB) and a
temporary store on disk, which is what keeps a 100k-version hot key inside the
256 MiB process budget.
"""

from __future__ import annotations

import os
import sqlite3

from . import validate as v
from .errors import DuplicateIdError, InputValidationError

CUSTOMER = "customer"
STORE = "store"

DDL = """
CREATE TABLE orders (
    rid        INTEGER PRIMARY KEY,
    row_id     TEXT NOT NULL,
    customer   TEXT,
    event_time INTEGER NOT NULL,
    known_time INTEGER NOT NULL,
    amount     INTEGER,
    src_file   TEXT NOT NULL,
    src_line   INTEGER NOT NULL
);
CREATE UNIQUE INDEX ux_orders_row_id ON orders(row_id);
CREATE INDEX ix_orders_customer ON orders(customer);

CREATE TABLE hist (
    hid        INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('customer','store')),
    version_id TEXT NOT NULL,
    key        TEXT NOT NULL,
    valid_from INTEGER NOT NULL,
    valid_to   INTEGER,
    known_from INTEGER NOT NULL,
    known_to   INTEGER,
    priority   INTEGER NOT NULL,
    deleted    INTEGER NOT NULL CHECK (deleted IN (0,1)),
    value      TEXT,
    src_file   TEXT NOT NULL,
    src_line   INTEGER NOT NULL
);
CREATE UNIQUE INDEX ux_hist_version ON hist(version_id);
CREATE INDEX ix_hist_key ON hist(kind, key, valid_from);
CREATE INDEX ix_hist_known ON hist(kind, key, known_from);
"""


def connect_work(path, cache_kib=32768, mmap_kib=0):
    """Open the scratch database with a bounded, disk-spilling footprint."""
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("PRAGMA synchronous=NORMAL")
    # hard upper bound on the page cache; sorts/temp tables spill to disk.
    # Numeric 0 = FILE is used on purpose: some SQLite builds map the
    # symbolic FILE keyword back to the compile-time default (memory).
    con.execute("PRAGMA temp_store=0")
    con.execute("PRAGMA cache_size=%d" % (-int(cache_kib),))
    if mmap_kib:
        con.execute("PRAGMA mmap_size=%d" % (int(mmap_kib) * 1024,))
    con.execute("PRAGMA foreign_keys=ON")
    return con


def create_work_tables(con):
    con.executescript(DDL)


def _report_duplicate(con, table, id_col, id_value, file, line):
    row = con.execute(
        "SELECT src_file, src_line FROM %s WHERE %s=?" % (table, id_col),
        (id_value,),
    ).fetchone()
    first_file, first_line = row if row else (None, None)
    raise DuplicateIdError(
        "duplicate %s %r at %s:%d; first seen at %s:%s"
        % (id_col, id_value, file, line, first_file, first_line),
        file=file, line=line, first_file=first_file, first_line=first_line,
    )


def ingest_orders(con, path, chunk=2000):
    """Stream the orders JSONL into ``orders``; count and return the rows."""
    n = 0
    batch = []
    for line_no, obj in v.iter_jsonl(path, "orders"):
        rec = v.validate_order(obj, path, line_no)
        batch.append(rec + (path, line_no))
        if len(batch) >= chunk:
            _flush_orders(con, batch)
            batch.clear()
        n += 1
    if batch:
        _flush_orders(con, batch)
    con.commit()
    if n == 0:
        raise InputValidationError("orders input %r contains no records" % path, file=path)
    return n


def _flush_orders(con, batch):
    try:
        con.executemany(
            "INSERT INTO orders (row_id, customer, event_time, known_time, amount,"
            " src_file, src_line) VALUES (?,?,?,?,?,?,?)",
            batch,
        )
    except sqlite3.IntegrityError:
        con.rollback()
        for rec in batch:
            try:
                con.execute(
                    "INSERT INTO orders (row_id, customer, event_time, known_time,"
                    " amount, src_file, src_line) VALUES (?,?,?,?,?,?,?)",
                    rec,
                )
            except sqlite3.IntegrityError:
                _report_duplicate(con, "orders", "row_id", rec[0], rec[5], rec[6])
        con.commit()


def ingest_history(con, path, kind, chunk=2000):
    """Stream one history JSONL (customer or store level) into ``hist``.

    An empty file is legal: it simply means the level has no versions, so
    every demand against it resolves to ``gap``.  A missing/unreadable file
    is still a hard input error (raised while opening the iterator).
    """
    n = 0
    batch = []
    for line_no, obj in v.iter_jsonl(path, "%s history" % kind):
        rec = v.validate_history(obj, path, line_no, kind)
        batch.append((kind,) + rec + (path, line_no))
        if len(batch) >= chunk:
            _flush_hist(con, batch)
            batch.clear()
        n += 1
    if batch:
        _flush_hist(con, batch)
    con.commit()
    return n


def _flush_hist(con, batch):
    try:
        con.executemany(
            "INSERT INTO hist (kind, version_id, key, valid_from, valid_to,"
            " known_from, known_to, priority, deleted, value, src_file, src_line)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            batch,
        )
    except sqlite3.IntegrityError:
        con.rollback()
        for rec in batch:
            try:
                con.execute(
                    "INSERT INTO hist (kind, version_id, key, valid_from, valid_to,"
                    " known_from, known_to, priority, deleted, value, src_file, src_line)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    rec,
                )
            except sqlite3.IntegrityError:
                # tuple layout: kind, version_id, ..., src_file(10), src_line(11)
                _report_duplicate(
                    con, "hist", "version_id", rec[1], rec[10], rec[11]
                )
        con.commit()


def unlink_quiet(path):
    try:
        os.remove(path)
    except OSError:
        pass
