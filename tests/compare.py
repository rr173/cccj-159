"""Compare product results with the independent reference enumerator."""

from __future__ import annotations

from fractions import Fraction


def normalise_product_row(row):
    """Product row -> comparable form (avoids importing product module)."""
    out = {"group": dict(row["group"])}
    for k, v in row.items():
        if k == "group":
            continue
        if isinstance(v, dict) and v.get("__fraction__"):
            out[k] = Fraction(v["num"], v["den"])
        else:
            out[k] = v
    return out


def normalise_rows(rows, query):
    return [normalise_product_row(r) for r in rows]


def compare(product_rows, expected_rows, query):
    """Return list of human-readable differences (empty means equal)."""
    got = normalise_rows(product_rows, query)
    if len(got) != len(expected_rows):
        return [
            "row count: product %d vs reference %d" % (len(got), len(expected_rows)),
            "product: %r" % (got,),
            "reference: %r" % (expected_rows,),
        ]
    problems = []
    gcols = list(query.get("group_by", []))
    exp_by_key = {tuple(sorted(r["group"].items())): r for r in expected_rows}
    for g in got:
        k = tuple(sorted(g["group"].items()))
        e = exp_by_key.get(k)
        if e is None:
            problems.append("unexpected group %r" % (g["group"],))
            continue
        for m in query["measures"]:
            alias = m["as"]
            if g.get(alias) != e.get(alias):
                problems.append(
                    "group %r alias %r: product %r vs reference %r"
                    % (g["group"], alias, g.get(alias), e.get(alias))
                )
    matched = {tuple(sorted(g["group"].items())) for g in got}
    for k, e in exp_by_key.items():
        if k not in matched:
            problems.append("missing group %r" % (e["group"],))
    return problems
