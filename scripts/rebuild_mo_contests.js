const fs = require('fs');
const path = require('path');
const readline = require('readline');

const ROOT = path.resolve(__dirname, '..');
const DATA_DIR = path.join(ROOT, 'Data');
const CONTESTS_DIR = path.join(DATA_DIR, 'contests');
const COUNTY_GEOJSON_PATH = path.join(DATA_DIR, 'tl_2020_29_county20.geojson');

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

const STATEWIDE_CONTEST_TYPES = new Set([
  'president',
  'us_senate',
  'governor',
  'lieutenant_governor',
  'attorney_general',
  'secretary_of_state',
  'treasurer',
  'auditor',
  'labor_commissioner',
  'insurance_commissioner',
  'agriculture_commissioner',
  'superintendent'
]);

class VoteAgg {
  constructor() {
    this.demVotes = 0;
    this.repVotes = 0;
    this.otherVotes = 0;
    this.demCandidates = new Map();
    this.repCandidates = new Map();
  }

  add(bucket, candidate, votes) {
    const amount = Number(votes) || 0;
    if (amount < 0) return;
    if (bucket === 'dem') {
      this.demVotes += amount;
      if (candidate) incrementMap(this.demCandidates, candidate, amount);
      return;
    }
    if (bucket === 'rep') {
      this.repVotes += amount;
      if (candidate) incrementMap(this.repCandidates, candidate, amount);
      return;
    }
    this.otherVotes += amount;
  }

  toResult() {
    const demVotes = Math.trunc(this.demVotes);
    const repVotes = Math.trunc(this.repVotes);
    const otherVotes = Math.trunc(this.otherVotes);
    const totalVotes = demVotes + repVotes + otherVotes;
    const margin = repVotes - demVotes;
    const marginPct = totalVotes ? (margin / totalVotes) * 100 : 0;
    const winner = margin > 0 ? 'REP' : (margin < 0 ? 'DEM' : 'TIE');
    return {
      dem_votes: demVotes,
      rep_votes: repVotes,
      other_votes: otherVotes,
      total_votes: totalVotes,
      dem_candidate: topCandidate(this.demCandidates),
      rep_candidate: topCandidate(this.repCandidates),
      margin,
      margin_pct: roundNumber(marginPct),
      winner,
      color: ''
    };
  }
}

function incrementMap(map, key, amount) {
  map.set(key, (map.get(key) || 0) + amount);
}

function topCandidate(map) {
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

function roundNumber(value, digits = 6) {
  const factor = 10 ** digits;
  return Math.round((Number(value) || 0) * factor) / factor;
}

function makeKey(...parts) {
  return parts.join('\u001f');
}

function splitKey(key) {
  return String(key || '').split('\u001f');
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

async function streamCsvRows(filePath, onRow) {
  const input = fs.createReadStream(filePath, { encoding: 'utf8' });
  const rl = readline.createInterface({ input, crlfDelay: Infinity });
  let header = null;

  for await (const rawLine of rl) {
    const line = rawLine.replace(/^\ufeff/, '');
    if (!header) {
      header = parseCsvLine(line);
      continue;
    }
    if (!line.trim()) continue;

    const values = parseCsvLine(line);
    const row = {};
    for (let i = 0; i < header.length; i += 1) {
      row[header[i]] = values[i] === undefined ? '' : values[i];
    }
    await onRow(row);
  }
}

function readJson(filePath, fallback = null) {
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf8'));
  } catch (_) {
    return fallback;
  }
}

function writeJson(filePath, payload) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, `${JSON.stringify(payload, null, 2)}\n`, 'utf8');
}

function normalizeCountyKey(value) {
  let token = String(value || '').toUpperCase();
  token = token.replace(/&/g, ' AND ');
  token = token.replace(/SAINT /g, 'ST ');
  token = token.replace(/ COUNTY/g, '');
  token = token.replace(/ CITY/g, ' CITY');
  token = token.replace(/[^A-Z0-9]/g, '');
  return token;
}

