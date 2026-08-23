#!/usr/bin/env python
"""
Re-aggregate statewide contests onto Jackson-based legislative districts
using the tabblock crosswalk, with Kansas City kept as its own election
jurisdiction during precinct matching.

Supports --scope state_senate (default) or state_house.

Why
---
OpenElections reports Kansas City Board precincts under county="Kansas City"
and Jackson County Election Board precincts under county="Jackson". County
maps already roll KC into JACKSON. For Jackson-area aggregation we must:

  1) Match KANSAS CITY rows against KC-tagged VTDs only
  2) Match JACKSON rows against non-KC Jackson VTDs (with KC as fallback)
  3) Allocate through tabblock-derived VTD → district weights

Inputs
------
  - Data/crosswalks/jackson_vtd20_to_2022_{scope}_from_tabblocks.csv
  - Data/crosswalks/jackson_vtd10_to_2022_{scope}_from_nhgis.csv
  - Data/crosswalks/jackson_vtd00_to_2022_{scope}_from_nhgis.csv
  - Data/mo_vtd{00,10,20}_precincts.geojson
  - Data/YYYYMMDD__mo__general__precinct.csv

Year → geography
----------------
  >= 2020 : VTD20 + tabblock20 weights
  2012-19 : VTD10 + NHGIS(tabblock10→20)
  <= 2011 : VTD00 + NHGIS(tabblock00→10→20)

Outputs
-------
  - Data/district_contests/{scope}_{contest}_{year}_jackson_tabblocks.json
  - Optional patch of Jackson districts into existing *_overlap.json (--patch-overlap)

Usage
-----
  python scripts/aggregate_jackson_sd_statewide_from_tabblocks.py
  python scripts/aggregate_jackson_sd_statewide_from_tabblocks.py --scope state_house --years 2018 --contests auditor --patch-overlap
  python scripts/aggregate_jackson_sd_statewide_from_tabblocks.py --years 2020,2024 --patch-overlap
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "Data"
CROSSWALK_DIR = DATA_DIR / "crosswalks"
DISTRICT_DIR = DATA_DIR / "district_contests"
VTD20_GEOJSON = DATA_DIR / "mo_vtd20_precincts.geojson"
VTD10_GEOJSON = DATA_DIR / "mo_vtd10_precincts.geojson"
VTD00_GEOJSON = DATA_DIR / "mo_vtd00_precincts.geojson"

VALID_SCOPES = ("state_senate", "state_house")
VTD_GEOJSON_BY_ERA = {
    "vtd20": VTD20_GEOJSON,
    "vtd10": VTD10_GEOJSON,
    "vtd00": VTD00_GEOJSON,
}

DEFAULT_YEARS = (2012, 2016, 2020, 2024)
DEFAULT_DISTRICTS_BY_SCOPE = {
    "state_senate": ("8", "11"),
    "state_house": None,  # all Jackson-touching from crosswalk
}

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
    "STATE TREASURER": "treasurer",
    "TREASURER": "treasurer",
    "STATE AUDITOR": "auditor",
    "AUDITOR": "auditor",
}

# OpenElections sometimes omits party (e.g. 2018 State Auditor).
# Keyed by normalized candidate name (lowercase, alnum-only).
KNOWN_CANDIDATE_PARTIES = {
    "nicolegalloway": "dem",
    "saundramcdowell": "rep",
    "seanotoole": "other",
    "jacobluetkemeyer": "other",
    "donfitz": "other",
    "arniecdienoff": "other",
    "arniecdienoffac": "other",
}

NON_GEO_RE = re.compile(
    r"(^|\b)(ABSENTEE|PROVISIONAL|CURBSIDE|CENTRAL|EARLY|VOTE\s*CENTER|VOTECENTER|WRITE-?INS?|CUMULATIVE|FEDERAL|TRANS|WRITE-IN)(\b|$)",
    re.I,
)
WARD_PRECINCT_RE = re.compile(
    r"\bW(?:ARD)?\s*0*(\d{1,2})\s*[-/ ]\s*P(?:CT|RECINCT)?\s*0*(\d{1,2})\b",
    re.I,
)
WARD_BUNDLE_RE = re.compile(
    r"\bW(?:ARD)?\s*0*(\d{1,2})\s*[- ]\s*P(?:CT|RECINCT)?\s*([0-9,\s/]+)",
    re.I,
)
JACKSON_TOWNSHIP_ALIASES = {
    "PR": ["PRAIRIE", "PRARIE"],
    "PRAIRIE": ["PRAIRIE", "PRARIE"],
    "PRARIE": ["PRAIRIE", "PRARIE"],
    "FO": ["FORT OSAGE"],
    "FORT": ["FORT OSAGE"],
    "BR": ["BROOKING", "BROOKING NO", "BROOKING NO."],
    "BROOKING": ["BROOKING", "BROOKING NO", "BROOKING NO."],
    "WA": ["WASHINGTON"],
    "WASHINGTON": ["WASHINGTON"],
    "VB": ["VAN BUREN"],
    "VAN": ["VAN BUREN"],
    "SN": ["SNI-A-BAR", "SNI A BAR", "SNIABAR"],
    "SNI": ["SNI-A-BAR", "SNI A BAR", "SNIABAR"],
    "SI": ["SNI-A-BAR", "SNI A BAR", "SNIABAR"],
}

# St. Louis County election files use short township codes while the 2000 VTD
# file often spells those names out.  Keeping the mapping here also lets bundle
# labels such as "AP 1,2,3" resolve to every constituent VTD instead of one
# arbitrary numeric alias.
ST_LOUIS_JURISDICTION_ALIASES = {
    "AIRPORT": "AP",
    "BONHOMME": "BON",
    "CHESTERFIELD": "CHE",
    "CLAYTON": "CLA",
    "CONCORD": "CON",
    "CREVECOEUR": "CC",
    "FERGUSON": "FER",
    "FLORISSANT": "FLO",
    "GRAVOIS": "GRA",
    "HADLEY": "HAD",
    "HALLSFERRY": "WH",
    "JEFFERSON": "JEF",
    "LAFAYETTE": "LAF",
    "LEMAY": "LEM",
    "LEWISCLARK": "LC",
    "MARYLANDHEIGHTS": "MHT",
    "MERAMEC": "MER",
    "MIDLAND": "MID",
    "MISSOURIRIVER": "MR",
    "NORMANDY": "NOR",
    "NORTHWEST": "NW",
    "NORWOOD": "NRW",
    "OAKVILLE": "OAK",
    "QUEENY": "QUE",
    "SPANISHLAKE": "SPL",
    "STFERDINAND": "SF",
    "TESSONFERRY": "TSF",
    "UNIVERSITY": "UNV",
}
ST_LOUIS_JURISDICTION_CODES = set(ST_LOUIS_JURISDICTION_ALIASES.values())
BLUE_TOWNSHIP_RE = re.compile(r"^B\s*0*([1-8])\s+([0-9,\s/]+)$", re.I)
TOWNSHIP_BUNDLE_RE = re.compile(
    r"^(PR|PRAIRIE|PRARIE|FO|FORT|BR|BROOKING|WA|WASHINGTON|VB|VAN|SN|SNI|SI)\s+([0-9,\s/]+)$",
    re.I,
)


@dataclass
class VoteAgg:
    dem_votes: float = 0.0
    rep_votes: float = 0.0
    other_votes: float = 0.0
    dem_candidates: Dict[str, float] = field(default_factory=dict)
    rep_candidates: Dict[str, float] = field(default_factory=dict)

    @property
    def total_votes(self) -> float:
        return self.dem_votes + self.rep_votes + self.other_votes

    def add(self, bucket: str, candidate: str, votes: float) -> None:
        amount = float(votes or 0.0)
        if amount == 0:
            return
        if bucket == "dem":
            self.dem_votes += amount
            if candidate:
                self.dem_candidates[candidate] = self.dem_candidates.get(candidate, 0.0) + amount
            return
        if bucket == "rep":
            self.rep_votes += amount
            if candidate:
                self.rep_candidates[candidate] = self.rep_candidates.get(candidate, 0.0) + amount
            return
        self.other_votes += amount

    def add_scaled(self, other: "VoteAgg", share: float) -> None:
        s = float(share or 0.0)
        if s == 0:
            return
        self.dem_votes += other.dem_votes * s
        self.rep_votes += other.rep_votes * s
        self.other_votes += other.other_votes * s
        for name, votes in other.dem_candidates.items():
            self.dem_candidates[name] = self.dem_candidates.get(name, 0.0) + votes * s
        for name, votes in other.rep_candidates.items():
            self.rep_candidates[name] = self.rep_candidates.get(name, 0.0) + votes * s

    def top_candidate(self, bucket: str) -> str:
        mapping = self.dem_candidates if bucket == "dem" else self.rep_candidates
        if not mapping:
            return ""
        return max(mapping.items(), key=lambda kv: (kv[1], kv[0]))[0]

    def to_result(self, dem_candidate: str = "", rep_candidate: str = "") -> Dict[str, object]:
        dem = int(round(self.dem_votes))
        rep = int(round(self.rep_votes))
        other = int(round(self.other_votes))
        total = dem + rep + other
        margin = rep - dem
        margin_pct = (margin / total) * 100 if total else 0.0
        winner = "REP" if margin > 0 else ("DEM" if margin < 0 else "TIE")
        return {
            "dem_votes": dem,
            "rep_votes": rep,
            "other_votes": other,
            "total_votes": total,
            "dem_candidate": self.top_candidate("dem") or dem_candidate,
            "rep_candidate": self.top_candidate("rep") or rep_candidate,
            "margin": margin,
            "margin_pct": round(margin_pct, 6),
            "winner": winner,
            "color": "",
        }


def normalize_office(raw: object) -> str:
    return (
        str(raw or "")
        .strip()
        .upper()
        .replace("U.S.", "US")
        .replace("  ", " ")
    )


def map_contest_type(raw_office: object) -> Optional[str]:
    office = normalize_office(raw_office)
    if office in STATEWIDE_OFFICE_MAP:
        return STATEWIDE_OFFICE_MAP[office]
    for prefix, contest in (
        ("PRESIDENT", "president"),
        ("US SENATE", "us_senate"),
        ("GOVERNOR", "governor"),
        ("LIEUTENANT GOVERNOR", "lieutenant_governor"),
        ("ATTORNEY GENERAL", "attorney_general"),
        ("SECRETARY OF STATE", "secretary_of_state"),
        ("STATE TREASURER", "treasurer"),
        ("STATE AUDITOR", "auditor"),
    ):
        if office.startswith(prefix):
            return contest
    return None


def normalize_party(raw: object) -> str:
    return str(raw or "").strip().upper()


def party_bucket(raw: object) -> str:
    party = normalize_party(raw)
    if party.startswith("DEM"):
        return "dem"
    if party.startswith("REP"):
        return "rep"
    return "other"


def normalize_candidate_key(name: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def infer_party_bucket(raw_party: object, candidate: object) -> str:
    """Use explicit party when present; otherwise known-candidate overrides."""
    bucket = party_bucket(raw_party)
    if bucket in {"dem", "rep"}:
        return bucket
    if normalize_party(raw_party):
        return bucket  # explicit third-party label
    known = KNOWN_CANDIDATE_PARTIES.get(normalize_candidate_key(candidate))
    return known or bucket


def parse_votes(raw: object) -> int:
    text = str(raw or "").strip().replace(",", "")
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def candidate_name(row: Dict[str, str]) -> str:
    direct = str(row.get("candidate") or "").strip()
    if direct:
        return direct
    first = str(row.get("first name") or row.get("first_name") or "").strip()
    last = str(row.get("last name") or row.get("last_name") or "").strip()
    return f"{first} {last}".strip()


def normalize_district_num(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    digits = "".join(ch for ch in text if ch.isdigit())
    return str(int(digits)) if digits else text.upper()


def compact_token(value: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def normalize_alias_token(value: object) -> str:
    token = str(value or "").strip().upper()
    if not token:
        return ""
    for word in ("PRECINCT", "PCT", "WARD", "DISTRICT", "TOWNSHIP", "BOX", "VOTING", "LOCATION"):
        token = token.replace(word, " ")
    token = re.sub(r"[-_.]+", " ", token)
    token = re.sub(r"\s+", " ", token).strip()
    return token


def election_county_key(raw: object) -> str:
    """Keep Kansas City distinct for matching; do not roll into Jackson here."""
    county = (
        str(raw or "")
        .replace("\u00a0", " ")
        .strip()
        .upper()
        .replace(".", "")
    )
    county = re.sub(r"\s+", " ", county)
    county = re.sub(r"\s+COUNTY$", "", county)
    if county in {"KANSAS CITY", "KANSAS CITY BOARD", "KC", "KCMO"}:
        return "KANSAS CITY"
    if county in {"DE KALB", "DEKALB"}:
        return "DEKALB"
    return county


def is_non_geographic(raw_code: object, raw_precinct: object) -> bool:
    blob = f"{raw_code or ''} {raw_precinct or ''}".strip()
    return (not blob) or bool(NON_GEO_RE.search(blob))


def expand_jackson_township_aliases(raw: object) -> Set[str]:
    """
    Map Jackson County Election Board labels onto Census NAME forms across decades.

    VTD20-style:
      'PR 30,31'       -> PRAIRIE 30
      'B4 05,08,10'    -> BLUE 04-05
    VTD10-style:
      'B1 01'          -> BLUE SUB 1 NO. 1
      'PR 13,14'       -> PRAIRIE NO. 13
      'SN 10' / 'SI..' -> SNI-A-BAR NO. 10
      'BR 01'          -> BROOKING NO. 1
      'FO 05,09'       -> FORT OSAGE NO. 5
      'WA 01'          -> WASHINGTON NO. 1
      'VB 12,15'       -> VAN BUREN NO. 12
    """
    out: Set[str] = set()
    text = re.sub(r"\s+", " ", str(raw or "").strip().upper())
    if not text:
        return out

    def add_no_forms(prefix: str, number: str) -> None:
        try:
            n = int(number)
        except ValueError:
            return
        bare = str(n)
        padded2 = f"{n:02d}"
        for label in (bare, padded2):
            out.add(f"{prefix} {label}")
            out.add(f"{prefix}{label}")
            out.add(f"{prefix}-{label}")
            out.add(f"{prefix} NO. {label}")
            out.add(f"{prefix} NO {label}")
            out.add(f"{prefix} NO. {n}")
            out.add(f"{prefix} NO {n}")

    def add_blue_forms(ward: int, precinct: int) -> None:
        out.add(f"B{ward} {precinct:02d}")
        out.add(f"B{ward} {precinct}")
        out.add(f"BLUE {ward:02d}-{precinct:02d}")
        out.add(f"BLUE {ward}-{precinct}")
        out.add(f"BLUE {ward:02d}-{precinct}")
        out.add(f"BLUE {ward}-{precinct:02d}")
        out.add(f"BLUE{ward:02d}{precinct:02d}")
        # 2010 Census VTD naming for Blue township subdivisions.
        out.add(f"BLUE SUB {ward} NO. {precinct}")
        out.add(f"BLUE SUB {ward} NO. {precinct:02d}")
        out.add(f"BLUE SUB {ward} NO {precinct}")
        out.add(f"BLUE SUB {ward} NO {precinct:02d}")
        out.add(f"BLUE SUB {ward:02d} NO. {precinct}")
        out.add(f"BLUE SUB {ward:02d} NO. {precinct:02d}")

    blue = BLUE_TOWNSHIP_RE.match(text)
    if blue:
        ward = int(blue.group(1))
        for part in re.split(r"[,/]+", blue.group(2)):
            digits = re.sub(r"[^0-9]", "", part)
            if not digits:
                continue
            add_blue_forms(ward, int(digits))
        return out

    town = TOWNSHIP_BUNDLE_RE.match(text)
    if town:
        key = town.group(1).upper()
        prefixes = JACKSON_TOWNSHIP_ALIASES.get(key, [key])
        for part in re.split(r"[,/]+", town.group(2)):
            digits = re.sub(r"[^0-9]", "", part)
            if not digits:
                continue
            for prefix in prefixes:
                add_no_forms(prefix, digits)
        return out

    # Already-expanded / census-like forms.
    m = re.match(
        r"^(PRAIRIE|PRARIE|SNI-A-BAR|SNI A BAR|FORT OSAGE|BROOKING(?: NO\.?)?|VAN BUREN|WASHINGTON|BLUE(?: SUB \d+)?)(?:\s+NO\.?)?[\s\-]+0*(\d{1,2}[A-Z]?)$",
        text,
    )
    if m:
        prefix = m.group(1).replace(" NO.", "").replace(" NO", "").strip()
        num = re.sub(r"[^0-9]", "", m.group(2))
        if num:
            add_no_forms(prefix, num)
            blue_sub = re.match(r"^BLUE SUB (\d+)$", prefix)
            if blue_sub:
                add_blue_forms(int(blue_sub.group(1)), int(num))
    return out


def expand_census_name_aliases(raw_name: object) -> Set[str]:
    """
    Index Census VTD names (esp. 2010 Blue Sub / Prairie No. bundles) back to
    election-board shorthand so 'B1 01' can hit 'BLUE SUB 1 NO. 1'.
    """
    out: Set[str] = set()
    text = re.sub(r"\s+", " ", str(raw_name or "").strip().upper())
    if not text:
        return out
    out.add(text)

    # BLUE SUB {ward} NO. {list}
    blue = re.match(r"^BLUE SUB\s+(\d+)\s+NO\.?\s+(.+)$", text)
    if blue:
        ward = int(blue.group(1))
        for token in re.split(r"[,&]+", blue.group(2)):
            token = token.strip()
            if not token or token in {"&"}:
                continue
            digits = re.sub(r"[^0-9]", "", token)
            if not digits:
                continue
            pct = int(digits)
            out.update(expand_jackson_township_aliases(f"B{ward} {pct:02d}"))
            out.add(f"BLUE SUB {ward} NO. {pct}")
            out.add(f"BLUE SUB {ward} NO. {token}")
        return out

    # {TOWNSHIP} NO. {list}
    town = re.match(
        r"^(PRAIRIE|PRARIE|SNI-A-BAR|SNI A BAR|FORT OSAGE|BROOKING|VAN BUREN|WASHINGTON)\s+NO\.?\s+(.+)$",
        text,
    )
    if town:
        prefix = town.group(1)
        abbr = {
            "PRAIRIE": "PR",
            "PRARIE": "PR",
            "SNI-A-BAR": "SN",
            "SNI A BAR": "SN",
            "FORT OSAGE": "FO",
            "BROOKING": "BR",
            "VAN BUREN": "VB",
            "WASHINGTON": "WA",
        }.get(prefix, "")
        for token in re.split(r"[,&]+", town.group(2)):
            token = token.strip()
            if not token or token in {"&"}:
                continue
            digits = re.sub(r"[^0-9]", "", token)
            if not digits:
                continue
            pct = int(digits)
            out.update(expand_jackson_township_aliases(f"{abbr} {pct:02d}" if abbr else f"{prefix} {pct}"))
            out.add(f"{prefix} NO. {pct}")
            out.add(f"{prefix} NO. {token}")
        return out

    # VTD20-style BLUE 04-05 / PRAIRIE 30
    out.update(expand_jackson_township_aliases(text))
    return out


def expand_kc_ward_precinct_aliases(raw: object) -> Set[str]:
    """
    Map OpenElections KC labels like 'W16-P7,13,14,16' onto Census NAME20 forms
    such as 'KC 1607', 'KC1607', 'W16 P7'.
    """
    out: Set[str] = set()
    text = str(raw or "").strip().upper()
    if not text:
        return out

    def add_pair(ward: str, precinct: str) -> None:
        try:
            w = int(ward)
            p = int(precinct)
        except ValueError:
            return
        code = f"{w}{p:02d}" if p < 100 else f"{w}{p}"
        out.add(f"KC {code}")
        out.add(f"KC{code}")
        out.add(f"KC {w}{p}")
        out.add(f"W{w} P{p}")
        out.add(f"W{w}-P{p}")
        out.add(f"WARD {w} PCT {p}")
        out.add(f"KC WD{w} PCT{w}{p:02d}" if p < 100 else f"KC WD{w} PCT{w}{p}")
        out.add(code)
        out.add(f"{w}{p:02d}" if p < 100 else f"{w}{p}")

    for match in WARD_PRECINCT_RE.finditer(text):
        add_pair(match.group(1), match.group(2))

    for match in WARD_BUNDLE_RE.finditer(text):
        ward = match.group(1)
        for part in re.split(r"[,/]+", match.group(2)):
            digits = re.sub(r"[^0-9]", "", part)
            if digits:
                add_pair(ward, digits)

    # Also accept already-normalized KC codes embedded in the label.
    for match in re.finditer(r"\bKC\s*0*(\d{3,4})\b", text):
        code = match.group(1)
        out.add(f"KC {code}")
        out.add(f"KC{code}")
        out.add(code)

    return out


def strip_election_sequence_prefix(raw: object) -> Set[str]:
    """Remove source-row sequence ids such as ``0001`` / ``0101``."""
    text = re.sub(r"\s+", " ", str(raw or "").strip().upper())
    out: Set[str] = set()
    if not text:
        return out
    for pattern in (r"^0*\d{1,4}\s+(.+)$", r"^0*\d{3,4}(W\s+.+)$"):
        match = re.match(pattern, text)
        if match and str(match.group(1) or "").strip():
            out.add(str(match.group(1)).strip())
    return out


def jackson_township_component_labels(raw: object) -> Set[str]:
    """Return one canonical source label per Jackson County bundle component."""
    out: Set[str] = set()
    texts = {re.sub(r"\s+", " ", str(raw or "").strip().upper())}
    texts.update(strip_election_sequence_prefix(raw))
    for text in texts:
        blue = BLUE_TOWNSHIP_RE.match(text)
        if blue:
            ward = int(blue.group(1))
            for part in re.split(r"[,/]+", blue.group(2)):
                digits = re.sub(r"[^0-9]", "", part)
                if digits:
                    out.add(f"B{ward} {int(digits)}")
            continue
        town = TOWNSHIP_BUNDLE_RE.match(text)
        if town:
            key = town.group(1).upper()
            for part in re.split(r"[,/]+", town.group(2)):
                digits = re.sub(r"[^0-9]", "", part)
                if digits:
                    out.add(f"{key} {int(digits)}")
    return out


def kc_ward_precinct_component_labels(raw: object) -> Set[str]:
    """Return one canonical source label per Kansas City ward/precinct component."""
    out: Set[str] = set()
    texts = {re.sub(r"\s+", " ", str(raw or "").strip().upper())}
    texts.update(strip_election_sequence_prefix(raw))
    for text in texts:
        for match in WARD_PRECINCT_RE.finditer(text):
            out.add(f"W{int(match.group(1))} P{int(match.group(2))}")
        for match in WARD_BUNDLE_RE.finditer(text):
            ward = int(match.group(1))
            for part in re.split(r"[,/]+", match.group(2)):
                digits = re.sub(r"[^0-9]", "", part)
                if digits:
                    out.add(f"W{ward} P{int(digits)}")
    return out


def expand_st_louis_precinct_aliases(raw: object) -> Set[str]:
    """Expand St. Louis bundles (``AP1,2``) into stable component aliases."""
    text = re.sub(r"\s+", " ", str(raw or "").strip().upper())
    if not text:
        return set()
    for stripped in strip_election_sequence_prefix(text):
        text = stripped

    # Standardize spelled-out VTD00 jurisdiction names before parsing numbers.
    compact = re.sub(r"[^A-Z0-9,&/ ]", "", text)
    for long_name, code in sorted(
        ST_LOUIS_JURISDICTION_ALIASES.items(), key=lambda item: -len(item[0])
    ):
        compact = compact.replace(long_name, f" {code} ")
    compact = re.sub(r"\s+", " ", compact).strip()

    code_alt = "|".join(sorted(ST_LOUIS_JURISDICTION_CODES, key=lambda x: -len(x)))
    group_re = re.compile(
        rf"(?:^|\s)({code_alt})\s*([0-9][0-9A-Z,\s&/]*)"
        rf"(?=(?:\s+(?:{code_alt})\s*[0-9])|$)"
    )
    out: Set[str] = set()
    for match in group_re.finditer(compact):
        code = match.group(1)
        numbers = re.findall(r"\d+[A-Z]?", match.group(2))
        for number in numbers:
            num_match = re.match(r"0*(\d+)([A-Z]?)$", number)
            if not num_match:
                continue
            base = str(int(num_match.group(1)))
            suffix = num_match.group(2)
            for digits in (base, base.zfill(2), base.zfill(3)):
                out.add(f"{code}{digits}{suffix}")
                out.add(f"{code} {digits}{suffix}")
    return out


def alias_candidates(*values: object) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []

    def add(token: object) -> None:
        t = str(token or "").strip().upper()
        if not t or t in seen:
            return
        seen.add(t)
        out.append(t)
        compact = compact_token(t)
        if compact and compact not in seen:
            seen.add(compact)
            out.append(compact)
        norm = normalize_alias_token(t)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
            norm_compact = compact_token(norm)
            if norm_compact and norm_compact not in seen:
                seen.add(norm_compact)
                out.append(norm_compact)

    for value in values:
        text = str(value or "").strip().upper()
        if not text:
            continue
        add(text)
        for stripped in strip_election_sequence_prefix(text):
            add(stripped)
        for piece in re.split(r"[/,|;]+", text):
            add(piece.strip())
        for alias in expand_kc_ward_precinct_aliases(text):
            add(alias)
        for alias in expand_jackson_township_aliases(text):
            add(alias)
        for alias in expand_st_louis_precinct_aliases(text):
            add(alias)

        alpha_numeric = re.fullmatch(r"([A-Z]+)0*(\d+)([A-Z]?)", compact_token(text))
        if alpha_numeric:
            prefix, number, suffix = alpha_numeric.groups()
            bare = str(int(number))
            for digits in (bare, bare.zfill(2), bare.zfill(3)):
                add(f"{prefix}{digits}{suffix}")
    return out


def normalize_weights(mapping: Dict[str, float]) -> Dict[str, float]:
    total = sum(v for v in mapping.values() if v and v > 0)
    if total <= 0:
        return {}
    return {k: (v / total) for k, v in mapping.items() if v and v > 0}


def crosswalk_by_era(scope: str) -> Dict[str, Path]:
    return {
        "vtd20": CROSSWALK_DIR / f"jackson_vtd20_to_2022_{scope}_from_tabblocks.csv",
        "vtd10": CROSSWALK_DIR / f"jackson_vtd10_to_2022_{scope}_from_nhgis.csv",
        "vtd00": CROSSWALK_DIR / f"jackson_vtd00_to_2022_{scope}_from_nhgis.csv",
    }


def era_for_year(year: int) -> str:
    """
    Pick precinct geography decade for election matching / crosswalk weights.
      >= 2020 -> VTD20 + tabblock20
      2012-2019 -> VTD10 + NHGIS(tabblock10)
      <= 2011 -> VTD00 + NHGIS(tabblock00)
    """
    y = int(year)
    if y >= 2020:
        return "vtd20"
    if y >= 2012:
        return "vtd10"
    return "vtd00"


def resolve_era_inputs(
    year: int,
    scope: str,
    crosswalk_override: Optional[Path] = None,
) -> Tuple[str, Path, Path]:
    era = era_for_year(year)
    paths = crosswalk_by_era(scope)
    # Only honor an explicit override for the matching decade; otherwise pick by year.
    if crosswalk_override and crosswalk_override.exists() and era == "vtd20":
        crosswalk = crosswalk_override
    else:
        crosswalk = paths[era]
    vtd_geojson = VTD_GEOJSON_BY_ERA[era]
    if not crosswalk.exists():
        # Fall back toward newer decades if historical NHGIS outputs are missing.
        for fallback in ("vtd20", "vtd10", "vtd00"):
            candidate = paths[fallback]
            if candidate.exists():
                print(
                    f"Warning: missing {crosswalk.name} for {year}; "
                    f"falling back to {candidate.name}"
                )
                return fallback, candidate, VTD_GEOJSON_BY_ERA[fallback]
        raise SystemExit(
            f"Missing crosswalk for {year} ({era}, scope={scope}). "
            "Run: python scripts/build_jackson_sd_crosswalks_from_tabblocks.py "
            f"--scope {scope} --districts all --with-nhgis"
        )
    if not vtd_geojson.exists():
        raise SystemExit(f"Missing VTD geojson for {era}: {vtd_geojson}")
    return era, crosswalk, vtd_geojson


def parse_years(raw: str) -> List[int]:
    years: List[int] = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        years.append(int(part))
    return years or list(DEFAULT_YEARS)


def parse_districts(raw: str) -> Optional[Set[str]]:
    text = str(raw or "").strip()
    if not text or text.lower() in {"all", "*"}:
        return None
    out = {normalize_district_num(p) for p in text.split(",") if normalize_district_num(p)}
    return out or None


def find_precinct_csv(year: int) -> Path:
    prefix = str(year)
    matches = sorted(DATA_DIR.glob(f"{prefix}????__mo__general__precinct.csv"))
    if not matches:
        raise SystemExit(f"Missing precinct CSV for {year} under {DATA_DIR}")
    return matches[0]


def load_crosswalk(path: Path, district_filter: Optional[Set[str]]) -> Dict[str, Dict[str, float]]:
    if not path.exists():
        raise SystemExit(
            f"Missing {path}. Run scripts/build_jackson_sd_crosswalks_from_tabblocks.py first."
        )
    by_precinct: Dict[str, Dict[str, float]] = defaultdict(dict)
    all_districts: Set[str] = set()
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            precinct = str(row.get("precinct_key") or "").strip().upper()
            district = normalize_district_num(row.get("district_num"))
            if not precinct or not district:
                continue
            try:
                weight = float(row.get("area_weight") or 0.0)
            except ValueError:
                weight = 0.0
            if weight <= 0:
                continue
            # Always keep full district shares for allocation. Filtering to SD-08/11
            # happens only when writing results, otherwise unmatched fallback dumps
            # the entire Jackson/KC vote into those two seats.
            by_precinct[precinct][district] = by_precinct[precinct].get(district, 0.0) + weight
            all_districts.add(district)

    out: Dict[str, Dict[str, float]] = {}
    for precinct, weights in by_precinct.items():
        normalized = normalize_weights(weights)
        if normalized:
            out[precinct] = normalized
    print(
        f"Loaded crosswalk: {len(out):,} Jackson VTDs -> districts "
        f"{sorted(all_districts, key=lambda x: int(x) if x.isdigit() else x)}"
        + (f" (emit filter: {sorted(district_filter)})" if district_filter else "")
    )
    return out


def load_jackson_matcher(vtd_path: Path) -> Dict[str, Dict[str, object]]:
    """
    Build alias indexes keyed by election jurisdiction:
      JACKSON      -> non-KC VTDs (plus all Jackson as fallback)
      KANSAS CITY  -> KC-tagged VTDs only
    """
    if not vtd_path.exists():
        raise SystemExit(f"Missing {vtd_path}")

    payload = json.loads(vtd_path.read_text(encoding="utf-8"))
    jackson_all: Dict[str, object] = {
        "alias_to_norms": defaultdict(set),
        "features": [],
        "kc_norms": set(),
        "non_kc_norms": set(),
    }
    kansas_city: Dict[str, object] = {
        "alias_to_norms": defaultdict(set),
        "features": [],
        "kc_norms": set(),
        "non_kc_norms": set(),
    }

    for feature in payload.get("features") or []:
        props = feature.get("properties") or {}
        county = str(props.get("county_nam") or props.get("COUNTYNAME") or "").strip().upper()
        county = re.sub(r"\s+COUNTY$", "", county)
        if county != "JACKSON":
            continue
        prec_id = str(props.get("VTDST20") or props.get("prec_id") or "").strip().upper()
        precinct_norm = str(
            props.get("precinct_norm")
            or (f"JACKSON - {prec_id}" if prec_id else "")
        ).strip().upper()
        if not precinct_norm:
            continue

        name20 = str(
            props.get("NAME20")
            or props.get("NAME10")
            or props.get("NAME00")
            or props.get("NAME")
            or ""
        ).strip().upper()
        namelsad = str(
            props.get("NAMELSAD20")
            or props.get("NAMELSAD10")
            or props.get("NAMELSAD")
            or ""
        ).strip().upper()
        is_kc = False
        blob = f"{name20} {namelsad} {precinct_norm}"
        if "KANSAS CITY" in blob or name20.startswith("KC") or " KC" in f" {name20}":
            is_kc = True
        if re.search(r"\bKC\d", compact_token(name20)):
            is_kc = True

        aliases = set(alias_candidates(prec_id, name20, namelsad, precinct_norm, props.get("precinct_name")))
        for alias in expand_kc_ward_precinct_aliases(name20):
            aliases.update(alias_candidates(alias))
        for alias in expand_jackson_township_aliases(name20):
            aliases.update(alias_candidates(alias))
        for alias in expand_census_name_aliases(name20):
            aliases.update(alias_candidates(alias))
        # Also index BLUE 04-05 style census names directly.
        if name20.startswith("BLUE "):
            aliases.update(alias_candidates(name20.replace("-", " "), name20.replace(" ", "")))

        feature_row = {
            "precinct_norm": precinct_norm,
            "name_aliases": {normalize_alias_token(a) for a in aliases if normalize_alias_token(a)},
            "is_kansas_city": is_kc,
        }

        targets = [jackson_all]
        if is_kc:
            targets.append(kansas_city)
            jackson_all["kc_norms"].add(precinct_norm)
            kansas_city["kc_norms"].add(precinct_norm)
        else:
            jackson_all["non_kc_norms"].add(precinct_norm)

        for target in targets:
            target["features"].append(feature_row)
            alias_map = target["alias_to_norms"]
            for alias in aliases:
                alias_map[alias].add(precinct_norm)

    print(
        f"Matcher: Jackson VTDs={len(jackson_all['features'])} "
        f"(KC={len(jackson_all['kc_norms'])}, non-KC={len(jackson_all['non_kc_norms'])})"
    )
    return {"JACKSON": jackson_all, "KANSAS CITY": kansas_city}


def match_precinct_norms(
    raw_code: str,
    raw_precinct: str,
    election_county: str,
    matcher: Dict[str, Dict[str, object]],
) -> List[str]:
    county_info = matcher.get(election_county) or matcher.get("JACKSON")
    if not county_info:
        return []

    tokens = alias_candidates(raw_code, raw_precinct, f"{raw_code} {raw_precinct}".strip())
    alias_map: Dict[str, Set[str]] = county_info["alias_to_norms"]  # type: ignore[assignment]

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
        return sorted(best)

    # Prefer KC norms when matching under KANSAS CITY even if alias is ambiguous.
    if election_county == "KANSAS CITY" and best:
        kc_only = {n for n in best if n in county_info["kc_norms"]}
        if kc_only:
            return sorted(kc_only)
    return sorted(best) if best else []


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


def county_area_fallback(crosswalk: Dict[str, Dict[str, float]], election_county: str, matcher: Dict[str, Dict[str, object]]) -> Dict[str, float]:
    weights: Dict[str, float] = defaultdict(float)
    county_info = matcher.get(election_county) or matcher.get("JACKSON") or {}
    if election_county == "KANSAS CITY":
        norms = set(county_info.get("kc_norms") or [])
    else:
        norms = set(county_info.get("non_kc_norms") or []) or set(crosswalk.keys())

    for precinct_norm in norms:
        for district, w in (crosswalk.get(precinct_norm) or {}).items():
            weights[district] += float(w)
    return normalize_weights(dict(weights))


def read_year_precinct_aggs(
    year: int,
    contest_filter: Optional[Set[str]],
    scope: str = "state_senate",
) -> Tuple[Dict[Tuple[str, str, str, str], VoteAgg], Dict[Tuple[str, str], Dict[str, float]], Dict[Tuple[str, str, str], Dict[str, float]]]:
    """
    Returns:
      precinct_aggs[(contest, election_county, raw_code, raw_precinct)]
      candidate_totals[(contest, bucket)][candidate] = votes
      same-year district label votes by (county, code, precinct) -> {district: votes}
    """
    csv_path = find_precinct_csv(year)
    print(f"Reading {csv_path.name} ...")
    precinct_aggs: Dict[Tuple[str, str, str, str], VoteAgg] = defaultdict(VoteAgg)
    candidate_totals: Dict[Tuple[str, str], Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    label_district_votes: Dict[Tuple[str, str, str], Dict[str, float]] = defaultdict(lambda: defaultdict(float))

    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = 0
        for row in reader:
            rows += 1
            county = election_county_key(row.get("county") or row.get("county_name"))
            if county not in {"JACKSON", "KANSAS CITY"}:
                continue
            votes = parse_votes(row.get("votes"))
            if votes <= 0:
                continue
            raw_code = str(row.get("precinct_code") or row.get("precinct_id") or row.get("ward") or "").strip().upper()
            raw_precinct = str(row.get("precinct") or row.get("precinct_name") or "").strip().upper()
            office = row.get("office") or ""
            contest = map_contest_type(office)
            candidate = candidate_name(row)
            bucket = infer_party_bucket(
                row.get("party") or row.get("party_simplified") or "",
                candidate,
            )

            if contest and (contest_filter is None or contest in contest_filter):
                key = (contest, county, raw_code, raw_precinct)
                precinct_aggs[key].add(bucket, candidate, votes)
                if bucket in {"dem", "rep"} and candidate:
                    candidate_totals[(contest, bucket)][candidate] += votes

            # Same-year district labels (useful for absentee buckets).
            office_norm = normalize_office(office)
            want_label = False
            if scope == "state_senate" and (
                office_norm.startswith("STATE SENATE") or office_norm.startswith("STATE SENATOR")
            ):
                want_label = True
            elif scope == "state_house" and (
                office_norm.startswith("STATE HOUSE") or office_norm.startswith("STATE REPRESENTATIVE")
            ):
                want_label = True
            if want_label:
                district = normalize_district_num(row.get("district"))
                if district:
                    label_district_votes[(county, raw_code, raw_precinct)][district] += votes

        print(f"  kept {len(precinct_aggs):,} Jackson/KC precinct-contest buckets from {rows:,} rows")
    return precinct_aggs, candidate_totals, label_district_votes


def top_candidate(candidate_totals: Dict[Tuple[str, str], Dict[str, float]], contest: str, bucket: str) -> str:
    mapping = candidate_totals.get((contest, bucket)) or {}
    if not mapping:
        return ""
    return max(mapping.items(), key=lambda kv: (kv[1], kv[0]))[0]


def aggregate_year(
    year: int,
    contests: Optional[Set[str]],
    districts: Optional[Set[str]],
    crosswalk: Dict[str, Dict[str, float]],
    matcher: Dict[str, Dict[str, object]],
    era: str = "vtd20",
    scope: str = "state_senate",
) -> Dict[str, Dict[str, object]]:
    precinct_aggs, candidate_totals, label_votes = read_year_precinct_aggs(year, contests, scope=scope)

    # Group by contest.
    by_contest: Dict[str, List[Tuple[str, str, str, VoteAgg]]] = defaultdict(list)
    for (contest, county, raw_code, raw_precinct), agg in precinct_aggs.items():
        if agg.total_votes <= 0:
            continue
        by_contest[contest].append((county, raw_code, raw_precinct, agg))

    area_fallback_cache = {
        "JACKSON": county_area_fallback(crosswalk, "JACKSON", matcher),
        "KANSAS CITY": county_area_fallback(crosswalk, "KANSAS CITY", matcher),
    }

    outputs: Dict[str, Dict[str, object]] = {}
    for contest, rows in sorted(by_contest.items()):
        dem_candidate = top_candidate(candidate_totals, contest, "dem")
        rep_candidate = top_candidate(candidate_totals, contest, "rep")
        district_agg: Dict[str, VoteAgg] = defaultdict(VoteAgg)
        county_matched_weights: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
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
            if county == "KANSAS CITY":
                kc_votes += precinct_total
            else:
                jackson_votes += precinct_total

            matched = match_precinct_norms(raw_code, raw_precinct, county, matcher)
            weights = district_weights_for_matches(matched, crosswalk)
            if weights:
                allocated_votes += precinct_total
                direct_votes += precinct_total
                if county == "KANSAS CITY":
                    kc_direct += precinct_total
                else:
                    jackson_direct += precinct_total
                for district, share in weights.items():
                    district_agg[district].add_scaled(agg, share)
                    county_matched_weights[county][district] += precinct_total * share
                continue
            unmatched.append((county, raw_code, raw_precinct, agg))

        for county, raw_code, raw_precinct, agg in unmatched:
            # Same-year district labels on absentee-like buckets.
            label_weights = normalize_weights(dict(label_votes.get((county, raw_code, raw_precinct)) or {}))
            if districts is not None:
                label_weights = normalize_weights(
                    {d: w for d, w in label_weights.items() if d in districts}
                )
            if label_weights and (year >= 2022 or is_non_geographic(raw_code, raw_precinct)):
                allocated_votes += agg.total_votes
                direct_votes += agg.total_votes
                for district, share in label_weights.items():
                    district_agg[district].add_scaled(agg, share)
                    county_matched_weights[county][district] += agg.total_votes * share
                continue

            preferred = normalize_weights(dict(county_matched_weights.get(county) or {}))
            area = area_fallback_cache.get(county) or {}
            # Blend matched vote-weighted shares with area fallback.
            county_total = kc_votes if county == "KANSAS CITY" else jackson_votes
            county_direct = kc_direct if county == "KANSAS CITY" else jackson_direct
            alpha = (county_direct / county_total) if county_total > 0 else 0.0
            blended: Dict[str, float] = {}
            keys = set(preferred) | set(area)
            for k in keys:
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
        if districts is not None:
            results = {d: results[d] for d in results if d in districts}

        coverage = (allocated_votes / total_votes * 100.0) if total_votes else 0.0
        direct_coverage = (direct_votes / total_votes * 100.0) if total_votes else 0.0
        payload = {
            "meta": {
                "scope": scope,
                "contest_type": contest,
                "year": int(year),
                "district_count": len(results),
                "match_coverage_pct": round(coverage, 6),
                "direct_match_coverage_pct": round(direct_coverage, 6),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "source_method": f"jackson_{era}_tabblock_nhgis_kc_split",
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


def patch_overlap_files(
    year: int,
    contest: str,
    results: Dict[str, Dict[str, object]],
    scope: str = "state_senate",
) -> Optional[Path]:
    overlap_path = DISTRICT_DIR / f"{scope}_{contest}_{year}_overlap.json"
    if not overlap_path.exists():
        return None
    payload = json.loads(overlap_path.read_text(encoding="utf-8"))
    general = payload.setdefault("general", {})
    dest = general.setdefault("results", {})
    for district, row in results.items():
        dest[str(district)] = row
    meta = payload.setdefault("meta", {})
    meta["jackson_tabblock_patch"] = {
        "patched_districts": sorted(results.keys(), key=lambda x: int(x) if str(x).isdigit() else x),
        "patched_at": datetime.now(timezone.utc).isoformat(),
        "source_method": "jackson_tabblock_crosswalk_kc_split",
    }
    write_payload(overlap_path, payload)
    return overlap_path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--scope",
        default="state_senate",
        choices=list(VALID_SCOPES),
        help="Legislative chamber to aggregate (default: state_senate).",
    )
    ap.add_argument("--years", default=",".join(str(y) for y in DEFAULT_YEARS))
    ap.add_argument(
        "--contests",
        default="",
        help="Comma-separated contest types (default: all statewide contests found).",
    )
    ap.add_argument(
        "--districts",
        default=None,
        help=(
            "Districts to emit. Defaults: senate 8,11; house all Jackson-touching. "
            "Use 'all' for every district in the crosswalk."
        ),
    )
    ap.add_argument(
        "--crosswalk",
        type=Path,
        default=None,
        help="Optional override for the VTD20 crosswalk only. Historical years still use NHGIS era files.",
    )
    ap.add_argument(
        "--patch-overlap",
        action="store_true",
        help="Also patch district results into existing {scope}_*_overlap.json files.",
    )
    args = ap.parse_args()

    scope = str(args.scope).strip().lower()
    years = parse_years(args.years)
    contests = {c.strip().lower() for c in str(args.contests).split(",") if c.strip()} or None
    if args.districts is None:
        default = DEFAULT_DISTRICTS_BY_SCOPE[scope]
        districts = set(default) if default is not None else None
    else:
        districts = parse_districts(args.districts)
    crosswalk_override = Path(args.crosswalk) if args.crosswalk else None

    DISTRICT_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    matcher_cache: Dict[str, Dict[str, Dict[str, object]]] = {}
    crosswalk_cache: Dict[str, Dict[str, Dict[str, float]]] = {}

    for year in years:
        era, crosswalk_path, vtd_path = resolve_era_inputs(year, scope, crosswalk_override)
        print(f"\n=== {year} {scope} using {era} ({crosswalk_path.name}) ===")
        if era not in crosswalk_cache:
            crosswalk_cache[era] = load_crosswalk(crosswalk_path, districts)
        if era not in matcher_cache:
            matcher_cache[era] = load_jackson_matcher(vtd_path)

        payloads = aggregate_year(
            year,
            contests,
            districts,
            crosswalk_cache[era],
            matcher_cache[era],
            era=era,
            scope=scope,
        )
        for contest, payload in payloads.items():
            out_path = DISTRICT_DIR / f"{scope}_{contest}_{year}_jackson_tabblocks.json"
            write_payload(out_path, payload)
            written += 1
            print(f"Wrote {out_path}")
            if args.patch_overlap:
                patched = patch_overlap_files(
                    year, contest, payload["general"]["results"], scope=scope  # type: ignore[index]
                )
                if patched:
                    print(f"Patched {patched}")
                else:
                    print(f"No overlap file to patch for {contest} {year}")

    print(f"Done. Wrote {written} aggregate file(s).")


if __name__ == "__main__":
    main()
