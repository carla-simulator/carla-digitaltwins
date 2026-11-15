"""Replace a twin's extruded OSM buildings with 3DBAG LoD 2.2 buildings (the Netherlands).

    python tools/threedbag_buildings.py build/<name>             # after `twinmodel bake-export`

Kept for existing scripts: the same as ``tools/real_buildings.py build/<name> --source 3dbag`` (see there
and twinmodel.ingest.buildings_3dbag for what it does; the options are the same).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from real_buildings import main  # noqa: E402 (same folder)

if __name__ == "__main__":
    sys.exit(main(default_source="3dbag"))
