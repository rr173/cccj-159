"""Canonical predicate representation: parsing, validation, three-valued logic.

The public grammar is structured JSON; no SQL text is ever parsed.

    {"op": "eq",      "col": <field>, "val": <int|str>}
    {"op": "range",   "col": <field>, "lo": <int|null>, "hi": <int|null>}
    {"op": "is_null", "col": <field>}
    {"op": "and",     "args": [<pred>, ...]}
    {"op": "or",      "args": [<pred>, ...]}
    {"op": "not",     "arg": <pred>}

`range` is a closed integer interval (null bound = open end).  Fields are
declared `int`, `text` or `null`; `eq` must match the declared type, and
`eq` with a null value is rejected in favour of `is_null`.

Internal canonical form (tuples, hashable, order-stable):

    ("eq", col, value)          value is int or str, never None
    ("range", col, lo, hi)      ints, lo <= hi, sentinels for open ends
    ("null", col, True|False)   IS NULL / IS NOT NULL
    ("and", (p, ...)) ("or", (p, ...)) ("not", p)
    ("is_true", p)              total: TRUE iff p evaluates to TRUE
    ("true",) ("false",)

`is_true` is the only way to express "p is admitted" without the NULL of
three-valued logic leaking into a complement.  `not(p)` is SQL NOT: it maps a
NULL of p to NULL, so `q AND NOT(p)` silently drops rows for which p is NULL
— exactly the bug this atom exists to avoid.

Three-valued evaluation: a predicate yields True / False / None.  A row enters
a result only when the predicate yields True; False and None are both filtered.
"""

from __future__ import annotations

import hashlib
import json

MIN_I64 = -(2 ** 63)
MAX_I64 = 2 ** 63 - 1


class PredicateError(ValueError):
    """Malformed or type-inconsistent predicate."""


class SchemaError(ValueError):
    """Unknown field or bad declared type."""


TRUE = ("true",)
FALSE = ("false",)


