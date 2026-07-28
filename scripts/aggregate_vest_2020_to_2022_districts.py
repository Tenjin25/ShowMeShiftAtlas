#!/usr/bin/env python
"""
Allocate VEST precinct results onto Missouri district geometries.

Why
---
OpenElections' precinct CSVs often omit countywide / precinct absentee.
VEST (Harvard Dataverse) includes absentee allocated to precincts. Area-weighted
overlay onto current district lines recovers certified-scale totals.

Writes **main** district_contest JSON by default (not *_overlap.json):
  - state_senate / state_house / congressional -> Data/district_contests/
  - congressional_2026                        -> Data/district_contests_2026/

VEST sources (Harvard Dataverse)
--------------------------------
  2016  doi:10.7910/DVN/NH5S2I  mo_2016.zip
  2018  doi:10.7910/DVN/UBKYRU  mo_2018.zip
  2020  doi:10.7910/DVN/K7760H  mo_2020.zip

Usage
-----
  python scripts/aggregate_vest_2020_to_2022_districts.py --year 2020 --scope state_senate --districts all
  python scripts/aggregate_vest_2020_to_2022_districts.py --year 2018 --scope state_house --exclude-jackson --merge-existing
  python scripts/aggregate_vest_2020_to_2022_districts.py --year 2016 --scope congressional --districts all
  python scripts/aggregate_vest_2020_to_2022_districts.py --download-only
"""

from __future__ import annotations

import argparse
import csv
import json
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

import geopandas as gpd


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "Data"
DISTRICT_DIR_2022 = DATA_DIR / "district_contests"
DISTRICT_DIR_2026 = DATA_DIR / "district_contests_2026"
CROSSWALK_DIR = DATA_DIR / "crosswalks"

DATAVERSE_API = "https://dataverse.harvard.edu/api/access/datafile/{file_id}"

# year -> Dataverse metadata + local paths + contest column maps
# Contest tuple: (dem_col, rep_col, other_cols, dem_name, rep_name)
VEST_YEARS: Dict[int, Dict[str, object]] = {
    2016: {
        "doi": "10.7910/DVN/NH5S2I",
        "file_id": 5007730,
        "remote_name": "mo_2016.zip",
        "zip": DATA_DIR / "mo_2016_vest.zip",
        "extract_dir": DATA_DIR / "_extract_mo_2016_vest",
        "shp_name": "mo_2016.shp",
        "contests": {
            "president": (
                "G16PREDCLI",
                "G16PRERTRU",
                ("G16PRELJOH", "G16PREGSTE", "G16PRECCAS"),
                "Hillary Rodham Clinton",
                "Donald J. Trump",
            ),
            "us_senate": (
                "G16USSDKAN",
                "G16USSRBLU",
                ("G16USSLDIN", "G16USSGMCF", "G16USSCRYM"),
                "Jason Kander",
                "Roy Blunt",
            ),
            "governor": (
                "G16GOVDKOS",
                "G16GOVRGRE",
                ("G16GOVLSPR", "G16GOVGFIT", "G16GOVITUR"),
                "Chris Koster",
                "Eric Greitens",
            ),
            "lieutenant_governor": (
                "G16LTGDCAR",
                "G16LTGRPAR",
                ("G16LTGLHED", "G16LTGGLEA"),
                "Russ Carnahan",
                "Mike Parson",
            ),
            "attorney_general": (
                "G16ATGDHEN",
                "G16ATGRHAW",
                (),
                "Teresa Hensley",
                "Josh Hawley",
            ),
            "secretary_of_state": (
                "G16SOSDSMI",
                "G16SOSRASH",
                ("G16SOSLMOR",),
                "Robin Smith",
                "John R. (Jay) Ashcroft",
            ),
            "treasurer": (
                "G16TREDBAK",
                "G16TRERSCH",
                ("G16TRELOTO", "G16TREGHEX"),
                "Judy Baker",
                "Eric Schmitt",
            ),
        },
    },
    2018: {
        "doi": "10.7910/DVN/UBKYRU",
        "file_id": 5007791,
        "remote_name": "mo_2018.zip",
        "zip": DATA_DIR / "mo_2018_vest.zip",
        "extract_dir": DATA_DIR / "_extract_mo_2018_vest",
        "shp_name": "mo_2018.shp",
        "contests": {
            "us_senate": (
                "G18USSDMCC",
                "G18USSRHAW",
                ("G18USSLCAM", "G18USSGCRA", "G18USSIODE"),
                "Claire McCaskill",
                "Josh Hawley",
            ),
            "auditor": (
                "G18AUDDGAL",
                "G18AUDRMCD",
                ("G18AUDLOTO", "G18AUDGFIT", "G18AUDCLUE"),
                "Nicole Galloway",
                "Saundra McDowell",
            ),
        },
    },
    2020: {
        "doi": "10.7910/DVN/K7760H",
        "file_id": 5007850,
        "remote_name": "mo_2020.zip",
        "zip": DATA_DIR / "mo_2020_vest.zip",
        "extract_dir": DATA_DIR / "_extract_mo_2020_vest",
        "shp_name": "mo_2020.shp",
        "contests": {
            "president": (
                "G20PREDBID",
                "G20PRERTRU",
                ("G20PRELJOR", "G20PREGHAW", "G20PRECBLA"),
                "Joseph R. Biden",
                "Donald J. Trump",
            ),
            "governor": (
                "G20GOVDGAL",
                "G20GOVRPAR",
                ("G20GOVLCOM", "G20GOVGBAU"),
                "Nicole Galloway",
                "Mike Parson",
            ),
            "lieutenant_governor": (
                "G20LTGDCAN",
                "G20LTGRKEH",
                ("G20LTGLSLA", "G20LTGGDRA"),
                "Alissia Canady",
                "Mike Kehoe",
            ),
            "attorney_general": (
                "G20ATGDFIN",
                "G20ATGRSCH",
                ("G20ATGLBAB",),
                "Rich Finneran",
                "Eric Schmitt",
            ),
            "secretary_of_state": (
                "G20SOSDFAL",
                "G20SOSRASH",
                ("G20SOSLFRE", "G20SOSGLEH", "G20SOSCVEN"),
                "Yinka Faleti",
                "John R. (Jay) Ashcroft",
            ),
            "treasurer": (
                "G20TREDENG",
                "G20TRERFIT",
                ("G20TRELKAS", "G20TREGCIV"),
                "Vicki Lorenz Englund",
                "Scott Fitzpatrick",
            ),
        },
    },
}

