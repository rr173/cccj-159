"""Errors with machine-readable reasons."""

from __future__ import annotations


class MatviewError(Exception):
    """Base error.  `reason` is a stable machine-readable code."""

    reason = "error"

    def __init__(self, message: str, reason: str = None):
        super().__init__(message)
        if reason is not None:
            self.reason = reason

    def as_dict(self) -> dict:
        return {"reason": self.reason, "message": str(self)}


class CatalogueError(MatviewError):
    reason = "catalogue_error"


class SchemaMismatchError(MatviewError):
    reason = "schema_mismatch"


class SnapshotMismatchError(MatviewError):
    reason = "snapshot_mismatch"


class MaterialisationBuildError(MatviewError):
    reason = "materialisation_build_failed"


class MaterialisationUnusable(MatviewError):
    def __init__(self, message: str, reason: str):
        super().__init__(message, reason)


class QueryError(MatviewError):
    reason = "query_error"


class NoPlanError(MatviewError):
    reason = "no_plan"
