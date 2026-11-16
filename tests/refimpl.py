"""Independent reference enumerator for tests.

This module deliberately shares *nothing* with the product's execution path:

* it reads the fact SQLite file with its own trivial query (`SELECT * FROM
  facts`) and no product helper;
* it evaluates predicates with its own three-valued evaluator, written
  directly from SQL semantics, never calling `matview.pred.eval3`;
* it builds answers with its own accumulator, never `matview.query.GroupAcc`
  and never `matview.build.aggregate_leaves`.

Tests compute expected answers with this module and compare against results
produced by the product's plan+execution.  It is also used to check plan
segments for double counting and completeness.
"""

from __future__ import annotations

import sqlite3
from fractions import Fraction


# -------------------------------------------------------------------------
# three-valued predicate semantics (independent implementation)
# -------------------------------------------------------------------------

def _truth_eq(row, col, value):
    v = row[col] if col in row else None
    if v is None or value is None:
        return None
    return v == value


def _truth_range(row, col, lo, hi):
    v = row[col] if col in row else None
    if v is None:
        return None
    if lo is not None and v < lo:
        return False
    if hi is not None and v > hi:
        return False
    return True


def _truth_isnull(row, col):
    v = row[col] if col in row else None
    return v is None


def truth(p, row):
    op = p["op"]
    if op == "eq":
        return _truth_eq(row, p["col"], p["val"])
    if op == "range":
        return _truth_range(row, p["col"], p.get("lo"), p.get("hi"))
    if op == "is_null":
        return _truth_isnull(row, p["col"])
    if op == "and":
        saw_null = False
        for a in p.get("args", []):
            t = truth(a, row)
            if t is False:
                return False
            if t is None:
                saw_null = True
        return None if saw_null else True
    if op == "or":
        saw_null = False
        for a in p.get("args", []):
            t = truth(a, row)
            if t is True:
                return True
            if t is None:
                saw_null = True
        return None if saw_null else False
    if op == "not":
        t = truth(p["arg"], row)
        return None if t is None else (not t)
    # internal atoms, only reached from plan-emitted predicates
    if op == "is_true":
        return truth(p["arg"], row) is True
    if op == "not_true":
        return truth(p["arg"], row) is not True
    raise ValueError("unknown predicate op %r" % (op,))


def admitted(p, row) -> bool:
    return truth(p, row) is True


# -------------------------------------------------------------------------
# independent fact loading and aggregation
# -------------------------------------------------------------------------

def load_facts(fact_path: str, field_names) -> list:
    con = sqlite3.connect("file:%s?mode=ro" % fact_path, uri=True)
    try:
        rid = con.execute("SELECT name FROM pragma_table_info('facts')").fetchone()
        names = [r[0] for r in con.execute("SELECT name FROM pragma_table_info('facts')")]
        ridcol = names[0]
        rows = []
        for vals in con.execute("SELECT * FROM facts"):
            d = dict(zip(names, vals))
            rows.append(d)
        return rows
    finally:
        con.close()


def _sortvalue(v):
    return (v is None, 0 if isinstance(v, int) else 1, "" if v is None else v)


def reference_answer(fact_path, fields, query):
    """Compute the query answer directly from base facts.

    `fields` is the schema field dict; `query` is the raw structured query.
    Returns a list of {group: {...}, alias: value} with Fraction averages.
    """
    where = query.get("where", {"op": "and", "args": []})
    gcols = query.get("group_by", [])
    measures = query["measures"]
    facts = load_facts(fact_path, fields)
    accs = {}
    for row in facts:
        if not admitted(where, row):
            continue
        key = tuple(row.get(c) for c in gcols)
        st = accs.get(key)
        if st is None:
            st = {"n": 0, "sums": [0] * len(measures), "nns": [0] * len(measures)}
            accs[key] = st
        st["n"] += 1
        for i, m in enumerate(measures):
            v = row.get(m.get("col")) if m.get("col") is not None else None
            if m["kind"] in ("count_star", "count"):
                if v is not None:
                    st["nns"][i] += 1
                continue
            if v is not None:
                st["sums"][i] += v
                st["nns"][i] += 1

    def emit(key, st):
        out = {"group": {c: v for c, v in zip(gcols, key)}}
        for i, m in enumerate(measures):
            alias = m["as"]
            if m["kind"] == "count_star":
                out[alias] = st["n"]
            elif m["kind"] == "count":
                out[alias] = st["nns"][i]
            elif m["kind"] == "sum":
                out[alias] = None if st["nns"][i] == 0 else st["sums"][i]
            elif m["kind"] == "avg":
                out[alias] = (
                    None if st["nns"][i] == 0
                    else Fraction(st["sums"][i], st["nns"][i])
                )
        return out

    if not gcols:
        empty = {"n": 0, "sums": [0] * len(measures), "nns": [0] * len(measures)}
        st = accs.get((), empty)
        return [emit((), st)]
    return [emit(k, accs[k]) for k in sorted(accs, key=lambda k: tuple(_sortvalue(v) for v in k))]