SCOPE_CONFIG = {
    "state_senate": {
        "geojson": DATA_DIR / "mo_state_senate_districts_2022.geojson",
        "out_dir": DISTRICT_DIR_2022,
        "file_prefix": "state_senate",
        "default_districts": None,  # all
        "district_fields": ("district", "SLDUST", "GEOID"),
    },
    "state_house": {
        "geojson": DATA_DIR / "mo_state_house_districts_2022.geojson",
        "out_dir": DISTRICT_DIR_2022,
        "file_prefix": "state_house",
        "default_districts": None,
        "district_fields": ("district", "SLDLST", "GEOID"),
    },
    "congressional": {
        "geojson": DATA_DIR / "mo_congressional_districts_2022.geojson",
        "out_dir": DISTRICT_DIR_2022,
        "file_prefix": "congressional",
        "default_districts": None,
        "district_fields": ("district", "DISTRICT", "LABEL", "GEOID", "CD116FP", "CD118FP"),
    },
    "congressional_2026": {
        "geojson": DATA_DIR / "mo_congressional_districts_2026.geojson",
        "out_dir": DISTRICT_DIR_2026,
        "file_prefix": "congressional",
        "default_districts": None,
        "district_fields": ("district", "DISTRICT", "LABEL", "GEOID"),
    },
}

JACKSON_HOUSE_CROSSWALK = CROSSWALK_DIR / "jackson_vtd20_to_2022_state_house_from_tabblocks.csv"
MIN_JACKSON_BLOCKS = 50.0


def vest_source_label(year: int) -> str:
    cfg = VEST_YEARS[year]
    return f"Harvard Dataverse doi:{cfg['doi']} {cfg['remote_name']}"


def source_method_label(year: int) -> str:
    return f"vest_{year}_area_weighted_overlay"


def download_vest_zip(year: int, force: bool = False) -> Path:
    cfg = VEST_YEARS[year]
    zip_path: Path = cfg["zip"]  # type: ignore[assignment]
    if zip_path.exists() and not force:
        return zip_path
    url = DATAVERSE_API.format(file_id=cfg["file_id"])
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading VEST {year} from {url}")
    print(f"  -> {zip_path}")
    urllib.request.urlretrieve(url, zip_path)
    print(f"  Done ({zip_path.stat().st_size:,} bytes)")
    return zip_path