function countyCanonicalFromFeature(props) {
  const name = String(props?.NAME20 || '').trim();
  if (!name) return '';

  const namelsad = String(props?.NAMELSAD20 || '').trim().toUpperCase();
  const classfp = String(props?.CLASSFP20 || '').trim().toUpperCase();
  const nameUpper = name.toUpperCase();

  if (namelsad.endsWith(' CITY') || classfp === 'C7') {
    if (normalizeCountyKey(nameUpper) === normalizeCountyKey('ST LOUIS')) {
      return 'ST. LOUIS CITY';
    }
    return `${nameUpper} CITY`;
  }

  return nameUpper;
}

function buildCountyLookup() {
  const lookup = new Map();
  const payload = readJson(COUNTY_GEOJSON_PATH, { features: [] });

  for (const feature of payload.features || []) {
    const props = feature?.properties || {};
    const canonical = countyCanonicalFromFeature(props);
    if (!canonical) continue;

    const namelsad = String(props.NAMELSAD20 || '').trim().toUpperCase();
    lookup.set(normalizeCountyKey(canonical), canonical);
    if (namelsad) lookup.set(normalizeCountyKey(namelsad), canonical);

    if (canonical === 'ST. LOUIS') {
      for (const alias of ['ST LOUIS', 'ST LOUIS COUNTY', 'SAINT LOUIS', 'SAINT LOUIS COUNTY']) {
        lookup.set(normalizeCountyKey(alias), canonical);
      }
    } else if (canonical === 'ST. LOUIS CITY') {
      for (const alias of [
        'ST LOUIS CITY',
        'ST. LOUIS CITY',
        'SAINT LOUIS CITY',
        'CITY OF ST LOUIS',
        'CITY OF ST. LOUIS',
        'CITY OF SAINT LOUIS'
      ]) {
        lookup.set(normalizeCountyKey(alias), canonical);
      }
    } else {
      lookup.set(normalizeCountyKey(`${canonical} COUNTY`), canonical);
    }
  }

  lookup.set(normalizeCountyKey('KANSAS CITY'), 'JACKSON');
  lookup.set(normalizeCountyKey('KANSAS CITY COUNTY'), 'JACKSON');
  return lookup;
}

function canonicalCountyName(rawCounty, countyLookup) {
  const raw = String(rawCounty || '').trim();
  if (!raw) return '';
  const key = normalizeCountyKey(raw);
  return countyLookup.get(key) || raw.toUpperCase();
}

