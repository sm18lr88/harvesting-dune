"""Guild characters through the real guild server over TLS, two detectors: A's profile reaches B's
Characters view, an unchanged profile is neither uploaded nor sent again, the switches delete or
trim what is shared, and no currency ever reaches the server."""

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest
from guildsync import httpd
from guildsync.api import Api
from guildsync.httpd import make_server
from guildsync.storage import Storage
from helpers import pose
from test_robustness import free_port
from test_sync_integration import ALPHA, OTHER, Player, Stop, issue

from harvesting_dune import profile, syncloop
from harvesting_dune.app import Detector
from harvesting_dune.feed import BusMessage

HERE = Path(__file__).parent
PAUL = {"characterName": "Paul Test", "characterId": 1001, "battlegroupId": ALPHA}
CHANI = {"characterName": "Chani Test", "characterId": 1002, "battlegroupId": ALPHA}
TOPICS = {  # shaped like the companion's payloads (character-data survey)
    "dune.skillProgress": {"spentByTag": {"Skill.Trooper.Rifle": 3, "Skill.Mentat.Poison": 2}, "isSinglePlayer": False,
                           "abilities": [{"name": "Shield Wall"}], "techniques": ["Shigawire Claw"]},
    "dune.knownRecipes": {"recipes": [{"recipeId": "unique_regis_rifle", "grade": 3}], "snapshotSource": "live"},
    "dune.landsraad": {"termLabel": "Term 4", "houses": [{"houseId": "house_hagal", "specializationId": "combat",
                       "progressValue": 1200, "progressMaximum": 5000,
                       "currentRewards": [{"itemId": "a", "isEarned": True}, {"itemId": "b", "isEarned": False}]}]},
    "dune.achievements": {"achievements": [{"id": "first_blood", "isCompleted": True, "completionProgress": 1},
                                           {"id": "spice_baron", "isCompleted": False, "completionProgress": 0.4}]},
    "dune.rawStats": {"groups": [{"name": "Hydration", "entries": [{"name": "WaterCapacity", "value": 2500.5}]}]},
    "dune.trackedSession": {**PAUL, "characterXp": 1469, "activeSeconds": 4000, "sessionId": "s-1",
                            "specializationXp": [{"id": "explorationtrack", "name": "Exploration", "amount": 125}],
                            "currencies": [{"id": "solaris", "name": "Solari", "amount": 98765}],
                            "loot": [{"id": "dolomite", "name": "Dolomite", "amount": 36}],
                            "dailies": [{"dailyId": "d1", "dailyName": "Harvest Spice", "completedAtMs": 1}],
                            "dungeonRuns": [{"dungeonId": "x"}]},
}


@pytest.fixture
def guild(tmp_path, monkeypatch):
    """The real server on a free port; `bodies` collects every request body as the server read it (gzip undone)."""
    bodies: list[tuple[str, bytes]] = []
    read = httpd.Handler._read_body

    def recorded(handler):
        raw = read(handler)
        bodies.append((handler.path, raw))
        return raw

    monkeypatch.setattr(httpd.Handler, "_read_body", recorded)
    db = tmp_path / "guildsync.sqlite"
    storage = Storage.open(str(db))
    server = make_server(storage, str(HERE / "certs" / "test-server.pem"), str(HERE / "certs" / "test-server.key"),
                         "127.0.0.1", free_port(), Api(storage, time.time))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"https://127.0.0.1:{server.server_address[1]}", str(db), bodies
    server.shutdown()
    server.server_close()
    storage.close()


@pytest.fixture
def players(tmp_path, guild, capsys, monkeypatch):
    async def no_wait(seconds, wake):
        raise Stop

    monkeypatch.setattr(syncloop, "nap", no_wait)
    url, db, _ = guild
    t0 = time.time()
    a = Player(tmp_path / "a", url, issue(capsys, db, "Paul")["token"], PAUL, t0)
    b = Player(tmp_path / "b", url, issue(capsys, db, "Chani")["token"], CHANI, t0)
    for p in (a, b):
        pose(p.det, 0, 0, t=t0 + 1)
    for topic, payload in TOPICS.items():
        a.det.handle(BusMessage("delta", topic, payload), t0 + 2)
    return a, b


