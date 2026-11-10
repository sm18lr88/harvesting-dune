# Guild sync protocol (wire contract v1)

This document is the contract between the detector and the guild sync server. Both sides implement exactly this. The shared test file `tests/fixtures/sync-contract.json` holds one worked example for each rule below. The server tests and the client tests both load it.

The machine-readable form is `docs/sync-openapi.yaml` (OpenAPI 3.2.1). It is written in JSON syntax, which is also valid YAML, so the tests read it with the standard library. `tests/test_sync_openapi.py` fails when it and the server disagree on routes, fields or limits.

Guild sync is off until you enter a key. With sync off, the detector never contacts the server.

## Routes

The server listens on one address: `https://158.69.179.64:8443`. There is no domain name.

There are six routes.

| Route | Key needed | What it does |
|---|---|---|
| `GET /health` | No | Returns `{"ok": true, "now": <server unix seconds>, "version": "1"}`. `HEAD /health` returns the same headers without the body. |
| `POST /v1/sync` | Yes | One round trip. The client uploads what it saw. The server returns what the guild saw. |
| `POST /v1/profile` | Yes | Stores or deletes the caller's character profile. See "Characters". |
| `POST /v1/profiles` | Yes | Returns the battlegroup's character profiles that changed. See "Characters". |
| `GET /v1/data` | Yes | Lists the datasets the server holds. See "Datasets". `HEAD` works too. |
| `GET /v1/data/<name>` | Yes | Returns one dataset. See "Datasets". `HEAD` works too. |

Any other path returns 404. Another method on one of these paths returns 405, with an `Allow` header that names the methods the path takes.

The client checks the server certificate inside the TLS handshake. It uses `ssl.create_default_context(cafile=<private CA file>)`. The private CA file ships with the detector. The server certificate has the IP address `158.69.179.64` in its subject alternative name. The client never turns certificate checks off. The server can change its certificate without a new detector release, as long as the same CA signs it.

## Auth

Each player gets one key from the admin. The key looks like this: `gs1_<keyid>_<secret>`.

- `keyid` is 8 characters from `a-z` and `0-9`.
- `secret` is 32 URL-safe characters, made by `secrets.token_urlsafe(24)`.

Every sync request sends these headers:

| Header | Value |
|---|---|
| `Authorization` | `Bearer gs1_<keyid>_<secret>` |
| `Content-Type` | `application/json`. Any other type gets 415. |
| `X-Client-Version` | the harvesting-dune version. The server writes it in its audit log, so the admin can see which versions still call a route. |
| `Accept-Encoding` | `gzip` |
| `Content-Encoding` | `gzip`, only when the body is gzipped |

### Compression

- The client gzips a request body bigger than 1 KiB and says so with `Content-Encoding: gzip`. Smaller bodies go as plain JSON.
- The server inflates a gzip body. The 256 KB limit counts the inflated bytes. The server stops inflating at the limit, so a small "zip bomb" is refused with 413 and never fills memory.
- Any other `Content-Encoding` gets 415, with the header `Accept-Encoding: gzip`.
- When the request says `Accept-Encoding: gzip`, the server gzips a reply bigger than 1 KiB and sends `Content-Encoding: gzip`.
- Only the Python standard library does this (`gzip`, `zlib`).

The server never stores the secret. It stores `sha256(secret)` as hex. It compares hashes with `hmac.compare_digest`.

An unknown key and a revoked key get the same answer: 401 with type `#invalid-key`. A revoked key stops working on its next request. A 401 always carries `WWW-Authenticate: Bearer realm="guildsync"`.

### Key expiry

A key is `regular` or `admin`. A regular key expires when nobody used it for more than 30 days. "Used" means a request that the server accepted. A key that was never used counts from the day it was made. A renewed key counts from the renewal.

An expired key gets 401 with type `#key-expired`. It has the same `WWW-Authenticate` header and the same v1 `error` text (`invalid key`) as `#invalid-key`. Only the type tells them apart. The server checks expiry on every request. A sweep every minute also writes it down.

An admin key never expires.

At most 64 regular keys are active at one time. Active means not revoked and not expired. Admin keys do not count. The admin can renew an expired key with `keysctl renew`. A renewed key counts toward the 64 again.

The key goes only in the `Authorization` header. A request with the key in the URL gets 404, and the server never logs a query string.

### Who may do what

The admin issues every key with `keysctl` on the server. No route creates, lists, revokes or renews keys. A key's role (`regular` or `admin`) changes only expiry and the 64-key limit. It gives no other rights on any route.

