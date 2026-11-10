"""The detector's wire bounds against docs/sync-openapi.yaml: the client never sends or expects more than the
server's contract allows, so the two cannot drift apart unnoticed."""

import json
import re
from pathlib import Path

from harvesting_dune import objects, peers, peerview, profile

SPEC = json.loads((Path(__file__).parent.parent / "docs" / "sync-openapi.yaml").read_text(encoding="utf-8"))
SCHEMAS = SPEC["components"]["schemas"]


def test_one_request_carries_no_more_records_or_objects_than_the_server_accepts():
    request = SCHEMAS["SyncRequest"]["properties"]
    assert (peers.MAX_RECORDS, objects.MAX_PER_SYNC) == (request["records"]["maxItems"], request["objects"]["maxItems"])


def test_the_pin_limits_are_the_server_object_limits():
    upload = SCHEMAS["ObjectUpload"]
    assert objects.MAX_TTL_S == upload["properties"]["ttlS"]["x-clamp-max"]
    assert (objects.MAX_LIVE_MINE, objects.MAX_LIVE_GROUP) == (upload["x-max-live-per-key"], upload["x-max-live-per-battlegroup"])
    [label] = re.findall(r"at most (\d+) characters", upload["properties"]["body"]["description"])
    assert objects.LABEL_MAX == int(label)


def test_a_full_object_page_is_the_server_page_size():
    reply = SCHEMAS["SyncReply"]["properties"]["objects"]
    assert objects.OBJECTS_PAGE == reply["x-page-size"] == reply["maxItems"]


def test_a_profile_is_trimmed_to_the_size_the_server_stores():
    assert profile.MAX_BYTES == SCHEMAS["Profile"]["x-max-stored-bytes"]


def test_sightings_are_keyed_by_the_server_cell():
    [metres] = re.findall(r"(\d+) m cell", SCHEMAS["Record"]["description"])
    assert peerview.WIRE_CELL_CM == peers.NOTE_CELL_CM == int(metres) * 100
