#!/usr/bin/env node
/**
 * Build district slices for statewide contests using a precinct-overlap crosswalk.
 *
 * Usage:
 *   node scripts/build_state_house_statewide_slices_from_overlap.js --scope state_house
 *   node scripts/build_state_house_statewide_slices_from_overlap.js --scope congressional
 *   node scripts/build_state_house_statewide_slices_from_overlap.js --scope state_senate
 *   node scripts/build_state_house_statewide_slices_from_overlap.js --scope state_house --years 2020,2024
 *
 * Inputs:
 * - Data/20201103__mo__general__precinct.csv
 * - Data/20241105__mo__general__precinct.csv
 * - Data/crosswalks/*_overlap.csv (scope-specific)
 *
 * Outputs (examples):
 * - Data/district_contests/state_house_president_2020_overlap.json
 * - Data/district_contests/congressional_president_2020_overlap.json
 * - Data/district_contests/state_senate_president_2024_overlap.json
 *
 * Notes:
 * - Crosswalk precinct keys are `COUNTY - VTDST20` (from Census VTD20).
 * - Election precinct identifiers in the CSV often do NOT directly equal VTD codes, so we
 *   match rows to VTD precincts using a lightweight alias matcher built from
 *   `Data/mo_vtd20_precincts.geojson` (NAME20, VTDST20, etc.), similar to the runtime matcher
 *   used in `index.html`.
 * - Non-geographic / unmatched precincts (e.g. absentees, provisionals) are allocated using
 *   county-weighted fallback derived from matched precincts and/or crosswalk totals.
 * - For years >= 2022, (and always for `--scope congressional`), unmatched precinct labels are
 *   also allocated using same-year district-contest totals when available (e.g. ABSENTEE buckets).
 * - For years < 2022, set `ALLOW_LEGACY_DISTRICT_LABELS=1` to enable this label-based allocation
 *   (district numbers may not correspond exactly across redistricting).
 */
const fs = require('fs');
const path = require('path');
const readline = require('readline');

const ROOT = path.resolve(__dirname, '..');
const DATA_DIR = path.join(ROOT, 'Data');
const DISTRICT_DIR = path.join(DATA_DIR, 'district_contests');
const VTD20_PRECINCTS_GEOJSON = path.join(DATA_DIR, 'mo_vtd20_precincts.geojson');

const YEARS_DEFAULT = [2020, 2024];
const KEY_SEP = '\u001f';

const SCOPE_DEFAULT = 'state_house';
const CROSSWALK_BY_SCOPE = new Map([
  ['congressional', path.join(DATA_DIR, 'crosswalks', 'precinct_to_cd118_overlap.csv')],
  ['state_house', path.join(DATA_DIR, 'crosswalks', 'precinct_to_2022_state_house_overlap.csv')],
  ['state_senate', path.join(DATA_DIR, 'crosswalks', 'precinct_to_2022_state_senate_overlap.csv')]
]);

const DISTRICT_SCOPES = ['congressional', 'state_house', 'state_senate'];

