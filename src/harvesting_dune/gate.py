# Gate: the detector works only with a guild key the server accepted within the last 72 hours.
"""A mixin of app.Detector, with no I/O. The live run sets `gate_required`; replay and the tests that
build a bare Detector leave it off.

The key is checked by the server, never here: a successful sync or dataset list call is a check
(`key_checked`), a 401 is a refusal (`key_refused`, the map clears at once). Between checks the
detector keeps working for GRACE_S with the data it already has, so a server outage does not stop
everyone. Without a key, with a refused key, or after the grace time it draws nothing, ranks nothing
and fetches no dataset. The companion keeps working either way.
"""

from __future__ import annotations

import logging
import time

from .itemsources import REGION_NAMES, parse_regions
from .routes import parse_routes

log = logging.getLogger(__name__)

GRACE_S = 72 * 3600.0
REFUSED = ("invalid", "expired")  # key states the server set with a 401: no request until a new key
TEXT = {
    "no-key": "Enter your guild key to use the detector.",
    "rejected": "Key rejected: ask the admin for a new key.",
    "expired": "Key expired: ask the admin to renew it.",
    "checking": "Checking the key...",
}


def gate_reason(key_set: bool, key_state: str, checked_at: float | None, now: float) -> str | None:
    """Why the detector is closed, or None when it may work."""
    if not key_set:
        return "no-key"
    if key_state in REFUSED:
        return "rejected" if key_state == "invalid" else "expired"
    if checked_at is None or now - checked_at > GRACE_S:
        return "unreachable"
    return None


class GateMixin:
    def _gate_setup(self) -> None:
        self.gate_required = False
        self.key_checked_at: float | None = None  # wall time of the last call the server accepted
        self.key_error: str | None = None  # why the last key check failed (unreachable, tls, ...)
        self.gate_clock = time.time
        self._gate_was = True

    def gate_reason(self, now: float | None = None) -> str | None:
        if not self.gate_required:
            return None
        now = self.gate_clock() if now is None else now
        reason = gate_reason(self.sync_key is not None, self.peers.key_state, self.key_checked_at, now)
        if reason == "unreachable" and self.key_checked_at is None and self.key_error is None:
            return "checking"  # a new key, not answered yet
        return reason

    def gate_open(self, now: float | None = None) -> bool:
        return self.gate_reason(now) is None

    def gate_text(self, reason: str | None, now: float) -> str | None:
        if reason != "unreachable":
            return TEXT.get(reason) if reason else None
        if self.key_checked_at is None:
            return "Server unreachable: the key cannot be checked."
        return f"Server unreachable for {int((now - self.key_checked_at) // 3600)} h: ask the admin."

    def gate_snapshot(self) -> dict:
        now = self.gate_clock()
        reason = self.gate_reason(now)
        return {"open": reason is None, "reason": reason, "text": self.gate_text(reason, now)}

    def gate_poll(self, now: float | None = None) -> bool:
        """Note an opened or closed gate (a check, a refusal, the grace time running out); True if
        it changed. A closed gate drops the ranking, so nothing old shows when it opens again."""
        now = self.gate_clock() if now is None else now
        is_open = self.gate_open(now)
        if is_open == self._gate_was:
            return False
        self._gate_was = is_open
        if not is_open:
            self.areas, self.hotspot = [], None
        log.info("detector %s (%s)", "on" if is_open else "off", self.gate_reason(now) or "key ok")
        self._changed()
        return True

    def key_checked(self, now: float) -> None:
        self.peers.key_state, self.key_checked_at, self.key_error = "valid", now, None
        self.gate_poll(now)

    def key_refused(self, state: str) -> None:
        """A 401: `state` is "invalid" or "expired". The map clears on the next push."""
        self.peers.key_state, self.key_checked_at = state, None
        self.gate_poll()
        self.publish_viewer()

    def key_unchecked(self, reason: str) -> None:
        self.key_error = reason
        self.gate_poll()

    def on_dataset(self, name: str, data: object) -> bool:
        """A dataset from the server (or its cached copy); False, changing nothing, for a bad one."""
        match name:
            case "routes":
                routes = parse_routes(data)
                if not routes:
                    return False
                self.routes = routes
            case "regions":
                names = parse_regions(data)
                if names is None:
                    return False
                self.items.region_names = REGION_NAMES | names
            case _:
                return False
        log.info("dataset %s loaded", name)
        self._changed()
        return True
