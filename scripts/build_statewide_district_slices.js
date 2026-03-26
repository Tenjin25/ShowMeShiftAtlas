const fs = require('fs');
const path = require('path');
const readline = require('readline');

const ROOT = path.resolve(__dirname, '..');
const DATA_DIR = path.join(ROOT, 'Data');
const DISTRICT_DIR = path.join(DATA_DIR, 'district_contests');
const CONTESTS_DIR = path.join(DATA_DIR, 'contests');
const DISTRICT_MANIFEST_PATH = path.join(DISTRICT_DIR, 'manifest.json');
const STATEWIDE_CONTEST_MANIFEST_PATH = path.join(CONTESTS_DIR, 'manifest_statewide_contested.json');
const KEY_SEP = '\u001f';

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

const DISTRICT_SCOPES = ['congressional', 'state_house', 'state_senate'];

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

  addScaledVotes(demVotes, repVotes, otherVotes, demCandidate, repCandidate) {
    const dem = Number(demVotes) || 0;
    const rep = Number(repVotes) || 0;
    const other = Number(otherVotes) || 0;
    if (dem) {
      this.demVotes += dem;
      if (demCandidate) incrementMap(this.demCandidates, demCandidate, dem);
    }
    if (rep) {
      this.repVotes += rep;
      if (repCandidate) incrementMap(this.repCandidates, repCandidate, rep);
    }
    if (other) {
      this.otherVotes += other;
    }
  }

  topDemCandidate() {
    return topCandidate(this.demCandidates);
  }

  topRepCandidate() {
    return topCandidate(this.repCandidates);
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
      dem_candidate: this.topDemCandidate() || defaultDemCandidate,
      rep_candidate: this.topRepCandidate() || defaultRepCandidate,
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
  return parts.join(KEY_SEP);
}

function splitKey(key) {
  return String(key || '').split(KEY_SEP);
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
    const districtMatch = districtText.match(/(\d+)/);
    if (districtMatch) return String(Number(districtMatch[1]));
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
    return { contestType: 'us_house', scope: 'congressional', district };
  }
  if (office.startsWith('STATE HOUSE') || office.startsWith('STATE REPRESENTATIVE')) {
    return { contestType: 'state_house', scope: 'state_house', district };
  }
  if (office.startsWith('STATE SENATE') || office.startsWith('STATE SENATOR')) {
    return { contestType: 'state_senate', scope: 'state_senate', district };
  }

  if (STATEWIDE_OFFICE_MAP.has(office)) {
    return { contestType: STATEWIDE_OFFICE_MAP.get(office), scope: null, district: '' };
  }
  if (office.startsWith('PRESIDENT')) return { contestType: 'president', scope: null, district: '' };
  if (office.startsWith('US SENATE')) return { contestType: 'us_senate', scope: null, district: '' };
  if (office.startsWith('GOVERNOR')) return { contestType: 'governor', scope: null, district: '' };
  if (office.startsWith('LIEUTENANT GOVERNOR')) return { contestType: 'lieutenant_governor', scope: null, district: '' };
  if (office.startsWith('ATTORNEY GENERAL')) return { contestType: 'attorney_general', scope: null, district: '' };
  if (office.startsWith('SECRETARY OF STATE')) return { contestType: 'secretary_of_state', scope: null, district: '' };
  if (office.startsWith('STATE TREASURER')) return { contestType: 'treasurer', scope: null, district: '' };
  if (office.startsWith('STATE AUDITOR')) return { contestType: 'auditor', scope: null, district: '' };

  return { contestType: null, scope: null, district: '' };
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
  const direct = String(row.candidate || '').trim();
  if (direct) return direct;

  const first = String(row['first name'] || row.first_name || row.firstName || '').trim();
  const last = String(row['last name'] || row.last_name || row.lastName || '').trim();
  if (first || last) return `${first} ${last}`.trim();
  return '';
}

function parseVotes(rawVotes) {
  if (rawVotes === undefined || rawVotes === null) return 0;
  const text = String(rawVotes).trim().replace(/,/g, '');
  if (!text) return 0;
  const num = Number(text);
  return Number.isFinite(num) ? Math.trunc(num) : 0;
}

function normalizePrecinctPart(rawValue) {
  return String(rawValue || '')
    .replace(/\u00a0/g, ' ')
    .replace(/&/g, ' AND ')
    .trim()
    .toUpperCase()
    .replace(/\s+/g, ' ');
}

