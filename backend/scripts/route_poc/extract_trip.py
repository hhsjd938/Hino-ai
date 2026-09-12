#!/usr/bin/env python3
"""從大型 Excel 中擷取一輛車的指定行程，供路線 POC 使用Extract one vehicle and journey from the source XLSX without changing it.

The workbook contains a very large worksheet. This reader streams the worksheet
XML and keeps only the columns needed by the route-planning proof of concept.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import time
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET


NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
CELL_REF = re.compile(r"([A-Z]+)")
KEEP_COLUMNS = {
    "A": "type",
    "B": "journey",
    "C": "time",
    "D": "car_status",
    "E": "vehicle",
    "G": "longitude",
    "H": "latitude",
    "L": "can_status",
    "M": "mileage",
    "N": "can_speed",
    "R": "fuel",
    "T": "rpm",
    "U": "engine_load",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--vehicle", required=True)
    parser.add_argument("--journey", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def shared_strings(book: zipfile.ZipFile) -> list[str]:
    values: list[str] = []
    with book.open("xl/sharedStrings.xml") as source:
        for event, elem in ET.iterparse(source, events=("end",)):
            if elem.tag == NS + "si":
                values.append("".join(node.text or "" for node in elem.iter(NS + "t")))
                elem.clear()
    return values


def cell_value(cell: ET.Element, strings: list[str]):
    kind = cell.attrib.get("t")
    value = cell.find(NS + "v")
    if kind == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(NS + "t"))
    if value is None or value.text is None:
        return None
    if kind == "s":
        return strings[int(value.text)]
    if kind == "b":
        return value.text == "1"
    return value.text


def number(value):
    if value in (None, ""):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def valid_coordinate(lon, lat) -> bool:
    return lon is not None and lat is not None and 119 <= lon <= 123 and 21 <= lat <= 26


def summarize_trip(rows: list[dict]) -> dict | None:
    rows = sorted((row for row in rows if row["timestamp"] is not None), key=lambda row: row["timestamp"])
    if len(rows) < 2:
        return None
    start, end = rows[0], rows[-1]
    if None in (start["mileage"], end["mileage"], start["fuel"], end["fuel"]):
        return None
    distance = end["mileage"] - start["mileage"]
    fuel = end["fuel"] - start["fuel"]
    mileage_values = [row["mileage"] for row in rows if row["mileage"] is not None]
    fuel_values = [row["fuel"] for row in rows if row["fuel"] is not None]
    monotonic = all(b >= a for a, b in zip(mileage_values, mileage_values[1:])) and all(
        b >= a for a, b in zip(fuel_values, fuel_values[1:])
    )
    return {
        "start": start["timestamp"].isoformat(sep=" "),
        "end": end["timestamp"].isoformat(sep=" "),
        "records": len(rows),
        "distance_km": distance,
        "fuel_l": fuel,
        "l_per_100km": fuel / distance * 100 if distance > 0 else None,
        "monotonic": monotonic,
        "can_status_normal": all(row["can_status"] in (None, 0) for row in rows),
    }


def main() -> None:
    args = parse_args()
    started = time.time()
    per_journey: dict[str, list[dict]] = defaultdict(list)
    target_points: list[dict] = []
    target_type = None
    scanned = 0

    with zipfile.ZipFile(args.source) as book:
        strings = shared_strings(book)
        with book.open("xl/worksheets/sheet1.xml") as sheet:
            for event, row in ET.iterparse(sheet, events=("end",)):
                if row.tag != NS + "row":
                    continue
                scanned += 1
                selected = {}
                for cell in row.findall(NS + "c"):
                    match = CELL_REF.match(cell.attrib.get("r", ""))
                    if match and match.group(1) in KEEP_COLUMNS:
                        selected[KEEP_COLUMNS[match.group(1)]] = cell_value(cell, strings)
                row.clear()

                vehicle = str(selected.get("vehicle") or "")
                if vehicle != args.vehicle:
                    continue
                journey = str(selected.get("journey") or "")
                timestamp = parse_time(selected.get("time"))
                record = {
                    "timestamp": timestamp,
                    "mileage": number(selected.get("mileage")),
                    "fuel": number(selected.get("fuel")),
                    "can_status": number(selected.get("can_status")),
                }
                per_journey[journey].append(record)
                if journey == args.journey:
                    target_type = selected.get("type")
                    lon = number(selected.get("longitude"))
                    lat = number(selected.get("latitude"))
                    if timestamp and valid_coordinate(lon, lat):
                        target_points.append(
                            {
                                "time": timestamp.isoformat(sep=" "),
                                "longitude": lon,
                                "latitude": lat,
                                "can_speed": number(selected.get("can_speed")),
                                "rpm": number(selected.get("rpm")),
                                "engine_load": number(selected.get("engine_load")),
                            }
                        )

    target_summary = summarize_trip(per_journey.get(args.journey, []))
    if target_summary is None or not target_points:
        raise SystemExit(f"Target trip not found: {args.vehicle}/{args.journey}")

    target_start = parse_time(target_summary["start"])
    historical = []
    for journey, rows in per_journey.items():
        if journey == args.journey:
            continue
        summary = summarize_trip(rows)
        if not summary:
            continue
        start = parse_time(summary["start"])
        if (
            start
            and target_start
            and start < target_start
            and summary["distance_km"] >= 10
            and summary["fuel_l"] >= 5
            and summary["monotonic"]
            and summary["can_status_normal"]
        ):
            historical.append({"journey": journey, **summary})

    rates = [row["l_per_100km"] for row in historical if row["l_per_100km"] is not None]
    target_points.sort(key=lambda row: row["time"])
    result = {
        "source": str(args.source),
        "vehicle": args.vehicle,
        "journey": args.journey,
        "vehicle_type": int(float(target_type)) if target_type not in (None, "") else None,
        "target": target_summary,
        "origin": {
            "longitude": target_points[0]["longitude"],
            "latitude": target_points[0]["latitude"],
        },
        "destination": {
            "longitude": target_points[-1]["longitude"],
            "latitude": target_points[-1]["latitude"],
        },
        "vehicle_history": {
            "eligible_prior_trips": len(historical),
            "median_l_per_100km": statistics.median(rates) if rates else None,
        },
        "track_points": target_points,
        "rows_scanned": scanned,
        "elapsed_seconds": round(time.time() - started, 3),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "track_points"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
