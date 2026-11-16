"""Structured query definitions and canonical output layout.

A query is a JSON object:

    {
      "group_by": ["d"],                    # optional; [] means one global row
      "where": <predicate>,                 # optional; default TRUE
      "measures": [                          # non-empty; order is output order
        {"kind": "count_star", "as": "cnt"},
        {"kind": "count", "col": "amount", "as": "nn"},
        {"kind": "sum",   "col": "amount", "as": "total"},
        {"kind": "avg",   "col": "amount", "as": "mean"}
      ]
    }

Measure semantics (all three-valued aware):
  count(*)   -> number of admitted rows                       (never null)
  count(x)   -> number of admitted rows with x IS NOT NULL     (never null)
  sum(x)     -> exact integer sum; NULL when no non-null value
  avg(x)     -> sum(x)/count(x) as an exact reduced fraction;
                NULL when no non-null value

With no group_by, the result is exactly one row even when nothing matches
(counts are 0, sum/avg are NULL).  With group_by, only groups that contain at
least one admitted row appear (SQL GROUP BY).
"""

from __future__ import annotations

import hashlib
from fractions import Fraction

from . import pred
from .errors import QueryError

MEASURE_KINDS = ("count_star", "count", "sum", "avg")


class Measure:
    __slots__ = ("kind", "col", "alias")

    def __init__(self, kind, col, alias):
        self.kind = kind
        self.col = col
        self.alias = alias

    def __eq__(self, other):
        return (
            isinstance(other, Measure)
            and (self.kind, self.col, self.alias) == (other.kind, other.col, other.alias)
        )

    def __hash__(self):
        return hash((self.kind, self.col, self.alias))

    def __repr__(self):
        return "Measure(%s,%r,%r)" % (self.kind, self.col, self.alias)

    def as_json(self):
        d = {"kind": self.kind, "as": self.alias}
        if self.col is not None:
            d["col"] = self.col
        return d

    def base_cols(self) -> set:
        return set() if self.col is None else {self.col}

    def is_avg(self):
        return self.kind == "avg"


def parse_query(obj, schema: pred.Schema) -> dict:
    if not isinstance(obj, dict):
        raise QueryError("query must be an object")
    gcols = obj.get("group_by", [])
    if not isinstance(gcols, list):
        raise QueryError("group_by must be a list")
    for c in gcols:
        schema.require(c)
    if len(set(gcols)) != len(gcols):
        raise QueryError("duplicate group_by column")
    wpred = obj.get("where", {"op": "and", "args": []})
    try:
        qpred = pred.parse(wpred, schema)
    except pred.PredicateError as exc:
        raise QueryError("invalid where: %s" % exc)
    raw = obj.get("measures")
    if not isinstance(raw, list) or not raw:
        raise QueryError("measures must be a non-empty list")
    measures = []
    seen = set()
    for i, m in enumerate(raw):
        if not isinstance(m, dict):
            raise QueryError("measure %d must be an object" % i)
        kind = m.get("kind")
        if kind not in MEASURE_KINDS:
            raise QueryError(
                "measure %d: kind must be one of %r" % (i, list(MEASURE_KINDS))
            )
        alias = m.get("as")
        if not isinstance(alias, str) or not alias:
            raise QueryError("measure %d: needs a non-empty 'as' alias" % i)
        if alias in seen:
            raise QueryError("duplicate measure alias %r" % (alias,))
        seen.add(alias)
        col = m.get("col")
        if kind == "count_star":
            if col is not None:
                raise QueryError("count_star takes no col")
            measures.append(Measure(kind, None, alias))
            continue
        if not isinstance(col, str):
            raise QueryError("measure %r needs a col" % (alias,))
        schema.require(col)
        if kind in ("sum", "avg") and not schema.is_measure(col):
            raise QueryError(
                "measure %r: %s(%r) needs an int field (schema says %r)"
                % (alias, kind, col, schema.fields.get(col))
            )
        measures.append(Measure(kind, col, alias))
    return {
        "group_cols": list(gcols),
        "predicate": qpred,
        "predicate_json": pred.to_json(qpred),
        "measures": measures,
        "raw": obj,
        "fingerprint": _fingerprint(gcols, qpred, measures),
    }


