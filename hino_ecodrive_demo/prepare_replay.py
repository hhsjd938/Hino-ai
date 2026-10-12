"""Stream one real trip out of the HINO workbook without changing the source."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
HERE = Path(__file__).resolve().parent
MAPPING = {"time": "timestamp", "can.canSpeed": "speed", "can.engine.rpm": "rpm", "carStatus": "car_status",
           "can.canStatus": "can_status", "can.totalMileage": "mileage", "can.engine.totalFuelUsed": "fuel",
           "gps.speedLimit": "speed_limit", "enabledCode": "vehicle", "journeyCode": "journey",
           "gps.longitude": "longitude", "gps.latitude": "latitude"}


def shared_strings(book):
    if "xl/sharedStrings.xml" not in book.namelist():
        return []
    strings = []
    with book.open("xl/sharedStrings.xml") as stream:
        parser = ET.iterparse(stream, events=("start", "end"))
        _, root = next(parser)
        for event, element in parser:
            if event == "end" and element.tag == NS + "si":
                strings.append("".join(node.text or "" for node in element.iter(NS + "t")))
                root.clear()
    return strings


def cell_value(cell, strings):
    if cell.get("t") == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(NS + "t"))
    value = cell.findtext(NS + "v")
    return strings[int(value)] if cell.get("t") == "s" and value is not None else value


def extract(source: Path, vehicle: str, journey: str) -> tuple[list[dict], int]:
    records = []
    scanned = 0
    with zipfile.ZipFile(source) as book:
        strings = shared_strings(book)
        with book.open("xl/worksheets/sheet1.xml") as stream:
            parser = ET.iterparse(stream, events=("start", "end"))
            _, root = next(parser)
            columns = {}
            sheet_data = None
            for event, row in parser:
                if event == "start" and row.tag == NS + "sheetData":
                    sheet_data = row
                if event != "end" or row.tag != NS + "row":
                    continue
                values = {''.join(c for c in cell.get("r", "") if c.isalpha()): cell_value(cell, strings)
                          for cell in row.findall(NS + "c")}
                if not columns:
                    columns = values
                    if not set(MAPPING).issubset(columns.values()):
                        raise ValueError("第一張工作表缺少 HINO 必要欄位")
                else:
                    scanned += 1
                    record = {columns.get(col): value for col, value in values.items()}
                    if str(record.get("enabledCode")) == vehicle and str(record.get("journeyCode")) == journey:
                        reports = []
                        for slot in range(3):
                            prefix = f"event[{slot}]."
                            report = {key[len(prefix):]: value for key, value in record.items()
                                      if key and key.startswith(prefix) and value not in (None, "")}
                            if report.get("type"):
                                reports.append(report)
                        records.append({**{dest: record.get(src) for src, dest in MAPPING.items()},
                                        "event_reports": json.dumps(reports, ensure_ascii=False),
                                        "source_excel_row": row.get("r")})
                row.clear()
                if sheet_data is not None:
                    sheet_data.clear()
                if scanned and scanned % 100000 == 0:
                    print(f"Scanned {scanned:,} source records", flush=True)
            root.clear()
    if len(records) < 2:
        raise ValueError(f"找不到足夠資料：{vehicle}/{journey}")
    records.sort(key=lambda row: datetime.fromisoformat(row["timestamp"]))
    return records, scanned


def reference(vehicle: str, journey: str, repo: Path) -> dict | None:
    members = repo / "data/processed/analysis/route_group_members.csv"
    if not members.exists():
        return None
    with members.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    target = next((row for row in rows if row["vehicle"] == vehicle and row["journey"] == journey), None)
    if target is None:
        return None
    peers = [row for row in rows if row["vehicle"] == vehicle and row["route_group"] == target["route_group"]]
    return {"route_group": target["route_group"], "trip_count": len(peers),
            "median_fuel_l": statistics.median(float(row["fuel_l"]) for row in peers),
            "median_l_per_100km": statistics.median(float(row["l_per_100km"]) for row in peers),
            "definition": "同車同路線完整群組（包含本趟）的中位數，非因果省油估計"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=HERE.parent / "hino/output data_Hotai_20260511.xlsx")
    parser.add_argument("--vehicle", default="C1N6TWBTVB")
    parser.add_argument("--journey", default="250731030003")
    parser.add_argument("--output", type=Path, default=HERE / "replay.csv")
    args = parser.parse_args()
    records, scanned = extract(args.source, args.vehicle, args.journey)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)
    def digest(path):
        result = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                result.update(chunk)
        return result.hexdigest()
    metadata = {"mode": "real", "source": args.source.name,
                "source_sha256": digest(args.source), "replay_sha256": digest(args.output),
                "vehicle": args.vehicle, "journey": args.journey, "records": len(records),
                "start": records[0]["timestamp"], "end": records[-1]["timestamp"], "source_records_scanned": scanned,
                "reference": reference(args.vehicle, args.journey, HERE.parent)}
    args.output.with_suffix(".meta.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
