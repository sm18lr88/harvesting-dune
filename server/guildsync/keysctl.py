"""Admin CLI for guild sync keys: issue, list, revoke, renew. Run it over SSH on the server host.

It opens the server's SQLite file directly (WAL makes that safe next to the running
server) and never uses the network. Exit codes: 0 ok, 2 usage error or unknown key,
3 database unavailable, 4 the regular-key cap is full (keystore.MAX_ACTIVE_REGULAR).
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from collections.abc import Callable

from .keystore import MAX_ACTIVE_REGULAR, KeyCapFull
from .storage import Storage

OK, USAGE, DB_UNAVAILABLE, CAP_FULL = 0, 2, 3, 4


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="keysctl", description="Manage guild sync keys.")
    p.add_argument("--db", default="/data/guildsync.sqlite", help="the server's SQLite file (default: %(default)s)")
    sub = p.add_subparsers(dest="cmd", required=True)
    issue = sub.add_parser("issue", help="create a key and print its token once")
    issue.add_argument("--label", required=True, help="a nickname for the key's player, e.g. their main character; never a real name")
    issue.add_argument("--note")
    issue.add_argument("--admin", action="store_true", help="an admin key: outside the regular-key cap, never expires")
    issue.add_argument("--json", action="store_true", help='print {"keyId", "token", "label", "role"}')
    listing = sub.add_parser("list", help="every key with its state and the characters seen through it")
    listing.add_argument("--json", action="store_true")
    revoke = sub.add_parser("revoke", help="stop a key; it fails on its next request")
    revoke.add_argument("key_id", metavar="keyId")
    renew = sub.add_parser("renew", help="make an expired key work again (counts against the cap)")
    renew.add_argument("key_id", metavar="keyId")
    return p


def _cap_full(active: int, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"error": "key cap reached", "active": active, "cap": MAX_ACTIVE_REGULAR}))
    print(f"keysctl: {active}/{MAX_ACTIVE_REGULAR} regular keys are active; revoke one first", file=sys.stderr)
    return CAP_FULL


def _issue(storage: Storage, args: argparse.Namespace, now: float) -> int:
    role = "admin" if args.admin else "regular"
    try:
        key_id, token = storage.issue_key(args.label, args.note, role)
    except KeyCapFull as full:
        return _cap_full(full.active, args.json)
    print(json.dumps({"keyId": key_id, "token": token, "label": args.label, "role": role}) if args.json else token)
    return OK


def _when(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts)) if ts is not None else "-"


def _list(storage: Storage, args: argparse.Namespace, now: float) -> int:
    keys = [
        {"keyId": k["id"], "label": k["label"], "note": k["note"], "role": k["role"], "state": k["state"],
         "created": k["created_at"], "revoked": k["revoked_at"], "expired": k["expired_at"], "lastSeen": k["last_seen"],
         "characters": [f"{c['name']}@{c['battlegroup']}" for c in k["characters"]]}
        for k in storage.list_keys()
    ]
    summary = f"active regular keys: {storage.active_regular(now)}/{MAX_ACTIVE_REGULAR}"
    if args.json:
        print(json.dumps(keys, indent=1))
        print(summary, file=sys.stderr)
        return OK
    rows = [("keyId", "label", "role", "state", "created", "revoked", "lastSeen", "characters")]
    rows += [(k["keyId"], k["label"], k["role"], k["state"], _when(k["created"]), _when(k["revoked"]), _when(k["lastSeen"]),
              ", ".join(k["characters"]) or "-") for k in keys]
    widths = [max(len(r[n]) for r in rows) for n in range(7)]
    for r in rows:
        print("  ".join(cell.ljust(w) for cell, w in zip(r, widths, strict=False)) + "  " + r[7])
    print(summary)
    return OK


def _revoke(storage: Storage, args: argparse.Namespace, now: float) -> int:
    match storage.revoke_key(args.key_id, now):
        case "revoked":
            print(f"revoked {args.key_id}")
            return OK
        case "already":
            print(f"keysctl: {args.key_id} was already revoked", file=sys.stderr)
            return OK
        case "unknown":
            print(f"keysctl: no key {args.key_id}", file=sys.stderr)
            return USAGE
        case other:
            raise AssertionError(other)


def _renew(storage: Storage, args: argparse.Namespace, now: float) -> int:
    try:
        result = storage.renew_key(args.key_id, now)
    except KeyCapFull as full:
        return _cap_full(full.active, False)
    match result:
        case "renewed":
            print(f"renewed {args.key_id}")
            return OK
        case "active":
            print(f"keysctl: {args.key_id} is active; nothing to renew", file=sys.stderr)
            return OK
        case "revoked":
            print(f"keysctl: {args.key_id} is revoked; issue a new key instead", file=sys.stderr)
            return USAGE
        case "unknown":
            print(f"keysctl: no key {args.key_id}", file=sys.stderr)
            return USAGE
        case other:
            raise AssertionError(other)


COMMANDS = {"issue": _issue, "list": _list, "revoke": _revoke, "renew": _renew}


def main(argv: list[str] | None = None, clock: Callable[[], float] = time.time) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.cmd == "issue" and not args.label.strip():
        parser.error("--label must not be empty")
    if not os.path.isfile(args.db):  # never create a stray empty database from a typo
        print(f"keysctl: database not found: {args.db}", file=sys.stderr)
        return DB_UNAVAILABLE
    try:
        storage = Storage.open(args.db, clock)
    except sqlite3.Error as exc:
        print(f"keysctl: database unavailable: {exc}", file=sys.stderr)
        return DB_UNAVAILABLE
    try:
        return COMMANDS[args.cmd](storage, args, clock())
    except sqlite3.Error as exc:
        print(f"keysctl: database unavailable: {exc}", file=sys.stderr)
        return DB_UNAVAILABLE
    finally:
        storage.close()


if __name__ == "__main__":
    sys.exit(main())