function buildPrecinctKey(row) {
  const county = normalizePrecinctPart(row.county || row.county_name || '');
  if (!county) return '';

  const parts = [county];
  const ward = normalizePrecinctPart(row.ward || '');
  const precinctCode = normalizePrecinctPart(row.precinct_code || row.precinct_id || '');
  const precinct = normalizePrecinctPart(row.precinct || row.precinct_name || '');

  for (const part of [ward, precinctCode, precinct]) {
    if (part && !parts.includes(part)) parts.push(part);
  }

  return parts.join(' | ');
}

function compareDistrictIds(a, b) {
  const aNum = Number(a);
  const bNum = Number(b);
  const aFinite = Number.isFinite(aNum);
  const bFinite = Number.isFinite(bNum);
  if (aFinite && bFinite) return aNum - bNum;
  return String(a).localeCompare(String(b), 'en');
}

function compareManifestEntries(a, b) {
  const scopeCompare = String(a.scope || '').localeCompare(String(b.scope || ''), 'en');
  if (scopeCompare) return scopeCompare;
  const contestCompare = String(a.contest_type || '').localeCompare(String(b.contest_type || ''), 'en');
  if (contestCompare) return contestCompare;
  return Number(a.year || 0) - Number(b.year || 0);
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

function getOrCreateVoteAgg(map, key) {
  if (!map.has(key)) map.set(key, new VoteAgg());
  return map.get(key);
}

function getOrCreateNestedMap(map, key) {
  if (!map.has(key)) map.set(key, new Map());
  return map.get(key);
}

function loadContestedStatewideSet() {
  const payload = readJson(STATEWIDE_CONTEST_MANIFEST_PATH, { files: [] });
  const out = new Set();
  for (const entry of payload.files || []) {
    if (!entry || !entry.contest_type) continue;
    out.add(makeKey(String(entry.contest_type), String(Number(entry.year))));
  }
  return out;
}

function buildPayload(scope, contestType, year, rows, coveragePct, candidateLookup, generatedAt) {
  const results = {};
  const candidates = candidateLookup.get(makeKey(contestType, year)) || { dem: '', rep: '' };

  rows
    .slice()
    .sort((a, b) => compareDistrictIds(a.district, b.district))
    .forEach(({ district, agg }) => {
      results[String(district)] = agg.toResult(candidates.dem, candidates.rep);
    });

  return {
    meta: {
      scope,
      contest_type: contestType,
      year: Number(year),
      district_count: Object.keys(results).length,
      match_coverage_pct: roundNumber(coveragePct),
      generated_at: generatedAt,
      source_method: 'same_year_precinct_reallocation'
    },
    general: {
      results
    }
  };
}

async function main() {
  const contestedStatewideSet = loadContestedStatewideSet();
  const statewidePrecinctAgg = new Map();
  const statewideContestAgg = new Map();
  const districtPrecinctVotes = new Map();

  const csvPaths = fs.readdirSync(DATA_DIR)
    .filter(name => /^\d{8}__mo__general__precinct\.csv$/i.test(name))
    .sort()
    .map(name => path.join(DATA_DIR, name));

  if (!csvPaths.length) {
    throw new Error(`No Missouri precinct CSVs found in ${DATA_DIR}`);
  }

  for (const csvPath of csvPaths) {
    const fileName = path.basename(csvPath);
    const year = Number(fileName.slice(0, 4));
    if (!Number.isFinite(year)) continue;

    await streamCsvRows(csvPath, async row => {
      const { contestType, scope, district } = mapContestType(row.office || '', row.district || '');
      if (!contestType) return;

      const votes = parseVotes(row.votes);
      if (votes < 0) return;

      const candidate = candidateName(row);
      const bucket = partyBucket(row.party || row.party_simplified || row.party_detailed || '');
      const precinctKey = buildPrecinctKey(row);

      if (scope && district && precinctKey) {
        const scopeKey = makeKey(scope, String(year), precinctKey);
        const districtMap = getOrCreateNestedMap(districtPrecinctVotes, scopeKey);
        districtMap.set(String(district), (districtMap.get(String(district)) || 0) + votes);
      }

      if (!STATEWIDE_CONTEST_TYPES.has(contestType)) return;
      if (!contestedStatewideSet.has(makeKey(contestType, String(year)))) return;
      if (!precinctKey) return;

      getOrCreateVoteAgg(statewidePrecinctAgg, makeKey(contestType, String(year), precinctKey))
        .addByParty(bucket, candidate, votes);
      getOrCreateVoteAgg(statewideContestAgg, makeKey(contestType, String(year)))
        .addByParty(bucket, candidate, votes);
    });
  }

  const candidateLookup = new Map();
  for (const [key, agg] of statewideContestAgg.entries()) {
    candidateLookup.set(key, {
      dem: agg.topDemCandidate(),
      rep: agg.topRepCandidate()
    });
  }

  const districtResultAgg = new Map();
  const coverageStats = new Map();

  for (const [key, agg] of statewidePrecinctAgg.entries()) {
    const [contestType, year, precinctKey] = splitKey(key);

    for (const scope of DISTRICT_SCOPES) {
      const coverageKey = makeKey(scope, contestType, year);
      if (!coverageStats.has(coverageKey)) {
        coverageStats.set(coverageKey, { matchedVotes: 0, totalVotes: 0 });
      }
      const coverage = coverageStats.get(coverageKey);
      coverage.totalVotes += agg.totalVotes;

      const districtMap = districtPrecinctVotes.get(makeKey(scope, year, precinctKey));
      if (!districtMap || !districtMap.size) continue;

      const positiveEntries = Array.from(districtMap.entries()).filter(([, value]) => (Number(value) || 0) > 0);
      if (!positiveEntries.length) continue;

      const totalWeight = positiveEntries.reduce((sum, [, value]) => sum + Number(value || 0), 0);
      if (!(totalWeight > 0)) continue;

      coverage.matchedVotes += agg.totalVotes;
      const candidates = candidateLookup.get(makeKey(contestType, year)) || { dem: '', rep: '' };

      for (const [district, weight] of positiveEntries) {
        const share = Number(weight || 0) / totalWeight;
        const resultAgg = getOrCreateVoteAgg(
          districtResultAgg,
          makeKey(scope, contestType, year, district)
        );
        resultAgg.addScaledVotes(
          agg.demVotes * share,
          agg.repVotes * share,
          agg.otherVotes * share,
          candidates.dem,
          candidates.rep
        );
      }
    }
  }

  const groupedResults = new Map();
  for (const [key, agg] of districtResultAgg.entries()) {
    const [scope, contestType, year, district] = splitKey(key);
    const groupKey = makeKey(scope, contestType, year);
    if (!groupedResults.has(groupKey)) groupedResults.set(groupKey, []);
    groupedResults.get(groupKey).push({ district, agg });
  }

  const generatedAt = new Date().toISOString();
  const newManifestEntries = [];
  let writtenFiles = 0;

  for (const [groupKey, rows] of groupedResults.entries()) {
    const [scope, contestType, year] = splitKey(groupKey);
    const coverage = coverageStats.get(groupKey) || { matchedVotes: 0, totalVotes: 0 };
    const coveragePct = coverage.totalVotes > 0
      ? (coverage.matchedVotes / coverage.totalVotes) * 100
      : 0;

    const payload = buildPayload(scope, contestType, year, rows, coveragePct, candidateLookup, generatedAt);
    const fileName = `${scope}_${contestType}_${year}.json`;
    writeJson(path.join(DISTRICT_DIR, fileName), payload);
    writtenFiles += 1;

    const results = Object.values(payload.general.results || {});
    const demTotal = results.reduce((sum, row) => sum + Number(row.dem_votes || 0), 0);
    const repTotal = results.reduce((sum, row) => sum + Number(row.rep_votes || 0), 0);

    newManifestEntries.push({
      scope,
      contest_type: contestType,
      year: Number(year),
      file: fileName,
      districts: results.length,
      rows: results.length,
      dem_total: demTotal,
      rep_total: repTotal,
      major_party_contested: demTotal > 0 && repTotal > 0,
      match_coverage_pct: roundNumber(coveragePct)
    });
  }

  const existingManifest = readJson(DISTRICT_MANIFEST_PATH, { files: [] });
  const preservedEntries = (existingManifest.files || []).filter(entry => {
    const contestType = String(entry && entry.contest_type || '');
    return !STATEWIDE_CONTEST_TYPES.has(contestType);
  });
  const mergedEntries = preservedEntries.concat(newManifestEntries).sort(compareManifestEntries);
  writeJson(DISTRICT_MANIFEST_PATH, { files: mergedEntries });

  console.log(`Wrote ${writtenFiles} statewide district slices to ${DISTRICT_DIR}`);
  console.log(`District manifest now lists ${mergedEntries.length} files`);
}

main().catch(err => {
  console.error(err && err.stack ? err.stack : err);
  process.exit(1);
});