A character belongs to the first unrevoked key that wrote it, with a sync or a profile upload. One key may play any number of characters (a player's alts on other Steam accounts of the same PC). The server checks this inside the same database transaction as the write, so two keys can never both win a new character. Revoking the key frees its characters.

| Operation | Who | Which objects | Which properties |
|---|---|---|---|
| `GET /health` | Anyone (10 requests a minute per IP) | None | Reads `ok`, `now`, `version` |
| `POST /v1/sync`, upload part | Any valid key | Writes only characters this key owns (else 403, nothing written). Sightings go to the battlegroup and layout of that character. | Writes only the fields in "Body". The server sets `keyId`, `observerKey`, `observers`, `rev`, `receivedAt` and `lag` itself. |
| `POST /v1/sync`, reply part | Any valid key | Reads everything of the battlegroup in `identity`: its presence, and its sightings in the named layout. Both guilds may read everything (owner decision). | Reads the fields in "Response". Never secrets, hashes, IPs or audit rows. |
| `POST /v1/profile` | Any valid key | Stores or deletes only the profile of a character this key owns (else 403, nothing written) | Writes only the seven profile keys |
| `GET /v1/data`, `GET /v1/data/<name>` | Any valid key | Reads every dataset the admin put on the server | Reads the whole file, its ETag and its size |
| `POST /v1/profiles` | Any valid key | Reads every profile of the battlegroup in `identity` | Reads `characterId`, `characterName`, `rev`, `ageS`, `profile`, `playerTag` |

Key labels (`keysctl issue --label`) are for the admin only. No answer to a player ever holds a label or anything made from it. `playerTag` is opaque: the same 8 hex characters for every character of one key, so a player's alts can be grouped. It is the start of HMAC-SHA256 of the key id, keyed with the stored secret hash, so it is not the key id and cannot be made from it.

The server cannot check that a `characterName` is true: the companion is the only source. A key can only write under character ids it owns, and the reply shows each row's `keyId`, so a false name is visible to the guild and the admin.

The key is secret. It never appears in a log line, a page, a snapshot, a bridge expression or an error text, on either side.

## Body

`POST /v1/sync` takes one JSON object:

```json
{
  "since": 0,
  "sentAt": 1794159999.0,
  "identity": {"characterName": "Paul Test", "characterId": 1001, "battlegroupId": "prd-srv-test-alpha-erg"},
  "partition": {"worldId": "deepdesert_1", "layout": "deepdesert_1_07"},
  "position": {"x": 120000.0, "y": -650000.0, "z": 1400.0, "observedAt": 1794159997.0},
  "records": [],
  "presence": {"nextS": 60, "inGame": true, "leaving": false}
}
```

| Field | Meaning |
|---|---|
| `since` | The `next` value from the last good response. 0 on the first request. |
| `sentAt` | The client clock when it sent the request, in unix seconds. |
| `identity` | The character from the companion. The client sends nothing until it knows `characterName` and `battlegroupId`. |
| `partition` | Where the player is. `layout` is the companion's map file id in the Deep Desert, for example `deepdesert_1_07`. In other worlds, `layout` is the same as `worldId`. |
| `position` | The player position, or `null`. Position sharing is on by default. A player can turn it off on the settings page. Then the client sends `null`. |
| `records` | Up to 200 sightings. See below. |
| `presence` | Optional. The heartbeat: see "Presence". A body without it comes from an older client; the server reads it as `{"nextS": 60, "inGame": true, "leaving": false}`. |
| `objects` | Optional. Up to 20 shared objects to create, replace or delete. See "Objects". |

All coordinates are world centimetres. All times are unix seconds.

### Field rules

The server checks every field before it writes anything. One bad field rejects the whole request with 400.

- Every number must be finite. `NaN` and `Infinity` are refused.
- `since` is an integer, 0 or more. `characterId` is an integer, 1 or more.
- `characterName`: 1-64 characters. `battlegroupId`, `worldId`, `layout`: 1-64 characters from `A-Z a-z 0-9 _ -`.
- `type`: 1-64 characters from `a-z 0-9 _`. `rowKey`: at most 128 characters, or `null`.
- `fill` is 0 to 1. `distCm` is 0 or more.
- `records`: at most 200.
- `presence`: an object with exactly `nextS` (an integer; the server clamps it to 5-600), `inGame` and `leaving` (JSON `true` or `false` only: `1` and `"true"` are refused). `null` is refused; leave the key out instead.
- Unknown fields are refused, so a typo never passes silently.

### Records

A record is one strong sighting:

```json
{"cls": "spice", "type": "spicefieldlarge", "x": 123456.0, "y": -654321.0, "z": 1500.0,
 "observedAt": 1794159969.0, "fill": 0.8, "size": "large", "rowKey": null, "distCm": null}
```

| `cls` | What it means |
|---|---|
| `spice` | A live medium or large spice field, with its fill. |
| `blow` | A spice blow. |
| `ship` | A downed ship. |
| `mined` | A node the player mined. |
| `stale` | A scan row that the player confirmed stale. `rowKey` is the scan row key `type:x:y`. `distCm` is the player's distance from the row. |
| `ore` | A live ore, crystal or stone node (`catalog.ORE_MARKERS`). `fill`, `size`, `rowKey` and `distCm` are `null`. |

`type` is the catalog marker, for example `spicefieldlarge`, `magnetiteore` or `salvagesteel`. `fill` is 0 to 1 or `null`. `size` is `small`, `medium`, `large` or `null`.

Only these strong classes travel. There is no "saw nothing here" class.

A live `ore` node goes out once. The client remembers the node it sent for each 20 m cell. It sends the cell again when another node is there, or when the node was gone and is live again. It also sends a node again when it was sent 20 minutes ago and is still live: that keeps the guild's ring from fading while the node is there (the ring fades over the respawn time, 30-45 minutes). A burst (for example after you enter a world) spreads over the next polls, 200 records at a time.

Never in a sync body: companion actor ids, currencies, loot, dailies, dungeon runs, skills, builds, the vehicle, or position history. Character data travels only in `/v1/profile`, and never holds currencies (see "Characters").

### Key rule

The server keeps one row per key:

`(battlegroupId, layout, cls, type, floor(x / 2000), floor(y / 2000))`

So each row covers one 20 m cell. A new record for the same key updates the row (an upsert):

- The newest observation wins. The server replaces the fields only when the new observation is the same age or newer on the server clock (`receivedAt - lag`).
- `observers` is the set of distinct key ids that reported the row. Every report adds its key id.
- `receivedAt` is the server time of the upsert.
- `lag = clamp(sentAt - observedAt, 0, 3600)`. Both times come from the same client clock, so clock skew cancels out.

### Rev cursor

The server has one global counter, `rev`. Each upsert adds 1 to it and stamps the row with the new value. The response gives `next`, the highest rev the server has assigned. The client sends it back as `since` next time. Then it gets only rows with `rev > since`.

The server handles the upload first, then reads. So the caller's own new rows come back in the same response.

One response holds at most 500 sightings, lowest rev first. When the server cuts the list, `next` is the rev of the last sighting it sent, or lower when the objects list was cut too (see "Objects"). The client then polls again at once, until a response is not cut.

## Response

A good request returns 200 and this object:

| Key | Content |
|---|---|
| `now` | The server clock, unix seconds. |
| `next` | The highest rev so far. When a list was cut (500 sightings or 200 objects), it is the lower of the cut ends instead. Send it as `since` next time. |
| `keyState` | `"valid"`. |
| `presence` | The other members of the same battlegroup that are not gone (see "Presence"), in any world. Never the caller. Each entry: `characterName`, `characterId`, `battlegroupId`, `worldId`, `layout`, `x`, `y`, `z`, `ageS`, `keyId`, `state`, `lastNextS`. `x`, `y` and `z` are `null` when that member turned position sharing off. |
| `sightings` | Rows with `rev > since` in the caller's partition only (same `battlegroupId` and same `layout`). Each entry: `cls`, `type`, `x`, `y`, `z`, `fill`, `size`, `rowKey`, `distCm`, `ageS`, `observerName`, `observerKey`, `observers` (key ids), `rev`, `worldId`, `layout`. The caller's own rows are included. |
| `objects` | Shared objects with `rev > since` in the caller's battlegroup, in any world. See "Objects". The caller's own objects are included. |

### Age

The client never compares its own clock with the server clock. It uses `ageS`:

- sightings and presence: `ageS = now - receivedAt + lag`

For presence, `lag` comes from `position.observedAt`. It is 0 when the position is `null`.

At merge, the client turns the age into feed clock time: `seen_at = det.clock - ageS`.

## Presence

Every accepted sync is a heartbeat. There are no extra requests. Each sync promises the next one within `nextS` seconds: the client's cadence (15, 60 or 180).

The server works out each member's state from its own clock only. Client clocks are never compared. Deadline = when the last sync arrived + `nextS`. Grace = 20 seconds.

| `state` | When | Minimap | Roster |
|---|---|---|---|
| `online` | Up to deadline + grace, and the last sync said `inGame: true` | A full ring (unless you hid that member) | "online" |
| `in_menu` | Up to deadline + grace, and the last sync said `inGame: false` (menu or loading screen) | No ring | "in menu" |
| `late` | Up to 2 x `nextS` after that (one or two missed syncs) | A faded ring at the last position | "last seen 2 min ago" |
| `offline` | Later, or at once after a goodbye (`leaving: true`) or a revoked key | No ring | Greyed, "left 4 min ago" |
| (gone) | 15 minutes after going offline | No ring | Not sent any more |

- A member turns `online` again only with a new sync, never because time passes.
- States come from stored values, so they survive a server restart.
- `lastNextS` is the member's last `nextS` (60 for an older client).
- A member in another world is still listed, with its world, but never drawn on your minimap.

The client says goodbye (`leaving: true`) when the detector stops (it waits at most 3 seconds and never holds the exit), when sync is paused, when position sharing is turned off (a normal sync follows at once, without a position), when the key is removed or changed, and when the character ends (see "Client"). A lost goodbye does no harm: the deadline rule covers it.

## Objects

A member can share small objects with the guild. The first kind is the pin: a ring with a short label on every guildmate's minimap. Objects travel inside `/v1/sync`. There is no extra route and no extra request.

The envelope is strict. The body is open: the server stores any small plain JSON value and never reads it. So a new kind of object needs no server change.

### Envelope

The request field `objects` is a list of at most 20 uploads. Each upload has exactly these six keys. Any other key gets 400.

| Key | Rule |
|---|---|
| `id` | 1-64 characters from `A-Z a-z 0-9 _ -`. The author picks it. It is unique per key, so two keys may use the same id. The same id again replaces the object. |
| `kind` | 1-64 characters from `a-z 0-9 _`, for example `pin`. The server keeps no list of kinds. |
| `scope` | `null` (the whole battlegroup) or `{"worldId": ..., "layout": ...}`, with the same rules as `partition`. |
| `geometry` | `null` (no position) or `{"type": "point", "x": ..., "y": ..., "z": ...}` in world centimetres. Numbers must be finite. `point` is the only type today. |
| `body` | Any plain JSON value, with the same rules as a profile value. As canonical JSON (sorted keys, no spaces) it is at most 512 bytes. A pin's body is `{"label": "IRON HERE"}`. |
| `ttlS` | An integer, 0 or more: seconds to live from when the server receives it. More than 86400 (24 hours) is cut to 86400. 0 deletes the object. |

The client never sends a clock time for an object. So client clocks never matter here.

### What the server adds

Each object in the reply has the six keys, with `ttlS` as seconds left, plus these:

| Key | Content |
|---|---|
| `characterId`, `characterName` | The character of the request that wrote it. |
| `playerTag` | The author's opaque tag, as in "Who may do what". The reader names an object by `playerTag` and `id`. |
| `rev` | A value of the shared rev counter (see "Cursor"). |
| `ageS` | Seconds since the server received it. |
| `ttlS` | Seconds left on the server clock. 0 or less means the object was deleted or has ended: drop it. |

The key id never goes on the wire. The server keeps it to know who owns each row.

### Lifetime and delete

- An object ends when its `ttlS` runs out. The client gives a pin 2 hours.
- `ttlS: 0` deletes an object. The row stays as a tombstone that ends now. It gets a new rev, so every reader learns it on its next sync.
- When the admin revokes a key, or a key expires after 30 idle days, all its live objects end at once. Each gets a new rev.
- The server deletes a row 24 hours after it ended.
- A key keeps at most 100 rows, live and ended. Beyond that, the server deletes the key's oldest ended rows. It never deletes a live row this way.

### Caps

| Cap | Value |
|---|---|
| Uploads in one request | 20 |
| Live objects per key | 20 |
| Live objects per battlegroup | 300 |
| Body | 512 bytes as canonical JSON |
| Lifetime | 24 hours (`ttlS` is cut to 86400) |

The server counts live objects inside the same transaction as the write. A new live object over a cap gets 400 with type `#object-cap`. Its `field` names the upload, for example `objects[0]`. Nothing of that request is written: no object, no presence, no sighting.

Replacing one of your live objects, or deleting one, always passes.

The client checks the caps itself before it stages an object. When the server refuses an upload anyway, the client drops the objects of the refused request and shows why on the settings page. After an `#object-cap` refusal it keeps its staged deletes, because a delete always passes. Then it syncs again at once.

### Cursor

Objects share the rev counter and the `since`/`next` cursor with sightings. One reply holds at most 200 objects, lowest rev first.

When the server cuts the objects list at 200, `next` is the lower of the two cut ends. So the next request misses nothing from either list. Then the client syncs again at once.

A row may come twice after a cut. The reader keys objects by `playerTag` and `id`, so a repeat changes nothing.

Objects are not filtered by world. A reply holds every object of the battlegroup, so a pin dropped in another map is already there when you arrive.

### Old clients and old servers

- An older detector does not know the `objects` reply key. It ignores it, like any key it does not know.
- An older server refuses `objects` in a body with 400, and then the whole sync fails. So the client sends `objects` only after a reply from this server carried the `objects` key.
- So the server is deployed first, then the detectors.

### Drawing

- The client draws an object only when its `scope` is `null` or equals the player's world and layout.
- It draws only `point` objects of kinds it knows. Today that is `pin`. It keeps other kinds and lists them, but does not draw them.
- Objects of a guildmate you hid on the roster are not drawn.
- Your own pins carry a second, smaller ring.
- A label is drawn as strokes from a fixed set of letters, digits and marks. Other characters are left out.
- Objects never change the ranking, the destination or the routes.

## Errors

Every answer with a status of 400 or more is a problem details object (RFC 9457), with `Content-Type: application/problem+json`:

```json
{"type": "https://github.com/sm18lr88/harvesting-dune/blob/master/docs/SYNC-PROTOCOL.md#invalid-field",
 "title": "Request body is invalid", "status": 400, "error": "bad request",
 "detail": "records[0].cls is missing, unknown or out of range", "field": "records[0].cls"}
```

- `status` is always the real status code.
- `type` is what a program checks. It is one of the types below, or `about:blank`. With `about:blank`, the status code says everything, and `title` is the status name (for example `Not Found`).
- `error` is the v1 text from before problem details. It stays, so an older reader keeps working.
- `detail` is for people. Programs must not parse it.
- No answer holds a stack trace, a file path, SQL or a host name. A 500 holds only `requestId`. The server log has the same id next to the full error.

| Status | `type` | When | Extra members |
|---|---|---|---|
| 400 | `#invalid-field` | The body is not JSON, nests too deep, its gzip does not inflate, or a field has the wrong shape or value. Also a bad `limit` or `after` on `GET /v1/data`, and a profile past the per-key cap (see "Characters"). | `field`: the first bad field, for example `records[0].cls`, or `body` |
| 400 | `#object-cap` | A new live object would pass a cap (see "Objects"). Nothing is written. | `field`: the upload, for example `objects[0]` |
| 401 | `#invalid-key` | The key is unknown, revoked or missing. | Header `WWW-Authenticate` |
| 401 | `#key-expired` | A regular key was not used for more than 30 days (see "Key expiry"). | Header `WWW-Authenticate` |
| 403 | `#character-owned` | The character in `identity` belongs to another key (see "Who may do what"). Nothing is written. | |
| 404 | `about:blank` | Any other path, a dataset name that is not served, or an unknown query parameter on `GET /v1/data`. | |
| 405 | `about:blank` | Another method on a known path. | Header `Allow` |
| 408 | `about:blank` | The body did not arrive within 10 s. | |
| 413 | `about:blank` | The body is over 256 KB (262144 bytes), as sent or after inflating. The server checks the declared size before it reads, and stops inflating at the limit. | |
| 415 | `about:blank` | `Content-Type` is not `application/json`, or `Content-Encoding` is not `gzip`. | Header `Accept-Encoding: gzip` for the second case |
| 429 | `#rate-limited` | Too many requests. | Header `Retry-After`: the wait in whole seconds |
| 500 | `about:blank` | A server bug. | `requestId` |

A rejected request changes nothing. Client input never causes a 500.

A proxy or a firewall between the client and the server can answer too, with an HTML body. So the client acts on the status code alone. It reads a problem body only when `Content-Type` is `application/problem+json`, and only for its log text (`type` and `field`).

### Problem types

#### invalid-field

The request body breaks a rule in "Field rules" or "Characters". `field` names the first bad field. Fix the client; sending the same body again gets the same answer.

#### character-owned

Another key owns this character. Ask the admin which key it is. Revoking that key frees the character.

#### object-cap

The key already has 20 live objects, or the battlegroup has 300. `detail` says which. Delete an object first, or wait until one ends. Sending the same body again gets the same answer.

#### rate-limited

Wait `Retry-After` seconds, then send again.

## Retries and conflicts

Every write is an upsert on a key, so sending the same request twice leaves the same state as sending it once:

- sightings: one row per 20 m cell (see "Key rule"). A repeat adds no row. Its key id is already in `observers`. The row gets a new `rev`, so a guildmate may receive it once more. That costs bytes but changes nothing on the map.
- roster and presence: one row per character.
- profiles: one row per character. The same profile again from the same key keeps its `rev`. Deleting twice is the same as deleting once.

So the routes take no `Idempotency-Key`: a retry can never make a second copy of anything, and no money or items move.

The routes take no `If-Match` either. Only the owning key writes a character's rows, and sightings keep the newest observation on the server clock (`receivedAt - lag`), whatever order the writes arrive in. The only possible "lost update" is between two detectors on the same key, and there the newer data should win.

## Changing the contract

The server and the detector ship together, but guildmates update their detectors at different times. So the server must serve old and new detectors at once.

- `/v1` never breaks. These count as breaking, and need a new path (`/v2`): a new required request field, a tighter rule on an existing field (shorter text, fewer classes, a smaller limit), a removed or renamed response key or header, a changed meaning or type of a field, a changed or reused problem `type`, a changed status code for an existing case, a different 403/404 rule, a new authentication rule, or much lower rate limits.
- These are additive and ship in `/v1`: a new response key (clients ignore keys they do not know), a new optional route, a new record class in a response (clients skip classes they do not know), and a looser limit.
- A new request field, even an optional one, is refused by an older server (unknown fields get 400). So the server ships first. A client sends the new field only after every server it talks to accepts it.
- `objects` is such an optional field. The client sends it only after a reply from the server carried the `objects` key (see "Objects").
- Before a route or a field goes away, every answer of the old route carries these headers, from the day it is announced:

  ```
  Deprecation: @1796083200
  Sunset: Thu, 31 Dec 2026 23:59:59 GMT
  Link: <https://github.com/sm18lr88/harvesting-dune/blob/master/docs/SYNC-PROTOCOL.md#changing-the-contract>; rel="deprecation"; type="text/html"
  ```

  `Deprecation` (RFC 9745) is `@` and unix seconds. `Sunset` (RFC 8594) is an HTTP date, never earlier than `Deprecation`. The route is marked `deprecated: true` in `docs/sync-openapi.yaml`. The admin removes the route only when the audit log (`client` and `key` per request) shows that nobody calls it, or at the sunset date with the owner's yes. After that, the route answers 410 with a problem body that links here.
- Today nothing is deprecated.

## Retention

The server removes old data. A sweep runs every 60 seconds.

| Data | Kept for |
|---|---|
| presence | Until 15 minutes after the member went offline (see "Presence") |
| `spice`, `blow` and `ore` sightings | 3 hours |
| `ship`, `mined` and `stale` sightings | 24 hours |
| character profiles | 30 days after the last upload, and longer while that character keeps syncing. A player who turns "Share my character" off deletes it. |
| audit log | 30 days |
| objects | 24 hours after they end (lifetime over, deleted, or the author's key revoked or expired). At most 100 rows per key. |

Rate limits:

- Each key: 30 requests per minute (a bucket of 30).
- Each IP address without a valid key, and `/health`: 10 requests per minute.

## Client

- The client syncs only when all these are true: a key is set, sync is on, the key was not rejected, and it knows the identity and the partition.
- How often it syncs depends on what the player does. The loop wakes every 15 seconds and checks:

  | State | One sync every | Syncs per hour |
  |---|---|---|
  | In a world, and moved more than 100 m since the last sync, or finds still waiting to go out | 15 s | 240 |
  | In a world, nothing changed (standing, crafting, in a base) | 60 s | 60 |
  | No world (menu, loading screen): only the heartbeat, at the last place | 180 s | 20 |
  | No key, sync paused, key rejected, the game closed, or no identity yet | never | 0 |

- The loop keeps each sync's promise (`nextS`): when the player slows down (moving to standing, world to menu), the next sync still comes within the promised time.

- The client syncs at once after you enter a world, save a key, turn sync back on, change a sharing switch, or open the guild lists on the settings page.
- When the server cut the sightings list at 500, the client syncs again at once.
- After a failure, it waits 30, 60, 120, 300, then 600 seconds. A good response resets the wait.
- Records the server did not acknowledge go out with the next sync, but only in the same partition. After a world change they are dropped: their coordinates mean nothing in the new world.
- A response that changed nothing (no new sightings, no guildmate moved) does no work beyond updating the members' age stamps. It does not redraw the map.
- 401: the key state becomes "invalid". Polling stops until the player enters a different key.
- 429: the client waits `Retry-After` seconds, at most 600.
- Sync failures are logged as WARNING, never ERROR. A sync failure never shows a problem banner on the map.
- Replay and the unit tests never contact the server.
- Who plays is one unit: `characterName`, `characterId` and `battlegroupId` of one character. A `dune.trackedSession` delta with another `characterId`, or another name without an id, starts a new identity from its own fields only. The identity also ends when the game closes (`dune.status` `hasGameSession: false`) or on a topicReset. Nothing is published until the name, the id and the battlegroup of one character are known. A loading screen does not end it: the companion sends the topic once per game start, and every loading screen reports no world.
- When the identity ends, the old character says goodbye, and everything built for it is dropped: its queued finds, records not yet acknowledged, its profile topics and its upload throttle. The next character uploads its own profile at once.
- If the companion does not track the session, nothing is published, and the settings page says "turn on session tracking in the companion to share".
- Peer data stays in memory. It is fetched again after a restart. It never goes into the journal or into any file under `data\`.
- Peer data changes the ranking only for spice. At each spice site, the newest report wins: your own companion, a guildmate (`spice` and `blow` = active, `stale` on a spice row = empty) or the scan database (no time, so it is always the oldest). A site that your own companion loads now is decided by your companion alone. All other peer data never changes the ranking. Peer data is drawn as its own kind, with its own colour and its age.
- Map rings are drawn only for `spice`, `blow`, `ship`, `ore` and members. `mined` and `stale` show only in the list on the settings page.
- Positions: each player can show or hide each guildmate on the minimap. The default is to show all.

### Hiding a scan row

A peer can hide a scan row only with strong proof. The client hides a scan row (`rowKey`) only when the `stale` sighting has at least two distinct key ids in `observers` and a `distCm`. One key alone never hides anything. The client never demotes a node that it sees live itself.

For a node that respawns (`catalog.NODE_MARKERS`), the client hides the row only while the sighting's age is less than `det.respawn_for(type)`. After that the row shows again, even if the server still keeps the sighting. Other markers stay hidden until the sighting's lifetime ends.

## Characters

Each player can share a character profile with the guild. The settings page shows the guild's profiles in its Characters section.

The profile holds only what the companion already shows on its own pages:

| Key | From | Content |
|---|---|---|
| `status` | `dune.session`, `dune.status` | `worldId`, `worldName`, `activity`, `inGame` |
| `skills` | `dune.skillProgress` | `gameMode`, `skills` (`[tag, level]` pairs), `abilities`, `techniques` |
| `stats` | `dune.rawStats` | `[{name, entries: [[name, value]]}]` |
| `recipes` | `dune.knownRecipes` | `[recipeId, grade]` pairs |
| `landsraad` | `dune.landsraad` | `term`, `houses` (`house`, `specialization`, `progress`, `max`, `earned`, `rewards`) |
| `achievements` | `dune.achievements` | `completed` ids, `progress` (`[id, share]`), `seen` |
| `session` | `dune.trackedSession` | `characterXp`, `specializationXp`, `dailies`, `dungeonRuns` (a count), `loot` (`[name, amount]`) |

Each key is `null` until its topic has arrived. The page then says "not reported yet".

Never in a profile: currencies (Solari), session history, base ids, anything from `dune.bases`. The companion has no inventory or gear topic for Dune, so a profile has no items.

### Routes

`POST /v1/profile` takes `{"identity": {...}, "profile": {...}}` and answers `{"ok": true}`.

- `profile` has exactly the seven keys above. `session` is `null` or has exactly its five keys. An unknown key gets 400 with its name (for example `profile.session.currencies`).
- Every value is plain JSON: finite numbers, text up to 200 characters, lists up to 1000 items, at most 6 levels deep.
- The stored profile is at most 32 KB (32768 bytes) as compact JSON.
- `profile: null` deletes the stored profile. The next `/v1/profiles` no longer lists it.
- The server keeps one profile per character id. The same profile again from the same key keeps its `rev`.
- One key stores at most 20 character profiles. Another new character gets 400 `#invalid-field` with `field` `identity.characterId`.

`POST /v1/profiles` takes `{"identity": {...}, "since": <int>}` and answers:

| Key | Content |
|---|---|
| `now` | The server clock. |
| `next` | The highest profile rev so far. Send it as `since` next time. |
| `ids` | Every character id of the caller's battlegroup that has a profile. A cached profile not in this list was deleted. |
| `profiles` | Profiles of the caller's battlegroup with `rev > since`: `characterId`, `characterName`, `rev`, `ageS` (since the last change of that profile; an upload with no change keeps its `rev` and its age), `profile`, `playerTag` (see "Who may do what"). |

Profiles have their own rev counter. The sync response never holds profiles.

### Client

- "Share my character" (on by default): off sends one `profile: null`, then nothing more.
- "Share this session" (on by default): off sends `session: null` with the next upload.
- The client uploads its profile only when its content changed, at most once every 5 minutes. A switch change, a new key, and the first upload after start go at once.
- If a profile is too big, the client leaves out `stats`, then `achievements`, then `recipes` (and so on) until it fits.
- The client asks for the guild's profiles only while the Characters section is open on the settings page: when it opens, then once a minute.
- The Characters list keeps each player's characters together (same `playerTag`). A player with alts gets one heading made of their character names, for example "Leto Test · Leto Alt". Your own characters come first, then players with someone online. Inside a player: online, in menu, late, offline, then characters that only have a profile.
- A character that is not played shows as offline, then disappears from the roster, but its profile stays in Characters.

## Datasets

Some data is not in the public detector. The server holds it, and gives it only to a valid key. Examples are farm routes and item-source tables. A key that is revoked or expired gets no new data. Data that a detector already downloaded stays on that PC.

Each dataset is one JSON file on the server. The admin puts it there (see GUILD-SYNC-ADMIN.md). The file name is the dataset name plus `.json`.

A name has 1 to 32 characters from `a-z`, `0-9` and `-`. The server serves a file only when it is a plain file, it is at most 4 MiB (4194304 bytes), and it is valid JSON. Any other name, or a file that breaks a rule, gets 404 `about:blank`. The server never URL-decodes the name, so `..`, `%2e%2e` and `\` are just names that break the rule.

The server reads a file once and keeps it in memory. It reads it again only when the file's modified time or size changes.

### One dataset

`GET /v1/data/<name>` with the key in `Authorization`:

- 200: the file's bytes as they are on the server, `Content-Type: application/json`.
- `ETag`: the sha256 of the file's bytes as lowercase hex, in double quotes. It is a strong ETag. The gzip reply has the same ETag as the plain one.
- `Cache-Control: private` and `Vary: Accept-Encoding`.
- When the request says `Accept-Encoding: gzip` and the file is bigger than 1 KiB, the body is gzipped, with `Content-Encoding: gzip`.
- Send the ETag of your cached copy in `If-None-Match`. When it matches, the answer is 304 with no body. `If-None-Match` may hold a list of ETags, or `*`. A `W/` prefix is ignored.
- A query string gets 404.

`HEAD` returns the same headers with no body.

### The list

`GET /v1/data` returns the datasets the server serves, by name:

```json
{"datasets": [{"name": "routes", "etag": "\"<64 hex>\"", "bytes": 18342}], "next": null}
```

- `etag` is the exact `ETag` header value, quotes included. Send it back as `If-None-Match`.
- `bytes` is the size without gzip.
- A file that breaks a rule is left out.
- At most 100 datasets come in one answer. `next` is not null when there are more. Send it back as `after` to get the next page: `GET /v1/data?after=<next>`. `limit=1` to `limit=100` sets the page size.
- A bad `limit` or `after`, or the same parameter twice, gets 400 `#invalid-field` (`field` is `limit`, `after` or `query`). Any other query parameter gets 404, because a key never goes in a URL.

### When the client asks

The client must ask for the list only on start, and then at most once a day. It must fetch a dataset only when its `etag` in the list differs from the cached copy, and then sends `If-None-Match`. So a normal day costs one small request. A sync never fetches data.

The data routes use the same key and the same rate bucket as sync: 30 requests a minute per key. A request without a valid key counts against the IP limit (10 a minute). The audit log gets one line per call, like sync.

## Cost

Measured on 2026-11-08 over the real TLS test server (`tests/certs`), with three guildmates online in the same map and random positions. Bytes are HTTP body bytes. They do not count HTTP headers or the TLS handshake (not measured).

| One request | Up, gzip | Down, gzip | Up, plain | Down, plain |
|---|---|---|---|---|
| Sync, nothing new | 297 | 805 | 295 | 803 |
| Sync, 5 finds up, 5 back | 523 | 932 | 1,265 | 4,305 |
| Sync, 200 ore records up (they come back too) | 8,917 | 10,538 | 37,838 | 69,605 |
| A guildmate's next sync with those 200 rows | 297 | 10,536 | 296 | 69,595 |
| Profile upload (10 KB profile) | 1,857 | 12 | 10,396 | 12 |
| Profiles, first fetch (one profile) | 111 | 1,990 | 111 | 11,603 |
| Profiles, nothing changed | 111 | 69 | 111 | 68 |

Syncs per hour: 240 moving, 60 standing in a world, 20 in menus, 0 without a key (`tests/test_sync_efficiency.py`). Before this, the client synced every 20 seconds in every state: 180 per hour.

Ore refresh: 200 live ore nodes sent again every 20 minutes cost about 25 KB per hour up for the player who sees them, and about 29 KB per hour down for each guildmate in that map (gzip on).

Objects: a restart fetches every object of the battlegroup once. That is at most 300 live objects plus at most 100 rows per key, 200 per reply.

Server CPU per sync (the handler: parse, writes, read, reply JSON and gzip; `time.process_time` over 1,000 syncs): 0.14 ms with nothing new, 0.28 ms for a typical sync, 3.5 ms with 200 records.

## Lifetimes

The client compares each sighting's age with a lifetime. When the age is over the lifetime, the client drops the sighting from the map and from the list.

| `cls` | Lifetime |
|---|---|
| `spice` | `store.REMEMBER_SPICE_S` (45 minutes) |
| `blow` | `store.REMEMBER_SPICE_S` (45 minutes). It counts as "fresh" while the age is at most `outputs.FRESH_BLOW_S` (20 minutes). |
| `ship` | `ships.REMEMBER_S` (30 minutes) |
| `mined` | `det.respawn_for(type)` |
| `ore` | `det.respawn_for(type)` |
| `stale` | `corrections.stale_ttl_s({"marker": type}, det.respawn_for)` |
