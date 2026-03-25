#!/usr/bin/env python
"""
Build Missouri precinct-to-district crosswalk CSVs from precinct centroids.

Outputs:
- Data/crosswalks/precinct_to_cd118.csv
- Data/crosswalks/precinct_to_2022_state_house.csv
- Data/crosswalks/precinct_to_2022_state_senate.csv

Each output row:
    precinct_key,district_num,area_weight

`area_weight` is set to 1.0 because this builder assigns each precinct centroid
to a single district polygon.
"""

from __future__ import annotations

import csv
import io
import json
import math
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

import shapefile


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "Data"
CROSSWALK_DIR = DATA_DIR / "crosswalks"

PRECINCT_CENTROIDS_GEOJSON = DATA_DIR / "mo_vtd20_precinct_centroids.geojson"

CD118_ZIP = DATA_DIR / "tl_2022_29_cd118.zip"
SLDL_ZIP = DATA_DIR / "tl_2022_29_sldl.zip"
SLDU_ZIP = DATA_DIR / "tl_2022_29_sldu.zip"


@dataclass
class DistrictFeature:
    district_num: str
    geometry_type: str
    coordinates: object
    bbox: Tuple[float, float, float, float]
    center: Tuple[float, float]


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


def point_in_ring(x: float, y: float, ring: Sequence[Sequence[float]]) -> bool:
    inside = False
    n = len(ring)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        crosses = ((yi > y) != (yj > y))
        if crosses:
            x_intersect = (xj - xi) * (y - yi) / ((yj - yi) or 1e-300) + xi
            if x < x_intersect:
                inside = not inside
        j = i
    return inside


def point_in_polygon(x: float, y: float, polygon_rings: Sequence[Sequence[Sequence[float]]]) -> bool:
    # Even-odd rule across all rings handles outer/inner rings regardless of orientation.
    inside = False
    for ring in polygon_rings:
        if point_in_ring(x, y, ring):
            inside = not inside
    return inside


def point_in_geometry(x: float, y: float, geometry_type: str, coordinates: object) -> bool:
    if geometry_type == "Polygon":
        return point_in_polygon(x, y, coordinates)  # type: ignore[arg-type]
    if geometry_type == "MultiPolygon":
        for polygon in coordinates:  # type: ignore[assignment]
            if point_in_polygon(x, y, polygon):
                return True
        return False
    return False


def load_district_features(zip_path: Path, stem: str, district_field: str) -> List[DistrictFeature]:
    with zipfile.ZipFile(zip_path, "r") as zf:
        shp = io.BytesIO(zf.read(f"{stem}.shp"))
        shx = io.BytesIO(zf.read(f"{stem}.shx"))
        dbf = io.BytesIO(zf.read(f"{stem}.dbf"))
        reader = shapefile.Reader(shp=shp, shx=shx, dbf=dbf)

        field_names = [f[0] for f in reader.fields[1:]]
        try:
            district_idx = field_names.index(district_field)
        except ValueError as err:
            raise RuntimeError(f"Field '{district_field}' not found in {zip_path.name}") from err

        out: List[DistrictFeature] = []
        for shape_rec in reader.iterShapeRecords():
            district_num = normalize_district_num(shape_rec.record[district_idx])
            if not district_num:
                continue
            geo = shape_rec.shape.__geo_interface__
            bbox = tuple(shape_rec.shape.bbox)  # xmin,ymin,xmax,ymax
            center = ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)
            out.append(
                DistrictFeature(
                    district_num=district_num,
                    geometry_type=str(geo.get("type", "")),
                    coordinates=geo.get("coordinates"),
                    bbox=(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
                    center=(float(center[0]), float(center[1])),
                )
            )
    return out


def load_precinct_centroids(path: Path) -> List[Tuple[str, float, float]]:
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    out: List[Tuple[str, float, float]] = []
    for feature in payload.get("features", []):
        props = feature.get("properties", {}) or {}
        geom = feature.get("geometry", {}) or {}
        if geom.get("type") != "Point":
            continue
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            continue
        key = normalize_precinct_key(props.get("precinct_norm") or props.get("precinct_name") or "")
        if not key:
            continue
        x, y = float(coords[0]), float(coords[1])
        out.append((key, x, y))
    return out


def assign_precincts_to_districts(
    precincts: Iterable[Tuple[str, float, float]],
    districts: Sequence[DistrictFeature],
) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for precinct_key, x, y in precincts:
        hit: DistrictFeature | None = None
        for district in districts:
            xmin, ymin, xmax, ymax = district.bbox
            if x < xmin or x > xmax or y < ymin or y > ymax:
                continue
            if point_in_geometry(x, y, district.geometry_type, district.coordinates):
                hit = district
                break

        if hit is None:
            # Fallback: nearest district bbox center (rare boundary-edge misses).
            best = None
            best_dist = math.inf
            for district in districts:
                cx, cy = district.center
                d2 = (x - cx) ** 2 + (y - cy) ** 2
                if d2 < best_dist:
                    best_dist = d2
                    best = district
            hit = best

        if hit is None:
            continue
        rows.append(
            {
                "precinct_key": precinct_key,
                "district_num": hit.district_num,
                "area_weight": 1.0,
            }
        )
    rows.sort(key=lambda r: str(r["precinct_key"]))
    return rows


def write_crosswalk(path: Path, rows: Sequence[Dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["precinct_key", "district_num", "area_weight"])
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main() -> None:
    precincts = load_precinct_centroids(PRECINCT_CENTROIDS_GEOJSON)
    if not precincts:
        raise RuntimeError(f"No precinct centroids found in {PRECINCT_CENTROIDS_GEOJSON}")

    builds = [
        (CD118_ZIP, "tl_2022_29_cd118", "CD118FP", CROSSWALK_DIR / "precinct_to_cd118.csv"),
        (SLDL_ZIP, "tl_2022_29_sldl", "SLDLST", CROSSWALK_DIR / "precinct_to_2022_state_house.csv"),
        (SLDU_ZIP, "tl_2022_29_sldu", "SLDUST", CROSSWALK_DIR / "precinct_to_2022_state_senate.csv"),
    ]

    for zip_path, stem, field, out_path in builds:
        districts = load_district_features(zip_path, stem, field)
        rows = assign_precincts_to_districts(precincts, districts)
        write_crosswalk(out_path, rows)
        print(f"Wrote {len(rows)} rows to {out_path}")


if __name__ == "__main__":
    main()
