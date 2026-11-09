# Profile: the character profile shared with the guild, reduced from the companion's own topics.
"""Only what the companion already shows on its own pages, cut down the way its pages cut it
(the skills page's reduction of dune.skillProgress, for instance). Never currencies (Solari), session
history, base ids or anything from dune.bases: those topics are not read here and the session part
copies named fields only.

Every part is None until its topic has arrived: the companion may not publish a topic to a
subscriber that is not one of its own pages (unverified), and the page then says "not reported yet".
Payloads are untrusted: a value of the wrong type is dropped, never raised on.
"""

from __future__ import annotations

import hashlib
import json
import math

SKILLS, RECIPES, ACHIEVEMENTS, LANDSRAAD = "dune.skillProgress", "dune.knownRecipes", "dune.achievements", "dune.landsraad"
STATS, SESSION = "dune.rawStats", "dune.trackedSession"
TOPICS = (SKILLS, RECIPES, ACHIEVEMENTS, LANDSRAAD, STATS, SESSION)
MAX_BYTES = 32768  # the server refuses bigger profiles (docs/SYNC-PROTOCOL.md)
MAX_TEXT, MAX_LIST = 120, 400
SHED = ("stats", "achievements", "recipes", "session", "skills", "landsraad")  # dropped in this order when too big


def _text(v: object) -> str | None:
    return v[:MAX_TEXT] if isinstance(v, str) and v else None


def _num(v: object) -> float | int | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return v if isinstance(v, int) and abs(v) < 2**53 else round(float(v), 2)


def _value(v: object) -> float | int | str | None:
    n = _num(v)
    return n if n is not None else _text(v)


def _rows(v: object) -> list:
    return v[:MAX_LIST] if isinstance(v, list) else []


def _names(v: object) -> list[str]:
    """Ability or technique names, whether the companion sends strings or objects."""
    out = []
    for item in _rows(v):
        name = _text(item) or (_text(item.get("name")) or _text(item.get("id")) or _text(item.get("tag")) if isinstance(item, dict) else None)
        if name:
            out.append(name)
    return out


def skills(p: object) -> dict | None:
    if not isinstance(p, dict):
        return None
    spent = p.get("spentByTag")
    if isinstance(spent, dict):
        rows = [[t, n] for t, v in spent.items() if _text(t) and (n := _num(v))]
    else:  # already in the skills page's shape
        rows = [[r[0], r[1]] for r in _rows(p.get("skills")) if isinstance(r, list) and len(r) == 2 and _text(r[0]) and _num(r[1])]
    single = p.get("isSinglePlayer")
    mode = _text(p.get("gameMode")) or ({True: "solo", False: "online"}.get(single) if isinstance(single, bool) else None)
    return {"gameMode": mode, "skills": sorted(rows)[:MAX_LIST],
            "abilities": _names(p.get("abilities")), "techniques": _names(p.get("techniques"))}


def stats(p: object) -> list | None:
    if not isinstance(p, dict) or not isinstance(p.get("groups"), list):
        return None
    out = []
    for g in _rows(p["groups"]):
        name = isinstance(g, dict) and (_text(g.get("name")) or _text(g.get("title")))
        if name:
            entries = [[n, v] for e in _rows(g.get("entries")) if isinstance(e, dict)
                       and (n := _text(e.get("name"))) and (v := _value(e.get("value"))) is not None]
            out.append({"name": name, "entries": entries})
    return out


def recipes(p: object) -> list | None:
    if not isinstance(p, dict) or not isinstance(p.get("recipes"), list):
        return None
    found = {(rid, _num(r.get("grade"))) for r in p["recipes"] if isinstance(r, dict) and (rid := _text(r.get("recipeId")))}
    return [[rid, grade] for rid, grade in sorted(found, key=lambda t: (t[0], t[1] or 0))][:1000]


def landsraad(p: object) -> dict | None:
    if not isinstance(p, dict) or not isinstance(p.get("houses"), list):
        return None
    houses = []
    for h in _rows(p["houses"]):
        if not isinstance(h, dict) or not _text(h.get("houseId")):
            continue
        rewards = [r for r in _rows(h.get("currentRewards")) if isinstance(r, dict)]
        houses.append({"house": _text(h["houseId"]), "specialization": _text(h.get("specializationId")),
                       "progress": _num(h.get("progressValue")), "max": _num(h.get("progressMaximum")),
                       "earned": sum(1 for r in rewards if r.get("isEarned") is True), "rewards": len(rewards)})
    return {"term": _text(p.get("termLabel")), "houses": houses}


def achievements(p: object) -> dict | None:
    if not isinstance(p, dict) or not isinstance(p.get("achievements"), list):
        return None
    rows = [a for a in p["achievements"] if isinstance(a, dict) and _text(a.get("id"))]
    done = sorted(_text(a["id"]) for a in rows if a.get("isCompleted") is True)
    going = sorted([_text(a["id"]), prog] for a in rows if a.get("isCompleted") is not True
                   and (prog := _num(a.get("completionProgress"))) and prog > 0)
    return {"completed": done[:MAX_LIST], "progress": going[:MAX_LIST], "seen": len(rows)}


