"""Planning: prove which facts may come from materialisations, exactly.

Each materialised group is a *lemma* over base rows:

    the rows of group g in materialisation M are exactly the base rows admitted
    by M.predicate whose values on M.group_cols are g.key, with every retained
    column pinned to its proven-constant value.

`region_of_group` takes the exact true-set of that sentence with
`region.true_set`; nothing is estimated.  A group contributes only when its
region

  * is wholly inside the query's admitted region (a group is never split), and
  * is disjoint from every region already credited (no double counting even
    when materialisation ranges intersect), and
  * has all query filter columns pinned by its key or retained pins (otherwise
    the group could straddle a query boundary and its internal share is not
    provable).

The result is a pairwise-disjoint contribution partition.  The rest of the
query is a residual computed as a genuine three-valued complement
``Q AND NOT_TRUE(cover)``, so rows where the cover predicate is NULL are kept.
Only base rows *true* under that residual enter the answer.
"""

from __future__ import annotations

import os

from . import pred, region, userdb
from .errors import MaterialisationUnusable, SnapshotMismatchError
from .query import parse_query

DEFAULT_BUDGET = 8192


# ---------------------------------------------------------------------------
# candidates and segments
# ---------------------------------------------------------------------------

class GroupCandidate:
    __slots__ = ("mid", "name", "gid", "key", "state", "retained", "region",
                 "data_file", "n")

    def __init__(self, mid, name, gid, key, state, retained, region_worlds, data_file):
        self.mid = mid
        self.name = name
        self.gid = gid
        self.key = key
        self.state = state
        self.retained = retained
        self.region = region_worlds
        self.data_file = data_file
        self.n = state.get("n", 0)


class Segment:
    def __init__(self, kind, **kw):
        self.kind = kind
        self.mat_id = kw.get("mat_id")
        self.name = kw.get("name")
        self.gid = kw.get("gid")
        self.output_key = kw.get("output_key", ())
        self.state = kw.get("state")
        self.key = kw.get("key")
        self.worlds = kw.get("worlds")
        self.residual_pred = kw.get("residual_pred")
        self.reason = kw.get("reason", "")
        self.data_file = kw.get("data_file")


class Plan:
    def __init__(self, query, snapshot_id, fact_path, segments, rejections,
                 reports, coverage, notes, budget):
        self.query = query
        self.snapshot_id = snapshot_id
        self.fact_path = fact_path
        self.segments = segments
        self.rejections = rejections
        self.reports = reports
        self.coverage = coverage
        self.notes = notes
        self.budget = budget


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def key_condition(group_cols, key):
    atoms = []
    for c, v in zip(group_cols, key):
        atoms.append(("null", c, True) if v is None else ("eq", c, v))
    return pred.band(*atoms)


def retained_condition(retained):
    atoms = [
        ("null", c, True) if retained[c] is None else ("eq", c, retained[c])
        for c in sorted(retained)
    ]
    return pred.band(*atoms)


def project_key(mcols, key, qcols):
    pos = {c: i for i, c in enumerate(mcols)}
    return tuple(key[pos[c]] for c in qcols)


def missing_state(meta, query):
    measures = list(meta.get("measures") or [])
    cols = set(meta.get("state_cols") or [])
    missing = []
    for m in query["measures"]:
        if m.kind == "count_star":
            if "n" not in cols:
                missing.append("count(*)")
        elif m.kind == "count":
            if m.col not in measures or ("nn:" + m.col) not in cols:
                missing.append("count(%s)" % m.col)
        elif m.kind in ("sum", "avg"):
            if (m.col not in measures or ("nn:" + m.col) not in cols
                    or ("sum:" + m.col) not in cols):
                missing.append("%s(%s)" % (m.kind, m.col))
    return missing


# ---------------------------------------------------------------------------
# main entry
# ---------------------------------------------------------------------------