function normalizeCalibrationToken(s) {
  return String(s || '')
    .toLowerCase()
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function districtStatsPathForLegislativeCalibration(scope, year, contestType) {
  const y = Number(year);
  const ct = String(contestType || '').toLowerCase().trim();
  const normalizedScope = String(scope || '').toLowerCase().trim();
  if (!y || !ct) return '';

  // Back-compat: older local naming used by the original presidential calibrator.
  if (normalizedScope === 'state_house' && ct === 'president') {
    const legacy = path.join(DATA_DIR, `district statistics state house ${y} pres.csv`);
    if (fs.existsSync(legacy)) return legacy;
  }

  const calDir = path.join(DATA_DIR, 'calibration csvs');
  if (!fs.existsSync(calDir)) return '';

  const scopeTokensByScope = new Map([
    ['state_house', ['state house']],
    ['state_senate', ['state senate']]
  ]);

  const officeTokensByContestType = new Map([
    ['president', ['pres', 'president']],
    ['governor', ['gov', 'governor']],
    ['lieutenant_governor', ['lt gov', 'lieutenant governor', 'lt governor']],
    ['attorney_general', ['ag', 'attorney general']],
    ['secretary_of_state', ['sos', 'secretary of state']],
    ['treasurer', ['treasurer', 'state treasurer']],
    ['us_senate', ['us senate', 'u.s. senate', 'senate']]
  ]);

  const scopeTokens = scopeTokensByScope.get(normalizedScope) || [];
  if (!scopeTokens.length) return '';
  const officeTokens = officeTokensByContestType.get(ct) || [ct];
  const wantYear = String(y);
  const wantParts = [
    normalizeCalibrationToken(`district-statistics ${wantYear}`),
    ...scopeTokens.map(normalizeCalibrationToken)
  ];

  const entries = fs.readdirSync(calDir).filter(name => /\.csv$/i.test(name));
  for (const name of entries) {
    const norm = normalizeCalibrationToken(name);
    if (!wantParts.every(p => norm.includes(p))) continue;
    const matchesOffice = officeTokens.some(tok => norm.includes(normalizeCalibrationToken(tok)));
    if (!matchesOffice) continue;
    return path.join(calDir, name);
  }

  return '';
}

function parseArgs(argv) {
  const out = { scope: SCOPE_DEFAULT, years: YEARS_DEFAULT, crosswalkPath: '', vtdGeojsonPath: '' };
  const args = Array.from(argv || []);
  for (let i = 0; i < args.length; i += 1) {
    const a = String(args[i] || '');
    if (a === '--scope') {
      out.scope = String(args[i + 1] || '').trim().toLowerCase();
      i += 1;
      continue;
    }
    if (a === '--crosswalk') {
      out.crosswalkPath = String(args[i + 1] || '').trim();
      i += 1;
      continue;
    }
    if (a === '--years') {
      const raw = String(args[i + 1] || '').trim();
      const years = raw
        .split(/[,\s]+/g)
        .map(v => Number(v))
        .filter(v => Number.isFinite(v) && v > 0);
      if (years.length) out.years = years;
      i += 1;
      continue;
    }
    if (a === '--vtd') {
      out.vtdGeojsonPath = String(args[i + 1] || '').trim();
      i += 1;
      continue;
    }
  }
  if (!out.crosswalkPath) out.crosswalkPath = CROSSWALK_BY_SCOPE.get(out.scope) || '';
  if (!out.crosswalkPath) {
    throw new Error(`Unknown scope "${out.scope}". Expected one of: ${Array.from(CROSSWALK_BY_SCOPE.keys()).join(', ')}`);
  }
  if (!out.vtdGeojsonPath) out.vtdGeojsonPath = VTD20_PRECINCTS_GEOJSON;
  return out;
}

const STATEWIDE_OFFICE_MAP = new Map([
  ['PRESIDENT', 'president'],
  ['US PRESIDENT', 'president'],
  ['U.S. PRESIDENT', 'president'],
  ['US SENATE', 'us_senate'],
  ['U.S. SENATE', 'us_senate'],
  ['GOVERNOR', 'governor'],
  ['LIEUTENANT GOVERNOR', 'lieutenant_governor'],
  ['ATTORNEY GENERAL', 'attorney_general'],
  ['SECRETARY OF STATE', 'secretary_of_state'],
  ['STATE TREASURER', 'treasurer'],
  ['TREASURER', 'treasurer'],
  ['STATE AUDITOR', 'auditor'],
  ['AUDITOR', 'auditor'],
  ['LABOR COMMISSIONER', 'labor_commissioner'],
  ['INSURANCE COMMISSIONER', 'insurance_commissioner'],
  ['AGRICULTURE COMMISSIONER', 'agriculture_commissioner'],
  ['SUPERINTENDENT', 'superintendent'],
  ['SUPERINTENDENT OF PUBLIC INSTRUCTION', 'superintendent']
]);

const STATEWIDE_CONTEST_TYPES = new Set(Array.from(STATEWIDE_OFFICE_MAP.values()));

function makeKey(...parts) {
  return parts.join(KEY_SEP);
}

function splitKey(key) {
  return String(key || '').split(KEY_SEP);
}

function roundNumber(value, digits = 6) {
  const factor = 10 ** digits;
  return Math.round((Number(value) || 0) * factor) / factor;
}

function normalizeOffice(rawOffice) {
  return String(rawOffice || '')
    .trim()
    .toUpperCase()
    .replace(/U\.S\./g, 'US')
    .replace(/\s+/g, ' ');
}

function normalizeCounty(rawCounty) {
  let county = String(rawCounty || '')
    .replace(/\u00a0/g, ' ')
    .trim()
    .toUpperCase()
    .replace(/[.\u2019']/g, '')
    .replace(/\s+/g, ' ');
  if (!county) return '';

  // Normalize common Missouri election-authority labels to Census county names.
  // Kansas City Board of Election Commissioners is not a county name; map it to Jackson.
  if (county === 'KANSAS CITY') return 'JACKSON';

  county = county.replace(/\s+COUNTY$/i, '');
  if (county === 'DE KALB') county = 'DEKALB';

  return county;
}

function normalizePrecinctCodeToken(value) {
  return String(value || '').trim().toUpperCase().replace(/\s+/g, ' ');
}

function compactPrecinctAliasToken(value) {
  const t = String(value || '').trim().toUpperCase().replace(/[^A-Z0-9]/g, '');
  return t || '';
}

const PRECINCT_ALIAS_COMMON_WORDS = ['PRECINCT', 'PCT', 'WARD', 'DISTRICT', 'TOWNSHIP', 'BOX', 'VOTING', 'LOCATION'];

function normalizePrecinctAliasToken(value) {
  // Match the runtime logic in index.html as closely as possible.
  let t = String(value || '').trim().toUpperCase();
  if (!t) return '';
  for (const word of PRECINCT_ALIAS_COMMON_WORDS) {
    t = t.replace(new RegExp(word, 'g'), ' ');
  }
  t = t.replace(/[-_.]/g, ' ');
  t = t.replace(/\s+/g, ' ').trim();
  return t;
}

function extractPrecinctAliasCandidates(rawPrecinctValue) {
  // Ported from index.html.
  const aliases = new Set();
  const p = String(rawPrecinctValue || '').trim().toUpperCase();
  if (!p) return aliases;
  const pn = normalizePrecinctAliasToken(p);

  aliases.add(p);
  const pCompact = compactPrecinctAliasToken(p);
  if (pCompact) aliases.add(pCompact);
  if (pn) {
    aliases.add(pn);
    const pnCompact = compactPrecinctAliasToken(pn);
    if (pnCompact) aliases.add(pnCompact);
  }

  const noHash = p.replace(/#\s*\d+\b/g, ' ').replace(/\s+/g, ' ').trim();
  if (noHash && noHash !== p) {
    aliases.add(noHash);
    const noHashCompact = compactPrecinctAliasToken(noHash);
    if (noHashCompact) aliases.add(noHashCompact);
    const noHashNorm = normalizePrecinctAliasToken(noHash);
    if (noHashNorm) {
      aliases.add(noHashNorm);
      const noHashNormCompact = compactPrecinctAliasToken(noHashNorm);
      if (noHashNormCompact) aliases.add(noHashNormCompact);
    }
  }

  if (p.includes('/')) {
    p.split('/').forEach(part => {
      const partTrim = String(part || '').trim().toUpperCase();
      if (!partTrim) return;
      aliases.add(partTrim);
      const partCompact = compactPrecinctAliasToken(partTrim);
      if (partCompact) aliases.add(partCompact);
    });
  }

  if (p.includes('_')) {
    const [left, ...restParts] = p.split('_');
    const right = restParts.join('_').trim();
    if (left && left.trim()) {
      aliases.add(left.trim().toUpperCase());
      const leftCompact = compactPrecinctAliasToken(left);
      if (leftCompact) aliases.add(leftCompact);
    }
    if (right) {
      aliases.add(right.toUpperCase());
      const rightCompact = compactPrecinctAliasToken(right);
      if (rightCompact) aliases.add(rightCompact);
    }
  }

  const parts = (pn || '').split(' ').filter(Boolean);
  if (parts.length) {
    const first = parts[0];
    if (/[0-9]/.test(first)) {
      aliases.add(first);
      const firstCompact = compactPrecinctAliasToken(first);
      if (firstCompact) aliases.add(firstCompact);
      const rest = parts.slice(1).join(' ').trim().toUpperCase();
      if (rest) {
        aliases.add(rest);
        const restCompact = compactPrecinctAliasToken(rest);
        if (restCompact) aliases.add(restCompact);
      }
    }
  }

  const dotVariant = p.replace(/-/g, '.');
  if (dotVariant.includes('.')) {
    const [aRaw, bRaw] = dotVariant.split('.', 2);
    if (/^\d+$/.test(aRaw || '') && /^\d+$/.test(bRaw || '')) {
      const a = Number(aRaw);
      const b = Number(bRaw);
      const z2 = (n) => String(n).padStart(2, '0');
      aliases.add(`${a}.${b}`);
      aliases.add(`${z2(a)}.${b}`);
      aliases.add(`${z2(a)}${b}`);
      aliases.add(`${z2(a)}${z2(b)}`);
    }
  }

  if (/^\d+$/.test(p)) {
    aliases.add(String(Number(p)));
    aliases.add(p.padStart(4, '0'));
  }

  return aliases;
}

const DIRECTIONAL_PRECINCT_PREFIXES = {
  NORTH: 'N',
  SOUTH: 'S',
  EAST: 'E',
  WEST: 'W',
  NORTHEAST: 'NE',
  NORTHWEST: 'NW',
  SOUTHEAST: 'SE',
  SOUTHWEST: 'SW'
};

const PRECINCT_NUMBER_WORDS = {
  FIRST: '1',
  SECOND: '2',
  THIRD: '3',
  FOURTH: '4',
  FIFTH: '5'
};

function normalizePrecinctNumberWord(token) {
  const t = String(token || '').trim().toUpperCase();
  if (!t) return '';
  if (/^\d+[A-Z]{0,2}$/.test(t)) return t;
  return PRECINCT_NUMBER_WORDS[t] || '';
}

function addPrecinctAliasVariants(rawValue, addAliasFn) {
  // Ported from index.html.
  if (typeof addAliasFn !== 'function') return;
  const raw = String(rawValue || '').trim().toUpperCase();
  if (!raw) return;

  const addSafe = (value) => {
    const token = String(value || '').trim().toUpperCase();
    if (token) addAliasFn(token);
  };

  const normalized = normalizePrecinctAliasToken(raw);
  const compact = compactPrecinctAliasToken(raw);
  addSafe(raw);
  addSafe(normalized);
  addSafe(compact);
  extractPrecinctAliasCandidates(raw).forEach(addSafe);

  const stripped = raw
    .replace(/\bVOTING DISTRICT\b/g, ' ')
    .replace(/\bDISTRICT\b/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  const strippedNorm = normalizePrecinctAliasToken(stripped);
  const strippedCompact = compactPrecinctAliasToken(stripped);
  addSafe(stripped);
  addSafe(strippedNorm);
  addSafe(strippedCompact);
  extractPrecinctAliasCandidates(stripped).forEach(addSafe);

  const words = (strippedNorm || '').split(' ').filter(Boolean);
  if (words.length >= 2) {
    const acronym = words
      .filter(word => /[A-Z]/.test(word) && !/^\d+$/.test(word))
      .map(word => word[0])
      .join('');
    if (acronym.length >= 2) addSafe(acronym);
  }

  if (words.length >= 2) {
    const tail = normalizePrecinctNumberWord(words[words.length - 1]);
    const dir = DIRECTIONAL_PRECINCT_PREFIXES[words[0]] || '';
    if (dir && tail) {
      addSafe(`${dir}${tail}`);
      addSafe(`${dir} ${tail}`);
    }
  }

  const trailingCode = (strippedNorm || '').match(/(?:^|\s)(\d+[A-Z]{0,2})$/);
  if (trailingCode) addSafe(trailingCode[1]);
}

function collectVtdCodeCandidates(rawCode) {
  const out = new Set();
  const raw = normalizePrecinctCodeToken(rawCode);
  if (!raw) return out;
  const compact = compactPrecinctAliasToken(raw);
  if (compact) out.add(compact);
  out.add(raw);

  // Pure numeric -> pad to VTDST20 6 digits.
  const digits = compact.replace(/[^0-9]/g, '');
  const hasLetter = /[A-Z]/.test(compact);
  if (digits && !hasLetter) {
    const n = String(Number(digits));
    if (n && n !== 'NaN') {
      out.add(String(Number(digits))); // "7"
      out.add(String(Number(digits)).padStart(2, '0'));
      out.add(String(Number(digits)).padStart(3, '0'));
      out.add(String(Number(digits)).padStart(4, '0'));
      out.add(String(Number(digits)).padStart(5, '0'));
      out.add(String(Number(digits)).padStart(6, '0')); // "000007"
    }
  }

  // Numeric + suffix letter(s), e.g. "1A" -> "00001A"
  const m = compact.match(/^0*([0-9]{1,4})([A-Z]{1,2})$/);
  if (m) {
    const n = String(Number(m[1]));
    const suf = m[2];
    if (n && n !== 'NaN') out.add(`${n.padStart(5, '0')}${suf}`); // 5 digits + suffix = 6 chars
    if (n && n !== 'NaN') out.add(`${n.padStart(4, '0')}${suf}`); // sometimes VTDs may be shorter in other datasets
  }

  return out;
}

function expandCompositePrecinctCodes(rawValue) {
  // Ported from index.html: expands bundles like "1-ABC" -> ["1A","1B","1C"],
  // "16-AB/27-A" -> ["16A","16B","27A"], etc.
  const out = new Set();
  const raw = String(rawValue || '').trim().toUpperCase();
  if (!raw) return out;

  const addExpanded = (value) => {
    const token = String(value || '').trim().toUpperCase();
    if (token) out.add(token);
  };

  const sourceParts = raw.split('/').map(part => part.trim()).filter(Boolean);
  if (!sourceParts.length) sourceParts.push(raw);

  for (const part of sourceParts) {
    const compact = part.replace(/\s+/g, '');

    const alphaBundle = compact.match(/^(\d{1,3})-([A-Z]{1,4})$/);
    if (alphaBundle) {
      alphaBundle[2].split('').forEach(ch => addExpanded(`${String(Number(alphaBundle[1]))}${ch}`));
      continue;
    }

    const alphaInline = compact.match(/^(\d{1,3})([A-Z]{2,4})$/);
    if (alphaInline) {
      alphaInline[2].split('').forEach(ch => addExpanded(`${String(Number(alphaInline[1]))}${ch}`));
      continue;
    }

    const simpleCode = compact.match(/^(\d{1,3})-([A-Z])$/);
    if (simpleCode) {
      addExpanded(`${String(Number(simpleCode[1]))}${simpleCode[2]}`);
      continue;
    }

    const numericOnly = compact.match(/^(\d{1,3})$/);
    if (numericOnly) {
      addExpanded(String(Number(numericOnly[1])));
    }
  }

  return out;
}

function isSubsequence(needle, haystack) {
  const n = String(needle || '');
  const h = String(haystack || '');
  if (!n || !h) return false;
  let i = 0;
  for (let j = 0; j < h.length && i < n.length; j += 1) {
    if (h[j] === n[i]) i += 1;
  }
  return i === n.length;
}

function buildRawPrecinctBucketKey(row) {
  const county = normalizeCounty(row.county || row.county_name || '');
  if (!county) return '';
  const rawCode = normalizePrecinctCodeToken(row.precinct_code || row.precinct_id || row.ward || '');
  const rawPrecinct = normalizePrecinctCodeToken(row.precinct || row.precinct_name || '');
  return makeKey(county, rawCode, rawPrecinct);
}

function parseVotes(rawVotes) {
  if (rawVotes === undefined || rawVotes === null) return 0;
  const text = String(rawVotes).trim().replace(/,/g, '');
  if (!text) return 0;
  const num = Number(text);
  return Number.isFinite(num) ? Math.trunc(num) : 0;
}

function normalizeParty(rawParty) {
  return String(rawParty || '').trim().toUpperCase();
}

function partyBucket(rawParty) {
  const party = normalizeParty(rawParty);
  if (party.startsWith('DEM')) return 'dem';
  if (party.startsWith('REP')) return 'rep';
  return 'other';
}

function normalizeCandidateKey(name) {
  return String(name || '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]/g, '');
}

// OpenElections sometimes omits party entirely (notably 2018 State Auditor).
const KNOWN_CANDIDATE_PARTIES = new Map([
  ['nicolegalloway', 'dem'],
  ['saundramcdowell', 'rep'],
  ['seanotoole', 'other'],
  ['jacobluetkemeyer', 'other'],
  ['donfitz', 'other'],
  ['arniecdienoff', 'other'],
  ['arniecacdienoff', 'other']
]);

function inferPartyBucket(rawParty, candidate, blankPartyInference = null, inferenceKey = '') {
  const explicit = normalizeParty(rawParty);
  if (explicit) return partyBucket(explicit);

  const known = KNOWN_CANDIDATE_PARTIES.get(normalizeCandidateKey(candidate));
  if (known) return known;

  if (blankPartyInference && inferenceKey) {
    const inferred = blankPartyInference.get(makeKey(inferenceKey, String(candidate || '').trim()));
    if (inferred) return inferred;
  }
  return 'other';
}

function candidateName(row) {
  const direct = String(row.candidate || '').trim();
  if (direct) return direct;
  const first = String(row['first name'] || row.first_name || row.firstName || '').trim();
  const last = String(row['last name'] || row.last_name || row.lastName || '').trim();
  if (first || last) return `${first} ${last}`.trim();
  return '';
}

function mapContestType(rawOffice) {
  const office = normalizeOffice(rawOffice);
  if (STATEWIDE_OFFICE_MAP.has(office)) return STATEWIDE_OFFICE_MAP.get(office);
  if (office.startsWith('PRESIDENT')) return 'president';
  if (office.startsWith('US SENATE')) return 'us_senate';
  if (office.startsWith('GOVERNOR')) return 'governor';
  if (office.startsWith('LIEUTENANT GOVERNOR')) return 'lieutenant_governor';
  if (office.startsWith('ATTORNEY GENERAL')) return 'attorney_general';
  if (office.startsWith('SECRETARY OF STATE')) return 'secretary_of_state';
  if (office.startsWith('STATE TREASURER')) return 'treasurer';
  if (office.startsWith('STATE AUDITOR')) return 'auditor';
  return null;
}

function normalizeDistrictNumber(rawDistrict) {
  const s = String(rawDistrict || '').trim();
  if (!s) return '';
  const digits = s.replace(/[^0-9]/g, '');
  if (digits) {
    const n = Number(digits);
    if (Number.isFinite(n)) return String(n);
  }
  return s.toUpperCase();
}

function mapDistrictContestScope(rawOffice) {
  const office = normalizeOffice(rawOffice);
  if (office.startsWith('US HOUSE') || office.startsWith('US REPRESENTATIVE')) return 'congressional';
  if (office.startsWith('STATE HOUSE') || office.startsWith('STATE REPRESENTATIVE')) return 'state_house';
  if (office.startsWith('STATE SENATE') || office.startsWith('STATE SENATOR')) return 'state_senate';
  return '';
}

function parseCsvLine(line) {
  const out = [];
  let current = '';
  let inQuotes = false;
  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i];
    if (ch === '"') {
      if (inQuotes && line[i + 1] === '"') {
        current += '"';
        i += 1;
      } else {
        inQuotes = !inQuotes;
      }
      continue;
    }
    if (ch === ',' && !inQuotes) {
      out.push(current);
      current = '';
      continue;
    }
    current += ch;
  }
  out.push(current);
  return out;
}

function normalizeWeights(mapLike) {
  const totals = new Map();
  let sum = 0;
  if (!mapLike) return new Map();
  for (const [k, v] of mapLike.entries()) {
    const amount = Number(v) || 0;
    if (!k || !(amount > 0)) continue;
    totals.set(String(k), amount);
    sum += amount;
  }
  if (!(sum > 0)) return new Map();
  const out = new Map();
  for (const [k, v] of totals.entries()) out.set(k, v / sum);
  return out;
}

function blendNormalizedWeights(preferred, fallback, alpha) {
  const a = Math.max(0, Math.min(1, Number(alpha) || 0));
  const p = preferred && preferred.size ? preferred : null;
  const f = fallback && fallback.size ? fallback : null;
  if (p && !f) return p;
  if (!p && f) return f;
  if (!p && !f) return new Map();

  const combined = new Map();
  const keys = new Set();
  for (const k of p.keys()) keys.add(k);
  for (const k of f.keys()) keys.add(k);
  for (const k of keys) {
    const pv = Number(p.get(k) || 0);
    const fv = Number(f.get(k) || 0);
    const v = a * pv + (1 - a) * fv;
    if (v > 0) combined.set(k, v);
  }
  return normalizeWeights(combined);
}

function isNonGeographicPrecinctLabel(rawCode, rawPrecinct) {
  const a = String(rawCode || '').toUpperCase();
  const b = String(rawPrecinct || '').toUpperCase();
  const s = `${a} ${b}`.replace(/\s+/g, ' ').trim();
  if (!s) return true;
  if (/(^|\b)(ABSENTEE|PROVISIONAL|CURBSIDE|CENTRAL|EARLY)(\b|$)/i.test(s)) return true;
  if (/(^|\b)(VOTE\s*CENTER|VOTECENTER)(\b|$)/i.test(s)) return true;
  if (/(^|\b)(WRITE-?INS?)(\b|$)/i.test(s)) return true;
  if (/(^|\b)(CUMULATIVE|FEDERAL|TRANS)(\b|$)/i.test(s)) return true;
  return false;
}

function normalizeLabelKey(year, county, rawCode, rawPrecinct) {
  return makeKey(
    String(year),
    String(county || '').trim().toUpperCase(),
    normalizePrecinctCodeToken(rawCode),
    normalizePrecinctCodeToken(rawPrecinct)
  );
}

function districtLabelWeightsFor(scope, year, county, rawCode, rawPrecinct, districtTurnoutByLabelKeyByScope, houseToSenate) {
  const labelKey = normalizeLabelKey(year, county, rawCode, rawPrecinct);
  const scopeMap = districtTurnoutByLabelKeyByScope.get(scope) || null;
  const directTotals = scopeMap ? (scopeMap.get(labelKey) || null) : null;
  if (directTotals && directTotals.size) {
    return normalizeWeights(directTotals);
  }

  if (scope === 'state_senate' && houseToSenate && houseToSenate.size) {
    const houseMap = districtTurnoutByLabelKeyByScope.get('state_house') || null;
    const houseTotals = houseMap ? (houseMap.get(labelKey) || null) : null;
    if (houseTotals && houseTotals.size) {
      const senateTotals = new Map();
      for (const [houseDistrict, votes] of houseTotals.entries()) {
        const senateDistrict = houseToSenate.get(String(houseDistrict));
        if (!senateDistrict) continue;
        senateTotals.set(senateDistrict, (senateTotals.get(senateDistrict) || 0) + (Number(votes) || 0));
      }
      const normalized = normalizeWeights(senateTotals);
      if (normalized.size) return normalized;
    }
  }

  return new Map();
}

class VoteAgg {
  constructor() {
    this.demVotes = 0;
    this.repVotes = 0;
    this.otherVotes = 0;
    this.demCandidates = new Map();
    this.repCandidates = new Map();
  }

  get totalVotes() {
    return this.demVotes + this.repVotes + this.otherVotes;
  }

  addByParty(bucket, candidate, votes) {
    const amount = Number(votes) || 0;
    if (amount === 0) return;
    if (bucket === 'dem') {
      this.demVotes += amount;
      if (candidate) this.demCandidates.set(candidate, (this.demCandidates.get(candidate) || 0) + amount);
      return;
    }
    if (bucket === 'rep') {
      this.repVotes += amount;
      if (candidate) this.repCandidates.set(candidate, (this.repCandidates.get(candidate) || 0) + amount);
      return;
    }
    this.otherVotes += amount;
  }

  addScaledVotes(demVotes, repVotes, otherVotes, demCandidate, repCandidate) {
    const dem = Number(demVotes) || 0;
    const rep = Number(repVotes) || 0;
    const other = Number(otherVotes) || 0;
    if (dem) {
      this.demVotes += dem;
      if (demCandidate) this.demCandidates.set(demCandidate, (this.demCandidates.get(demCandidate) || 0) + dem);
    }
    if (rep) {
      this.repVotes += rep;
      if (repCandidate) this.repCandidates.set(repCandidate, (this.repCandidates.get(repCandidate) || 0) + rep);
    }
    if (other) this.otherVotes += other;
  }

  topCandidate(map) {
    let bestName = '';
    let bestVotes = -1;
    for (const [name, votes] of map.entries()) {
      if (votes > bestVotes) {
        bestVotes = votes;
        bestName = name;
      }
    }
    return bestName;
  }

  toResult(defaultDemCandidate = '', defaultRepCandidate = '') {
    const demVotes = Math.round(this.demVotes);
    const repVotes = Math.round(this.repVotes);
    const otherVotes = Math.round(this.otherVotes);
    const totalVotes = demVotes + repVotes + otherVotes;
    const margin = repVotes - demVotes;
    const marginPct = totalVotes ? (margin / totalVotes) * 100 : 0;
    const winner = margin > 0 ? 'REP' : (margin < 0 ? 'DEM' : 'TIE');
    return {
      dem_votes: demVotes,
      rep_votes: repVotes,
      other_votes: otherVotes,
      total_votes: totalVotes,
      dem_candidate: this.topCandidate(this.demCandidates) || defaultDemCandidate,
      rep_candidate: this.topCandidate(this.repCandidates) || defaultRepCandidate,
      margin,
      margin_pct: roundNumber(marginPct),
      winner,
      color: ''
    };
  }
}

function ensureVoteAgg(map, key) {
  if (!map.has(key)) map.set(key, new VoteAgg());
  return map.get(key);
}

function ensureNestedMap(map, key) {
  if (!map.has(key)) map.set(key, new Map());
  return map.get(key);
}

function readCrosswalkByPrecinct(csvPath) {
  const text = fs.readFileSync(csvPath, 'utf8');
  const lines = text.split(/\r?\n/).filter(Boolean);
  if (lines.length < 2) throw new Error(`Crosswalk CSV is empty: ${csvPath}`);
  const header = parseCsvLine(lines[0]).map(h => String(h || '').trim());
  const idxPrec = header.indexOf('precinct_key');
  const idxDist = header.indexOf('district_num');
  const idxW = header.indexOf('area_weight');
  if (idxPrec < 0 || idxDist < 0 || idxW < 0) throw new Error(`Crosswalk missing required columns: ${csvPath}`);

  const byPrecinct = new Map();
  for (let i = 1; i < lines.length; i += 1) {
    const parts = parseCsvLine(lines[i]);
    const precinctKey = String(parts[idxPrec] || '').trim().toUpperCase();
    const district = String(parts[idxDist] || '').trim();
    const w = Number(parts[idxW]);
    if (!precinctKey || !district || !Number.isFinite(w) || !(w > 0)) continue;
    if (!byPrecinct.has(precinctKey)) byPrecinct.set(precinctKey, new Map());
    const node = byPrecinct.get(precinctKey);
    node.set(district, (node.get(district) || 0) + w);
  }

  // Normalize weights per precinct.
  const out = new Map();
  for (const [precinctKey, wmap] of byPrecinct.entries()) {
    const normalized = normalizeWeights(wmap);
    if (normalized.size) out.set(precinctKey, normalized);
  }
  return out;
}

function buildDistrictMapFromPrecinctCrosswalks(fromByPrecinct, toByPrecinct) {
  // Produces a best-effort mapping from "fromDistrict" -> "toDistrict" by
  // composing precinct overlaps (no geometry deps).
  const matrix = new Map(); // fromDistrict -> Map(toDistrict -> score)
  for (const [precinctKey, fromWeights] of (fromByPrecinct || new Map()).entries()) {
    const toWeights = toByPrecinct.get(precinctKey);
    if (!toWeights || !toWeights.size) continue;
    for (const [fromDistrict, fromW] of fromWeights.entries()) {
      const fw = Number(fromW) || 0;
      if (!(fw > 0)) continue;
      if (!matrix.has(fromDistrict)) matrix.set(fromDistrict, new Map());
      const row = matrix.get(fromDistrict);
      for (const [toDistrict, toW] of toWeights.entries()) {
        const tw = Number(toW) || 0;
        if (!(tw > 0)) continue;
        row.set(toDistrict, (row.get(toDistrict) || 0) + (fw * tw));
      }
    }
  }

  const out = new Map();
  for (const [fromDistrict, row] of matrix.entries()) {
    let bestTo = '';
    let bestScore = -1;
    for (const [toDistrict, score] of row.entries()) {
      if (score > bestScore) {
        bestScore = score;
        bestTo = toDistrict;
      }
    }
    if (bestTo) out.set(String(fromDistrict), String(bestTo));
  }
  return out;
}

function readDistrictStatsDemRepOthShares(csvPath) {
  const text = fs.readFileSync(csvPath, 'utf8');
  const lines = text.split(/\r?\n/).filter(Boolean);
  if (lines.length < 2) return new Map();
  const header = parseCsvLine(lines[0]).map(h => String(h || '').trim());
  const idxId = header.indexOf('ID');
  const idxDem = header.indexOf('Dem');
  const idxRep = header.indexOf('Rep');
  const idxOth = header.indexOf('Oth');
  if (idxId < 0 || idxDem < 0 || idxRep < 0 || idxOth < 0) return new Map();

  const out = new Map();
  for (let i = 1; i < lines.length; i += 1) {
    const parts = parseCsvLine(lines[i]);
    const idRaw = String(parts[idxId] || '').replace(/^"|"$/g, '').trim();
    if (!/^\d+$/.test(idRaw)) continue;
    const id = String(Number(idRaw));
    const dem = Number(parts[idxDem]);
    const rep = Number(parts[idxRep]);
    const oth = Number(parts[idxOth]);
    if (![dem, rep, oth].every(Number.isFinite)) continue;
    const sum = dem + rep + oth;
    if (!(sum > 0)) continue;
    // Normalize defensively (csv values are typically rounded).
    out.set(id, { dem: dem / sum, rep: rep / sum, oth: oth / sum });
  }
  return out;
}

function apportionVotesByShares(totalVotes, shares) {
  const tot = Math.max(0, Math.round(Number(totalVotes) || 0));
  if (!(tot > 0) || !shares) return { dem: 0, rep: 0, oth: 0 };

  const sDem = Math.max(0, Number(shares.dem) || 0);
  const sRep = Math.max(0, Number(shares.rep) || 0);
  const sOth = Math.max(0, Number(shares.oth) || 0);
  const sSum = sDem + sRep + sOth;
  if (!(sSum > 0)) return { dem: 0, rep: 0, oth: 0 };

  const demF = tot * (sDem / sSum);
  const repF = tot * (sRep / sSum);
  const othF = tot * (sOth / sSum);

  const floors = [
    { k: 'dem', v: Math.floor(demF), frac: demF - Math.floor(demF) },
    { k: 'rep', v: Math.floor(repF), frac: repF - Math.floor(repF) },
    { k: 'oth', v: Math.floor(othF), frac: othF - Math.floor(othF) }
  ];

  let used = floors[0].v + floors[1].v + floors[2].v;
  let remainder = tot - used;
  floors.sort((a, b) => (b.frac - a.frac) || a.k.localeCompare(b.k, 'en'));
  for (let i = 0; i < floors.length && remainder > 0; i += 1) {
    floors[i].v += 1;
    remainder -= 1;
  }

  const out = { dem: 0, rep: 0, oth: 0 };
  floors.forEach(x => { out[x.k] = x.v; });
  // Fix any off-by-one due to weird inputs.
  const sum = out.dem + out.rep + out.oth;
  if (sum !== tot) out.oth += (tot - sum);
  return out;
}

function calibratePayloadToDistrictStats(payload, year, csvPath) {
  if (!payload?.general?.results || !csvPath || !fs.existsSync(csvPath)) return payload;
  const sharesByDistrict = readDistrictStatsDemRepOthShares(csvPath);
  if (!sharesByDistrict.size) return payload;

  const results = payload.general.results;
  let calibratedDistricts = 0;
  Object.entries(results).forEach(([district, row]) => {
    const target = sharesByDistrict.get(String(district)) || null;
    if (!target) return;
    const totalVotes = Math.max(0, Math.round(Number(row?.total_votes) || 0));
    if (!(totalVotes > 0)) return;
    const v = apportionVotesByShares(totalVotes, target);
    row.dem_votes = v.dem;
    row.rep_votes = v.rep;
    row.other_votes = v.oth;
    row.total_votes = v.dem + v.rep + v.oth;
    row.margin = row.rep_votes - row.dem_votes;
    row.margin_pct = row.total_votes ? roundNumber((row.margin / row.total_votes) * 100) : 0;
    row.winner = row.margin > 0 ? 'REP' : (row.margin < 0 ? 'DEM' : 'TIE');
    calibratedDistricts += 1;
  });

  payload.meta = payload.meta || {};
  payload.meta.source_method = 'calibrated_district_stats_csv';
  payload.meta.calibrated_to_district_stats_csv = true;
  payload.meta.district_stats_csv = path.relative(ROOT, csvPath).replace(/\\/g, '/');
  payload.meta.calibrated_districts = calibratedDistricts;
  // When calibrated, treat coverage as complete for display purposes.
  payload.meta.match_coverage_pct = 100;
  payload.meta.calibrated_year = Number(year) || null;

  return payload;
}

function buildCountyWeightFallbacks(crosswalkByPrecinct) {
  const byCounty = new Map();
  const statewide = new Map();
  for (const [precinctKey, weights] of crosswalkByPrecinct.entries()) {
    const county = normalizeCounty((precinctKey.split(' - ', 2)[0] || '').toString());
    if (!county) continue;
    if (!byCounty.has(county)) byCounty.set(county, new Map());
    const countyNode = byCounty.get(county);
    for (const [district, w] of weights.entries()) {
      const amount = Number(w) || 0;
      if (!(amount > 0)) continue;
      countyNode.set(district, (countyNode.get(district) || 0) + amount);
      statewide.set(district, (statewide.get(district) || 0) + amount);
    }
  }
  return { byCounty, statewide };
}

function buildPrecinctMatcherIndex(vtdGeojsonPath) {
  const payload = JSON.parse(fs.readFileSync(vtdGeojsonPath, 'utf8'));
  const idx = new Map();

  for (const feature of (payload.features || [])) {
    const props = feature && feature.properties ? feature.properties : {};
    const countyNorm = normalizeCounty(props.county_nam || props.COUNTYNAME || props.County || props.NAME || '');
    const precId = String(props.VTDST20 || props.prec_id || props.PREC_ID || '').trim().toUpperCase();
    const precinctNorm = String(props.precinct_norm || (countyNorm && precId ? `${countyNorm} - ${precId}` : '')).trim().toUpperCase();
    if (!countyNorm || !precinctNorm) continue;

    if (!idx.has(countyNorm)) {
      idx.set(countyNorm, {
        aliasToNorms: new Map(),
        features: []
      });
    }
    const countyNode = idx.get(countyNorm);

    const aliases = new Set();
    const nameAliases = new Set();
    const addAlias = (value) => {
      const token = String(value || '').trim().toUpperCase();
      if (token) aliases.add(token);
    };
    const addNameAlias = (value) => {
      const token = normalizePrecinctAliasToken(value);
      if (token) nameAliases.add(token);
    };

    [precId, props.NAME20, props.NAMELSAD20, props.precinct_name, precinctNorm].forEach(value => {
      addPrecinctAliasVariants(value, addAlias);
      addNameAlias(value);
    });
    if (precId) addAlias(compactPrecinctAliasToken(precId));

    const addLookupValue = (map, key, value) => {
      if (!map || !key || !value) return;
      if (!map.has(key)) map.set(key, new Set());
      map.get(key).add(value);
    };

    for (const alias of aliases) addLookupValue(countyNode.aliasToNorms, alias, precinctNorm);
    countyNode.features.push({ precinctNorm, nameAliases });
  }

  return idx;
}

function matchPrecinctNormsForRawRow(rawCode, rawPrecinct, countyNorm, countyInfo) {
  if (!countyNorm || !countyInfo) return [];
  const code = String(rawCode || '').trim();
  const precinct = String(rawPrecinct || '').trim();
  const combined = [code, precinct].filter(Boolean).join(' ');

  const candidateTokens = [];
  const tokenSeen = new Set();
  const addToken = (value) => {
    const token = String(value || '').trim().toUpperCase();
    if (!token || tokenSeen.has(token)) return;
    tokenSeen.add(token);
    candidateTokens.push(token);
  };
  const addRawVariants = (value) => {
    if (!value) return;
    addPrecinctAliasVariants(value, addToken);
    for (const v of collectVtdCodeCandidates(value)) addToken(v);
    for (const codeToken of expandCompositePrecinctCodes(value)) {
      addToken(codeToken);
      addPrecinctAliasVariants(codeToken, addToken);
    }

    // Directional swap heuristic: "N REPUBLIC A" -> "REPUBLIC NORTH" (and variants).
    const rawNorm = normalizePrecinctAliasToken(value);
    const m = rawNorm.match(/^(N|S|E|W|NE|NW|SE|SW)\s+(.+)$/);
    if (m) {
      const dir = m[1];
      const rest = String(m[2] || '').trim();
      const dirWord = ({
        N: 'NORTH',
        S: 'SOUTH',
        E: 'EAST',
        W: 'WEST',
        NE: 'NORTHEAST',
        NW: 'NORTHWEST',
        SE: 'SOUTHEAST',
        SW: 'SOUTHWEST'
      })[dir] || '';
      if (dirWord && rest) {
        addToken(`${rest} ${dirWord}`);
        const restNoSuffix = rest.replace(/\s+[A-Z]$/i, '').trim();
        if (restNoSuffix && restNoSuffix !== rest) addToken(`${restNoSuffix} ${dirWord}`);
      }
    }
  };

  [precinct, code, combined].forEach(addRawVariants);

  // Prefer the most specific token match rather than unioning everything (which can explode).
  let bestExact = null; // Set<string>
  let bestExactSize = Infinity;
  for (const token of candidateTokens) {
    const hits = countyInfo.aliasToNorms.get(token);
    if (!hits || hits.size === 0) continue;
    if (hits.size === 1) return Array.from(hits);
    if (hits.size < bestExactSize) {
      bestExact = hits;
      bestExactSize = hits.size;
    }
  }
  if (bestExact && bestExactSize <= 12) return Array.from(bestExact);

  const fuzzyCandidates = Array.from(candidateTokens)
    .map(token => ({
      raw: token,
      norm: normalizePrecinctAliasToken(token),
      compact: compactPrecinctAliasToken(token)
    }))
    .filter(t => (t.norm && t.norm.length >= 4) || (t.compact && t.compact.length >= 4));
  if (fuzzyCandidates.length) {
    const scored = [];
    for (const feature of countyInfo.features) {
      const aliases = feature.nameAliases || new Set();
      let score = 0;
      for (const alias of aliases) {
        const aliasNorm = String(alias || '').trim().toUpperCase();
        if (!aliasNorm || aliasNorm.length < 4) continue;
        const aliasCompact = compactPrecinctAliasToken(aliasNorm);
        for (const token of fuzzyCandidates) {
          const tokenNorm = token.norm || '';
          const tokenCompact = token.compact || '';

          if (tokenNorm && (tokenNorm.includes(aliasNorm) || aliasNorm.includes(tokenNorm))) {
            score = Math.max(score, Math.min(30, Math.max(aliasNorm.length, tokenNorm.length)));
            continue;
          }
          if (tokenCompact && aliasCompact && (tokenCompact.includes(aliasCompact) || aliasCompact.includes(tokenCompact))) {
            score = Math.max(score, Math.min(26, Math.max(aliasCompact.length, tokenCompact.length)));
            continue;
          }
          // Abbreviation heuristic: "BRKLN" is a subsequence of "BROOKLINE".
          if (tokenCompact.length >= 4 && aliasCompact.length >= 6 && isSubsequence(tokenCompact, aliasCompact)) {
            score = Math.max(score, Math.min(18, tokenCompact.length));
          }
        }
      }
      if (score > 0) scored.push({ precinctNorm: feature.precinctNorm, score });
    }
    if (scored.length) {
      scored.sort((a, b) => (b.score - a.score) || String(a.precinctNorm).localeCompare(String(b.precinctNorm), 'en'));
      return scored.slice(0, 6).map(s => s.precinctNorm);
    }
  }
  return [];
}

async function buildBlankPartyInferenceForCsv(csvPath) {
  // For races where every row lacks an explicit party, assign the first two
  // distinct candidates in file order as REP then DEM (matches OpenElections MO
  // listing order and the Python contest builder heuristic). Known-name overrides
  // in inferPartyBucket still win when present (e.g. Nicole Galloway -> DEM).
  const rl = readline.createInterface({
    input: fs.createReadStream(csvPath, { encoding: 'utf8' }),
    crlfDelay: Infinity
  });

  let header = null;
  const raceMeta = new Map(); // contestType -> { hasExplicitParty, firstSeen: string[] }

  for await (const line of rl) {
    if (!line) continue;
    if (!header) {
      header = parseCsvLine(line).map(h => String(h || '').trim());
      continue;
    }
    const parts = parseCsvLine(line);
    const row = {};
    for (let i = 0; i < header.length; i += 1) row[header[i]] = parts[i];

    const contestType = mapContestType(row.office);
    if (!contestType || !STATEWIDE_CONTEST_TYPES.has(contestType)) continue;
    const candidate = candidateName(row);
    if (!candidate) continue;

    if (!raceMeta.has(contestType)) {
      raceMeta.set(contestType, { hasExplicitParty: false, firstSeen: [] });
    }
    const meta = raceMeta.get(contestType);
    if (normalizeParty(row.party || row.party_simplified || row.party_detailed || '')) {
      meta.hasExplicitParty = true;
    }
    if (!meta.firstSeen.includes(candidate)) meta.firstSeen.push(candidate);
  }

  const inferred = new Map(); // contestType|candidate -> dem|rep
  for (const [contestType, meta] of raceMeta.entries()) {
    if (meta.hasExplicitParty) continue;
    if (meta.firstSeen.length < 2) continue;
    inferred.set(makeKey(contestType, meta.firstSeen[0]), 'rep');
    inferred.set(makeKey(contestType, meta.firstSeen[1]), 'dem');
  }
  return inferred;
}

async function readPrecinctCsvYear(year, scope, precinctAggByContestYear, candidateTotalsByContestYear, districtTurnoutByLabelKeyByScope) {
  const prefix = String(Number(year));
  const csvCandidates = fs.readdirSync(DATA_DIR)
    .filter(name => new RegExp(`^${prefix}\\d{4}__mo__general__precinct\\.csv$`, 'i').test(name))
    .sort()
    .map(name => path.join(DATA_DIR, name));

  const csvPath = csvCandidates.length ? csvCandidates[0] : '';
  if (!csvPath) throw new Error(`Missing precinct CSV for ${year}`);

  const blankPartyInference = await buildBlankPartyInferenceForCsv(csvPath);

  const rl = readline.createInterface({
    input: fs.createReadStream(csvPath, { encoding: 'utf8' }),
    crlfDelay: Infinity
  });

  let header = null;
  let rowCount = 0;
  for await (const line of rl) {
    if (!line) continue;
    if (!header) {
      header = parseCsvLine(line).map(h => String(h || '').trim());
      continue;
    }
    const parts = parseCsvLine(line);
    const row = {};
    for (let i = 0; i < header.length; i += 1) row[header[i]] = parts[i];

    const precinctKey = buildRawPrecinctBucketKey(row);
    if (!precinctKey) continue;

    const votes = parseVotes(row.votes);
    if (!votes) continue;

    const contestType = mapContestType(row.office);
    const candidate = candidateName(row);
    const bucket = inferPartyBucket(
      row.party || row.party_simplified || row.party_detailed || '',
      candidate,
      blankPartyInference,
      contestType || ''
    );
    const contestYearKey = contestType ? makeKey(contestType, String(year)) : makeKey('unknown', String(year));

    if (contestType && STATEWIDE_CONTEST_TYPES.has(contestType)) {
      ensureVoteAgg(precinctAggByContestYear, makeKey(contestType, String(year), precinctKey))
        .addByParty(bucket, candidate, votes);

      if (bucket === 'dem' || bucket === 'rep') {
        const k = makeKey(contestType, String(year), bucket, candidate || '(unknown)');
        candidateTotalsByContestYear.set(k, (candidateTotalsByContestYear.get(k) || 0) + votes);
      }
    }

    // Capture same-year district contest totals by (county, precinct_code, precinct) label.
    // This is especially useful for non-geographic buckets like ABSENTEE / early vote that
    // are already reported by district in the precinct CSV.
    const districtScope = mapDistrictContestScope(row.office);
    if (districtScope && DISTRICT_SCOPES.includes(districtScope)) {
      const districtNum = normalizeDistrictNumber(row.district);
      if (districtNum) {
        const [countyNorm, rawCode, rawPrecinct] = splitKey(precinctKey);
        const labelKey = makeKey(String(year), countyNorm, rawCode, rawPrecinct);
        if (!districtTurnoutByLabelKeyByScope.has(districtScope)) {
          districtTurnoutByLabelKeyByScope.set(districtScope, new Map());
        }
        const scopeMap = districtTurnoutByLabelKeyByScope.get(districtScope);
        const districtTotals = ensureNestedMap(scopeMap, labelKey);
        districtTotals.set(districtNum, (districtTotals.get(districtNum) || 0) + votes);
      }
    }

    rowCount += 1;
    if (rowCount % 250000 === 0) console.log(`Parsed ${rowCount} rows from ${path.basename(csvPath)}... (${contestYearKey})`);
  }
}

function topCandidateFor(contestType, year, bucket, candidateTotalsByContestYear) {
  let bestName = '';
  let bestVotes = -1;
  const prefix = makeKey(contestType, String(year), bucket);
  for (const [k, votes] of candidateTotalsByContestYear.entries()) {
    if (!String(k).startsWith(prefix + KEY_SEP)) continue;
    const [, , , name] = splitKey(k);
    if ((Number(votes) || 0) > bestVotes) {
      bestVotes = Number(votes) || 0;
      bestName = name === '(unknown)' ? '' : name;
    }
  }
  return bestName;
}

function writeJson(filePath, payload) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, `${JSON.stringify(payload, null, 2)}\n`, 'utf8');
}

function buildPayload(
  scope,
  contestType,
  year,
  districtAgg,
  totalVotes,
  allocatedVotes,
  directMatchedVotes,
  demCandidate,
  repCandidate,
  extraMeta = null
) {
  const results = {};
  const districts = Array.from(districtAgg.keys()).sort((a, b) => Number(a) - Number(b));
  for (const d of districts) results[String(d)] = districtAgg.get(d).toResult(demCandidate, repCandidate);

  const matchCoveragePct = totalVotes > 0 ? (allocatedVotes / totalVotes) * 100 : 0;
  const directCoveragePct = totalVotes > 0 ? (directMatchedVotes / totalVotes) * 100 : 0;
  return {
    meta: {
      scope,
      contest_type: contestType,
      year: Number(year),
      district_count: Object.keys(results).length,
      match_coverage_pct: roundNumber(matchCoveragePct),
      direct_match_coverage_pct: roundNumber(directCoveragePct),
      generated_at: new Date().toISOString(),
      source_method: 'precinct_overlap_crosswalk',
      ...(extraMeta && typeof extraMeta === 'object' ? extraMeta : {})
    },
    general: { results }
  };
}

async function main() {
  const { scope, years, crosswalkPath, vtdGeojsonPath } = parseArgs(process.argv.slice(2));
  const crosswalkByPrecinct = readCrosswalkByPrecinct(crosswalkPath);
  const matcherIdx = buildPrecinctMatcherIndex(vtdGeojsonPath);
  const countyArea = buildCountyWeightFallbacks(crosswalkByPrecinct);
  const statewideFallbackWeights = normalizeWeights(countyArea.statewide);

  const precinctAggByContestYear = new Map();
  const candidateTotalsByContestYear = new Map();
  const districtTurnoutByLabelKeyByScope = new Map(); // scope -> Map(year|county|rawCode|rawPrecinct -> Map(district -> totalVotes))
  const reportEnabled = String(process.env.REPORT_MATCH || '').trim() === '1';
  const reportContest = String(process.env.REPORT_CONTEST || '').trim().toLowerCase();
  const reportYear = Number(process.env.REPORT_YEAR || 0) || null;
  const reportCounty = String(process.env.REPORT_COUNTY || '').trim().toUpperCase();
  const allowLegacyDistrictLabels = String(process.env.ALLOW_LEGACY_DISTRICT_LABELS || '').trim() === '1';

  // For state senate fallback, infer mapping house_district -> senate_district from overlap crosswalks.
  let houseToSenate = null;
  try {
    const housePath = CROSSWALK_BY_SCOPE.get('state_house') || '';
    const senatePath = CROSSWALK_BY_SCOPE.get('state_senate') || '';
    if (housePath && senatePath && fs.existsSync(housePath) && fs.existsSync(senatePath)) {
      const houseByPrecinct = readCrosswalkByPrecinct(housePath);
      const senateByPrecinct = readCrosswalkByPrecinct(senatePath);
      houseToSenate = buildDistrictMapFromPrecinctCrosswalks(houseByPrecinct, senateByPrecinct);
    }
  } catch (_) {
    houseToSenate = null;
  }

  for (const year of years) {
    await readPrecinctCsvYear(year, scope, precinctAggByContestYear, candidateTotalsByContestYear, districtTurnoutByLabelKeyByScope);
  }

  // Group precinct aggs by contest/year.
  const precinctsByContestYear = new Map();
  for (const [k, agg] of precinctAggByContestYear.entries()) {
    const [contestType, year] = splitKey(k);
    const key = makeKey(contestType, year);
    if (!precinctsByContestYear.has(key)) precinctsByContestYear.set(key, []);
    precinctsByContestYear.get(key).push({ key: k, agg });
  }

  let writtenFiles = 0;
  for (const [contestYearKey, precinctRows] of precinctsByContestYear.entries()) {
    const [contestType, year] = splitKey(contestYearKey);
    const demCandidate = topCandidateFor(contestType, year, 'dem', candidateTotalsByContestYear);
    const repCandidate = topCandidateFor(contestType, year, 'rep', candidateTotalsByContestYear);

    const districtAgg = new Map();
    const countyMatchedDistrictWeights = new Map();
    const unmatched = [];
    const countyFallbackCache = new Map(); // county -> { blended, preferred, area, alpha }
    const countyTotalsAll = new Map();
    const countyDirectAll = new Map();
    const reportByCounty = reportEnabled && (!reportContest || reportContest === String(contestType))
      && (!reportYear || Number(reportYear) === Number(year));
    const countyTotalsByCounty = reportByCounty ? new Map() : null;
    const countyDirectByCounty = reportByCounty ? new Map() : null;
    const topUnmatchedByCounty = reportByCounty ? new Map() : null;
    const resolvedReportLabelKeys = reportByCounty ? new Set() : null;

    let totalVotes = 0;
    let allocatedVotes = 0;
    let directMatchedVotes = 0;
    let labelMatchedVotes = 0;

    for (const row of precinctRows) {
      const [, , countyNorm, rawCode, rawPrecinct] = splitKey(row.key);
      const agg = row.agg;
      const precinctTotal = agg.totalVotes;
      if (!(precinctTotal > 0)) continue;
      totalVotes += precinctTotal;

      const county = String(countyNorm || '').trim().toUpperCase();
      countyTotalsAll.set(county, (countyTotalsAll.get(county) || 0) + precinctTotal);
      if (reportByCounty) {
        countyTotalsByCounty.set(county, (countyTotalsByCounty.get(county) || 0) + precinctTotal);
      }
      const countyInfo = matcherIdx.get(county) || null;
      const matchedPrecinctNorms = countyInfo
        ? matchPrecinctNormsForRawRow(rawCode, rawPrecinct, county, countyInfo)
        : [];

      // Build district weights by distributing to matched VTDs, then applying overlap crosswalk.
      const districtWeights = new Map();
      if (matchedPrecinctNorms.length) {
        const baseShare = 1 / matchedPrecinctNorms.length;
        for (const precinctNorm of matchedPrecinctNorms) {
          const weights = crosswalkByPrecinct.get(String(precinctNorm || '').trim().toUpperCase()) || null;
          if (!weights || !weights.size) continue;
          for (const [district, share] of weights.entries()) {
            const amount = baseShare * (Number(share) || 0);
            if (!(amount > 0)) continue;
            districtWeights.set(district, (districtWeights.get(district) || 0) + amount);
          }
        }
      }
      const normalizedDirect = normalizeWeights(districtWeights);
      if (normalizedDirect.size) {
        allocatedVotes += precinctTotal;
        directMatchedVotes += precinctTotal;
        countyDirectAll.set(county, (countyDirectAll.get(county) || 0) + precinctTotal);
        if (reportByCounty) {
          countyDirectByCounty.set(county, (countyDirectByCounty.get(county) || 0) + precinctTotal);
        }
        if (!countyMatchedDistrictWeights.has(county)) countyMatchedDistrictWeights.set(county, new Map());
        const countyTotals = countyMatchedDistrictWeights.get(county);
        for (const [district, share] of normalizedDirect.entries()) {
          if (!districtAgg.has(district)) districtAgg.set(district, new VoteAgg());
          districtAgg.get(district).addScaledVotes(
            agg.demVotes * share,
            agg.repVotes * share,
            agg.otherVotes * share,
            demCandidate,
            repCandidate
          );
          countyTotals.set(district, (countyTotals.get(district) || 0) + (precinctTotal * share));
        }
        continue;
      }
      unmatched.push({ county, rawCode, rawPrecinct, agg });
      if (reportByCounty) {
        if (!topUnmatchedByCounty.has(county)) topUnmatchedByCounty.set(county, []);
        const list = topUnmatchedByCounty.get(county);
        list.push({
          county,
          label_key: normalizeLabelKey(year, county, rawCode, rawPrecinct),
          raw_code: String(rawCode || '').trim(),
          raw_precinct: String(rawPrecinct || '').trim(),
          votes: precinctTotal
        });
      }
    }

    for (const { county, rawCode, rawPrecinct, agg } of unmatched) {
      const nonGeo = isNonGeographicPrecinctLabel(rawCode, rawPrecinct);

      const allowLabelAllocation = Number(year) >= 2022 || allowLegacyDistrictLabels;
      if (allowLabelAllocation) {
        const labelKey = normalizeLabelKey(year, county, rawCode, rawPrecinct);
        const labelWeights = districtLabelWeightsFor(scope, year, county, rawCode, rawPrecinct, districtTurnoutByLabelKeyByScope, houseToSenate);
        if (labelWeights.size) {
          if (reportByCounty && resolvedReportLabelKeys) resolvedReportLabelKeys.add(labelKey);
          const precinctTotal = agg.totalVotes;
          allocatedVotes += precinctTotal;
          directMatchedVotes += precinctTotal;
          labelMatchedVotes += precinctTotal;
          countyDirectAll.set(county, (countyDirectAll.get(county) || 0) + precinctTotal);
          if (reportByCounty) {
            countyDirectByCounty.set(county, (countyDirectByCounty.get(county) || 0) + precinctTotal);
          }

          if (!countyMatchedDistrictWeights.has(county)) countyMatchedDistrictWeights.set(county, new Map());
          const countyTotals = countyMatchedDistrictWeights.get(county);

          for (const [district, share] of labelWeights.entries()) {
            if (!districtAgg.has(district)) districtAgg.set(district, new VoteAgg());
            districtAgg.get(district).addScaledVotes(
              agg.demVotes * share,
              agg.repVotes * share,
              agg.otherVotes * share,
              demCandidate,
              repCandidate
            );
            countyTotals.set(district, (countyTotals.get(district) || 0) + (precinctTotal * share));
          }
          continue;
        }
      }

      if (!countyFallbackCache.has(county)) {
        const preferred = normalizeWeights(countyMatchedDistrictWeights.get(county)); // vote-weighted from matched VTDs
        const areaFallback = normalizeWeights(countyArea.byCounty.get(county)); // area-weighted from full VTD set
        const tot = Number(countyTotalsAll.get(county) || 0);
        const direct = Number(countyDirectAll.get(county) || 0);
        const alpha = tot > 0 ? (direct / tot) : 0;
        const blended = blendNormalizedWeights(preferred, areaFallback, alpha);
        countyFallbackCache.set(county, {
          blended: blended.size ? blended : new Map(),
          preferred,
          area: areaFallback,
          alpha
        });
      }
      const node = countyFallbackCache.get(county);
      const preferPreferredForNonGeo = nonGeo && node && node.preferred && node.preferred.size && (node.alpha >= 0.5);
      const fallbackWeights =
        (preferPreferredForNonGeo ? node.preferred : (node && node.blended && node.blended.size ? node.blended : null))
        || (node && node.area && node.area.size ? node.area : null)
        || statewideFallbackWeights;
      if (!fallbackWeights.size) continue;
      allocatedVotes += agg.totalVotes;
      for (const [district, share] of fallbackWeights.entries()) {
        if (!districtAgg.has(district)) districtAgg.set(district, new VoteAgg());
        districtAgg.get(district).addScaledVotes(
          agg.demVotes * share,
          agg.repVotes * share,
          agg.otherVotes * share,
          demCandidate,
          repCandidate
        );
      }
    }

    let payload = buildPayload(
      scope,
      contestType,
      year,
      districtAgg,
      totalVotes,
      allocatedVotes,
      directMatchedVotes,
      demCandidate,
      repCandidate,
      {
        label_match_votes: Math.round(labelMatchedVotes),
        legacy_district_labels_enabled: Boolean(allowLegacyDistrictLabels && Number(year) < 2022)
      }
    );

    // Optional calibration: for state legislative statewide contests, overwrite district D/R/O shares
    // using external per-district stats (keeps each district's total_votes).
    if (scope === 'state_house' || scope === 'state_senate') {
      const statsPath = districtStatsPathForLegislativeCalibration(scope, year, contestType);
      if (statsPath && fs.existsSync(statsPath)) {
        payload = calibratePayloadToDistrictStats(payload, year, statsPath);
      }
    }
    const outName = `${scope}_${contestType}_${year}_overlap.json`;
    writeJson(path.join(DISTRICT_DIR, outName), payload);
    writtenFiles += 1;
    writtenFiles && console.log(`Wrote ${outName} (coverage ${payload.meta.match_coverage_pct}%, direct ${payload.meta.direct_match_coverage_pct}%)`);

    if (reportByCounty) {
      const rows = [];
      for (const [county, tot] of countyTotalsByCounty.entries()) {
        const direct = countyDirectByCounty.get(county) || 0;
        rows.push({ county, total: tot, direct, pct: tot > 0 ? (direct / tot) * 100 : 0 });
      }
      rows.sort((a, b) => b.total - a.total);
      const overallPct = totalVotes > 0 ? (directMatchedVotes / totalVotes) * 100 : 0;
      console.log(`REPORT ${contestType} ${year}: overall direct-match ${roundNumber(overallPct, 3)}%`);

      if (reportCounty) {
        const node = rows.find(r => r.county === reportCounty);
        if (node) {
          console.log(`REPORT ${contestType} ${year}: ${reportCounty} direct-match ${roundNumber(node.pct, 3)}% (direct=${Math.round(node.direct)}, total=${Math.round(node.total)})`);
          const list = (topUnmatchedByCounty.get(reportCounty) || [])
            .filter(item => !(resolvedReportLabelKeys && resolvedReportLabelKeys.has(item.label_key)))
            .sort((a, b) => b.votes - a.votes)
            .slice(0, 30);
          console.log(`Top unmatched precinct labels for ${reportCounty} (${list.length}):`);
          list.forEach(item => {
            console.log(`  ${Math.round(item.votes)}\tcode="${item.raw_code}"\tprecinct="${item.raw_precinct}"`);
          });
        } else {
          console.log(`REPORT: no rows for county ${reportCounty}`);
        }
      } else {
        const topByTotal = rows.slice(0, 12);
        const bottomByPct = rows
          .slice()
          .sort((a, b) => (a.pct - b.pct) || (b.total - a.total))
          .slice(0, 12);
        console.log('Top counties by vote total (direct-match %):');
        topByTotal.forEach(r => console.log(`  ${r.county}\t${roundNumber(r.pct, 2)}%\t(total=${Math.round(r.total)})`));
        console.log('Lowest-coverage counties (direct-match %):');
        bottomByPct.forEach(r => console.log(`  ${r.county}\t${roundNumber(r.pct, 2)}%\t(total=${Math.round(r.total)})`));
      }
    }
  }

  console.log(`Wrote ${writtenFiles} overlap statewide ${scope} slices to ${DISTRICT_DIR}`);
}

main().catch(err => {
  console.error(err && err.stack ? err.stack : err);
  process.exit(1);
});
