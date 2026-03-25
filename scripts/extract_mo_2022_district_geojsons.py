#!/usr/bin/env python
"""
Extract Missouri 2022 district geometries from Census TIGER ZIPs to GeoJSON.

Outputs:
- Data/mo_congressional_districts_2022.geojson
- Data/mo_state_house_districts_2022.geojson
- Data/mo_state_senate_districts_2022.geojson
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

BUILDS = [
    {
        "zip_path": DATA_DIR / "tl_2022_29_cd118.zip",
        "stem": "tl_2022_29_cd118",
        "district_field": "CD118FP",
        "out_path": DATA_DIR / "mo_congressional_districts_2022.geojson",
    },
    {
        "zip_path": DATA_DIR / "tl_2022_29_sldl.zip",
        "stem": "tl_2022_29_sldl",
        "district_field": "SLDLST",
        "out_path": DATA_DIR / "mo_state_house_districts_2022.geojson",
    },
    {
        "zip_path": DATA_DIR / "tl_2022_29_sldu.zip",
        "stem": "tl_2022_29_sldu",
        "district_field": "SLDUST",
        "out_path": DATA_DIR / "mo_state_senate_districts_2022.geojson",
    },
]


def normalize_district_num(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    digits = "".join(ch for ch in text if ch.isdigit())
    if digits:
        return str(int(digits))
    return text.upper()


def to_json_value(value: object) -> object:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def district_sort_key(feature: dict) -> tuple[int, str]:
    district = str((feature.get("properties") or {}).get("district") or "")
    if district.isdigit():
        return (0, f"{int(district):04d}")
    return (1, district)


def extract_one(build: dict) -> None:
    zip_path = build["zip_path"]
    stem = build["stem"]
    district_field = build["district_field"]
    out_path = build["out_path"]

    with zipfile.ZipFile(zip_path, "r") as zf:
        shp = io.BytesIO(zf.read(f"{stem}.shp"))
        shx = io.BytesIO(zf.read(f"{stem}.shx"))
        dbf = io.BytesIO(zf.read(f"{stem}.dbf"))
        reader = shapefile.Reader(shp=shp, shx=shx, dbf=dbf)
        field_names = [field[0] for field in reader.fields[1:]]

        features = []
        for shape_record in reader.iterShapeRecords():
            props = {
                name: to_json_value(value)
                for name, value in zip(field_names, shape_record.record)
            }
            props["district"] = normalize_district_num(props.get(district_field))
            features.append(
                {
                    "type": "Feature",
                    "properties": props,
                    "geometry": shape_record.shape.__geo_interface__,
                }
            )

    features.sort(key=district_sort_key)
    payload = {"type": "FeatureCollection", "features": features}
    out_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {len(features)} features -> {out_path}")


def main() -> None:
    for build in BUILDS:
        extract_one(build)


if __name__ == "__main__":
    main()
