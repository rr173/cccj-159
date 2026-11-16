"""Materialisation reuse layer.

Public entry points live in :mod:`matview.api`:

    build_snapshot / register / plan_query / execute / explain / run

Everything is driven by structured JSON; no SQL text from the caller is ever
parsed.  See README.md for the protocol.
"""

from .errors import (  # noqa: F401
    CatalogueError,
    MaterialisationBuildError,
    MaterialisationUnusable,
    MatviewError,
    QueryError,
    SchemaMismatchError,
    SnapshotMismatchError,
)

__all__ = [
    "CatalogueError",
    "MaterialisationBuildError",
    "MaterialisationUnusable",
    "MatviewError",
    "QueryError",
    "SchemaMismatchError",
    "SnapshotMismatchError",
]
