"""Aggregate-state semantics and the physical materialisation writer.

Stored state is always additive and never a finished average:

  * ``n``              count(*) of the group's admitted base rows
  * ``nn:<m>``         number of those rows with m IS NOT NULL
  * ``sum:<m>``        exact integer sum of m over those rows

``count(x)`` is answered from ``nn:<x>``; ``sum(x)`` from ``sum:<x>``;
``avg(x)`` is only ever formed at answer time as an exact reduced fraction
``sum/nn``.  A stored AVG cannot answer a query that needs to merge groups,
because averages are not additive.

Retained columns
----------------
A materialisation may declare extra *retained* columns (e.g. the text column it
filtered on) so that a group can later be placed relative to a query boundary.
Retention is only valid when the column is **constant** over all base rows of
the group; the builder verifies this against real facts and refuses to build
otherwise.  A retained value therefore describes every row of its group, and
planning may use it as a pin.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid

from . import pred, stable
from .errors import MaterialisationBuildError

STATE_VERSION = 1


def _rid_key(rid):
    return (0, rid) if isinstance(rid, int) else (1, str(rid))


_NONCONST = object()


def aggregate_leaves(leaves: dict, p, group_cols, measures, retained=(), state_spec=None) -> tuple:
    """Scan *base* leaves with full three-valued semantics.

    Returns ``(state, retained_map)``:

    * state: ``{key_digest: {"key": [...], "n": int, "nn:m": int, "sum:m": int}}``
    * retained_map: ``{key_digest: {col: value}}``, only for columns proven
      constant over the group.

    `state_spec` lets a definition *omit* parts of the aggregate state on
    purpose, to model a materialisation that only saved, say, an average.
    Accepted values: ``"full"`` (default), or a dict with
    ``{"skip": ["sum:<m>", "nn:<m>", "n"], "extra": {"avg:<m>": true}}``.
    Skipped components are simply not written; planning must then reject the
    materialisation for queries that need them.  ``extra: {"avg:<m>": ...}``
    records a final average as saved metadata — never used as a merge input.
    """
    spec = state_spec or "full"
    skip = set()
    extra_avg = set()
    if isinstance(spec, dict):
        skip = set(spec.get("skip", []))
        if "n" in skip:
            raise MaterialisationBuildError(
                "every materialisation group must keep count(*); refusing to skip 'n'"
            )
        for key, flag in spec.get("extra", {}).items():
            if flag and key.startswith("avg:"):
                extra_avg.add(key[4:])
    state = {}
    ret = {}
    for rid in sorted(leaves, key=_rid_key):
        row = leaves[rid]
        if not pred.admitted(p, row):
            continue
        key = tuple(row.get(c) for c in group_cols)
        kd = stable.key_digest(group_cols, key)
        st = state.get(kd)
        if st is None:
            st = {"key": list(key), "n": 0}
            for m in measures:
                st["nn:" + m] = 0
                st["sum:" + m] = 0
            state[kd] = st
            rv = {}
            for c in retained:
                rv[c] = row.get(c)
            ret[kd] = rv
        st["n"] += 1
        for m in measures:
            v = row.get(m)
            if v is not None:
                st["nn:" + m] += 1
                st["sum:" + m] += v
        rv = ret[kd]
        for c in retained:
            if rv[c] is not _NONCONST and rv[c] != row.get(c):
                rv[c] = _NONCONST
    for kd, rv in ret.items():
        for c, v in rv.items():
            if v is _NONCONST:
                raise MaterialisationBuildError(
                    "retained column %r is not constant within group %r; "
                    "it cannot be used to place the whole group"
                    % (c, state[kd]["key"])
                )
    for st in state.values():
        for m in extra_avg:
            if st.get("nn:" + m, 0) > 0 and ("sum:" + m) in st:
                st["avg:" + m] = {"num": st["sum:" + m], "den": st["nn:" + m]}
        for m in measures:
            if ("nn:" + m) in skip or ("sum:" + m) in skip:
                st.pop("nn:" + m, None)
                st.pop("sum:" + m, None)
    return state, ret


def merge_states(states) -> dict:
    total = None
    for st in states:
        if st is None:
            continue
        if total is None:
            total = {"key": list(st["key"]), "n": 0}
            for k in st:
                if k.startswith("nn:") or k.startswith("sum:"):
                    total[k] = 0
        if list(st["key"]) != list(total["key"]):
            raise MaterialisationBuildError("merging states with different group keys")
        total["n"] += st["n"]
        for k in st:
            if k.startswith("nn:") or k.startswith("sum:"):
                total[k] += st[k]
    return total


def write_materialisation(
    mdir: str,
    base: dict,
    state: dict,
    retained_map: dict,
    schema: pred.Schema,
    name: str,
) -> dict:
    """Write data.sqlite + MAT.json into a fresh directory, atomically.

    The file is written under a temporary name, fsync'd, renamed, then read
    back and compared.  The caller removes the build tree on any exception,
    so a failed build can never leave a registerable half-product.
    """
    measures = list(base.get("measures", []))
    retained = list(base.get("retained_cols", []))
    for c in retained:
        schema.require(c)
        if c in base.get("group_cols", []):
            raise MaterialisationBuildError("retained column %r is a group column" % (c,))
        if schema.fields[c] == "null":
            raise MaterialisationBuildError("cannot retain always-null column %r" % (c,))

    cols = ["n"]
    for m in measures:
        cols += ["nn:" + m, "sum:" + m]
    present = set()
    for st in state.values():
        present |= set(st)
    # a definition may deliberately save only part of the state; with no
    # groups at all the intent is still explicit on the definition
    saved = base.get("saved_cols")
    if saved is not None:
        cols = [c for c in cols if c in set(saved)]
    elif not state:
        # no groups: derive the intended column set from the state spec
        spec = base.get("state_spec") or "full"
        drop = set()
        if isinstance(spec, dict):
            drop = {
                x for x in spec.get("skip", [])
                if x.startswith("nn:") or x.startswith("sum:")
            }
        for m in measures:
            if ("nn:" + m) in drop or ("sum:" + m) in drop:
                drop.add("nn:" + m)
                drop.add("sum:" + m)
        cols = [c for c in cols if c not in drop]
    else:
        cols = [c for c in cols if c in present]

    ddl = [
        "gid INTEGER PRIMARY KEY",
        "key_json TEXT NOT NULL",
    ]
    for c in cols:
        ddl.append('"%s" INTEGER NOT NULL' % c)
    for c in retained:
        ddl.append('"retained:%s" TEXT' % c)
    ddl.append("extra_json TEXT")

    tmp = os.path.join(mdir, "data.tmp-%s.sqlite" % uuid.uuid4().hex)
    dst = os.path.join(mdir, "data.sqlite")
    con = sqlite3.connect(tmp)
    try:
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("PRAGMA synchronous=FULL")
        con.execute("CREATE TABLE groups (%s)" % ", ".join(ddl))
        insert_cols = ["gid", "key_json"] + cols + ["retained:%s" % c for c in retained] + ["extra_json"]
        rows = []
        for gid, kd in enumerate(sorted(state)):
            st = state[kd]
            vals = [gid, pred.json_dumps(st["key"])]
            for c in cols:
                vals.append(st[c])
            for c in retained:
                v = retained_map.get(kd, {}).get(c)
                vals.append(pred.json_dumps(v))
            extras = {
                k: v for k, v in sorted(st.items())
                if k.startswith("avg:") or k.startswith("extra:")
            }
            vals.append(pred.json_dumps(extras))
            rows.append(vals)
        con.executemany(
            'INSERT INTO groups (%s) VALUES (%s)'
            % (", ".join('"%s"' % c for c in insert_cols), ",".join("?" * len(insert_cols))),
            rows,
        )
        con.commit()
    finally:
        con.close()
    os.replace(tmp, dst)
    _fsync_file(dst)

    meta = {
        "kind": "materialisation",
        "name": name,
        "state_version": STATE_VERSION,
        "group_cols": list(base["group_cols"]),
        "measures": measures,
        "retained_cols": retained,
        "predicate_json": base["predicate_json"],
        "pred_sig": base["pred_sig"],
        "state_cols": list(cols),
        "state_spec": base.get("state_spec", "full"),
        "data_file": "data.sqlite",
        "data_sha256": stable.sha256_file(dst),
        "data_size": os.path.getsize(dst),
        "n_groups": len(state),
        "n_rows_covered": sum(st["n"] for st in state.values()),
        "field_digest": schema.field_digest,
        "snapshot_id": base.get("snapshot_id"),
        "mat_id": base.get("mat_id"),
    }

    from .userdb import read_materialisation

    from .userdb import read_materialisation as _read
    back = _read(mdir, meta, schema)
    back_state = {}
    for gid, g in back["groups"].items():
        st = dict(g["state"])
        st.update(g.get("extras") or {})
        back_state[stable.key_digest(meta["group_cols"], g["key"])] = st
    if _canon(back_state) != _canon(state):
        raise MaterialisationBuildError(
            "materialisation %r failed to round-trip through disk" % name
        )
    if {k: v for k, v in back["retained"].items()} != {
        kd: {c: v for c, v in rv.items()} for kd, rv in retained_map.items()
    }:
        raise MaterialisationBuildError(
            "materialisation %r retained columns failed to round-trip" % name
        )
    _write_json(os.path.join(mdir, "MAT.json"), meta)
    return meta


def _canon(state):
    def norm(v):
        if isinstance(v, dict):
            return tuple(sorted(v.items()))
        return v

    return {
        kd: (
            tuple(st["key"]),
            st.get("n"),
            tuple(sorted((k, norm(v)) for k, v in st.items() if k not in ("key", "n"))),
        )
        for kd, st in state.items()
    }


def _fsync_file(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json(path, obj):
    tmp = path + ".tmp-" + uuid.uuid4().hex
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, sort_keys=True, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_path_dir(os.path.dirname(path))


def _fsync_path_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:  # pragma: no cover
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover
        pass
    finally:
        os.close(fd)
