"""Command-line entry points.

    python -m histjoin.cli build ORDERS CUSTOMER_HIST STORE_HIST OUT_DIR [options]
    python -m histjoin.cli lineage OUT_DIR ROW_ID
    python -m histjoin.cli quarantine OUT_DIR
    python -m histjoin.cli facts OUT_DIR
    python -m histjoin.cli counts OUT_DIR

All output is JSON on stdout.  ``build`` prints the publish manifest; on an
input error it exits non-zero with the file and 1-based line number on stderr
and leaves any previously published result directory untouched.
"""

from __future__ import annotations

import json
import sys

from . import builder, query
from .errors import HistJoinError


def _dump(obj):
    json.dump(obj, sys.stdout, indent=2, sort_keys=True, ensure_ascii=False)
    sys.stdout.write("\n")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.stderr.write(__doc__)
        return 2
    cmd, args = argv[0], argv[1:]
    try:
        if cmd == "build":
            return _build(args)
        if cmd == "lineage":
            _dump(query.lineage_for(args[0], args[1]))
            return 0
        if cmd == "quarantine":
            _dump(query.list_quarantine(args[0]))
            return 0
        if cmd == "facts":
            _dump(query.list_facts(args[0]))
            return 0
        if cmd == "counts":
            _dump(query.counts(args[0]))
            return 0
        sys.stderr.write("unknown command %r\n%s" % (cmd, __doc__))
        return 2
    except HistJoinError as exc:
        sys.stderr.write(json.dumps(exc.as_dict(), sort_keys=True) + "\n")
        return 1


def _build(args):
    opts = {
        "chunk": builder.DEFAULT_CHUNK,
        "cache_kib": builder.DEFAULT_CACHE_KIB,
        "work_db_path": None,
        "keep_work_db": False,
    }
    positional = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--chunk":
            opts["chunk"] = int(args[i + 1]); i += 2; continue
        if a == "--cache-kib":
            opts["cache_kib"] = int(args[i + 1]); i += 2; continue
        if a == "--work-db":
            opts["work_db_path"] = args[i + 1]; i += 2; continue
        if a == "--keep-work-db":
            opts["keep_work_db"] = True; i += 1; continue
        positional.append(a); i += 1
    if len(positional) != 4:
        sys.stderr.write("build needs ORDERS CUSTOMER_HIST STORE_HIST OUT_DIR\n")
        return 2
    orders, cust, store, out_dir = positional
    manifest = builder.build_warehouse(orders, cust, store, out_dir, **opts)
    _dump(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
