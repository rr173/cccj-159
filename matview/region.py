"""An exact, bounded region algebra over the predicate language.

A *world* is a conjunction of per-column constraints describing a set of fact
rows.  Two things are computed with it, both exactly (no heuristics):

* the **true set** of a predicate: all rows on which the predicate evaluates
  to TRUE under three-valued logic;
* set difference between such sets.

Region subtraction is what makes the contribution partition honest: a
materialised group may be used only if its whole region lies inside the query
and outside every region already credited.  If a part is missing, the group is
skipped wholesale — a group is never split.

Column constraint forms
-----------------------
    NULL_C                 value IS NULL
    INT(lo, hi)            non-null integer in [lo, hi]; None = unbounded
    TVAL(s)                value = s           (text)
    TNOTIN(S)              value NOT IN S, and is not NULL (text; S finite)

Universe = no constraint = NULL plus all values.  Every operation below stays
inside these forms; `complement` enumerates a finite union of forms.  The only
unbounded enumerations come from text `NOT IN S` with finite S (returns one
world per excluded value) and integer interval splits (at most two), so the
algebra is total for the documented finite boolean combinations.

If a computation exceeds `limit` worlds, `RegionLimit` is raised.  Callers
must treat that as "cannot prove", never as "empty".
"""

from __future__ import annotations

import json

from . import pred
from .errors import QueryError

MIN = pred.MIN_I64
MAX = pred.MAX_I64


class RegionLimit(Exception):
    """A region computation exceeded the world budget."""


NULL_C = ("null",)


def int_c(lo, hi):
    """Non-null integer interval.  lo/hi are ints or None (unbounded)."""
    return ("int", lo, hi)


def tval(s):
    return ("tval", s)


def tnotin(values):
    return ("tnotin", frozenset(values))


def nonnull(ftype):
    if ftype == "int":
        return int_c(None, None)
    if ftype == "text":
        return tnotin(())
    return None  # an always-null field has no non-null values


