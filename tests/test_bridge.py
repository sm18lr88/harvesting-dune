"""Runs bridge.js in Node against a fake companion page (chrome.webview + page listener)."""

import json
import shutil
import subprocess

import pytest

from harvesting_dune.feed import BRIDGE_SCRIPT, TOPICS

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

HARNESS = r"""
const posted = [], emitted = [], listeners = [];
const wv = {
  postMessage: (m) => posted.push(m),
  addEventListener: (_, fn) => listeners.push(fn),
  dispatchEvent: (e) => { for (const fn of [...listeners]) fn(e); return true; },
};
// the companion page's own bus listener: remembers the last state per topic, like its client
const page = {};
wv.addEventListener('message', (e) => { const m = e.data; if (m && m.topic) page[m.topic] = m.type === 'topicReset' ? undefined : m.payload; });
globalThis.window = globalThis;
globalThis.chrome = { webview: wv };
globalThis.location = { pathname: PATH };
globalThis.document = { getElementById: () => null, body: { appendChild() {} }, documentElement: { appendChild() {} }, createElement: () => ({ style: {}, dataset: {}, append() {}, appendChild() {} }) };
globalThis.__harvestingDuneEmit = (s) => emitted.push(JSON.parse(s));
const scanRows = [{ type: 'SpiceField', x: 100.4, y: 200.9, z: 0 }, { type: 'SpiceField', x: 5, y: 6, z: 0 }];
globalThis.fetch = async (url) => new Response(JSON.stringify(url.includes('dune-api-v2') ? scanRows : {}), { status: 200 });
const native = (topic, payload) => wv.dispatchEvent({ data: { type: 'delta', topic, payload } });
const tick = () => new Promise((r) => setTimeout(r, 5));
const log = {};
(async () => {
  log.first = eval(SCRIPT); log.second = eval(SCRIPT);
  __dd.primary = true;
  native('dune.map.actors', { worldId: 'DD', actors: [{ id: 1 }] });
  native('palia.status', {});
  const DEST = { hasDestination: true, x: 1, y: 2, z: 3, title: 'Iron' };
  const ROUTE = { mapId: 'DD', stops: [{ x: 1, y: 2, z: 3 }, { x: 4, y: 5, z: 6 }] };
  __dd.set({ dest: DEST, route: ROUTE, stale: ['SpiceField:100:200'], extraActors: [{ id: 'dd-stale', spawned: false }] });
  log.afterSet = JSON.parse(JSON.stringify(page));
  native('dune.trackedDestination', { hasDestination: false });   // native "no destination" must not wipe ours
  native('dune.map.actors', { worldId: 'DD', actors: [{ id: 2 }] }); // our overlay actor is merged back in
  await tick();
  log.afterNative = JSON.parse(JSON.stringify(page));
  native('dune.trackedDestination', { hasDestination: true, x: 9, y: 9, z: 9, title: 'player pick' }); // player's own wins
  await tick();
  log.playerDest = page['dune.trackedDestination'];
  native('dune.trackedDestination', { hasDestination: false });
  native('dune.landsraad.routeMode', { enabled: true });  // Landsraad route mode in use: leave it alone
  native('dune.map.route', { mapId: 'DD', stops: [{ x: 7, y: 7, z: 7 }] });
  await tick();
  log.landsraad = { mode: page['dune.landsraad.routeMode'], route: page['dune.map.route'], dest: page['dune.trackedDestination'] };
  const res = await fetch('https://dune-api-v2.gaming.tools/actors?world=deepdesert_1&seed=7');
  log.filtered = await res.json();
  log.fetchScan = JSON.parse(await __dd.fetchScan('deepdesert_1', 7));
  __dd.set({});
  log.cleared = { dest: page['dune.trackedDestination'] };
  wv.postMessage({ type: 'unsubscribe', payload: { topics: ['dune.map.actors', 'dune.bases'] } });
  log.pingAfterSet = __dd.ping();
  console.log(JSON.stringify({ log, posted, emitted }));
  process.exit(0);
})();
"""


def run(path="/dune/minimap"):
    code = HARNESS.replace("SCRIPT", json.dumps(BRIDGE_SCRIPT)).replace("PATH", json.dumps(path))
    out = subprocess.run(["node", "-"], input=code, capture_output=True, text=True, timeout=30, check=False)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.splitlines()[0])


