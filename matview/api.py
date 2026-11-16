"""Public entry points.

    build_snapshot(catalogue_dir, fact_path, specs, schema, ...)  -> snapshot id
    register(catalogue_dir, snapshot_id, mat_id)                  -> explicit registration
    plan(catalogue_dir, snapshot_id, query_json)                  -> plan JSON
    execute(catalogue_dir, snapshot_id, query_json)               -> result + evidence
    explain(catalogue_dir, snapshot_id, query_json)               -> plan + evidence + reasons
    run(...)                                                      -> plan then execute

A new process only needs the catalogue directory path: the schema, the
snapshots and the registrations are all recovered from disk.
"""

from __future__ import annotations

import json
import os

from . import catalog, build, pred, plan as planmod, execute as execcmod
from .errors import CatalogueError, MatviewError, QueryError
from .query import parse_query


def open_catalogue(catalogue_dir: str) -> catalog.Catalogue:
    return catalog.Catalogue(catalogue_dir)


def create_fact_db(fact_path: str, schema_json: dict, rows) -> None:
    schema = pred.Schema.from_json(schema_json)
    catalog.create_fact_db(fact_path, schema, rows)


def build_snapshot(catalogue_dir, fact_path, specs, schema, snapshot_id=None,
                   label="", register=True):
    """Build from real facts.  `schema` is a dict or a Schema."""
    if isinstance(schema, dict):
        schema = pred.Schema.from_json(schema)
    cat = catalog.Catalogue(catalogue_dir)
    return cat.build_snapshot(
        fact_path, specs, schema, snapshot_id=snapshot_id, label=label,
        register=register,
    )


def register(catalogue_dir, snapshot_id, mat_id):
    cat = catalog.Catalogue(catalogue_dir)
    cat.register(snapshot_id, mat_id)
    return {"snapshot_id": snapshot_id, "mat_id": mat_id, "registered": True}


def plan(catalogue_dir, snapshot_id, query_json, budget=planmod.DEFAULT_BUDGET):
    cat = catalog.Catalogue(catalogue_dir)
    p = planmod.make_plan(cat, snapshot_id, query_json, budget=budget)
    return planmod.plan_to_json(p)


def execute(catalogue_dir, snapshot_id, query_json, budget=planmod.DEFAULT_BUDGET):
    cat = catalog.Catalogue(catalogue_dir)
    p = planmod.make_plan(cat, snapshot_id, query_json, budget=budget)
    return execcmod.Execution(cat, p).run()


def run(catalogue_dir, snapshot_id, query_json, budget=planmod.DEFAULT_BUDGET):
    return execute(catalogue_dir, snapshot_id, query_json, budget=budget)


def explain(catalogue_dir, snapshot_id, query_json, budget=planmod.DEFAULT_BUDGET):
    cat = catalog.Catalogue(catalogue_dir)
    schema = cat.schema()
    query = parse_query(query_json, schema)
    p = planmod.make_plan(cat, snapshot_id, query_json, budget=budget)
    result = execcmod.Execution(cat, p).run()
    plan_json = planmod.plan_to_json(p)
    per_mat = _per_materialisation(plan_json)
    return {
        "snapshot_id": snapshot_id,
        "query": plan_json["query"],
        "result": result["rows"],
        "segments": result["segments"],
        "coverage": result["coverage"],
        "materialisations": per_mat,
        "rejections": plan_json["rejections"],
        "unavailable_reasons": {
            r["mat_id"]: {"reason": r["reason"], "message": r["message"]}
            for r in plan_json["rejections"]
        },
        "evidence": {
            "base_rows_scanned": result["execution"]["base_rows_scanned"],
            "base_rows_admitted": result["execution"]["base_rows_admitted"],
            "base_full_scan": result["execution"]["base_full_scan"],
            "fact_table": result["execution"]["fact_table"],
            "materialisation_reads": result["execution"]["materialisation_reads"],
            "world_queries": result["execution"]["world_queries"],
        },
        "plan": plan_json,
    }


def _per_materialisation(plan_json):
    out = []
    segs = {}
    for s in plan_json["segments"]:
        if s["kind"] == "materialisation":
            segs.setdefault(s["mat_id"], []).append(
                {
                    "gid": s["gid"],
                    "group_key": s["group_key"],
                    "rows_covered": s["rows_covered"],
                    "region": s["region"],
                }
            )
    for rep in plan_json["materialisations"]:
        g = rep["groups"]
        skipped = [
            {"gid": e["gid"], "key": e["key"], "reason": e["reason"],
             "detail": e.get("detail", "")}
            for e in g if e["status"] == "skipped" and e["reason"]
            not in ("group_outside_query",)
        ]
        out.append(
            {
                "mat_id": rep["mat_id"],
                "name": rep["name"],
                "usable": rep["usable"],
                "reason": rep["reason"],
                "detail": rep["detail"],
                "selected_groups": segs.get(rep["mat_id"], []),
                "skipped_groups_summary": _group_reason_counts(g),
                "skipped_groups": skipped,
            }
        )
    return out


def _group_reason_counts(groups):
    counts = {}
    for e in groups:
        r = e["reason"] or e["status"]
        counts[r] = counts.get(r, 0) + 1
    return counts
