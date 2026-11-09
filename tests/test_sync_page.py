"""The settings page's Guild section, run in Node against a real detector snapshot: status words,
the roster with its tick boxes, shared finds (live ore too), the Characters view, and a key that only
ever travels once, in the body of POST /sync/key."""

import json
import shutil
import subprocess
import time
from pathlib import Path
from importlib import resources

import pytest
from helpers import pose, session

from harvesting_dune.app import Detector
from harvesting_dune.feed import BusMessage

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

KEY = "gs1_alpha001_Qm9vdHN0cmFwVGVzdEtleUFscGhhMDAx"
DD, LAYOUT, GROUP = "deepdesert_1", "deepdesert_1_07", "prd-srv-test-alpha-erg"
ME = {"characterName": "Paul Test", "characterId": 1001, "battlegroupId": GROUP}
# a DOM just deep enough for the page script: elements by id, document listeners, fetch and storage spies
HARNESS = r"""
const els = {}, calls = [], stored = [], docListeners = {};
const el = (id) => els[id] || (els[id] = { id, innerHTML: "", textContent: "", hidden: false, value: "", checked: false, open: true,
  dataset: {}, style: {}, scrollWidth: 0, clientWidth: 0, classList: { toggle() {}, add() {}, remove() {}, contains: () => false },
  setAttribute() {}, getAttribute: () => null, addEventListener() {}, appendChild() {}, focus() {}, select() {},
  querySelector: () => null, querySelectorAll: () => [] });
globalThis.document = { getElementById: el, addEventListener: (t, fn) => (docListeners[t] = docListeners[t] || []).push(fn),
  querySelectorAll: () => [], activeElement: null, createElement: () => el("_new") };
globalThis.window = globalThis; globalThis.addEventListener = () => {};
globalThis.localStorage = { getItem: () => null, setItem: (_, v) => stored.push(v) };
globalThis.location = { reload() {} }; globalThis.navigator = {}; globalThis.EventSource = class {};
globalThis.fetch = async (url, opts = {}) => {
  calls.push({ url, method: opts.method || "GET", body: opts.body });
  return new Response(null, { status: 204 });
};
const fire = (type, target) => (docListeners[type] || []).forEach((fn) => fn({ target }));
const tick = () => new Promise((r) => setTimeout(r, 0));
const SNAP = SNAPSHOT;
const page = eval(SCRIPT + "\n;({ render })");
const log = {};
(async () => {
  STEPS
  console.log(JSON.stringify(log));
})().catch((e) => { console.error((e && e.stack) || e); process.exit(1); });
"""


def member(cid, name, x, y, age=5.0):
    return {"characterName": name, "characterId": cid, "battlegroupId": GROUP, "worldId": DD, "layout": LAYOUT,
            "x": x, "y": y, "z": 0.0, "ageS": age}


def sighting(cls, x, age, marker, size=None):
    return {"cls": cls, "type": marker, "x": x, "y": 0.0, "z": 0.0, "fill": 0.8, "size": size, "rowKey": None, "distCm": None,
            "ageS": age, "observerName": "Leto Test", "observerKey": "beta0002", "observers": ["beta0002"], "rev": 1,
            "worldId": DD, "layout": LAYOUT}


def snapshot(tmp_path) -> dict:
    """A real snapshot: synced with a valid key, us plus two guildmates online (one hidden from
    the minimap), a fresh, a middle-aged and an old find, and a live iron node."""
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    det.set_sync_key(KEY)
    session(det, DD)
    det.map_files[det.store.world] = LAYOUT
    det.handle(BusMessage("delta", "dune.trackedSession", ME), 0)
    pose(det, 0, 0, t=1)
    det.ingest_peer({"now": time.time(), "next": 5, "keyState": "valid",
                     "presence": [member(1001, "Paul Test", 0.0, 0.0), member(1002, "Chani Test", 150000.0, 0.0),
                                  member(1003, "Stilgar Test", 5000000.0, 5000000.0)],
                     "sightings": [sighting("spice", 1000.0, 60.0, "spicefieldlarge", "large"),
                                   sighting("blow", 2000.0, 1500.0, "spicefieldmedium", "medium"),
                                   sighting("ship", 3000.0, 1700.0, "downedship"),
                                   sighting("ore", 4000.0, 900.0, "magnetiteore")]}, time.time())
    det.peers.connected_as = "Paul Test"  # what the sync loop records after an accepted exchange
    det.ui("sync_member", "1003")  # Stilgar stays off my minimap
    return det.snapshot()