def _max_lo(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _min_hi(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def c_intersect(a, b, ftype):
    """Intersection of two constraints; None means empty."""
    if a is None or b is None:
        return None
    if a[0] == "null" or b[0] == "null":
        return NULL_C if (a[0] == "null" and b[0] == "null") else None
    if ftype == "int":
        lo = _max_lo(a[1], b[1])
        hi = _min_hi(a[2], b[2])
        if lo is not None and hi is not None and lo > hi:
            return None
        return int_c(lo, hi)
    if ftype == "text":
        if a[0] == "tval" and b[0] == "tval":
            return tval(a[1]) if a[1] == b[1] else None
        if a[0] == "tval":
            return tval(a[1]) if a[1] not in b[1] else None
        if b[0] == "tval":
            return tval(b[1]) if b[1] not in a[1] else None
        return tnotin(a[1] | b[1])
    return None


def c_complement(c, ftype):
    """Constraints whose union is every value *not* in `c`.

    Includes NULL when NULL is outside `c`.  Returns [] when the complement is
    empty (only possible for an always-null field plus a non-null constraint).
    """
    if ftype == "null":
        if c[0] == "null":
            return []
        return [NULL_C]
    if c[0] == "null":
        nn = nonnull(ftype)
        return [nn]
    out = [NULL_C]
    if ftype == "int":
        lo, hi = c[1], c[2]
        if lo is not None and lo > MIN:
            out.append(int_c(None, lo - 1))
        if hi is not None and hi < MAX:
            out.append(int_c(hi + 1, None))
        return out
    # text
    if c[0] == "tval":
        out.append(tnotin((c[1],)))
        return out
    # tnotin(S): the complement (other than NULL) is exactly the finite S
    for v in sorted(c[1]):
        out.append(tval(v))
    return out


# ---------------------------------------------------------------------------
# worlds
# ---------------------------------------------------------------------------

def world_key(w):
    return json.dumps(
        sorted((c, _cons_key(v)) for c, v in w.items()),
        sort_keys=True,
        separators=(",", ":"),
    )


def _cons_key(c):
    if c[0] == "null":
        return ["null"]
    if c[0] == "int":
        return ["int", c[1], c[2]]
    if c[0] == "tval":
        return ["tval", c[1]]
    return ["tnotin", sorted(c[1])]


def world_is_empty(w) -> bool:
    return any(v is None for v in w.values())


def world_intersect(a, b, types) -> dict | None:
    out = dict(a)
    for c, cb in b.items():
        if c in out:
            ci = c_intersect(out[c], cb, types.get(c))
            if ci is None:
                return None
            out[c] = ci
        else:
            out[c] = cb
    return out


def world_difference(a, b, types, limit) -> list:
    """Exact decomposition of a \\ b into pairwise disjoint worlds."""
    out = []
    prefix = dict(a)
    for col in sorted(b):
        cb = b[col]
        comps = c_complement(cb, types.get(col))
        for comp in comps:
            w = dict(prefix)
            if col in w:
                ci = c_intersect(w[col], comp, types.get(col))
                if ci is None:
                    continue
                w[col] = ci
            else:
                w[col] = comp
            if not world_is_empty(w):
                out.append(w)
                _check(out, limit)
        # extend the prefix so later pieces require equality on this column
        if col in prefix:
            ci = c_intersect(prefix[col], cb, types.get(col))
            if ci is None:
                break
            prefix[col] = ci
        else:
            prefix[col] = cb
    return out


def _check(seq, limit):
    if limit is not None and len(seq) > limit:
        raise RegionLimit("world budget %d exceeded" % limit)


def _types_for(p, schema):
    return pred.cols_of(p)


def dedupe(worlds, limit=None) -> list:
    seen = {}
    for w in worlds:
        if world_is_empty(w):
            continue
        seen.setdefault(world_key(w), w)
        _check(seen, limit)
    return [seen[k] for k in sorted(seen)]


def disjointify(worlds, types, limit=None) -> list:
    """Canonicalise a union into pairwise-disjoint worlds.

    `region_union` only removes *identical* worlds.  A union of overlapping
    worlds answers set questions correctly, but if it is later executed as one
    query per world the overlaps would be counted twice.  This folds each world
    against those already kept, so the result is a genuine partition.
    """
    out = []
    for w in worlds:
        if world_is_empty(w):
            continue
        pieces = [w]
        for kept in out:
            nxt = []
            for p in pieces:
                nxt.extend(world_difference(p, kept, types, limit))
            pieces = nxt
            if not pieces:
                break
        for p in pieces:
            if not world_is_empty(p):
                out.append(p)
                _check(out, limit)
    return out


def region_union(a, b, limit=None, types=None):
    u = dedupe(list(a) + list(b), limit)
    if types is None:
        return u
    return disjointify(u, types, limit)


def region_intersect(a, b, types, limit=None) -> list:
    out = []
    for wa in a:
        for wb in b:
            w = world_intersect(wa, wb, types)
            if w is not None and not world_is_empty(w):
                out.append(w)
                _check(out, limit)
    return dedupe(out, limit)


def region_subtract(a, b, types, limit=None) -> list:
    """Exact a \\ b, keeping the result as a disjoint union of worlds."""
    out = list(a)
    for wb in b:
        nxt = []
        for wa in out:
            nxt.extend(world_difference(wa, wb, types, limit))
            _check(nxt, limit)
        out = nxt
        if not out:
            return []
    return dedupe(out, limit)


def region_is_empty(a) -> bool:
    return not a or all(world_is_empty(w) for w in a)


def region_equals(a, b, types, limit=None) -> bool:
    return not region_subtract(a, b, types, limit) and not region_subtract(b, a, types, limit)


# ---------------------------------------------------------------------------
# predicate -> true set / false set
# ---------------------------------------------------------------------------

def _atom_true(atom, schema):
    tag = atom[0]
    col = atom[1]
    ftype = schema.fields[col]
    if tag == "range":
        lo = None if atom[2] == MIN else atom[2]
        hi = None if atom[3] == MAX else atom[3]
        return [{col: int_c(lo, hi)}]
    if tag == "eq":
        if ftype == "int":
            return [{col: int_c(atom[2], atom[2])}]
        if ftype == "text":
            return [{col: tval(atom[2])}]
        raise QueryError("eq on always-null field cannot be true")
    if tag == "null":
        if atom[2]:
            return [{col: NULL_C}]
        nn = nonnull(ftype)
        return [] if nn is None else [{col: nn}]
    raise QueryError("bad atom %r" % (atom,))


def _atom_false(atom, schema):
    tag = atom[0]
    col = atom[1]
    ftype = schema.fields[col]
    if tag == "range":
        lo = atom[2]
        hi = atom[3]
        out = []
        if lo > MIN:
            out.append({col: int_c(None, lo - 1)})
        if hi < MAX:
            out.append({col: int_c(hi + 1, None)})
        return out
    if tag == "eq":
        if ftype == "int":
            v = atom[2]
            out = []
            if v > MIN:
                out.append({col: int_c(None, v - 1)})
            if v < MAX:
                out.append({col: int_c(v + 1, None)})
            return out
        if ftype == "text":
            return [{col: tnotin((atom[2],))}]
        return []
    if tag == "null":
        if atom[2]:  # IS NULL is false exactly on non-null values
            nn = nonnull(ftype)
            return [] if nn is None else [{col: nn}]
        return [{col: NULL_C}]
    raise QueryError("bad atom %r" % (atom,))


def true_set(p, schema, limit=None) -> list:
    tag = p[0]
    if tag == "true":
        return [{}]
    if tag == "false":
        return []
    if tag in ("eq", "range", "null"):
        return _atom_true(p, schema)
    if tag == "and":
        out = [{}]
        for a in p[1]:
            out = region_intersect(out, true_set(a, schema, limit), schema.fields, limit)
            if not out:
                return []
        return out
    if tag == "or":
        out = []
        for a in p[1]:
            out = region_union(out, true_set(a, schema, limit), limit)
        return out
    if tag == "not":
        return false_set(p[1], schema, limit)
    if tag == "is_true":
        return true_set(p[1], schema, limit)
    if tag == "not_true":
        return false_set(p[1], schema, limit)
    raise QueryError("bad predicate %r" % (p,))


def false_set(p, schema, limit=None) -> list:
    tag = p[0]
    if tag == "true":
        return []
    if tag == "false":
        return [{}]
    if tag in ("eq", "range", "null"):
        return _atom_false(p, schema)
    if tag == "and":
        out = []
        for a in p[1]:
            out = region_union(out, false_set(a, schema, limit), limit)
        return out
    if tag == "or":
        out = [{}]
        for a in p[1]:
            out = region_intersect(out, false_set(a, schema, limit), schema.fields, limit)
            if not out:
                return []
        return out
    if tag == "not":
        return true_set(p[1], schema, limit)
    if tag == "is_true":
        return region_subtract([{}], true_set(p[1], schema, limit), schema.fields, limit)
    if tag == "not_true":
        return true_set(p[1], schema, limit)
    raise QueryError("bad predicate %r" % (p,))


# ---------------------------------------------------------------------------
# abstract three-valued truth on a whole world (for "is it always TRUE?")
# ---------------------------------------------------------------------------

T, F, N, M = "true", "false", "null", "mixed"


def _atom_status(atom, w, schema):
    tag = atom[0]
    col = atom[1]
    ftype = schema.fields[col]
    if ftype == "null":
        if tag == "null":
            return T if atom[2] else F
        return N
    if col not in w or w[col] is None:
        return M
    c = w[col]
    if tag == "null":
        if c[0] == "null":
            return T if atom[2] else F
        return F if atom[2] else T
    if c[0] == "null":
        return N
    if tag == "range":
        lo, hi = c[1], c[2]
        if lo is not None and hi is not None:
            if lo >= atom[2] and hi <= atom[3]:
                return T
        if (lo is not None and lo > atom[3]) or (hi is not None and hi < atom[2]):
            return F
        # non-null and wholly inside/outside handled above; partial -> varies
        return M
    if tag == "eq":
        if c[0] == "int":
            if c[1] is not None and c[1] == c[2]:
                return T if c[1] == atom[2] else F
            if c[1] is not None and atom[2] < c[1]:
                return F
            if c[2] is not None and atom[2] > c[2]:
                return F
            return M
        if c[0] == "tval":
            return T if c[1] == atom[2] else F
        if c[0] == "tnotin":
            return F if atom[2] in c[1] else M
    return M


def predicate_status(p, w, schema):
    tag = p[0]
    if tag == "true":
        return T
    if tag == "false":
        return F
    if tag in ("eq", "range", "null"):
        return _atom_status(p, w, schema)
    if tag == "and":
        sts = [predicate_status(a, w, schema) for a in p[1]]
        if F in sts:
            return F
        if M in sts:
            return M
        if N in sts:
            return N
        return T
    if tag == "or":
        sts = [predicate_status(a, w, schema) for a in p[1]]
        if T in sts:
            return T
        if M in sts:
            return M
        if N in sts:
            return N
        return F
    if tag == "not":
        s = predicate_status(p[1], w, schema)
        return {T: F, F: T, N: N, M: M}[s]
    if tag == "is_true":
        s = predicate_status(p[1], w, schema)
        return {T: T, F: F, N: F, M: M}[s]
    if tag == "not_true":
        s = predicate_status(p[1], w, schema)
        return {T: F, F: T, N: T, M: M}[s]
    raise QueryError("bad predicate %r" % (p,))


def always_true(p, worlds, schema) -> bool:
    return all(predicate_status(p, w, schema) == T for w in worlds)


# ---------------------------------------------------------------------------
# SQL emission for exact worlds
# ---------------------------------------------------------------------------

def _q(col):
    return '"' + col.replace('"', '""') + '"'


def world_sql(w):
    """(sql, params) selecting exactly the rows of world `w`.

    Constraints are exact, so the emitted condition is exact; the executor
    nevertheless re-evaluates the canonical residual predicate on every row.
    """
    clauses = []
    params = []
    for col in sorted(w):
        c = w[col]
        q = _q(col)
        if c[0] == "null":
            clauses.append("%s IS NULL" % q)
        elif c[0] == "int":
            lo, hi = c[1], c[2]
            if lo is None and hi is None:
                clauses.append("%s IS NOT NULL" % q)
            elif lo is None:
                clauses.append("%s <= ?" % q)
                params.append(hi)
            elif hi is None:
                clauses.append("%s >= ?" % q)
                params.append(lo)
            elif lo == hi:
                clauses.append("%s = ?" % q)
                params.append(lo)
            else:
                clauses.append("%s BETWEEN ? AND ?" % q)
                params.extend([lo, hi])
        elif c[0] == "tval":
            clauses.append("%s = ?" % q)
            params.append(c[1])
        else:
            if not c[1]:
                clauses.append("%s IS NOT NULL" % q)
            else:
                clauses.append("%s IS NOT NULL AND %s NOT IN (%s)" % (
                    q, q, ",".join("?" * len(c[1]))
                ))
                params.extend(sorted(c[1]))
    if not clauses:
        return "1=1", []
    return " AND ".join(clauses), params


def describe_region(worlds):
    """Human-oriented list of region descriptions (no SQL required)."""
    out = []
    for w in worlds:
        parts = []
        for col in sorted(w):
            c = w[col]
            if c[0] == "null":
                parts.append({"col": col, "is_null": True})
            elif c[0] == "int":
                parts.append({"col": col, "lo": c[1], "hi": c[2]})
            elif c[0] == "tval":
                parts.append({"col": col, "eq": c[1]})
            else:
                parts.append({"col": col, "not_in": sorted(c[1])})
        out.append({"and": parts})
    return out


def world_predicate(w):
    """Canonical predicate whose true set is exactly world `w`."""
    atoms = []
    for col in sorted(w):
        c = w[col]
        if c[0] == "null":
            atoms.append(("null", col, True))
        elif c[0] == "int":
            atoms.append(("range", col,
                          MIN if c[1] is None else c[1],
                          MAX if c[2] is None else c[2]))
        elif c[0] == "tval":
            atoms.append(("eq", col, c[1]))
        else:
            t = ("and", tuple(("not", ("eq", col, v)) for v in sorted(c[1]))) if c[1] \
                else pred.TRUE
            atoms.append(("null", col, False))
            atoms.append(t)
    return pred.band(*atoms)


def region_predicate(worlds):
    """Canonical predicate whose true set is exactly the region."""
    if not worlds:
        return pred.FALSE
    return pred.bor(*[world_predicate(w) for w in worlds])


def predicate_json(worlds):
    """The region as structured JSON, for evidence and round-trip tests."""
    return pred.to_json(region_predicate(worlds))

