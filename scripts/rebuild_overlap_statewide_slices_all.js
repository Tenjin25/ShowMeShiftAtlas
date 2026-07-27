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

function buildScriptArgs({ scope, years, vtdPath, crosswalkPath }) {
  return [
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
    }
  }
];

const SCOPES = ['congressional', 'state_house', 'state_senate'];

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
        crosswalkPath
      });
      run(process.execPath, args);
    }
  }
  console.log('\nDone.');
}

main();