def test_bridge_end_to_end_in_fake_page():
    r = run()
    log = r["log"]
    assert log["first"] == "installed" and log["second"] == "already"
    assert r["posted"][0] == {"type": "subscribe", "payload": {"topics": [*TOPICS, "dune.overlayWidgets"]}}
    # forwarding: only wanted topics, from the primary page, never our own synthetic ones
    topics = [m.get("topic") or m.get("type") for m in r["emitted"]]
    assert "palia.status" not in topics and topics.count("dune.map.actors") == 2
    assert "scanWorld" in topics
    # our destination / route are rendered by the page's own listener
    assert log["afterSet"]["dune.trackedDestination"]["title"] == "Iron"
    assert log["afterSet"]["dune.landsraad.routeMode"] == {"enabled": True}
    assert len(log["afterSet"]["dune.map.route"]["stops"]) == 2
    # native updates do not wipe ours; overlay actors are merged into the live list
    assert log["afterNative"]["dune.trackedDestination"]["title"] == "Iron"
    assert [a["id"] for a in log["afterNative"]["dune.map.actors"]["actors"]] == [2, "dd-stale"]
    # the player's own destination and Landsraad routes always win
    assert log["playerDest"]["title"] == "player pick"
    assert log["landsraad"]["mode"] == {"enabled": True} and log["landsraad"]["route"]["stops"][0]["x"] == 7
    # scan fetch: confirmed-stale rows dropped for the page, raw for us
    assert [row["x"] for row in log["filtered"]] == [5]
    assert len(log["fetchScan"]["rows"]) == 2
    # clearing restores the native state
    assert log["cleared"]["dest"] == {"hasDestination": False}
    assert log["pingAfterSet"] == "ok"
    # reclaimed topic after the page unsubscribed
    assert r["posted"][-1] == {"type": "subscribe", "payload": {"topics": ["dune.map.actors"]}}


DOC_START = r"""
const listeners = [];
const wv = {
  postMessage: () => {},
  addEventListener: (_, fn) => listeners.push(fn),
  removeEventListener: (_, fn) => { const i = listeners.indexOf(fn); if (i >= 0) listeners.splice(i, 1); },
  dispatchEvent: (e) => { for (const fn of [...listeners]) fn(e); return true; },
};
globalThis.window = globalThis;
globalThis.chrome = { webview: wv };
globalThis.location = { pathname: '/dune/minimap' };
globalThis.document = { readyState: 'loading', getElementById: () => null };
globalThis.__harvestingDuneEmit = () => {};
globalThis.fetch = async () => new Response('[]');
const r = eval(SCRIPT);                       // document start: bridge first ...
const seen = [];                              // ... then the page registers its bus listener
const pageFn = (e) => { const m = e.data; if (m && m.topic) seen.push([m.topic, m.payload]); };
wv.addEventListener('message', pageFn);
const native = (topic, payload) => wv.dispatchEvent({ data: { type: 'delta', topic, payload } });
__dd.set({ dest: { hasDestination: true, title: 'Iron' }, route: { mapId: 'DD', stops: [{ x: 1 }, { x: 2 }] }, extraActors: [{ id: 'dd-stale' }] });
const afterSet = seen.length;
native('dune.map.actors', { actors: [{ id: 1 }] });
native('dune.trackedDestination', { hasDestination: false });
native('dune.trackedDestination', { hasDestination: true, title: 'mine' });
native('dune.map.route', { mapId: 'DD', stops: [] });
setTimeout(() => {
  wv.removeEventListener('message', pageFn);
  native('dune.map.actors', { actors: [] });
  console.log(JSON.stringify({ r, intercepting: __dd.intercepting, afterSet, seen: seen.slice(afterSet), total: seen.length }));
  process.exit(0);
}, 20);
"""


def test_document_start_install_rewrites_in_flight_with_a_single_render():
    code = DOC_START.replace("SCRIPT", json.dumps(BRIDGE_SCRIPT))
    out = subprocess.run(["node", "-"], input=code, capture_output=True, text=True, timeout=30, check=False)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout.splitlines()[0])
    assert r["r"] == "installed" and r["intercepting"] is True
    after = r["seen"]
    # each native message reaches the page exactly once, already rewritten; nothing re-dispatched
    assert [t for t, _ in after] == ["dune.map.actors", "dune.trackedDestination", "dune.trackedDestination", "dune.map.route"]
    assert [a["id"] for a in after[0][1]["actors"]] == [1, "dd-stale"]
    assert after[1][1]["title"] == "Iron"  # native "none" replaced by ours
    assert after[2][1]["title"] == "mine"  # the player's own destination passes through
    assert len(after[3][1]["stops"]) == 2  # our route in place of the empty native one
    assert r["total"] == r["afterSet"] + 4  # removed page listener really stopped receiving