def make_plan(catalogue, snapshot_id, query_spec, budget=DEFAULT_BUDGET):
    schema = catalogue.schema()
    query = parse_query(query_spec, schema)
    snap = catalogue.snapshot(snapshot_id)
    current_digest = verify_snapshot_facts(catalogue, snap, schema)

    qcols = list(query["group_cols"])
    notes = []
    rejections = []
    reports = []

    try:
        q_true = region.disjointify(
            region.true_set(query["predicate"], schema, budget), schema.fields, budget
        )
    except region.RegionLimit:
        q_true = None
        notes.append("query region exceeded the world budget; base scan will be used")

    registered = [r["mat_id"] for r in catalogue.registrations(snapshot_id)]
    report_by_mid = {}
    prepared = []
    for mid in registered:
        rep = {
            "mat_id": mid,
            "name": mid,
            "usable": False,
            "reason": None,
            "detail": "",
            "groups": [],
            "selected_groups": 0,
        }
        reports.append(rep)
        report_by_mid[mid] = rep
        prep = _prepare(catalogue, snapshot_id, mid, rep, schema, query, qcols,
                        current_digest)
        if prep is None:
            rejections.append(
                {"mat_id": mid, "name": rep["name"], "reason": rep["reason"],
                 "message": rep["detail"]}
            )
        else:
            prepared.append(prep)

    rejections.extend(
        _unregistered_and_foreign(catalogue, snapshot_id, set(registered),
                                  current_digest, reports)
    )

    selected = []
    covered = []
    for prep in prepared:
        _select_groups(prep, q_true, query, qcols, selected, covered, schema, budget)

    segments = []
    for cand, out_key in selected:
        segments.append(
            Segment(
                "materialisation",
                mat_id=cand.mid,
                name=cand.name,
                gid=cand.gid,
                output_key=out_key,
                state=cand.state,
                key=cand.key,
                worlds=cand.region,
                data_file=cand.data_file,
            )
        )

    base_segment, coverage, keep_materialised = _residual(
        query, q_true, covered, selected, schema, budget, notes
    )
    if not keep_materialised:
        # The residual could not be computed exactly, so the whole query is
        # answered by one three-valued base scan.  Crediting the selected
        # groups as well would count their rows twice; drop them.
        segments = [s for s in segments if s.kind != "materialisation"]
        for rep in reports:
            if rep["selected_groups"]:
                rep["detail"] = (
                    (rep["detail"] or "")
                    + " [contributions withdrawn: residual could not be proven exact]"
                ).strip()
            rep["selected_groups"] = 0
            for entry in rep["groups"]:
                if entry.get("status") == "selected":
                    entry["status"] = "skipped"
                    entry["reason"] = "residual_not_provable"
        coverage["materialisation_groups"] = 0
        coverage["covered"] = False
    if base_segment is not None:
        segments.append(base_segment)

    return Plan(query, snapshot_id, snap["fact_path"], segments, rejections,
                reports, coverage, notes, budget)


# ---------------------------------------------------------------------------
# snapshot fact verification
# ---------------------------------------------------------------------------

def verify_snapshot_facts(catalogue, snap, schema) -> str:
    from .catalog import facts_digest, read_facts

    path = snap["fact_path"]
    if not os.path.exists(path):
        raise SnapshotMismatchError("snapshot fact file is missing: %s" % path)
    leaves = read_facts(path, schema)
    digest = facts_digest(leaves, schema)
    if digest != snap["fact_digest"]:
        raise SnapshotMismatchError(
            "fact table changed since snapshot %s was built; refusing to reuse "
            "materialised groups against different base rows" % snap["snapshot_id"]
        )
    if len(leaves) != snap["n_facts"]:
        raise SnapshotMismatchError(
            "fact row count changed since snapshot %s was built" % snap["snapshot_id"]
        )
    return digest


# ---------------------------------------------------------------------------
# materialisation preparation
# ---------------------------------------------------------------------------

