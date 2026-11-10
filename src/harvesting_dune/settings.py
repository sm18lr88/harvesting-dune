# Settings: reads and writes data\settings.json (the choices made on the settings page).
"""The settings file. Its values live on the detector (they are read everywhere); this module is
the only reader and writer of the file, and checks every value it reads."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from .catalog import BY_KEY, EXTRA_BY_KEY, MINIMAP_RESOURCES, RESOURCES
from .fsutil import read_json, try_write_json
from .objects import LABEL_MAX
from .zones import ROWS

if TYPE_CHECKING:
    from .app import Detector

log = logging.getLogger(__name__)

KEY_RE = re.compile(r"gs1_[a-z0-9]{8}_[A-Za-z0-9_-]{32}")  # a guild key; use fullmatch (a trailing newline must fail)


def _number(value: object, low: float, high: float) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and low <= value <= high:
        return float(value)
    return None


def load(det: Detector) -> None:
    s = read_json(det.settings_path, dict)
    if s is None:
        return
    try:
        hidden = s.get("minimapHidden", [])
        det.minimap_hidden = {k for k in hidden if isinstance(k, str) and k in BY_KEY} if isinstance(hidden, list) else set()
        # resources added after these settings were saved start hidden: a new tick box must
        # never un-hide markers or switch your lines off by itself
        seen = s.get("seenResources", [r.key for r in RESOURCES if r.key != "cages"])
        seen = {k for k in seen if isinstance(k, str)} if isinstance(seen, list) else {r.key for r in RESOURCES}
        det.minimap_hidden |= {r.key for r in MINIMAP_RESOURCES if r.key not in seen}
        extras = s.get("minimapExtras", [])
        det.minimap_extras = {k for k in extras if isinstance(k, str) and k in EXTRA_BY_KEY} if isinstance(extras, list) else set()
        det.lines_enabled = bool(s.get("lines", True))
        if isinstance(s.get("resource"), str) and (s["resource"] in BY_KEY or s["resource"].startswith("item:")):
            det.resource_key = s["resource"]  # an item not in the index yet falls back to spice until it is
        else:  # settings from before the choice was saved: the one ticked resource
            ticked = [r.key for r in MINIMAP_RESOURCES if r.key not in det.minimap_hidden]
            if len(ticked) == 1:
                det.resource_key = ticked[0]
        if s.get("lastPveRow") in tuple(ROWS):
            det.last_pve_row = s["lastPveRow"]
        if s.get("linesFromRow") in tuple(ROWS):
            det.lines_from_row = s["linesFromRow"]
        if s.get("minimapFocus") is True and det.lines_enabled:
            det._show_only(det.resource_key)  # the old Focus option: same view, now the rule
        if (km := _number(s.get("maxKm"), 0, 100)) is not None:
            det.max_km = km
        if isinstance(s.get("pvpOnly"), bool):
            det.pvp_only = s["pvpOnly"]
        if (y := _number(s.get("pvpYield"), 1, 10)) is not None:
            det.pvp_yield = y
        bases = s.get("bases")
        for k, b in (bases.items() if isinstance(bases, dict) else ()):
            world, _, seed = k.rpartition("|")
            if world and isinstance(b, list) and len(b) == 2 and all(isinstance(c, (int, float)) for c in b):
                det._bases[(world, int(seed) if seed.lstrip("-").isascii() and seed.lstrip("-").isdigit() else None)] = (float(b[0]), float(b[1]))
    except (TypeError, ValueError, AttributeError):
        log.warning("ignoring malformed parts of %s", det.settings_path)
    _load_sync(det, s)


def _load_sync(det: Detector, s: dict) -> None:
    if isinstance(s.get("syncKey"), str) and KEY_RE.fullmatch(s["syncKey"]):
        det.sync_key = s["syncKey"]
        det.peers.key_state = "unknown"
    if isinstance(s.get("syncEnabled"), bool):
        det.sync_enabled = s["syncEnabled"]
    if isinstance(s.get("syncPositions"), bool):
        det.sync_positions = s["syncPositions"]
    if isinstance(s.get("shareCharacter"), bool):
        det.share_character = s["shareCharacter"]
    if isinstance(s.get("shareSession"), bool):
        det.share_session = s["shareSession"]
    if isinstance(s.get("pinLabel"), str) and s["pinLabel"].strip():
        det.pin_label = s["pinLabel"].strip()[:LABEL_MAX]
    hidden = s.get("syncHiddenMembers")
    if isinstance(hidden, list):
        det.sync_hidden_members = {m for m in hidden if isinstance(m, int) and not isinstance(m, bool)}


def save(det: Detector) -> None:
    try_write_json(det.settings_path, {
        "minimapHidden": sorted(det.minimap_hidden), "lines": det.lines_enabled,
        "minimapExtras": sorted(det.minimap_extras),
        "seenResources": [r.key for r in RESOURCES], "resource": det.resource_key,
        "lastPveRow": det.last_pve_row, "linesFromRow": det.lines_from_row, "pvpYield": det.pvp_yield, "pvpOnly": det.pvp_only, "maxKm": det.max_km,
        "bases": {f"{w[0]}|{w[1]}": list(b) for w, b in det._bases.items() if w and w[0]},
        "syncKey": det.sync_key, "syncEnabled": det.sync_enabled, "syncPositions": det.sync_positions,
        "syncHiddenMembers": sorted(det.sync_hidden_members),
        "shareCharacter": det.share_character, "shareSession": det.share_session, "pinLabel": det.pin_label,
    }, "settings")
