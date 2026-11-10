"""The settings page's Guild section, run in Node against a real detector snapshot: status words,
the roster with its tick boxes, shared finds (live ore too), the Pins block (your own pins with Delete,
the hotkey label, the drop field), the Characters view, and a key that only ever travels once, in the
body of POST /sync/key. Also the key paths of snapshot() and page_state() that the
open pages read."""

import json
import shutil
import subprocess
import time
from pathlib import Path
from importlib import resources

import pytest
from helpers import CELL_CM, actors, pose, session, spice

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


def shared(oid, kind, cid, name, tag, body, x=None, y=None, scope=True, ttl=7200, age=0, rev=1):
    """One object as the server echoes it (docs/SYNC-PROTOCOL.md "Objects"); no x = positionless."""
    return {"id": oid, "kind": kind, "scope": {"worldId": DD, "layout": LAYOUT} if scope else None,
            "geometry": None if x is None else {"type": "point", "x": x, "y": y, "z": 0.0}, "body": body,
            "ttlS": ttl, "ageS": age, "characterId": cid, "characterName": name, "playerTag": tag, "rev": rev}


def snapshot(tmp_path, label="IRON HERE") -> dict:
    """A real snapshot: synced with a valid key, us plus two guildmates online (one hidden from
    the minimap), a fresh, a middle-aged and an old find, a live iron node, a companion destination,
    and four shared objects: two pins of ours (the newer one where we stand, the older one at the
    destination), a guildmate's pin and a positionless note."""
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    det.set_sync_key(KEY)
    session(det, DD)
    det.map_files[det.store.world] = LAYOUT
    det.handle(BusMessage("delta", "dune.trackedSession", ME), 0)
    pose(det, 0, 0, t=1)
    det.handle(BusMessage("delta", "dune.trackedDestination", {"hasDestination": True, "x": 250000.0, "y": 100000.0, "z": 0.0}), 1)
    det.ingest_peer({"now": time.time(), "next": 5, "keyState": "valid",
                     "presence": [member(1001, "Paul Test", 0.0, 0.0), member(1002, "Chani Test", 150000.0, 0.0),
                                  member(1003, "Stilgar Test", 5000000.0, 5000000.0)],
                     "sightings": [sighting("spice", 1000.0, 60.0, "spicefieldlarge", "large"),
                                   sighting("blow", 2000.0, 1500.0, "spicefieldmedium", "medium"),
                                   sighting("ship", 3000.0, 1700.0, "downedship"),
                                   sighting("ore", 4000.0, 900.0, "magnetiteore")],
                     "objects": [shared("p1", "pin", 1001, "Paul Test", "0a0a0a0a", {"label": label}, 0.0, 0.0, rev=1),
                                 shared("p2", "pin", 1001, "Paul Test", "0a0a0a0a", {"label": "MEET HERE"}, 250000.0, 100000.0,
                                        ttl=2700, age=4500, rev=2),
                                 shared("c1", "pin", 1002, "Chani Test", "1b1b1b1b", {"label": "SPICE"}, 150000.0, 0.0, rev=3),
                                 shared("n1", "note", 1002, "Chani Test", "1b1b1b1b", {"text": "meet at the blow"}, scope=False, rev=4)]},
                    time.time())
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
    assert "fresh" in log["finds"] and "old" in log["finds"] and "Downed ship" in log["finds"] and "Leto Test" in log["finds"]
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