def _prepare(catalogue, snapshot_id, mid, rep, schema, query, qcols, current_digest):
    try:
        meta = catalogue.mat_meta(snapshot_id, mid)
    except Exception as exc:
        rep["reason"] = "unreadable_materialisation"
        rep["detail"] = str(exc)
        return None
    rep["name"] = meta.get("name", mid)

    if meta.get("snapshot_id") not in (None, snapshot_id):
        rep["reason"] = "different_snapshot"
        rep["detail"] = "materialisation belongs to snapshot %r" % (meta.get("snapshot_id"),)
        return None
    if meta.get("fact_digest") not in (None, current_digest):
        rep["reason"] = "snapshot_mismatch"
        rep["detail"] = "materialisation was built from different base facts"
        return None
    if meta.get("field_digest") != schema.field_digest:
        rep["reason"] = "schema_mismatch"
        rep["detail"] = "materialisation field schema differs from the catalogue"
        return None

    known = set(schema.fields)
    for c in (list(meta.get("group_cols", [])) + list(meta.get("measures", []))
              + list(meta.get("retained_cols", []))):
        if c not in known:
            rep["reason"] = "unknown_field"
            rep["detail"] = "definition references unknown field %r" % (c,)
            return None

    try:
        mpred = pred.parse(meta["predicate_json"], schema)
    except pred.PredicateError as exc:
        rep["reason"] = "invalid_definition"
        rep["detail"] = str(exc)
        return None
    if meta.get("pred_sig") != pred.signature(mpred):
        rep["reason"] = "definition_digest_mismatch"
        rep["detail"] = "stored predicate does not match its digest"
        return None

    if not set(qcols) <= set(meta.get("group_cols", [])):
        rep["reason"] = "coarser_group_cols"
        rep["detail"] = (
            "materialisation groups by %r; a coarser group cannot be split into "
            "query groups %r" % (meta.get("group_cols", []), qcols)
        )
        return None

    try:
        data = userdb.read_materialisation(catalogue.mat_dir(snapshot_id, mid), meta, schema)
    except MaterialisationUnusable as exc:
        rep["reason"] = exc.reason
        rep["detail"] = str(exc)
        return None
    except Exception as exc:
        rep["reason"] = "unreadable_materialisation"
        rep["detail"] = str(exc)
        return None

    miss = missing_state(meta, query)
    if miss:
        rep["reason"] = "missing_aggregate_state"
        rep["detail"] = "cannot answer %s from stored state %r" % (
            ", ".join(miss), sorted(set(meta.get("state_cols") or [])),
        )
        return None

    rep["usable"] = True
    rep["group_cols"] = list(meta.get("group_cols", []))
    rep["retained_cols"] = list(meta.get("retained_cols", []))
    rep["measures"] = list(meta.get("measures", []))
    rep["n_groups"] = data["physical_rows"]
    rep["data_file"] = data["file"]
    rep["file_size"] = data["size"]

    return {
        "mid": mid,
        "name": rep["name"],
        "meta": meta,
        "mpred": mpred,
        "mcols": list(meta.get("group_cols", [])),
        "data": data,
        "rep": rep,
        "qcols": qcols,
        "data_file": data["file"],
    }


# ---------------------------------------------------------------------------
# group selection
# ---------------------------------------------------------------------------

def _select_groups(prep, q_true, query, qcols, selected, covered, schema, budget):
    rep = prep["rep"]
    mpred = prep["mpred"]
    mcols = prep["mcols"]
    for gid in sorted(prep["data"]["groups"]):
        g = prep["data"]["groups"][gid]
        key = tuple(g["key"])
        entry = {"gid": gid, "key": list(key), "status": None, "reason": None}
        rep["groups"].append(entry)
        gregion = _group_region(g, mpred, mcols, schema, budget, entry)
        if gregion is None:
            continue
        if q_true is None:
            entry["status"] = "skipped"
            entry["reason"] = "query_region_unknown"
            continue
        if not _inside(gregion, q_true, schema, budget, entry, query["predicate"],
                       mcols, list(prep["meta"].get("retained_cols", []))):
            continue
        if _overlaps(gregion, covered, schema, budget, entry):
            continue
        cand = GroupCandidate(
            prep["mid"], prep["name"], gid, key, g["state"], g["retained"],
            gregion, prep["data_file"],
        )
        selected.append((cand, project_key(mcols, key, qcols)))
        try:
            covered[:] = region.region_union(
                covered, gregion, budget, schema.fields
            )
        except region.RegionLimit:
            selected.pop()
            entry["status"] = "skipped"
            entry["reason"] = "region_budget_exceeded"
            break
        entry["status"] = "selected"
        entry["rows_covered"] = g["state"].get("n", 0)
        rep["selected_groups"] += 1