def _amounts(v: object) -> list:
    return [[n, a] for r in _rows(v) if isinstance(r, dict)
            and (n := _text(r.get("name")) or _text(r.get("id"))) and (a := _num(r.get("amount"))) is not None]


def session(p: object) -> dict | None:
    """This session's earnings, never its currencies."""
    if not isinstance(p, dict):
        return None
    runs = p.get("dungeonRuns")
    return {"characterXp": _num(p.get("characterXp")), "specializationXp": _amounts(p.get("specializationXp")),
            "dailies": [n for d in _rows(p.get("dailies")) if isinstance(d, dict) and (n := _text(d.get("dailyName")))],
            "dungeonRuns": len(runs) if isinstance(runs, list) else None, "loot": _amounts(p.get("loot"))}


# -- a guildmate's profile as received: each part keeps its documented shape, or becomes None -----
def _dict(v: object) -> dict | None:
    return v if isinstance(v, dict) else None


def _texts(v: object) -> list[str]:
    return [s[:MAX_TEXT] for s in _rows(v) if isinstance(s, str)]


def _pairs(v: object, value=_num) -> list:
    return [[r[0][:MAX_TEXT], x] for r in _rows(v)
            if isinstance(r, list) and len(r) == 2 and isinstance(r[0], str) and (x := value(r[1])) is not None]


def _got_status(d: dict) -> dict:
    return {"worldId": _text(d.get("worldId")), "worldName": _text(d.get("worldName")), "activity": _text(d.get("activity")),
            "inGame": d["inGame"] if isinstance(d.get("inGame"), bool) else None}


def _got_skills(d: dict) -> dict:
    return {"gameMode": _text(d.get("gameMode")), "skills": _pairs(d.get("skills")),
            "abilities": _texts(d.get("abilities")), "techniques": _texts(d.get("techniques"))}


def _got_landsraad(d: dict) -> dict:
    return {"term": _text(d.get("term")), "houses": [
        {"house": name, "specialization": _text(h.get("specialization")), "progress": _num(h.get("progress")),
         "max": _num(h.get("max")), "earned": _num(h.get("earned")) or 0, "rewards": _num(h.get("rewards")) or 0}
        for h in _rows(d.get("houses")) if isinstance(h, dict) and (name := _text(h.get("house")))]}


def _got_achievements(d: dict) -> dict:
    return {"completed": _texts(d.get("completed")), "progress": _pairs(d.get("progress")), "seen": _num(d.get("seen")) or 0}


def _got_session(d: dict) -> dict:
    return {"characterXp": _num(d.get("characterXp")), "specializationXp": _pairs(d.get("specializationXp")),
            "dailies": _texts(d.get("dailies")), "dungeonRuns": _num(d.get("dungeonRuns")), "loot": _pairs(d.get("loot"))}


def received(p: dict) -> dict:
    """A profile from the server, checked part by part: the settings page then renders known shapes only."""
    stats = p.get("stats")
    recipes = p.get("recipes")
    out = {key: (fn(d) if (d := _dict(p.get(key))) is not None else None) for key, fn in (
        ("status", _got_status), ("skills", _got_skills), ("landsraad", _got_landsraad),
        ("achievements", _got_achievements), ("session", _got_session))}
    out["stats"] = [{"name": name, "entries": _pairs(g.get("entries"), _value)} for g in _rows(stats)
                    if isinstance(g, dict) and (name := _text(g.get("name")))] if isinstance(stats, list) else None
    out["recipes"] = [[r[0][:MAX_TEXT], _num(r[1])] for r in _rows(recipes)
                      if isinstance(r, list) and len(r) == 2 and isinstance(r[0], str)] if isinstance(recipes, list) else None
    return out


def size(profile: dict) -> int:
    return len(canonical(profile))


def canonical(profile: dict) -> bytes:
    return json.dumps(profile, separators=(",", ":"), sort_keys=True).encode()


def digest(profile: dict | None) -> str:
    return "deleted" if profile is None else hashlib.blake2b(canonical(profile), digest_size=12).hexdigest()


def build(topics: dict, status: dict, with_session: bool) -> dict:
    """The profile from the latest topic payloads; parts that would push it over MAX_BYTES are left out."""
    out = {"status": status, "skills": skills(topics.get(SKILLS)), "stats": stats(topics.get(STATS)),
           "recipes": recipes(topics.get(RECIPES)), "landsraad": landsraad(topics.get(LANDSRAAD)),
           "achievements": achievements(topics.get(ACHIEVEMENTS)),
           "session": session(topics.get(SESSION)) if with_session else None}
    for part in SHED:
        if size(out) <= MAX_BYTES:
            break
        out[part] = None
    return out
