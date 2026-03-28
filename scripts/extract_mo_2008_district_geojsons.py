#!/usr/bin/env python
"""
Extract Missouri ~2000s-era district geometries from Census TIGER/Line 2008 ZIPs to GeoJSON.

Outputs:
- Data/mo_congressional_districts_2008.geojson
- Data/mo_state_house_districts_2008.geojson
- Data/mo_state_senate_districts_2008.geojson
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
TIGER2008_DIR = DATA_DIR / "tiger2008"

BUILDS = [
    {
        "kind": "congressional",
        "zip_path": TIGER2008_DIR / "tl_2008_29_cd110.zip",
        "stem": "tl_2008_29_cd110",
        "district_field": "CD110FP",
        "out_path": DATA_DIR / "mo_congressional_districts_2008.geojson",
    },
    {
        "kind": "state_house",
        "zip_path": TIGER2008_DIR / "tl_2008_29_sldl00.zip",
        "stem": "tl_2008_29_sldl00",
        "district_field": "SLDLST00",
        "out_path": DATA_DIR / "mo_state_house_districts_2008.geojson",
    },
    {
        "kind": "state_senate",
        "zip_path": TIGER2008_DIR / "tl_2008_29_sldu00.zip",
        "stem": "tl_2008_29_sldu00",
        "district_field": "SLDUST00",
        "out_path": DATA_DIR / "mo_state_senate_districts_2008.geojson",
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


def pad3(district_num: str) -> str:
    return f"{int(district_num):03d}" if district_num.isdigit() else district_num


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


def standardize_props(kind: str, props: dict, district_field: str) -> dict:
    district_num = normalize_district_num(props.get(district_field))

    if kind == "congressional":
        statefp = str(props.get("STATEFP") or "").strip()
        out = {
            "STATEFP": statefp,
            "NAMELSAD": to_json_value(props.get("NAMELSAD") or ""),
            "DISTRICT": district_num,
            "district": district_num,
            "CD": district_num,
        }
        if statefp and district_num.isdigit():
            out["GEOID"] = f"{statefp}{int(district_num):02d}"
        return out

    if kind == "state_house":
        statefp = str(props.get("STATEFP00") or props.get("STATEFP") or "").strip()
        sldlst = pad3(normalize_district_num(props.get("SLDLST00") or props.get("SLDLST") or ""))
        out = {
            "STATEFP": statefp,
            "SLDLST": sldlst,
            "NAMELSAD": to_json_value(props.get("NAMELSAD00") or props.get("NAMELSAD") or ""),
            "LSAD": to_json_value(props.get("LSAD00") or props.get("LSAD") or ""),
            "LSY": to_json_value(props.get("LSY") or ""),
            "MTFCC": to_json_value(props.get("MTFCC00") or props.get("MTFCC") or ""),
            "FUNCSTAT": to_json_value(props.get("FUNCSTAT00") or props.get("FUNCSTAT") or ""),
            "DISTRICT": district_num,
            "district": district_num,
        }
        if statefp and district_num.isdigit():
            out["GEOID"] = f"{statefp}{int(district_num):03d}"
        return out

    if kind == "state_senate":
        statefp = str(props.get("STATEFP00") or props.get("STATEFP") or "").strip()
        sldust = pad3(normalize_district_num(props.get("SLDUST00") or props.get("SLDUST") or ""))
        out = {
            "STATEFP": statefp,
            "SLDUST": sldust,
            "NAMELSAD": to_json_value(props.get("NAMELSAD00") or props.get("NAMELSAD") or ""),
            "LSAD": to_json_value(props.get("LSAD00") or props.get("LSAD") or ""),
            "LSY": to_json_value(props.get("LSY") or ""),
            "MTFCC": to_json_value(props.get("MTFCC00") or props.get("MTFCC") or ""),
            "FUNCSTAT": to_json_value(props.get("FUNCSTAT00") or props.get("FUNCSTAT") or ""),
            "DISTRICT": district_num,
            "district": district_num,
        }
        if statefp and district_num.isdigit():
            out["GEOID"] = f"{statefp}{int(district_num):03d}"
        return out

    raise ValueError(f"Unknown kind: {kind}")


def extract_one(build: dict) -> None:
    zip_path = Path(build["zip_path"])
    stem = build["stem"]
    district_field = build["district_field"]
    kind = build["kind"]
    out_path = Path(build["out_path"])

    with zipfile.ZipFile(zip_path, "r") as zf:
        shp = io.BytesIO(zf.read(f"{stem}.shp"))
        shx = io.BytesIO(zf.read(f"{stem}.shx"))
        dbf = io.BytesIO(zf.read(f"{stem}.dbf"))
        reader = shapefile.Reader(shp=shp, shx=shx, dbf=dbf)
        field_names = [field[0] for field in reader.fields[1:]]

        features = []
        for shape_record in reader.iterShapeRecords():
            raw_props = {
                name: to_json_value(value)
                for name, value in zip(field_names, shape_record.record)
            }
            props = standardize_props(kind, raw_props, district_field)
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