def _group_region(g, mpred, mcols, schema, budget, entry):
    sentence = pred.band(
        mpred, key_condition(mcols, tuple(g["key"])), retained_condition(g["retained"])
    )
    try:
        gregion = region.true_set(sentence, schema, budget)
    except region.RegionLimit:
        entry["status"] = "skipped"
        entry["reason"] = "region_budget_exceeded"
        return None
    if not gregion:
        entry["status"] = "skipped"
        entry["reason"] = "group_region_empty"
        return None
    return gregion


def _inside(gregion, q_true, schema, budget, entry, query_pred=None,
            mcols=(), retained_cols=()):
    try:
        overlap = region.region_intersect(gregion, q_true, schema.fields, budget)
    except region.RegionLimit:
        entry["status"] = "skipped"
        entry["reason"] = "region_budget_exceeded"
        return False
    if not overlap:
        entry["status"] = "skipped"
        entry["reason"] = "group_outside_query"
        return False
    try:
        outside = region.region_subtract(gregion, q_true, schema.fields, budget)
    except region.RegionLimit:
        entry["status"] = "skipped"
        entry["reason"] = "region_budget_exceeded"
        return False
    if outside:
        entry["status"] = "skipped"
        entry["reason"] = "group_crosses_boundary_unsplittable"
        unpinned = sorted(
            pred.cols_of(query_pred) - set(mcols) - set(retained_cols)
        ) if query_pred is not None else []
        entry["detail"] = (
            "the materialised group straddles the query boundary; its saved key "
            "and retained columns cannot determine what share lies inside, and a "
            "group is never split by proportion"
            + (
                " (queried filter column(s) %r are neither grouped nor retained)"
                % (unpinned,)
                if unpinned else ""
            )
        )
        return False
    return True


def _overlaps(gregion, covered, schema, budget, entry):
    try:
        overlap = region.region_intersect(gregion, covered, schema.fields, budget)
    except region.RegionLimit:
        entry["status"] = "skipped"
        entry["reason"] = "region_budget_exceeded"
        return True
    if overlap:
        entry["status"] = "skipped"
        entry["reason"] = "region_already_covered"
        entry["detail"] = "another materialisation already credited these rows"
        return True
    return False


# ---------------------------------------------------------------------------
# residual / complement
# ---------------------------------------------------------------------------

def _residual(query, q_true, covered, selected, schema, budget, notes):
    if q_true is None:
        seg = Segment(
            "base",
            residual_pred=query["predicate"],
            reason="query region could not be represented; full three-valued base scan",
        )
        return seg, {"covered": False, "fallback": "full_base_scan"}, False

    try:
        noncovered = region.region_subtract(q_true, covered, schema.fields, budget)
        noncovered = region.disjointify(noncovered, schema.fields, budget)
    except region.RegionLimit:
        seg = Segment(
            "base",
            residual_pred=query["predicate"],
            reason="complement exceeded the world budget; full three-valued base scan",
        )
        notes.append(
            "materialisation contributions withdrawn: the exact complement "
            "could not be represented within the %d-world budget" % budget
        )
        return seg, {"covered": False, "fallback": "full_base_scan"}, False

    coverage = {
        "covered": bool(selected),
        "materialisation_groups": len(selected),
        "duplicate_regions_avoided": True,
        "query_worlds": len(q_true),
        "residual_worlds": len(noncovered),
        "full_base_fallback": False,
        "specificity": {
            "query_cols": list(query["group_cols"]),
            "exact": True,
        },
    }
    if not noncovered:
        return (
            Segment("base", worlds=[], reason="materialisations cover the full query region"),
            coverage,
            True,
        )

    residual_pred = _residual_predicate(query["predicate"], selected)
    seg = Segment(
        "base",
        worlds=noncovered,
        residual_pred=residual_pred,
        reason=(
            "no materialised group is provably inside the query and uncredited here"
            if not selected
            else "three-valued complement of the credited materialisation regions"
        ),
    )
    return seg, coverage, True