def json_dumps(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------

FIELD_TYPES = ("int", "text", "null")


class Schema:
    """The fixed fact-table schema of a snapshot."""

    def __init__(self, row_id: str, fields: dict):
        if not isinstance(row_id, str) or not row_id:
            raise SchemaError("row_id must be a non-empty string")
        if not isinstance(fields, dict) or not fields:
            raise SchemaError("fact table needs at least one business field")
        if row_id in fields:
            raise SchemaError("row_id must not also be a business field")
        for name, ftype in fields.items():
            if not isinstance(name, str) or not name:
                raise SchemaError("field names must be non-empty strings")
            if ftype not in FIELD_TYPES:
                raise SchemaError("field %r has unsupported type %r" % (name, ftype))
        self.row_id = row_id
        self.fields = dict(fields)
        self.field_digest = "fld:" + hashlib.sha256(
            json_dumps(sorted(self.fields)).encode("utf-8")
        ).hexdigest()[:32]

    def require(self, col) -> str:
        if not isinstance(col, str) or col not in self.fields:
            raise SchemaError(
                "unknown field %r (known: %s)" % (col, sorted(self.fields))
            )
        return col

    def is_measure(self, col) -> bool:
        return self.fields.get(col) == "int"

    def as_json(self) -> dict:
        return {"row_id": self.row_id, "fields": self.fields}

    @classmethod
    def from_json(cls, obj) -> "Schema":
        if not isinstance(obj, dict):
            raise SchemaError("schema must be an object")
        return cls(obj.get("row_id"), obj.get("fields"))


# ---------------------------------------------------------------------------
# canonical constructors
# ---------------------------------------------------------------------------

def eq(col, value, schema: Schema):
    schema.require(col)
    ftype = schema.fields[col]
    if value is None:
        if ftype != "null":
            raise PredicateError(
                "eq(null) on %r is not allowed (field is %s); use is_null" % (col, ftype)
            )
        return ("null", col, True)
    if ftype == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise PredicateError("eq on int field %r needs an int, got %r" % (col, value))
    elif ftype == "text":
        if not isinstance(value, str):
            raise PredicateError("eq on text field %r needs a str, got %r" % (col, value))
    else:
        raise PredicateError("eq on always-null field %r is impossible" % (col,))
    return ("eq", col, value)


def is_null(col, schema: Schema, positive: bool = True):
    schema.require(col)
    return ("null", col, bool(positive))


def rng(col, lo, hi, schema: Schema):
    schema.require(col)
    if schema.fields[col] != "int":
        raise PredicateError("range on non-int field %r" % (col,))
    if lo is None and hi is None:
        return ("null", col, False)
    if lo is not None and (isinstance(lo, bool) or not isinstance(lo, int)):
        raise PredicateError("range lo must be int or null")
    if hi is not None and (isinstance(hi, bool) or not isinstance(hi, int)):
        raise PredicateError("range hi must be int or null")
    lo = MIN_I64 if lo is None else lo
    hi = MAX_I64 if hi is None else hi
    if lo > hi:
        return FALSE
    return ("range", col, lo, hi)


def band(*preds):
    args = []
    for p in preds:
        if p == TRUE:
            continue
        if p == FALSE:
            return FALSE
        if p[0] == "and":
            args.extend(p[1])
        else:
            args.append(p)
    if not args:
        return TRUE
    if len(args) == 1:
        return args[0]
    return ("and", tuple(args))


def bor(*preds):
    args = []
    for p in preds:
        if p == FALSE:
            continue
        if p == TRUE:
            return TRUE
        if p[0] == "or":
            args.extend(p[1])
        else:
            args.append(p)
    if not args:
        return FALSE
    if len(args) == 1:
        return args[0]
    return ("or", tuple(args))


def neg(p):
    if p[0] == "true":
        return FALSE
    if p[0] == "false":
        return TRUE
    if p[0] == "not":
        return p[1]
    return ("not", p)


def not_true(p):
    """Total negation: TRUE iff p is *not* TRUE (FALSE and NULL both count)."""
    if p[0] == "true":
        return FALSE
    if p[0] == "false":
        return TRUE
    if p[0] == "not_true":
        return is_true(p[1])
    return ("not_true", p)


def is_true(p):
    if p[0] == "true":
        return TRUE
    if p[0] == "false":
        return FALSE
    return ("is_true", p)


def cols_of(p) -> frozenset:
    out = set()
    _walk_cols(p, out)
    return frozenset(out)


def _walk_cols(p, out):
    tag = p[0]
    if tag in ("eq", "range", "null"):
        out.add(p[1])
    elif tag in ("and", "or"):
        for a in p[1]:
            _walk_cols(a, out)
    elif tag in ("not", "is_true", "not_true"):
        _walk_cols(p[1], out)


def atoms_of(p) -> list:
    """All atomic predicates (in order) inside a canonical predicate."""
    tag = p[0]
    if tag in ("eq", "range", "null"):
        return [p]
    if tag in ("and", "or"):
        out = []
        for a in p[1]:
            out.extend(atoms_of(a))
        return out
    if tag in ("not", "is_true", "not_true"):
        return atoms_of(p[1])
    return []


# ---------------------------------------------------------------------------
# parse / serialise
# ---------------------------------------------------------------------------

def parse(obj, schema: Schema):
    if not isinstance(obj, dict):
        raise PredicateError("predicate must be an object, got %r" % (type(obj),))
    op = obj.get("op")
    if op == "eq":
        if "col" not in obj or "val" not in obj:
            raise PredicateError("eq needs col and val")
        return eq(obj["col"], obj["val"], schema)
    if op == "range":
        if "col" not in obj:
            raise PredicateError("range needs col")
        return rng(obj["col"], obj.get("lo"), obj.get("hi"), schema)
    if op == "is_null":
        if "col" not in obj:
            raise PredicateError("is_null needs col")
        return is_null(obj["col"], schema)
    if op == "and":
        args = obj.get("args")
        if not isinstance(args, list):
            raise PredicateError("and needs an args list")
        return band(*(parse(a, schema) for a in args))
    if op == "or":
        args = obj.get("args")
        if not isinstance(args, list):
            raise PredicateError("or needs an args list")
        return bor(*(parse(a, schema) for a in args))
    if op == "not":
        if "arg" not in obj:
            raise PredicateError("not needs arg")
        return neg(parse(obj["arg"], schema))
    if op == "is_true":  # internal: used for exact complements, round-trip only
        if "arg" not in obj:
            raise PredicateError("is_true needs arg")
        return is_true(parse(obj["arg"], schema))
    raise PredicateError("unsupported predicate op %r" % (op,))


def to_json(p):
    tag = p[0]
    if tag == "true":
        return {"op": "and", "args": []}
    if tag == "false":
        return {"op": "or", "args": []}
    if tag == "eq":
        return {"op": "eq", "col": p[1], "val": p[2]}
    if tag == "range":
        return {
            "op": "range",
            "col": p[1],
            "lo": None if p[2] == MIN_I64 else p[2],
            "hi": None if p[3] == MAX_I64 else p[3],
        }
    if tag == "null":
        if p[2]:
            return {"op": "is_null", "col": p[1]}
        return {"op": "not", "arg": {"op": "is_null", "col": p[1]}}
    if tag in ("and", "or"):
        return {"op": tag, "args": [to_json(a) for a in p[1]]}
    if tag == "not":
        return {"op": "not", "arg": to_json(p[1])}
    if tag == "is_true":
        return {"op": "is_true", "arg": to_json(p[1])}
    if tag == "not_true":
        return {"op": "not_true", "arg": to_json(p[1])}
    raise PredicateError("bad canonical predicate %r" % (p,))


def signature(p) -> str:
    return "pred:" + hashlib.sha256(json_dumps(p).encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# three-valued evaluation
# ---------------------------------------------------------------------------

_NULL = object()


def eval3(p, row: dict):
    """SQL three-valued evaluation on a fact row (dict)."""
    tag = p[0]
    if tag == "true":
        return True
    if tag == "false":
        return False
    if tag == "eq":
        v = row.get(p[1], _NULL)
        v = None if v is _NULL else v
        if v is None:
            return None
        return v == p[2]
    if tag == "range":
        v = row.get(p[1], _NULL)
        v = None if v is _NULL else v
        if v is None:
            return None
        return p[2] <= v <= p[3]
    if tag == "null":
        v = row.get(p[1], _NULL)
        v = None if v is _NULL else v
        return (v is None) if p[2] else (v is not None)
    if tag == "and":
        saw_null = False
        for a in p[1]:
            r = eval3(a, row)
            if r is False:
                return False
            if r is None:
                saw_null = True
        return None if saw_null else True
    if tag == "or":
        saw_null = False
        for a in p[1]:
            r = eval3(a, row)
            if r is True:
                return True
            if r is None:
                saw_null = True
        return None if saw_null else False
    if tag == "not":
        r = eval3(p[1], row)
        return None if r is None else (not r)
    if tag == "is_true":
        return eval3(p[1], row) is True
    if tag == "not_true":
        return eval3(p[1], row) is not True
    raise PredicateError("bad canonical predicate %r" % (p,))


def admitted(p, row: dict) -> bool:
    """Only TRUE enters a result; FALSE and NULL do not."""
    return eval3(p, row) is True


def eval3_constants(p):
    """Evaluate a predicate that mentions no columns (TRUE/FALSE only)."""
    return eval3(p, {})
