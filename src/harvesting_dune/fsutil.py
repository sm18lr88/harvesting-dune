# File utilities: crash-safe JSON writes and checked JSON reads.
"""Crash-safe JSON files.

The detector is stopped with a hard kill when the companion closes (or when Windows shuts
down), so a plain write_text can leave a half-written file that the next start ignores:
settings, stale corrections or learned terrain would silently reset. Writes go to a
temporary file that replaces the target in one step.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def write_json(path: Path, data: Any, **dump_kw: Any) -> None:
    """Atomically replace `path` with `data` as JSON. Raises OSError if it cannot."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf8") as fh:
            json.dump(data, fh, allow_nan=False, **dump_kw)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                # Windows: a virus scanner or indexer may hold the target open for a moment
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        tmp.unlink(missing_ok=True)


def _refuse_constant(name: str) -> float:
    raise ValueError(f"{name} is not a number a loader can use")


def _finite_float(text: str) -> float:
    f = float(text)
    if not math.isfinite(f):
        raise ValueError(f"{text} is out of range")
    return f


def _bounded_int(text: str) -> int:
    i = int(text)
    try:
        float(i)  # loaders turn numbers into floats; ids up to 2**63 stay far below this
    except OverflowError:
        raise ValueError(f"an integer of {len(text)} digits is out of range") from None
    return i


def parse_json(text: str) -> Any:
    """json.loads without the values that write_json never emits (NaN, Infinity, numbers
    beyond a float): loaders call float() on them, and a hand-edited file with one such
    value must be refused like a cut-off one, not stop the start or break a world."""
    return json.loads(text, parse_constant=_refuse_constant, parse_float=_finite_float, parse_int=_bounded_int)


def read_json(path: Path | None, expect: type | tuple[type, ...], default: Any = None, *, per_value: bool = False) -> Any:
    """`path` parsed as JSON if it exists and has the expected top-level type, else `default`.

    per_value=True is for a loader that checks every value itself and skips only the bad one
    (terrain cells): a number beyond a float then reaches that check instead of costing the
    whole file. NaN and Infinity are refused either way."""
    if path is None or not path.exists():
        return default
    try:
        text = path.read_text("utf8")
        data = json.loads(text, parse_constant=_refuse_constant) if per_value else parse_json(text)
    except (OSError, ValueError) as exc:
        log.warning("ignoring unreadable %s: %s", path, exc)
        return default
    if not isinstance(data, expect):
        log.warning("ignoring %s: unexpected content", path)
        return default
    return data


def try_write_json(path: Path | None, data: Any, what: str, **dump_kw: Any) -> bool:
    """write_json that logs instead of raising: losing one save must never stop the detector."""
    if path is None:
        return False
    try:
        write_json(path, data, **dump_kw)
        return True
    except (OSError, ValueError) as exc:
        log.warning("could not save %s to %s: %s", what, path, exc)
        return False
