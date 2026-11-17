"""Strict, type-exact validation of the three JSONL record shapes.

The rules come straight from the protocol:

* times and ``amount``/``priority`` are exact int64 integers -- a JSON float,
  a bool or a bigger integer is a whole-input error;
* every interval is half-open ``[from, to)`` and must be non-empty; ``to`` of
  null means positive infinity.  Bounds are compared as Python integers, so
  the int64 end points are exact and no float index or ``+1`` trick is used;
* ``deleted=true`` records carry no business value; ``deleted=false`` with
  ``value=null`` is a perfectly valid *empty attribute*, not a deletion.

Only record-level checks live here.  Cross-record checks (duplicate ids,
global version uniqueness) are handled while streaming into the work
database, where the unique indexes remember the first occurrence on disk.
"""

from __future__ import annotations

import json

from .errors import InputValidationError, InvalidIntervalError

MIN_I64 = -(2 ** 63)
MAX_I64 = 2 ** 63 - 1

ORDER_FIELDS = ("row_id", "customer", "event_time", "known_time", "amount")
HISTORY_FIELDS = (
    "version_id", "key", "valid_from", "valid_to",
    "known_from", "known_to", "priority", "deleted", "value",
)


def is_exact_i64(v):
    """True only for a Python ``int`` inside the int64 range (bool excluded)."""
    return isinstance(v, int) and not isinstance(v, bool) and MIN_I64 <= v <= MAX_I64


def is_nonempty_text(v):
    return isinstance(v, str) and v != ""


def check_interval(vfrom, vto, file, line, what):
    """Validate a ``[from, to)`` interval without mutating the end points."""
    if not is_exact_i64(vfrom):
        raise InputValidationError(
            "%s valid_from/known_from must be an int64 integer, got %r" % (what, vfrom),
            file=file, line=line,
        )
    if vto is not None and not is_exact_i64(vto):
        raise InputValidationError(
            "%s *_to must be an int64 integer or null, got %r" % (what, vto),
            file=file, line=line,
        )
    if vto is not None and not vfrom < vto:
        raise InvalidIntervalError(
            "%s interval [%r, %r) is empty (half-open intervals need from < to)"
            % (what, vfrom, vto),
            file=file, line=line,
        )


def validate_order(obj, file, line):
    if not isinstance(obj, dict):
        raise InputValidationError("order record must be a JSON object", file=file, line=line)
    extra = sorted(set(obj) - set(ORDER_FIELDS))
    if extra:
        raise InputValidationError(
            "order record has unknown fields %r" % (extra,), file=file, line=line
        )
    missing = [f for f in ORDER_FIELDS if f not in obj]
    if missing:
        raise InputValidationError(
            "order record is missing fields %r" % (missing,), file=file, line=line
        )
    row_id = obj["row_id"]
    if not is_nonempty_text(row_id):
        raise InputValidationError(
            "order row_id must be a non-empty string, got %r" % (row_id,),
            file=file, line=line,
        )
    customer = obj["customer"]
    if customer is not None and not is_nonempty_text(customer):
        raise InputValidationError(
            "order customer must be null or a non-empty string, got %r" % (customer,),
            file=file, line=line,
        )
    for f in ("event_time", "known_time"):
        if not is_exact_i64(obj[f]):
            raise InputValidationError(
                "order %s must be an int64 integer, got %r" % (f, obj[f]),
                file=file, line=line,
            )
    amount = obj["amount"]
    if amount is not None and not is_exact_i64(amount):
        raise InputValidationError(
            "order amount must be an int64 integer or null, got %r" % (amount,),
            file=file, line=line,
        )
    return (
        row_id, customer, obj["event_time"], obj["known_time"], amount,
    )


def validate_history(obj, file, line, level):
    what = "%s history" % level
    if not isinstance(obj, dict):
        raise InputValidationError("%s record must be a JSON object" % what, file=file, line=line)
    extra = sorted(set(obj) - set(HISTORY_FIELDS))
    if extra:
        raise InputValidationError(
            "%s record has unknown fields %r" % (what, extra), file=file, line=line
        )
    missing = [f for f in HISTORY_FIELDS if f not in obj]
    if missing:
        raise InputValidationError(
            "%s record is missing fields %r" % (what, missing), file=file, line=line
        )
    version_id = obj["version_id"]
    key = obj["key"]
    if not is_nonempty_text(version_id):
        raise InputValidationError(
            "%s version_id must be a non-empty string, got %r" % (what, version_id),
            file=file, line=line,
        )
    if not is_nonempty_text(key):
        raise InputValidationError(
            "%s key must be a non-empty string, got %r" % (what, key),
            file=file, line=line,
        )
    check_interval(obj["valid_from"], obj["valid_to"], file, line, what + " business")
    check_interval(obj["known_from"], obj["known_to"], file, line, what + " knowledge")
    priority = obj["priority"]
    if not is_exact_i64(priority):
        raise InputValidationError(
            "%s priority must be an int64 integer, got %r" % (what, priority),
            file=file, line=line,
        )
    deleted = obj["deleted"]
    if not isinstance(deleted, bool):
        raise InputValidationError(
            "%s deleted must be a JSON boolean, got %r" % (what, deleted),
            file=file, line=line,
        )
    value = obj["value"]
    if value is not None and not isinstance(value, str):
        raise InputValidationError(
            "%s value must be a string or null, got %r" % (what, value),
            file=file, line=line,
        )
    if deleted and value is not None:
        raise InputValidationError(
            "%s deleted record %r must not carry a business value (got %r); "
            "an empty attribute is deleted=false with value=null"
            % (what, version_id, value),
            file=file, line=line,
        )
    return (
        version_id, key,
        obj["valid_from"], obj["valid_to"],
        obj["known_from"], obj["known_to"],
        priority, 1 if deleted else 0, value,
    )


def iter_jsonl(path, role):
    """Yield ``(line_number, parsed_object)`` with no silent line dropping.

    A malformed JSON line, an empty/whitespace-only line or a trailing blob
    all raise :class:`InputValidationError` carrying file and line number.
    """
    try:
        f = open(path, "r", encoding="utf-8")
    except OSError as exc:
        raise InputValidationError(
            "cannot open %s input %r: %s" % (role, path, exc), file=path
        )
    with f:
        line_no = 0
        while True:
            line = f.readline()
            if line == "":
                return
            line_no += 1
            if not line.strip():
                raise InputValidationError(
                    "empty/blank lines are not valid JSONL", file=path, line=line_no
                )
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise InputValidationError(
                    "invalid JSON on %s line %d: %s" % (path, line_no, exc.msg),
                    file=path, line=line_no,
                )
            yield line_no, obj
