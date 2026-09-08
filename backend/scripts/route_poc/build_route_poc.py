#!/usr/bin/env python3
"""Build the HINO energy-route proof of concept for one extracted trip.

The script sends only the trip endpoints to a configured Valhalla endpoint,
requests truck routes, obtains elevation profiles, estimates fuel from the
historical similar-route median, and writes JSON plus a standalone HTML report.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_ENDPOINT = "https://valhalla1.openstreetmap.de"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trip-json", type=Path, required=True)
    parser.add_argument("--coverage-csv", type=Path, required=True)
    parser.add_argument("--diesel-price", type=float, default=29.3)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-html", type=Path, required=True)
    parser.add_argument("--height", type=float, default=3.2)
    parser.add_argument("--width", type=float, default=2.5)
    parser.add_argument("--length", type=float, default=8.0)
    parser.add_argument("--weight", type=float, default=12.0)
    parser.add_argument("--axle-load", type=float, default=6.0)
    return parser.parse_args()


def post_json(url: str, payload: dict) -> dict:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "hino-energy-coach-hackathon-poc/0.1",
            "X-Client-Id": "hino-energy-coach-hackathon-poc",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=45) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {detail[:500]}") from exc
    except URLError as exc:
        raise RuntimeError(f"Unable to reach {url}: {exc.reason}") from exc


def decode_polyline6(encoded: str) -> list[list[float]]:
    coordinates = []
    previous = [0, 0]
    index = 0
    while index < len(encoded):
        pair = [0, 0]
        for axis in (0, 1):
            shift = 0
            value = 0
            while True:
                byte = ord(encoded[index]) - 63
                index += 1
                value |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            delta = ~(value >> 1) if value & 1 else value >> 1
            pair[axis] = previous[axis] + delta
            previous[axis] = pair[axis]
        coordinates.append([pair[1] / 1_000_000, pair[0] / 1_000_000])
    return coordinates


def load_reference_median(path: Path, vehicle: str, journey: str) -> tuple[int, int, float]:
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            if row["vehicle"] == vehicle and row["journey"] == journey:
                return (
                    int(row["prior_count"]),
                    int(row["prior_dates"]),
                    float(row["median_prior_l_per_100km"]),
                )
    raise RuntimeError(f"Historical reference row not found for {vehicle}/{journey}")


def trips_from_response(response: dict) -> list[dict]:
    trips = []
    if isinstance(response.get("trip"), dict):
        trips.append(response["trip"])
    for alternate in response.get("alternates") or []:
        if isinstance(alternate, dict):
            trip = alternate.get("trip", alternate)
            if isinstance(trip, dict) and trip.get("legs"):
                trips.append(trip)
    return trips


def encoded_shape(trip: dict) -> str:
    legs = trip.get("legs") or []
    if len(legs) != 1 or not isinstance(legs[0].get("shape"), str):
        raise RuntimeError("POC expects one encoded polyline6 leg per route")
    return legs[0]["shape"]


def elevation_summary(range_height: list[list[float | None]]) -> dict:
    ascent = 0.0
    descent = 0.0
    uphill_bins = {"flat_0_1_km": 0.0, "grade_1_4_km": 0.0, "grade_4_7_km": 0.0, "grade_over_7_km": 0.0}
    valid = [(float(distance), float(height)) for distance, height in range_height if height is not None]
    for (d1, h1), (d2, h2) in zip(valid, valid[1:]):
        run = d2 - d1
        if run <= 0:
            continue
        rise = h2 - h1
        if rise >= 1:
            ascent += rise
        elif rise <= -1:
            descent += -rise
        grade = rise / run * 100
        if grade < 1:
            uphill_bins["flat_0_1_km"] += run / 1000
        elif grade < 4:
            uphill_bins["grade_1_4_km"] += run / 1000
        elif grade < 7:
            uphill_bins["grade_4_7_km"] += run / 1000
        else:
            uphill_bins["grade_over_7_km"] += run / 1000
    return {
        "ascent_m": round(ascent, 1),
        "descent_m": round(descent, 1),
        "start_elevation_m": valid[0][1] if valid else None,
        "end_elevation_m": valid[-1][1] if valid else None,
        "range_height": [[round(d, 1), round(h, 1)] for d, h in valid],
        **{key: round(value, 3) for key, value in uphill_bins.items()},
    }


def build_routes(args: argparse.Namespace, trip_data: dict, median_l_per_100km: float) -> list[dict]:
    origin = trip_data["origin"]
    destination = trip_data["destination"]
    truck = {
        "height": args.height,
        "width": args.width,
        "length": args.length,
        "weight": args.weight,
        "axle_load": args.axle_load,
        "use_highways": 0.5,
        "use_tolls": 0.5,
    }
    payload = {
        "locations": [
            {"lat": origin["latitude"], "lon": origin["longitude"], "type": "break"},
            {"lat": destination["latitude"], "lon": destination["longitude"], "type": "break"},
        ],
        "costing": "truck",
        "costing_options": {"truck": truck},
        "units": "kilometers",
        "language": "zh-TW",
        "directions_type": "none",
        "shape_format": "polyline6",
        "alternates": 2,
    }
    response = post_json(args.endpoint.rstrip("/") + "/route", payload)
    trips = trips_from_response(response)
    if not trips:
        raise RuntimeError("Routing service returned no route")

    routes = []
    for index, trip in enumerate(trips[:3], 1):
        shape = encoded_shape(trip)
        time.sleep(1.05)
        height = post_json(
            args.endpoint.rstrip("/") + "/height",
            {
                "range": True,
                "encoded_polyline": shape,
                "shape_format": "polyline6",
                "resample_distance": 100,
                "height_precision": 1,
            },
        )
        summary = trip["summary"]
        distance_km = float(summary["length"])
        fuel_l = distance_km * median_l_per_100km / 100
        elevation = elevation_summary(height.get("range_height") or [])
        routes.append(
            {
                "id": f"route-{index}",
                "name": "主要路線" if index == 1 else f"替代路線 {index - 1}",
                "distance_km": round(distance_km, 2),
                "duration_minutes": round(float(summary["time"]) / 60, 1),
                "estimated_fuel_l": round(fuel_l, 2),
                "estimated_cost_twd": round(fuel_l * args.diesel_price),
                "coordinates": decode_polyline6(shape),
                **elevation,
            }
        )
    return routes


def write_html(path: Path, result: dict) -> None:
    safe_json = json.dumps(result, ensure_ascii=False).replace("</", "<\\/")
    route_rows = "".join(
        f"<tr><td>{html.escape(route['name'])}</td>"
        f"<td>{route['distance_km']:.2f} km</td>"
        f"<td>{route['duration_minutes']:.1f} 分</td>"
        f"<td>{route['ascent_m']:.0f} m</td>"
        f"<td>{route['estimated_fuel_l']:.2f} L</td>"
        f"<td>NT$ {route['estimated_cost_twd']:,}</td></tr>"
        for route in result["routes"]
    )
    document = f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI 節能導航教練 POC</title>
<style>
:root {{ color-scheme: light dark; font-family: system-ui, "Noto Sans TC", sans-serif; }}
body {{ margin: 0; background: #f3f6f4; color: #17221b; }}
main {{ max-width: 1100px; margin: auto; padding: 32px 20px 48px; }}
h1 {{ margin: 0 0 6px; font-size: 28px; }}
.subtitle {{ color: #526157; margin-bottom: 24px; }}
.grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 20px; }}
.card {{ background: white; border: 1px solid #d9e2dc; border-radius: 12px; padding: 16px; }}
.label {{ color: #65746a; font-size: 13px; }}
.value {{ font-size: 23px; font-weight: 700; margin-top: 4px; }}
.layout {{ display: grid; grid-template-columns: 1.2fr .8fr; gap: 16px; }}
svg {{ width: 100%; min-height: 430px; background: #eef2ef; border-radius: 10px; }}
table {{ width: 100%; border-collapse: collapse; background: white; }}
th, td {{ padding: 11px 9px; border-bottom: 1px solid #e2e8e4; text-align: right; }}
th:first-child, td:first-child {{ text-align: left; }}
.recommend {{ border-left: 5px solid #25894b; }}
.note {{ color: #526157; line-height: 1.6; }}
@media (max-width: 760px) {{ .grid {{ grid-template-columns: 1fr 1fr; }} .layout {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body><main>
<h1>AI 節能導航教練 POC</h1>
<div class="subtitle">真實車輛 {html.escape(result['vehicle'])}・行程 {html.escape(result['journey'])}</div>
<section class="grid">
  <div class="card"><div class="label">歷史相似行程</div><div class="value">{result['reference']['prior_count']} 趟</div></div>
  <div class="card"><div class="label">歷史油耗中位數</div><div class="value">{result['reference']['median_l_per_100km']:.2f}</div><div class="label">L/100 km</div></div>
  <div class="card"><div class="label">柴油參考價</div><div class="value">NT$ {result['diesel_price_twd']:.1f}</div><div class="label">每公升</div></div>
  <div class="card recommend"><div class="label">節能推薦</div><div class="value">{html.escape(result['recommended_route_name'])}</div></div>
</section>
<section class="layout">
  <div class="card"><svg id="map" viewBox="0 0 720 430" role="img" aria-label="候選路線示意圖"></svg></div>
  <div class="card"><h2>路線比較</h2><table><thead><tr><th>路線</th><th>距離</th><th>時間</th><th>爬升</th><th>耗油</th><th>油費</th></tr></thead><tbody>{route_rows}</tbody></table>
  <p class="note">油耗估計採34趟歷史相似行程的每百公里油耗中位數乘以路線距離；坡度已取得並展示，下一階段再納入模型。</p></div>
</section>
</main>
<script>
const data = {safe_json};
const svg = document.getElementById('map');
const NS = 'http://www.w3.org/2000/svg';
const all = data.routes.flatMap(r => r.coordinates);
const xs = all.map(p => p[0]), ys = all.map(p => p[1]);
const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
const pad = 34, width = 720, height = 430;
const project = p => [pad + (p[0]-minX)/(maxX-minX||1)*(width-pad*2), height-pad-(p[1]-minY)/(maxY-minY||1)*(height-pad*2)];
const colors = ['#16854b','#2b6cb0','#d97706'];
data.routes.forEach((route, i) => {{
  const path = document.createElementNS(NS, 'polyline');
  path.setAttribute('points', route.coordinates.map(p => project(p).join(',')).join(' '));
  path.setAttribute('fill','none'); path.setAttribute('stroke',colors[i]||'#555'); path.setAttribute('stroke-width', i===0?'5':'3'); path.setAttribute('stroke-linecap','round'); path.setAttribute('stroke-linejoin','round');
  svg.appendChild(path);
}});
[['起點',all[0]],['終點',data.routes[0].coordinates.at(-1)]].forEach(([label,p]) => {{
  const [x,y]=project(p); const c=document.createElementNS(NS,'circle'); c.setAttribute('cx',x);c.setAttribute('cy',y);c.setAttribute('r','7');c.setAttribute('fill','#17221b');svg.appendChild(c);
  const t=document.createElementNS(NS,'text');t.setAttribute('x',x+10);t.setAttribute('y',y-10);t.setAttribute('font-size','14');t.setAttribute('fill','#17221b');t.textContent=label;svg.appendChild(t);
}});
</script></body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")


def main() -> None:
    args = parse_args()
    trip_data = json.loads(args.trip_json.read_text(encoding="utf-8"))
    prior_count, prior_dates, median = load_reference_median(
        args.coverage_csv, trip_data["vehicle"], trip_data["journey"]
    )
    routes = build_routes(args, trip_data, median)
    recommended = min(routes, key=lambda route: route["estimated_cost_twd"])
    result = {
        "vehicle": trip_data["vehicle"],
        "journey": trip_data["journey"],
        "origin": trip_data["origin"],
        "destination": trip_data["destination"],
        "actual_trip": trip_data["target"],
        "reference": {
            "prior_count": prior_count,
            "prior_dates": prior_dates,
            "median_l_per_100km": median,
        },
        "diesel_price_twd": args.diesel_price,
        "diesel_price_effective_date": "2026-09-07",
        "estimate_method": "historical similar-route median L/100 km multiplied by candidate-route distance",
        "routing_profile": "Valhalla truck; no live-traffic comparison",
        "routes": routes,
        "recommended_route_id": recommended["id"],
        "recommended_route_name": recommended["name"],
    }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    write_html(args.output_html, result)
    print(json.dumps({key: value for key, value in result.items() if key != "routes"}, ensure_ascii=False, indent=2))
    print(json.dumps(routes, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
