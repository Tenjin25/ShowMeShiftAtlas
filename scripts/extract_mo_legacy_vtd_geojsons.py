#!/usr/bin/env python
"""
Extract Missouri legacy VTD precinct geometries to GeoJSON for overlap-based reallocation onto 2022 districts.

Outputs:
- Data/mo_vtd00_precincts.geojson  (from per-county TIGER 2008 VTD00 zips)
- Data/mo_vtd10_precincts.geojson  (from TIGER 2012 VTD10 zip)

The output schema intentionally mirrors the VTD20 GeoJSON used elsewhere:
- properties.county_nam (Census county name)
- properties.VTDST20 (6-digit precinct id string)
- properties.NAME20 / properties.NAMELSAD20
- properties.prec_id / properties.precinct_name / properties.precinct_norm
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import date, datetime
from pathlib import Path

import shapefile


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "Data"
COUNTIES_GEOJSON = DATA_DIR / "tl_2020_29_county20.geojson"

VTD00_DIR = DATA_DIR / "tiger2008" / "vtd00_by_county"
VTD10_ZIP = DATA_DIR / "tl_2012_29_vtd10.zip"

OUT_VTD00 = DATA_DIR / "mo_vtd00_precincts.geojson"
OUT_VTD10 = DATA_DIR / "mo_vtd10_precincts.geojson"


def to_json_value(value: object) -> object:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def pad6(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    digits = "".join(ch for ch in text if ch.isdigit())
    if digits:
        return f"{int(digits):06d}"
    return text.upper()


def load_county_name_by_fips() -> dict[str, str]:
    payload = json.loads(COUNTIES_GEOJSON.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for feat in payload.get("features", []):
        props = feat.get("properties") or {}
        fips = str(props.get("COUNTYFP20") or "").strip()
        name = str(props.get("NAME20") or "").strip()
        if not fips or not name:
            continue
        out[fips.zfill(3)] = name
    # Census county code 510 is the independent City of St. Louis.  The county
    # boundary file calls both 189 (St. Louis County) and 510 (St. Louis city)
    # simply "St. Louis", which otherwise makes their VTD identifiers collide.
    out["510"] = "St. Louis City"
    if len(out) < 100:
        raise RuntimeError(f"Unexpected county map size: {len(out)}")
    return out


def district_sort_key(feature: dict) -> tuple[str, str]:
    props = feature.get("properties") or {}
    return (str(props.get("county_nam") or ""), str(props.get("VTDST20") or ""))


def write_geojson(path: Path, features: list[dict]) -> None:
    features.sort(key=district_sort_key)
    payload = {"type": "FeatureCollection", "features": features}
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {len(features)} features -> {path}")


def extract_vtd00(county_name_by_fips: dict[str, str]) -> None:
    features: list[dict] = []
    zips = sorted(VTD00_DIR.glob("tl_2008_29*_vtd00.zip"))
    if not zips:
        raise FileNotFoundError(f"No VTD00 zips found under {VTD00_DIR}")

    for zp in zips:
        with zipfile.ZipFile(zp, "r") as zf:
            # File stem is the zip filename without extension.
            stem = zp.stem
            shp = io.BytesIO(zf.read(f"{stem}.shp"))
            shx = io.BytesIO(zf.read(f"{stem}.shx"))
            dbf = io.BytesIO(zf.read(f"{stem}.dbf"))
            reader = shapefile.Reader(shp=shp, shx=shx, dbf=dbf)
            field_names = [field[0] for field in reader.fields[1:]]

            for shape_record in reader.iterShapeRecords():
                raw_props = {
                    name: to_json_value(value)
                    for name, value in zip(field_names, shape_record.record)
                }
                county_fips = str(raw_props.get("COUNTYFP00") or "").strip().zfill(3)
                county_name = county_name_by_fips.get(county_fips, "")
                vtd_id = pad6(raw_props.get("VTDST00"))
                if not county_name or not vtd_id:
                    continue

                precinct_norm = f"{county_name.upper()} - {vtd_id}"
                props = {
                    "STATEFP20": str(raw_props.get("STATEFP00") or "").strip(),
                    "COUNTYFP20": county_fips,
                    "GEOID20": str(raw_props.get("VTDIDFP00") or "").strip(),
                    "NAME20": str(raw_props.get("NAME00") or "").strip(),
                    "NAMELSAD20": str(raw_props.get("NAMELSAD00") or "").strip(),
                    "VTDST20": vtd_id,
                    "county_nam": county_name,
                    "prec_id": vtd_id,
                    "precinct_name": f"{county_name} - {vtd_id}",
                    "precinct_norm": precinct_norm,
                }
                features.append(
                    {
                        "type": "Feature",
                        "properties": props,
                        "geometry": shape_record.shape.__geo_interface__,
                    }
                )

    write_geojson(OUT_VTD00, features)


def extract_vtd10(county_name_by_fips: dict[str, str]) -> None:
    features: list[dict] = []
    if not VTD10_ZIP.exists():
        raise FileNotFoundError(f"Missing {VTD10_ZIP}")

    with zipfile.ZipFile(VTD10_ZIP, "r") as zf:
        stem = "tl_2012_29_vtd10"
        shp = io.BytesIO(zf.read(f"{stem}.shp"))
        shx = io.BytesIO(zf.read(f"{stem}.shx"))
        dbf = io.BytesIO(zf.read(f"{stem}.dbf"))
        reader = shapefile.Reader(shp=shp, shx=shx, dbf=dbf)
        field_names = [field[0] for field in reader.fields[1:]]

        for shape_record in reader.iterShapeRecords():
            raw_props = {
                name: to_json_value(value)
                for name, value in zip(field_names, shape_record.record)
            }
            county_fips = str(raw_props.get("COUNTYFP10") or "").strip().zfill(3)
            county_name = county_name_by_fips.get(county_fips, "")
            vtd_id = pad6(raw_props.get("VTDST10"))
            if not county_name or not vtd_id:
                continue

            precinct_norm = f"{county_name.upper()} - {vtd_id}"
            props = {
                "STATEFP20": str(raw_props.get("STATEFP10") or "").strip(),
                "COUNTYFP20": county_fips,
                "GEOID20": str(raw_props.get("GEOID10") or "").strip(),
                "NAME20": str(raw_props.get("NAME10") or "").strip(),
                "NAMELSAD20": str(raw_props.get("NAMELSAD10") or "").strip(),
                "VTDST20": vtd_id,
                "county_nam": county_name,
                "prec_id": vtd_id,
                "precinct_name": f"{county_name} - {vtd_id}",
                "precinct_norm": precinct_norm,
            }
            features.append(
                {
                    "type": "Feature",
                    "properties": props,
                    "geometry": shape_record.shape.__geo_interface__,
                }
            )

    write_geojson(OUT_VTD10, features)


def main() -> None:
    county_name_by_fips = load_county_name_by_fips()
    extract_vtd00(county_name_by_fips)
    extract_vtd10(county_name_by_fips)


if __name__ == "__main__":
    main()