def _residual_predicate(qpred, selected):
    if not selected:
        return qpred
    cover = pred.bor(*[region.region_predicate(cand.region) for cand, _ in selected])
    return pred.band(qpred, pred.not_true(cover))


# ---------------------------------------------------------------------------
# unregistered / foreign materialisations
# ---------------------------------------------------------------------------

def _unregistered_and_foreign(catalogue, snapshot_id, registered, current_digest, reports):
    """Explain every materialisation that exists but may not be used."""
    out = []
    current = catalogue.snapshot(snapshot_id)
    names = current.get("names", {})
    for mid in current.get("materialisations", []):
        if mid in registered:
            continue
        out.append(
            {
                "mat_id": mid,
                "name": names.get(mid, mid),
                "reason": "not_registered",
                "message": "present in snapshot %s but not registered; refusing to use"
                % snapshot_id,
            }
        )
    for snap in catalogue.list_snapshots():
        if snap["snapshot_id"] == snapshot_id:
            continue
        try:
            info = catalogue.snapshot(snap["snapshot_id"])
        except Exception:
            continue
        foreign_names = info.get("names", {})
        same_facts = info.get("fact_digest") == current_digest
        for mid in info.get("materialisations", []):
            if mid in registered:
                continue
            out.append(
                {
                    "mat_id": mid,
                    "name": foreign_names.get(mid, mid),
                    "reason": "different_snapshot",
                    "message": (
                        "belongs to snapshot %s with %s base facts; a materialisation "
                        "is only valid within its own snapshot"
                        % (snap["snapshot_id"], "the same" if same_facts else "different")
                    ),
                }
            )
    return out


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

def plan_to_json(plan):
    return {
        "query": {
            "group_by": list(plan.query["group_cols"]),
            "where": plan.query["predicate_json"],
            "measures": [m.as_json() for m in plan.query["measures"]],
            "fingerprint": plan.query["fingerprint"],
        },
        "snapshot_id": plan.snapshot_id,
        "fact_path": plan.fact_path,
        "segments": [_segment_json(s) for s in plan.segments],
        "coverage": plan.coverage,
        "rejections": plan.rejections,
        "materialisations": plan.reports,
        "notes": plan.notes,
        "budget_worlds": plan.budget,
    }


def _segment_json(s):
    if s.kind == "materialisation":
        return {
            "kind": "materialisation",
            "mat_id": s.mat_id,
            "name": s.name,
            "gid": s.gid,
            "group_key": list(s.key),
            "output_group": list(s.output_key),
            "rows_covered": s.state.get("n", 0),
            "state": {k: v for k, v in sorted(s.state.items()) if k != "key"},
            "region": region.describe_region(s.worlds),
            "data_file": s.data_file,
        }
    d = {
        "kind": "base",
        "reason": s.reason,
        "residual": pred.to_json(s.residual_pred) if s.residual_pred is not None else None,
    }
    if s.worlds is None:
        d["worlds"] = None
        d["full_base_scan"] = True
    else:
        d["worlds"] = region.describe_region(s.worlds)
        d["world_count"] = len(s.worlds)
        d["world_queries"] = [_world_sql_json(w) for w in s.worlds]
    return d


def _world_sql_json(w):
    sql, params = region.world_sql(w)
    return {"sql": sql, "params": params}
