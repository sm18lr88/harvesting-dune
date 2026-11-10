# Controls: settings-page, picker and hotkey actions, and the minimap resource filter.
"""User actions and the minimap filter (a mixin of app.Detector; it shares the detector's state).

Rule: with lines on, the minimap shows exactly the searched resource (ticking another one
switches the search). Every accepted settings-page choice is saved through settings.save; the
vehicle override and the on/off switch last only for this run, blocked cells are saved with the
terrain, and pins are memory-only.
"""

from __future__ import annotations

import math

from . import settings
from .catalog import BY_KEY, EXTRA_BY_KEY, GROUP_OF, MINIMAP_EXTRAS, MINIMAP_RESOURCES, RESOURCES
from .objects import LABEL_MAX
from .vehicle import VEHICLE_CHOICES
from .zones import ROWS


def _finite(value: str | None, default: str = "") -> float | None:
    try:
        v = float(value or default)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


class ControlsMixin:
    # -- minimap filter ------------------------------------------------------------------------
    def minimap_filter(self) -> tuple[list[str], list[str]]:
        """(marker types to hide on the minimap, every marker type we manage).

        Extras (buried treasure...) are hidden unless ticked.
        """
        managed = sorted({m for r in MINIMAP_RESOURCES for m in r.marker_types} | {m for e in MINIMAP_EXTRAS for m in e.marker_types})
        extras_hidden = {m for e in MINIMAP_EXTRAS if e.key not in self.minimap_extras for m in e.marker_types}
        hidden = {m for k in self.minimap_hidden for m in BY_KEY[k].marker_types}
        return sorted(hidden | extras_hidden), managed

    @staticmethod
    def family(key: str) -> str:
        variant = BY_KEY[key].variant_of if key in BY_KEY else None
        return variant or ("spice" if key.startswith("spice") else "cages" if key.startswith("cages") else key)

    def _known_resource(self, key: str | None) -> bool:
        return key in BY_KEY or self.items.is_item(key)

    def visible_resources(self) -> list[str]:
        """Resource families the minimap shows."""
        return [r.key for r in MINIMAP_RESOURCES if r.key not in self.minimap_hidden]

    @property
    def lines_active(self) -> bool:
        """Lines are drawn only when the minimap shows exactly one resource: never a tangle.
        A looted material has no companion markers: its lines need the map resources all hidden."""
        visible = len(self.visible_resources())
        return self.lines_enabled and (visible == 0 if self._minimap_steps_aside() else visible == 1)

    def _minimap_steps_aside(self) -> bool:
        """Searches whose markers the detector does not manage (materials, downed ships): lines need
        the map resources all hidden."""
        fam = BY_KEY.get(self.family(self.resource_key))
        return self.items.is_item(self.resource_key) or (fam is not None and not fam.minimap_managed)

    def _follow_single_visible(self) -> None:
        """With lines on and one resource shown, search for that resource (keeps a spice size mode)."""
        visible = self.visible_resources()
        fam = BY_KEY.get(self.family(self.resource_key))
        if fam is not None and not fam.minimap_managed:
            return  # a downed-ship search keeps its own view
        if self.lines_enabled and len(visible) == 1 and self.family(self.resource_key) != visible[0]:
            self.resource_key = visible[0]

    def _show_only(self, key: str) -> None:
        """Lines need exactly one resource: the minimap shows only the searched resource's family."""
        fam = self.family(key)
        hidden = {r.key for r in MINIMAP_RESOURCES if r.key != fam}
        if hidden != self.minimap_hidden:
            self.minimap_hidden = hidden
            self.minimap_rev += 1

    def minimap_panel(self) -> dict:
        items = [[r.key, r.name, r.key not in self.minimap_hidden, GROUP_OF.get(r.key, "")] for r in MINIMAP_RESOURCES]
        extras = [[e.key, e.name, e.key in self.minimap_extras] for e in MINIMAP_EXTRAS]
        return {"items": items, "extras": extras, "lines": self.lines_enabled,
                "linesActive": self.lines_active, "visibleCount": len(self.visible_resources())}

    # -- actions ---------------------------------------------------------------------------------
    def ui(self, action: str, value: str | None = None) -> bool:
        """False when the action is unknown or refused its value: the settings page must say so."""
        if action == "minimap_extra" and value in EXTRA_BY_KEY:
            self.minimap_extras ^= {value}  # minimap only: the search and lines are unaffected
            settings.save(self)
            self.minimap_rev += 1
            self.publish_rev += 1
            return True
        if action in ("minimap_toggle", "lines"):
            return self._minimap_action(action, value)
        if action.startswith("route_"):  # farm route mode (farm.py): never touches the ranking
            return self.farm_action(action, value) is not False  # None: farm.py does not report a refusal yet
        if action in ("sync_pause", "sync_positions", "sync_member", "share_character", "share_session"):
            return self._sync_action(action, value)
        if action in ("object_pin", "object_delete", "pin_label"):
            return self._object_action(action, value)
        if action == "guild_view":  # the settings page opened a guild list: fresh data now
            if value == "characters":
                self.characters_opened()
            else:
                self.sync_wake()
            return True
        if not self._setting_action(action, value):
            return False
        self._restart_ranking()  # never show a recommendation computed for the previous choice
        return True

    def _minimap_action(self, action: str, value: str | None) -> bool:
        searched = self.resource_key
        if action == "minimap_toggle":
            if not self._known_resource(value):
                return False
            if self.items.is_item(value):
                # a looted material has no companion markers: selecting it means lines to its sources,
                # so the map resources step aside (the same exclusive view as with lines on)
                self.resource_key = value
                self._show_only(value)
            elif self.lines_enabled:
                # one resource at a time: ticking one selects it (a spice size mode is kept)
                if self.family(self.resource_key) != value:
                    self.resource_key = value
                self._show_only(value)
            else:
                self.minimap_hidden ^= {value}
                self._follow_single_visible()
            self.minimap_rev += 1
        else:  # lines
            self.lines_enabled = not self.lines_enabled
            if self.lines_enabled and not self.lines_active:  # an item or downed-ship search needs 0 shown
                self._show_only(self.resource_key)
            self._follow_single_visible()
        settings.save(self)
        self._restart_ranking(clear=self.resource_key != searched)  # another search: nothing of the old one stays
        return True

    def _sync_action(self, action: str, value: str | None) -> bool:
        """Guild sync switches; they never touch the ranking. The key has its own endpoint."""
        match action:
            case "sync_pause":
                if self.sync_enabled:
                    self.queue_farewell()
                self.sync_enabled = not self.sync_enabled
                self.sync_wake()
            case "sync_positions":
                if self.sync_positions:  # off: the ring goes from the guild's minimaps at once
                    self.queue_farewell()
                self.sync_positions = not self.sync_positions
            case "share_character":
                self.share_character = not self.share_character
                self.sharing_changed()
            case "share_session":
                self.share_session = not self.share_session
                self.sharing_changed()
            # sync_member: one guildmate on my minimap; int() raises past 4300 digits
            case _ if value is not None and value.isascii() and value.isdigit() and len(value) <= 18:
                self.sync_hidden_members ^= {int(value)}
            case _:
                return False
        settings.save(self)
        return True

    def _object_action(self, action: str, value: str | None) -> bool:
        """Pins: one publish path for the hotkey and the settings page; never touches the ranking."""
        text = (value or "").strip()[:LABEL_MAX]
        match action:
            case "object_pin":
                label = text or self.pin_label
                return bool(label) and self.publish_object("pin", {"label": label}) is not None
            case "object_delete":
                return self.delete_object(value or "")
            case _ if text:  # pin_label
                self.pin_label = text
                settings.save(self)
                return True
        return False

    def _setting_action(self, action: str, value: str | None) -> bool:
        """Apply one action; False if it was not recognised or its value was invalid."""
        keys = [r.key for r in RESOURCES]
        if action == "max_km":
            km = _finite(value, "0")
            if km is None:
                return False
            self.max_km = min(max(km, 0.0), 100.0)
        elif action == "pvp_only":
            self.pvp_only = not self.pvp_only
        elif action == "pve_row" and value in tuple(ROWS):
            self.last_pve_row = value
        elif action == "lines_row" and value in tuple(ROWS):
            self.lines_from_row = value
        elif action == "pvp_yield":
            y = _finite(value)
            if y is None:
                return False
            self.pvp_yield = min(max(y, 1.0), 10.0)
        elif action == "resource" and self._known_resource(value):
            self.resource_key = value
            self._resource_chosen()
        elif action in ("next_resource", "prev_resource"):
            self.resource_key = self._cycled_resource(keys, action == "next_resource")
            self._resource_chosen()
        elif action == "vehicle" and value in VEHICLE_CHOICES:
            self.vehicles.set_override(None if value == "auto" else value)
            return True
        elif action == "cycle_vehicle":
            cur = self.vehicles.override or "auto"
            nxt = VEHICLE_CHOICES[(VEHICLE_CHOICES.index(cur) + 1) % len(VEHICLE_CHOICES)]
            self.vehicles.set_override(None if nxt == "auto" else nxt)
            return True
        elif action == "toggle":
            self.enabled = not self.enabled
            return True
        elif action == "base" and self.store.pose:
            self._bases[self.store.world] = (self.store.pose.x, self.store.pose.y)
        elif action == "block" and self.store.pose:
            self.grid.block(self.store.pose.x, self.store.pose.y, self.plan_vehicle)
            return True
        else:
            return False
        settings.save(self)
        return True

    def _cycled_resource(self, keys: list[str], forward: bool) -> str:
        """A search outside the list (a material) re-enters it at the end it steps towards."""
        if self.resource_key not in keys:
            return keys[0] if forward else keys[-1]
        return keys[(keys.index(self.resource_key) + (1 if forward else -1)) % len(keys)]

    def _resource_chosen(self) -> None:
        if self.lines_enabled:
            self._show_only(self.resource_key)  # the minimap follows: lines never point at a hidden resource
