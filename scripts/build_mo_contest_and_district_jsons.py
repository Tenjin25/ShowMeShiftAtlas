#!/usr/bin/env python
"""
Build Missouri contest and district JSON slices from OpenElections-style precinct CSV files.

Outputs:
- Data/contests/manifest.json
- Data/contests/manifest_statewide_contested.json
- Data/contests/<contest_type>_<year>.json
- Data/district_contests/manifest.json
- Data/district_contests/<scope>_<contest_type>_<year>.json
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "Data"

CONTESTS_DIR = DATA_DIR / "contests"
DISTRICT_DIR = DATA_DIR / "district_contests"

COUNTY_GEOJSON_PATH = DATA_DIR / "tl_2020_29_county20.geojson"


STATEWIDE_OFFICE_MAP = {
    "PRESIDENT": "president",
    "US PRESIDENT": "president",
    "U.S. PRESIDENT": "president",
    "US SENATE": "us_senate",
    "U.S. SENATE": "us_senate",
    "GOVERNOR": "governor",
    "LIEUTENANT GOVERNOR": "lieutenant_governor",
    "ATTORNEY GENERAL": "attorney_general",
    "SECRETARY OF STATE": "secretary_of_state",
    "SECRETARY OF STATE": "secretary_of_state",
    "SECRETARY OF STATE": "secretary_of_state",
    "SECRETARY OF STATE": "secretary_of_state",
    "SECRETARY OF STATE": "secretary_of_state",
    "STATE TREASURER": "treasurer",
    "TREASURER": "treasurer",
    "STATE AUDITOR": "auditor",
    "AUDITOR": "auditor",
    "LABOR COMMISSIONER": "labor_commissioner",
    "INSURANCE COMMISSIONER": "insurance_commissioner",
    "AGRICULTURE COMMISSIONER": "agriculture_commissioner",
    "SUPERINTENDENT": "superintendent",
    "SUPERINTENDENT OF PUBLIC INSTRUCTION": "superintendent",
}

STATEWIDE_CONTEST_TYPES = {
    "president",
    "us_senate",
    "governor",
    "lieutenant_governor",
    "attorney_general",
    "secretary_of_state",
    "treasurer",
    "auditor",
    "labor_commissioner",
    "insurance_commissioner",
    "agriculture_commissioner",
    "superintendent",
}


@dataclass
class VoteAgg:
    dem_votes: int = 0
    rep_votes: int = 0
    other_votes: int = 0
    total_votes: int = 0
    dem_candidates: Counter = field(default_factory=Counter)
    rep_candidates: Counter = field(default_factory=Counter)

    def add(self, party_bucket: str, candidate: str, votes: int) -> None:
        self.total_votes += votes
        if party_bucket == "dem":
            self.dem_votes += votes
            if candidate:
                self.dem_candidates[candidate] += votes
        elif party_bucket == "rep":
            self.rep_votes += votes
            if candidate:
                self.rep_candidates[candidate] += votes
        else:
            self.other_votes += votes

    def to_result(self) -> Dict[str, object]:
        dem_candidate = self.dem_candidates.most_common(1)[0][0] if self.dem_candidates else ""
        rep_candidate = self.rep_candidates.most_common(1)[0][0] if self.rep_candidates else ""
        margin = self.rep_votes - self.dem_votes
        margin_pct = (margin / self.total_votes * 100.0) if self.total_votes else 0.0
        winner = "REP" if margin > 0 else ("DEM" if margin < 0 else "TIE")
        return {
            "dem_votes": int(self.dem_votes),
            "rep_votes": int(self.rep_votes),
            "other_votes": int(self.other_votes),
            "total_votes": int(self.total_votes),
            "dem_candidate": dem_candidate,
            "rep_candidate": rep_candidate,
            "margin": int(margin),
            "margin_pct": round(float(margin_pct), 6),
            "winner": winner,
            "color": "",
        }


@dataclass
class RaceCandidateInfo:
    first_seen: int
    has_explicit_party: bool = False


def normalize_county_key(value: str) -> str:
    token = (value or "").upper()
    token = token.replace("&", " AND ")
    token = token.replace("SAINT ", "ST ")
    token = token.replace(" COUNTY", "")
    token = token.replace(" CITY", " CITY")
    token = re.sub(r"[^A-Z0-9]", "", token)
    return token


def county_canonical_from_feature(props: Dict[str, object]) -> str:
    name = str(props.get("NAME20") or "").strip()
    if not name:
        return ""

    namelsad = str(props.get("NAMELSAD20") or "").strip().upper()
    classfp = str(props.get("CLASSFP20") or "").strip().upper()
    name_upper = name.upper()

    # Missouri's independent City of St. Louis shares NAME20 with St. Louis County,
    # so NAME20 alone is ambiguous here.
    if namelsad.endswith(" CITY") or classfp == "C7":
        if normalize_county_key(name_upper) == normalize_county_key("ST LOUIS"):
            return "ST. LOUIS CITY"
        return f"{name_upper} CITY"

    return name_upper


def build_county_lookup() -> Dict[str, str]:
    lookup: Dict[str, str] = {}
    if not COUNTY_GEOJSON_PATH.exists():
        return lookup

    with COUNTY_GEOJSON_PATH.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    for feature in payload.get("features", []):
        props = feature.get("properties", {}) or {}
        canonical = county_canonical_from_feature(props)
        if not canonical:
            continue
        namelsad = str(props.get("NAMELSAD20") or "").strip().upper()
        key = normalize_county_key(canonical)
        lookup[key] = canonical
        if namelsad:
            lookup.setdefault(normalize_county_key(namelsad), canonical)

        # Extra aliases for historical naming variants in election CSV files.
        if canonical == "ST. LOUIS":
            lookup.setdefault(normalize_county_key("ST LOUIS"), canonical)
            lookup.setdefault(normalize_county_key("ST LOUIS COUNTY"), canonical)
            lookup.setdefault(normalize_county_key("SAINT LOUIS"), canonical)
            lookup.setdefault(normalize_county_key("SAINT LOUIS COUNTY"), canonical)
        elif canonical == "ST. LOUIS CITY":
            lookup.setdefault(normalize_county_key("ST LOUIS CITY"), canonical)
            lookup.setdefault(normalize_county_key("ST. LOUIS CITY"), canonical)
            lookup.setdefault(normalize_county_key("SAINT LOUIS CITY"), canonical)
            lookup.setdefault(normalize_county_key("CITY OF ST LOUIS"), canonical)
            lookup.setdefault(normalize_county_key("CITY OF ST. LOUIS"), canonical)
            lookup.setdefault(normalize_county_key("CITY OF SAINT LOUIS"), canonical)
        else:
            lookup.setdefault(normalize_county_key(canonical + " COUNTY"), canonical)

    # Election board jurisdiction labels that should aggregate into county geography.
    # Kansas City Election Board totals should roll up into Jackson County for county maps.
    lookup[normalize_county_key("KANSAS CITY")] = "JACKSON"
    lookup[normalize_county_key("KANSAS CITY COUNTY")] = "JACKSON"
    return lookup


def canonical_county_name(raw_county: str, county_lookup: Dict[str, str]) -> str:
    raw = (raw_county or "").strip()
    if not raw:
        return ""
    key = normalize_county_key(raw)
    return county_lookup.get(key, raw.upper())


def parse_votes(raw_votes: str) -> int:
    if raw_votes is None:
        return 0
    text = str(raw_votes).strip().replace(",", "")
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def normalize_party(raw_party: str) -> str:
    return (raw_party or "").strip().upper()


def party_bucket(raw_party: str) -> str:
    party = normalize_party(raw_party)
    if party.startswith("DEM"):
        return "dem"
    if party.startswith("REP"):
        return "rep"
    return "other"


def candidate_name(row: Dict[str, str]) -> str:
    candidate = (row.get("candidate") or "").strip()
    if candidate:
        return candidate

    # Legacy 2000 file uses first/last columns.
    first = (row.get("first name") or row.get("first_name") or "").strip()
    last = (row.get("last name") or row.get("last_name") or "").strip()
    if first or last:
        return f"{first} {last}".strip()
    return ""


def race_key(year: int, contest_type: str, scope: Optional[str], district: str) -> Tuple[int, str, str, str]:
    return int(year), contest_type, scope or "", district if scope else ""


def build_blank_party_inference() -> Dict[Tuple[Tuple[int, str, str, str], str], str]:
    race_candidates: Dict[Tuple[int, str, str, str], Dict[str, RaceCandidateInfo]] = defaultdict(dict)
    sequence = 0

    for year, csv_path in iter_missouri_general_csvs():
        with csv_path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                contest_type, scope, district = map_contest_type(
                    row.get("office", ""),
                    row.get("district", ""),
                )
                if not contest_type:
                    continue

                candidate = candidate_name(row)
                if not candidate:
                    continue

                key = race_key(year, contest_type, scope, district)
                info = race_candidates[key].get(candidate)
                if info is None:
                    info = RaceCandidateInfo(first_seen=sequence)
                    race_candidates[key][candidate] = info
                    sequence += 1

                if normalize_party(
                    row.get("party") or row.get("party_simplified") or row.get("party_detailed") or ""
                ):
                    info.has_explicit_party = True

    inferred: Dict[Tuple[Tuple[int, str, str, str], str], str] = {}
    for key, candidates in race_candidates.items():
        if len(candidates) < 2:
            continue
        if any(info.has_explicit_party for info in candidates.values()):
            continue

        ordered = sorted(candidates.items(), key=lambda item: item[1].first_seen)
        inferred[(key, ordered[0][0])] = "rep"
        inferred[(key, ordered[1][0])] = "dem"

    return inferred


def normalize_office(raw_office: str) -> str:
    office = (raw_office or "").strip().upper()
    office = office.replace("U.S.", "US")
    office = office.replace("SECRETARY OF STATE", "SECRETARY OF STATE")
    office = re.sub(r"\s+", " ", office)
    return office


def extract_district(raw_office: str, raw_district: str) -> str:
    district = (raw_district or "").strip()
    if district and district.upper() != "STATEWIDE":
        m = re.search(r"(\d+)", district)
        if m:
            return str(int(m.group(1)))
        return district

    office = normalize_office(raw_office)
    m = re.search(r"\bDISTRICT\s*([0-9]+)\b", office)
    if m:
        return str(int(m.group(1)))
    return ""


def map_contest_type(raw_office: str, raw_district: str) -> Tuple[Optional[str], Optional[str], str]:
    office = normalize_office(raw_office)
    district = extract_district(raw_office, raw_district)

    # District races first.
    if office.startswith("US HOUSE") or office.startswith("US REPRESENTATIVE"):
        return "us_house", "congressional", district
    if office.startswith("STATE HOUSE") or office.startswith("STATE REPRESENTATIVE"):
        return "state_house", "state_house", district
    if office.startswith("STATE SENATE") or office.startswith("STATE SENATOR"):
        return "state_senate", "state_senate", district

    # Statewide races.
    if office in STATEWIDE_OFFICE_MAP:
        return STATEWIDE_OFFICE_MAP[office], None, ""

    # Additional normalization fallbacks.
    if office.startswith("PRESIDENT"):
        return "president", None, ""
    if office.startswith("US SENATE"):
        return "us_senate", None, ""
    if office.startswith("GOVERNOR"):
        return "governor", None, ""
    if office.startswith("LIEUTENANT GOVERNOR"):
        return "lieutenant_governor", None, ""
    if office.startswith("ATTORNEY GENERAL"):
        return "attorney_general", None, ""
    if office.startswith("SECRETARY OF STATE"):
        return "secretary_of_state", None, ""
    if office.startswith("STATE TREASURER"):
        return "treasurer", None, ""
    if office.startswith("STATE AUDITOR"):
        return "auditor", None, ""

    return None, None, ""


def iter_missouri_general_csvs() -> Iterable[Tuple[int, Path]]:
    paths = sorted(DATA_DIR.glob("*__mo__general__precinct.csv"))
    for path in paths:
        try:
            year = int(path.name[:4])
        except ValueError:
            continue
        yield year, path


def aggregate_data() -> Tuple[
    Dict[Tuple[str, int, str], VoteAgg],
    Dict[Tuple[str, str, int, str], VoteAgg],
]:
    county_lookup = build_county_lookup()
    inferred_buckets = build_blank_party_inference()
    contest_agg: Dict[Tuple[str, int, str], VoteAgg] = defaultdict(VoteAgg)
    district_agg: Dict[Tuple[str, str, int, str], VoteAgg] = defaultdict(VoteAgg)

    for year, csv_path in iter_missouri_general_csvs():
        with csv_path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                contest_type, scope, district = map_contest_type(
                    row.get("office", ""),
                    row.get("district", ""),
                )
                if not contest_type:
                    continue

                county_raw = row.get("county") or row.get("county_name") or ""
                county = canonical_county_name(county_raw, county_lookup)
                if not county:
                    continue

                votes = parse_votes(row.get("votes", "0"))
                if votes < 0:
                    continue

                party = row.get("party") or row.get("party_simplified") or row.get("party_detailed") or ""
                candidate = candidate_name(row)
                bucket = party_bucket(party)
                if bucket == "other" and not normalize_party(party) and candidate:
                    bucket = inferred_buckets.get((race_key(year, contest_type, scope, district), candidate), bucket)

                contest_agg[(contest_type, year, county)].add(bucket, candidate, votes)

                if scope and district:
                    district_agg[(scope, contest_type, year, district)].add(bucket, candidate, votes)

    return contest_agg, district_agg


def write_json(path: Path, payload: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        json.dump(payload, f, ensure_ascii=True, indent=2)
        f.write("\n")


def build_contest_slices(contest_agg: Dict[Tuple[str, int, str], VoteAgg]) -> List[Dict[str, object]]:
    grouped: Dict[Tuple[str, int], List[Tuple[str, VoteAgg]]] = defaultdict(list)
    for (contest_type, year, county), agg in contest_agg.items():
        grouped[(contest_type, year)].append((county, agg))

    generated_at = datetime.now(timezone.utc).isoformat()
    manifest_entries: List[Dict[str, object]] = []

    for (contest_type, year), rows in sorted(grouped.items(), key=lambda x: (x[0][0], x[0][1])):
        out_rows = []
        dem_total = 0
        rep_total = 0

        for county, agg in sorted(rows, key=lambda x: x[0]):
            result = agg.to_result()
            dem_total += int(result["dem_votes"])
            rep_total += int(result["rep_votes"])
            out_rows.append({
                "county": county,
                **result,
            })

        file_name = f"{contest_type}_{year}.json"
        payload = {
            "meta": {
                "contest_type": contest_type,
                "year": int(year),
                "rows": len(out_rows),
                "generated_at": generated_at,
            },
            "rows": out_rows,
        }
        write_json(CONTESTS_DIR / file_name, payload)

        manifest_entries.append({
            "contest_type": contest_type,
            "year": int(year),
            "file": file_name,
            "rows": len(out_rows),
            "dem_total": dem_total,
            "rep_total": rep_total,
            "major_party_contested": bool(dem_total > 0 and rep_total > 0),
        })

    write_json(CONTESTS_DIR / "manifest.json", {"files": manifest_entries})
    statewide_contested = [
        entry
        for entry in manifest_entries
        if entry.get("contest_type") in STATEWIDE_CONTEST_TYPES and bool(entry.get("major_party_contested"))
    ]
    write_json(CONTESTS_DIR / "manifest_statewide_contested.json", {"files": statewide_contested})
    return manifest_entries


def build_district_payload(
    scope: str,
    contest_type: str,
    year: int,
    rows: List[Tuple[str, VoteAgg]],
    generated_at: str,
) -> Dict[str, object]:
    results = {}
    for district, agg in sorted(rows, key=lambda x: int(x[0]) if x[0].isdigit() else x[0]):
        results[str(district)] = agg.to_result()

    return {
        "meta": {
            "scope": scope,
            "contest_type": contest_type,
            "year": int(year),
            "district_count": len(results),
            "match_coverage_pct": 100.0,
            "generated_at": generated_at,
        },
        "general": {
            "results": results,
        },
    }


def write_district_bundle(
    target_dir: Path,
    payload_by_file: Dict[str, Dict[str, object]],
    manifest_entries: List[Dict[str, object]],
) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    for file_name, payload in payload_by_file.items():
        write_json(target_dir / file_name, payload)
    write_json(target_dir / "manifest.json", {"files": manifest_entries})


def build_district_slices(
    district_agg: Dict[Tuple[str, str, int, str], VoteAgg],
) -> List[Dict[str, object]]:
    grouped: Dict[Tuple[str, str, int], List[Tuple[str, VoteAgg]]] = defaultdict(list)
    for (scope, contest_type, year, district), agg in district_agg.items():
        grouped[(scope, contest_type, year)].append((district, agg))

    generated_at = datetime.now(timezone.utc).isoformat()
    manifest_entries: List[Dict[str, object]] = []
    payload_by_file: Dict[str, Dict[str, object]] = {}

    for (scope, contest_type, year), rows in sorted(grouped.items(), key=lambda x: (x[0][0], x[0][1], x[0][2])):
        payload = build_district_payload(scope, contest_type, year, rows, generated_at)
        file_name = f"{scope}_{contest_type}_{year}.json"
        payload_by_file[file_name] = payload

        dem_total = sum(int(r[1].dem_votes) for r in rows)
        rep_total = sum(int(r[1].rep_votes) for r in rows)
        manifest_entries.append({
            "scope": scope,
            "contest_type": contest_type,
            "year": int(year),
            "file": file_name,
            "districts": len(rows),
            "rows": len(rows),
            "dem_total": dem_total,
            "rep_total": rep_total,
            "major_party_contested": bool(dem_total > 0 and rep_total > 0),
        })

    # Main district directory (2022 lines).
    write_district_bundle(DISTRICT_DIR, payload_by_file, manifest_entries)

    return manifest_entries


def ensure_placeholder_files() -> None:
    (DATA_DIR / "district_descriptions.json").parent.mkdir(parents=True, exist_ok=True)
    if not (DATA_DIR / "district_descriptions.json").exists():
        write_json(DATA_DIR / "district_descriptions.json", {})
    if not (DATA_DIR / "mo_elections_aggregated.json").exists():
        write_json(DATA_DIR / "mo_elections_aggregated.json", {"results_by_year": {}})
    if not (DATA_DIR / "mo_district_results_2022_lines.json").exists():
        write_json(DATA_DIR / "mo_district_results_2022_lines.json", {"results_by_year": {}})


def main() -> None:
    contest_agg, district_agg = aggregate_data()
    contest_manifest = build_contest_slices(contest_agg)
    district_manifest = build_district_slices(district_agg)
    ensure_placeholder_files()
    print(f"Built {len(contest_manifest)} county contest slices in {CONTESTS_DIR}")
    print(f"Built {len(district_manifest)} district contest slices in {DISTRICT_DIR}")


if __name__ == "__main__":
    main()