@pytest.mark.parametrize(("path", "expected"), [("/dune", "ok"), ("/dune/", "ok"), ("/dune/overlay", "skip"), ("/dune/minimap", "skip"), ("/dune/raw-stats", "skip")])
def test_picker_renders_only_in_the_map_window(path, expected):
    code = r"""
const made = [];
globalThis.window = globalThis;
globalThis.chrome = { webview: { postMessage() {}, addEventListener() {}, removeEventListener() {}, dispatchEvent() {} } };
globalThis.location = { pathname: PATH };
const el = () => ({ style: {}, dataset: {}, append() {}, appendChild() {}, set innerHTML(v) {}, querySelector() { return null; } });
const nodes = {};
globalThis.document = { readyState: 'complete', body: { appendChild(n) { nodes.__dd_picker = n; made.push(1); } },
  getElementById: (id) => nodes[id] || (made.length ? (nodes[id] = el()) : null), createElement: () => el(), createTextNode: () => ({}) };
globalThis.__harvestingDuneEmit = () => {}; globalThis.fetch = async () => new Response('[]');
eval(SCRIPT);
const r = __dd.renderPicker([['spice', 'Spice']], [['auto', 'auto']], 'spice', 'auto', 'ok', { lines: true, items: [['iron', 'Iron Ore', true]] });
console.log(JSON.stringify({ r })); process.exit(0);
""".replace("SCRIPT", json.dumps(BRIDGE_SCRIPT)).replace("PATH", json.dumps(path))
    out = subprocess.run(["node", "-"], input=code, capture_output=True, text=True, timeout=30, check=False)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.splitlines()[0])["r"] == expected


@pytest.mark.parametrize(("path", "shown"), [("/dune", True), ("/dune/minimap", False), ("/dune/overlay", False)])
def test_a_problem_shows_a_small_notice_in_the_map_window_only(path, shown):
    code = r"""
const nodes = {};
const el = () => { const n = { style: {}, dataset: {}, children: [], setAttribute() {}, append(...c) { n.children.push(...c); },
  remove() { for (const k of Object.keys(nodes)) if (nodes[k] === n || n.children.includes(nodes[k])) delete nodes[k]; } };
  Object.defineProperty(n, 'id', { set(v) { nodes[v] = n; }, get() { return ''; } }); return n; };
const store = {};
globalThis.window = globalThis;
globalThis.chrome = { webview: { postMessage() {}, addEventListener() {}, removeEventListener() {}, dispatchEvent() {} } };
globalThis.location = { pathname: PATH };
globalThis.sessionStorage = { getItem: (k) => store[k] || null, setItem: (k, v) => { store[k] = v; } };
globalThis.document = { readyState: 'complete', body: { appendChild() {} }, getElementById: (id) => nodes[id] || null, createElement: () => el() };
globalThis.__harvestingDuneEmit = () => {}; globalThis.fetch = async () => new Response('[]');
eval(SCRIPT);
const log = {};
__dd.set({ problem: { id: 3, text: 'harvesting-dune hit 3 problems.' } });
log.text = nodes.__dd_problem_text ? nodes.__dd_problem_text.textContent : null;
__dd.set({});
log.afterClear = !!nodes.__dd_problem;
__dd.set({ problem: { id: 4, text: 'again' } });
if (nodes.__dd_problem) nodes.__dd_problem.children[1].onclick();   // "Hide"
__dd.set({ problem: { id: 5, text: 'later' } });
log.afterHide = !!nodes.__dd_problem;
chrome.webview.postMessage({ type: 'unsubscribe', payload: { topics: 'not a list' } });  // the page's own odd call
log.pageCallSurvived = true;
console.log(JSON.stringify(log)); process.exit(0);
""".replace("SCRIPT", json.dumps(BRIDGE_SCRIPT)).replace("PATH", json.dumps(path))
    out = subprocess.run(["node", "-"], input=code, capture_output=True, text=True, timeout=30, check=False)
    assert out.returncode == 0, out.stderr
    log = json.loads(out.stdout.splitlines()[0])
    assert log["text"] == ("harvesting-dune hit 3 problems." if shown else None)
    assert not log["afterClear"] and not log["afterHide"] and log["pageCallSurvived"]


def test_shipwrecks_are_read_from_the_page_map_file_with_its_map_id():
    code = r"""
globalThis.window = globalThis;
globalThis.chrome = { webview: { postMessage() {}, addEventListener() {}, removeEventListener() {}, dispatchEvent() {} } };
globalThis.location = { pathname: '/dune' };
globalThis.document = { readyState: 'complete', getElementById: () => null };
globalThis.__harvestingDuneEmit = () => {};
Object.defineProperty(globalThis, 'performance', { configurable: true,
  value: { getEntriesByType: () => [{ name: 'https://cdn/x/maps/survival_1.d.json?version=1' }] } });
// the companion's compact map data: every field holds the index of its value
const raw = [{ iconId: 1, name: 2, location: 3 }, 'shipwreck', 'The Wreck of the Pallas', { x: 4, y: 5, z: 6 }, -48675, -70247, 4799,
  { iconId: 8, name: 9, location: 3 }, 'lootcontainer', 'Basic Container', { iconId: 1, name: 2, location: 3 }];
globalThis.fetch = async () => new Response(JSON.stringify(raw));
eval(SCRIPT);
__dd.shipwrecksFromData().then((r) => { console.log(JSON.stringify(r)); process.exit(0); });
""".replace("SCRIPT", json.dumps(BRIDGE_SCRIPT))
    out = subprocess.run(["node", "-"], input=code, capture_output=True, text=True, timeout=30, check=False)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.splitlines()[0]) == {"map": "survival_1",
                                                      "wrecks": [["The Wreck of the Pallas", -48675, -70247, 4799]]}


