"""Child process used by test_lifecycle: a fresh interpreter opens the dir."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from matview import api, catalog  # noqa: E402

catalogue_dir, snapshot_id, query_json = sys.argv[1], sys.argv[2], sys.argv[3]
cat = catalog.Catalogue(catalogue_dir)
result = api.execute(catalogue_dir, snapshot_id, json.loads(query_json))
print(json.dumps({
    "snapshots": len(cat.list_snapshots()),
    "result": result["rows"],
    "evidence": {
        "base_rows_scanned": result["execution"]["base_rows_scanned"],
        "materialisation_contributions": result["execution"]["materialisation_contributions"],
    },
}, sort_keys=True))
