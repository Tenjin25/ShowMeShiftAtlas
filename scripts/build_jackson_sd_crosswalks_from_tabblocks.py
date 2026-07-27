#!/usr/bin/env python
"""
Build Jackson County state-senate crosswalks via 2020 tabblocks (+ optional NHGIS).

Why this exists
---------------
OpenElections splits Jackson County into two reporting jurisdictions:
  - JACKSON      (Jackson County Election Board)
  - KANSAS CITY  (Kansas City Board of Election Commissioners)

County-map aggregation already rolls KANSAS CITY into JACKSON. Senate districts
SD-08 and SD-11 are Jackson-based and include many Kansas City VTDs, so the same
split breaks precinct→district aggregation unless KC-named VTDs are dual-aliased.

This script:
  1) Loads Jackson County (FIPS 095) 2020 tabulation blocks.
  2) Point-in-polygon assigns each block to a VTD20 precinct and a 2022 SLDU.
  3) Aggregates block counts into VTD20 → state senate area weights.
  4) Tags KC-named VTDs so election matching can try KANSAS CITY then JACKSON.
  5) Optionally chains NHGIS blk2010→blk2020 (and blk2000→blk2010) weights to
     emit VTD10 / VTD00 → 2022 senate crosswalk rows for the same districts.

Defaults focus on SD-08 and SD-11; pass --districts to widen.

Usage:
  python scripts/build_jackson_sd_crosswalks_from_tabblocks.py
  python scripts/build_jackson_sd_crosswalks_from_tabblocks.py --districts 7,8,9,11
  python scripts/build_jackson_sd_crosswalks_from_tabblocks.py --districts 7,8,9,11 --with-nhgis

Decade chain (--with-nhgis)
---------------------------
  tabblock20 + VTD20  -> jackson_vtd20_..._from_tabblocks.csv
  + NHGIS 2010->2020 + tabblock10 + VTD10
                      -> jackson_vtd10_..._from_nhgis.csv
  + NHGIS 2000->2010 + Jackson tabblock00 + VTD00
                      -> jackson_vtd00_..._from_nhgis.csv
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

JACKSON_COUNTYFP = "095"
STATEFP = "29"
DEFAULT_DISTRICTS = ("7", "8", "9", "11")

TABBLOCK20_SHP = DATA_DIR / "_extract_tabblock20" / "tl_2020_29_tabblock20.shp"
TABBLOCK20_ZIP = DATA_DIR / "tl_2020_29_tabblock20.zip"
TABBLOCK10_ZIP = DATA_DIR / "tl_2020_29_tabblock10.zip"
TABBLOCK10_SHP = DATA_DIR / "_extract_tabblock10" / "tl_2020_29_tabblock10.shp"
TABBLOCK00_JACKSON_ZIP = DATA_DIR / "tiger2008" / "tabblock00_by_county" / "tl_2008_29095_tabblock00.zip"
TABBLOCK00_STATE_ZIP = DATA_DIR / "tiger2008" / "tl_2008_29_tabblock00.zip"

VTD20_GEOJSON = DATA_DIR / "mo_vtd20_precincts.geojson"
VTD10_GEOJSON = DATA_DIR / "mo_vtd10_precincts.geojson"
VTD00_GEOJSON = DATA_DIR / "mo_vtd00_precincts.geojson"
SLDU_GEOJSON = DATA_DIR / "mo_state_senate_districts_2022.geojson"

NHGIS_2010_2020_CSV = DATA_DIR / "_extract_nhgis_blk2010_blk2020_29" / "nhgis_blk2010_blk2020_29.csv"
NHGIS_2010_2020_ZIP = DATA_DIR / "nhgis_blk2010_blk2020_29.zip"
NHGIS_2000_2010_CSV = DATA_DIR / "_extract_nhgis_blk2000_blk2010_29" / "nhgis_blk2000_blk2010_29.csv"
NHGIS_2000_2010_ZIP = DATA_DIR / "nhgis_blk2000_blk2010_29.zip"

OUT_BLOCK = CROSSWALK_DIR / "jackson_tabblock20_to_2022_state_senate.csv"
OUT_VTD20 = CROSSWALK_DIR / "jackson_vtd20_to_2022_state_senate_from_tabblocks.csv"
OUT_VTD10 = CROSSWALK_DIR / "jackson_vtd10_to_2022_state_senate_from_nhgis.csv"
OUT_VTD00 = CROSSWALK_DIR / "jackson_vtd00_to_2022_state_senate_from_nhgis.csv"


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


def is_kansas_city_vtd_name(name20: object, namelsad20: object = "", precinct_norm: object = "") -> bool:
    blob = " ".join(
        str(v or "").strip().upper()
        for v in (name20, namelsad20, precinct_norm)
    ).strip()
    if not blob:
        return False
    if "KANSAS CITY" in blob:
        return True
    # Census NAME20 for KCMO VTDs is typically "KC 1102", "KC WD13 PCT1302", etc.
    tokens = blob.replace("-", " ").split()
    if tokens and tokens[0] in {"KC", "KCMO"}:
        return True
    if any(tok.startswith("KC") and any(ch.isdigit() for ch in tok) for tok in tokens):
        return True
    return False


def election_county_for_vtd(is_kc: bool) -> str:
    # Election returns for KC Board precincts use county="Kansas City".
    # Non-KC Jackson precincts use county="Jackson".
    return "KANSAS CITY" if is_kc else "JACKSON"


def election_county_aliases(is_kc: bool) -> str:
    # Always keep JACKSON as a fallback because VTD geography is Jackson County.
    if is_kc:
        return "KANSAS CITY|JACKSON"
    return "JACKSON|KANSAS CITY"


def block_geoid_to_gisjoin(geoid: object) -> str:
    # NHGIS GISJOIN: G + state(2) + "0" + county(3) + "0" + tract(6) + block(4)
    s = str(geoid or "").strip().replace(".0", "")
    s = s.zfill(15)
    return f"G{s[0:2]}0{s[2:5]}0{s[5:11]}{s[11:15]}"


def parse_districts(raw: str) -> Optional[Set[str]]:
    text = str(raw or "").strip()
    if not text or text.lower() in {"all", "*"}:
        return None
    out: Set[str] = set()
    for part in text.split(","):
        d = normalize_district_num(part)
        if d:
            out.add(d)
    return out or None


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


def load_jackson_block_points() -> "gpd.GeoDataFrame":
    import geopandas as gpd
    import pyogrio
    from shapely.geometry import Point

    shp = ensure_tabblock20_shp()
    print(f"Reading Jackson tabblock20 points from {shp} ...")
    df = pyogrio.read_dataframe(
        str(shp),
        columns=["GEOID20", "COUNTYFP20", "INTPTLAT20", "INTPTLON20"],
        where=f"COUNTYFP20 = '{JACKSON_COUNTYFP}'",
        read_geometry=False,
    )
    if df.empty:
        raise SystemExit("No Jackson County tabblocks found (COUNTYFP20=095).")

    df["GEOID20"] = df["GEOID20"].astype(str).str.strip().str.zfill(15)
    df["blk2020gj"] = df["GEOID20"].map(block_geoid_to_gisjoin)
    df["INTPTLAT20"] = pd.to_numeric(df["INTPTLAT20"], errors="coerce")
    df["INTPTLON20"] = pd.to_numeric(df["INTPTLON20"], errors="coerce")
    df = df.dropna(subset=["INTPTLAT20", "INTPTLON20"]).copy()
    geometry = [Point(xy) for xy in zip(df["INTPTLON20"].tolist(), df["INTPTLAT20"].tolist())]
    gdf = gpd.GeoDataFrame(df, geometry=geometry, crs="EPSG:4326")
    print(f"  {len(gdf):,} Jackson blocks")
    return gdf


def load_jackson_vtds(path: Path, era_label: str) -> "gpd.GeoDataFrame":
    import geopandas as gpd

    if not path.exists():
        raise SystemExit(f"Missing {path}")
    print(f"Reading {era_label} precincts from {path} ...")
    gdf = gpd.read_file(path)
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    else:
        gdf = gdf.to_crs("EPSG:4326")

    county_col = next(
        (c for c in ("county_nam", "COUNTYNAME", "COUNTYFP20", "COUNTYFP10", "COUNTYFP") if c in gdf.columns),
        None,
    )
    if county_col is None:
        raise SystemExit(f"{path} missing a county field")

    county_series = gdf[county_col].astype(str).str.strip().str.upper()
    if county_col.upper().startswith("COUNTYFP"):
        mask = county_series.str.zfill(3) == JACKSON_COUNTYFP
    else:
        mask = county_series.str.replace(r"\s+COUNTY$", "", regex=True).eq("JACKSON")

    gdf = gdf.loc[mask].copy()
    if gdf.empty:
        raise SystemExit(f"No Jackson precincts found in {path}")

    if "precinct_norm" in gdf.columns:
        gdf["precinct_key"] = gdf["precinct_norm"].map(normalize_precinct_key)
    else:
        name_col = next((c for c in ("NAME20", "NAME10", "NAME00", "NAME") if c in gdf.columns), None)
        vtd_col = next((c for c in ("VTDST20", "VTDST10", "VTDST00", "VTDST") if c in gdf.columns), None)
        if name_col and vtd_col:
            gdf["precinct_key"] = (
                "JACKSON - " + gdf[vtd_col].astype(str).str.strip().str.upper()
            ).map(normalize_precinct_key)
        elif vtd_col:
            gdf["precinct_key"] = (
                "JACKSON - " + gdf[vtd_col].astype(str).str.strip().str.upper()
            ).map(normalize_precinct_key)
        else:
            raise SystemExit(f"{path} missing precinct identity fields")

    name20 = gdf["NAME20"] if "NAME20" in gdf.columns else (gdf["NAME10"] if "NAME10" in gdf.columns else "")
    namelsad = gdf["NAMELSAD20"] if "NAMELSAD20" in gdf.columns else ""
    gdf["is_kansas_city"] = [
        is_kansas_city_vtd_name(n, s, p)
        for n, s, p in zip(name20, namelsad, gdf["precinct_key"])
    ]
    gdf["election_county"] = [election_county_for_vtd(flag) for flag in gdf["is_kansas_city"]]
    gdf["election_county_aliases"] = [election_county_aliases(flag) for flag in gdf["is_kansas_city"]]

    print(
        f"  {len(gdf):,} Jackson {era_label} precincts "
        f"({int(gdf['is_kansas_city'].sum()):,} KC-tagged)"
    )
    return gdf[
        ["precinct_key", "is_kansas_city", "election_county", "election_county_aliases", "geometry"]
    ].drop_duplicates(subset=["precinct_key"], keep="first")


def load_senate_districts(district_filter: Optional[Set[str]]) -> "gpd.GeoDataFrame":
    import geopandas as gpd

    if not SLDU_GEOJSON.exists():
        raise SystemExit(f"Missing {SLDU_GEOJSON}")
    print(f"Reading state senate districts from {SLDU_GEOJSON} ...")
    gdf = gpd.read_file(SLDU_GEOJSON)
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    else:
        gdf = gdf.to_crs("EPSG:4326")

    district_col = next((c for c in ("district", "SLDUST", "GEOID") if c in gdf.columns), None)
    if district_col is None:
        raise SystemExit(f"{SLDU_GEOJSON} missing district field")

    gdf["district_num"] = gdf[district_col].map(normalize_district_num)
    if district_filter is not None:
        gdf = gdf.loc[gdf["district_num"].isin(district_filter)].copy()
    gdf = gdf.loc[gdf["district_num"] != ""].copy()
    if gdf.empty:
        raise SystemExit("No senate districts selected")
    print(f"  districts: {', '.join(sorted(gdf['district_num'].unique(), key=lambda x: int(x) if x.isdigit() else x))}")
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
    # If a point lands on a boundary / miss, fall back to nearest polygon in a
    # projected CRS so geodesic distance isn't used on lon/lat degrees.
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
            meta[precinct] = {
                "is_kansas_city": bool(getattr(row, "is_kansas_city", False)),
                "election_county": str(getattr(row, "election_county", "") or ""),
                "election_county_aliases": str(getattr(row, "election_county_aliases", "") or ""),
            }

    rows: List[Dict[str, object]] = []
    for (precinct, district), weight in sorted(pair_weight.items(), key=lambda x: (x[0][0], int(x[0][1]) if x[0][1].isdigit() else x[0][1])):
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
                "election_county": info.get("election_county") or election_county_for_vtd(False),
                "election_county_aliases": info.get("election_county_aliases")
                or election_county_aliases(False),
            }
        )
    return rows


def build_vtd20_from_tabblocks(district_filter: Optional[Set[str]]) -> pd.DataFrame:
    blocks = load_jackson_block_points()
    vtds = load_jackson_vtds(VTD20_GEOJSON, "VTD20")
    # Assign against ALL senate districts so nearest-fallback cannot pull SD-7/9
    # blocks into SD-8/11. Output filtering happens after assignment.
    districts = load_senate_districts(None)

    blocks = blocks.copy()
    blocks["precinct_key"] = spatial_assign_blocks(blocks, vtds, "precinct_key", "VTD20")
    blocks["district_num"] = spatial_assign_blocks(blocks, districts, "district_num", "SLDU 2022")

    # Attach KC / election-county metadata from the VTD layer.
    vtd_meta = vtds.drop(columns=["geometry"]).drop_duplicates("precinct_key")
    blocks = blocks.merge(vtd_meta, on="precinct_key", how="left")
    blocks["is_kansas_city"] = blocks["is_kansas_city"].fillna(False).astype(bool)
    blocks["election_county"] = blocks.apply(
        lambda r: r["election_county"]
        if isinstance(r["election_county"], str) and r["election_county"]
        else election_county_for_vtd(bool(r["is_kansas_city"])),
        axis=1,
    )
    blocks["election_county_aliases"] = blocks.apply(
        lambda r: r["election_county_aliases"]
        if isinstance(r["election_county_aliases"], str) and r["election_county_aliases"]
        else election_county_aliases(bool(r["is_kansas_city"])),
        axis=1,
    )

    # Keep all Jackson blocks in the block dump; VTD weights are filtered below.
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
    if district_filter is not None:
        # Keep true multi-district shares; do not renormalize after filtering.
        vtd_rows = [r for r in vtd_rows if str(r["district_num"]) in district_filter]
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
    print(f"  VTD20 rows with KC election alias: {kc_rows:,}")
    return blocks


def load_era_block_points(era: str) -> "gpd.GeoDataFrame":
    """Load Jackson block internal points for a historical decade."""
    import geopandas as gpd
    import pyogrio
    from shapely.geometry import Point

    if era == "vtd10":
        extract_guess = TABBLOCK10_SHP
        zip_path = TABBLOCK10_ZIP
        geoid_field = "GEOID10"
        lat_field = "INTPTLAT10"
        lon_field = "INTPTLON10"
        where = f"COUNTYFP10 = '{JACKSON_COUNTYFP}'"
        if extract_guess.exists():
            src = str(extract_guess)
        elif zip_path.exists():
            src = f"zip://{zip_path}"
        else:
            raise SystemExit(f"Missing tabblock10 source ({extract_guess} / {zip_path})")
        print(f"Reading Jackson tabblock10 points from {src} ...")
        try:
            hdf = pyogrio.read_dataframe(
                src,
                columns=[geoid_field, "COUNTYFP10", lat_field, lon_field],
                where=where,
                read_geometry=False,
            )
        except Exception:
            hdf = pyogrio.read_dataframe(
                src,
                columns=[geoid_field, lat_field, lon_field],
                read_geometry=False,
            )
            hdf = hdf.loc[hdf[geoid_field].astype(str).str[2:5] == JACKSON_COUNTYFP].copy()
    elif era == "vtd00":
        zip_path = TABBLOCK00_JACKSON_ZIP if TABBLOCK00_JACKSON_ZIP.exists() else TABBLOCK00_STATE_ZIP
        if not zip_path.exists():
            raise SystemExit(f"Missing tabblock00 source ({TABBLOCK00_JACKSON_ZIP})")
        extract_dir = DATA_DIR / "_extract_tabblock00_jackson"
        extract_dir.mkdir(parents=True, exist_ok=True)
        shp_files = list(extract_dir.glob("*.shp"))
        if not shp_files:
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(extract_dir)
            shp_files = list(extract_dir.glob("*.shp"))
        if not shp_files:
            raise SystemExit(f"No .shp found after extracting {zip_path}")
        src = str(shp_files[0])
        print(f"Reading Jackson tabblock00 points from {src} ...")
        # 2008 TIGER tabblock00 field names vary slightly by extract.
        info = pyogrio.read_info(src)
        raw_fields = info.get("fields")
        if raw_fields is None:
            fields: Set[str] = set()
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
            # Fall back to geometry centroids.
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
            # Jackson-only filter if statewide extract was used.
            pts = pts.loc[pts["GEOID"].str[2:5] == JACKSON_COUNTYFP].copy()
            pts["blk_gj"] = pts["GEOID"].map(block_geoid_to_gisjoin)
            print(f"  {len(pts):,} Jackson 2000 blocks")
            return pts

        hdf = pyogrio.read_dataframe(
            src,
            columns=[geoid_field, lat_field, lon_field],
            read_geometry=False,
        )
        hdf = hdf.loc[hdf[geoid_field].astype(str).str[2:5] == JACKSON_COUNTYFP].copy()
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
    print(f"  {len(gdf):,} Jackson {era} blocks")
    return gdf


def assign_historical_vtds_via_nhgis(
    blocks_2020: pd.DataFrame,
    *,
    era: str,
    vtd_geojson: Path,
    nhgis_frames: List[Tuple[str, pd.DataFrame]],
    out_path: Path,
    district_filter: Optional[Set[str]],
) -> None:
    """
    Chain NHGIS block weights onto 2020 block→district assignments, then attach
    historical VTD polygons using that decade's tabblock points.
    """
    if not vtd_geojson.exists():
        print(f"Skip {era}: missing {vtd_geojson}")
        return

    # Use ALL 2020 block→district assignments for chaining so split shares stay true.
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

    vtds = load_jackson_vtds(vtd_geojson, era.upper())
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
    if district_filter is not None:
        rows = [r for r in rows if str(r["district_num"]) in district_filter]

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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--districts",
        default=",".join(DEFAULT_DISTRICTS),
        help="Comma-separated senate districts to include (default: 8,11). Use 'all' for every district.",
    )
    ap.add_argument(
        "--with-nhgis",
        action="store_true",
        help="Also build VTD10 (and VTD00 when possible) crosswalks via NHGIS block weights.",
    )
    args = ap.parse_args()
    district_filter = parse_districts(args.districts)

    blocks = build_vtd20_from_tabblocks(district_filter)

    if not args.with_nhgis:
        print("Done (VTD20 via tabblocks). Re-run with --with-nhgis for decade chaining.")
        return

    print("Loading NHGIS blk2010->blk2020 ...")
    nhgis_10_20 = load_nhgis_csv(
        NHGIS_2010_2020_CSV,
        NHGIS_2010_2020_ZIP,
        usecols=["blk2010gj", "blk2020gj", "weight"],
    )
    # Keep rows that touch Jackson 2020 blocks only.
    jackson_gj = set(blocks["blk2020gj"].dropna().astype(str))
    nhgis_10_20 = nhgis_10_20.loc[nhgis_10_20["blk2020gj"].isin(jackson_gj)].copy()
    print(f"  {len(nhgis_10_20):,} NHGIS 2010->2020 rows touching Jackson blocks")

    assign_historical_vtds_via_nhgis(
        blocks,
        era="vtd10",
        vtd_geojson=VTD10_GEOJSON,
        nhgis_frames=[("2010", nhgis_10_20)],
        out_path=OUT_VTD10,
        district_filter=district_filter,
    )

    if NHGIS_2000_2010_ZIP.exists() and VTD00_GEOJSON.exists():
        print("Loading NHGIS blk2000->blk2010 ...")
        nhgis_00_10 = load_nhgis_csv(
            NHGIS_2000_2010_CSV,
            NHGIS_2000_2010_ZIP,
            usecols=["blk2000gj", "blk2010gj", "weight"],
        )
        jackson_2010 = set(nhgis_10_20["blk2010gj"].dropna().astype(str))
        nhgis_00_10 = nhgis_00_10.loc[nhgis_00_10["blk2010gj"].isin(jackson_2010)].copy()
        print(f"  {len(nhgis_00_10):,} NHGIS 2000->2010 rows touching Jackson 2010 blocks")
        assign_historical_vtds_via_nhgis(
            blocks,
            era="vtd00",
            vtd_geojson=VTD00_GEOJSON,
            nhgis_frames=[("2010", nhgis_10_20), ("2000", nhgis_00_10)],
            out_path=OUT_VTD00,
            district_filter=district_filter,
        )
    else:
        print("Skip VTD00 NHGIS chain (missing nhgis_blk2000_blk2010 zip and/or mo_vtd00_precincts.geojson)")

    print("Done.")


if __name__ == "__main__":
    main()
