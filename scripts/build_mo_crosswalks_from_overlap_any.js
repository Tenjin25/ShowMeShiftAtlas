#!/usr/bin/env node
/**
 * Build Missouri "precinct" -> 2022 district overlap crosswalks from polygon overlap weights.
 *
 * This is a generalized version of `scripts/build_mo_crosswalks_from_overlap.js` that can be
 * used for VTD00 / VTD10 / VTD20 precinct geometries.
 *
 * Usage:
 *   node scripts/build_mo_crosswalks_from_overlap_any.js --precincts Data/mo_vtd00_precincts.geojson --outPrefix vtd00
 *   node scripts/build_mo_crosswalks_from_overlap_any.js --precincts Data/mo_vtd10_precincts.geojson --outPrefix vtd10
 *   node scripts/build_mo_crosswalks_from_overlap_any.js --precincts Data/mo_vtd20_precincts.geojson --outPrefix vtd20
 *
 * Output (examples):
 *   Data/crosswalks/vtd00_to_cd118_overlap.csv
 *   Data/crosswalks/vtd00_to_2022_state_house_overlap.csv
 *   Data/crosswalks/vtd00_to_2022_state_senate_overlap.csv
 */
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const DATA_DIR = path.join(ROOT, 'Data');
const CROSSWALK_DIR = path.join(DATA_DIR, 'crosswalks');

function parseArgs(argv) {
  const out = { precincts: '', outPrefix: '' };
  const args = Array.from(argv || []);
  for (let i = 0; i < args.length; i += 1) {
    const a = String(args[i] || '').trim();
    if (a === '--precincts') {
      out.precincts = String(args[i + 1] || '').trim();
      i += 1;
      continue;
    }
    if (a === '--outPrefix') {
      out.outPrefix = String(args[i + 1] || '').trim();
      i += 1;
      continue;
    }
  }
  if (!out.precincts) throw new Error('Missing --precincts <geojson>');
  if (!out.outPrefix) {
    out.outPrefix = path.basename(out.precincts).replace(/\.geojson$/i, '').replace(/^mo_/, '');
  }
  return out;
}

