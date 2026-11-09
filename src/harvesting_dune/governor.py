# Governor: keeps the detector light (priority, memory, pruning) and runs periodic upkeep.
"""Self-management of the detector's own footprint while the game runs.

* Process policy (once): below-normal CPU priority, and Windows EcoQoS so the scheduler
  prefers efficiency cores and lower clocks for us. The game always wins contention.
* Periodic (every CHECK_S):
    - lean mode when system memory is short or our own working set grows: stop journaling
      actors, drop rebuildable caches, collect garbage;
    - forget long-unseen actors (the store would otherwise grow all session);
    - cap the journal folder size, oldest files first.
"""

from __future__ import annotations

import ctypes
import gc
import logging
import sys
from ctypes import wintypes
from pathlib import Path

from .store import REMEMBER_SPICE_S

log = logging.getLogger(__name__)

CHECK_S = 15.0
LEAN_ENTER_FREE_MB = 1200  # system free RAM below this: go lean
LEAN_EXIT_FREE_MB = 2000  # hysteresis
OWN_SOFT_LIMIT_MB = 400
JOURNAL_CAP_MB = 300
FORGET_AFTER_S = 20 * 60  # unseen, out-of-range actors


class _MemStatus(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _Counters(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class _PowerThrottling(ctypes.Structure):
    _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]


def _k32():
    """kernel32 with explicit handle types: undeclared, a 64-bit pseudo-handle gets truncated."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Counters), wintypes.DWORD]
    k32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
    k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.SetPriorityClass.restype = wintypes.BOOL
    k32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.SetProcessInformation.restype = wintypes.BOOL
    k32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MemStatus)]
    k32.GlobalMemoryStatusEx.restype = wintypes.BOOL
    return k32


def system_free_mb() -> float | None:
    if sys.platform != "win32":
        return None
    st = _MemStatus()
    st.dwLength = ctypes.sizeof(st)
    if not _k32().GlobalMemoryStatusEx(ctypes.byref(st)):
        return None
    return st.ullAvailPhys / 2**20


def own_working_set_mb() -> float | None:
    if sys.platform != "win32":
        return None
    c = _Counters()
    c.cb = ctypes.sizeof(c)
    k32 = _k32()
    if not k32.K32GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
        return None
    return c.WorkingSetSize / 2**20


def apply_process_policy() -> list[str]:
    """Below-normal priority + EcoQoS. Returns what was applied (for the log)."""
    if sys.platform != "win32":
        return []
    k32 = _k32()
    proc = k32.GetCurrentProcess()
    applied = []
    if k32.SetPriorityClass(proc, 0x00004000):  # BELOW_NORMAL_PRIORITY_CLASS
        applied.append("below-normal priority")
    state = _PowerThrottling(1, 0x1, 0x1)  # PROCESS_POWER_THROTTLING_EXECUTION_SPEED on
    if k32.SetProcessInformation(proc, 4, ctypes.byref(state), ctypes.sizeof(state)):  # ProcessPowerThrottling
        applied.append("EcoQoS (efficiency cores)")
    return applied


def cap_directory(path: Path, cap_mb: float, keep: set[Path] | frozenset = frozenset()) -> int:
    """Delete oldest files until the folder is under cap. Returns files removed."""
    if not path.is_dir():
        return 0
    files = sorted((f for f in path.iterdir() if f.is_file()), key=lambda f: f.stat().st_mtime)
    total = sum(f.stat().st_size for f in files) / 2**20
    removed = 0
    for f in files:
        if total <= cap_mb:
            break
        if f in keep:
            continue
        size = f.stat().st_size / 2**20
        try:
            f.unlink()
        except OSError as exc:
            log.warning("could not delete old journal %s: %s", f.name, exc)
            continue
        total -= size
        removed += 1
    return removed


class Governor:
    def __init__(self, det, journal_dir: Path | None) -> None:
        self.det = det
        self.journal_dir = journal_dir
        self.lean = False
        self._last = -1e18
        self.free_mb: float | None = None
        self.own_mb: float | None = None

    def start(self) -> None:
        applied = apply_process_policy()
        if applied:
            log.info("process policy: %s", ", ".join(applied))

    def tick(self, now: float) -> None:
        if now - self._last < CHECK_S:
            return
        self._last = now
        self.free_mb, self.own_mb = system_free_mb(), own_working_set_mb()
        short = self.free_mb is not None and self.free_mb < LEAN_ENTER_FREE_MB
        heavy = self.own_mb is not None and self.own_mb > OWN_SOFT_LIMIT_MB
        recovered = self.free_mb is None or self.free_mb > LEAN_EXIT_FREE_MB
        if not self.lean and (short or heavy):
            self.lean = True
            log.warning("lean mode on (free RAM %s MB, detector %s MB)", _mb(self.free_mb), _mb(self.own_mb))
        elif self.lean and recovered and not heavy:
            self.lean = False
            log.info("lean mode off (free RAM %s MB)", _mb(self.free_mb))
        self.det.lean = self.lean

        forgotten = self.det.store.prune(now, FORGET_AFTER_S, REMEMBER_SPICE_S)
        self.det.save_ranges()
        self.det.save_terrain(now)
        if self.det.scan.expire(now) | self.det.expire_mined(now):  # both run
            self.det.changed()  # respawned rows are candidates again; the page reloads its scan layer
        if self.lean:
            self.det.scan.drop_caches()
            gc.collect()
        if self.journal_dir is not None:
            current = {Path(self.det.journal_path)} if self.det.journal_path else set()
            removed = cap_directory(self.journal_dir, JOURNAL_CAP_MB, current)
            if removed:
                log.info("journal folder over %s MB: removed %s oldest file(s)", JOURNAL_CAP_MB, removed)
        if forgotten:
            log.debug("forgot %s long-unseen actors", forgotten)

    def status(self) -> dict:
        return {"lean": self.lean, "freeMb": _mb(self.free_mb), "ownMb": _mb(self.own_mb)}


def _mb(v: float | None) -> int | None:
    return None if v is None else round(v)