def _fingerprint(gcols, qpred, measures) -> str:
    payload = {
        "group_by": list(gcols),
        "where": pred.json_dumps(qpred),
        "measures": [m.as_json() for m in measures],
    }
    return "query:" + hashlib.sha256(
        pred.json_dumps(payload).encode("utf-8")
    ).hexdigest()[:24]


def count_expr_of(m: Measure):
    """(count-alias, x-column or None) for the quantity a measure counts."""
    return m.kind, m.col


def output_key_cols(query) -> tuple:
    return tuple(query["group_cols"])


# ---------------------------------------------------------------------------
# result assembly
# ---------------------------------------------------------------------------

class GroupAcc:
    """Answer accumulator: count(*), and per measure (sum, count) or count."""

    __slots__ = ("n", "counts", "sums", "nns")

    def __init__(self, measures):
        self.n = 0
        self.counts = [0] * len(measures)
        self.sums = [0] * len(measures)
        self.nns = [0] * len(measures)

    def add_row(self, row: dict, config):
        self.n += 1
        for i, m in enumerate(config["measures"]):
            if m.kind in ("count", "count_star"):
                self.counts[i] += 1 if (m.col is None or row.get(m.col) is not None) else 0
            else:
                v = row.get(m.col)
                if v is not None:
                    self.sums[i] += v
                    self.nns[i] += 1

    def add_state(self, st, config):
        """Absorb a materialised group's stored state."""
        self.n += st.get("n", 0)
        for i, m in enumerate(config["measures"]):
            if m.kind in ("count", "count_star"):
                self.counts[i] += st.get("n", 0) if m.col is None else st.get("nn:" + m.col, 0)
            else:
                self.sums[i] += st.get("sum:" + m.col, 0)
                self.nns[i] += st.get("nn:" + m.col, 0)

    def as_values(self, config, include_counts=True) -> dict:
        out = {}
        for i, m in enumerate(config["measures"]):
            if m.kind == "count_star":
                out[m.alias] = self.n
            elif m.kind == "count":
                out[m.alias] = self.counts[i]
            elif m.kind == "sum":
                out[m.alias] = None if self.nns[i] == 0 else self.sums[i]
            elif m.kind == "avg":
                if self.nns[i] == 0:
                    out[m.alias] = None
                else:
                    fr = Fraction(self.sums[i], self.nns[i])
                    out[m.alias] = fr
        return out


def value_to_json(v):
    if isinstance(v, Fraction):
        return {
            "__fraction__": True,
            "num": v.numerator,
            "den": v.denominator,
            "value": float(v),
        }
    return v


def result_rows(accs: dict, config) -> list:
    """Turn keyed accumulators into ordered result rows.

    No group_by: one row, even when `accs` is empty.
    With group_by: one row per group seen.
    """
    gcols = config["group_cols"]
    if not gcols:
        acc = accs.get(()) or GroupAcc(config["measures"])
        row = {"group": {}}
        row.update({m.alias: value_to_json(v) for m, v in _pairs(acc, config)})
        return [row]
    rows = []
    for key in sorted(accs, key=lambda k: _sort_key(k)):
        acc = accs[key]
        row = {"group": {c: v for c, v in zip(gcols, key)}}
        row.update({m.alias: value_to_json(v) for m, v in _pairs(acc, config)})
        rows.append(row)
    return rows


def _pairs(acc, config):
    vals = acc.as_values(config)
    return [(m, vals[m.alias]) for m in config["measures"]]


def _sort_key(key):
    return tuple((v is None, "" if v is None else (0 if isinstance(v, int) else 1), v) for v in key)
