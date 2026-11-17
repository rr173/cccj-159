"""End-to-end build orchestration.

``build_warehouse`` is the single product entry point: validate and ingest the
three unordered JSONL inputs into a disk-backed scratch database, run the two
exact adjudication sweeps, then atomically publish a self-describing result
directory.  Any input error aborts the whole run before publication, so a
previously published complete result is never replaced.

The scratch database and all temp tables live on disk and use a bounded page
cache; the only Python accumulations are fixed-size batches.  Changing the
input file order or the chunk size changes neither business results nor
provenance, because every ordering decision is an explicit SQL ``ORDER BY`` on
stable columns (row_id / version_id text in codepoint order).
"""

from __future__ import annotations

import os
import tempfile

from . import engine, ingest, publish
from .stable import sha256_file

DEFAULT_CHUNK = 2000
DEFAULT_CACHE_KIB = 32768  # 32 MiB SQLite page-cache ceiling


def build_warehouse(
    orders_path,
    customer_hist_path,
    store_hist_path,
    out_dir,
    chunk=DEFAULT_CHUNK,
    cache_kib=DEFAULT_CACHE_KIB,
    work_db_path=None,
    keep_work_db=False,
):
    """Build the warehouse; return the published manifest dict.

    Parameters
    ----------
    chunk:
        Streaming batch size.  Purely a resource knob: output is identical for
        any positive value.
    cache_kib:
        Upper bound (KiB) on the SQLite page cache in the scratch database.
    work_db_path / keep_work_db:
        Optionally pin/keep the scratch database (used by the resource tests).
    """
    for role, path in (
        ("orders", orders_path),
        ("customer history", customer_hist_path),
        ("store history", store_hist_path),
    ):
        if not os.path.isfile(path):
            from .errors import InputValidationError

            raise InputValidationError(
                "cannot open %s input %r: not a readable file" % (role, path),
                file=path,
            )
    inputs = _digests(orders_path, customer_hist_path, store_hist_path)
    own_work = work_db_path is None
    if own_work:
        fd, work_db_path = tempfile.mkstemp(prefix="histjoin-work-", suffix=".sqlite")
        os.close(fd)
        ingest.unlink_quiet(work_db_path)
    con = ingest.connect_work(work_db_path, cache_kib=cache_kib)
    try:
        ingest.create_work_tables(con)
        n_orders = ingest.ingest_orders(con, orders_path, chunk=chunk)
        n_customer = ingest.ingest_history(
            con, customer_hist_path, ingest.CUSTOMER, chunk=chunk
        )
        n_store = ingest.ingest_history(
            con, store_hist_path, ingest.STORE, chunk=chunk
        )
        # ANALYZE so the sweep access paths are chosen on the real data volume
        con.execute("ANALYZE")
        sweeper = engine.SweepEngine(con, chunk=chunk)
        sweeper.prepare_outputs()
        sweeper.resolve_level1()
        sweeper.build_level2_demands()
        sweeper.resolve_level2()
        n_facts, n_quar = sweeper.finalise()
        counts_in = {
            "orders": n_orders,
            "customer_history": n_customer,
            "store_history": n_store,
        }
        manifest = publish.publish(con, out_dir, inputs, counts_in, chunk=chunk)
        manifest["counts"]["facts"] = n_facts
        manifest["counts"]["quarantine"] = n_quar
        if own_work and not keep_work_db:
            con.close()
            ingest.unlink_quiet(work_db_path)
            for suffix in ("-wal", "-shm"):
                ingest.unlink_quiet(work_db_path + suffix)
        return manifest
    finally:
        con.close()


def _digests(orders_path, customer_hist_path, store_hist_path):
    return {
        "orders": {
            "path": os.path.abspath(orders_path),
            "sha256": sha256_file(orders_path),
        },
        "customer_history": {
            "path": os.path.abspath(customer_hist_path),
            "sha256": sha256_file(customer_hist_path),
        },
        "store_history": {
            "path": os.path.abspath(store_hist_path),
            "sha256": sha256_file(store_hist_path),
        },
    }
