"""Error types for the history join.

Every malformed *input* is a whole-run failure: the error carries the source
file name and 1-based line number so the caller can fix the data instead of
silently losing a row or publishing a partial fact database.
"""

from __future__ import annotations


class HistJoinError(Exception):
    """Base error. ``reason`` is a stable machine-readable code."""

    reason = "histjoin_error"

    def __init__(self, message, reason=None, file=None, line=None):
        super().__init__(message)
        if reason is not None:
            self.reason = reason
        self.file = file
        self.line = line

    def as_dict(self):
        return {
            "reason": self.reason,
            "message": str(self),
            "file": self.file,
            "line": self.line,
        }


class InputValidationError(HistJoinError):
    """A JSONL line has the wrong shape or type."""

    reason = "input_validation_error"


class InvalidIntervalError(HistJoinError):
    """A half-open interval is empty or otherwise invalid."""

    reason = "invalid_interval"


class DuplicateIdError(HistJoinError):
    """A row_id / version_id repeats; both locations are reported."""

    reason = "duplicate_id"

    def __init__(self, message, file=None, line=None, first_file=None, first_line=None):
        super().__init__(message, file=file, line=line)
        self.first_file = first_file
        self.first_line = first_line

    def as_dict(self):
        d = super().as_dict()
        d["first_file"] = self.first_file
        d["first_line"] = self.first_line
        return d


class WarehouseError(HistJoinError):
    """A published result directory cannot be read as advertised."""

    reason = "warehouse_error"


class LineageNotFound(WarehouseError):
    reason = "lineage_not_found"
