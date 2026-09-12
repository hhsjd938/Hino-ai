#!/usr/bin/env python3
"""Analyze driving behavior for trips belonging to the same vehicle and route."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import openpyxl


CORE_COLUMNS = [
    "trip_id",
    "route_group_id",
    "vehicle_id",
    "distance_km",
    "duration_min",
    "fuel_l",
    "l_per_100km",
    "idle_minutes",
    "idle_ratio",
    "high_rpm_minutes",
    "high_rpm_ratio",
    "high_rpm_low_speed_minutes",
    "rapid_accel_count",
    "rapid_decel_count",
    "stop_go_count",
    "speed_std",
    "fuel_deviation_pct",
    "time_deviation_pct",
]

QUALITY_COLUMNS = [
    "group_trip_count",
    "direct_pair_density",
    "group_quality",
    "valid_duration_min",
    "idle_coverage_ratio",
    "rpm_coverage_ratio",
    "speed_coverage_ratio",
    "rpm_speed_joint_coverage_ratio",
    "invalid_interval_count",
    "long_gap_count",
]


@dataclass(frozen=True)
class Thresholds:
    high_rpm: float = 2000.0
    low_speed: float = 20.0
    stop_speed: float = 3.0
    go_speed: float = 10.0
    max_gap_seconds: float = 120.0


@dataclass(frozen=True)
class Observation:
    timestamp: datetime
    car_status: float | None
    mileage: float | None
    speed: float | None
    fuel: float | None
    rpm: float | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare behavior across HINO trips from the same vehicle and route group."
    )
    parser.add_argument("--source", type=Path, required=True, help="Path to the source XLSX workbook")
    parser.add_argument("--route-members", type=Path, required=True, help="Path to route_group_members.csv")
    parser.add_argument("--route-groups", type=Path, required=True, help="Path to route_groups.csv")
    parser.add_argument("--output-dir", type=Path, required=True, help="Directory for CSV, JSON, and HTML outputs")
    parser.add_argument("--high-rpm", type=float, default=2000.0)
    parser.add_argument("--low-speed", type=float, default=20.0)
    parser.add_argument("--stop-speed", type=float, default=3.0)
    parser.add_argument("--go-speed", type=float, default=10.0)
    parser.add_argument("--max-gap-seconds", type=float, default=120.0)
    return parser.parse_args()


def normalize_id(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def validate_thresholds(thresholds: Thresholds) -> None:
    if thresholds.high_rpm <= 0:
        raise ValueError("--high-rpm must be positive")
    if thresholds.low_speed < 0 or thresholds.stop_speed < 0:
        raise ValueError("Speed thresholds cannot be negative")
    if thresholds.go_speed <= thresholds.stop_speed:
        raise ValueError("--go-speed must be greater than --stop-speed")
    if thresholds.max_gap_seconds <= 0:
        raise ValueError("--max-gap-seconds must be positive")


def load_route_inputs(
    members_path: Path, groups_path: Path
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    members = read_csv_rows(members_path)
    groups = read_csv_rows(groups_path)
    member_required = {"route_group", "vehicle", "journey", "km", "fuel_l", "l_per_100km"}
    group_required = {"route_group", "vehicle", "trip_count", "direct_pair_density"}
    if not members:
        raise ValueError("route_group_members.csv is empty")
    if not groups:
        raise ValueError("route_groups.csv is empty")
    for label, rows, required in (
        ("route members", members, member_required),
        ("route groups", groups, group_required),
    ):
        missing = required.difference(rows[0])
        if missing:
            raise ValueError(f"Missing {label} columns: {', '.join(sorted(missing))}")

    normalized_members: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in members:
        vehicle = normalize_id(row["vehicle"])
        journey = normalize_id(row["journey"])
        key = (vehicle, journey)
        if not vehicle or not journey:
            raise ValueError("Route member contains an empty vehicle or journey ID")
        if key in seen:
            raise ValueError(f"Duplicate route member: {vehicle}/{journey}")
        seen.add(key)
        normalized_members.append(
            {
                **row,
                "vehicle": vehicle,
                "journey": journey,
                "km": number(row["km"]),
                "fuel_l": number(row["fuel_l"]),
                "l_per_100km": number(row["l_per_100km"]),
            }
        )

    group_map: dict[str, dict[str, Any]] = {}
    for row in groups:
        group_id = row["route_group"].strip()
        if group_id in group_map:
            raise ValueError(f"Duplicate route group: {group_id}")
        density = number(row["direct_pair_density"])
        group_map[group_id] = {
            **row,
            "vehicle": normalize_id(row["vehicle"]),
            "trip_count": int(float(row["trip_count"])),
            "direct_pair_density": density,
            "group_quality": "complete_pair" if density is not None and math.isclose(density, 1.0) else "chain_connected",
        }

    member_counts = Counter(row["route_group"] for row in normalized_members)
    for row in normalized_members:
        group_id = row["route_group"]
        if group_id not in group_map:
            raise ValueError(f"Route member references an unknown group: {group_id}")
        if row["vehicle"] != group_map[group_id]["vehicle"]:
            raise ValueError(f"Vehicle mismatch in route group {group_id}")
    for group_id, count in member_counts.items():
        if count != group_map[group_id]["trip_count"]:
            raise ValueError(f"Trip count mismatch in {group_id}: members={count}, summary={group_map[group_id]['trip_count']}")
    return normalized_members, group_map


def required_workbook_columns() -> list[str]:
    columns = [
        "journeyCode",
        "time",
        "carStatus",
        "enabledCode",
        "can.totalMileage",
        "can.canSpeed",
        "can.engine.totalFuelUsed",
        "can.engine.rpm",
    ]
    for slot in range(3):
        columns.extend((f"event[{slot}].type", f"event[{slot}].startTime"))
    return columns


def add_rapid_event(
    event_sets: dict[tuple[str, str], dict[str, set[str]]],
    key: tuple[str, str],
    event_type: Any,
    start_time: Any,
    seen_events: set[tuple[str, str, str]] | None = None,
) -> bool:
    event_id = normalize_id(event_type)
    if event_id not in {"6", "7"}:
        return False
    start = parse_datetime(start_time)
    if start is None:
        return True
    start_id = start.isoformat()
    global_key = (key[0], event_id, start_id)
    if seen_events is not None:
        if global_key in seen_events:
            return False
        seen_events.add(global_key)
    event_sets[key][event_id].add(start_id)
    return False


def stream_selected_trips(
    source: Path,
    wanted: set[tuple[str, str]],
) -> tuple[dict[tuple[str, str], list[Observation]], dict[tuple[str, str], dict[str, set[str]]], dict[str, int]]:
    workbook = openpyxl.load_workbook(source, read_only=True, data_only=True)
    sheet = workbook.worksheets[0]
    rows = sheet.iter_rows(values_only=True)
    try:
        header = [str(value) if value is not None else "" for value in next(rows)]
    except StopIteration as exc:
        workbook.close()
        raise ValueError("Source workbook has no rows") from exc
    indexes = {name: i for i, name in enumerate(header)}
    missing = set(required_workbook_columns()).difference(indexes)
    if missing:
        workbook.close()
        raise ValueError(f"Missing workbook columns: {', '.join(sorted(missing))}")

    points: dict[tuple[str, str], list[Observation]] = defaultdict(list)
    events: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(lambda: {"6": set(), "7": set()})
    seen_events: set[tuple[str, str, str]] = set()
    stats = {"source_rows_scanned": 0, "selected_rows": 0, "selected_rows_invalid_time": 0, "rapid_event_reports_missing_start": 0}
    for row in rows:
        stats["source_rows_scanned"] += 1
        key = (
            normalize_id(row[indexes["enabledCode"]]),
            normalize_id(row[indexes["journeyCode"]]),
        )
        if key not in wanted:
            continue
        stats["selected_rows"] += 1
        timestamp = parse_datetime(row[indexes["time"]])
        if timestamp is None:
            stats["selected_rows_invalid_time"] += 1
        else:
            points[key].append(
                Observation(
                    timestamp=timestamp,
                    car_status=number(row[indexes["carStatus"]]),
                    mileage=number(row[indexes["can.totalMileage"]]),
                    speed=number(row[indexes["can.canSpeed"]]),
                    fuel=number(row[indexes["can.engine.totalFuelUsed"]]),
                    rpm=number(row[indexes["can.engine.rpm"]]),
                )
            )
        for slot in range(3):
            missing_start = add_rapid_event(
                events,
                key,
                row[indexes[f"event[{slot}].type"]],
                row[indexes[f"event[{slot}].startTime"]],
                seen_events,
            )
            stats["rapid_event_reports_missing_start"] += int(missing_start)
    workbook.close()
    return points, events, stats


def endpoint_delta(points: list[Observation], attribute: str) -> float | None:
    values = [getattr(point, attribute) for point in points]
    if not values or values[0] is None or values[-1] is None:
        return None
    return float(values[-1] - values[0])


def stop_go_count(points: list[Observation], thresholds: Thresholds) -> int:
    phase = "seeking_moving"
    count = 0
    previous: Observation | None = None
    for point in points:
        if previous is not None:
            gap = (point.timestamp - previous.timestamp).total_seconds()
            if gap <= 0 or gap > thresholds.max_gap_seconds:
                phase = "seeking_moving"
        speed = point.speed
        if speed is not None:
            if speed >= thresholds.go_speed:
                if phase == "stopped_after_moving":
                    count += 1
                phase = "moving"
            elif speed <= thresholds.stop_speed and phase == "moving":
                phase = "stopped_after_moving"
        previous = point
    return count


def safe_ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator > 0 else None


def compute_trip_metrics(
    points: Iterable[Observation],
    rapid_events: dict[str, set[str]],
    thresholds: Thresholds,
) -> dict[str, Any]:
    ordered = sorted(points, key=lambda point: point.timestamp)
    if len(ordered) < 2:
        raise ValueError("Trip needs at least two valid timestamped observations")

    valid_seconds = 0.0
    idle_seconds = 0.0
    state_seconds = 0.0
    high_rpm_seconds = 0.0
    rpm_seconds = 0.0
    high_rpm_low_speed_seconds = 0.0
    joint_seconds = 0.0
    speed_seconds = 0.0
    speed_weighted_sum = 0.0
    speed_squared_weighted_sum = 0.0
    invalid_intervals = 0
    long_gaps = 0

    for left, right in zip(ordered, ordered[1:]):
        seconds = (right.timestamp - left.timestamp).total_seconds()
        if seconds <= 0:
            invalid_intervals += 1
            continue
        if seconds > thresholds.max_gap_seconds:
            long_gaps += 1
            continue
        valid_seconds += seconds
        if left.car_status is not None:
            state_seconds += seconds
            if left.car_status == 2:
                idle_seconds += seconds
        if left.rpm is not None:
            rpm_seconds += seconds
            if left.rpm >= thresholds.high_rpm:
                high_rpm_seconds += seconds
        if left.rpm is not None and left.speed is not None:
            joint_seconds += seconds
            if left.rpm >= thresholds.high_rpm and left.speed <= thresholds.low_speed:
                high_rpm_low_speed_seconds += seconds
        if left.speed is not None:
            speed_seconds += seconds
            speed_weighted_sum += left.speed * seconds
            speed_squared_weighted_sum += left.speed * left.speed * seconds

    speed_std = None
    if speed_seconds > 0:
        mean_speed = speed_weighted_sum / speed_seconds
        variance = max(0.0, speed_squared_weighted_sum / speed_seconds - mean_speed * mean_speed)
        speed_std = math.sqrt(variance)

    distance = endpoint_delta(ordered, "mileage")
    fuel = endpoint_delta(ordered, "fuel")
    elapsed = (ordered[-1].timestamp - ordered[0].timestamp).total_seconds()
    return {
        "distance_km": distance,
        "duration_min": elapsed / 60.0,
        "fuel_l": fuel,
        "l_per_100km": fuel / distance * 100 if fuel is not None and distance is not None and distance > 0 else None,
        "idle_minutes": idle_seconds / 60.0,
        "idle_ratio": safe_ratio(idle_seconds, state_seconds),
        "high_rpm_minutes": high_rpm_seconds / 60.0,
        "high_rpm_ratio": safe_ratio(high_rpm_seconds, rpm_seconds),
        "high_rpm_low_speed_minutes": high_rpm_low_speed_seconds / 60.0,
        "rapid_accel_count": len(rapid_events.get("6", set())),
        "rapid_decel_count": len(rapid_events.get("7", set())),
        "stop_go_count": stop_go_count(ordered, thresholds),
        "speed_std": speed_std,
        "valid_duration_min": valid_seconds / 60.0,
        "idle_coverage_ratio": safe_ratio(state_seconds, valid_seconds),
        "rpm_coverage_ratio": safe_ratio(rpm_seconds, valid_seconds),
        "speed_coverage_ratio": safe_ratio(speed_seconds, valid_seconds),
        "rpm_speed_joint_coverage_ratio": safe_ratio(joint_seconds, valid_seconds),
        "invalid_interval_count": invalid_intervals,
        "long_gap_count": long_gaps,
    }


def add_group_deviations(rows: list[dict[str, Any]]) -> None:
    by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_group[row["route_group_id"]].append(row)
    for group_rows in by_group.values():
        fuel_values = [float(row["fuel_l"]) for row in group_rows if row["fuel_l"] is not None]
        time_values = [float(row["duration_min"]) for row in group_rows if row["duration_min"] is not None]
        fuel_median = statistics.median(fuel_values) if fuel_values else None
        time_median = statistics.median(time_values) if time_values else None
        for row in group_rows:
            row["fuel_deviation_pct"] = (
                (row["fuel_l"] / fuel_median - 1) * 100
                if row["fuel_l"] is not None and fuel_median not in (None, 0)
                else None
            )
            row["time_deviation_pct"] = (
                (row["duration_min"] / time_median - 1) * 100
                if row["duration_min"] is not None and time_median not in (None, 0)
                else None
            )


def compare_value(name: str, computed: float | None, reference: float | None, tolerance: float = 1e-6) -> float | None:
    if computed is None or reference is None:
        return None
    return abs(computed - reference)


def write_metrics(path: Path, rows: list[dict[str, Any]]) -> None:
    columns = CORE_COLUMNS + QUALITY_COLUMNS
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column) for column in columns})


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def describe_values(values: Iterable[float | None]) -> dict[str, float | None]:
    valid = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return {
        "minimum": min(valid) if valid else None,
        "median": statistics.median(valid) if valid else None,
        "maximum": max(valid) if valid else None,
    }


def build_report(rows: list[dict[str, Any]], thresholds: Thresholds, summary: dict[str, Any]) -> str:
    payload = json.dumps(json_safe(rows), ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    config = json.dumps(asdict(thresholds), ensure_ascii=False, separators=(",", ":"))
    generated = html.escape(summary["generated_at"])
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HINO Trip Behavior Report</title>
<style>
:root{{--bg:#f4f7f5;--panel:#fff;--ink:#18312a;--muted:#64766f;--line:#dbe5e0;--brand:#007a5e;--good:#16825d;--bad:#c4473b;--warn:#a36a00}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}
header{{background:linear-gradient(120deg,#123d32,#007a5e);color:white;padding:28px max(24px,calc((100vw - 1320px)/2))}}
header h1{{margin:0 0 6px;font-size:28px}} header p{{margin:0;color:#d9eee7}}
main{{max-width:1320px;margin:0 auto;padding:24px}} .toolbar,.panel{{background:var(--panel);border:1px solid var(--line);border-radius:14px;box-shadow:0 5px 18px #16372e0c}}
.toolbar{{padding:16px;display:flex;gap:16px;align-items:end;flex-wrap:wrap}} label{{display:grid;gap:5px;color:var(--muted);font-weight:650}}
select{{min-width:220px;border:1px solid #b9c9c2;border-radius:8px;padding:9px;background:white;color:var(--ink)}}
.quality{{margin-left:auto;padding:7px 10px;border-radius:999px;font-weight:700}} .complete_pair{{background:#daf3e8;color:#0c6a4a}} .chain_connected{{background:#fff0ca;color:#815000}}
.cards{{display:grid;grid-template-columns:repeat(5,minmax(150px,1fr));gap:12px;margin:16px 0}} .card{{background:white;border:1px solid var(--line);border-radius:12px;padding:14px}}
.card .label{{color:var(--muted);font-size:12px}} .card .value{{font-size:24px;font-weight:750;margin-top:3px}}
.panel{{padding:16px;overflow:auto}} table{{width:100%;border-collapse:collapse;white-space:nowrap}} th,td{{padding:9px 10px;border-bottom:1px solid #e6ece9;text-align:right}}
th{{position:sticky;top:0;background:#eef5f2;color:#385b50;font-size:12px}} th:first-child,td:first-child{{text-align:left}} tr:hover{{background:#f5faf8}}
.good{{color:var(--good);font-weight:700}} .bad{{color:var(--bad);font-weight:700}} .bar{{display:inline-block;height:7px;background:#65ad97;border-radius:6px;margin-left:7px;vertical-align:middle}}
.note{{margin:16px 2px;color:var(--muted)}} code{{background:#e8efec;padding:2px 5px;border-radius:5px}} @media(max-width:900px){{.cards{{grid-template-columns:repeat(2,1fr)}}.quality{{margin-left:0}}}}
</style>
</head>
<body>
<header><h1>同車同路徑 Trip Behavior</h1><p>比較相同 route group 內每一趟的油耗、時間與駕駛行為</p></header>
<main>
  <section class="toolbar">
    <label>車輛<select id="vehicle"></select></label>
    <label>路徑群組<select id="group"></select></label>
    <span id="quality" class="quality"></span>
  </section>
  <section id="cards" class="cards"></section>
  <section class="panel"><table><thead><tr>
    <th>Trip</th><th>時間 min</th><th>油耗 L</th><th>L/100km</th><th>油耗偏差</th><th>時間偏差</th><th>怠速 min</th><th>怠速率</th><th>高 RPM min</th><th>高 RPM率</th><th>高RPM低速 min</th><th>急加速</th><th>急減速</th><th>Stop-go</th><th>速度σ</th>
  </tr></thead><tbody id="rows"></tbody></table></section>
  <p class="note">門檻：高 RPM ≥ {thresholds.high_rpm:g}、低速 ≤ {thresholds.low_speed:g} km/h、stop-go 為 ≥ {thresholds.go_speed:g} → ≤ {thresholds.stop_speed:g} → ≥ {thresholds.go_speed:g} km/h。正偏差表示高於同群組中位數。非完全配對群組可能包含僅透過其他行程間接連接的趟次。</p>
  <p class="note">本報告呈現關聯比較，不代表怠速、RPM 或 stop-go 單獨造成油耗差異。產生時間：{generated}</p>
</main>
<script>
const DATA={payload}; const CONFIG={config};
const vehicle=document.querySelector('#vehicle'),group=document.querySelector('#group'),tbody=document.querySelector('#rows'),cards=document.querySelector('#cards'),quality=document.querySelector('#quality');
const uniq=a=>[...new Set(a)].sort(); const fmt=(v,n=1)=>v==null?'—':Number(v).toFixed(n); const pct=v=>v==null?'—':`${{(v*100).toFixed(1)}}%`;
function deviation(v){{if(v==null)return '—';const cls=v>0?'bad':v<0?'good':'';return `<span class="${{cls}}">${{v>0?'+':''}}${{fmt(v)}}%</span>`}}
function options(el,items){{el.innerHTML=items.map(x=>`<option value="${{x}}">${{x}}</option>`).join('')}}
function updateGroups(){{const groups=uniq(DATA.filter(x=>x.vehicle_id===vehicle.value).map(x=>x.route_group_id));options(group,groups);render()}}
function render(){{const rows=DATA.filter(x=>x.route_group_id===group.value).sort((a,b)=>a.fuel_deviation_pct-b.fuel_deviation_pct);if(!rows.length)return;const r=rows[0];quality.className=`quality ${{r.group_quality}}`;quality.textContent=r.group_quality==='complete_pair'?'完整配對群組':'鏈式連接群組';
 const median=k=>{{const a=rows.map(x=>x[k]).filter(x=>x!=null).sort((a,b)=>a-b);const n=a.length;return n?n%2?a[(n-1)/2]:(a[n/2-1]+a[n/2])/2:null}};
 cards.innerHTML=[['群組趟數',rows.length,''],['中位時間',fmt(median('duration_min')),' min'],['中位油耗',fmt(median('fuel_l'),2),' L'],['中位怠速',fmt(median('idle_minutes')),' min'],['中位 Stop-go',fmt(median('stop_go_count'),0),' 次']].map(x=>`<div class="card"><div class="label">${{x[0]}}</div><div class="value">${{x[1]}}<small>${{x[2]}}</small></div></div>`).join('');
 const max=k=>Math.max(1,...rows.map(x=>Number(x[k])||0));const idleMax=max('idle_minutes'),rpmMax=max('high_rpm_minutes');
 tbody.innerHTML=rows.map(x=>`<tr><td>${{x.trip_id}}</td><td>${{fmt(x.duration_min)}}</td><td>${{fmt(x.fuel_l,2)}}</td><td>${{fmt(x.l_per_100km,2)}}</td><td>${{deviation(x.fuel_deviation_pct)}}</td><td>${{deviation(x.time_deviation_pct)}}</td><td>${{fmt(x.idle_minutes)}}<span class="bar" style="width:${{50*x.idle_minutes/idleMax}}px"></span></td><td>${{pct(x.idle_ratio)}}</td><td>${{fmt(x.high_rpm_minutes)}}<span class="bar" style="width:${{50*x.high_rpm_minutes/rpmMax}}px"></span></td><td>${{pct(x.high_rpm_ratio)}}</td><td>${{fmt(x.high_rpm_low_speed_minutes)}}</td><td>${{x.rapid_accel_count}}</td><td>${{x.rapid_decel_count}}</td><td>${{x.stop_go_count}}</td><td>${{fmt(x.speed_std)}}</td></tr>`).join('');}}
options(vehicle,uniq(DATA.map(x=>x.vehicle_id)));vehicle.addEventListener('change',updateGroups);group.addEventListener('change',render);updateGroups();
</script>
</body></html>"""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    args = parse_args()
    thresholds = Thresholds(
        high_rpm=args.high_rpm,
        low_speed=args.low_speed,
        stop_speed=args.stop_speed,
        go_speed=args.go_speed,
        max_gap_seconds=args.max_gap_seconds,
    )
    validate_thresholds(thresholds)
    source = args.source.resolve()
    members_path = args.route_members.resolve()
    groups_path = args.route_groups.resolve()
    for path in (source, members_path, groups_path):
        if not path.is_file():
            raise SystemExit(f"Input file not found: {path}")

    members, group_map = load_route_inputs(members_path, groups_path)
    wanted = {(row["vehicle"], row["journey"]) for row in members}
    points, events, scan_stats = stream_selected_trips(source, wanted)
    missing_trips = sorted(wanted.difference(points))
    if missing_trips:
        sample = ", ".join(f"{v}/{t}" for v, t in missing_trips[:10])
        raise SystemExit(f"{len(missing_trips)} route-member trips were not found in the workbook: {sample}")

    output_rows: list[dict[str, Any]] = []
    reconciliation = {"distance_mismatch_count": 0, "fuel_mismatch_count": 0, "l_per_100km_mismatch_count": 0, "max_distance_abs_delta": 0.0, "max_fuel_abs_delta": 0.0, "max_l_per_100km_abs_delta": 0.0}
    for member in members:
        key = (member["vehicle"], member["journey"])
        metrics = compute_trip_metrics(points[key], events[key], thresholds)
        group = group_map[member["route_group"]]
        row = {
            "trip_id": member["journey"],
            "route_group_id": member["route_group"],
            "vehicle_id": member["vehicle"],
            **metrics,
            "group_trip_count": group["trip_count"],
            "direct_pair_density": group["direct_pair_density"],
            "group_quality": group["group_quality"],
        }
        for output_name, reference_name, count_name, max_name in (
            ("distance_km", "km", "distance_mismatch_count", "max_distance_abs_delta"),
            ("fuel_l", "fuel_l", "fuel_mismatch_count", "max_fuel_abs_delta"),
            ("l_per_100km", "l_per_100km", "l_per_100km_mismatch_count", "max_l_per_100km_abs_delta"),
        ):
            delta = compare_value(output_name, row[output_name], member[reference_name])
            if delta is not None:
                reconciliation[max_name] = max(reconciliation[max_name], delta)
                reconciliation[count_name] += int(delta > 1e-6)
        output_rows.append(row)

    if any(reconciliation[name] for name in ("distance_mismatch_count", "fuel_mismatch_count", "l_per_100km_mismatch_count")):
        raise SystemExit(f"Workbook metrics do not reconcile with route_group_members.csv: {reconciliation}")

    add_group_deviations(output_rows)
    output_rows.sort(key=lambda row: (row["vehicle_id"], row["route_group_id"], row["trip_id"]))
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    metrics_path = output / "trip_behavior_metrics.csv"
    summary_path = output / "trip_behavior_summary.json"
    report_path = output / "trip_behavior_report.html"
    write_metrics(metrics_path, output_rows)

    quality_counts = Counter(row["group_quality"] for row in group_map.values())
    coverage_columns = (
        "idle_coverage_ratio",
        "rpm_coverage_ratio",
        "speed_coverage_ratio",
        "rpm_speed_joint_coverage_ratio",
    )
    summary = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": {"name": source.name, "size_bytes": source.stat().st_size, "sha256": sha256(source)},
        "route_members_source": members_path.name,
        "route_groups_source": groups_path.name,
        "thresholds": asdict(thresholds),
        "definitions": {
            "interval_weighting": "Left observation carried forward; only intervals with 0 < dt <= max_gap_seconds are used.",
            "rapid_accel_count": "Unique event type 6 by vehicle and startTime.",
            "rapid_decel_count": "Unique event type 7 by vehicle and startTime.",
            "stop_go_count": "Complete speed cycle >= go_speed, then <= stop_speed, then >= go_speed; gaps reset the cycle.",
            "deviations": "Percentage difference from the median of the same vehicle-specific route group.",
        },
        "counts": {
            **scan_stats,
            "vehicles": len({row["vehicle_id"] for row in output_rows}),
            "route_groups": len(group_map),
            "trips": len(output_rows),
            "complete_pair_groups": quality_counts["complete_pair"],
            "chain_connected_groups": quality_counts["chain_connected"],
        },
        "reconciliation": reconciliation,
        "excluded_intervals": {
            "nonpositive_seconds": sum(row["invalid_interval_count"] for row in output_rows),
            "above_max_gap_seconds": sum(row["long_gap_count"] for row in output_rows),
        },
        "signal_coverage": {
            column: describe_values(row[column] for row in output_rows)
            for column in coverage_columns
        },
        "missing_signal_counts": {
            column: sum(row[column] is None for row in output_rows)
            for column in ("distance_km", "duration_min", "fuel_l", "l_per_100km", "idle_ratio", "high_rpm_ratio", "speed_std")
        },
        "outputs": [metrics_path.name, report_path.name, summary_path.name],
        "limitation": "Comparisons are observational associations and do not isolate load, traffic, weather, driver, or causal effects.",
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(build_report(output_rows, thresholds, summary), encoding="utf-8")
    print(json.dumps({"outputs": summary["outputs"], "counts": summary["counts"], "reconciliation": reconciliation}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
