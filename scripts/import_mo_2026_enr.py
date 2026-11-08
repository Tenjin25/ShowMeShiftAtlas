#!/usr/bin/env python
"""Import Missouri's 2026 unofficial ENR tables into atlas slices.

The ENR site is protected by Cloudflare, so this helper accepts the tables
captured from an ordinary browser session over a localhost-only HTTP form.
It writes a reproducible county-total CSV plus only the 2026 JSON slices and
manifest entries, leaving historical generated files untouched.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "Data"
CONTESTS = DATA / "contests"
DISTRICTS = DATA / "district_contests"
SOURCE_URL = "https://enr.sos.mo.gov/PickaRace.aspx"
SOURCE_STATUS = "unofficial"


def party_bucket(party: str) -> str:
    token = (party or "").strip().upper()
    if token.startswith("DEM"):
        return "dem"
    if token.startswith("REP"):
        return "rep"
    return "other"


def county_key(name: str) -> str:
    token = re.sub(r"[^A-Z0-9]", "", (name or "").upper().replace("SAINT ", "ST "))
    if token in {"KANSASCITY", "KANSASCITYCOUNTY"}:
        return "JACKSON"
    if token in {"STLOUISCITY", "CITYOFSTLOUIS"}:
        return "ST. LOUIS CITY"
    return (name or "").strip().upper()


def parse_race(label: str) -> tuple[str, str, str, str]:
    if label == "State Auditor":
        return "auditor", "", "", "State Auditor"
    match = re.search(r"District\s+(\d+)$", label)
    district = match.group(1) if match else ""
    if label.startswith("U.S. Representative"):
        return "us_house", "congressional", district, "US House"
    if label.startswith("State Senator"):
        return "state_senate", "state_senate", district, "State Senate"
    if label.startswith("State Representative"):
        return "state_house", "state_house", district, "State House"
    raise ValueError(f"Unsupported race label: {label}")


def parse_candidate_header(value: str) -> tuple[str, str]:
    text = (value or "").strip()
    if "," not in text:
        return text, ""
    name, party = text.rsplit(",", 1)
    return name.strip(), party.strip().upper()


def parse_votes(value: object) -> int:
    text = str(value or "").replace(",", "").strip()
    return int(text) if text.isdigit() else 0


class Aggregate:
    def __init__(self) -> None:
        self.dem = 0
        self.rep = 0
        self.other = 0
        self.dem_names: Counter[str] = Counter()
        self.rep_names: Counter[str] = Counter()

    def add(self, party: str, candidate: str, votes: int) -> None:
        bucket = party_bucket(party)
        if bucket == "dem":
            self.dem += votes
            self.dem_names[candidate] += votes
        elif bucket == "rep":
            self.rep += votes
            self.rep_names[candidate] += votes
        else:
            self.other += votes

    def result(self) -> dict:
        total = self.dem + self.rep + self.other
        margin = self.rep - self.dem
        return {
            "dem_votes": self.dem,
            "rep_votes": self.rep,
            "other_votes": self.other,
            "total_votes": total,
            "dem_candidate": self.dem_names.most_common(1)[0][0] if self.dem_names else "",
            "rep_candidate": self.rep_names.most_common(1)[0][0] if self.rep_names else "",
            "margin": margin,
            "margin_pct": round((margin / total * 100) if total else 0, 6),
            "winner": "REP" if margin > 0 else ("DEM" if margin < 0 else "TIE"),
            "color": "",
        }


def update_manifest(path: Path, entries: list[dict]) -> None:
    payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"files": []}
    replacements = {(e.get("scope", ""), e["contest_type"], int(e["year"])): e for e in entries}
    kept = []
    for entry in payload.get("files", []):
        key = (entry.get("scope", ""), entry.get("contest_type"), int(entry.get("year", 0)))
        if key not in replacements:
            kept.append(entry)
    kept.extend(entries)
    kept.sort(key=lambda e: (str(e.get("scope", "")), str(e.get("contest_type", "")), int(e.get("year", 0))))
    path.write_text(json.dumps({"files": kept}, indent=2) + "\n", encoding="utf-8")


def import_payload(races: list[dict]) -> dict:
    generated_at = datetime.now(timezone.utc).isoformat()
    county_aggs: dict[tuple[str, str], Aggregate] = defaultdict(Aggregate)
    district_aggs: dict[tuple[str, str, str], Aggregate] = defaultdict(Aggregate)
    csv_rows: list[dict] = []

    for race in races:
        contest_type, scope, district, office = parse_race(str(race.get("race", "")))
        table = race.get("county") or []
        if not table:
            continue
        candidates = [parse_candidate_header(cell) for cell in table[0][1:]]
        for row in table[1:]:
            if not row or not str(row[0]).strip():
                continue
            county_raw = str(row[0]).strip()
            county = county_key(county_raw)
            for idx, (candidate, party) in enumerate(candidates, start=1):
                votes = parse_votes(row[idx] if idx < len(row) else 0)
                if not candidate:
                    continue
                csv_rows.append({
                    "county": county_raw,
                    "precinct_code": "COUNTY_TOTAL",
                    "precinct": "County total (unofficial)",
                    "office": office,
                    "district": district,
                    "candidate": candidate,
                    "party": party,
                    "votes": votes,
                })
                county_aggs[(contest_type, county)].add(party, candidate, votes)
                if scope:
                    district_aggs[(scope, contest_type, district)].add(party, candidate, votes)

    raw_path = DATA / "20261103__mo__general__county.csv"
    with raw_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "county", "precinct_code", "precinct", "office", "district", "candidate", "party", "votes"
        ])
        writer.writeheader()
        writer.writerows(csv_rows)

    contest_entries = []
    for contest_type in sorted({key[0] for key in county_aggs}):
        rows = []
        for (_, county), agg in sorted(county_aggs.items()):
            if _ != contest_type:
                continue
            rows.append({"county": county, **agg.result()})
        file_name = f"{contest_type}_2026.json"
        meta = {
            "contest_type": contest_type,
            "year": 2026,
            "rows": len(rows),
            "generated_at": generated_at,
            "source": "missouri_sos_enr",
            "source_url": SOURCE_URL,
            "status": SOURCE_STATUS,
        }
        (CONTESTS / file_name).write_text(json.dumps({"meta": meta, "rows": rows}, indent=2) + "\n", encoding="utf-8")
        dem_total = sum(int(row["dem_votes"]) for row in rows)
        rep_total = sum(int(row["rep_votes"]) for row in rows)
        contest_entries.append({
            "contest_type": contest_type, "year": 2026, "file": file_name, "rows": len(rows),
            "dem_total": dem_total, "rep_total": rep_total,
            "major_party_contested": bool(dem_total and rep_total),
            "source": "missouri_sos_enr", "status": SOURCE_STATUS,
        })

    district_entries = []
    grouped: dict[tuple[str, str], dict[str, Aggregate]] = defaultdict(dict)
    for (scope, contest_type, district), agg in district_aggs.items():
        grouped[(scope, contest_type)][district] = agg
    for (scope, contest_type), districts in sorted(grouped.items()):
        results = {district: districts[district].result() for district in sorted(districts, key=int)}
        file_name = f"{scope}_{contest_type}_2026.json"
        meta = {
            "scope": scope, "contest_type": contest_type, "year": 2026,
            "district_count": len(results), "match_coverage_pct": 100.0,
            "generated_at": generated_at, "source": "missouri_sos_enr",
            "source_url": SOURCE_URL, "status": SOURCE_STATUS,
            "lines_year": 2022 if scope == "congressional" else 2022,
        }
        (DISTRICTS / file_name).write_text(
            json.dumps({"meta": meta, "general": {"results": results}}, indent=2) + "\n",
            encoding="utf-8",
        )
        dem_total = sum(int(row["dem_votes"]) for row in results.values())
        rep_total = sum(int(row["rep_votes"]) for row in results.values())
        district_entries.append({
            "scope": scope, "contest_type": contest_type, "year": 2026, "file": file_name,
            "districts": len(results), "rows": len(results), "dem_total": dem_total,
            "rep_total": rep_total, "major_party_contested": bool(dem_total and rep_total),
            "match_coverage_pct": 100.0, "source": "missouri_sos_enr",
            "status": SOURCE_STATUS, "lines_year": 2022,
        })

    update_manifest(CONTESTS / "manifest.json", contest_entries)
    statewide = [entry for entry in contest_entries if entry["contest_type"] == "auditor"]
    update_manifest(CONTESTS / "manifest_statewide_contested.json", statewide)
    update_manifest(DISTRICTS / "manifest.json", district_entries)
    return {"races": len(races), "csv_rows": len(csv_rows), "contest_slices": len(contest_entries), "district_slices": len(district_entries)}


def payload_from_source_csv(path: Path) -> list[dict]:
    """Convert the public ENR snapshot to the same table shape as the browser capture."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    party_lookup: dict[tuple[str, str], str] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        source_rows = list(csv.DictReader(handle))
        for row in source_rows:
            contest = (row.get("contest") or "").strip()
            candidate_key = re.sub(r"[^A-Z0-9]", "", (row.get("candidate") or "").upper())
            party = (row.get("party") or "").strip()
            if party:
                party_lookup[(contest, candidate_key)] = party
        for row in source_rows:
            contest = (row.get("contest") or "").strip()
            if (row.get("level") or "").strip() != "county":
                continue
            if contest != "State Auditor" and not contest.startswith("U.S. Representative - District "):
                continue
            if not (row.get("party") or "").strip():
                candidate_key = re.sub(r"[^A-Z0-9]", "", (row.get("candidate") or "").upper())
                row["party"] = party_lookup.get((contest, candidate_key), "")
            grouped[contest].append(row)

    races = []
    for contest, rows in grouped.items():
        candidates: list[tuple[str, str]] = []
        for row in rows:
            item = ((row.get("candidate") or "").strip(), (row.get("party") or "").strip())
            if item not in candidates:
                candidates.append(item)
        party_codes = {
            "Democratic": "DEM", "Republican": "REP", "Libertarian": "LIB",
            "Constitution": "CON", "Green": "GRN", "Independent": "IND",
        }
        header = ["County"] + [f"{name}, {party_codes.get(party, party.upper()[:3])}" for name, party in candidates]
        by_county: dict[str, dict[tuple[str, str], str]] = defaultdict(dict)
        for row in rows:
            key = ((row.get("candidate") or "").strip(), (row.get("party") or "").strip())
            by_county[(row.get("county") or "").strip()][key] = (row.get("votes") or "0").strip()
        table = [header, [""] * len(header)]
        for county in sorted(by_county):
            table.append([county] + [by_county[county].get(candidate, "0") for candidate in candidates])
        races.append({"race": contest, "county": table})
    return races


HTML = b"""<!doctype html><meta charset=utf-8><title>2026 importer</title>
<form method=post><textarea name=payload style=\"width:95vw;height:80vh\"></textarea><button>Import</button></form>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(HTML)))
        self.end_headers()
        self.wfile.write(HTML)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        from urllib.parse import parse_qs
        races = json.loads(parse_qs(body)["payload"][0])
        result = import_payload(races)
        response = json.dumps(result).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)
        self.server.shutdown_requested = True

    def log_message(self, fmt: str, *args: object) -> None:
        print(fmt % args)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("json_file", nargs="?")
    args = parser.parse_args()
    if args.serve:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
        print(f"Listening on http://127.0.0.1:{args.port}", flush=True)
        while not getattr(server, "shutdown_requested", False):
            server.handle_request()
        return
    if not args.json_file:
        parser.error("provide --serve or a JSON file")
    source_path = Path(args.json_file)
    races = payload_from_source_csv(source_path) if source_path.suffix.lower() == ".csv" else json.loads(source_path.read_text(encoding="utf-8"))
    print(json.dumps(import_payload(races)))


if __name__ == "__main__":
    main()