def ensure_vest_shapefile(year: int, force_download: bool = False) -> Path:
    if year not in VEST_YEARS:
        raise SystemExit(f"Unsupported VEST year {year}; have {sorted(VEST_YEARS)}")
    cfg = VEST_YEARS[year]
    extract_dir: Path = cfg["extract_dir"]  # type: ignore[assignment]
    shp = extract_dir / str(cfg["shp_name"])
    if shp.exists() and not force_download:
        return shp
    zip_path = download_vest_zip(year, force=force_download)
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)
    if not shp.exists():
        raise SystemExit(f"Extracted {zip_path} but did not find {shp}")
    return shp


def normalize_district_num(raw: object) -> str:
    text = str(raw or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    return str(int(digits)) if digits else ""


def parse_districts(raw: str, scope: str) -> Optional[Set[str]]:
    text = str(raw or "").strip()
    if not text:
        default = SCOPE_CONFIG[scope]["default_districts"]
        return set(default) if default is not None else None
    if text.lower() in {"all", "*"}:
        return None
    out = {normalize_district_num(p) for p in text.split(",") if normalize_district_num(p)}
    return out or None


def jackson_primary_house_districts(min_blocks: float = MIN_JACKSON_BLOCKS) -> Set[str]:
    weights: Dict[str, float] = {}
    if not JACKSON_HOUSE_CROSSWALK.exists():
        return set()
    with JACKSON_HOUSE_CROSSWALK.open("r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            d = normalize_district_num(row.get("district_num"))
            if not d:
                continue
            try:
                w = float(row.get("block_weight") or 0.0)
            except ValueError:
                w = 0.0
            weights[d] = weights.get(d, 0.0) + w
    return {d for d, w in weights.items() if w >= min_blocks}


def load_districts(
    scope: str,
    district_filter: Optional[Set[str]],
    exclude: Optional[Set[str]] = None,
) -> gpd.GeoDataFrame:
    cfg = SCOPE_CONFIG[scope]
    path = cfg["geojson"]
    if not path.exists():
        raise SystemExit(f"Missing district geojson: {path}")
    gdf = gpd.read_file(path)
    district_series = None
    for field in cfg["district_fields"]:
        if field in gdf.columns:
            district_series = gdf[field].map(normalize_district_num)
            break
    if district_series is None:
        raise SystemExit(f"No district id field in {path}; looked for {cfg['district_fields']}")
    gdf = gdf.copy()
    gdf["district"] = district_series
    gdf = gdf[gdf["district"] != ""].copy()
    if district_filter is not None:
        gdf = gdf[gdf["district"].isin(district_filter)].copy()
    if exclude:
        gdf = gdf[~gdf["district"].isin(exclude)].copy()
    if gdf.empty:
        raise SystemExit(f"No districts selected for scope={scope}")
    return gdf[["district", "geometry"]].dissolve(by="district", as_index=False)


def contest_map(year: int) -> Dict[str, Tuple]:
    return VEST_YEARS[year]["contests"]  # type: ignore[return-value]


def allocate(
    vest: gpd.GeoDataFrame,
    districts: gpd.GeoDataFrame,
    year: int,
    contests: Iterable[str],
) -> Dict[str, Dict[str, Dict[str, object]]]:
    cmap = contest_map(year)
    contest_list = [c for c in contests if c in cmap]
    if not contest_list:
        raise SystemExit(f"No supported contests in {list(contests)}; have {sorted(cmap)}")

    needed_cols: List[str] = []
    for contest in contest_list:
        dem, rep, others, _dn, _rn = cmap[contest]
        needed_cols.extend([dem, rep, *others])
    needed_cols = sorted(set(needed_cols))
    missing = [c for c in needed_cols if c not in vest.columns]
    if missing:
        raise SystemExit(f"VEST {year} shapefile missing columns: {missing}")

    vest_p = vest.to_crs(5070).copy()
    dist_p = districts.to_crs(5070).copy()
    vest_p["vest_id"] = vest_p.index.astype(str)
    vest_p["vest_area"] = vest_p.geometry.area.clip(lower=0.0)

    keep = ["vest_id", "vest_area", "geometry", *needed_cols]
    inter = gpd.overlay(
        vest_p[keep],
        dist_p[["district", "geometry"]],
        how="intersection",
        keep_geom_type=False,
    )
    inter["piece_area"] = inter.geometry.area.clip(lower=0.0)
    inter["share"] = (inter["piece_area"] / inter["vest_area"].clip(lower=1e-9)).clip(upper=1.0)

    outputs: Dict[str, Dict[str, Dict[str, object]]] = {}
    for contest in contest_list:
        dem_col, rep_col, other_cols, dem_name, rep_name = cmap[contest]
        dem = (inter[dem_col] * inter["share"]).groupby(inter["district"]).sum()
        rep = (inter[rep_col] * inter["share"]).groupby(inter["district"]).sum()
        other = None
        for col in other_cols:
            part = (inter[col] * inter["share"]).groupby(inter["district"]).sum()
            other = part if other is None else (other + part)
        if other is None:
            other = dem * 0.0

        results: Dict[str, Dict[str, object]] = {}
        for district in sorted(set(dem.index) | set(rep.index), key=lambda x: int(x) if str(x).isdigit() else x):
            d_votes = float(dem.get(district, 0.0) or 0.0)
            r_votes = float(rep.get(district, 0.0) or 0.0)
            o_votes = float(other.get(district, 0.0) or 0.0)
            dem_i = int(round(d_votes))
            rep_i = int(round(r_votes))
            other_i = int(round(o_votes))
            total = dem_i + rep_i + other_i
            margin = rep_i - dem_i
            margin_pct = (margin / total * 100.0) if total else 0.0
            winner = "REP" if margin > 0 else ("DEM" if margin < 0 else "TIE")
            results[str(district)] = {
                "dem_votes": dem_i,
                "rep_votes": rep_i,
                "other_votes": other_i,
                "total_votes": total,
                "dem_candidate": dem_name,
                "rep_candidate": rep_name,
                "margin": margin,
                "margin_pct": round(margin_pct, 6),
                "winner": winner,
                "color": "",
            }
        outputs[contest] = results
    return outputs


def write_payload(path: Path, payload: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def apply_meta(
    meta: Dict[str, object],
    scope: str,
    contest: str,
    year: int,
    district_count: int,
    updated: Optional[List[str]] = None,
    mode: str = "replace",
) -> None:
    meta.update(
        {
            "scope": "congressional" if scope.startswith("congressional") else scope,
            "contest_type": contest,
            "year": year,
            "district_count": district_count,
            "match_coverage_pct": 100.0,
            "direct_match_coverage_pct": 100.0,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_method": source_method_label(year),
            "vest_source": vest_source_label(year),
        }
    )
    key = f"vest_{year}_main"
    updated_sorted = sorted(
        updated or [],
        key=lambda x: int(x) if str(x).isdigit() else x,
    )
    meta[key] = {
        "updated_districts": updated_sorted,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
    }


def merge_into_existing(
    existing_path: Path,
    vest_results: Dict[str, Dict[str, object]],
    scope: str,
    contest: str,
    year: int,
) -> Dict[str, object]:
    """Replace selected districts in an existing main JSON; keep the rest."""
    if existing_path.exists():
        payload = json.loads(existing_path.read_text(encoding="utf-8"))
    else:
        payload = {"meta": {}, "general": {"results": {}}}
    dest = payload.setdefault("general", {}).setdefault("results", {})
    for district, row in vest_results.items():
        dest[str(district)] = row
    apply_meta(
        payload.setdefault("meta", {}),
        scope,
        contest,
        year,
        district_count=len(dest),
        updated=list(vest_results.keys()),
        mode="merge_existing",
    )
    return payload


def build_full_payload(
    scope: str,
    contest: str,
    year: int,
    results: Dict[str, Dict[str, object]],
) -> Dict[str, object]:
    districts = sorted(results.keys(), key=lambda x: int(x) if x.isdigit() else x)
    payload: Dict[str, object] = {
        "meta": {
            "districts": districts,
        },
        "general": {"results": results},
    }
    apply_meta(
        payload["meta"],  # type: ignore[arg-type]
        scope,
        contest,
        year,
        district_count=len(results),
        updated=districts,
        mode="replace",
    )
    return payload


def patch_overlap_sidecar(
    overlap_path: Path,
    vest_results: Dict[str, Dict[str, object]],
    year: int,
) -> None:
    """Mark overlap JSON as superseded by VEST for the updated districts."""
    if not overlap_path.exists():
        return
    payload = json.loads(overlap_path.read_text(encoding="utf-8"))
    meta = payload.setdefault("meta", {})
    key = f"vest_{year}_patch"
    meta[key] = {
        "patched_districts": sorted(
            vest_results.keys(), key=lambda x: int(x) if str(x).isdigit() else x
        ),
        "patched_at": datetime.now(timezone.utc).isoformat(),
        "source_method": source_method_label(year),
        "vest_source": vest_source_label(year),
    }
    # Prefer VEST as the authoritative note when we fully replace.
    if meta.get("source_method") != source_method_label(year):
        meta["superseded_by"] = key
    write_payload(overlap_path, payload)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--year", type=int, default=2020, choices=sorted(VEST_YEARS))
    ap.add_argument("--scope", default="state_senate", choices=sorted(SCOPE_CONFIG))
    ap.add_argument("--districts", default="all", help="Comma list, or 'all' (default).")
    ap.add_argument(
        "--exclude-jackson",
        action="store_true",
        help="For state_house: skip Jackson-primary house districts (keep existing rows if merging).",
    )
    ap.add_argument(
        "--merge-existing",
        action="store_true",
        help="Merge VEST rows into existing main JSON instead of replacing the whole file.",
    )
    ap.add_argument(
        "--contests",
        default="",
        help="Comma-separated contest types. Default: all contests available for --year.",
    )
    ap.add_argument(
        "--write-vest-sidecar",
        action="store_true",
        help="Deprecated: VEST results are written to main *.json only. Sidecars are not used by the app.",
    )
    ap.add_argument(
        "--patch-overlap",
        action="store_true",
        help="Annotate matching *_overlap.json as superseded by this VEST run.",
    )
    ap.add_argument(
        "--download-only",
        action="store_true",
        help="Ensure VEST zip+shapefile for --year (or all years if --year omitted via loop).",
    )
    ap.add_argument(
        "--force-download",
        action="store_true",
        help="Re-download from Harvard Dataverse even if local zip exists.",
    )
    args = ap.parse_args()

    year = int(args.year)
    if args.download_only:
        shp = ensure_vest_shapefile(year, force_download=args.force_download)
        print(f"Ready: {shp}")
        return

    scope = args.scope
    cfg = SCOPE_CONFIG[scope]
    cmap = contest_map(year)
    if args.contests.strip():
        contests = [c.strip().lower() for c in str(args.contests).split(",") if c.strip()]
    else:
        contests = list(cmap.keys())

    district_filter = parse_districts(args.districts, scope)
    exclude: Set[str] = set()
    if args.exclude_jackson:
        if scope != "state_house":
            raise SystemExit("--exclude-jackson only applies to --scope state_house")
        exclude = jackson_primary_house_districts()
        print(f"Excluding Jackson-primary house districts: {sorted(exclude, key=lambda x: int(x))}")

    shp = ensure_vest_shapefile(year, force_download=args.force_download)
    print(f"Loading VEST {shp} ...")
    vest = gpd.read_file(shp)
    districts = load_districts(scope, district_filter, exclude=exclude or None)
    selected = sorted(districts["district"].tolist(), key=lambda x: int(x) if x.isdigit() else x)
    print(f"Allocating {len(contests)} contest(s) onto {len(selected)} {scope} district(s) (year={year})")
    print(f"  source: {vest_source_label(year)}")

    allocated = allocate(vest, districts, year, contests)
    out_dir: Path = cfg["out_dir"]
    prefix: str = cfg["file_prefix"]
    out_dir.mkdir(parents=True, exist_ok=True)

    for contest, results in allocated.items():
        main_path = out_dir / f"{prefix}_{contest}_{year}.json"
        if args.merge_existing or args.exclude_jackson:
            payload = merge_into_existing(main_path, results, scope, contest, year)
        else:
            payload = build_full_payload(scope, contest, year, results)

        write_payload(main_path, payload)
        print(f"Wrote main {main_path} ({payload['meta']['district_count']} districts)")

        sample = sorted(results.keys(), key=lambda x: int(x) if x.isdigit() else x)[:8]
        for d in sample:
            row = results[d]
            tot = row["total_votes"] or 1
            print(
                f"  {prefix} {d}: D={row['dem_votes']} R={row['rep_votes']} "
                f"tot={row['total_votes']} D%={100 * row['dem_votes'] / tot:.1f} "
                f"{row['winner']} R+={row['margin_pct']:.1f}"
            )
        if len(results) > len(sample):
            print(f"  ... +{len(results) - len(sample)} more districts")

        if args.write_vest_sidecar:
            print("  Note: --write-vest-sidecar ignored; VEST results are main *.json only")

        if args.patch_overlap:
            overlap = out_dir / f"{prefix}_{contest}_{year}_overlap.json"
            patch_overlap_sidecar(overlap, results, year)
            if overlap.exists():
                print(f"  Patched overlap note {overlap}")


if __name__ == "__main__":
    main()
