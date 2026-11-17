"""Offline two-level bi-temporal history join.

Turns three unordered JSONL inputs (orders, customer->store history,
store->region history) into the fixed fact table consumed by the existing
``matview`` query layer, plus a per-row lineage trail and an isolation list
for every order that could not be warehoused.

Public entry points (see :mod:`histjoin.builder` and :mod:`histjoin.query`):

    build_warehouse(orders_path, customer_hist_path, store_hist_path,
                    out_dir, ...)                 -> build manifest dict
    lineage_for(out_dir, row_id)                  -> full provenance of one row
    list_quarantine(out_dir)                      -> every isolated order
    counts(out_dir)                               -> input/output reconciliation

The CLI mirrors the API::

    python -m histjoin.cli build ORDERS CUST_HIST STORE_HIST OUT_DIR
    python -m histjoin.cli lineage OUT_DIR ROW_ID
    python -m histjoin.cli quarantine OUT_DIR
    python -m histjoin.cli counts OUT_DIR

Nothing here changes the materialisation selection algorithm: the output fact
database has the exact physical shape produced by ``matview.api.create_fact_db``
and is consumed through the existing ``build_snapshot`` entry point.
"""

from .builder import build_warehouse  # noqa: F401
from .errors import (  # noqa: F401
    DuplicateIdError,
    HistJoinError,
    InputValidationError,
    InvalidIntervalError,
    LineageNotFound,
)
from .query import counts, lineage_for, list_facts, list_quarantine  # noqa: F401

__all__ = [
    "build_warehouse",
    "lineage_for",
    "list_quarantine",
    "list_facts",
    "counts",
    "HistJoinError",
    "InputValidationError",
    "InvalidIntervalError",
    "DuplicateIdError",
    "LineageNotFound",
]
