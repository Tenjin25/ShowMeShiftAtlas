#!/usr/bin/env python
"""
Aggregate statewide contests onto 2026 congressional districts using the
tabblock / NHGIS VTD→CD crosswalks, with the Jackson / Kansas City fix.

Year → geography
----------------
  >= 2020 : VTD20 + tabblock20 weights
  2010-19 : VTD10 + NHGIS(tabblock10→20)
  <= 2008 : VTD00 + NHGIS(tabblock00→10→20)

Outputs (separate from 2022 lines)
----------------------------------
  - Data/district_contests_2026/congressional_{contest}_{year}.json
  - Data/district_contests_2026/manifest.json

Usage
-----
  python scripts/aggregate_congressional_2026_from_tabblocks.py
  python scripts/aggregate_congressional_2026_from_tabblocks.py --years 2020,2024 --contests president
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import re
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "Data"
CROSSWALK_DIR = DATA_DIR / "crosswalks"
OUT_DIR = DATA_DIR / "district_contests_2026"
VTD20_GEOJSON = DATA_DIR / "mo_vtd20_precincts.geojson"
VTD10_GEOJSON = DATA_DIR / "mo_vtd10_precincts.geojson"
VTD00_GEOJSON = DATA_DIR / "mo_vtd00_precincts.geojson"

CROSSWALK_BY_ERA = {
    "vtd20": CROSSWALK_DIR / "precinct_to_cd2026_from_tabblocks.csv",
    "vtd10": CROSSWALK_DIR / "vtd10_to_cd2026_from_nhgis.csv",
    "vtd00": CROSSWALK_DIR / "vtd00_to_cd2026_from_nhgis.csv",
}
VTD_GEOJSON_BY_ERA = {
    "vtd20": VTD20_GEOJSON,
    "vtd10": VTD10_GEOJSON,
    "vtd00": VTD00_GEOJSON,
}

# Same statewide-contest years as Data/district_contests congressional_*_overlap.json
DEFAULT_YEARS = (
    2000, 2002, 2004, 2006, 2008,
    2010, 2012, 2014, 2016, 2018,
    2020, 2022, 2024,
)
SCOPE = "congressional"
TRANSFER_DISTRICTS = ("7", "8")

# Match getPreferredDistrictSlicePaths() in index.html so the copied rows are
# exactly the results users see when the atlas is set to the 2022 lines.
VEST_MAIN_BY_YEAR = {
    2016: {
        "president",
        "us_senate",
        "governor",
        "lieutenant_governor",
        "attorney_general",
        "secretary_of_state",
        "treasurer",
    },
    2018: {"us_senate", "auditor"},
    2020: {
        "president",
        "governor",
        "lieutenant_governor",
        "attorney_general",
        "secretary_of_state",
        "treasurer",
    },
}


def _load_jackson_helpers():
    """Reuse alias / VoteAgg helpers from the Jackson senate tabblock aggregator."""
    import sys

    path = Path(__file__).resolve().parent / "aggregate_jackson_sd_statewide_from_tabblocks.py"
    module_name = "jackson_tabblock_agg"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Unable to load helpers from {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


J = _load_jackson_helpers()
VoteAgg = J.VoteAgg


def era_for_year(year: int) -> str:
    y = int(year)
    if y >= 2020:
        return "vtd20"
    if y >= 2010:
        return "vtd10"
    return "vtd00"


def resolve_era_inputs(year: int, crosswalk_override: Optional[Path] = None) -> Tuple[str, Path, Path]:
    era = era_for_year(year)
    if crosswalk_override and crosswalk_override.exists() and era == "vtd20":
        crosswalk = crosswalk_override
    else:
        crosswalk = CROSSWALK_BY_ERA[era]
    vtd_geojson = VTD_GEOJSON_BY_ERA[era]
    if not crosswalk.exists():
        for fallback in ("vtd20", "vtd10", "vtd00"):
            candidate = CROSSWALK_BY_ERA[fallback]
            if candidate.exists():
                print(
                    f"Warning: missing {crosswalk.name} for {year}; "
                    f"falling back to {candidate.name}"
                )
                return fallback, candidate, VTD_GEOJSON_BY_ERA[fallback]
        raise SystemExit(
            f"Missing crosswalk for {year} ({era}). "
            "Run: python scripts/build_congressional_2026_crosswalks_from_tabblocks.py --with-nhgis"
        )
    if not vtd_geojson.exists():
        raise SystemExit(f"Missing VTD geojson for {era}: {vtd_geojson}")
    return era, crosswalk, vtd_geojson


def parse_years(raw: str) -> List[int]:
    years: List[int] = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if part:
            years.append(int(part))
    return years or list(DEFAULT_YEARS)


def find_precinct_csv(year: int) -> Path:
    matches = sorted(DATA_DIR.glob(f"{year}????__mo__general__precinct.csv"))
    if not matches:
        raise SystemExit(f"Missing precinct CSV for {year} under {DATA_DIR}")
    return matches[0]


def normalize_weights(mapping: Dict[str, float]) -> Dict[str, float]:
    total = sum(v for v in mapping.values() if v and v > 0)
    if total <= 0:
        return {}
    return {k: (v / total) for k, v in mapping.items() if v and v > 0}


def load_crosswalk(path: Path) -> Dict[str, Dict[str, float]]:
    if not path.exists():
        raise SystemExit(
            f"Missing {path}. Run scripts/build_congressional_2026_crosswalks_from_tabblocks.py first."
        )
    by_precinct: Dict[str, Dict[str, float]] = defaultdict(dict)
    with path.open("r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            precinct = str(row.get("precinct_key") or "").strip().upper()
            district = J.normalize_district_num(row.get("district_num"))
            if not precinct or not district:
                continue
            try:
                weight = float(row.get("area_weight") or 0.0)
            except ValueError:
                weight = 0.0
            if weight <= 0:
                continue
            by_precinct[precinct][district] = by_precinct[precinct].get(district, 0.0) + weight

    out = {p: normalize_weights(w) for p, w in by_precinct.items() if normalize_weights(w)}
    districts = sorted(
        {d for weights in out.values() for d in weights},
        key=lambda x: int(x) if x.isdigit() else x,
    )
    print(f"Loaded crosswalk: {len(out):,} VTDs -> CDs {districts}")
    return out


def load_statewide_matcher(vtd_path: Path) -> Dict[str, Dict[str, object]]:
    """
    Build alias indexes keyed by election jurisdiction.
    Jackson is split into JACKSON (non-KC) and KANSAS CITY (KC-tagged).
    """
    if not vtd_path.exists():
        raise SystemExit(f"Missing {vtd_path}")

    payload = json.loads(vtd_path.read_text(encoding="utf-8"))
    by_county: Dict[str, Dict[str, object]] = {}

    def ensure_county(name: str) -> Dict[str, object]:
        if name not in by_county:
            by_county[name] = {
                "alias_to_norms": defaultdict(set),
                "features": [],
                "kc_norms": set(),
                "non_kc_norms": set(),
                "all_norms": set(),
            }
        return by_county[name]

    for feature in payload.get("features") or []:
        props = feature.get("properties") or {}
        precinct_norm = str(props.get("precinct_norm") or "").strip().upper()
        if not precinct_norm:
            continue
        county_fips = re.sub(
            r"[^0-9]",
            "",
            str(
                props.get("COUNTYFP20")
                or props.get("COUNTYFP10")
                or props.get("COUNTYFP00")
                or props.get("COUNTYFP")
                or ""
            ),
        ).zfill(3)
        county = J.election_county_key(
            props.get("county_nam") or props.get("COUNTYNAME") or precinct_norm.split(" - ", 1)[0]
        )
        if county_fips == "510":
            county = "ST LOUIS CITY"
            vtd_id = str(
                props.get("VTDST20")
                or props.get("VTDST10")
                or props.get("VTDST00")
                or props.get("prec_id")
                or ""
            ).strip().upper()
            if vtd_id:
                precinct_norm = f"ST. LOUIS CITY - {vtd_id}"
        # Geography county for Jackson VTDs is always JACKSON; election county may be KC.
        geo_county = county
        if county == "KANSAS CITY":
            geo_county = "JACKSON"

        prec_id = str(props.get("VTDST20") or props.get("prec_id") or "").strip().upper()
        name20 = str(
            props.get("NAME20") or props.get("NAME10") or props.get("NAME00") or props.get("NAME") or ""
        ).strip().upper()
        namelsad = str(
            props.get("NAMELSAD20") or props.get("NAMELSAD10") or props.get("NAMELSAD") or ""
        ).strip().upper()

        is_kc = False
        if geo_county == "JACKSON":
            blob = f"{name20} {namelsad} {precinct_norm}"
            if "KANSAS CITY" in blob or name20.startswith("KC") or " KC" in f" {name20}":
                is_kc = True
            if re.search(r"\bKC\d", J.compact_token(name20)):
                is_kc = True

        aliases = set(
            J.alias_candidates(prec_id, name20, namelsad, precinct_norm, props.get("precinct_name"))
        )
        if geo_county == "JACKSON":
            for alias in J.expand_kc_ward_precinct_aliases(name20):
                aliases.update(J.alias_candidates(alias))
            for alias in J.expand_jackson_township_aliases(name20):
                aliases.update(J.alias_candidates(alias))
            for alias in J.expand_census_name_aliases(name20):
                aliases.update(J.alias_candidates(alias))
            if name20.startswith("BLUE "):
                aliases.update(J.alias_candidates(name20.replace("-", " "), name20.replace(" ", "")))

        feature_row = {
            "precinct_norm": precinct_norm,
            "name_aliases": {J.normalize_alias_token(a) for a in aliases if J.normalize_alias_token(a)},
            "is_kansas_city": is_kc,
        }

        if geo_county == "JACKSON":
            jackson_all = ensure_county("JACKSON")
            targets = [jackson_all]
            if is_kc:
                kansas_city = ensure_county("KANSAS CITY")
                targets.append(kansas_city)
                jackson_all["kc_norms"].add(precinct_norm)
                kansas_city["kc_norms"].add(precinct_norm)
            else:
                jackson_all["non_kc_norms"].add(precinct_norm)
            for target in targets:
                target["features"].append(feature_row)
                target["all_norms"].add(precinct_norm)
                alias_map = target["alias_to_norms"]
                for alias in aliases:
                    alias_map[alias].add(precinct_norm)
        else:
            bucket = ensure_county(geo_county)
            bucket["features"].append(feature_row)
            bucket["all_norms"].add(precinct_norm)
            bucket["non_kc_norms"].add(precinct_norm)
            alias_map = bucket["alias_to_norms"]
            for alias in aliases:
                alias_map[alias].add(precinct_norm)

    jackson = by_county.get("JACKSON")
    kc = by_county.get("KANSAS CITY")
    print(
        f"Matcher: counties={len(by_county):,} "
        f"Jackson VTDs={len((jackson or {}).get('features') or [])} "
        f"(KC={len((jackson or {}).get('kc_norms') or [])}, "
        f"non-KC={len((jackson or {}).get('non_kc_norms') or [])}; "
        f"KC index norms={len((kc or {}).get('features') or [])})"
    )
    return by_county


def match_precinct_norms(
    raw_code: str,
    raw_precinct: str,
    election_county: str,
    matcher: Dict[str, Dict[str, object]],
) -> List[str]:
    county_info = matcher.get(election_county)
    if not county_info and election_county == "KANSAS CITY":
        county_info = matcher.get("JACKSON")
    if not county_info:
        return []

    raw_values = [raw_code, raw_precinct, f"{raw_code} {raw_precinct}".strip()]
    tokens = list(J.alias_candidates(*raw_values))
    if election_county in {"JACKSON", "KANSAS CITY"}:
        expanded: List[str] = []
        for token in tokens:
            for alias in J.expand_jackson_township_aliases(token):
                expanded.extend(J.alias_candidates(alias))
            for alias in J.expand_kc_ward_precinct_aliases(token):
                expanded.extend(J.alias_candidates(alias))
        tokens.extend(expanded)

    alias_map: Dict[str, Set[str]] = county_info["alias_to_norms"]  # type: ignore[assignment]

    # Multi-precinct bundles must keep every component.  Returning the first
    # unique alias turns labels such as "B1 01,02" or "AP1,2,3" into a single
    # arbitrary VTD and materially distorts split counties.
    st_louis_components: Set[str] = set()
    jackson_components: Set[str] = set()
    component_aliases: Set[str] = set()
    for value in raw_values:
        value_variants = {str(value or "").strip().upper()}
        value_variants.update(J.strip_election_sequence_prefix(value))
        for variant in value_variants:
            if not variant:
                continue
            for alias in J.expand_st_louis_precinct_aliases(variant):
                compact = J.compact_token(alias)
                match = re.fullmatch(r"([A-Z]+)0*(\d+)([A-Z]?)", compact)
                if match:
                    prefix, number, suffix = match.groups()
                    st_louis_components.add(f"{prefix}{int(number)}{suffix}")
            if election_county in {"JACKSON", "KANSAS CITY"}:
                jackson_components.update(J.jackson_township_component_labels(variant))
                jackson_components.update(J.kc_ward_precinct_component_labels(variant))
                component_aliases.update(J.expand_jackson_township_aliases(variant))
                component_aliases.update(J.expand_kc_ward_precinct_aliases(variant))

    # VTD00 polygons often bundle several St. Louis election precincts. Keep one
    # matched-polygon contribution per logical component so a 4+1 bundle is
    # weighted 80/20 rather than collapsing to two polygons and becoming 50/50.
    weighted_st_louis_hits: List[str] = []
    for component in sorted(st_louis_components):
        hits: Set[str] = set()
        for token in J.alias_candidates(component):
            hits.update(alias_map.get(token) or set())
        weighted_st_louis_hits.extend(sorted(hits))
    if weighted_st_louis_hits:
        return weighted_st_louis_hits

    weighted_jackson_hits: List[str] = []
    for component in sorted(jackson_components):
        hits: Set[str] = set()
        for token in J.alias_candidates(component):
            hits.update(alias_map.get(token) or set())
        if election_county == "KANSAS CITY":
            hits = {n for n in hits if n in county_info["kc_norms"]}
        else:
            hits = {n for n in hits if n in county_info["non_kc_norms"]}
        weighted_jackson_hits.extend(sorted(hits))
    if weighted_jackson_hits:
        return weighted_jackson_hits

    component_hits: Set[str] = set()
    for component in component_aliases:
        for token in J.alias_candidates(component):
            component_hits.update(alias_map.get(token) or set())
    if component_hits:
        if election_county == "KANSAS CITY":
            kc_only = {n for n in component_hits if n in county_info["kc_norms"]}
            if kc_only:
                component_hits = kc_only
        elif election_county == "JACKSON":
            non_kc = {n for n in component_hits if n in county_info["non_kc_norms"]}
            if non_kc:
                component_hits = non_kc
        return sorted(component_hits)

    best: Optional[Set[str]] = None
    best_size = math.inf
    for token in tokens:
        hits = alias_map.get(token)
        if not hits:
            continue
        if len(hits) == 1:
            return sorted(hits)
        if len(hits) < best_size:
            best = hits
            best_size = len(hits)
    if best and best_size <= 12:
        if election_county == "KANSAS CITY":
            kc_only = {n for n in best if n in county_info["kc_norms"]}
            if kc_only:
                return sorted(kc_only)
        if election_county == "JACKSON":
            non_kc = {n for n in best if n in county_info["non_kc_norms"]}
            if non_kc and len(non_kc) <= 12:
                return sorted(non_kc)
        return sorted(best)
    if election_county == "KANSAS CITY" and best:
        kc_only = {n for n in best if n in county_info["kc_norms"]}
        if kc_only:
            return sorted(kc_only)
    if best:
        return sorted(best)

    # Conservative name fallback for legacy files whose election sequence ids
    # do not equal Census VTD ids (for example, "001 Sherman" vs "Sherman1").
    fuzzy_tokens = {
        J.normalize_alias_token(token)
        for token in tokens
        if len(J.compact_token(token)) >= 4 and not J.compact_token(token).isdigit()
    }
    scored: List[Tuple[float, str]] = []
    for feature in county_info.get("features") or []:
        score = 0.0
        for alias in feature.get("name_aliases") or set():
            alias_compact = J.compact_token(alias)
            if len(alias_compact) < 4:
                continue
            for token in fuzzy_tokens:
                token_compact = J.compact_token(token)
                if len(token_compact) < 4:
                    continue
                if token_compact == alias_compact:
                    score = max(score, 100.0)
                elif token_compact in alias_compact or alias_compact in token_compact:
                    ratio = min(len(token_compact), len(alias_compact)) / max(
                        len(token_compact), len(alias_compact)
                    )
                    score = max(score, 60.0 + 30.0 * ratio)
        if score >= 75.0:
            scored.append((score, str(feature.get("precinct_norm") or "")))
    if not scored:
        return []
    top = max(score for score, _ in scored)
    matches = {norm for score, norm in scored if norm and score >= top - 0.5}
    if election_county == "KANSAS CITY":
        matches = {n for n in matches if n in county_info["kc_norms"]} or matches
    elif election_county == "JACKSON":
        matches = {n for n in matches if n in county_info["non_kc_norms"]} or matches
    return sorted(matches)[:12]


def district_weights_for_matches(
    matched_norms: Sequence[str],
    crosswalk: Dict[str, Dict[str, float]],
) -> Dict[str, float]:
    weights: Dict[str, float] = defaultdict(float)
    if not matched_norms:
        return {}
    share = 1.0 / len(matched_norms)
    for precinct_norm in matched_norms:
        for district, w in (crosswalk.get(precinct_norm) or {}).items():
            amount = share * float(w)
            if amount > 0:
                weights[district] += amount
    return normalize_weights(dict(weights))


def county_area_fallback(
    crosswalk: Dict[str, Dict[str, float]],
    election_county: str,
    matcher: Dict[str, Dict[str, object]],
) -> Dict[str, float]:
    weights: Dict[str, float] = defaultdict(float)
    county_info = matcher.get(election_county) or {}
    if election_county == "KANSAS CITY":
        norms = set(county_info.get("kc_norms") or [])
    elif election_county == "JACKSON":
        norms = set(county_info.get("non_kc_norms") or []) or set(county_info.get("all_norms") or [])
    else:
        norms = set(county_info.get("all_norms") or [])

    if not norms:
        # Fall back to any crosswalk keys for this county prefix.
        prefix = f"{election_county} - "
        if election_county == "KANSAS CITY":
            prefix = "JACKSON - "
        norms = {k for k in crosswalk if k.startswith(prefix)}

    for precinct_norm in norms:
        for district, w in (crosswalk.get(precinct_norm) or {}).items():
            weights[district] += float(w)
    return normalize_weights(dict(weights))


def read_year_precinct_aggs(
    year: int,
    contest_filter: Optional[Set[str]],
) -> Tuple[
    Dict[Tuple[str, str, str, str], VoteAgg],
    Dict[Tuple[str, str], Dict[str, float]],
    Dict[Tuple[str, str, str], Dict[str, float]],
]:
    csv_path = find_precinct_csv(year)
    print(f"Reading {csv_path.name} ...")
    precinct_aggs: Dict[Tuple[str, str, str, str], VoteAgg] = defaultdict(VoteAgg)
    candidate_totals: Dict[Tuple[str, str], Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    us_house_label_votes: Dict[Tuple[str, str, str], Dict[str, float]] = defaultdict(
        lambda: defaultdict(float)
    )

    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = 0
        kept = 0
        for row in reader:
            rows += 1
            county = J.election_county_key(row.get("county") or row.get("county_name"))
            if not county:
                continue
            votes = J.parse_votes(row.get("votes"))
            if votes <= 0:
                continue
            raw_code = str(
                row.get("precinct_code") or row.get("precinct_id") or row.get("ward") or ""
            ).strip().upper()
            raw_precinct = str(row.get("precinct") or row.get("precinct_name") or "").strip().upper()
            office = row.get("office") or ""
            contest = J.map_contest_type(office)
            candidate = J.candidate_name(row)
            bucket = J.infer_party_bucket(
                row.get("party") or row.get("party_simplified") or "",
                candidate,
            )

            if contest and (contest_filter is None or contest in contest_filter):
                key = (contest, county, raw_code, raw_precinct)
                precinct_aggs[key].add(bucket, candidate, votes)
                kept += 1
                if bucket in {"dem", "rep"} and candidate:
                    candidate_totals[(contest, bucket)][candidate] += votes

            office_norm = J.normalize_office(office)
            if office_norm.startswith("US HOUSE") or office_norm.startswith("US REPRESENTATIVE"):
                district = J.normalize_district_num(row.get("district"))
                if district:
                    us_house_label_votes[(county, raw_code, raw_precinct)][district] += votes

        print(f"  kept {kept:,} statewide precinct-contest rows from {rows:,} CSV rows")
    return precinct_aggs, candidate_totals, us_house_label_votes


def top_candidate(
    candidate_totals: Dict[Tuple[str, str], Dict[str, float]], contest: str, bucket: str
) -> str:
    mapping = candidate_totals.get((contest, bucket)) or {}
    if not mapping:
        return ""
    return max(mapping.items(), key=lambda kv: (kv[1], kv[0]))[0]


def aggregate_year(
    year: int,
    contests: Optional[Set[str]],
    crosswalk: Dict[str, Dict[str, float]],
    matcher: Dict[str, Dict[str, object]],
    era: str = "vtd20",
) -> Dict[str, Dict[str, object]]:
    precinct_aggs, candidate_totals, house_labels = read_year_precinct_aggs(year, contests)

    by_contest: Dict[str, List[Tuple[str, str, str, VoteAgg]]] = defaultdict(list)
    for (contest, county, raw_code, raw_precinct), agg in precinct_aggs.items():
        if agg.total_votes <= 0:
            continue
        by_contest[contest].append((county, raw_code, raw_precinct, agg))

    area_fallback_cache: Dict[str, Dict[str, float]] = {}
    statewide_area: Dict[str, float] = defaultdict(float)
    for weights in crosswalk.values():
        for district, w in weights.items():
            statewide_area[district] += float(w)
    statewide_area = normalize_weights(dict(statewide_area))

    def area_for(county: str) -> Dict[str, float]:
        if county not in area_fallback_cache:
            area_fallback_cache[county] = county_area_fallback(crosswalk, county, matcher)
        return area_fallback_cache[county] or statewide_area

    outputs: Dict[str, Dict[str, object]] = {}
    for contest, rows in sorted(by_contest.items()):
        dem_candidate = top_candidate(candidate_totals, contest, "dem")
        rep_candidate = top_candidate(candidate_totals, contest, "rep")
        district_agg: Dict[str, VoteAgg] = defaultdict(VoteAgg)
        county_matched_weights: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
        county_totals: Dict[str, float] = defaultdict(float)
        county_direct: Dict[str, float] = defaultdict(float)
        unmatched: List[Tuple[str, str, str, VoteAgg]] = []

        total_votes = 0.0
        allocated_votes = 0.0
        direct_votes = 0.0
        kc_votes = 0.0
        jackson_votes = 0.0
        kc_direct = 0.0
        jackson_direct = 0.0

        for county, raw_code, raw_precinct, agg in rows:
            precinct_total = agg.total_votes
            total_votes += precinct_total
            county_totals[county] += precinct_total
            if county == "KANSAS CITY":
                kc_votes += precinct_total
            elif county == "JACKSON":
                jackson_votes += precinct_total

            matched = match_precinct_norms(raw_code, raw_precinct, county, matcher)
            weights = district_weights_for_matches(matched, crosswalk)
            if weights:
                allocated_votes += precinct_total
                direct_votes += precinct_total
                county_direct[county] += precinct_total
                if county == "KANSAS CITY":
                    kc_direct += precinct_total
                elif county == "JACKSON":
                    jackson_direct += precinct_total
                for district, share in weights.items():
                    district_agg[district].add_scaled(agg, share)
                    county_matched_weights[county][district] += precinct_total * share
                continue
            unmatched.append((county, raw_code, raw_precinct, agg))

        for county, raw_code, raw_precinct, agg in unmatched:
            # Do NOT use same-year US House district labels here: historical CD
            # numbers (and even 2022 CD118) do not match the 2026 map and can
            # invent phantom districts (e.g. a 9th seat in 2000–2010).
            preferred = normalize_weights(dict(county_matched_weights.get(county) or {}))
            area = area_for(county)
            county_total = county_totals.get(county, 0.0)
            county_dir = county_direct.get(county, 0.0)
            alpha = (county_dir / county_total) if county_total > 0 else 0.0
            blended: Dict[str, float] = {}
            for k in set(preferred) | set(area):
                blended[k] = alpha * preferred.get(k, 0.0) + (1.0 - alpha) * area.get(k, 0.0)
            fallback = normalize_weights(blended) or preferred or area
            if not fallback:
                continue
            allocated_votes += agg.total_votes
            for district, share in fallback.items():
                district_agg[district].add_scaled(agg, share)

        results = {
            d: district_agg[d].to_result(dem_candidate, rep_candidate)
            for d in sorted(district_agg.keys(), key=lambda x: int(x) if x.isdigit() else x)
        }
        coverage = (allocated_votes / total_votes * 100.0) if total_votes else 0.0
        direct_coverage = (direct_votes / total_votes * 100.0) if total_votes else 0.0
        payload = {
            "meta": {
                "scope": SCOPE,
                "contest_type": contest,
                "year": int(year),
                "district_count": len(results),
                "match_coverage_pct": round(coverage, 6),
                "direct_match_coverage_pct": round(direct_coverage, 6),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "source_method": f"statewide_{era}_tabblock_nhgis_cd2026_kc_split",
                "lines_year": 2026,
                "geography_era": era,
                "jackson_votes": int(round(jackson_votes)),
                "kansas_city_votes": int(round(kc_votes)),
                "jackson_direct_votes": int(round(jackson_direct)),
                "kansas_city_direct_votes": int(round(kc_direct)),
                "districts": sorted(results.keys(), key=lambda x: int(x) if x.isdigit() else x),
            },
            "general": {"results": results},
        }
        outputs[contest] = payload
        print(
            f"  {contest} {year}: districts={len(results)} "
            f"coverage={coverage:.2f}% direct={direct_coverage:.2f}% "
            f"KC={int(kc_votes):,} (direct {int(kc_direct):,}) "
            f"Jackson={int(jackson_votes):,} (direct {int(jackson_direct):,})"
        )
    return outputs


def write_payload(path: Path, payload: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def preferred_2022_lines_source(contest: str, year: int) -> Path:
    base = f"congressional_{contest}_{int(year)}"
    main_path = DATA_DIR / "district_contests" / f"{base}.json"
    overlap_path = DATA_DIR / "district_contests" / f"{base}_overlap.json"
    prefer_main = contest in VEST_MAIN_BY_YEAR.get(int(year), set())
    candidates = (main_path, overlap_path) if prefer_main else (overlap_path, main_path)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise SystemExit(f"Missing 2022-lines source for {contest} {year}: {candidates}")


def transfer_unchanged_district_results(
    payload: Dict[str, object], contest: str, year: int
) -> Path:
    source_path = preferred_2022_lines_source(contest, year)
    source_payload = json.loads(source_path.read_text(encoding="utf-8"))
    source_results = ((source_payload.get("general") or {}).get("results")) or {}
    target_results = ((payload.get("general") or {}).get("results")) or {}

    for district in TRANSFER_DISTRICTS:
        source_row = source_results.get(district)
        if not source_row:
            raise SystemExit(
                f"Missing district {district} in 2022-lines source {source_path.name}"
            )
        target_results[district] = deepcopy(source_row)

    meta = payload.setdefault("meta", {})
    meta["direct_transfer"] = {
        "source_lines_year": 2022,
        "districts": list(TRANSFER_DISTRICTS),
        "source_file": source_path.name,
    }
    return source_path


def transfer_existing_outputs(
    out_dir: Path,
    years: Optional[Set[int]] = None,
    contests: Optional[Set[str]] = None,
) -> int:
    transferred = 0
    for path in sorted(out_dir.glob("congressional_*.json")):
        if path.name == "manifest.json" or path.name.endswith("_overlap.json"):
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        meta = payload.get("meta") or {}
        contest = str(meta.get("contest_type") or "").strip()
        year = int(meta.get("year") or 0)
        if not contest or not year or contest == "us_house":
            continue
        if years and year not in years:
            continue
        if contests and contest not in contests:
            continue
        source_path = transfer_unchanged_district_results(payload, contest, year)
        write_payload(path, payload)
        transferred += 1
        print(
            f"Transferred CDs {','.join(TRANSFER_DISTRICTS)} in {path.name} "
            f"from {source_path.name}"
        )
    return transferred


def rebuild_manifest(out_dir: Path) -> Path:
    files = []
    for path in sorted(out_dir.glob("congressional_*.json")):
        if path.name == "manifest.json" or path.name.endswith("_overlap.json"):
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        meta = payload.get("meta") or {}
        results = ((payload.get("general") or {}).get("results")) or {}
        dem_total = sum(int((r or {}).get("dem_votes") or 0) for r in results.values())
        rep_total = sum(int((r or {}).get("rep_votes") or 0) for r in results.values())
        entry = {
            "scope": SCOPE,
            "contest_type": meta.get("contest_type"),
            "year": meta.get("year"),
            "file": path.name,
            "districts": int(meta.get("district_count") or len(results)),
            "rows": len(results),
            "dem_total": dem_total,
            "rep_total": rep_total,
            "major_party_contested": dem_total > 0 and rep_total > 0,
            "match_coverage_pct": meta.get("match_coverage_pct"),
            "source_method": meta.get("source_method"),
            "lines_year": 2026,
        }
        if meta.get("direct_transfer"):
            entry["direct_transfer"] = meta["direct_transfer"]
        files.append(entry)
    manifest_path = out_dir / "manifest.json"
    write_payload(manifest_path, {"files": files, "lines_year": 2026})
    return manifest_path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--years", default=",".join(str(y) for y in DEFAULT_YEARS))
    ap.add_argument(
        "--contests",
        default="",
        help="Comma-separated contest types (default: all statewide contests found).",
    )
    ap.add_argument(
        "--crosswalk",
        type=Path,
        default=None,
        help="Optional override for the VTD20 crosswalk only. Historical years still use NHGIS era files.",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=OUT_DIR,
        help="Output folder for 2026 congressional contest slices.",
    )
    ap.add_argument(
        "--transfer-unchanged-only",
        action="store_true",
        help="Copy CDs 7 and 8 from the preferred 2022-lines slices without rebuilding other districts.",
    )
    ap.add_argument(
        "--transfer-unchanged",
        action="store_true",
        help=(
            "After rebuilding, replace CDs 7 and 8 from the 2022-lines slices. "
            "Disabled by default because mixing independently rounded crosswalks "
            "does not conserve statewide totals."
        ),
    )
    args = ap.parse_args()

    years = parse_years(args.years)
    contests = {c.strip().lower() for c in str(args.contests).split(",") if c.strip()} or None
    crosswalk_override = Path(args.crosswalk) if args.crosswalk else None
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.transfer_unchanged_only:
        transferred = transfer_existing_outputs(out_dir, set(years), contests)
        manifest_path = rebuild_manifest(out_dir)
        print(f"Wrote {manifest_path}")
        print(f"Done. Updated {transferred} aggregate file(s) in {out_dir}")
        return

    written = 0
    matcher_cache: Dict[str, Dict[str, Dict[str, object]]] = {}
    crosswalk_cache: Dict[str, Dict[str, Dict[str, float]]] = {}

    for year in years:
        era, crosswalk_path, vtd_path = resolve_era_inputs(year, crosswalk_override)
        print(f"\n=== {year} using {era} ({crosswalk_path.name}) ===")
        if era not in crosswalk_cache:
            crosswalk_cache[era] = load_crosswalk(crosswalk_path)
        if era not in matcher_cache:
            matcher_cache[era] = load_statewide_matcher(vtd_path)

        payloads = aggregate_year(
            year,
            contests,
            crosswalk_cache[era],
            matcher_cache[era],
            era=era,
        )
        for contest, payload in payloads.items():
            source_path = None
            if args.transfer_unchanged:
                source_path = transfer_unchanged_district_results(payload, contest, year)
            out_path = out_dir / f"congressional_{contest}_{year}.json"
            write_payload(out_path, payload)
            written += 1
            if source_path:
                print(
                    f"Wrote {out_path} "
                    f"(CDs {','.join(TRANSFER_DISTRICTS)} from {source_path.name})"
                )
            else:
                print(f"Wrote {out_path}")

    manifest_path = rebuild_manifest(out_dir)
    print(f"Wrote {manifest_path}")
    print(f"Done. Wrote {written} aggregate file(s) to {out_dir}")


if __name__ == "__main__":
    main()
