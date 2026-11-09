"""Body field rules shared by the guild sync routes: strict JSON shapes, named in the 400 reply."""

import math
import re

MAX_INT = 2**63 - 1  # SQLite INTEGER
PLACE = re.compile(r"[A-Za-z0-9_-]{1,64}")
ANY = re.compile(r".+", re.DOTALL)
TYPE = re.compile(r"[a-z0-9_]{1,64}")  # a sighting type, an object kind
IDENTITY_FIELDS = ("characterName", "characterId", "battlegroupId")


class BadField(Exception):
    """The first field that breaks the body rules, named as in the 400 reply (e.g. `records[0].cls`)."""

    def __init__(self, field: str) -> None:
        super().__init__(field)
        self.field = field


def join(parent: str, name: str) -> str:
    return f"{parent}.{name}" if parent else name


def obj(value: object, field: str, keys: tuple[str, ...]) -> dict:
    """A JSON object with exactly `keys`: a missing or an unknown key is refused."""
    if not isinstance(value, dict):
        raise BadField(field or "body")
    for key in (*keys, *value):
        if key not in keys or key not in value:
            raise BadField(join(field, str(key)))
    return value


def integer(value: object, field: str, low: int) -> int:
    if type(value) is not int or not low <= value <= MAX_INT:
        raise BadField(field)
    return value


def flag(value: object, field: str) -> bool:
    """A JSON true or false: 0, 1 and "true" are refused."""
    if type(value) is not bool:
        raise BadField(field)
    return value


def number(value: object, field: str, low: float = -math.inf, high: float = math.inf) -> float:
    """A finite number in [low, high]; JSON's NaN, Infinity and ints too big for a float are refused."""
    if type(value) not in (int, float):
        raise BadField(field)
    try:
        n = float(value)
    except OverflowError:
        raise BadField(field) from None
    if not (math.isfinite(n) and low <= n <= high):
        raise BadField(field)
    return n


def text(value: object, field: str, pattern: re.Pattern, max_len: int) -> str:
    if not isinstance(value, str) or len(value) > max_len or not pattern.fullmatch(value):
        raise BadField(field)
    return value


def identity(value: object) -> dict:
    i = obj(value, "identity", IDENTITY_FIELDS)
    return {
        "characterName": text(i["characterName"], "identity.characterName", ANY, 64),
        "characterId": integer(i["characterId"], "identity.characterId", 1),
        "battlegroupId": text(i["battlegroupId"], "identity.battlegroupId", PLACE, 64),
    }
