# Data cache: the guild server's datasets as last downloaded, with their ETags, under data\datasets\.
"""This module is the only writer of data\\datasets\\ (index.json plus one <name>.json per dataset).

The index keeps each dataset's ETag, so the next check sends `If-None-Match` and an unchanged dataset
costs nothing, and when the key was last accepted (`checkedAt`, with the key id it belongs to), so the
72 h offline grace (gate.py) survives a restart. The key id is the public part of the key; the secret
never reaches a file here. Data already downloaded stays on this PC when a key is revoked; a refused
key only clears `checkedAt`.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

from .fsutil import read_json, try_write_json

log = logging.getLogger(__name__)

NAMES = ("routes", "regions")  # the datasets this detector uses; any other listed name is ignored
CHECKED_STEP_S = 3600.0  # checkedAt is saved again only after this long: a sync every minute writes no file


def key_id(key: str | None) -> str | None:
    """The public id part of `gs1_<keyid>_<secret>`."""
    return key.split("_")[1] if key else None


class DataCache:
    def __init__(self, data_dir: Path | None) -> None:
        self.dir = data_dir / "datasets" if data_dir else None
        self.index: dict = {"keyId": None, "checkedAt": None, "etags": {}}

    def _path(self, name: str) -> Path | None:
        return self.dir / f"{name}.json" if self.dir else None

    def load(self) -> dict[str, dict]:
        """Read the index and every cached dataset it names; a missing or bad file loses its ETag."""
        got = read_json(self._path("index"), dict, {})
        etags = got.get("etags") if isinstance(got.get("etags"), dict) else {}
        at = got.get("checkedAt")
        self.index = {"keyId": got.get("keyId") if isinstance(got.get("keyId"), str) else None,
                      "checkedAt": float(at) if isinstance(at, (int, float)) and math.isfinite(at) else None,
                      "etags": {}}
        out = {}
        for name in NAMES:
            data = read_json(self._path(name), dict)
            if data is not None and isinstance(etags.get(name), str):
                out[name], self.index["etags"][name] = data, etags[name]
        return out

    def etag(self, name: str) -> str | None:
        return self.index["etags"].get(name)

    def checked_at(self, key: str | None) -> float | None:
        """When this key was last accepted, from the file; None for another key."""
        return self.index["checkedAt"] if key and self.index["keyId"] == key_id(key) else None

    def store(self, name: str, etag: str | None, data: dict) -> None:
        if try_write_json(self._path(name), data, f"dataset {name}"):
            if etag:
                self.index["etags"][name] = etag
            else:
                self.index["etags"].pop(name, None)  # no ETag: fetch it whole next time
            self._save()

    def note_check(self, key: str | None, at: float | None) -> None:
        """Remember the last accepted check of `key` (None after a refusal), at most once per CHECKED_STEP_S."""
        same = self.index["keyId"] == key_id(key)
        old = self.index["checkedAt"] if same else None
        if same and (at == old or (at is not None and old is not None and 0 <= at - old < CHECKED_STEP_S)):
            return
        self.index["keyId"], self.index["checkedAt"] = key_id(key), at
        self._save()

    def _save(self) -> None:
        try_write_json(self._path("index"), self.index, "dataset index")