def paths(player) -> list[str]:
    return [p for p, _, _ in player.profile_calls]


def test_a_profile_reaches_the_guild_and_unchanged_goes_nowhere(players, guild):
    a, b = players
    a.sync_once()
    b.det.characters_opened()
    b.sync_once()
    assert paths(a) == ["/v1/profile"] and paths(b) == ["/v1/profile", "/v1/profiles"]

    [row] = [r for r in b.det.characters_snapshot()["rows"] if not r["self"]]
    assert (row["id"], row["name"], row["world"], row["activity"]) == (1001, "Paul Test", "survival_1", "Exploration")
    assert 0 <= row["profileAgeS"] < 60 and row["seenS"] is not None
    seen = b.det.character_detail(1001)["profile"]
    assert seen["skills"] == {"gameMode": "online", "skills": [["Skill.Mentat.Poison", 2], ["Skill.Trooper.Rifle", 3]],
                              "abilities": ["Shield Wall"], "techniques": ["Shigawire Claw"]}
    assert seen["recipes"] == [["unique_regis_rifle", 3]]
    assert seen["landsraad"]["houses"] == [{"house": "house_hagal", "specialization": "combat", "progress": 1200,
                                            "max": 5000, "earned": 1, "rewards": 2}]
    assert seen["achievements"] == {"completed": ["first_blood"], "progress": [["spice_baron", 0.4]], "seen": 2}
    assert seen["stats"] == [{"name": "Hydration", "entries": [["WaterCapacity", 2500.5]]}]
    assert seen["session"] == {"characterXp": 1469, "specializationXp": [["Exploration", 125]], "dailies": ["Harvest Spice"],
                               "dungeonRuns": 1, "loot": [["Dolomite", 36]]}
    assert b.det.character_detail(1002)["profile"]["skills"] is None  # B's companion sent no skills: "not reported yet"

    # unchanged: past the 5-minute gate A uploads nothing, and B's next fetch carries no profile
    a.det.chars.sent_at -= 400
    a.sync_once()
    b.det.characters_opened()
    b.sync_once()
    assert paths(a) == ["/v1/profile"] and paths(b)[-1] == "/v1/profiles"
    resp = b.profile_calls[-1][2]
    assert resp["profiles"] == [] and resp["ids"] == [1001, 1002]
    assert len(json.dumps(resp)) < 120

    # closed view: no profile request at all
    b.det.chars.open_at = 0.0
    b.sync_once()
    assert paths(b).count("/v1/profiles") == 2


def test_switches_trim_then_delete_and_no_currency_is_ever_sent(players, guild):
    a, b = players
    a.sync_once()
    a.det.ui("share_session")
    a.sync_once()  # a switch change goes at once, gate or not
    _, _, bodies = guild
    uploads = [json.loads(raw) for path, raw in bodies if path == "/v1/profile" and b'"Paul Test"' in raw]
    assert [u["profile"]["session"] is None for u in uploads] == [False, True]
    assert b"characterXp" not in [raw for path, raw in bodies if path == "/v1/profile"][-1]

    a.det.ui("share_character")
    a.sync_once()
    a.sync_once()  # the delete goes once
    assert paths(a) == ["/v1/profile"] * 3 and a.profile_calls[-1][1]["profile"] is None
    b.det.characters_opened()
    b.sync_once()
    # a guildmate who shares nothing still gets a details pane: "Not shared.", never "Pick a character."
    assert b.det.character_detail(1001) == {"id": 1001, "name": "Paul Test", "self": False, "ageS": None,
                                            "profile": None, "recipeNames": {}}
    assert [r["profileAgeS"] for r in b.det.characters_snapshot()["rows"] if r["id"] == 1001] == [None]

    wire = b"".join(raw for _, raw in bodies)
    for word in (b"olari", b"currenc", b"98765", b"activeSeconds", b"sessionId"):
        assert word not in wire


def test_a_profile_too_big_for_the_server_sheds_its_largest_parts():
    many = {"groups": [{"name": f"g{i}", "entries": [{"name": f"stat{i}_{j}", "value": j} for j in range(300)]} for i in range(20)]}
    built = profile.build({"dune.rawStats": many, **{k: v for k, v in TOPICS.items() if k != "dune.rawStats"}}, {}, True)
    assert built["stats"] is None and built["skills"] is not None and profile.size(built) <= profile.MAX_BYTES


