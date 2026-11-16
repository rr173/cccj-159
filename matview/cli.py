"""Command-line entry points.

    python -m matview.cli build   CATALOGUE FACTDB SCHEMA_JSON SPECS_JSON
    python -m matview.cli register CATALOGUE SNAPSHOT MAT_ID
    python -m matview.cli plan    CATALOGUE SNAPSHOT QUERY_JSON
    python -m matview.cli execute CATALOGUE SNAPSHOT QUERY_JSON
    python -m matview.cli explain CATALOGUE SNAPSHOT QUERY_JSON
    python -m matview.cli inspect CATALOGUE [SNAPSHOT]

`SCHEMA_JSON` is the fact schema, `SPECS_JSON` a list of materialisation
definitions, `QUERY_JSON` a query definition.  All are structured JSON; no SQL
text is accepted or parsed.  Output is JSON on stdout.
"""

from __future__ import annotations

import json
import sys

from . import api, catalog


def _load(arg):
    if arg == "-":
        return json.load(sys.stdin)
    with open(arg) as f:
        return json.load(f)


def _dump(obj):
    json.dump(obj, sys.stdout, indent=2, sort_keys=True, default=_default)
    sys.stdout.write("\n")


def _default(o):
    try:
        from fractions import Fraction

        if isinstance(o, Fraction):
            return {"num": o.numerator, "den": o.denominator}
    except Exception:
        pass
    raise TypeError("cannot serialise %r" % (type(o),))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.stderr.write(__doc__)
        return 2
    cmd, args = argv[0], argv[1:]
    if cmd == "build":
        catalogue_dir, fact, schema_p, specs_p = args[:4]
        out = api.build_snapshot(catalogue_dir, fact, _load(specs_p), _load(schema_p))
        _dump({"snapshot_id": out})
        return 0
    if cmd == "register":
        catalogue_dir, sid, mid = args[:3]
        _dump(api.register(catalogue_dir, sid, mid))
        return 0
    if cmd == "plan":
        catalogue_dir, sid, query_p = args[:3]
        _dump(api.plan(catalogue_dir, sid, _load(query_p)))
        return 0
    if cmd == "execute":
        catalogue_dir, sid, query_p = args[:3]
        _dump(api.execute(catalogue_dir, sid, _load(query_p)))
        return 0
    if cmd == "explain":
        catalogue_dir, sid, query_p = args[:3]
        _dump(api.explain(catalogue_dir, sid, _load(query_p)))
        return 0
    if cmd == "inspect":
        catalogue_dir = args[0]
        cat = catalog.Catalogue(catalogue_dir)
        if len(args) > 1:
            _dump(cat.snapshot(args[1]))
        else:
            _dump({"snapshots": cat.list_snapshots()})
        return 0
    sys.stderr.write("unknown command %r\n" % (cmd,))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
