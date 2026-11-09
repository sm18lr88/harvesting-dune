// Installed into each gaming.tools companion page (map window and minimap) over DevTools.
// Placeholders %TOPICS% and %BINDING% are substituted by feed.py.
(() => {
  const wv = window.chrome && window.chrome.webview;
  if (!wv) return 'no-webview';
  if (window.__dd) return window.__dd.version === '%VERSION%' ? 'already' : 'outdated';
  const TOPICS = %TOPICS%;
  const want = new Set(TOPICS);
  const emit = (m) => { try { %BINDING%(JSON.stringify(m)); } catch (_) {} };
  // our own failures go to the detector's log (data\logs\errors.log); the page keeps running
  const report = (where, err) => emit({ type: 'log', where, message: String((err && err.stack) || err).slice(0, 500) });
  const dd = (window.__dd = {
    version: '%VERSION%',
    primary: false, // only the primary page forwards bus topics (avoids duplicates)
    dest: null, destShown: false, nativeDest: null,
    route: null, routeShown: false, nativeRoute: null, nativeRouteMode: false,
    extraActors: [], lastActors: null, lastSession: null,
    hideSpawnPoints: true, // empty spice spawn points look like fields on the companion map
    alternatives: [], objects: [], pose: null, maps: [], altLayer: null, altMap: null, altTransform: null,
    stale: new Set(),
    // what we made this page do (read over DevTools to diagnose overlay cost)
    stats: { sets: 0, actors: 0, dest: 0, route: 0, redraws: 0, dom: 0 },
  });

  // Synthetic messages go through the page's own listeners, exactly like native ones.
  const COUNTED = { 'dune.map.actors': 'actors', 'dune.trackedDestination': 'dest', 'dune.map.route': 'route' };
  const dispatch = (topic, payload) => {
    if (COUNTED[topic]) dd.stats[COUNTED[topic]]++;
    wv.dispatchEvent(new MessageEvent('message', { data: { type: 'delta', topic, payload, __dd: 1 } }));
  };

  const applyDest = () => {
    if (dd.nativeDest && dd.nativeDest.hasDestination) return; // the player's own destination wins
    if (dd.dest) { dispatch('dune.trackedDestination', dd.dest); dd.destShown = true; }
    else if (dd.destShown) { dispatch('dune.trackedDestination', dd.nativeDest || { hasDestination: false }); dd.destShown = false; }
  };
  const applyRoute = () => {
    if (dd.nativeRouteMode) return; // Landsraad route mode is in use
    if (dd.route) {
      dispatch('dune.landsraad.routeMode', { enabled: true });
      dispatch('dune.map.route', dd.route);
      dd.routeShown = true;
    } else if (dd.routeShown) {
      dispatch('dune.landsraad.routeMode', { enabled: false });
      dispatch('dune.map.route', dd.nativeRoute);
      dd.routeShown = false;
    }
  };
  const applyActors = () => {
    if (!dd.extraActors.length || !dd.lastActors) return;
    dispatch('dune.map.actors', { ...dd.lastActors, actors: [...(dd.lastActors.actors || []), ...dd.extraActors] });
  };
  // Run after every other listener so native updates never overwrite ours.
  const later = (fn) => setTimeout(fn, 0);

  // An empty spice spawn point: the game replicates every site, active or not.
  const isSpawnPoint = (a) =>
    a && a.spawned === false && !(a.fillRate > 0) && /spicefield/i.test(a.markerType || a.spriteKey || a.map_marker_id || a.type || '');

  // Installed at document start (every reload), we can hook the page's own listeners and
  // rewrite native messages in flight: one render per update instead of native + ours.
  // Installed into an already running page, we fall back to re-dispatching after it.
  const transform = (m) => {
    if (!m || typeof m !== 'object' || m.__dd || m.type === 'topicReset') return m;
    const t = m.topic, p = m.payload;
    if (t === 'dune.map.actors' && p && Array.isArray(p.actors) && (dd.extraActors.length || dd.hideSpawnPoints)) {
      const shown = dd.hideSpawnPoints ? p.actors.filter((a) => !isSpawnPoint(a)) : p.actors;
      return { ...m, payload: { ...p, actors: dd.extraActors.length ? shown.concat(dd.extraActors) : shown } };
    }
    if (t === 'dune.trackedDestination' && dd.dest && !(p && p.hasDestination))
      return { ...m, payload: dd.dest };
    if (dd.route && !dd.nativeRouteMode) {
      if (t === 'dune.landsraad.routeMode') return { ...m, payload: { enabled: true } };
      if (t === 'dune.map.route') return { ...m, payload: dd.route };
    }
    return m;
  };
  dd.intercepting = document.readyState === 'loading';

  // gtmap (the companion's map engine) assigns this._createdLayers = [] in its constructor. A setter
  // on Object.prototype sees that assignment, so we get each map instance as it is created. The
  // property then becomes a normal own property of that object; nothing else changes. Installed into a
  // running page, it still sees maps the page makes later (opening the map in the map window).
  if (!Object.prototype.hasOwnProperty('_createdLayers')) {
    const kept = new WeakMap(); // only if an object refuses own properties: the map still gets its value
    Object.defineProperty(Object.prototype, '_createdLayers', {
      configurable: true,
      get() { return kept.get(this); },
      set(v) {
        try {
          Object.defineProperty(this, '_createdLayers', { value: v, writable: true, configurable: true, enumerable: true });
        } catch (err) { try { kept.set(this, v); } catch (_) {} report('layers', err); }
        if (dd.maps.length < 8) dd.maps.push(this);
      },
    });
  }
  // Installed into a running page, that hook is too late for a map that already exists. gtmap's render
  // loop asks for every frame, so a frame request's call stack names gtmap's module; import() of that
  // already loaded module only hands it over, and it exports the map class (the one that sets
  // _createdLayers). The next call of one of its methods shows the live map as `this`. Everything we
  // wrap is put back as soon as the map is found.
  const findLateMap = () => {
    const raf = window.requestAnimationFrame;
    if (typeof raf !== 'function') return;
    const tried = new Set();
    let found = false, lookedAt = 0;
    const isMap = (m) => !!(m && m.layers && m.view && m._createdLayers);
    const isMapClass = (v) => typeof v === 'function' && !!v.prototype && /\b_createdLayers\s*=/.test(Function.prototype.toString.call(v));
    const unhook = () => { if (window.requestAnimationFrame === hook) window.requestAnimationFrame = raf; };
    const watch = (proto) => {
      const kept = [];
      const restore = () => { for (const [k, desc, w] of kept) if (proto[k] === w) Object.defineProperty(proto, k, desc); };
      for (const k of Object.getOwnPropertyNames(proto)) {
        const desc = Object.getOwnPropertyDescriptor(proto, k);
        if (k === 'constructor' || typeof desc.value !== 'function' || !desc.configurable) continue;
        const orig = desc.value;
        const w = function (...args) {
          try { if (isMap(this) && !dd.maps.includes(this) && dd.maps.length < 8) { dd.maps.push(this); restore(); } } catch (err) { report('late-map', err); }
          return orig.apply(this, args);
        };
        Object.defineProperty(proto, k, { ...desc, value: w });
        kept.push([k, desc, w]);
      }
    };
    const look = (stack) => {
      for (const [, url] of String(stack).matchAll(/((?:https?|file):\/\/[^\s()]+?\.m?js)(?::\d+)+/g)) {
        if (tried.has(url) || !url.startsWith(location.origin + '/')) continue; // only the page's own modules
        tried.add(url);
        import(url).then((mod) => {
          const C = !found && Object.values(mod).find(isMapClass);
          if (!C) return;
          found = true;
          unhook();
          watch(C.prototype);
        }).catch((err) => report('late-map', err));
      }
      if (tried.size >= 40) unhook();
    };
    const hook = function (...args) {
      try {
        const now = Date.now();
        if (found || dd.maps.length) unhook(); // a map came by the constructor hook
        else if (now - lookedAt >= 100) { lookedAt = now; look(new Error().stack); }
      } catch (err) { report('late-map', err); }
      return raf.apply(this, args);
    };
    window.requestAnimationFrame = hook;
  };
  if (!dd.intercepting) { try { findLateMap(); } catch (err) { report('late-map', err); } }

  const addListener = wv.addEventListener.bind(wv);
  if (dd.intercepting) {
    const removeListener = wv.removeEventListener ? wv.removeEventListener.bind(wv) : () => {};
    const wrapped = new Map();
    wv.addEventListener = (type, fn, opts) => {
      if (type !== 'message' || typeof fn !== 'function') return addListener(type, fn, opts);
      const w = (e) => {
        const m = e && e.data;
        let t = m;
        try { t = transform(m); } catch (err) { report('transform', err); }  // never break the page's own listener
        fn(t === m ? e : { data: t });
      };
      wrapped.set(fn, w);
      return addListener(type, w, opts);
    };
    wv.removeEventListener = (type, fn, opts) => removeListener(type, wrapped.get(fn) || fn, opts);
  }
  const fix = (fn) => { if (!dd.intercepting) later(fn); };

  // topics the bridge reads itself (never forwarded): the companion's overlay widget settings
  const OWN = ['dune.overlayWidgets'];
  const subscribe = () => wv.postMessage({ type: 'subscribe', payload: { topics: [...TOPICS, ...OWN] } });

  // The companion draws the route line from the player's position at the moment the route
  // arrives and never redraws it as you move (it was made for fixed Landsraad routes). Re-send
  // our route after ~30 m of movement (at most once a second) so the line keeps starting at you.
  let anchor = null, anchoredAt = 0;
  const reanchorRoute = (pose) => {
    if (!dd.route || !dd.routeShown || dd.nativeRouteMode || typeof pose.x !== 'number') return;
    const now = Date.now();
    if (anchor && now - anchoredAt < 1000) return;
    if (anchor && Math.hypot(pose.x - anchor.x, pose.y - anchor.y) < 3000) return;
    anchor = { x: pose.x, y: pose.y };
    anchoredAt = now;
    later(() => dispatch('dune.map.route', dd.route));
  };
  addListener('message', (e) => { try { onMessage(e); } catch (err) { report('message', err); } });
  function onMessage(e) {
    const m = e.data;
    if (!m || typeof m !== 'object' || m.__dd) return;
    if (m.type === 'helloAck') { subscribe(); if (dd.primary) emit({ type: 'helloAck' }); return; }
    const t = m.topic;
    const p = m.type === 'topicReset' ? null : m.payload;
    const reset = m.type === 'topicReset';
    if (t === 'dune.trackedDestination') { dd.nativeDest = p; if (dd.dest) (reset ? later : fix)(applyDest); }
    else if (t === 'dune.landsraad.routeMode') { dd.nativeRouteMode = !!(p && p.enabled === true); if (dd.route) (reset ? later : fix)(applyRoute); }
    else if (t === 'dune.map.route') { dd.nativeRoute = p; if (dd.route) (reset ? later : fix)(applyRoute); }
    else if (t === 'dune.map.actors') { dd.lastActors = p; if (dd.extraActors.length) fix(applyActors); }
    else if (t === 'dune.session' && p) { dd.lastSession = p; }
    else if (t === 'dune.player' && p) { dd.pose = p; reanchorRoute(p); redrawAlternatives(false); }
    else if (t === 'dune.overlayWidgets' && p) { dd.widgets = p; }
    if (dd.primary && typeof t === 'string' && want.has(t)) emit(m);
  }

  // Keep topics we rely on if the page drops them.
  const post = wv.postMessage.bind(wv);
  wv.postMessage = (msg) => {
    post(msg);
    try {
      if (msg && msg.type === 'unsubscribe') {
        const topics = msg.payload && msg.payload.topics;
        const lost = (Array.isArray(topics) ? topics : []).filter((t) => want.has(t) || OWN.includes(t));
        if (lost.length) post({ type: 'subscribe', payload: { topics: lost } });
      }
    } catch (err) { report('resubscribe', err); } // the page's own message is already sent
  };

  // Scan database: learn which map id the page uses, drop rows confirmed stale.
  const SCAN = 'dune-api-v2.gaming.tools/actors';
  const rowKey = (r) => `${r.type}:${Math.trunc(r.x)}:${Math.trunc(r.y)}`;
  const origFetch = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const url = typeof input === 'string' ? input : (input && input.url) || String(input);
    const res = await origFetch(input, init);
    if (!url.includes(SCAN)) return res;
    try { const u = new URL(url); emit({ type: 'scanWorld', world: u.searchParams.get('world'), seed: u.searchParams.get('seed') }); } catch (_) {}
    if ((!dd.stale.size && !dd.hideSpawnPoints) || !res.ok) return res;
    try {
      const rows = await res.clone().json();
      if (!Array.isArray(rows)) return res;
      const kept = rows.filter((r) => !dd.stale.has(rowKey(r)) && !(dd.hideSpawnPoints && isSpawnPoint({ ...r, ...(r.metadata || {}) })));
      return new Response(JSON.stringify(kept), { status: res.status, statusText: res.statusText, headers: res.headers });
    } catch (e) { report('scan-filter', e); return res; }
  };
  dd.fetchScan = async (world, seed) => {
    const q = new URLSearchParams({ world, seed: String(seed), refresh: String(Date.now()) });
    const r = await origFetch(`https://${SCAN}?${q}`, { cache: 'no-store' });
    if (!r.ok) return JSON.stringify({ status: r.status, rows: [] });
    return JSON.stringify({ status: r.status, rows: await r.json() });
  };

  // Make the page reload its scan-database layer: a session without a scan seed clears the
  // layer, the real session makes it fetch again, and our fetch hook drops stale rows.
  dd.refreshScan = () => {
    const s = dd.lastSession;
    if (!s || !s.mapScanSeed) return 'no-session';
    dispatch('dune.session', { ...s, mapScanSeed: 0 });
    setTimeout(() => dispatch('dune.session', dd.lastSession || s), 50);
    return 'ok';
  };

  // Thin secondary lines from the player to alternative fields, drawn in our own gtmap layer.
  let altDrawnAt = 0, altAnchor = null;
  const liveMap = () => dd.maps.find((m) => m && m.layers && m.view && !m._destroyed && m._createdLayers);
  const findRouteLine = (map) => {
    for (const layer of map._createdLayers || []) {
      const vecs = layer && layer.vectors;
      if (!vecs || typeof vecs.values !== 'function') continue;
      for (const v of vecs.values()) {
        const vec = v && (v.vector || v);
        if (vec && vec.type === 'polyline' && vec.style && /89,\s*221,\s*255/.test(vec.style.color || '') && vec.points && vec.points.length > 1)
          return vec.points;
      }
    }
    return null;
  };
  // Pick the world->map transform the page itself uses: every marker it draws stores its world
  // position (data.x/y) next to its map point, so any marker on the map is a reference.
  // The page's own route line is the fallback reference.
  const TRANSFORMS = ['flipVertical', 'original', 'flipHorizontal', 'flipBoth'];
  const matchTransform = (map, wx, wy, px, py) => {
    for (const tt of TRANSFORMS) {
      try {
        const q = map.view.translate(wx, wy, tt);
        if (Math.abs(q.x - px) < 0.5 && Math.abs(q.y - py) < 0.5) return tt;
      } catch (_) {}
    }
    return null;
  };
  const calibrate = (map) => {
    if (dd.altTransform && dd.altMap === map) return dd.altTransform;
    for (const layer of map._createdLayers || []) {
      const items = layer && layer._items;
      if (!items || typeof items.values !== 'function') continue;
      let n = 0;
      for (const it of items.values()) {
        if (++n > 40) break;
        const d = it && it.data, pt = it && it.point;
        if (!d || !pt || !Number.isFinite(d.x) || !Number.isFinite(d.y) || (d.x === 0 && d.y === 0)) continue;
        const tt = matchTransform(map, d.x, d.y, pt.x, pt.y);
        if (tt) return (dd.altTransform = tt);
      }
    }
    const pts = findRouteLine(map);
    if (pts && dd.route && dd.route.stops.length) {
      const stop = dd.route.stops[dd.route.stops.length - 1], end = pts[pts.length - 1];
      const tt = matchTransform(map, stop.x, stop.y, end.x, end.y);
      if (tt) return (dd.altTransform = tt);
    }
    return null;
  };
  // Chance finds: a ring around each likely place (in world units, so it scales with the zoom), a dashed
  // bearing line, and a slow pulse (fainter and wider every other beat): never mistaken for a route.
  const dim = (c) => String(c).replace(/[\d.]+\)\s*$/, (v) => (parseFloat(v) * 0.4).toFixed(2) + ')');
  const ring = (map, tt, a, r) => {
    const pts = [];
    for (let i = 0; i <= 32; i++) {
      const t = (i / 32) * 2 * Math.PI;
      pts.push(map.view.translate(a.x + r * Math.cos(t), a.y + r * Math.sin(t), tt));
    }
    return pts;
  };
  // Farm route stops: the stop's number drawn inside its ring as seven-segment strokes, in map space
  // (screen y down) so digits read upright whatever the world-to-map transform is.
  const SEGS = { a: [0, 0, 1, 0], b: [1, 0, 1, 1], c: [1, 1, 1, 2], d: [0, 2, 1, 2], e: [0, 1, 0, 2], f: [0, 0, 0, 1], g: [0, 1, 1, 1] };
  const DIGITS = ['abcdef', 'bc', 'abdeg', 'abcdg', 'bcfg', 'acdfg', 'acdefg', 'abc', 'abcdefg', 'abcdfg'];
  const digits = (map, tt, a, r, num) => {
    const c = map.view.translate(a.x, a.y, tt), e = map.view.translate(a.x + r, a.y, tt);
    const h = Math.hypot(e.x - c.x, e.y - c.y) * 0.5, w = h * 0.5, text = String(num).slice(-2);
    const at = (x, y) => Object.assign(Object.create(Object.getPrototypeOf(c)), c, { x, y });
    const out = [];
    [...text].forEach((ch, k) => {
      const left = c.x - (text.length * w * 1.6 - w * 0.6) / 2 + k * w * 1.6, top = c.y - h;
      for (const s of DIGITS[+ch] || '') { const [x1, y1, x2, y2] = SEGS[s]; out.push([at(left + x1 * w, top + y1 * h), at(left + x2 * w, top + y2 * h)]); }
    });
    return out;
  };
  const dashes = (p, q, n = 16) => {
    const at = (t) => Object.assign(Object.create(Object.getPrototypeOf(p)), p, { x: p.x + (q.x - p.x) * t, y: p.y + (q.y - p.y) * t });
    const out = [];
    for (let i = 0; i < n; i++) out.push([at(i / n), at((i + 0.55) / n)]);
    return out;
  };
  // Guild pin labels: strokes on a 4 x 6 cell, flat [x, y, x, y, ...] polylines, y down. The table is the
  // whitelist: a character not in it is dropped, so another player's text only ever becomes these strokes.
  const GLYPHS = Object.assign(Object.create(null), {
    A: [[0, 6, 0, 2, 2, 0, 4, 2, 4, 6], [0, 3, 4, 3]], B: [[0, 0, 0, 6, 3, 6, 4, 5, 4, 4, 3, 3, 0, 3], [0, 0, 3, 0, 4, 1, 4, 2, 3, 3]],
    C: [[4, 0, 0, 0, 0, 6, 4, 6]], D: [[0, 0, 0, 6, 2, 6, 4, 4, 4, 2, 2, 0, 0, 0]], E: [[4, 0, 0, 0, 0, 6, 4, 6], [0, 3, 3, 3]],
    F: [[4, 0, 0, 0, 0, 6], [0, 3, 3, 3]], G: [[4, 0, 0, 0, 0, 6, 4, 6, 4, 3, 2, 3]], H: [[0, 0, 0, 6], [4, 0, 4, 6], [0, 3, 4, 3]],
    I: [[1, 0, 3, 0], [2, 0, 2, 6], [1, 6, 3, 6]], J: [[4, 0, 4, 6, 0, 6, 0, 4]], K: [[0, 0, 0, 6], [4, 0, 0, 3, 4, 6]],
    L: [[0, 0, 0, 6, 4, 6]], M: [[0, 6, 0, 0, 2, 3, 4, 0, 4, 6]], N: [[0, 6, 0, 0, 4, 6, 4, 0]], O: [[0, 0, 4, 0, 4, 6, 0, 6, 0, 0]],
    P: [[0, 6, 0, 0, 4, 0, 4, 3, 0, 3]], Q: [[0, 0, 4, 0, 4, 6, 0, 6, 0, 0], [2, 4, 4, 6]], R: [[0, 6, 0, 0, 4, 0, 4, 3, 0, 3, 4, 6]],
    S: [[4, 0, 0, 0, 0, 3, 4, 3, 4, 6, 0, 6]], T: [[0, 0, 4, 0], [2, 0, 2, 6]], U: [[0, 0, 0, 6, 4, 6, 4, 0]], V: [[0, 0, 2, 6, 4, 0]],
    W: [[0, 0, 1, 6, 2, 3, 3, 6, 4, 0]], X: [[0, 0, 4, 6], [4, 0, 0, 6]], Y: [[0, 0, 2, 3, 4, 0], [2, 3, 2, 6]], Z: [[0, 0, 4, 0, 0, 6, 4, 6]],
    0: [[0, 0, 4, 0, 4, 6, 0, 6, 0, 0], [0, 6, 4, 0]], 1: [[1, 1, 2, 0, 2, 6], [1, 6, 3, 6]], 2: [[0, 0, 4, 0, 4, 3, 0, 3, 0, 6, 4, 6]],
    3: [[0, 0, 4, 0, 4, 6, 0, 6], [1, 3, 4, 3]], 4: [[0, 0, 0, 3, 4, 3], [4, 0, 4, 6]], 5: [[4, 0, 0, 0, 0, 3, 3, 3, 4, 4, 4, 6, 0, 6]],
    6: [[4, 0, 0, 0, 0, 6, 4, 6, 4, 3, 0, 3]], 7: [[0, 0, 4, 0, 1, 6]], 8: [[0, 0, 4, 0, 4, 6, 0, 6, 0, 0], [0, 3, 4, 3]],
    9: [[4, 3, 0, 3, 0, 0, 4, 0, 4, 6, 0, 6]], '-': [[0, 3, 4, 3]], '.': [[2, 5.5, 2, 6]], '!': [[2, 0, 2, 4], [2, 5.5, 2, 6]],
    '?': [[0, 1, 0, 0, 4, 0, 4, 3, 2, 3, 2, 4], [2, 5.5, 2, 6]], '/': [[4, 0, 0, 6]], ' ': [],
  });
  const LABEL_MAX = 16, PIN_RING = 3000;
  // the label centred under a ring of radius r, in map space (screen y down) so it reads upright
  const glyphLines = (map, tt, a, r, text) => {
    const c = map.view.translate(a.x, a.y, tt), e = map.view.translate(a.x + r, a.y, tt);
    const rpx = Math.hypot(e.x - c.x, e.y - c.y), h = rpx * 0.5, w = h * 0.6, step = w * 1.3;
    const at = (x, y) => Object.assign(Object.create(Object.getPrototypeOf(c)), c, { x, y });
    const chars = [...text].filter((ch) => GLYPHS[ch]).slice(0, LABEL_MAX);
    const left = c.x - (chars.length * step - (step - w)) / 2, top = c.y + rpx * 1.2;
    const out = [];
    chars.forEach((ch, k) => {
      for (const line of GLYPHS[ch]) {
        const pts = [];
        for (let i = 0; i < line.length; i += 2) pts.push(at(left + k * step + (line[i] / 4) * w, top + (line[i + 1] / 6) * h));
        out.push(pts);
      }
    });
    return out;
  };
  const drawPin = (map, tt, o) => {
    const style = { color: o.mine ? 'rgba(255,255,255,0.9)' : 'rgba(255,255,255,0.7)', weight: 2 };
    dd.altLayer.addPolyline(ring(map, tt, o, PIN_RING), style);
    if (o.mine) dd.altLayer.addPolyline(ring(map, tt, o, PIN_RING / 2), style);
    const label = String((o.body && o.body.label) || '').toUpperCase();
    for (const pts of glyphLines(map, tt, o, PIN_RING, label)) dd.altLayer.addPolyline(pts, style);
  };
  const RENDER = Object.assign(Object.create(null), { pin: drawPin });
  const redrawAlternatives = (force) => {
    const map = liveMap();
    if (!map) return 'no-map';
    const now = Date.now();
    const pose = dd.pose;
    if (!force) {
      if (!dd.alternatives.length && !dd.objects.length && !(dd.altLayer && dd.altLayer.vectors && dd.altLayer.vectors.size)) return 'idle';
      if (now - altDrawnAt < 1000) return 'throttled';
      if (pose && altAnchor && Math.hypot(pose.x - altAnchor.x, pose.y - altAnchor.y) < 3000) return 'still';
    }
    try {
      if (!dd.altLayer || dd.altMap !== map) {
        dd.altLayer = map.layers.createStaticLayer();
        map.layers.addLayer(dd.altLayer, { z: 11 }); // just under the main route line (z 12)
        dd.altMap = map;
      }
      dd.altLayer.clearVectors();
      dd.stats.redraws++;
      const tt = calibrate(map);
      if (!tt || !pose || (!dd.alternatives.length && !dd.objects.length)) return tt ? 'empty' : 'uncalibrated';
      const from = map.view.translate(pose.x, pose.y, tt);
      for (const a of dd.alternatives) {
        if (!Number.isFinite(a.x) || !Number.isFinite(a.y)) continue; // peer items come from other players: one bad item must not blank the rest
        const faded = a.pulse && dd.altPhase;
        const style = { color: faded ? dim(a.color) : a.color, weight: a.weight };
        // peer / member items are rings only (never a line to you, never pulsed); a missing ring means 4000 cm
        if (a.ring || a.kind === 'peer' || a.kind === 'member') {
          const r = a.ring || 4000;
          dd.altLayer.addPolyline(ring(map, tt, a, faded ? r * 1.25 : r), style);
          if (Number.isInteger(a.num)) for (const seg of digits(map, tt, a, r, a.num)) dd.altLayer.addPolyline(seg, style);
          continue;
        }
        const to = map.view.translate(a.x, a.y, tt);
        for (const seg of a.dash ? dashes(from, to) : [[from, to]]) dd.altLayer.addPolyline(seg, style);
      }
      // guild objects come from other players: unknown kinds are skipped, one bad object never blanks the rest
      for (const o of dd.objects) {
        const draw = RENDER[o.kind];
        if (!draw || !Number.isFinite(o.x) || !Number.isFinite(o.y)) continue;
        try { draw(map, tt, o); } catch (e) { report('objects', e); }
      }
      altDrawnAt = now;
      altAnchor = pose ? { x: pose.x, y: pose.y } : null;
      if (map.requestRender) map.requestRender();
      return 'drawn';
    } catch (e) {
      report('lines', e);
      return 'error: ' + e;
    }
  };
  dd.redrawAlternatives = redrawAlternatives;
  dd.altPhase = false;
  dd.pulse = () => {
    if (!dd.alternatives.some((a) => a.pulse)) return 'idle';
    dd.altPhase = !dd.altPhase;
    return redrawAlternatives(true);
  };
  // the beat runs only while something pulses: no timer work on a page with nothing to animate
  let pulseTimer = 0;
  const pulseWhenNeeded = () => {
    const want = dd.alternatives.some((a) => a && a.pulse);
    if (want && !pulseTimer) pulseTimer = setInterval(() => { try { dd.pulse(); } catch (err) { report('pulse', err); } }, 900);
    else if (!want && pulseTimer) { clearInterval(pulseTimer); pulseTimer = 0; dd.altPhase = false; }
  };

  // (grid cell, x, y) of the page's own markers: the detector fits the map grid from these, so
  // grid cells and PvP rows follow the map as the companion draws it, week after week.
  dd.gridSamples = (limit = 400) => {
    const out = [];
    for (const map of dd.maps) {
      for (const layer of (map && map._createdLayers) || []) {
        const items = layer && layer._items;
        if (!items || typeof items.values !== 'function') continue;
        for (const it of items.values()) {
          const d = it && it.data;
          if (d && typeof d.gridCell === 'string' && /^[A-Z][0-9]{1,2}$/.test(d.gridCell)
              && Number.isFinite(d.x) && Number.isFinite(d.y) && !(d.x === 0 && d.y === 0)) {
            out.push([d.gridCell, d.x, d.y]);
            if (out.length >= limit) return out;
          }
        }
      }
    }
    return out;
  };
  // Same pairs from the map data file the page downloaded (every location carries its grid cell),
  // for pages that draw no labelled markers themselves (the minimap, the in-game overlay).
  dd.gridSamplesFromData = async (limit = 600) => {
    const urls = performance.getEntriesByType('resource').map((e) => e.name).filter((u) => /\/maps\/[^/?]+\.d\.json/.test(u));
    if (!urls.length) return [];
    const raw = await (await origFetch(urls[urls.length - 1])).json();
    if (!Array.isArray(raw)) return [];
    // compact "devalue" layout: every field holds the index of its value in the same array
    const out = [];
    for (const o of raw) {
      if (!o || typeof o !== 'object' || Array.isArray(o) || !('gridCell' in o) || !('location' in o)) continue;
      const cell = raw[o.gridCell], loc = raw[o.location];
      if (typeof cell !== 'string' || !/^[A-Z][0-9]{1,2}$/.test(cell) || !loc || typeof loc !== 'object') continue;
      const x = raw[loc.x], y = raw[loc.y];
      if (Number.isFinite(x) && Number.isFinite(y) && !(x === 0 && y === 0)) {
        out.push([cell, x, y]);
        if (out.length >= limit) break;
      }
    }
    return out;
  };

  // The named shipwrecks ("The Wreck of the Pallas") from the same map data file: fixed places to loot.
  dd.shipwrecksFromData = async (limit = 500) => {
    const urls = performance.getEntriesByType('resource').map((e) => e.name).filter((u) => /\/maps\/[^/?]+\.d\.json/.test(u));
    if (!urls.length) return null;
    const url = urls[urls.length - 1];
    const raw = await (await origFetch(url)).json();
    if (!Array.isArray(raw)) return null;
    const map = (url.match(/\/maps\/([^/?]+)\.d\.json/) || [])[1] || null; // the detector checks it is your map
    const seen = new Set(), out = [];
    for (const o of raw) {
      if (!o || typeof o !== 'object' || Array.isArray(o) || !('iconId' in o) || !('location' in o)) continue;
      const name = raw[o.name], loc = raw[o.location];
      if (raw[o.iconId] !== 'shipwreck' || typeof name !== 'string' || !loc || typeof loc !== 'object') continue;
      const x = raw[loc.x], y = raw[loc.y], z = raw[loc.z];
      const key = name + ':' + Math.round(x) + ':' + Math.round(y);
      if (!Number.isFinite(x) || !Number.isFinite(y) || seen.has(key)) continue;
      seen.add(key);
      out.push([name, x, y, Number.isFinite(z) ? z : 0]);
      if (out.length >= limit) break;
    }
    return { map, wrecks: out };
  };

  // A problem in the detector: a small notice in the map window (never the in-game overlay or
  // minimap). It points to the settings page, where the report is. "Hide" keeps it away until reload.
  const renderProblem = (p) => {
    let box = document.getElementById('__dd_problem');
    if (!p || !p.text || !/^\/dune\/?$/.test(location.pathname) || sessionStorage.getItem('__dd_problem_hidden')) {
      if (box) box.remove();
      return;
    }
    if (!box) {
      dd.stats.dom++;
      box = document.createElement('div');
      box.id = '__dd_problem';
      box.setAttribute('role', 'status');
      box.style.cssText = 'position:fixed;right:12px;bottom:12px;z-index:2147483647;max-width:360px;display:flex;gap:8px;'
        + 'align-items:flex-start;padding:8px 10px;border-radius:8px;background:rgba(40,16,14,.95);color:#f3d7d2;'
        + 'font:12px/1.4 system-ui,sans-serif;border:1px solid #e0645a;box-shadow:0 2px 10px rgba(0,0,0,.5)';
      const text = document.createElement('div');
      text.id = '__dd_problem_text';
      const hide = document.createElement('button');
      hide.textContent = 'Hide';
      hide.style.cssText = 'background:#2a241b;color:#eadfcb;border:1px solid #3a3226;border-radius:4px;padding:1px 6px;font:inherit;cursor:pointer';
      hide.onclick = () => { sessionStorage.setItem('__dd_problem_hidden', '1'); box.remove(); };
      box.append(text, hide);
      (document.body || document.documentElement).appendChild(box);
    }
    document.getElementById('__dd_problem_text').textContent = p.text;
  };

  // Each part of the state as last applied: a push applies only the parts that changed, so an
  // unrelated change never makes the page re-process its actor list or our layer.
  const applied = {};
  const changed = (part, value) => {
    let sig;
    try { sig = JSON.stringify(value); } catch (_) { delete applied[part]; return true; } // unreadable: apply as before
    if (applied[part] === sig) return false;
    applied[part] = sig;
    return true;
  };
  dd.set = (state) => {
    state = state || {};
    dd.stats.sets++;
    dd.dest = state.dest || null;
    dd.alternatives = state.alternatives || [];
    dd.objects = state.objects || [];
    dd.route = state.route || null;
    dd.extraActors = state.extraActors || [];
    dd.stale = new Set(state.stale || []);
    const route = changed('route', dd.route);
    if (changed('dest', dd.dest)) applyDest();
    if (route) applyRoute();
    if (changed('extraActors', dd.extraActors)) applyActors();
    if (changed('problem', state.problem || null)) {
      try { renderProblem(state.problem); } catch (err) { report('problem', err); }
    }
    pulseWhenNeeded();
    // the page redraws its route line after applyRoute; draw ours right after it (a new route can
    // be the only calibration reference)
    if (changed('layer', [dd.alternatives, dd.objects]) || (route && !dd.altTransform)) {
      setTimeout(() => redrawAlternatives(true), 30);
    }
    return 'ok';
  };

  // No guild key accepted (gate.py): the picker goes away with everything else of ours.
  dd.hidePicker = () => {
    const box = document.getElementById('__dd_picker');
    if (box) box.remove();
    return 'ok';
  };

  // Resource / vehicle picker (map window only; the minimap overlay is not interactive).
  dd.renderPicker = (resources, vehicles, selected, vehicle, status, minimap) => {
    // only in the companion's map window (/dune), never the in-game overlay, minimap or other pages
    if (!/^\/dune\/?$/.test(location.pathname)) {
      const stray = document.getElementById('__dd_picker');
      if (stray) stray.remove();
      return 'skip';
    }
    let box = document.getElementById('__dd_picker');
    if (!box) {
      dd.stats.dom++;
      box = document.createElement('div');
      box.id = '__dd_picker';
      box.style.cssText = 'position:fixed;top:56px;right:12px;z-index:2147483647;display:flex;gap:6px;align-items:center;'
        + 'padding:6px 8px;border-radius:8px;background:rgba(20,17,13,.92);color:#eadfcb;font:12px system-ui,sans-serif;'
        + 'box-shadow:0 2px 10px rgba(0,0,0,.5);border:1px solid rgba(192,124,240,.5)';
      const mk = (id, onChange) => {
        const s = document.createElement('select');
        s.id = id;
        s.style.cssText = 'background:#2a241b;color:#eadfcb;border:1px solid #3a3226;border-radius:4px;padding:2px 4px;font:inherit';
        s.onchange = () => onChange(s.value);
        return s;
      };
      const label = document.createElement('span');
      label.textContent = 'Find';
      label.style.color = '#c07cf0';
      const res = mk('__dd_res', (v) => emit({ type: 'ui', action: 'resource', value: v }));
      const veh = mk('__dd_veh', (v) => emit({ type: 'ui', action: 'vehicle', value: v }));
      const st = document.createElement('span');
      st.id = '__dd_status';
      st.style.cssText = 'color:#9a8c74;max-width:320px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis';
      const mm = document.createElement('button');
      mm.id = '__dd_mm_btn';
      mm.textContent = 'Minimap ▾';
      mm.title = 'Choose which resources the minimap shows (the big map is not affected)';
      mm.style.cssText = 'background:#2a241b;color:#eadfcb;border:1px solid #3a3226;border-radius:4px;padding:2px 6px;font:inherit;cursor:pointer';
      const panel = document.createElement('div');
      panel.id = '__dd_mm_panel';
      panel.style.cssText = 'display:none;position:absolute;top:100%;right:0;margin-top:4px;padding:8px;min-width:210px;max-height:60vh;overflow:auto;'
        + 'background:rgba(20,17,13,.97);border:1px solid rgba(192,124,240,.5);border-radius:8px;box-shadow:0 4px 14px rgba(0,0,0,.6)';
      mm.onclick = () => { panel.style.display = panel.style.display === 'none' ? 'block' : 'none'; };
      box.style.position = 'fixed';
      box.append(label, res, veh, mm, st, panel);
      document.body.appendChild(box);
    }
    if (minimap) {
      const panel = document.getElementById('__dd_mm_panel');
      const sig = JSON.stringify(minimap);
      if (panel.dataset.sig !== sig) {
        dd.stats.dom++;
        panel.dataset.sig = sig;
        panel.innerHTML = '';
        const row = (text, checked, onChange, bold) => {
          const l = document.createElement('label');
          l.style.cssText = 'display:flex;gap:6px;align-items:center;padding:2px 0;cursor:pointer;white-space:nowrap' + (bold ? ';font-weight:600;color:#c07cf0' : '');
          const c = document.createElement('input');
          c.type = 'checkbox'; c.checked = checked; c.onchange = () => onChange(c.checked);
          l.append(c, document.createTextNode(text));
          panel.appendChild(l);
        };
        for (const [key, name, shown] of minimap.items) row(name, shown, () => emit({ type: 'ui', action: 'minimap_toggle', value: key }));
        if ((minimap.extras || []).length) {
          const hr2 = document.createElement('div'); hr2.style.cssText = 'border-top:1px solid #3a3226;margin:6px 0'; panel.appendChild(hr2);
          for (const [key, name, shown] of minimap.extras) row(name, shown, () => emit({ type: 'ui', action: 'minimap_extra', value: key }));
        }
        const note = document.createElement('div');
        note.style.cssText = 'color:#9a8c74;font-size:11px;margin-top:6px;white-space:normal';
        note.textContent = minimap.lines ? 'Lines are on: one resource at a time (ticking another switches to it).' : 'Unticked resources are hidden on the minimap only.';
        panel.appendChild(note);
      }
    }
    const fill = (id, opts, value) => {
      const s = document.getElementById(id);
      const sig = JSON.stringify(opts);
      if (s.dataset.sig !== sig) {
        dd.stats.dom++;
        s.innerHTML = '';
        for (const [v, t] of opts) { const o = document.createElement('option'); o.value = v; o.textContent = t; s.appendChild(o); }
        s.dataset.sig = sig;
      }
      s.value = value;
    };
    fill('__dd_res', resources, selected);
    fill('__dd_veh', vehicles, vehicle);
    document.getElementById('__dd_status').textContent = status || '';
    return 'ok';
  };

  // Minimap-only resource filter. The companion keeps a separate deselected-types list for the
  // minimap in localStorage but offers no way to edit it; it reads it on load, so we write it
  // and reload this page. Non-resource choices the player made are left untouched.
  dd.setMinimapHidden = (hiddenMarkers, resourceMarkers) => {
    if (!location.pathname.includes('minimap')) return 'skip';
    const KEY = 'dune.companion.minimap.deselectedMarkerTypes';
    let current = [];
    try { current = JSON.parse(localStorage.getItem(KEY) || '[]'); } catch (_) {}
    const managed = new Set(resourceMarkers);
    const next = [...current.filter((m) => !managed.has(m)), ...hiddenMarkers];
    if (JSON.stringify([...new Set(next)].sort()) === JSON.stringify([...new Set(current)].sort())) return 'unchanged';
    localStorage.setItem(KEY, JSON.stringify(next));
    setTimeout(() => location.reload(), 50);
    return 'reloading';
  };

  // Minimap on / off from our hotkey, whatever you are doing. The companion's own "Toggle Minimap"
  // keeps its per-activity widget settings (Exploration, Combat, Driving, building), so it cannot
  // show the minimap where those hide it. This changes those settings with the companion's own
  // commands: shown in every activity, or switched off.
  const ACTIVITIES = ['Exploration', 'Combat', 'Driving'];
  let cmdId = 7_000_000;  // far above the page's own command ids
  dd.toggleMinimap = () => {
    const list = dd.widgets && Array.isArray(dd.widgets.widgets) ? dd.widgets.widgets : [];
    let w = list.find((x) => x && x.key === 'dune.minimap');
    const all = w && Array.isArray(w.supported) ? ACTIVITIES.filter((a) => w.supported.includes(a)) : ACTIVITIES;
    const everywhere = !!(w && w.enabled && all.every((a) => (w.activities || []).includes(a)));
    const show = !everywhere;
    const cmd = (name, payload) => post({ type: 'cmd', id: cmdId++, cmd: name, payload: { key: 'dune.minimap', ...payload } });
    if (show) {
      cmd('overlay.setWidgetActivities', { activities: all });
      cmd('overlay.setWidgetBuildModeVisibility', { visible: true });
    }
    cmd('overlay.setWidgetVisibility', { enabled: show });
    // the companion confirms with a new dune.overlayWidgets state; until then a second press must flip back
    if (!w) { w = { key: 'dune.minimap' }; dd.widgets = { widgets: [...list, w] }; }
    w.enabled = show;
    if (show) w.activities = all;
    return show ? 'shown' : 'hidden';
  };

  // Small in-game badge on the minimap overlay: current search and vehicle.
  dd.renderBadge = (text) => {
    if (!location.pathname.includes('minimap')) return 'skip';
    let b = document.getElementById('__dd_badge');
    if (!text) { if (b) b.remove(); return 'ok'; }
    if (!b) {
      dd.stats.dom++;
      b = document.createElement('div');
      b.id = '__dd_badge';
      b.style.cssText = 'position:fixed;left:50%;bottom:4px;transform:translateX(-50%);z-index:2147483647;pointer-events:none;'
        + 'padding:2px 8px;border-radius:10px;background:rgba(20,17,13,.8);color:#eadfcb;font:11px system-ui,sans-serif;'
        + 'border:1px solid rgba(192,124,240,.55);white-space:nowrap';
      (document.body || document.documentElement).appendChild(b);
    }
    b.textContent = text;
    return 'ok';
  };

  // Dead-man switch: if the detector stops (even killed), undo everything we added.
  dd.lastPing = Date.now();
  // 'ok' only once this page instance has received state; a reloaded page answers 'empty'.
  dd.ping = () => { dd.lastPing = Date.now(); return dd.hasState && !dd.cleared ? 'ok' : 'empty'; };
  setInterval(() => {
    if (Date.now() - dd.lastPing < 60000 || dd.cleared) return; // survives a detector restart
    dd.cleared = true;
    origSet({}); // not dd.set: that marks the page as holding state and restarts this timer
    dd.renderBadge(null);
    const pk = document.getElementById('__dd_picker');
    if (pk) pk.remove();
  }, 5000);
  const origSet = dd.set;
  dd.set = (state) => { dd.cleared = false; dd.hasState = true; dd.lastPing = Date.now(); return origSet(state); };

  subscribe();
  return 'installed';
})()