def test_minimap_hotkey_shows_it_in_every_activity_then_switches_it_off():
    code = r"""
const listeners = [], sent = [];
const wv = { postMessage: (m) => sent.push(m), addEventListener: (_, f) => listeners.push(f), removeEventListener() {}, dispatchEvent: (e) => { for (const f of [...listeners]) f(e); } };
globalThis.window = globalThis; globalThis.chrome = { webview: wv };
globalThis.location = { pathname: '/dune' };
globalThis.document = { readyState: 'loading', getElementById: () => null };
globalThis.__harvestingDuneEmit = () => {}; globalThis.fetch = async () => new Response('[]');
eval(SCRIPT);
wv.dispatchEvent({ data: { type: 'helloAck' } });
const subscribed = sent.some((m) => m.type === 'subscribe' && m.payload.topics.includes('dune.overlayWidgets'));
// the companion's own setting: the minimap only while driving
wv.dispatchEvent({ data: { type: 'snapshot', topic: 'dune.overlayWidgets', payload: { widgets: [
  { key: 'dune.minimap', enabled: true, activities: ['Driving'], supported: ['Exploration', 'Combat', 'Driving'] }] } } });
sent.length = 0;
const first = __dd.toggleMinimap(), shown = sent.map((m) => [m.type, m.cmd, m.payload]);
sent.length = 0;
const second = __dd.toggleMinimap(), hidden = sent.map((m) => [m.type, m.cmd, m.payload]);
console.log(JSON.stringify({ subscribed, first, shown, second, hidden })); process.exit(0);
""".replace("SCRIPT", json.dumps(BRIDGE_SCRIPT))
    out = subprocess.run(["node", "-"], input=code, capture_output=True, text=True, timeout=30, check=False)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout.splitlines()[0])
    key = {"key": "dune.minimap"}
    assert r["subscribed"]
    assert r["first"] == "shown" and r["shown"] == [
        ["cmd", "overlay.setWidgetActivities", {**key, "activities": ["Exploration", "Combat", "Driving"]}],
        ["cmd", "overlay.setWidgetBuildModeVisibility", {**key, "visible": True}],
        ["cmd", "overlay.setWidgetVisibility", {**key, "enabled": True}]]
    assert r["second"] == "hidden" and r["hidden"] == [["cmd", "overlay.setWidgetVisibility", {**key, "enabled": False}]]


def test_chance_finds_are_pulsing_rings_and_a_dashed_line():
    code = r"""
const listeners = [];
const wv = { postMessage() {}, addEventListener: (_, f) => listeners.push(f), removeEventListener() {}, dispatchEvent: (e) => { for (const f of [...listeners]) f(e); } };
globalThis.window = globalThis; globalThis.chrome = { webview: wv };
globalThis.location = { pathname: '/dune/minimap' };
globalThis.document = { readyState: 'loading', getElementById: () => null };
globalThis.__harvestingDuneEmit = () => {}; globalThis.fetch = async () => new Response('[]');
eval(SCRIPT);
const drawn = [];
const layer = { clearVectors() { drawn.length = 0; }, addPolyline: (pts, style) => drawn.push([pts.length, style.color]) };
const map = { layers: { createStaticLayer: () => layer, addLayer() {} }, view: { translate: (x, y) => ({ x, y }) } };
map._createdLayers = [{ _items: new Map([[1, { data: { x: 5, y: 7 }, point: { x: 5, y: 7 } }]]) }];  // a page marker: calibration
const GOLD = 'rgba(255,209,102,0.95)';
__dd.set({ alternatives: [{ x: 1000, y: 0, kind: 'chance', dash: true, pulse: true, color: GOLD, weight: 3 },
                          { x: 1000, y: 0, kind: 'chance-area', ring: 500, pulse: true, color: GOLD, weight: 3 }] });
wv.dispatchEvent({ data: { type: 'delta', topic: 'dune.player', payload: { x: 0, y: 0, z: 0 } } });
__dd.redrawAlternatives(true);
const first = drawn.slice();
__dd.pulse();
console.log(JSON.stringify({ first, faded: drawn.slice() })); process.exit(0);
""".replace("SCRIPT", json.dumps(BRIDGE_SCRIPT))
    out = subprocess.run(["node", "-"], input=code, capture_output=True, text=True, timeout=30, check=False)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout.splitlines()[0])
    gold, dim = "rgba(255,209,102,0.95)", "rgba(255,209,102,0.38)"
    assert sorted({n for n, _ in r["first"]}) == [2, 33]  # dashes (2 points each) and one ring (33 points)
    assert sum(1 for n, _ in r["first"] if n == 2) == 16 and {c for _, c in r["first"]} == {gold}
    assert len(r["faded"]) == len(r["first"]) and {c for _, c in r["faded"]} == {dim}  # the pulse beat


