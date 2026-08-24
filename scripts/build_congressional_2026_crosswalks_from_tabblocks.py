#!/usr/bin/env python
"""
Build statewide precinct -> district crosswalks via tabulation blocks
(+ optional NHGIS decade chaining), with the Jackson / Kansas City
election-county fix. Supports the enacted 2022 congressional and legislative
plans as well as the 2026 congressional plan.

Decade chain (--with-nhgis)
---------------------------
  tabblock20 + VTD20  -> precinct_to_{plan}_from_tabblocks.csv
  + NHGIS 2010->2020 + tabblock10 + VTD10
                      -> vtd10_to_{plan}_from_nhgis.csv
  + NHGIS 2000->2010 + tabblock00 + VTD00
                      -> vtd00_to_{plan}_from_nhgis.csv

Where {plan} is selected with --plan (cd2026 by default).

Usage
-----
  python scripts/build_congressional_2026_crosswalks_from_tabblocks.py
  python scripts/build_congressional_2026_crosswalks_from_tabblocks.py --with-nhgis
  python scripts/build_congressional_2026_crosswalks_from_tabblocks.py --with-nhgis --skip-block-dump
  python scripts/build_congressional_2026_crosswalks_from_tabblocks.py --plan 2022 --with-nhgis
  python scripts/build_congressional_2026_crosswalks_from_tabblocks.py --plan state-house-2022 --with-nhgis
"""

from __future__ import annotations

import argparse
import csv
import math
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "Data"
CROSSWALK_DIR = DATA_DIR / "crosswalks"
SOURCE_DATA_DIR = DATA_DIR

JACKSON_COUNTYFP = "095"
STATEFP = "29"

TABBLOCK20_SHP = DATA_DIR / "_extract_tabblock20" / "tl_2020_29_tabblock20.shp"
TABBLOCK20_ZIP = DATA_DIR / "tl_2020_29_tabblock20.zip"
VTD20_GEOJSON = DATA_DIR / "mo_vtd20_precincts.geojson"
PLAN_CONFIG = {
    "2022": {
        "district_geojson": DATA_DIR / "mo_congressional_districts_2022.geojson",
        "district_label": "CD 2022 (CD118)",
        "out_block": CROSSWALK_DIR / "tabblock20_to_cd118.csv",
        "out_vtd20": CROSSWALK_DIR / "precinct_to_cd118_from_tabblocks.csv",
        "out_vtd10": CROSSWALK_DIR / "vtd10_to_cd118_from_nhgis.csv",
        "out_vtd00": CROSSWALK_DIR / "vtd00_to_cd118_from_nhgis.csv",
    },
    "2026": {
        "district_geojson": DATA_DIR / "mo_congressional_districts_2026.geojson",
        "district_label": "CD 2026",
        "out_block": CROSSWALK_DIR / "tabblock20_to_cd2026.csv",
        "out_vtd20": CROSSWALK_DIR / "precinct_to_cd2026_from_tabblocks.csv",
        "out_vtd10": CROSSWALK_DIR / "vtd10_to_cd2026_from_nhgis.csv",
        "out_vtd00": CROSSWALK_DIR / "vtd00_to_cd2026_from_nhgis.csv",
    },
    "state-house-2022": {
        "district_geojson": DATA_DIR / "mo_state_house_districts_2022.geojson",
        "district_label": "2022 State House",
        "out_block": CROSSWALK_DIR / "tabblock20_to_2022_state_house.csv",
        "out_vtd20": CROSSWALK_DIR / "precinct_to_2022_state_house_from_tabblocks.csv",
        "out_vtd10": CROSSWALK_DIR / "vtd10_to_2022_state_house_from_nhgis.csv",
        "out_vtd00": CROSSWALK_DIR / "vtd00_to_2022_state_house_from_nhgis.csv",
    },
    "state-senate-2022": {
        "district_geojson": DATA_DIR / "mo_state_senate_districts_2022.geojson",
        "district_label": "2022 State Senate",
        "out_block": CROSSWALK_DIR / "tabblock20_to_2022_state_senate.csv",
        "out_vtd20": CROSSWALK_DIR / "precinct_to_2022_state_senate_from_tabblocks.csv",
        "out_vtd10": CROSSWALK_DIR / "vtd10_to_2022_state_senate_from_nhgis.csv",
        "out_vtd00": CROSSWALK_DIR / "vtd00_to_2022_state_senate_from_nhgis.csv",
    },
}

CD_GEOJSON = PLAN_CONFIG["2026"]["district_geojson"]
DISTRICT_LABEL = PLAN_CONFIG["2026"]["district_label"]
OUT_BLOCK = PLAN_CONFIG["2026"]["out_block"]
OUT_VTD20 = PLAN_CONFIG["2026"]["out_vtd20"]
OUT_VTD10 = PLAN_CONFIG["2026"]["out_vtd10"]
OUT_VTD00 = PLAN_CONFIG["2026"]["out_vtd00"]