def run(snap: dict, steps: str) -> dict:
    html = resources.files("harvesting_dune").joinpath("viewer.html").read_text("utf8")
    script = html[html.rindex("<script>") + len("<script>"):html.rindex("</script>")]
    code = (HARNESS.replace("STEPS", steps).replace("SNAPSHOT", json.dumps(snap))
            .replace("SCRIPT", json.dumps(script)))  # the page script last: its text is never scanned for placeholders
    out = subprocess.run(["node", "-"], input=code, capture_output=True, encoding="utf-8", timeout=30, check=False)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.splitlines()[-1])


def test_guild_section_shows_status_roster_and_finds_from_a_real_snapshot(tmp_path):
    log = run(snapshot(tmp_path), """
      page.render(SNAP);
      log.sum = el("sum-guild").textContent; log.status = el("gStatus").textContent;
      log.members = el("gMembers").innerHTML; log.finds = el("gSightings").innerHTML;
      log.pause = el("gPause").checked; log.positions = el("gPos").checked;
      log.formHidden = el("gKeyForm").hidden; log.savedHidden = el("gKeySaved").hidden;
    """)
    assert "2 online" in log["sum"] and "4 shared" in log["sum"]
    assert log["status"].startswith("connected as Paul Test")
    assert "Chani Test" in log["members"] and "Stilgar Test" in log["members"]
    assert 'data-gmember="1002" checked' in log["members"]  # shown by default
    assert 'data-gmember="1003"' in log["members"] and 'data-gmember="1003" checked' not in log["members"]  # hidden
    assert "(you)" in log["members"] and 'data-gmember="1001"' not in log["members"]  # you are never drawn
    assert "fresh" in log["finds"] and "old" in log["finds"] and "downed ship" in log["finds"] and "Leto Test" in log["finds"]
    assert '<td>Iron Ore · live</td><td class="nw">15 min</td>' in log["finds"]  # its age against the 30 min respawn
    assert log["pause"] is False and log["positions"] is True
    assert log["formHidden"] is True and log["savedHidden"] is False  # a saved key shows only as "key saved"


def test_a_rejected_key_is_said_in_plain_words(tmp_path):
    log = run(snapshot(tmp_path), """
      page.render({ ...SNAP, peers: { ...SNAP.peers, keyState: "invalid" } });
      log.status = el("gStatus").textContent; log.sum = el("sum-guild").textContent;
    """)
    assert "rejected" in log["status"] and "rejected" in log["sum"]


def test_an_older_detector_without_peers_still_renders(tmp_path):
    log = run(snapshot(tmp_path), """
      const old = { ...SNAP }; delete old.peers;
      page.render(old);
      log.sum = el("sum-guild").textContent; log.target = el("tName").textContent;
    """)
    assert log["sum"] == "" and log["target"]  # the rest of the page rendered


def test_the_key_goes_out_once_in_a_json_body_and_nowhere_else(tmp_path):
    log = run(snapshot(tmp_path), """
      page.render(SNAP);
      el("gkey").value = "   "; el("gSave").onclick();
      log.emptyPosts = calls.filter((c) => c.url === "/sync/key").length;
      el("gkey").value = " " + KEY + "\\n"; el("gSave").onclick(); await tick();
      log.posts = calls.filter((c) => c.url === "/sync/key");
      log.inputAfter = el("gkey").value;
      el("gRemove").onclick(); await tick();
      log.removeBody = calls[calls.length - 1].body;
      const shown = Object.values(els).map((e) => e.innerHTML + e.textContent + e.value);
      log.keyElsewhere = [...calls.map((c) => c.url), ...stored, ...shown].some((t) => String(t).includes(KEY));
    """.replace("KEY", json.dumps(KEY)))
    assert log["emptyPosts"] == 0  # nothing to save, nothing sent
    assert len(log["posts"]) == 1 and log["posts"][0]["method"] == "POST"
    assert json.loads(log["posts"][0]["body"]) == {"key": KEY}  # trimmed, in the body only
    assert log["inputAfter"] == "" and log["removeBody"] == "{}"
    assert log["keyElsewhere"] is False


