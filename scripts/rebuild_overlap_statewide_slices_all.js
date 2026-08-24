#!/usr/bin/env node
/**
 * Rebuild statewide district overlap slices for Missouri using the 2022 district
 * geometries, but matching precinct labels against the appropriate VTD vintage.
 *
 * Outputs: Data/district_contests/*_overlap.json
 *
 * Notes
 * - 2000–2008: VTD00
 * - 2010–2018: VTD10
 * - 2020–2024: VTD20 (includes 2022)
 *
 * Usage:
 *   node scripts/rebuild_overlap_statewide_slices_all.js
 */
const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');

function run(cmd, args, opts = {}) {
  const res = spawnSync(cmd, args, {
    cwd: ROOT,
    stdio: 'inherit',
    env: process.env,
    ...opts
  });
  if (res.error) throw res.error;
  if (res.status !== 0) {
    const pretty = [cmd, ...args].join(' ');
    throw new Error(`Command failed (${res.status}): ${pretty}`);
  }
}

function buildScriptArgs({
  scope,
  years,
  vtdPath,
  crosswalkPath,
  crosswalkOverridePath = '',
  outputDir = '',
  filenameSuffix = '',
  linesYear = null
}) {
  const args = [
    path.join('scripts', 'build_state_house_statewide_slices_from_overlap.js'),
    '--scope',
    scope,
    '--years',
    years.join(','),
    '--vtd',
    vtdPath,
    '--crosswalk',
    crosswalkPath
  ];
  if (crosswalkOverridePath) {
    args.push('--crosswalk-override', crosswalkOverridePath);
  }
  if (outputDir) args.push('--output-dir', outputDir);
  if (filenameSuffix) args.push('--filename-suffix', filenameSuffix);
  if (linesYear) args.push('--lines-year', String(linesYear));
  return args;
}

const VINTAGES = [
  {
    label: 'vtd00',
    years: [2000, 2002, 2004, 2006, 2008],
    vtd: path.join('Data', 'mo_vtd00_precincts.geojson'),
    crosswalks: {
      congressional: path.join('Data', 'crosswalks', 'vtd00_to_cd118_overlap.csv'),
      state_house: path.join('Data', 'crosswalks', 'vtd00_to_2022_state_house_overlap.csv'),
      state_senate: path.join('Data', 'crosswalks', 'vtd00_to_2022_state_senate_overlap.csv')
    },
    crosswalkOverrides: {
      congressional: path.join('Data', 'crosswalks', 'vtd00_to_cd118_from_nhgis.csv'),
      state_house: path.join('Data', 'crosswalks', 'vtd00_to_2022_state_house_from_nhgis.csv'),
      state_senate: path.join('Data', 'crosswalks', 'vtd00_to_2022_state_senate_from_nhgis.csv')
    }
  },
  {
    label: 'vtd10',
    years: [2010, 2012, 2014, 2016, 2018],
    vtd: path.join('Data', 'mo_vtd10_precincts.geojson'),
    crosswalks: {
      congressional: path.join('Data', 'crosswalks', 'vtd10_to_cd118_overlap.csv'),
      state_house: path.join('Data', 'crosswalks', 'vtd10_to_2022_state_house_overlap.csv'),
      state_senate: path.join('Data', 'crosswalks', 'vtd10_to_2022_state_senate_overlap.csv')
    },
    crosswalkOverrides: {
      congressional: path.join('Data', 'crosswalks', 'vtd10_to_cd118_from_nhgis.csv'),
      state_house: path.join('Data', 'crosswalks', 'vtd10_to_2022_state_house_from_nhgis.csv'),
      state_senate: path.join('Data', 'crosswalks', 'vtd10_to_2022_state_senate_from_nhgis.csv')
    }
  },
  {
    label: 'vtd20',
    years: [2020, 2022, 2024],
    vtd: path.join('Data', 'mo_vtd20_precincts.geojson'),
    crosswalks: {
      congressional: path.join('Data', 'crosswalks', 'precinct_to_cd118_overlap.csv'),
      state_house: path.join('Data', 'crosswalks', 'precinct_to_2022_state_house_overlap.csv'),
      state_senate: path.join('Data', 'crosswalks', 'precinct_to_2022_state_senate_overlap.csv')
    },
    crosswalkOverrides: {
      congressional: path.join('Data', 'crosswalks', 'precinct_to_cd118_from_tabblocks.csv'),
      state_house: path.join('Data', 'crosswalks', 'precinct_to_2022_state_house_from_tabblocks.csv'),
      state_senate: path.join('Data', 'crosswalks', 'precinct_to_2022_state_senate_from_tabblocks.csv')
    }
  }
];