def test_peer_and_member_rings_draw_as_rings_only_and_never_pulse():
    code = r"""
const listeners = [], emitted = [];
const wv = { postMessage() {}, addEventListener: (_, f) => listeners.push(f), removeEventListener() {}, dispatchEvent: (e) => { for (const f of [...listeners]) f(e); } };
globalThis.window = globalThis; globalThis.chrome = { webview: wv };
globalThis.location = { pathname: '/dune/minimap' };
globalThis.document = { readyState: 'loading', getElementById: () => null };
globalThis.__harvestingDuneEmit = (s) => emitted.push(JSON.parse(s)); globalThis.fetch = async () => new Response('[]');
eval(SCRIPT);
const drawn = [];
const layer = { clearVectors() { drawn.length = 0; }, addPolyline: (pts, style) => drawn.push([pts.length, style.color]) };
const translate = (x, y) => { if (!Number.isFinite(x) || !Number.isFinite(y)) throw new Error('bad point'); return { x, y }; };
const map = { layers: { createStaticLayer: () => layer, addLayer() {} }, view: { translate } };
map._createdLayers = [{ _items: new Map([[1, { data: { x: 5, y: 7 }, point: { x: 5, y: 7 } }]]) }];
const set = __dd.set({ alternatives: [{ kind: 'peer', ring: 4000, color: 'red', weight: 2 },  // malformed peer item first: skipped
                                      { x: 1, y: 2, kind: 'peer', ring: 4000, color: 'rgba(120,255,140,0.6)', weight: 2, age: 120 },
                                      { x: 3, y: 4, kind: 'member', ring: 6000, color: 'rgba(255,230,120,0.6)', weight: 2, age: 30 }] });
wv.dispatchEvent({ data: { type: 'delta', topic: 'dune.player', payload: { x: 0, y: 0, z: 0 } } });
__dd.redrawAlternatives(true);
const first = drawn.slice();
const idle = __dd.pulse();
console.log(JSON.stringify({ set, idle, first, logs: emitted.filter((e) => e.type === 'log') })); process.exit(0);
""".replace("SCRIPT", json.dumps(BRIDGE_SCRIPT))
    out = subprocess.run(["node", "-"], input=code, capture_output=True, text=True, timeout=30, check=False)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout.splitlines()[0])
    assert r["set"] == "ok" and r["idle"] == "idle"
    assert sorted(n for n, _ in r["first"]) == [33, 33]  # one ring per item, no line to the player
    assert r["logs"] == []  # no report(...) call