def test_a_roster_tick_box_toggles_that_guildmate_on_my_minimap(tmp_path):
    log = run(snapshot(tmp_path), """
      page.render(SNAP);
      fire("change", { dataset: { gmember: "1003" } });
      log.urls = calls.map((c) => c.url);
    """)
    assert "/control?action=sync_member&value=1003" in log["urls"]


def characters(tmp_path) -> tuple[dict, dict, dict]:
    """A real snapshot plus the /character answers for a guildmate with a full profile and for us,
    whose companion has reported nothing but the session."""
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    det.set_sync_key(KEY)
    session(det, DD)
    det.map_files[det.store.world] = LAYOUT
    det.handle(BusMessage("delta", "dune.trackedSession", ME), 0)
    det.items.items["unique_regis_rifle"] = {"name": "Regis Rifle", "group": "Uniques & schematics", "schematic": True}
    chani = {"status": {"worldId": DD, "worldName": "Deep Desert", "activity": "Driving", "inGame": True},
             "skills": {"gameMode": "online", "skills": [["Skill.Trooper.Rifle", 3]], "abilities": ["Shield Wall"], "techniques": []},
             "stats": [{"name": "Hydration", "entries": [["WaterCapacity", 2500.5]]}],
             "recipes": [["unique_regis_rifle", 3], ["unique_maula_pistol", 2]],
             "landsraad": {"term": "Term 4", "houses": [{"house": "house_hagal", "specialization": None, "progress": 1200,
                                                         "max": 5000, "earned": 1, "rewards": 2}]},
             "achievements": {"completed": ["first_blood"], "progress": [["spice_baron", 0.4]], "seen": 2},
             "session": None}
    det.ingest_peer({"now": time.time(), "next": 1, "keyState": "valid", "sightings": [],
                     "presence": [member(1002, "Chani Test", 150000.0, 0.0)]}, time.time())
    det.ingest_profiles({"now": 0, "next": 1, "ids": [1002],
                         "profiles": [{"characterId": 1002, "characterName": "Chani Test", "rev": 1, "ageS": 300, "profile": chani}]},
                        time.time())
    return det.snapshot(), det.character_detail(1002), det.character_detail(1001)


CHAR_STEPS = """
  globalThis.fetch = async (url, opts = {}) => {
    calls.push({ url, method: opts.method || "GET" });
    const id = /id=(\\d+)/.exec(url);
    return id ? new Response(JSON.stringify(id[1] === "1002" ? CHANI : MINE), { status: 200 }) : new Response(null, { status: 204 });
  };
  page.render(SNAP); await tick(); await tick();
  log.mine = el("cDetail").innerHTML;
  docListeners.click.forEach((fn) => fn({ target: { closest: () => ({ dataset: { char: "1002" } }) } }));
  await tick(); await tick();
  log.rows = el("cRows").innerHTML; log.detail = el("cDetail").innerHTML; log.recipes = el("cRecipes").innerHTML;
  el("cRecipeQ").oninput({ target: { value: "regis rifel" } });
  log.found = el("cRecipes").innerHTML;
  log.sum = el("sum-chars").textContent; log.share = el("cShare").checked;
  log.urls = calls.map((c) => c.url);
"""


def test_the_characters_view_lists_the_guild_and_shows_a_profile(tmp_path):
    snap, chani, mine = characters(tmp_path)
    log = run(snap, CHAR_STEPS.replace("CHANI", json.dumps(chani)).replace("MINE", json.dumps(mine)))
    assert "Paul Test" in log["rows"] and "(you)" in log["rows"] and 'data-char="1002" class="sel"' in log["rows"]
    assert "<td>Driving</td>" in log["rows"] and "Deep Desert" in log["rows"]
    assert log["mine"].count("not reported yet") == 4  # our companion sent no skills, stats, Landsraad or achievements yet
    assert "Rifle" in log["detail"] and "Hydration" in log["detail"] and "1200 / 5000" in log["detail"]
    assert "1 completed of 2 seen" in log["detail"] and "not shared" in log["detail"]  # Chani keeps her session private
    assert "Regis Rifle" in log["recipes"] and "maula pistol" in log["recipes"]
    assert "Regis Rifle" in log["found"] and "maula" not in log["found"]  # typo-tolerant search
    assert log["sum"].startswith("1 guildmate") and log["share"] is True
    assert "/control?action=guild_view&value=characters" in log["urls"] and "/character?id=1002" in log["urls"]