const SCOPES = ['congressional', 'state_house', 'state_senate'];
const CD2026_HISTORICAL = [
  {
    years: [2000, 2002, 2004, 2006, 2008],
    vtd: path.join('Data', 'mo_vtd00_precincts.geojson'),
    crosswalk: path.join('Data', 'crosswalks', 'vtd00_to_cd2026_from_nhgis.csv')
  },
  {
    years: [2010, 2012, 2014],
    vtd: path.join('Data', 'mo_vtd10_precincts.geojson'),
    crosswalk: path.join('Data', 'crosswalks', 'vtd10_to_cd2026_from_nhgis.csv')
  }
];

function rebuildCd2026Manifest() {
  const outDir = path.join(ROOT, 'Data', 'district_contests_2026');
  const files = fs.readdirSync(outDir)
    .filter(name => /^congressional_.+_\d{4}\.json$/i.test(name))
    .sort();
  const entries = files.map(name => {
    const payload = JSON.parse(fs.readFileSync(path.join(outDir, name), 'utf8'));
    const meta = payload.meta || {};
    const results = (payload.general && payload.general.results) || {};
    const rows = Object.values(results);
    const demTotal = rows.reduce((sum, row) => sum + (Number(row.dem_votes) || 0), 0);
    const repTotal = rows.reduce((sum, row) => sum + (Number(row.rep_votes) || 0), 0);
    return {
      scope: 'congressional',
      contest_type: meta.contest_type,
      year: meta.year,
      file: name,
      districts: Number(meta.district_count) || rows.length,
      rows: rows.length,
      dem_total: demTotal,
      rep_total: repTotal,
      major_party_contested: demTotal > 0 && repTotal > 0,
      match_coverage_pct: meta.match_coverage_pct,
      source_method: meta.source_method,
      lines_year: 2026,
      ...(meta.direct_transfer ? { direct_transfer: meta.direct_transfer } : {})
    };
  });
  fs.writeFileSync(
    path.join(outDir, 'manifest.json'),
    `${JSON.stringify({ files: entries, lines_year: 2026 }, null, 2)}\n`,
    'utf8'
  );
}

function main() {
  console.log('Rebuilding statewide district overlap slices...');
  for (const vintage of VINTAGES) {
    const years = vintage.years;
    console.log(`\n== ${vintage.label} (${years[0]}–${years[years.length - 1]}) ==`);
    for (const scope of SCOPES) {
      const crosswalkPath = vintage.crosswalks[scope];
      if (!crosswalkPath) throw new Error(`Missing crosswalk for ${vintage.label}/${scope}`);
      const args = buildScriptArgs({
        scope,
        years,
        vtdPath: vintage.vtd,
        crosswalkPath,
        crosswalkOverridePath: (vintage.crosswalkOverrides || {})[scope] || ''
      });
      run(process.execPath, args);
    }
  }
  console.log('\n== 2026 congressional lines (pre-2016 contests) ==');
  for (const vintage of CD2026_HISTORICAL) {
    const args = buildScriptArgs({
      scope: 'congressional',
      years: vintage.years,
      vtdPath: vintage.vtd,
      crosswalkPath: vintage.crosswalk,
      outputDir: path.join('Data', 'district_contests_2026'),
      filenameSuffix: 'none',
      linesYear: 2026
    });
    run(process.execPath, args);
  }
  rebuildCd2026Manifest();
  console.log('\nDone.');
}

main();