def test_character_topics_stay_out_of_the_journal(tmp_path):
    det = Detector(tmp_path / "journals", data_dir=tmp_path, min_rescore_s=0)
    for n, (topic, payload) in enumerate(TOPICS.items()):
        det.handle(BusMessage("delta", topic, payload), n)
    written = (tmp_path / "journals").glob("*.jsonl")
    topics = {json.loads(line)["topic"] for f in written for line in f.read_text("utf8").splitlines()}
    assert topics == {"dune.rawStats", "dune.trackedSession"}  # both cut down as before
    assert det.own_profile()["skills"] is not None


def test_a_guildmates_profile_is_reduced_to_known_shapes_before_the_page_sees_it(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    hostile = {"status": "Hagga", "skills": {"skills": "x", "abilities": [1, "<b>Wall</b>"], "gameMode": 7},
               "stats": {"groups": []}, "recipes": [["unique_regis_rifle", "3"], "junk", [5, 1]],
               "landsraad": {"houses": [{"house": "house_hagal", "earned": "<img src=x onerror=alert(1)>", "progress": "9"}]},
               "achievements": {"completed": "all", "seen": "<i>"}, "session": [1, 2], "extra": {"currencies": 5}}
    det.ingest_profiles({"next": 1, "ids": [7], "profiles": [{"characterId": 7, "characterName": "Feyd", "rev": 1, "ageS": 1,
                                                              "profile": hostile}]}, time.time())
    got = det.character_detail(7)["profile"]
    assert got == {"status": None, "skills": {"gameMode": None, "skills": [], "abilities": ["<b>Wall</b>"], "techniques": []},
                   "stats": None, "recipes": [["unique_regis_rifle", None]],
                   "landsraad": {"term": None, "houses": [{"house": "house_hagal", "specialization": None, "progress": None,
                                                          "max": None, "earned": 0, "rewards": 0}]},
                   "achievements": {"completed": [], "progress": [], "seen": 0}, "session": None}


def test_a_character_on_another_battlegroup_gets_that_battlegroups_profiles(tmp_path, players, guild, capsys):
    a, b = players
    url, db, _ = guild
    t0 = time.time()
    feyd = Player(tmp_path / "c", url, issue(capsys, db, "Feyd")["token"],
                  {"characterName": "Feyd Test", "characterId": 1003, "battlegroupId": OTHER}, t0)
    pose(feyd.det, 0, 0, t=t0 + 1)
    feyd.sync_once()  # Feyd's profile goes up first: the lowest profile rev
    a.sync_once()
    b.det.characters_opened()
    b.sync_once()  # as Chani on ALPHA: the cursor moves past Feyd's rev
    b.det.handle(BusMessage("delta", "dune.trackedSession",
                            {"characterName": "Chani Alt", "characterId": 1004, "battlegroupId": OTHER}), t0 + 3)
    b.det.characters_opened()
    with pytest.raises(Stop):  # this pass also carries Chani's goodbye
        asyncio.run(b.runner.sync_loop())
    assert [r["profileAgeS"] is not None for r in b.det.characters_snapshot()["rows"] if r["id"] == 1003] == [True]


def test_the_still_open_ping_fetches_profiles_without_a_sync(players, monkeypatch):
    a, b = players
    a.sync_once()
    b.det.ui("guild_view", "characters")
    b.sync_once()
    naps: list[float] = []

    async def a_minute_later(seconds, wake):
        if naps:
            raise Stop
        naps.append(seconds)
        b.det.chars.open_at -= 60
        b.det.chars.fetched_at -= 61
        b.det.ui("guild_view", "characters")  # the open view's once-a-minute "still open"
        await asyncio.sleep(0)  # let the loop run the wake-up the ping scheduled
        woken = wake.is_set()
        wake.clear()
        return seconds, woken

    monkeypatch.setattr(syncloop, "nap", a_minute_later)
    syncs, fetches = len(b.calls), paths(b).count("/v1/profiles")
    with pytest.raises(Stop):
        asyncio.run(b.runner.sync_loop())
    assert (len(b.calls) - syncs, paths(b).count("/v1/profiles") - fetches) == (1, 1)