function csvCell(value) {
  const s = String(value ?? '');
  if (!/[",\n\r]/.test(s)) return s;
  return `"${s.replace(/"/g, '""')}"`;
}

function fnv1a32(str) {
  let h = 0x811c9dc5;
  for (let i = 0; i < str.length; i += 1) {
    h ^= str.charCodeAt(i);
    h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) >>> 0;
  }
  return h >>> 0;
}

function xorshift32(seed) {
  let x = (seed >>> 0) || 0x1a2b3c4d;
  return () => {
    x ^= (x << 13) >>> 0;
    x ^= (x >>> 17) >>> 0;
    x ^= (x << 5) >>> 0;
    return x >>> 0;
  };
}

function bboxInit() {
  return [Infinity, Infinity, -Infinity, -Infinity];
}

function bboxExtend(bbox, x, y) {
  if (x < bbox[0]) bbox[0] = x;
  if (y < bbox[1]) bbox[1] = y;
  if (x > bbox[2]) bbox[2] = x;
  if (y > bbox[3]) bbox[3] = y;
}

function bboxFromCoords(coords) {
  const bbox = bboxInit();
  const stack = [coords];
  while (stack.length) {
    const node = stack.pop();
    if (!Array.isArray(node) || node.length === 0) continue;
    if (typeof node[0] === 'number' && typeof node[1] === 'number') {
      bboxExtend(bbox, node[0], node[1]);
      continue;
    }
    for (let i = 0; i < node.length; i += 1) stack.push(node[i]);
  }
  return bbox;
}

function bboxIntersects(a, b) {
  return !(a[2] < b[0] || a[0] > b[2] || a[3] < b[1] || a[1] > b[3]);
}

function bboxContainsPoint(b, x, y) {
  return x >= b[0] && x <= b[2] && y >= b[1] && y <= b[3];
}

function pointInRing(x, y, ring) {
  let inside = false;
  const n = ring.length;
  if (n < 3) return false;
  let j = n - 1;
  for (let i = 0; i < n; i += 1) {
    const xi = ring[i][0];
    const yi = ring[i][1];
    const xj = ring[j][0];
    const yj = ring[j][1];
    const crosses = (yi > y) !== (yj > y);
    if (crosses) {
      const denom = (yj - yi) || 1e-300;
      const xIntersect = (xj - xi) * (y - yi) / denom + xi;
      if (x < xIntersect) inside = !inside;
    }
    j = i;
  }
  return inside;
}

function pointInPolygonRings(x, y, rings) {
  let inside = false;
  for (let i = 0; i < rings.length; i += 1) {
    if (pointInRing(x, y, rings[i])) inside = !inside;
  }
  return inside;
}

function pointInGeometry(x, y, geom) {
  if (!geom) return false;
  const type = geom.type;
  const coords = geom.coordinates;
  if (!coords) return false;
  if (type === 'Polygon') return pointInPolygonRings(x, y, coords);
  if (type === 'MultiPolygon') {
    for (let i = 0; i < coords.length; i += 1) {
      if (pointInPolygonRings(x, y, coords[i])) return true;
    }
    return false;
  }
  return false;
}

function normalizePrecinctKey(raw) {
  return String(raw || '').replace(/\u00a0/g, ' ').trim().toUpperCase();
}

function normalizeDistrictNum(raw) {
  const text = String(raw || '').trim();
  if (!text) return '';
  const digits = text.replace(/[^0-9]/g, '');
  if (digits) return String(Number(digits));
  return text.toUpperCase();
}

function findDistrictForPoint(x, y, candidates) {
  for (let i = 0; i < candidates.length; i += 1) {
    const d = candidates[i];
    if (!bboxContainsPoint(d.bbox, x, y)) continue;
    if (pointInGeometry(x, y, d.geom)) return d.district;
  }
  return '';
}

function sampleWeightsForPrecinct(precinctKey, precinctGeom, precinctBbox, candidates, opts) {
  if (candidates.length === 0) return new Map();
  if (candidates.length === 1) return new Map([[candidates[0].district, 1]]);

  const seed = fnv1a32(precinctKey);
  const rnd = xorshift32(seed);
  const [minX, minY, maxX, maxY] = precinctBbox;
  const spanX = maxX - minX;
  const spanY = maxY - minY;

  function runSampling(targetInside, maxAttempts) {
    const counts = new Map();
    let inside = 0;
    let attempts = 0;
    while (attempts < maxAttempts && inside < targetInside) {
      attempts += 1;
      const rx = rnd() / 0xffffffff;
      const ry = rnd() / 0xffffffff;
      const x = minX + rx * spanX;
      const y = minY + ry * spanY;
      if (!pointInGeometry(x, y, precinctGeom)) continue;
      const district = findDistrictForPoint(x, y, candidates);
      if (!district) continue;
      inside += 1;
      counts.set(district, (counts.get(district) || 0) + 1);
    }
    return { counts, inside };
  }

  const quick = runSampling(opts.quickInside, opts.quickMaxAttempts);
  if (quick.inside > 0 && quick.counts.size === 1) {
    const only = Array.from(quick.counts.keys())[0];
    return new Map([[only, 1]]);
  }

  const full = runSampling(opts.fullInside, opts.fullMaxAttempts);
  const total = full.inside;
  if (!(total > 0) || full.counts.size === 0) return new Map([[candidates[0].district, 1]]);

  const weights = new Map();
  for (const [district, count] of full.counts.entries()) {
    const w = Number(count || 0) / total;
    if (w > 0) weights.set(district, w);
  }
  return weights;
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const precinctsPath = path.isAbsolute(args.precincts) ? args.precincts : path.join(ROOT, args.precincts);

  const BUILDS = [
    {
      label: 'congressional (CD118)',
      districtsGeojson: path.join(DATA_DIR, 'mo_congressional_districts_2022.geojson'),
      outCsv: path.join(CROSSWALK_DIR, `${args.outPrefix}_to_cd118_overlap.csv`)
    },
    {
      label: 'state_house (SLDL 2022)',
      districtsGeojson: path.join(DATA_DIR, 'mo_state_house_districts_2022.geojson'),
      outCsv: path.join(CROSSWALK_DIR, `${args.outPrefix}_to_2022_state_house_overlap.csv`)
    },
    {
      label: 'state_senate (SLDU 2022)',
      districtsGeojson: path.join(DATA_DIR, 'mo_state_senate_districts_2022.geojson'),
      outCsv: path.join(CROSSWALK_DIR, `${args.outPrefix}_to_2022_state_senate_overlap.csv`)
    }
  ];

  const precinctsPayload = JSON.parse(fs.readFileSync(precinctsPath, 'utf8'));
  const precincts = (precinctsPayload.features || [])
    .map(f => {
      const props = f.properties || {};
      const key = normalizePrecinctKey(props.precinct_norm || props.precinct_name || props.precinct || '');
      if (!key || !f.geometry) return null;
      const bbox = bboxFromCoords(f.geometry.coordinates);
      return { key, bbox, geom: f.geometry };
    })
    .filter(Boolean);

  if (!precincts.length) throw new Error(`No precinct polygons loaded from ${precinctsPath}`);
  fs.mkdirSync(CROSSWALK_DIR, { recursive: true });

  const opts = {
    quickInside: 40,
    quickMaxAttempts: 4000,
    fullInside: 420,
    fullMaxAttempts: 60000
  };

  for (const build of BUILDS) {
    const districtsPayload = JSON.parse(fs.readFileSync(build.districtsGeojson, 'utf8'));
    const districts = (districtsPayload.features || [])
      .map(f => {
        const props = f.properties || {};
        const district = normalizeDistrictNum(props.district || props.CD118FP || props.SLDLST || props.SLDUST || props.GEOID || '');
        if (!district || !f.geometry) return null;
        const bbox = bboxFromCoords(f.geometry.coordinates);
        return { district, bbox, geom: f.geometry };
      })
      .filter(Boolean);

    if (!districts.length) throw new Error(`No districts loaded from ${build.districtsGeojson}`);

    const out = fs.createWriteStream(build.outCsv, { encoding: 'utf8' });
    out.write('precinct_key,district_num,area_weight\n');

    let written = 0;
    for (let i = 0; i < precincts.length; i += 1) {
      const p = precincts[i];
      const candidates = [];
      for (let j = 0; j < districts.length; j += 1) {
        const d = districts[j];
        if (bboxIntersects(p.bbox, d.bbox)) candidates.push(d);
      }
      const weights = sampleWeightsForPrecinct(p.key, p.geom, p.bbox, candidates, opts);
      for (const [district, weight] of weights.entries()) {
        out.write(`${csvCell(p.key)},${csvCell(district)},${Number(weight).toFixed(6)}\n`);
        written += 1;
      }
      if ((i + 1) % 500 === 0) {
        console.log(`[${build.label}] Processed ${i + 1}/${precincts.length} precincts (wrote ${written} rows)...`);
      }
    }

    out.end();
    console.log(`[${build.label}] Wrote ${written} rows to ${build.outCsv}`);
  }
}

main();

