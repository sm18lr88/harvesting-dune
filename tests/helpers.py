import json
from pathlib import Path

import pytest

from harvesting_dune.feed import BusMessage
from harvesting_dune.terrain import CELL_CM

M = 100.0  # cm per metre


def spice(id_, x, y, size="Medium", fill=0.8, z=0.0, spawned=True):
    return {"id": id_, "actorType": "SpiceField", "markerType": f"SpiceField{size}", "spriteKey": f"spicefield{size.lower()}",
            "x": x, "y": y, "z": z, "fillRate": fill, "spawned": spawned, "reportable": True}


def ore(id_, x, y, z=0.0, marker="MagnetiteOre"):
    return {"id": id_, "actorType": "ResourceNode", "resourceItemId": marker, "spriteKey": marker.lower(),
            "x": x, "y": y, "z": z, "reportable": True}


def payload(*actors, world="DD", seed=7):
    return {"gated": False, "worldId": world, "coriolisWorldSeed": seed, "actors": list(actors)}


def session(det, world, seed=7, t=0, activity="Exploration", state=None, scan_seed=7):
    det.handle(BusMessage("snapshot", "dune.session", {
        "worldId": world, "coriolisWorldSeed": seed, "mapScanSeed": scan_seed, "worldName": world,
        "activity": activity, "characterState": state or f"CharacterState.State.{activity}",
    }), t)


def pose(det, x, y, z=10.0, t=0):
    det.handle(BusMessage("delta", "dune.player", {"x": x, "y": y, "z": z}), t)


def actors(det, *acts, t=0, world="DD", seed=7):
    det.handle(BusMessage("delta", "dune.map.actors", payload(*acts, world=world, seed=seed)), t)


def mountain(grid, cx, cy, radius_cells=3, height=6000.0):
    """Rock outcrop as the companion would reveal it: ore nodes high above the sand."""
    for i in range(-radius_cells, radius_cells + 1):
        for j in range(-radius_cells, radius_cells + 1):
            x, y = (cx + i + 0.5) * CELL_CM, (cy + j + 0.5) * CELL_CM
            grid.add_height_sample(x, y, height * (1 - max(abs(i), abs(j)) / (radius_cells + 1)))
            grid.add_height_sample(x + 1000, y + 1000, 0.0 if max(abs(i), abs(j)) == radius_cells else height * 0.4)


DATASETS = Path(__file__).parent / "fixtures" / "datasets"  # what the guild server serves (a cut of it)


def datasets(det):
    """Hand the fixture datasets to a detector, as the dataset loop does with the server's."""
    if not DATASETS.is_dir():
        pytest.skip("needs tests/fixtures/datasets: server-held data, not in the public repository")
    for path in sorted(DATASETS.glob("*.json")):
        assert det.on_dataset(path.stem, json.loads(path.read_text("utf8")))
    return det