def test_farm_route_draws_one_line_to_the_next_stop_and_numbered_rings():
    code = r"""
const listeners = [];
const wv = { postMessage() {}, addEventListener: (_, f) => listeners.push(f), removeEventListener() {}, dispatchEvent: (e) => { for (const f of [...listeners]) f(e); } };
globalThis.window = globalThis; globalThis.chrome = { webview: wv };
globalThis.location = { pathname: '/dune/minimap' };
globalThis.document = { readyState: 'loading', getElementById: () => null };
globalThis.__harvestingDuneEmit = () => {}; globalThis.fetch = async () => new Response('[]');
eval(SCRIPT);
const page = {};
wv.addEventListener('message', (e) => { if (e.data.topic) page[e.data.topic] = e.data.payload; });
const drawn = [];
const layer = { clearVectors() { drawn.length = 0; }, addPolyline: (pts, style) => drawn.push(pts.map((p) => [p.x, p.y])) };
const map = { layers: { createStaticLayer: () => layer, addLayer() {} }, view: { translate: (x, y) => ({ x, y }) } };
map._createdLayers = [{ _items: new Map([[1, { data: { x: 5, y: 7 }, point: { x: 5, y: 7 } }]]) }];
const CYAN = 'rgba(89,221,255,0.95)', DIM = 'rgba(255,255,255,0.30)';
__dd.set({ route: { mapId: 'survival_1', stops: [{ x: 1000, y: 0, z: 0, houseName: 'Farm route' }] },
  alternatives: [{ x: 1000, y: 0, kind: 'route-stop', ring: 500, num: 1, color: CYAN, weight: 2.5 },
                 { x: 3000, y: 0, kind: 'route-stop', ring: 500, num: 2, color: CYAN, weight: 2.5 },
                 { x: 5000, y: 0, kind: 'route-stop', ring: 500, num: 12, color: CYAN, weight: 2.5 },
                 { x: -1000, y: 0, kind: 'route-done', ring: 500, color: DIM, weight: 1.5 }] });
wv.dispatchEvent({ data: { type: 'delta', topic: 'dune.player', payload: { x: 0, y: 0, z: 0 } } });
__dd.redrawAlternatives(true);
console.log(JSON.stringify({ route: page['dune.map.route'], drawn })); process.exit(0);
""".replace("SCRIPT", json.dumps(BRIDGE_SCRIPT))
    out = subprocess.run(["node", "-"], input=code, capture_output=True, text=True, timeout=30, check=False)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout.splitlines()[0])
    assert len(r["route"]["stops"]) == 1  # the companion's own line: to the next stop only
    rings = [p for p in r["drawn"] if len(p) == 33]
    strokes = [p for p in r["drawn"] if len(p) == 2]
    assert len(rings) == 4 and not any(len(p) not in (2, 33) for p in r["drawn"])  # no line from you to any ring
    assert len(strokes) == 2 + 5 + (2 + 5)  # seven-segment "1", "2" and "12"; the done ring has no number
    one = [s for s in strokes if all(abs(x - 1000) <= 250 for x, _ in s)]
    assert len(one) == 2 and all(s[0][0] == s[1][0] for s in one)  # "1" is two upright strokes inside its ring


def test_route_line_is_reanchored_as_the_player_moves():
    code = r"""
const listeners = [];
const wv = { postMessage() {}, addEventListener: (_, f) => listeners.push(f), removeEventListener() {}, dispatchEvent: (e) => { for (const f of [...listeners]) f(e); } };
globalThis.window = globalThis; globalThis.chrome = { webview: wv };
globalThis.location = { pathname: '/dune/minimap' };
globalThis.document = { readyState: 'loading', getElementById: () => null };
globalThis.__harvestingDuneEmit = () => {}; globalThis.fetch = async () => new Response('[]');
eval(SCRIPT);
let routes = 0;
wv.addEventListener('message', (e) => { if (e.data.topic === 'dune.map.route') routes++; });
__dd.set({ route: { mapId: 'DD', stops: [{ x: 1e6, y: 0, z: 0 }] } });
const base = routes;
const move = (x) => wv.dispatchEvent({ data: { type: 'delta', topic: 'dune.player', payload: { x, y: 0, z: 0 } } });
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  move(0); await wait(20);            // first pose: anchor + redraw
  move(1000); await wait(20);         // 10 m: too little, and too soon
  await wait(1050); move(5000); await wait(20);   // 50 m after a second: redraw
  move(9000); await wait(20);         // moved, but within the same second: wait
  console.log(JSON.stringify({ redraws: routes - base })); process.exit(0);
})();
""".replace("SCRIPT", json.dumps(BRIDGE_SCRIPT))
    out = subprocess.run(["node", "-"], input=code, capture_output=True, text=True, timeout=30, check=False)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.splitlines()[0])["redraws"] == 2


DRAW = r"""
const listeners = [], posted = [], emitted = [], page = {}, seen = [];
const wv = { postMessage: (m) => posted.push(m), addEventListener: (_, f) => listeners.push(f), removeEventListener() {},
  dispatchEvent: (e) => { for (const f of [...listeners]) f(e); } };
globalThis.window = globalThis; globalThis.chrome = { webview: wv };
globalThis.location = { pathname: '/dune/minimap' };
globalThis.document = { readyState: 'loading', getElementById: () => null };
globalThis.__harvestingDuneEmit = (s) => emitted.push(JSON.parse(s)); globalThis.fetch = async () => new Response('[]');
eval(SCRIPT);
__dd.primary = true;
wv.addEventListener('message', (e) => { const m = e.data; if (m && m.topic) { page[m.topic] = m.payload; seen.push(m.topic); } });
const drawn = [];
const layer = { clearVectors() { drawn.length = 0; }, addPolyline: (pts, style) => drawn.push([pts.length, style.color]) };
const translate = (x, y) => { if (!Number.isFinite(x) || !Number.isFinite(y)) throw new Error('bad point'); return { x, y }; };
const map = { layers: { createStaticLayer: () => layer, addLayer() {} }, view: { translate } };
map._createdLayers = [{ _items: new Map([[1, { data: { x: 5, y: 7 }, point: { x: 5, y: 7 } }]]) }];  // a page marker: calibration
const STATES = STATES_JSON;
EXTRA
const runs = [];
for (const state of STATES) {
  __dd.set(state);
  wv.dispatchEvent({ data: { type: 'delta', topic: 'dune.player', payload: { x: 0, y: 0, z: 0 } } });
  const result = __dd.redrawAlternatives(true);
  runs.push({ result, drawn: drawn.slice() });
}
console.log(JSON.stringify({ runs, posted, emitted, page, seen })); process.exit(0);
"""