def test_the_pins_block_lists_my_pins_with_delete_and_the_hotkey_label(tmp_path):
    snap = snapshot(tmp_path)
    log = run(snap, """
      page.render(SNAP);
      log.hidden = el("gPins").hidden; log.mine = el("gMine").innerHTML; log.preset = el("gPinPreset").value;
      log.where = el("gPinWhere").textContent; log.note = el("gPinNote").textContent; log.sum = el("sum-guild").textContent;
      const o = SNAP.peers.objects, withObjects = (more) => ({ ...SNAP, peers: { ...SNAP.peers, objects: { ...o, ...more } } });
      page.render(withObjects({ pending: 1 })); log.pending = el("gPinNote").textContent;
      page.render(withObjects({ pending: 1, note: "wait a moment" })); log.refused = el("gPinNote").textContent;
      page.render(withObjects({ mine: [], here: 0, where: null })); log.none = el("gMine").innerHTML;
      log.sumNone = el("sum-guild").textContent; log.whereNone = el("gPinWhere").textContent;
      document.activeElement = el("gPinPreset"); el("gPinPreset").value = "ME"; page.render(SNAP);
      log.typing = el("gPinPreset").value;
    """)
    o = snap["peers"]["objects"]
    assert log["hidden"] is False and log["preset"] == "PIN" and log["note"] == ""
    mine = log["mine"]
    assert mine.count("data-gobjdel=") == 2 and 'data-gobjdel="p1"' in mine and 'data-gobjdel="p2"' in mine
    assert "IRON HERE" in mine and "MEET HERE" in mine
    assert f"<td>Deep Desert {o['mine'][0]['sector']}</td>" in mine and '<td class="nw">2.0 h</td>' in mine  # the fresh pin
    assert f"<td>Deep Desert {o['mine'][1]['sector']}</td>" in mine and '<td class="nw">45 min</td>' in mine  # the older one
    assert "SPICE" not in mine and "Chani" not in mine and "meet at the blow" not in mine  # only yours are listed
    assert log["where"] == "at your companion destination" and log["whereNone"] == "no position yet"
    assert log["sum"].endswith(f"{o['here']} pins") and o["here"] == 3 and "pins" not in log["sumNone"]
    assert log["pending"] == "sending 1\u2026" and log["refused"] == "wait a moment"  # the detector's reason wins
    assert "data-gobjdel" not in log["none"] and 'class="note"' in log["none"]
    assert log["typing"] == "ME"  # a render never overwrites what you are typing


def test_drop_pin_posts_the_label_once_and_ignores_blanks(tmp_path):
    log = run(snapshot(tmp_path), """
      page.render(SNAP);
      el("gPinText").value = "  IRON HERE "; el("gPinDrop").onclick(); await tick();
      log.afterClick = el("gPinText").value;
      el("gPinText").value = "   "; el("gPinDrop").onclick(); await tick();
      el("gPinText").value = "MEET"; el("gPinText").onkeydown({ key: "Enter", preventDefault() {} }); await tick();
      el("gPinText").value = "x"; el("gPinText").onkeydown({ key: "a", preventDefault() {} }); await tick();
      el("gPinPreset").onchange({ target: { value: " MEET " } }); el("gPinPreset").onchange({ target: { value: "  " } }); await tick();
      log.pins = calls.filter((c) => c.url.includes("action=object_pin")).map((c) => [c.url, c.method]);
      log.presets = calls.filter((c) => c.url.includes("action=pin_label")).map((c) => c.url);
    """)
    assert log["pins"] == [["/control?action=object_pin&value=IRON%20HERE", "POST"], ["/control?action=object_pin&value=MEET", "POST"]]
    assert log["afterClick"] == ""  # the field is ready for the next pin
    assert log["presets"] == ["/control?action=pin_label&value=MEET"]


def test_delete_posts_object_delete_with_the_row_id(tmp_path):
    log = run(snapshot(tmp_path), """
      page.render(SNAP);
      const hit = (sel) => sel.includes("[data-gobjdel]") ? { dataset: { gobjdel: "p2" } } : null;
      docListeners.click.forEach((fn) => fn({ target: { closest: hit } }));
      await tick();
      log.deletes = calls.filter((c) => c.url.includes("action=object_delete")).map((c) => [c.url, c.method]);
    """)
    assert log["deletes"] == [["/control?action=object_delete&value=p2", "POST"]]


def test_an_older_detector_without_objects_hides_the_block(tmp_path):
    log = run(snapshot(tmp_path), """
      const old = { ...SNAP, peers: { ...SNAP.peers } }; delete old.peers.objects;
      page.render(old);
      log.hidden = el("gPins").hidden; log.sum = el("sum-guild").textContent; log.members = el("gMembers").innerHTML;
    """)
    assert log["hidden"] is True and "pins" not in log["sum"]
    assert "Chani Test" in log["members"] and "4 shared" in log["sum"]  # the rest of the section rendered


