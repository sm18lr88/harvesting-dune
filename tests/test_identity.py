import json
from pathlib import Path

from helpers import pose, session

from harvesting_dune.app import Detector
from harvesting_dune.feed import BRIDGE_SCRIPT, to_bus
from harvesting_dune.model import BusMessage

FIXTURE = Path(__file__).parent / "fixtures" / "tracked_session.jsonl"
TOPIC = "dune.trackedSession"


def recorded() -> list[tuple[float, BusMessage]]:
    """Journal lines in the companion's shape: a topicReset, two deltas without the name, then the named one."""
    recs = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
    return [(r["t"], BusMessage(r["type"], r["topic"], r["payload"])) for r in recs]


def test_identity_waits_for_name_and_battlegroup():
    det = Detector(None)
    msgs = recorded()
    for t, msg in msgs[:-1]:
        det.handle(msg, t)
    assert not det.identity_ready
    assert det.identity == {"characterId": 10000001, "battlegroupId": "prd-srv-test-home-erg"}
    t, msg = msgs[-1]
    det.handle(msg, t)
    assert det.identity_ready
    assert det.identity == {"characterName": "Leto Test", "characterId": 10000001, "battlegroupId": "prd-srv-test-home-erg"}


def test_later_delta_without_fields_keeps_the_known_identity():
    det = Detector(None)
    for t, msg in recorded():
        det.handle(msg, t)
    det.handle(BusMessage("delta", TOPIC, {"characterName": None, "activeSeconds": 40}), 2e9)
    assert det.identity["characterName"] == "Leto Test" and det.identity_ready


def test_topic_reset_clears_the_identity():
    det = Detector(None)
    for t, msg in recorded():
        det.handle(msg, t)
    det.handle(BusMessage("topicReset", TOPIC, None), 2e9)
    assert det.identity == {} and not det.identity_ready


def test_malformed_fields_leave_the_identity_empty():
    det = Detector(None)
    det.handle(BusMessage("delta", TOPIC, {"characterName": 7, "characterId": True, "battlegroupId": ["x"]}), 1)
    det.handle(BusMessage("delta", TOPIC, "not a dict"), 2)
    det.handle(BusMessage("delta", TOPIC, {"characterName": "x" * 65, "battlegroupId": "y" * 65}), 3)
    assert det.identity == {} and not det.identity_ready


def test_journal_keeps_who_plays_and_drops_earnings_and_timers(tmp_path):
    det = Detector(tmp_path / "journals", data_dir=tmp_path)
    for t, msg in recorded():
        det.handle(msg, t)
    det.handle(BusMessage("delta", TOPIC, {"characterName": "Leto Test", "characterId": 10000001, "battlegroupId": "prd-srv-test-home-erg",
                                           "sessionId": "00000000000000000000000000000000", "isActive": True, "activeSeconds": 99.0,
                                           "currencies": [{"name": "Solari", "amount": 5}], "loot": [{"item": "x"}], "dailies": [1]}), 2e9)
    text = next((tmp_path / "journals").glob("*.jsonl")).read_text()
    for word in ("currencies", "loot", "dailies", "activeSeconds"):
        assert word not in text
    lines = [json.loads(line) for line in text.splitlines()]
    # topicReset + the three distinct identities; the last delta only ticked timers and earnings
    assert [line["type"] for line in lines] == ["topicReset", "delta", "delta", "delta"]
    assert lines[-1]["payload"] == {"battlegroupId": "prd-srv-test-home-erg", "characterId": 10000001, "characterName": "Leto Test",
                                    "isActive": True, "sessionId": "00000000000000000000000000000000"}


def test_bridge_subscribes_and_feed_forwards_the_topic():
    assert TOPIC in BRIDGE_SCRIPT
    msg = to_bus({"type": "delta", "topic": TOPIC, "payload": {"characterName": "Leto Test"}})
    assert msg is not None and msg.topic == TOPIC


# -- one PC, two accounts: a main, then an alt (switched by quitting the game) ----------------
BG = "prd-srv-test-home-erg"
MAIN = {"characterName": "Leto Test", "characterId": 10000001, "battlegroupId": BG}
ALT = {"characterName": "Leto Alt", "characterId": 10000002, "battlegroupId": BG}
KEY = "gs1_alpha001_Qm9vdHN0cmFwVGVzdEtleUFscGhhMDAx"
SKILLS = "dune.skillProgress"


def playing_main() -> Detector:
    det = Detector(None, min_rescore_s=0)
    det.set_sync_key(KEY)
    session(det, "survival_1")
    det.handle(BusMessage("delta", "dune.status", {"hasGameSession": True}), 0)
    det.handle(BusMessage("snapshot", SKILLS, {"spentByTag": {"Skill.Main.Only": 3}}), 0)
    det.handle(BusMessage("snapshot", "dune.rawStats", {"groups": [{"name": "Hydration", "entries": [{"name": "Water", "value": 9}]}]}), 0)
    det.handle(BusMessage("delta", TOPIC, MAIN), 1)
    pose(det, 0, 0, t=1)
    return det


