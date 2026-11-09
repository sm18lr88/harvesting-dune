"""Problem details (RFC 9457) for every guild sync refusal; docs/SYNC-PROTOCOL.md "Errors".

A client branches on the status code and, where it needs more, on `type`. Only the refusals a
client may act on differently get their own type; the rest are `about:blank` with the status
phrase as title. `error` is the v1 body's machine text, kept as an extension member so a v1
reader of the old `{"error": ...}` shape keeps working.
"""

from http import HTTPStatus

MEDIA_TYPE = "application/problem+json"
BASE = "https://github.com/sm18lr88/harvesting-dune/blob/master/docs/SYNC-PROTOCOL.md#"
TYPES = {  # kind -> (title, v1 `error` text); the type URI is BASE + kind
    "invalid-field": ("Request body is invalid", "bad request"),
    "character-owned": ("Character belongs to another key", "character belongs to another key"),
    "rate-limited": ("Too many requests", "rate limited"),
    "invalid-key": ("Key is not valid", "invalid key"),
    "key-expired": ("Key expired", "invalid key"),  # same v1 text: a v1 reader treats both alike
    "object-cap": ("Too many live objects", "bad request"),
}
ERRORS = {  # v1 `error` text of the about:blank refusals
    401: "invalid key", 404: "not found", 405: "method not allowed", 413: "body too large",
    415: "unsupported media type",
}


def problem(status: int, kind: str | None = None, detail: str | None = None, **extra: object) -> dict:
    """The body of one refusal: `kind` is a key of TYPES, or None for about:blank."""
    if kind is None:
        body = {"type": "about:blank", "title": HTTPStatus(status).phrase, "status": status,
                "error": ERRORS.get(status, "bad request" if status < 500 else "server")}
    else:
        title, error = TYPES[kind]
        body = {"type": BASE + kind, "title": title, "status": status, "error": error}
    if detail is not None:
        body["detail"] = detail
    return {**body, **extra}


def invalid(field: str, detail: str | None = None) -> dict:
    return problem(400, "invalid-field", detail or f"{field} is missing, unknown or out of range", field=field)


OTHER_KEY = problem(403, "character-owned", "Another key already writes for this character.")
BAD_KEY = problem(401, "invalid-key", "The key is missing, unknown or revoked.")
EXPIRED_KEY = problem(401, "key-expired", "The key was not used for 30 days. Ask the admin to renew it.")
