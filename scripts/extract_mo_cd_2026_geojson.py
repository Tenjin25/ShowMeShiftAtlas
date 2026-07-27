#!/usr/bin/env python
"""
Extract Missouri 2026 congressional district geometries from MO_CD_MO_2025.zip.

Output:
  Data/mo_congressional_districts_2026.geojson
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
ZIP_PATH = DATA_DIR / "MO_CD_MO_2025.zip"
OUT_PATH = DATA_DIR / "mo_congressional_districts_2026.geojson"
STEM = "MO_CD_MO_First_2025"


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


def district_sort_key(feature: dict) -> tuple:
    district = str((feature.get("properties") or {}).get("district") or "")
    if district.isdigit():
        return (0, f"{int(district):04d}")
    return (1, district)


def main() -> None:
    if not ZIP_PATH.exists():
        raise SystemExit(f"Missing {ZIP_PATH}")

    with zipfile.ZipFile(ZIP_PATH, "r") as zf:
        shp = io.BytesIO(zf.read(f"{STEM}.shp"))
        shx = io.BytesIO(zf.read(f"{STEM}.shx"))
        dbf = io.BytesIO(zf.read(f"{STEM}.dbf"))
        reader = shapefile.Reader(shp=shp, shx=shx, dbf=dbf)
        field_names = [field[0] for field in reader.fields[1:]]

        features = []
        for shape_record in reader.iterShapeRecords():
            props = {
                name: to_json_value(value)
                for name, value in zip(field_names, shape_record.record)
            }
            props["district"] = normalize_district_num(props.get("DISTRICT") or props.get("LABEL"))
            features.append(
                {
                    "type": "Feature",
                    "properties": props,
                    "geometry": shape_record.shape.__geo_interface__,
                }
            )

    features.sort(key=district_sort_key)
    OUT_PATH.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}),
        encoding="utf-8",
    )
    print(f"Wrote {OUT_PATH} ({len(features)} districts)")


if __name__ == "__main__":
    main()