TABBLOCK10_ZIP = DATA_DIR / "tl_2020_29_tabblock10.zip"
TABBLOCK10_SHP = DATA_DIR / "_extract_tabblock10" / "tl_2020_29_tabblock10.shp"
TABBLOCK00_STATE_ZIP = DATA_DIR / "tiger2008" / "tl_2008_29_tabblock00.zip"

VTD10_GEOJSON = DATA_DIR / "mo_vtd10_precincts.geojson"
VTD00_GEOJSON = DATA_DIR / "mo_vtd00_precincts.geojson"

NHGIS_2010_2020_CSV = DATA_DIR / "_extract_nhgis_blk2010_blk2020_29" / "nhgis_blk2010_blk2020_29.csv"
NHGIS_2010_2020_ZIP = DATA_DIR / "nhgis_blk2010_blk2020_29.zip"
NHGIS_2000_2010_CSV = DATA_DIR / "_extract_nhgis_blk2000_blk2010_29" / "nhgis_blk2000_blk2010_29.csv"
NHGIS_2000_2010_ZIP = DATA_DIR / "nhgis_blk2000_blk2010_29.zip"


def normalize_district_num(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    digits = "".join(ch for ch in text if ch.isdigit())
    if digits:
        return str(int(digits))
    return text.upper()


def normalize_precinct_key(raw: object) -> str:
    return str(raw or "").replace("\u00a0", " ").strip().upper()


def normalize_county_name(raw: object) -> str:
    county = (
        str(raw or "")
        .replace("\u00a0", " ")
        .strip()
        .upper()
        .replace(".", "")
    )
    county = " ".join(county.split())
    county = county.removesuffix(" COUNTY").strip()
    if county in {"DE KALB", "DEKALB"}:
        return "DEKALB"
    if county in {"ST LOUIS", "SAINT LOUIS"}:
        return "ST LOUIS"
    if county in {"ST CHARLES", "SAINT CHARLES"}:
        return "ST CHARLES"
    if county in {"ST CLAIR", "SAINT CLAIR"}:
        return "ST CLAIR"
    if county in {"ST FRANCOIS", "SAINT FRANCOIS"}:
        return "ST FRANCOIS"
    return county


def is_kansas_city_vtd_name(name20: object, namelsad20: object = "", precinct_norm: object = "") -> bool:
    blob = " ".join(
        str(v or "").strip().upper()
        for v in (name20, namelsad20, precinct_norm)
    ).strip()
    if not blob:
        return False
    if "KANSAS CITY" in blob:
        return True
    tokens = blob.replace("-", " ").split()
    if tokens and tokens[0] in {"KC", "KCMO"}:
        return True
    if any(tok.startswith("KC") and any(ch.isdigit() for ch in tok) for tok in tokens):
        return True
    return False


def election_county_for_vtd(is_kc: bool, county_name: str) -> str:
    if county_name == "JACKSON":
        return "KANSAS CITY" if is_kc else "JACKSON"
    return county_name or "UNKNOWN"


def election_county_aliases(is_kc: bool, county_name: str) -> str:
    if county_name != "JACKSON":
        return county_name or "UNKNOWN"
    if is_kc:
        return "KANSAS CITY|JACKSON"
    return "JACKSON|KANSAS CITY"


def block_geoid_to_gisjoin(geoid: object) -> str:
    s = str(geoid or "").strip().replace(".0", "")
    s = s.zfill(15)
    return f"G{s[0:2]}0{s[2:5]}0{s[5:11]}{s[11:15]}"


def ensure_tabblock20_shp() -> Path:
    if TABBLOCK20_SHP.exists():
        return TABBLOCK20_SHP
    if not TABBLOCK20_ZIP.exists():
        raise SystemExit(
            f"Missing {TABBLOCK20_SHP} and {TABBLOCK20_ZIP}. "
            "Extract tl_2020_29_tabblock20.zip into Data/_extract_tabblock20/."
        )
    extract_dir = DATA_DIR / "_extract_tabblock20"
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(TABBLOCK20_ZIP, "r") as zf:
        zf.extractall(extract_dir)
    if not TABBLOCK20_SHP.exists():
        raise SystemExit(f"Extracted {TABBLOCK20_ZIP} but still missing {TABBLOCK20_SHP}")
    return TABBLOCK20_SHP


def load_block_points() -> "gpd.GeoDataFrame":
    import geopandas as gpd
    import pyogrio
    from shapely.geometry import Point

    shp = ensure_tabblock20_shp()
    print(f"Reading statewide tabblock20 points from {shp} ...")
    df = pyogrio.read_dataframe(
        str(shp),
        columns=["GEOID20", "COUNTYFP20", "INTPTLAT20", "INTPTLON20"],
        read_geometry=False,
    )
    if df.empty:
        raise SystemExit("No tabblocks found.")

    df["GEOID20"] = df["GEOID20"].astype(str).str.strip().str.zfill(15)
    df["COUNTYFP20"] = df["COUNTYFP20"].astype(str).str.strip().str.zfill(3)
    df["blk2020gj"] = df["GEOID20"].map(block_geoid_to_gisjoin)
    df["INTPTLAT20"] = pd.to_numeric(df["INTPTLAT20"], errors="coerce")
    df["INTPTLON20"] = pd.to_numeric(df["INTPTLON20"], errors="coerce")
    df = df.dropna(subset=["INTPTLAT20", "INTPTLON20"]).copy()
    geometry = [Point(xy) for xy in zip(df["INTPTLON20"].tolist(), df["INTPTLAT20"].tolist())]
    gdf = gpd.GeoDataFrame(df, geometry=geometry, crs="EPSG:4326")
    print(f"  {len(gdf):,} blocks")
    return gdf


def load_vtds(path: Path, era_label: str = "VTD20") -> "gpd.GeoDataFrame":
    import geopandas as gpd

    if not path.exists():
        raise SystemExit(f"Missing {path}")
    print(f"Reading {era_label} precincts from {path} ...")
    gdf = gpd.read_file(path)
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    else:
        gdf = gdf.to_crs("EPSG:4326")

    if "precinct_norm" in gdf.columns:
        gdf["precinct_key"] = gdf["precinct_norm"].map(normalize_precinct_key)
    else:
        vtd_col = next((c for c in ("VTDST20", "VTDST10", "VTDST00", "VTDST") if c in gdf.columns), None)
        county_col = next(
            (c for c in ("county_nam", "COUNTYNAME", "NAME") if c in gdf.columns),
            None,
        )
        if not vtd_col:
            raise SystemExit(f"{path} missing precinct identity fields")
        if county_col:
            gdf["precinct_key"] = (
                gdf[county_col].map(normalize_county_name)
                + " - "
                + gdf[vtd_col].astype(str).str.strip().str.upper()
            ).map(normalize_precinct_key)
        else:
            raise SystemExit(f"{path} missing county + VTD fields")

    county_fips_col = next(
        (c for c in ("COUNTYFP20", "COUNTYFP10", "COUNTYFP00", "COUNTYFP") if c in gdf.columns),
        None,
    )
    vtd_col = next(
        (c for c in ("VTDST20", "VTDST10", "VTDST00", "VTDST", "prec_id") if c in gdf.columns),
        None,
    )
    if county_fips_col and vtd_col:
        county_fips = gdf[county_fips_col].astype(str).str.replace(r"\D", "", regex=True).str.zfill(3)
        city_mask = county_fips.eq("510")
        gdf.loc[city_mask, "precinct_key"] = (
            "ST. LOUIS CITY - "
            + gdf.loc[city_mask, vtd_col].astype(str).str.strip().str.upper()
        )

    county_col = next(
        (c for c in ("county_nam", "COUNTYNAME", "COUNTYFP20", "COUNTYFP10", "COUNTYFP") if c in gdf.columns),
        None,
    )
    if county_col is None or not str(county_col).upper().startswith("COUNTYFP"):
        key_county = gdf["precinct_key"].map(
            lambda k: normalize_county_name(str(k).split(" - ", 1)[0] if " - " in str(k) else "")
        )
        gdf["county_name"] = key_county
        if county_col is not None:
            named = gdf[county_col].map(normalize_county_name)
            gdf.loc[~gdf["county_name"].astype(bool), "county_name"] = named[~gdf["county_name"].astype(bool)]
    else:
        # FIPS-only fallback; prefer precinct_key county when present.
        key_county = gdf["precinct_key"].map(
            lambda k: normalize_county_name(str(k).split(" - ", 1)[0] if " - " in str(k) else "")
        )
        gdf["county_name"] = key_county

    if county_fips_col:
        county_fips = gdf[county_fips_col].astype(str).str.replace(r"\D", "", regex=True).str.zfill(3)
        gdf.loc[county_fips.eq("510"), "county_name"] = "ST LOUIS CITY"

    name20 = (
        gdf["NAME20"]
        if "NAME20" in gdf.columns
        else (gdf["NAME10"] if "NAME10" in gdf.columns else (gdf["NAME00"] if "NAME00" in gdf.columns else (gdf["NAME"] if "NAME" in gdf.columns else "")))
    )
    namelsad = (
        gdf["NAMELSAD20"]
        if "NAMELSAD20" in gdf.columns
        else (gdf["NAMELSAD10"] if "NAMELSAD10" in gdf.columns else (gdf["NAMELSAD"] if "NAMELSAD" in gdf.columns else ""))
    )
    is_jackson = gdf["county_name"].eq("JACKSON")
    gdf["is_kansas_city"] = [
        bool(is_jackson.iloc[i]) and is_kansas_city_vtd_name(n, s, p)
        for i, (n, s, p) in enumerate(zip(name20, namelsad, gdf["precinct_key"]))
    ]
    gdf["election_county"] = [
        election_county_for_vtd(bool(kc), str(county))
        for kc, county in zip(gdf["is_kansas_city"], gdf["county_name"])
    ]
    gdf["election_county_aliases"] = [
        election_county_aliases(bool(kc), str(county))
        for kc, county in zip(gdf["is_kansas_city"], gdf["county_name"])
    ]

    gdf = gdf.loc[gdf["precinct_key"].astype(bool)].copy()
    gdf = gdf.drop_duplicates(subset=["precinct_key"], keep="first")
    kc_count = int(gdf["is_kansas_city"].sum())
    print(f"  {len(gdf):,} VTDs ({kc_count:,} Jackson KC-tagged)")
    return gdf[
        [
            "precinct_key",
            "county_name",
            "is_kansas_city",
            "election_county",
            "election_county_aliases",
            "geometry",
        ]
    ]


def load_districts() -> "gpd.GeoDataFrame":
    import geopandas as gpd

    if not CD_GEOJSON.exists():
        raise SystemExit(f"Missing district geography: {CD_GEOJSON}")
    print(f"Reading {DISTRICT_LABEL} districts from {CD_GEOJSON} ...")
    gdf = gpd.read_file(CD_GEOJSON)
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    else:
        gdf = gdf.to_crs("EPSG:4326")

    district_col = next(
        (c for c in ("district", "DISTRICT", "LABEL", "CD118FP", "GEOID") if c in gdf.columns),
        None,
    )
    if district_col is None:
        raise SystemExit(f"{CD_GEOJSON} missing district field")

    gdf["district_num"] = gdf[district_col].map(normalize_district_num)
    gdf = gdf.loc[gdf["district_num"] != ""].copy()
    if gdf.empty:
        raise SystemExit("No districts loaded")
    print(
        "  districts: "
        + ", ".join(sorted(gdf["district_num"].unique(), key=lambda x: int(x) if x.isdigit() else x))
    )
    return gdf[["district_num", "geometry"]]


def spatial_assign_blocks(
    blocks: "gpd.GeoDataFrame",
    polygons: "gpd.GeoDataFrame",
    value_col: str,
    label: str,
) -> pd.Series:
    import geopandas as gpd

    print(f"Spatial join blocks -> {label} ...")
    joined = gpd.sjoin(
        blocks[["geometry"]].copy(),
        polygons[[value_col, "geometry"]],
        how="left",
        predicate="within",
    )
    # Drop duplicate join hits (rare multipolygon overlaps) by keeping first.
    if joined.index.duplicated().any():
        joined = joined[~joined.index.duplicated(keep="first")]

    missing = joined[value_col].isna()
    if missing.any():
        print(f"  {int(missing.sum()):,} blocks missed 'within'; assigning nearest {label}")
        miss_idx = joined.index[missing]
        blocks_proj = blocks.loc[miss_idx, ["geometry"]].to_crs("EPSG:26915")
        polys_proj = polygons[[value_col, "geometry"]].to_crs("EPSG:26915")
        nearest = gpd.sjoin_nearest(
            blocks_proj,
            polys_proj,
            how="left",
            distance_col="_dist",
        )
        if nearest.index.duplicated().any():
            nearest = nearest[~nearest.index.duplicated(keep="first")]
        joined.loc[miss_idx, value_col] = nearest[value_col].values

    assigned = joined[value_col]
    hit = int(assigned.notna().sum())
    print(f"  assigned {hit:,}/{len(blocks):,}")
    return assigned


def write_csv(path: Path, fieldnames: Sequence[str], rows: Iterable[Dict[str, object]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(fieldnames), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def aggregate_vtd_district_weights(
    block_df: pd.DataFrame,
    *,
    precinct_col: str = "precinct_key",
    district_col: str = "district_num",
    weight_col: Optional[str] = None,
) -> List[Dict[str, object]]:
    pair_weight: Dict[Tuple[str, str], float] = defaultdict(float)
    precinct_total: Dict[str, float] = defaultdict(float)
    meta: Dict[str, Dict[str, object]] = {}

    for row in block_df.itertuples(index=False):
        precinct = normalize_precinct_key(getattr(row, precinct_col, ""))
        district = normalize_district_num(getattr(row, district_col, ""))
        if not precinct or not district:
            continue
        weight = 1.0
        if weight_col:
            try:
                weight = float(getattr(row, weight_col) or 0.0)
            except (TypeError, ValueError):
                weight = 0.0
        if not math.isfinite(weight) or weight <= 0:
            continue
        pair_weight[(precinct, district)] += weight
        precinct_total[precinct] += weight
        if precinct not in meta:
            county_name = str(getattr(row, "county_name", "") or "")
            is_kc = bool(getattr(row, "is_kansas_city", False))
            meta[precinct] = {
                "is_kansas_city": is_kc,
                "election_county": str(getattr(row, "election_county", "") or "")
                or election_county_for_vtd(is_kc, county_name),
                "election_county_aliases": str(getattr(row, "election_county_aliases", "") or "")
                or election_county_aliases(is_kc, county_name),
            }

    rows: List[Dict[str, object]] = []
    for (precinct, district), weight in sorted(
        pair_weight.items(),
        key=lambda x: (x[0][0], int(x[0][1]) if x[0][1].isdigit() else x[0][1]),
    ):
        total = precinct_total.get(precinct, 0.0)
        if total <= 0:
            continue
        area_weight = weight / total
        info = meta.get(precinct, {})
        rows.append(
            {
                "precinct_key": precinct,
                "district_num": district,
                "area_weight": f"{area_weight:.10f}",
                "block_weight": f"{weight:.10f}",
                "precinct_block_weight": f"{total:.10f}",
                "is_kansas_city": "1" if info.get("is_kansas_city") else "0",
                "election_county": info.get("election_county") or "",
                "election_county_aliases": info.get("election_county_aliases") or "",
            }
        )
    return rows


def build_crosswalk(*, write_blocks: bool = True) -> pd.DataFrame:
    blocks = load_block_points()
    vtds = load_vtds(VTD20_GEOJSON, "VTD20")
    districts = load_districts()

    blocks = blocks.copy()
    blocks["precinct_key"] = spatial_assign_blocks(blocks, vtds, "precinct_key", "VTD20")
    blocks["district_num"] = spatial_assign_blocks(blocks, districts, "district_num", DISTRICT_LABEL)

    vtd_meta = vtds.drop(columns=["geometry"]).drop_duplicates("precinct_key")
    blocks = blocks.merge(vtd_meta, on="precinct_key", how="left")
    blocks["is_kansas_city"] = blocks["is_kansas_city"].fillna(False).astype(bool)
    blocks["county_name"] = blocks["county_name"].fillna("").astype(str)
    blocks["election_county"] = blocks.apply(
        lambda r: r["election_county"]
        if isinstance(r["election_county"], str) and r["election_county"]
        else election_county_for_vtd(bool(r["is_kansas_city"]), str(r["county_name"] or "")),
        axis=1,
    )
    blocks["election_county_aliases"] = blocks.apply(
        lambda r: r["election_county_aliases"]
        if isinstance(r["election_county_aliases"], str) and r["election_county_aliases"]
        else election_county_aliases(bool(r["is_kansas_city"]), str(r["county_name"] or "")),
        axis=1,
    )

    if write_blocks:
        block_rows = []
        for row in blocks.itertuples(index=False):
            if not row.GEOID20:
                continue
            block_rows.append(
                {
                    "block_geoid20": row.GEOID20,
                    "blk2020gj": row.blk2020gj,
                    "precinct_key": normalize_precinct_key(row.precinct_key),
                    "district_num": normalize_district_num(row.district_num),
                    "is_kansas_city": "1" if bool(getattr(row, "is_kansas_city", False)) else "0",
                    "election_county": str(getattr(row, "election_county", "") or ""),
                    "election_county_aliases": str(getattr(row, "election_county_aliases", "") or ""),
                }
            )
        n = write_csv(
            OUT_BLOCK,
            [
                "block_geoid20",
                "blk2020gj",
                "precinct_key",
                "district_num",
                "is_kansas_city",
                "election_county",
                "election_county_aliases",
            ],
            block_rows,
        )
        print(f"Wrote {OUT_BLOCK} ({n:,} rows)")

    weight_source = blocks.dropna(subset=["precinct_key", "district_num"]).copy()
    vtd_rows = aggregate_vtd_district_weights(weight_source)
    n = write_csv(
        OUT_VTD20,
        [
            "precinct_key",
            "district_num",
            "area_weight",
            "block_weight",
            "precinct_block_weight",
            "is_kansas_city",
            "election_county",
            "election_county_aliases",
        ],
        vtd_rows,
    )
    print(f"Wrote {OUT_VTD20} ({n:,} rows)")
    kc_rows = sum(1 for r in vtd_rows if r["is_kansas_city"] == "1")
    districts_seen = sorted(
        {r["district_num"] for r in vtd_rows},
        key=lambda x: int(x) if str(x).isdigit() else str(x),
    )
    print(f"  precincts: {len({r['precinct_key'] for r in vtd_rows}):,}")
    print(f"  districts: {', '.join(districts_seen)}")
    print(f"  VTD20 rows with KC election alias: {kc_rows:,}")
    return blocks


def load_nhgis_csv(preferred_csv: Path, zip_path: Path, usecols: Sequence[str]) -> pd.DataFrame:
    if preferred_csv.exists():
        return pd.read_csv(preferred_csv, dtype=str, usecols=list(usecols))
    if not zip_path.exists():
        raise SystemExit(f"Missing NHGIS source: {preferred_csv} and {zip_path}")
    with zipfile.ZipFile(zip_path, "r") as zf:
        members = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not members:
            raise SystemExit(f"No CSV inside {zip_path}")
        with zf.open(members[0]) as fh:
            return pd.read_csv(fh, dtype=str, usecols=list(usecols))


def load_era_block_points(era: str) -> "gpd.GeoDataFrame":
    """Load statewide block internal points for a historical decade."""
    import geopandas as gpd
    import pyogrio
    from shapely.geometry import Point

    if era == "vtd10":
        extract_guess = TABBLOCK10_SHP
        zip_path = TABBLOCK10_ZIP
        geoid_field = "GEOID10"
        lat_field = "INTPTLAT10"
        lon_field = "INTPTLON10"
        if extract_guess.exists():
            src = str(extract_guess)
        elif zip_path.exists():
            src = f"zip://{zip_path}"
        else:
            raise SystemExit(f"Missing tabblock10 source ({extract_guess} / {zip_path})")
        print(f"Reading statewide tabblock10 points from {src} ...")
        hdf = pyogrio.read_dataframe(
            src,
            columns=[geoid_field, lat_field, lon_field],
            read_geometry=False,
        )
    elif era == "vtd00":
        county_dir = SOURCE_DATA_DIR / "tiger2008" / "tabblock00_by_county"
        county_zips = sorted(county_dir.glob("tl_2008_29*_tabblock00.zip")) if county_dir.exists() else []
        # Statewide zip in this repo is truncated (no EOCD); prefer county extracts.
        if county_zips:
            print(f"Reading statewide tabblock00 points from {len(county_zips)} county zips ...")
            frames = []
            for i, zip_path in enumerate(county_zips):
                src = f"zip://{zip_path.resolve()}"
                try:
                    info = pyogrio.read_info(src)
                    raw_fields = info.get("fields")
                    if isinstance(raw_fields, (list, tuple, set)):
                        fields = {str(x) for x in raw_fields}
                    else:
                        try:
                            fields = {str(x) for x in list(raw_fields)}
                        except TypeError:
                            fields = set()
                    geoid_field = next((c for c in ("GEOID00", "BLKIDFP00", "GEOID") if c in fields), None)
                    if not geoid_field:
                        continue
                    lat_field = next((c for c in ("INTPTLAT00", "INTPTLAT", "LAT") if c in fields), None)
                    lon_field = next((c for c in ("INTPTLON00", "INTPTLON", "LON") if c in fields), None)
                    if lat_field and lon_field:
                        part = pyogrio.read_dataframe(
                            src,
                            columns=[geoid_field, lat_field, lon_field],
                            read_geometry=False,
                        )
                        part = part.rename(
                            columns={
                                geoid_field: "GEOID00",
                                lat_field: "INTPTLAT00",
                                lon_field: "INTPTLON00",
                            }
                        )
                    else:
                        # 2008 county extracts often omit INTPT*; use polygon reps.
                        part_gdf = pyogrio.read_dataframe(src, columns=[geoid_field])
                        if part_gdf.crs is None:
                            part_gdf = part_gdf.set_crs("EPSG:4326")
                        else:
                            part_gdf = part_gdf.to_crs("EPSG:4326")
                        pts = part_gdf.geometry.representative_point()
                        part = pd.DataFrame(
                            {
                                "GEOID00": part_gdf[geoid_field].astype(str),
                                "INTPTLON00": pts.x,
                                "INTPTLAT00": pts.y,
                            }
                        )
                    frames.append(part)
                except Exception as err:
                    print(f"  skip {zip_path.name}: {err}")
                if (i + 1) % 25 == 0:
                    print(f"  loaded {i + 1}/{len(county_zips)} counties ...")
            if not frames:
                raise SystemExit(f"No readable tabblock00 county zips under {county_dir}")
            hdf = pd.concat(frames, ignore_index=True)
            geoid_field, lat_field, lon_field = "GEOID00", "INTPTLAT00", "INTPTLON00"
        else:
            zip_path = TABBLOCK00_STATE_ZIP
            if not zip_path.exists():
                raise SystemExit(
                    f"Missing tabblock00 sources ({county_dir} and {TABBLOCK00_STATE_ZIP})"
                )
            extract_dir = SOURCE_DATA_DIR / "_extract_tabblock00"
            extract_dir.mkdir(parents=True, exist_ok=True)
            shp_files = list(extract_dir.glob("*.shp")) or list(extract_dir.rglob("*.shp"))
            if not shp_files:
                print(f"Extracting {zip_path} ...")
                with zipfile.ZipFile(zip_path, "r") as zf:
                    zf.extractall(extract_dir)
                shp_files = list(extract_dir.glob("*.shp")) or list(extract_dir.rglob("*.shp"))
            if not shp_files:
                raise SystemExit(f"No .shp found after extracting {zip_path}")
            src = str(shp_files[0])
            print(f"Reading statewide tabblock00 points from {src} ...")
            info = pyogrio.read_info(src)
            raw_fields = info.get("fields")
            if raw_fields is None:
                fields = set()
            elif isinstance(raw_fields, (list, tuple, set)):
                fields = {str(x) for x in raw_fields}
            else:
                try:
                    fields = {str(x) for x in list(raw_fields)}
                except TypeError:
                    fields = set()
            geoid_field = next((c for c in ("GEOID00", "BLKIDFP00", "GEOID") if c in fields), None)
            lat_field = next((c for c in ("INTPTLAT00", "INTPTLAT", "LAT") if c in fields), None)
            lon_field = next((c for c in ("INTPTLON00", "INTPTLON", "LON") if c in fields), None)
            if not geoid_field or not lat_field or not lon_field:
                gdf = gpd.read_file(src)
                if gdf.crs is None:
                    gdf = gdf.set_crs("EPSG:4326")
                else:
                    gdf = gdf.to_crs("EPSG:4326")
                geoid_col = next((c for c in ("GEOID00", "BLKIDFP00", "GEOID") if c in gdf.columns), None)
                if not geoid_col:
                    raise SystemExit(f"tabblock00 missing GEOID-like field in {src}")
                pts = gdf[[geoid_col, "geometry"]].copy()
                pts["geometry"] = pts.geometry.representative_point()
                pts = pts.rename(columns={geoid_col: "GEOID"})
                pts["GEOID"] = pts["GEOID"].astype(str).str.strip().str.zfill(15)
                pts["blk_gj"] = pts["GEOID"].map(block_geoid_to_gisjoin)
                print(f"  {len(pts):,} statewide 2000 blocks")
                return pts

            hdf = pyogrio.read_dataframe(
                src,
                columns=[geoid_field, lat_field, lon_field],
                read_geometry=False,
            )
    else:
        raise SystemExit(f"Unsupported era for block points: {era}")

    hdf[geoid_field] = hdf[geoid_field].astype(str).str.strip().str.zfill(15)
    hdf["blk_gj"] = hdf[geoid_field].map(block_geoid_to_gisjoin)
    hdf[lat_field] = pd.to_numeric(hdf[lat_field], errors="coerce")
    hdf[lon_field] = pd.to_numeric(hdf[lon_field], errors="coerce")
    hdf = hdf.dropna(subset=[lat_field, lon_field]).copy()
    gdf = gpd.GeoDataFrame(
        hdf,
        geometry=[Point(xy) for xy in zip(hdf[lon_field], hdf[lat_field])],
        crs="EPSG:4326",
    )
    print(f"  {len(gdf):,} statewide {era} blocks")
    return gdf


def assign_historical_vtds_via_nhgis(
    blocks_2020: pd.DataFrame,
    *,
    era: str,
    vtd_geojson: Path,
    nhgis_frames: List[Tuple[str, pd.DataFrame]],
    out_path: Path,
) -> None:
    """
    Chain NHGIS block weights onto 2020 block→district assignments, then attach
    historical VTD polygons using that decade's tabblock points.
    """
    if not vtd_geojson.exists():
        print(f"Skip {era}: missing {vtd_geojson}")
        return

    base = blocks_2020.dropna(subset=["GEOID20", "district_num"]).copy()
    base["blk2020gj"] = base["GEOID20"].map(block_geoid_to_gisjoin)
    base = base[["blk2020gj", "district_num"]].drop_duplicates()

    current = base.rename(columns={"blk2020gj": "blk_gj"})
    current["weight"] = 1.0
    current_label = "2020"

    for target_label, frame in nhgis_frames:
        if current_label == "2020" and "blk2020gj" in frame.columns and "blk2010gj" in frame.columns:
            left_on, right_from, right_to = "blk_gj", "blk2020gj", "blk2010gj"
        elif current_label == "2010" and "blk2010gj" in frame.columns and "blk2000gj" in frame.columns:
            left_on, right_from, right_to = "blk_gj", "blk2010gj", "blk2000gj"
        else:
            raise SystemExit(f"Unsupported NHGIS hop {current_label} -> {target_label}")

        print(f"Chaining NHGIS {current_label}->{target_label} ...")
        hop = frame[[right_from, right_to, "weight"]].copy()
        hop["weight"] = pd.to_numeric(hop["weight"], errors="coerce").fillna(0.0)
        hop = hop.loc[hop["weight"] > 0].copy()

        merged = current.merge(hop, left_on=left_on, right_on=right_from, how="inner")
        merged["weight"] = merged["weight_x"] * merged["weight_y"]
        current = (
            merged.groupby([right_to, "district_num"], as_index=False)["weight"]
            .sum()
            .rename(columns={right_to: "blk_gj"})
        )
        current_label = target_label
        print(f"  {len(current):,} {current_label} block-district weight rows")

    vtds = load_vtds(vtd_geojson, era.upper())
    try:
        hgdf = load_era_block_points(era)
    except SystemExit as err:
        print(f"Skip {era}: {err}")
        return

    hgdf = hgdf.copy()
    hgdf["precinct_key"] = spatial_assign_blocks(hgdf, vtds, "precinct_key", era.upper())
    hist = current.merge(
        hgdf[["blk_gj", "precinct_key"]].drop_duplicates("blk_gj"),
        on="blk_gj",
        how="inner",
    )
    hist = hist.merge(
        vtds.drop(columns=["geometry"]).drop_duplicates("precinct_key"),
        on="precinct_key",
        how="left",
    )

    rows = aggregate_vtd_district_weights(hist, weight_col="weight")
    n = write_csv(
        out_path,
        [
            "precinct_key",
            "district_num",
            "area_weight",
            "block_weight",
            "precinct_block_weight",
            "is_kansas_city",
            "election_county",
            "election_county_aliases",
        ],
        rows,
    )
    print(f"Wrote {out_path} ({n:,} rows)")
    kc_rows = sum(1 for r in rows if r["is_kansas_city"] == "1")
    print(f"  {era} KC-tagged rows: {kc_rows:,}")


def configure_plan(plan: str) -> None:
    global CD_GEOJSON, DISTRICT_LABEL, OUT_BLOCK, OUT_VTD20, OUT_VTD10, OUT_VTD00
    config = PLAN_CONFIG[str(plan)]
    CD_GEOJSON = config["district_geojson"]
    DISTRICT_LABEL = str(config["district_label"])
    OUT_BLOCK = config["out_block"]
    OUT_VTD20 = config["out_vtd20"]
    OUT_VTD10 = config["out_vtd10"]
    OUT_VTD00 = config["out_vtd00"]


def configure_block_source(source_data_dir: Path) -> None:
    global SOURCE_DATA_DIR, TABBLOCK20_SHP, TABBLOCK20_ZIP
    global TABBLOCK10_SHP, TABBLOCK10_ZIP, TABBLOCK00_STATE_ZIP
    SOURCE_DATA_DIR = Path(source_data_dir).resolve()
    TABBLOCK20_SHP = SOURCE_DATA_DIR / "_extract_tabblock20" / "tl_2020_29_tabblock20.shp"
    TABBLOCK20_ZIP = SOURCE_DATA_DIR / "tl_2020_29_tabblock20.zip"
    TABBLOCK10_SHP = SOURCE_DATA_DIR / "_extract_tabblock10" / "tl_2020_29_tabblock10.shp"
    TABBLOCK10_ZIP = SOURCE_DATA_DIR / "tl_2020_29_tabblock10.zip"
    TABBLOCK00_STATE_ZIP = SOURCE_DATA_DIR / "tiger2008" / "tl_2008_29_tabblock00.zip"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--plan",
        choices=sorted(PLAN_CONFIG),
        default="2026",
        help="District plan to target (default: 2026 congressional).",
    )
    ap.add_argument(
        "--block-data-dir",
        type=Path,
        default=DATA_DIR,
        help="Data directory containing TIGER tab-block files (default: repository Data).",
    )
    ap.add_argument(
        "--skip-block-dump",
        action="store_true",
        help="Do not write the per-block assignment CSV (still writes precinct weights).",
    )
    ap.add_argument(
        "--with-nhgis",
        action="store_true",
        help="Also build VTD10 and VTD00 crosswalks via NHGIS + tabblock10/00.",
    )
    args = ap.parse_args()
    configure_plan(args.plan)
    configure_block_source(args.block_data_dir)
    if not CD_GEOJSON.exists():
        raise SystemExit(f"Missing {CD_GEOJSON}")

    blocks = build_crosswalk(write_blocks=not args.skip_block_dump)

    if not args.with_nhgis:
        print("Done (VTD20 via tabblocks). Re-run with --with-nhgis for decade chaining.")
        return

    print("Loading NHGIS blk2010->blk2020 ...")
    nhgis_10_20 = load_nhgis_csv(
        NHGIS_2010_2020_CSV,
        NHGIS_2010_2020_ZIP,
        usecols=["blk2010gj", "blk2020gj", "weight"],
    )
    print(f"  {len(nhgis_10_20):,} NHGIS 2010->2020 rows")

    assign_historical_vtds_via_nhgis(
        blocks,
        era="vtd10",
        vtd_geojson=VTD10_GEOJSON,
        nhgis_frames=[("2010", nhgis_10_20)],
        out_path=OUT_VTD10,
    )

    if NHGIS_2000_2010_ZIP.exists() and VTD00_GEOJSON.exists():
        print("Loading NHGIS blk2000->blk2010 ...")
        nhgis_00_10 = load_nhgis_csv(
            NHGIS_2000_2010_CSV,
            NHGIS_2000_2010_ZIP,
            usecols=["blk2000gj", "blk2010gj", "weight"],
        )
        print(f"  {len(nhgis_00_10):,} NHGIS 2000->2010 rows")
        assign_historical_vtds_via_nhgis(
            blocks,
            era="vtd00",
            vtd_geojson=VTD00_GEOJSON,
            nhgis_frames=[("2010", nhgis_10_20), ("2000", nhgis_00_10)],
            out_path=OUT_VTD00,
        )
    else:
        print("Skip VTD00 NHGIS chain (missing nhgis_blk2000_blk2010 zip and/or mo_vtd00_precincts.geojson)")

    print("Done.")


if __name__ == "__main__":
    main()