def draw_runs(*states, extra=""):
    """Each state through __dd.set + a dune.player delta + a forced redraw, in one page; `extra` is JS run first."""
    code = DRAW.replace("SCRIPT", json.dumps(BRIDGE_SCRIPT)).replace("STATES_JSON", json.dumps(list(states)))
    out = subprocess.run(["node", "-"], input=code.replace("EXTRA", extra), capture_output=True, text=True, timeout=30, check=False)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.splitlines()[0])


def draw(state: dict) -> list[tuple[int, str]]:
    """(points, colour) per polyline the bridge draws for one page_state."""
    return [(n, c) for n, c in draw_runs(state)["runs"][0]["drawn"]]


def pin(label, **kw):
    return {"id": "t1:a", "kind": "pin", "x": 1000, "y": 0, "z": 0, "body": {"label": label}, "mine": False, **kw}


FOREIGN, MINE = "rgba(255,255,255,0.7)", "rgba(255,255,255,0.9)"


def test_pin_objects_draw_a_ring_an_own_mark_and_a_stroke_label():
    blank, labelled = draw({"objects": [pin("")]}), draw({"objects": [pin("IRON HERE")]})
    for drawn in (blank, labelled):
        assert [p for p in drawn if p[0] == 33] == [(33, FOREIGN)] and {c for _, c in drawn} == {FOREIGN}
    assert len(labelled) > len(blank) + 8
    mine = draw({"objects": [pin("", mine=True)]})
    assert mine == [(33, MINE), (33, MINE)]


def test_pin_labels_are_strokes_from_the_glyph_whitelist_and_never_dispatched():
    r = draw_runs({"objects": [pin("<b>x</b>")]}, {"objects": [pin("BX/B")]},
                  {"objects": [pin("A" * 40)]}, {"objects": [pin("A" * 16)]}, {"objects": [pin("IRON HERE")]})
    hostile, plain, long, cap, _ = (run["drawn"] for run in r["runs"])
    # "<", ">" are not in the table and are dropped ("/" is a whitelisted mark), never substituted
    assert len(hostile) == len(plain) and hostile == plain
    assert len(long) == len(cap)
    text = json.dumps({"posted": r["posted"], "emitted": r["emitted"], "page": r["page"]})
    assert "IRON HERE" not in text and "<b>" not in text
    assert set(r["seen"]) == {"dune.player"}  # no dune.map.actors / dune.trackedDestination dispatch for objects


def test_unknown_kinds_and_bad_objects_are_skipped_one_at_a_time():
    assert draw({"objects": [{**pin("BASE"), "kind": "base", "body": {"name": "BASE"}}]}) == []
    bad_then_good = draw_runs({"objects": [{**pin("X"), "x": None}, pin("X")]},
                              extra="STATES[0].objects[0].x = NaN;").get("runs")
    assert [p for p in bad_then_good[0]["drawn"] if p[0] == 33] == [[33, FOREIGN]]
    # a pin that throws while drawing is reported (once per redraw); the next pin still draws
    throws = draw_runs({"objects": [pin("X"), pin("Y")]},
                       extra="Object.defineProperty(STATES[0].objects[0].body, 'label', { get() { throw new Error('bad'); } });")
    assert [p for p in throws["runs"][0]["drawn"] if p[0] == 33] == [[33, FOREIGN], [33, FOREIGN]]
    assert {e["where"] for e in throws["emitted"] if e.get("type") == "log"} == {"objects"}
    nobody = draw_runs({"objects": [{**pin(""), "body": None}]})
    assert nobody["runs"][0]["drawn"] == [[33, FOREIGN]]
    assert [e for e in nobody["emitted"] if e.get("type") == "log"] == []


def test_objects_draw_with_no_alternatives_and_vanish_by_absence():
    r = draw_runs({"alternatives": [], "objects": [pin("IRON")]}, {"objects": []})
    shown, gone = r["runs"]
    assert shown["result"] == "drawn" and [33, FOREIGN] in shown["drawn"]
    assert gone["result"] == "empty" and gone["drawn"] == []


