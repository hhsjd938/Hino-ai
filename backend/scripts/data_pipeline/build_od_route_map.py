#!/usr/bin/env python3
"""Build a separate OpenStreetMap page for directional endpoint searches."""
from __future__ import annotations

import argparse
import csv
import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from build_route_stop_map import (
    CELL_REF, NS, TEMPLATE as ORIGINAL_TEMPLATE, cell_value, downsample,
    haversine_m, inferred_stops, number, parse_time, shared_strings, valid_coordinate,
)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def catalog(analysis: Path) -> dict:
    with (analysis / 'route_group_members.csv').open(encoding='utf-8-sig', newline='') as handle:
        members = {(r['vehicle'], r['journey']): r['route_group'] for r in csv.DictReader(handle)}
    with (analysis / 'route_groups.csv').open(encoding='utf-8-sig', newline='') as handle:
        groups = {r['route_group']: {'vehicle': r['vehicle'], 'count': int(r['trip_count']),
                                    'density': float(r['direct_pair_density'])} for r in csv.DictReader(handle)}
    trips, total, invalid = [], 0, 0
    with (analysis / 'trip_metrics.csv').open(encoding='utf-8-sig', newline='') as handle:
        for row in csv.DictReader(handle):
            total += 1
            origin = [number(row['start_lat']), number(row['start_lon'])]
            destination = [number(row['end_lat']), number(row['end_lon'])]
            if not all(valid_coordinate(lon, lat) for lat, lon in (origin, destination)):
                invalid += 1
                continue
            trips.append({'vehicle': row['vehicle'], 'journey': row['journey'], 'start': row['start'],
                          'origin': origin, 'destination': destination,
                          'group': members.get((row['vehicle'], row['journey']), ''),
                          'screened': row['screened'].lower() == 'true',
                          'km': number(row['km']), 'fuel': number(row['fuel_l']),
                          'efficiency': number(row['l_per_100km']), 'minutes': number(row['minutes']),
                          'segments': [], 'stops': []})
    trips.sort(key=lambda row: (not bool(row['group']), not row['screened'], row['vehicle'], row['start'], row['journey']))
    return {'trips': trips, 'groups': groups, 'total': total, 'invalid': invalid}


def continuous_runs(points: list[dict], max_gap: float) -> list[list[dict]]:
    runs, run = [], []
    for point in points:
        if point['dt'] is None or not valid_coordinate(point['lon'], point['lat']):
            if run:
                runs.append(run)
            run = []
            continue
        if run and not 0 < (point['dt'] - run[-1]['dt']).total_seconds() <= max_gap:
            runs.append(run)
            run = []
        run.append(point)
    if run:
        runs.append(run)
    return runs


def extract_tracks(source: Path, data: dict, *, max_gap=120, stop_speed=3, stop_minutes=3, endpoint_radius=300) -> int:
    index = {(t['vehicle'], t['journey']): t for t in data['trips']}
    collected = {key: [] for key in index}
    names = {'enabledCode', 'journeyCode', 'time', 'gps.longitude', 'gps.latitude', 'can.canSpeed', 'can.canStatus'}
    count = 0
    with zipfile.ZipFile(source) as book:
        strings = shared_strings(book)
        columns = None
        with book.open('xl/worksheets/sheet1.xml') as sheet:
            for _, row in ET.iterparse(sheet, events=('end',)):
                if row.tag != NS + 'row':
                    continue
                selected = {}
                for cell in row.findall(NS + 'c'):
                    match = CELL_REF.match(cell.attrib.get('r', ''))
                    if match and (columns is None or match.group(1) in columns):
                        selected[match.group(1)] = cell_value(cell, strings)
                row.clear()
                if columns is None:
                    columns = {col: name for col, name in selected.items() if name in names}
                    if set(columns.values()) != names:
                        raise ValueError(f'Excel 缺少欄位：{names - set(columns.values())}')
                    continue
                count += 1
                values = {columns[col]: value for col, value in selected.items()}
                key = (str(values.get('enabledCode') or ''), str(values.get('journeyCode') or ''))
                if key in index:
                    speed = number(values.get('can.canSpeed'))
                    if number(values.get('can.canStatus')) != 0 or speed is None or speed < 0:
                        speed = None
                    collected[key].append({'dt': parse_time(values.get('time')),
                                           'lon': number(values.get('gps.longitude')),
                                           'lat': number(values.get('gps.latitude')), 'speed': speed})
                if count % 100000 == 0:
                    print(f'GPS: {count:,} records read', flush=True)
    for key, points in collected.items():
        # Stable ordering preserves duplicate observations; duplicates split runs.
        points.sort(key=lambda p: p['dt'].isoformat() if p['dt'] else '')
        trip = index[key]
        runs = continuous_runs(points, max_gap)
        trip['segments'] = [[[round(p['lat'], 6), round(p['lon'], 6)] for p in downsample(run)] for run in runs]
        start = {'lat': trip['origin'][0], 'lon': trip['origin'][1]}
        end = {'lat': trip['destination'][0], 'lon': trip['destination'][1]}
        trip['stops'] = [stop for run in runs for stop in inferred_stops(run, stop_speed, stop_minutes * 60, max_gap)
                         if haversine_m(stop, start) > endpoint_radius and haversine_m(stop, end) > endpoint_radius]
    return count


def render(data: dict) -> str:
    # Reuse the original page's styling without modifying its generator or output.
    base_css = ORIGINAL_TEMPLATE.split('<style>', 1)[1].split('</style>', 1)[0]
    payload = json.dumps(data, ensure_ascii=False, separators=(',', ':'), allow_nan=False).replace('<', '\\u003c')
    html = (HERE / 'od_route_map.html').read_text(encoding='utf-8')
    return html.replace('__BASE_CSS__', base_css).replace('__DATA__', payload).replace(
        '__CSS__', (HERE / 'od_route_map.css').read_text(encoding='utf-8')).replace(
        '__JS__', (HERE / 'od_route_map.js').read_text(encoding='utf-8'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT / 'hino/output data_Hotai_20260511.xlsx')
    parser.add_argument('--analysis-dir', type=Path, default=ROOT / 'data/processed/analysis')
    parser.add_argument('--output', type=Path, default=ROOT / 'data/processed/analysis/od_route_map.html')
    args = parser.parse_args()
    original = (args.analysis_dir / 'route_stop_map.html').resolve()
    if args.output.resolve() == original:
        parser.error('請使用新檔名；不能覆寫原本的 route_stop_map.html')
    data = catalog(args.analysis_dir)
    count = extract_tracks(args.source, data)
    data['source'] = args.source.name
    data['records'] = count
    data['rules'] = {'gap': 120, 'speed': 3, 'minutes': 3, 'radius': 300}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(data), encoding='utf-8')
    print(json.dumps({'output': str(args.output), 'searchable': len(data['trips']), 'invalid_endpoints': data['invalid'],
                      'with_tracks': sum(any(len(s) > 1 for s in t['segments']) for t in data['trips'])}, ensure_ascii=False))


if __name__ == '__main__':
    main()