function parseVotes(rawVotes) {
  if (rawVotes === undefined || rawVotes === null) return 0;
  const text = String(rawVotes).trim().replace(/,/g, '');
  if (!text) return 0;
  const value = Number(text);
  return Number.isFinite(value) ? Math.trunc(value) : 0;
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

function candidateName(row) {
  const candidate = String(row.candidate || '').trim();
  if (candidate) return candidate;

  const first = String(row['first name'] || row.first_name || '').trim();
  const last = String(row['last name'] || row.last_name || '').trim();
  if (first || last) return `${first} ${last}`.trim();
  return '';
}

function normalizeOffice(rawOffice) {
  return String(rawOffice || '')
    .trim()
    .toUpperCase()
    .replace(/U\.S\./g, 'US')
    .replace(/\s+/g, ' ');
}

function extractDistrict(rawOffice, rawDistrict) {
  const districtText = String(rawDistrict || '').trim();
  if (districtText && districtText.toUpperCase() !== 'STATEWIDE') {
    const match = districtText.match(/(\d+)/);
    if (match) return String(Number(match[1]));
    return districtText;
  }

  const office = normalizeOffice(rawOffice);
  const officeMatch = office.match(/\bDISTRICT\s*([0-9]+)\b/);
  if (officeMatch) return String(Number(officeMatch[1]));
  return '';
}

function mapContestType(rawOffice, rawDistrict) {
  const office = normalizeOffice(rawOffice);
  const district = extractDistrict(rawOffice, rawDistrict);

  if (office.startsWith('US HOUSE') || office.startsWith('US REPRESENTATIVE')) {
    return 'us_house';
  }
  if (office.startsWith('STATE HOUSE') || office.startsWith('STATE REPRESENTATIVE')) {
    return 'state_house';
  }
  if (office.startsWith('STATE SENATE') || office.startsWith('STATE SENATOR')) {
    return 'state_senate';
  }
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

function iterMissouriGeneralCsvs() {
  return fs.readdirSync(DATA_DIR)
    .filter(name => /__mo__general__precinct\.csv$/i.test(name))
    .map(name => ({ year: Number(name.slice(0, 4)), filePath: path.join(DATA_DIR, name) }))
    .filter(entry => Number.isFinite(entry.year))
    .sort((a, b) => a.year - b.year || a.filePath.localeCompare(b.filePath));
}

async function aggregateContestData() {
  const countyLookup = buildCountyLookup();
  const contestAgg = new Map();

  for (const { year, filePath } of iterMissouriGeneralCsvs()) {
    await streamCsvRows(filePath, row => {
      const contestType = mapContestType(row.office || '', row.district || '');
      if (!contestType) return;

      const countyRaw = row.county || row.county_name || '';
      const county = canonicalCountyName(countyRaw, countyLookup);
      if (!county) return;

      const votes = parseVotes(row.votes);
      if (votes < 0) return;

      const bucket = partyBucket(row.party || row.party_simplified || row.party_detailed || '');
      const candidate = candidateName(row);
      const key = makeKey(contestType, year, county);
      let agg = contestAgg.get(key);
      if (!agg) {
        agg = new VoteAgg();
        contestAgg.set(key, agg);
      }
      agg.add(bucket, candidate, votes);
    });
  }

  return contestAgg;
}

function buildContestSlices(contestAgg) {
  const grouped = new Map();

  for (const [key, agg] of contestAgg.entries()) {
    const [contestType, year, county] = splitKey(key);
    const groupKey = makeKey(contestType, year);
    if (!grouped.has(groupKey)) grouped.set(groupKey, []);
    grouped.get(groupKey).push([county, agg]);
  }

  const generatedAt = new Date().toISOString();
  const manifestEntries = [];

  for (const [groupKey, rows] of Array.from(grouped.entries()).sort((a, b) => a[0].localeCompare(b[0], 'en'))) {
    const [contestType, yearText] = splitKey(groupKey);
    const year = Number(yearText);
    const outRows = [];
    let demTotal = 0;
    let repTotal = 0;

    rows.sort((a, b) => a[0].localeCompare(b[0], 'en'));
    for (const [county, agg] of rows) {
      const result = agg.toResult();
      demTotal += Number(result.dem_votes || 0);
      repTotal += Number(result.rep_votes || 0);
      outRows.push({
        county,
        ...result
      });
    }

    const fileName = `${contestType}_${year}.json`;
    writeJson(path.join(CONTESTS_DIR, fileName), {
      meta: {
        contest_type: contestType,
        year,
        rows: outRows.length,
        generated_at: generatedAt
      },
      rows: outRows
    });

    manifestEntries.push({
      contest_type: contestType,
      year,
      file: fileName,
      rows: outRows.length,
      dem_total: demTotal,
      rep_total: repTotal,
      major_party_contested: Boolean(demTotal > 0 && repTotal > 0)
    });
  }

  manifestEntries.sort((a, b) =>
    String(a.contest_type).localeCompare(String(b.contest_type), 'en') || Number(a.year) - Number(b.year)
  );

  writeJson(path.join(CONTESTS_DIR, 'manifest.json'), { files: manifestEntries });
  writeJson(path.join(CONTESTS_DIR, 'manifest_statewide_contested.json'), {
    files: manifestEntries.filter(entry =>
      STATEWIDE_CONTEST_TYPES.has(entry.contest_type) && Boolean(entry.major_party_contested)
    )
  });
}

async function main() {
  const contestAgg = await aggregateContestData();
  buildContestSlices(contestAgg);
  console.log(`Built ${contestAgg.size} county contest aggregates into ${CONTESTS_DIR}`);
}

main().catch(err => {
  console.error(err);
  process.exitCode = 1;
});
