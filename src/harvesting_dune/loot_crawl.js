// Runs inside a companion page (same site as the data CDN, so Cloudflare lets it through).
// Builds a per-container-location loot table by inverting gaming.tools item "dropLocations".
(async () => {
  const BASE = 'https://cdn-hosted.gaming.tools/dune/data';
  const devalue = (arr) => {
    const memo = new Map();
    const h = (i) => {
      if (typeof i !== 'number' || i < 0) return null;
      if (memo.has(i)) return memo.get(i);
      const v = arr[i];
      let out;
      if (Array.isArray(v)) { out = []; memo.set(i, out); for (const x of v) out.push(h(x)); }
      else if (v && typeof v === 'object') { out = {}; memo.set(i, out); for (const k of Object.keys(v)) out[k] = h(v[k]); }
      else { out = v; memo.set(i, out); }
      return out;
    };
    return h(0);
  };
  const get = async (path) => {
    const r = await fetch(BASE + path);
    if (!r.ok) return null;
    return devalue(await r.json());
  };
  const t0 = Date.now();
  // a refused or empty crawl must fail: a result would replace the good caches with nothing for 7 days
  const entities = await get('/en/entities.d.json');
  if (!Array.isArray(entities)) throw new Error('entities.d.json unavailable');
  const items = entities.filter((e) => e && e.mainCategoryId === 'items');
  if (!items.length) throw new Error('entities.d.json lists no items');
  const containers = new Map(); // "map|x|y" -> {map, x, y, z, name, drops: []}
  // materials you can go and loot (Diamondine Dust...): every drop location, all container kinds
  const MATERIAL = /^items\/misc\/(components|refinedresources|rawresources)/;  // never consumables
  const materials = [];
  // unique gear and schematics: found only by chance, from any source on any map (testing stations too)
  const uniques = [];
  const mapNames = {};
  let fetched = 0, withDrops = 0, failed = 0;
  const queue = items.slice();
  const worker = async () => {
    while (queue.length) {
      const it = queue.shift();
      let d = null;
      try { d = await get(`/en/items/${it.id}.d.json`); } catch (_) {}
      fetched++;
      if (!d) { failed++; continue; }
      const cats = d.categories || [];
      // unique gear: the game drops its schematic (you craft the item), so gaming.tools keeps the drop
      // places on d.schematic; some schematics are sold instead, some items are contract rewards
      const rare = !cats.some((c) => MATERIAL.test(c) || /consumable/i.test(c))
        && (d.rarity === 'Unique' || !!d.isSchematic || (d.itemTags || []).some((t) => /unique/i.test(t)));
      if (rare) {
        const sc = d.schematic || {};
        const drops = [];
        for (const m of [...(d.dropLocations || []), ...(sc.dropLocations || [])]) {
          if (m && m.map && m.map.id && m.map.name) mapNames[m.map.id] = m.map.name;
          for (const loc of (m && m.locations) || []) {
            drops.push([m.map && m.map.id, Math.round(loc.x), Math.round(loc.y), Math.round(loc.z),
              loc.name || '', loc.chance || 0, loc.minQuantity || 1, loc.maxQuantity || 1]);
          }
        }
        // an empty list is truthy in JS: take the first list that names a vendor
        const vendors = [d.schematicSoldBy, sc.soldBy, d.soldBy].find((v) => Array.isArray(v) && v.length) || [];
        const reward = (d.rewardFrom || []).map((r) => r && r.name).filter(Boolean).slice(0, 4);
        if (drops.length || vendors.length || reward.length) {
          uniques.push({ id: d.id, name: d.name, icon: d.iconPath, rarity: d.rarity, tier: d.tier, categories: cats,
            schematic: !!d.isSchematic, via: (sc.dropLocations || []).length || (d.schematicSoldBy || []).length || (sc.soldBy || []).length ? 'schematic' : 'item',
            sold: vendors.slice(0, 6).map((s) => [s.entity && s.entity.name, s.price || 0, s.stockAmount || null]),
            reward, drops });
        }
      }
      // a cage drops unique gear as its schematic: those places go into the cage tables as well
      const own = Array.isArray(d.dropLocations) ? d.dropLocations : [];
      const viaSchematic = rare && Array.isArray((d.schematic || {}).dropLocations) ? d.schematic.dropLocations : [];
      if (!own.length && !viaSchematic.length) continue;
      withDrops++;
      if (cats.some((c) => MATERIAL.test(c))) {
        const drops = [];
        for (const m of d.dropLocations) {
          for (const loc of (m && m.locations) || []) {
            drops.push([m.map && m.map.id, Math.round(loc.x), Math.round(loc.y), Math.round(loc.z),
              loc.name || '', loc.chance || 0, loc.minQuantity || 1, loc.maxQuantity || 1]);
          }
        }
        materials.push({
          id: d.id, name: d.name, icon: d.iconPath, rarity: d.rarity, tier: d.tier, categories: cats,
          sold: (d.soldBy || []).slice(0, 6).map((s) => [s.entity && s.entity.name, s.price || 0, s.stockAmount || null]),
          drops,
        });
      }
      for (const m of [...own, ...viaSchematic]) {
        const schematic = !!d.isSchematic || viaSchematic.includes(m);
        const mapId = m && m.map && m.map.id;
        if (mapId && m.map.name) mapNames[mapId] = m.map.name;
        for (const loc of (m && m.locations) || []) {
          if (!/container|rare/i.test(loc.name || '')) continue; // 'Small Ultra Rare' names no container
          const key = `${mapId}|${Math.round(loc.x)}|${Math.round(loc.y)}|${loc.name}`;
          let c = containers.get(key);
          if (!c) containers.set(key, (c = { map: mapId, x: loc.x, y: loc.y, z: loc.z, name: loc.name, drops: [] }));
          c.drops.push({
            id: d.id, name: d.name, rarity: d.rarity, tier: d.tier, icon: d.iconPath,
            chance: loc.chance, min: loc.minQuantity, max: loc.maxQuantity, price: d.baseBuyFromVendorPrice || 0,
            unique: (d.itemTags || []).some((t) => /unique/i.test(t)) || /unique/i.test(d.id),
            schematic,
            categories: d.categories || [],
          });
        }
      }
    }
  };
  await Promise.all(Array.from({ length: 12 }, worker));
  // a few missing item files are the CDN's own gaps; more means it refused us (Cloudflare, outage)
  if (failed > items.length / 10) throw new Error(`${failed} of ${items.length} item files unavailable`);
  return JSON.stringify({ built: Date.now(), ms: Date.now() - t0, items: items.length, fetched, failed, withDrops,
    containers: [...containers.values()], materials, uniques, mapNames });
})()