def test_a_push_applies_only_the_parts_that_changed():
    code = r"""
const listeners = [], timers = [];
const wv = { postMessage() {}, addEventListener: (_, f) => listeners.push(f), removeEventListener() {}, dispatchEvent: (e) => { for (const f of [...listeners]) f(e); } };
globalThis.window = globalThis; globalThis.chrome = { webview: wv };
globalThis.location = { pathname: '/dune/minimap' };
globalThis.document = { readyState: 'loading', getElementById: () => null };
globalThis.__harvestingDuneEmit = () => {}; globalThis.fetch = async () => new Response('[]');
// timers run when the test says so: the 30 ms redraw yes, the 60 s dead-man switch never
globalThis.setTimeout = (fn, ms) => timers.push([fn, ms || 0]); globalThis.clearTimeout = () => {};
globalThis.setInterval = () => 0; globalThis.clearInterval = () => {};
const flush = () => { for (let t; (t = timers.findIndex(([, ms]) => ms < 1000)) >= 0;) timers.splice(t, 1)[0][0](); };
eval(SCRIPT);
const n = { actors: 0, dest: 0, route: 0, redraws: 0 };
wv.addEventListener('message', (e) => { const m = e.data; if (m && m.__dd) {
  if (m.topic === 'dune.map.actors') n.actors++; if (m.topic === 'dune.trackedDestination') n.dest++; if (m.topic === 'dune.map.route') n.route++; } });
const layer = { vectors: new Map(), clearVectors() { n.redraws++; }, addPolyline() {} };
const map = { layers: { createStaticLayer: () => layer, addLayer() {} }, view: { translate: (x, y) => ({ x, y }) } };
map._createdLayers = [{ _items: new Map([[1, { data: { x: 5, y: 7 }, point: { x: 5, y: 7 } }]]) }];
wv.dispatchEvent({ data: { type: 'delta', topic: 'dune.map.actors', payload: { actors: [{ id: 1 }] } } });
wv.dispatchEvent({ data: { type: 'delta', topic: 'dune.player', payload: { x: 0, y: 0, z: 0 } } });
const S = { dest: { hasDestination: true, x: 1, y: 2, z: 3, title: 'Iron', detail: '~4 min' },
  route: { mapId: 'DD', stops: [{ x: 1, y: 2, z: 3 }] }, extraActors: [{ id: 'dd-a' }],
  alternatives: [{ x: 1000, y: 0, kind: 'area', color: 'red', weight: 2 }], objects: [], stale: [], problem: null };
const step = (state) => { const before = { ...n }; __dd.set(JSON.parse(JSON.stringify(state))); flush();
  return Object.fromEntries(Object.keys(n).map((k) => [k, n[k] - before[k]])); };
const log = { first: step(S), same: step(S), staleOnly: step({ ...S, stale: ['SpiceField:1:2'] }),
  destText: step({ ...S, stale: ['SpiceField:1:2'], dest: { ...S.dest, detail: '~5 min' } }),
  actors: step({ ...S, stale: ['SpiceField:1:2'], dest: { ...S.dest, detail: '~5 min' }, extraActors: [{ id: 'dd-b' }] }),
  lines: step({ ...S, stale: ['SpiceField:1:2'], dest: { ...S.dest, detail: '~5 min' }, extraActors: [{ id: 'dd-b' }], alternatives: [] }),
  stats: __dd.stats };
console.log(JSON.stringify(log)); process.exit(0);
""".replace("SCRIPT", json.dumps(BRIDGE_SCRIPT))
    out = subprocess.run(["node", "-"], input=code, capture_output=True, text=True, timeout=30, check=False)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout.splitlines()[0])
    assert r["first"] == {"actors": 1, "dest": 1, "route": 1, "redraws": 1}
    assert r["same"] == {"actors": 0, "dest": 0, "route": 0, "redraws": 0}  # an identical push costs the page nothing
    assert r["staleOnly"] == {"actors": 0, "dest": 0, "route": 0, "redraws": 0}
    assert r["destText"] == {"actors": 0, "dest": 1, "route": 0, "redraws": 0}  # unrelated: no actor list, no layer
    assert r["actors"] == {"actors": 1, "dest": 0, "route": 0, "redraws": 0}
    assert r["lines"] == {"actors": 0, "dest": 0, "route": 0, "redraws": 1}
    assert r["stats"] == {"sets": 6, "actors": 2, "dest": 2, "route": 1, "redraws": 2, "dom": 0}


def test_our_own_synthetic_destination_is_never_emitted_back():
    emitted = run()["emitted"]
    dests = [m["payload"] for m in emitted if m.get("topic") == "dune.trackedDestination"]
    assert not any((p or {}).get("title") == "Iron" for p in dests)
    assert any((p or {}).get("title") == "player pick" for p in dests)