def published(det: Detector, now: float) -> dict | None:
    body = det.outbox(now)
    return None if body is None else body["identity"]


def test_quitting_the_game_ends_the_identity_and_drops_what_was_built_for_it():
    det = playing_main()
    assert published(det, 2) == MAIN and det.profile_upload(2)["identity"] == MAIN
    det.profile_sent(det.profile_upload(2), 2)
    det.peer_note("blow", "spicefieldlarge", 100.0, 0.0, 0.0, 3)
    det.peers.connected_as = "Leto Test"
    det.handle(BusMessage("delta", "dune.status", {"hasGameSession": False, "status": "waitingForProcess"}), 4)
    assert det.identity == {} and det.outbox(5) is None and det.profile_upload(5) is None
    assert det.peers.outbox == {} and det.chars.topics == {} and det.peers.connected_as is None
    assert det.peers.farewell["identity"] == MAIN  # the main says goodbye on the sync loop's next pass


def test_switching_to_another_character_never_mixes_their_fields_or_profiles():
    det = playing_main()
    det.handle(BusMessage("delta", "dune.status", {"hasGameSession": False}), 4)
    det.handle(BusMessage("delta", "dune.status", {"hasGameSession": True}), 5)
    det.handle(BusMessage("snapshot", SKILLS, {"spentByTag": {"Skill.Alt.Only": 1}}), 5)
    det.handle(BusMessage("delta", TOPIC, {"battlegroupId": BG, "activeSeconds": 2}), 6)  # early: no name, no id
    assert published(det, 6) is None
    det.handle(BusMessage("delta", TOPIC, {"characterName": "Leto Alt", "battlegroupId": BG}), 7)
    assert det.identity == {"characterName": "Leto Alt", "battlegroupId": BG} and published(det, 7) is None
    det.handle(BusMessage("delta", TOPIC, ALT), 8)
    assert published(det, 8) == ALT
    upload = det.profile_upload(8)  # the first upload after a switch goes at once
    assert upload["identity"] == ALT and [s[0] for s in upload["profile"]["skills"]["skills"]] == ["Skill.Alt.Only"]
    assert upload["profile"]["stats"] is None  # the main's stats never ride along: the alt has not reported any


def test_a_delta_naming_another_character_starts_a_new_identity_even_without_a_reset():
    det = playing_main()
    det.handle(BusMessage("delta", TOPIC, {"characterName": "Leto Alt"}), 4)  # no id yet: never paired with the main's
    assert det.identity == {"characterName": "Leto Alt"} and published(det, 4) is None
    assert det.chars.topics.get(SKILLS) is None and det.profile_upload(4) is None
    assert det.chars.topics[TOPIC] == {"characterName": "Leto Alt"}  # the switching delta belongs to the new character
    det.handle(BusMessage("delta", TOPIC, {"characterId": ALT["characterId"], "battlegroupId": BG}), 5)
    assert published(det, 5) == ALT
    det.handle(BusMessage("delta", TOPIC, {"characterId": MAIN["characterId"]}), 6)  # another id: another identity
    assert det.identity == {"characterId": MAIN["characterId"]} and published(det, 6) is None


def test_a_loading_screen_keeps_the_identity():
    det = playing_main()
    session(det, None)  # every map change reports no world for a moment; the topic is not sent again
    session(det, "survival_1")
    assert published(det, 9) == MAIN


def test_the_page_says_when_the_companion_does_not_track_the_session():
    det = Detector(None, min_rescore_s=0)
    det.set_sync_key(KEY)
    session(det, "survival_1")
    det.handle(BusMessage("delta", "dune.status", {"hasGameSession": True}), 0)
    assert det.peers_snapshot()["needsTracking"] is True and det.outbox(1) is None
    det.handle(BusMessage("delta", TOPIC, MAIN), 1)
    assert det.peers_snapshot()["needsTracking"] is False


def test_a_named_character_with_an_id_replaces_a_known_other_name_without_id():
    det = Detector(None)
    det.handle(BusMessage("delta", TOPIC, {"characterName": "Alia", "battlegroupId": "bg-EU-1"}), 1)
    det.handle(BusMessage("delta", TOPIC, {"characterName": "Leto", "characterId": 42}), 2)
    assert det.identity == {"characterName": "Leto", "characterId": 42} and not det.identity_ready


def test_the_same_id_under_a_new_name_is_no_switch():
    det = Detector(None)
    det.handle(BusMessage("delta", TOPIC, {"characterName": "Alia", "characterId": 42, "battlegroupId": "bg-EU-1"}), 1)
    det.handle(BusMessage("delta", TOPIC, {"characterName": "Alia II", "characterId": 42}), 2)
    assert det.identity == {"characterName": "Alia II", "characterId": 42, "battlegroupId": "bg-EU-1"}
