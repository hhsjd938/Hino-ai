#!/usr/bin/env python3
"""Build an interactive map of repeated routes and inferred stops.

The source workbook is streamed directly from its XLSX archive. Only journeys
listed in route_group_members.csv are retained, so the workbook is never
modified and no raw-data cache is created.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET


NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
CELL_REF = re.compile(r"([A-Z]+)")
KEEP_COLUMNS = {
    "B": "journey",
    "C": "time",
    "E": "vehicle",
    "G": "longitude",
    "H": "latitude",
    "N": "can_speed",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--members", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stop-speed", type=float, default=3.0)
    parser.add_argument("--stop-minutes", type=float, default=3.0)
    parser.add_argument("--max-gap-seconds", type=float, default=120.0)
    parser.add_argument("--endpoint-radius-meters", type=float, default=300.0)
    return parser.parse_args()


def shared_strings(book: zipfile.ZipFile) -> list[str]:
    values: list[str] = []
    with book.open("xl/sharedStrings.xml") as source:
        for _, elem in ET.iterparse(source, events=("end",)):
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
    return value.text


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def parse_time(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def valid_coordinate(lon, lat) -> bool:
    return lon is not None and lat is not None and 119 <= lon <= 123 and 21 <= lat <= 26


def haversine_m(a: dict, b: dict) -> float:
    radius = 6_371_000
    lat1, lat2 = math.radians(a["lat"]), math.radians(b["lat"])
    dlat = lat2 - lat1
    dlon = math.radians(b["lon"] - a["lon"])
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(h))


def downsample(points: list[dict]) -> list[dict]:
    if len(points) <= 2:
        return points
    kept = [points[0]]
    for point in points[1:-1]:
        elapsed = (point["dt"] - kept[-1]["dt"]).total_seconds()
        if elapsed >= 60 or haversine_m(point, kept[-1]) >= 100:
            kept.append(point)
    kept.append(points[-1])
    return kept


def inferred_stops(points: list[dict], speed_limit: float, min_seconds: float, max_gap: float) -> list[dict]:
    stops: list[dict] = []
    run: list[dict] = []

    def finish() -> None:
        nonlocal run
        if len(run) >= 2:
            duration = (run[-1]["dt"] - run[0]["dt"]).total_seconds()
            if duration >= min_seconds:
                stops.append({
                    "lat": round(statistics.median(p["lat"] for p in run), 6),
                    "lon": round(statistics.median(p["lon"] for p in run), 6),
                    "start": run[0]["dt"].isoformat(sep=" "),
                    "end": run[-1]["dt"].isoformat(sep=" "),
                    "minutes": round(duration / 60, 1),
                })
        run = []

    for point in points:
        slow = point["speed"] is not None and point["speed"] <= speed_limit
        if slow and (not run or (point["dt"] - run[-1]["dt"]).total_seconds() <= max_gap):
            run.append(point)
        else:
            finish()
            if slow:
                run = [point]
    finish()
    return stops


def main() -> None:
    args = parse_args()
    member_rows = list(csv.DictReader(args.members.open(encoding="utf-8-sig", newline="")))
    metadata = {(row["vehicle"], row["journey"]): row for row in member_rows}
    wanted = set(metadata)
    collected: dict[tuple[str, str], list[dict]] = {key: [] for key in wanted}

    with zipfile.ZipFile(args.source.resolve()) as book:
        strings = shared_strings(book)
        with book.open("xl/worksheets/sheet1.xml") as sheet:
            for _, row in ET.iterparse(sheet, events=("end",)):
                if row.tag != NS + "row":
                    continue
                selected = {}
                for cell in row.findall(NS + "c"):
                    match = CELL_REF.match(cell.attrib.get("r", ""))
                    if match and match.group(1) in KEEP_COLUMNS:
                        selected[KEEP_COLUMNS[match.group(1)]] = cell_value(cell, strings)
                row.clear()
                key = (str(selected.get("vehicle") or ""), str(selected.get("journey") or ""))
                if key not in wanted:
                    continue
                dt = parse_time(selected.get("time"))
                lon = number(selected.get("longitude"))
                lat = number(selected.get("latitude"))
                if dt and valid_coordinate(lon, lat):
                    collected[key].append({"dt": dt, "lon": lon, "lat": lat, "speed": number(selected.get("can_speed"))})

    trips = []
    for key, points in collected.items():
        if not points:
            continue
        points.sort(key=lambda p: p["dt"])
        row = metadata[key]
        route_points = downsample(points)
        stops = inferred_stops(points, args.stop_speed, args.stop_minutes * 60, args.max_gap_seconds)
        stops = [
            stop
            for stop in stops
            if haversine_m(stop, points[0]) > args.endpoint_radius_meters
            and haversine_m(stop, points[-1]) > args.endpoint_radius_meters
        ]
        trips.append({
            "group": row["route_group"],
            "vehicle": row["vehicle"],
            "journey": row["journey"],
            "start": row["start"],
            "km": round(float(row["km"]), 1),
            "fuel": round(float(row["fuel_l"]), 1),
            "efficiency": round(float(row["l_per_100km"]), 1),
            "points": [[round(p["lat"], 6), round(p["lon"], 6)] for p in route_points],
            "stops": stops,
        })
    trips.sort(key=lambda t: (t["group"], t["start"]))

    payload = json.dumps(trips, ensure_ascii=False, separators=(",", ":"))
    html = TEMPLATE.replace("__TRIP_DATA__", payload).replace("__STOP_SPEED__", f"{args.stop_speed:g}").replace(
        "__STOP_MINUTES__", f"{args.stop_minutes:g}"
    ).replace("__ENDPOINT_RADIUS__", f"{args.endpoint_radius_meters:g}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html, encoding="utf-8")
    print(json.dumps({"trips": len(trips), "groups": len({t['group'] for t in trips}), "stops": sum(len(t['stops']) for t in trips), "output": str(args.output)}, ensure_ascii=False))


TEMPLATE = r'''<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>HINO 重複路線與疑似停靠點</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    *{box-sizing:border-box} body{margin:0;font-family:"Segoe UI","Microsoft JhengHei",sans-serif;color:#17202a;background:#f3f5f7}
    header{height:68px;padding:12px 18px;background:#fff;border-bottom:1px solid #d8dee4;display:flex;gap:18px;align-items:center;position:relative;z-index:1000}
    h1{font-size:18px;margin:0;white-space:nowrap} .controls{display:flex;gap:10px;flex:1;align-items:end}
    label{font-size:12px;color:#52606d;display:grid;gap:3px} select{min-width:220px;padding:7px 9px;border:1px solid #b8c2cc;border-radius:6px;background:white}
    details.trip-picker{position:relative;min-width:320px;color:#17202a} details.trip-picker summary{list-style:none;cursor:pointer;min-width:320px;padding:7px 30px 7px 9px;border:1px solid #b8c2cc;border-radius:6px;background:white;position:relative} details.trip-picker summary::-webkit-details-marker{display:none} details.trip-picker summary::after{content:'▼';position:absolute;right:9px;color:#52606d;font-size:10px} details.trip-picker[open] summary::after{content:'▲'} .trip-options{position:absolute;z-index:1200;top:100%;left:0;right:0;max-height:360px;overflow:auto;margin-top:3px;padding:6px;background:#fff;border:1px solid #b8c2cc;border-radius:6px;box-shadow:0 5px 16px #0002}.trip-options label{display:flex;grid-template-columns:none;gap:7px;align-items:flex-start;padding:6px 5px;font-size:12px;color:#17202a;cursor:pointer}.trip-options label:hover{background:#f0f4f7}.trip-options input{margin-top:2px;flex:0 0 auto}
    #map{height:calc(100vh - 68px)} #info{position:absolute;z-index:900;top:84px;right:16px;width:300px;background:rgba(255,255,255,.96);padding:14px;border-radius:9px;box-shadow:0 3px 16px #0002;line-height:1.5}
    #info b{font-size:15px} #info .muted{font-size:12px;color:#5c6773;margin-top:7px}.legend{display:flex;gap:12px;margin-top:9px;font-size:12px}.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:4px}.start{background:#16803c}.stop{background:#d97706}.end{background:#c92a2a}
    @media(max-width:760px){header{height:auto;align-items:flex-start;flex-direction:column}.controls{width:100%;flex-direction:column;align-items:stretch}select{width:100%;min-width:0}#map{height:calc(100vh - 166px)}#info{top:180px;right:10px;width:260px}}
  </style>
</head>
<body>
<header><h1>HINO 重複路線與疑似停靠點</h1><div class="controls"><label>路線群組<select id="group"></select></label><label>行程<details id="tripPicker" class="trip-picker"><summary id="tripSummary">行程</summary><div id="tripOptions" class="trip-options"></div></details></label></div></header>
<div id="map"></div><aside id="info"></aside>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const trips=__TRIP_DATA__;
const groupSel=document.getElementById('group'),tripPicker=document.getElementById('tripPicker'),tripSummary=document.getElementById('tripSummary'),tripOptions=document.getElementById('tripOptions'),info=document.getElementById('info');
const selectedJourneys=new Set();
const map=L.map('map',{preferCanvas:true});
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'}).addTo(map);
const layer=L.layerGroup().addTo(map); const palette=['#1769aa','#8e44ad','#00897b','#e65100','#5d4037','#c2185b'];
const groups=[...new Set(trips.map(t=>t.group))]; groups.forEach(g=>groupSel.add(new Option(`${g}（${trips.filter(t=>t.group===g).length} 趟）`,g)));
function currentTrips(){return trips.filter(t=>t.group===groupSel.value)}
function syncTripChecks(){const current=currentTrips();const all=tripOptions.querySelector('input[data-all]');if(all)all.checked=current.length>0&&selectedJourneys.size===current.length;tripOptions.querySelectorAll('input[data-journey]').forEach(input=>{input.checked=selectedJourneys.has(input.value)});tripSummary.textContent=`行程（${selectedJourneys.size}/${current.length} 已選）`}
function renderTripOptions(){const current=currentTrips();selectedJourneys.clear();current.forEach(t=>selectedJourneys.add(t.journey));tripOptions.innerHTML='';const allLabel=document.createElement('label');const allInput=document.createElement('input');allInput.type='checkbox';allInput.dataset.all='true';allLabel.append(allInput,document.createTextNode('全部行程'));tripOptions.append(allLabel);allInput.addEventListener('change',()=>{if(allInput.checked)current.forEach(t=>selectedJourneys.add(t.journey));else selectedJourneys.clear();syncTripChecks();draw()});current.forEach(t=>{const label=document.createElement('label');const input=document.createElement('input');input.type='checkbox';input.value=t.journey;input.dataset.journey='true';label.append(input,document.createTextNode(`${t.start.replace('T',' ')}｜${t.km} km｜${t.efficiency} L/100km`));tripOptions.append(label);input.addEventListener('change',()=>{if(input.checked)selectedJourneys.add(input.value);else selectedJourneys.delete(input.value);syncTripChecks();draw()})});syncTripChecks()}
function marker(point,color,label,popup){return L.circleMarker(point,{radius:7,color:'#fff',weight:2,fillColor:color,fillOpacity:1}).bindTooltip(label).bindPopup(popup)}
function draw(){layer.clearLayers();const shown=currentTrips().filter(t=>selectedJourneys.has(t.journey));const bounds=[];let stopCount=0;
 shown.forEach((t,i)=>{const color=palette[i%palette.length];L.polyline(t.points,{color,weight:shown.length===1?5:3,opacity:shown.length===1?.9:.7}).addTo(layer).bindPopup(`${t.start.replace('T',' ')}<br>${t.km} km｜${t.fuel} L｜${t.efficiency} L/100km`);t.points.forEach(p=>bounds.push(p));
  marker(t.points[0],'#16803c','起點',`起點<br>${t.start.replace('T',' ')}<br>${t.km} km｜${t.fuel} L｜${t.efficiency} L/100km`).addTo(layer);marker(t.points[t.points.length-1],'#c92a2a','終點',`終點<br>${t.start.replace('T',' ')}`).addTo(layer)
  t.stops.forEach((s,n)=>{stopCount++;marker([s.lat,s.lon],'#d97706',`疑似停靠 ${s.minutes} 分鐘`,`疑似停靠點 ${n+1}<br>${s.start}<br>至 ${s.end}<br><b>${s.minutes} 分鐘</b>`).addTo(layer)})});
 if(bounds.length)map.fitBounds(bounds,{padding:[30,30],maxZoom:15});const one=shown.length===1?shown[0]:null;
 info.innerHTML=`<b>${groupSel.value}</b><br>${one?`${one.start.replace('T',' ')}<br>${one.km} km｜${one.fuel} L｜${one.efficiency} L/100km`:`顯示 ${shown.length} 趟歷史行程`}<br>疑似中途停靠：${stopCount}<div class="legend"><span><i class="dot start"></i>起點</span><span><i class="dot stop"></i>中途停靠</span><span><i class="dot end"></i>終點</span></div><div class="muted">疑似中途停靠：CAN 車速 ≤ __STOP_SPEED__ km/h，連續至少 __STOP_MINUTES__ 分鐘，並排除起點與終點周圍 __ENDPOINT_RADIUS__ 公尺。它可能是裝卸貨、休息、號誌或壅塞，無法直接確認為客戶。</div>`}
groupSel.addEventListener('change',()=>{renderTripOptions();draw()});groupSel.value=groups[0];renderTripOptions();draw();
</script></body></html>'''


if __name__ == "__main__":
    main()