def test_peer_text_is_escaped_in_the_pins_block(tmp_path):
    log = run(snapshot(tmp_path, label="<img src=x>"), """
      page.render(SNAP);
      log.mine = el("gMine").innerHTML;
      log.raw = Object.values(els).some((e) => String(e.innerHTML + e.textContent + e.value).includes("<img"));
    """)
    assert "&lt;img src=x&gt;" in log["mine"] and log["raw"] is False


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


def test_a_setting_the_detector_refuses_says_why_instead_of_reload_the_page(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    session(det, DD)  # on a map, but the companion sent no position yet
    assert det.ui("base") is False  # server.py answers 400 for this
    log = run(det.snapshot(), """
      globalThis.fetch = async (url) => { calls.push({ url }); return new Response(null, { status: 400 }); };
      page.render(SNAP);
      el("base").onclick(); await tick(); await tick();
      log.saved = el("saved").textContent;
    """)
    assert log["saved"].startswith("Not applied") and "Reload" not in log["saved"] and "Ctrl+F5" not in log["saved"]


def test_a_finished_crawl_that_found_nothing_is_not_shown_as_building(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    raw = json.dumps({"built": time.time() * 1000, "materials": [], "uniques": [], "containers": []})
    det.loot.ingest_crawl(raw)  # the two calls runner.refresh_loot_index makes
    det.items.ingest(json.loads(raw))
    snap = Detector(None, data_dir=tmp_path, min_rescore_s=0).snapshot()  # after a restart: the files load back
    log = run(snap, """
      page.render(SNAP);
      log.browse = el("browse").innerHTML; log.cages = el("cages").innerHTML; log.sum = el("sum-loot").textContent;
    """)
    assert "Building" not in log["browse"] and "Building" not in log["cages"] and "building" not in log["sum"]


def test_a_profile_update_keeps_the_caret_in_the_schematic_search(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    det.set_sync_key(KEY)
    session(det, DD)
    det.map_files[det.store.world] = LAYOUT
    det.handle(BusMessage("delta", "dune.trackedSession", ME), 0)
    det.ingest_peer({"now": time.time(), "next": 1, "keyState": "valid", "sightings": [],
                     "presence": [member(1002, "Chani Test", 150000.0, 0.0)]}, time.time())

    def profile(rev, water):
        prof = {"status": {"worldId": DD, "worldName": "Deep Desert", "activity": "Driving", "inGame": True},
                "skills": None, "stats": [{"name": "Hydration", "entries": [["WaterCapacity", water]]}],
                "recipes": [["unique_regis_rifle", 3]], "landsraad": None, "achievements": None, "session": None}
        det.ingest_profiles({"now": 0, "next": 1, "ids": [1002], "profiles": [
            {"characterId": 1002, "characterName": "Chani Test", "rev": rev, "ageS": 30, "profile": prof}]}, time.time())
        return det.snapshot(), det.character_detail(1002)

    snap1, d1 = profile(1, 2500.0)
    snap2, d2 = profile(2, 2600.0)  # the guildmate's profile changed while we type
    log = run(snap1, """
      let cur = D1;
      globalThis.fetch = async (url) => { calls.push({ url });
        return url.startsWith("/character?id=1002") ? new Response(JSON.stringify(cur), { status: 200 }) : new Response(null, { status: 204 }); };
      // like a browser: new markup makes new elements, and the focused one is gone with its focus
      const fresh = () => { delete els.cRecipeQ; const q = el("cRecipeQ");
        q.focus = () => { document.activeElement = q; }; q.setSelectionRange = (a, b) => { q.caret = [a, b]; }; };
      let html = "", rebuilt = 0;
      Object.defineProperty(el("cDetail"), "innerHTML", { get: () => html, set: (v) => { html = v; rebuilt++; fresh(); document.activeElement = null; } });
      page.render(SNAP); await tick(); await tick();
      docListeners.click.forEach((fn) => fn({ target: { closest: (s) => s.includes("tr[data-char]") ? { dataset: { char: "1002" } } : null } }));
      await tick(); await tick();
      const q = el("cRecipeQ"); q.value = "regis"; q.selectionStart = q.selectionEnd = 3; q.oninput({ target: q }); q.focus();
      const before = rebuilt; cur = D2;
      page.render(SNAP2); await tick(); await tick();
      log.rebuilt = rebuilt - before; log.focused = document.activeElement === el("cRecipeQ");
      log.value = el("cRecipeQ").value; log.caret = el("cRecipeQ").caret;
    """.replace("D1", json.dumps(d1)).replace("D2", json.dumps(d2)).replace("SNAP2", json.dumps(snap2)))
    assert log["rebuilt"] == 1  # the new profile is shown
    assert log["focused"] is True and log["value"] == "regis" and log["caret"] == [3, 3]


def test_gaming_tools_vendor_price_and_stock_are_escaped(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    session(det, DD)
    det.items.ingest({"built": time.time() * 1000, "materials": [
        {"id": "x", "name": "Test Part", "categories": ["items/misc", "items/misc/components"],
         "sold": [["Vendor", 100, 9]], "drops": []}]})
    assert det.ui("resource", "item:x")
    snap = det.snapshot()
    snap["itemInfo"]["vendors"] = [{"name": "Vendor", "price": "<img src=x>", "stock": "<b>9</b>"}]  # crawled data is not typed
    log = run(snap, """
      page.render(SNAP);
      log.info = el("itemInfo").innerHTML;
    """)
    assert "&lt;img src=x&gt; Solari" in log["info"] and "&lt;b&gt;9&lt;/b&gt; in stock" in log["info"]
    assert "<img" not in log["info"] and "<b>" not in log["info"]


def test_gaming_tools_cage_drop_amounts_are_escaped(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    session(det, DD)
    snap = det.snapshot()
    snap["lootIndex"] = {"cages": 1, "builtAt": time.time()}
    drop = {"name": "Part", "icon": None, "rarity": "rare", "chance": 0.5, "special": False, "schematic": False,
            "min": "<b>2</b>", "max": "<i>5</i>"}  # crawled data is not typed
    snap["loot"] = [{"kind": "Rare", "distanceM": 100, "heightM": 0, "known": True, "drops": [drop], "moreDrops": 0,
                     "bestRarity": "rare", "specialChance": 0}]
    log = run(snap, """
      page.render(SNAP);
      log.cages = el("cages").innerHTML;
    """)
    assert "&lt;b&gt;2&lt;/b&gt;&ndash;&lt;i&gt;5&lt;/i&gt;" in log["cages"]
    assert "<b>" not in log["cages"] and "<i>" not in log["cages"]


# Key paths the settings page and bridge.js read: a list element is written '[]'. Frozen from the code before the
# audit; adding a key passes, renaming or removing one breaks a page that is already open (src/harvesting_dune/AGENTS.md).
SNAPSHOT_KEYS = (
    "about", "about.platform", "about.python", "about.version", "activity", "areas", "areas[]", "areas[].detail",
    "areas[].distance_m", "areas[].expected", "areas[].feasible", "areas[].live", "areas[].nodes", "areas[].reasons",
    "areas[].reasons[]", "areas[].trip_s", "areas[].x", "areas[].y", "base", "blows", "blows[]", "blows[].ageMin",
    "blows[].distanceM", "blows[].size", "blows[].t", "blows[].x", "blows[].y", "blows[].z", "characterState", "characters",
    "characters.rows", "characters.rows[]", "characters.rows[].activity", "characters.rows[].id", "characters.rows[].name",
    "characters.rows[].player", "characters.rows[].profileAgeS", "characters.rows[].rev", "characters.rows[].seenS",
    "characters.rows[].self", "characters.rows[].state", "characters.rows[].world", "characters.share",
    "characters.shareSession", "connected", "detectionRangeM", "enabled", "farm", "farmRoutes", "gate", "gate.open",
    "gate.reason", "gate.text", "groupOrder", "groupOrder[]", "hotspot", "hotspot.cell", "hotspot.nodes", "hotspot.x",
    "hotspot.y", "hotspot.zone", "itemGroups", "itemIndex", "itemIndex.builtAt", "itemIndex.items", "itemInfo", "learned",
    "learned.gridFitted", "learned.paceSamples", "learned.secondsPerNode", "loot", "lootIndex", "lootIndex.builtAt",
    "lootIndex.cages", "maxKm", "mined", "mined.count", "mined.nextBackMin", "minimap", "minimap.extras", "minimap.extras[]",
    "minimap.extras[][]", "minimap.items", "minimap.items[]", "minimap.items[][]", "minimap.lines", "minimap.linesActive",
    "minimap.visibleCount", "peers", "peers.connectedAs", "peers.enabled", "peers.hiddenMembers", "peers.identity",
    "peers.identity.battlegroupId", "peers.identity.characterName", "peers.keySet", "peers.keyState", "peers.lastError",
    "peers.lastSyncAgeS", "peers.members", "peers.members[]", "peers.members[].ageS", "peers.members[].characterId",
    "peers.members[].distanceM", "peers.members[].name", "peers.members[].sector", "peers.members[].self",
    "peers.members[].shown", "peers.members[].state", "peers.members[].worldId", "peers.needsTracking", "peers.objects",
    "peers.objects.all", "peers.objects.here", "peers.objects.hotkeyLabel", "peers.objects.mine", "peers.objects.note",
    "peers.objects.pending", "peers.objects.where", "peers.positions", "peers.server", "peers.sightings", "peers.syncError",
    "planVehicle", "pose", "pose.x", "pose.y", "pose.z", "problems", "problems.count", "problems.items", "resource",
    "resources", "resources[]", "resources[][]", "scanRows", "scanStatus", "scoreMs", "ships", "shipsReported", "shipwrecks",
    "stale", "status", "tracked", "vehicle", "vehicleOverride", "viewers", "world", "worldName", "zones", "zones.here",
    "zones.lastPveRow", "zones.linesFromRow", "zones.pvpOnly", "zones.pvpYield",
)
PAGE_STATE_KEYS = (
    "alternatives", "alternatives[]", "alternatives[].color", "alternatives[].kind", "alternatives[].size",
    "alternatives[].weight", "alternatives[].x", "alternatives[].y", "dest", "dest.detail", "dest.hasDestination", "dest.title",
    "dest.x", "dest.y", "dest.z", "extraActors", "extraActors[]", "extraActors[].actorType", "extraActors[].fillRate",
    "extraActors[].id", "extraActors[].markerType", "extraActors[].reportable", "extraActors[].resourceItemId",
    "extraActors[].spawned", "extraActors[].spriteKey", "extraActors[].x", "extraActors[].y", "extraActors[].z", "objects",
    "problem", "route", "route.mapId", "route.stops", "route.stops[]", "route.stops[].houseName", "route.stops[].x",
    "route.stops[].y", "route.stops[].z", "stale",
)


def key_paths(obj, prefix="") -> set[str]:
    if isinstance(obj, list):  # an empty list has no element path: its element keys cannot be checked
        return set().union(*({prefix + "[]"} | key_paths(v, prefix + "[]") for v in obj))
    if isinstance(obj, dict):
        return set().union(*({f"{prefix}.{k}".lstrip(".")} | key_paths(v, f"{prefix}.{k}".lstrip(".")) for k, v in obj.items()))
    return set()


def ranked(tmp_path) -> Detector:
    """Every list the pages read holds an element: synced guild identity, a pose, a spice blow, a field
    remembered out of range, and a spice ranking with alternatives."""
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    det.set_sync_key(KEY)
    session(det, DD)
    det.map_files[det.store.world] = LAYOUT
    det.handle(BusMessage("delta", "dune.trackedSession", ME), 0)
    pose(det, 10, 0, t=1)
    large = (spice("L1", 30 * CELL_CM, 0, "Large", fill=0.9), spice("L2", -45 * CELL_CM, 0, "Large", fill=0.7))
    actors(det, *large, spice("B1", 10 * CELL_CM, 0, "Medium", fill=0, spawned=False),
           spice("R1", 20 * CELL_CM, 5 * CELL_CM, "Medium", fill=0.7), world=DD, t=1)
    actors(det, *large, spice("B1", 10 * CELL_CM, 0, "Medium", fill=0.05), world=DD, t=2)  # B1 blows, R1 is left behind
    det.ui("resource", "spice")
    det.compute(3)
    return det


def test_snapshot_keeps_every_key_an_open_settings_page_reads(tmp_path):
    assert sorted(set(SNAPSHOT_KEYS) - key_paths(ranked(tmp_path).snapshot())) == []


def test_page_state_keeps_every_key_bridge_js_reads(tmp_path):
    assert sorted(set(PAGE_STATE_KEYS) - key_paths(ranked(tmp_path).page_state())) == []