LIVE_SKILLS = json.loads((Path(__file__).parent / "fixtures" / "profile-skills-live.json").read_text(encoding="utf-8"))


def test_real_skill_tags_are_grouped_and_readable(tmp_path):
    """The companion's own tags (captured live): grouped by their middle part, named from the last."""
    snap, chani, mine = characters(tmp_path)
    chani = {**chani, "profile": {**chani["profile"], "skills": LIVE_SKILLS}}
    log = run(snap, CHAR_STEPS.replace("CHANI", json.dumps(chani)).replace("MINE", json.dumps(mine)))
    detail = log["detail"]
    for group in ("Ability", "Attribute", "Perk"):
        assert f'<tr class="sec-row"><td colspan="2">{group}</td></tr>' in detail
    assert "<td>Cable Pull</td>" in detail and "<td>Suspensor Grenade Amplification</td>" in detail
    assert "<td>Aggression 3</td>" in detail and "Skills." not in detail
    assert "Abilities: Crippling Strike, Cable Pull, Weirding Step" in detail and "Techniques: Regen Cap" in detail


def test_roster_and_characters_show_presence_states_grouped_by_player(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    det.set_sync_key(KEY)
    session(det, DD)
    det.map_files[det.store.world] = LAYOUT
    det.handle(BusMessage("delta", "dune.trackedSession", ME), 0)
    rows = [{**member(1002, "Chani Test", 150000.0, 0.0), "state": "online"},
            {**member(1003, "Stilgar Test", 1000.0, 0.0, age=150.0), "state": "late"},
            {**member(1004, "Leto Test", 2000.0, 0.0, age=300.0), "state": "offline"},
            {**member(1005, "Irulan Test", None, None), "state": "in_menu"},
            {**member(1006, "Chani Alt", 3000.0, 0.0, age=600.0), "state": "offline"}]
    det.ingest_peer({"now": time.time(), "next": 1, "keyState": "valid", "sightings": [], "presence": rows}, time.time())
    status = {"worldId": DD, "worldName": "Deep Desert", "activity": "Driving", "inGame": True}
    blank = {"status": status, "skills": None, "stats": None, "recipes": None, "landsraad": None, "achievements": None, "session": None}
    owners = {1001: "0a0a0a0a", 1002: "1b1b1b1b", 1006: "1b1b1b1b", 1003: "2c2c2c2c", 1004: "3d3d3d3d"}  # playerTag
    det.ingest_profiles({"now": 0, "next": 1, "ids": list(owners), "profiles": [
        {"characterId": cid, "characterName": n, "rev": 1, "ageS": 60, "profile": blank, "playerTag": owners[cid]}
        for cid, n in ((1001, "Paul Test"), (1002, "Chani Test"), (1006, "Chani Alt"), (1003, "Stilgar Test"), (1004, "Leto Test"))]},
        time.time())
    log = run(det.snapshot(), """
      page.render(SNAP);
      log.members = el("gMembers").innerHTML; log.sum = el("sum-guild").textContent; log.rows = el("cRows").innerHTML;
    """)
    m = log["members"]
    assert m.index("Chani Test") < m.index("Irulan Test") < m.index("Stilgar Test") < m.index("Leto Test")
    assert ">online<" in m and ">in menu<" in m and "last seen 3 min ago" in m and "left 5 min ago" in m
    assert '<tr class="no"><td><input type="checkbox" data-gmember="1004"' in m  # offline: greyed
    assert "(you)" in m and ">you<" in m and "2 online" in log["sum"]  # online and in menu are playing
    r = log["rows"]
    head = '<tr class="sec-row"><td colspan="5">Chani Test · Chani Alt</td></tr>'
    assert r.count(head) == 1 and r.count("sec-row") == 1  # one heading, made of names only, for the player with an alt
    order = ["(you)", head, 'data-char="1002"', 'data-char="1006"', "Stilgar Test", "Leto Test", "Irulan Test"]
    assert [r.index(t) for t in order] == sorted(r.index(t) for t in order)  # you, then online first; no tag: last
    assert not [t for t in owners.values() if t in r]  # the tag itself is never shown
