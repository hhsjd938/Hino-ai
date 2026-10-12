"""Exercise trip eligibility and route-pair filtering through the actual pipeline."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / 'backend/scripts/data_pipeline'


class RouteScreeningTests(unittest.TestCase):
    def test_short_low_fuel_trips_enter_routes_but_nonpositive_and_bad_data_do_not(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            source = out / 'fixture.xlsx'
            source.write_bytes(b'Identity-only fixture; records supplied via matching cache.')
            stat = source.stat()
            (out / 'source_cache.json').write_text(json.dumps({
                'name': source.name, 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
            }), encoding='utf-8')
            cases = [
                ('short1', 1, 1, 'ok'), ('short2', 1, 1, 'ok'),
                ('lowfuel1', 10, 1, 'ok'), ('lowfuel2', 10, 1, 'ok'),
                ('zero_km', 0, 1, 'ok'), ('zero_fuel', 1, 0, 'ok'),
                ('negative_km', -1, 1, 'ok'), ('negative_fuel', 1, -1, 'ok'),
                ('bad_can', 1, 1, 'can'), ('rollback', 1, 1, 'rollback'),
                ('long_gap', 1, 1, 'gap'), ('no_boundary', 1, 1, 'boundary'),
            ]
            records = []
            for index, (journey, km, fuel, quality) in enumerate(cases):
                start = pd.Timestamp('2025-01-01') + pd.Timedelta(days=index)
                times = [start + pd.Timedelta(seconds=s) for s in (0, 10, 200 if quality == 'gap' else 20)]
                for n, timestamp in enumerate(times):
                    row = {
                        'enabledCode': 'V1', 'journeyCode': journey, 'time': str(timestamp),
                        'Type': '1', 'carStatus': '1',
                        'can.canStatus': '1' if quality == 'can' else '0',
                        'can.totalMileage': str(100 + (km * n / 2 if quality != 'rollback' else [0, 2, 1][n])),
                        'can.engine.totalFuelUsed': str(100 + fuel * n / 2),
                        'can.canSpeed': '20', 'gps.speed': '20', 'gps.longitude': '120',
                        'gps.latitude': '23', 'gps.speedLimit': '50',
                        'can.engine.rpm': '1500', 'can.engine.engineLoad': '50',
                    }
                    for slot in range(3):
                        for field in ('type', 'startTime', 'endTime', 'info.duration'):
                            row[f'event[{slot}].{field}'] = None
                    if n in (0, 2) and quality != 'boundary':
                        row['event[0].type'] = '4' if n == 0 else '3'
                        row['event[0].startTime'] = str(timestamp)
                        row['event[0].endTime'] = str(timestamp)
                    records.append(row)
            pd.DataFrame(records).to_parquet(out / 'source_records.parquet', index=False)
            self.run_script('verify_topics.py', '--source', str(source), '--output-dir', str(out))
            metrics = pd.read_csv(out / 'trip_metrics.csv').set_index('journey')
            for journey, _, _, quality in cases:
                expected = journey in ('short1', 'short2', 'lowfuel1', 'lowfuel2')
                self.assertEqual(bool(metrics.loc[journey, 'screened']), expected, journey)
            candidates = pd.read_csv(out / 'similar_endpoint_pairs.csv')
            self.assertEqual(len(candidates), 2)
            self.assertFalse(candidates['both_fuel_at_least_5l'].any())

            # Defensive pair validation must also reject invalid upstream rows.
            invalid_pairs = []
            for field in ('earlier_km', 'later_km', 'earlier_fuel_l', 'later_fuel_l'):
                for value in (0, -1):
                    row = candidates.iloc[0].copy()
                    row[field] = value
                    row['both_fuel_at_least_5l'] = True
                    invalid_pairs.append(row)
            pd.concat([candidates, pd.DataFrame(invalid_pairs)], ignore_index=True).to_csv(
                out / 'similar_endpoint_pairs.csv', index=False)
            self.run_script('verify_route_pairs.py', '--analysis-dir', str(out))
            pairs = pd.read_csv(out / 'route_screened_pairs.csv')
            self.assertEqual(len(pairs), 2)
            self.assertTrue(pairs['route_min_coverage'].eq(1).all())
            references = pd.read_csv(out / 'historical_reference_coverage.csv')
            self.assertEqual(set(references['journey']), {'short2', 'lowfuel2'})
            self.assertTrue(references['prior_count'].eq(1).all())
            self.run_script('summarize_route_groups.py', '--pairs', str(out / 'route_screened_pairs.csv'),
                            '--output-dir', str(out))
            members = pd.read_csv(out / 'route_group_members.csv')
            self.assertEqual(set(members['journey']), {'short1', 'short2', 'lowfuel1', 'lowfuel2'})

    def run_script(self, script, *arguments):
        result = subprocess.run([sys.executable, str(SCRIPTS / script), *arguments],
                                capture_output=True, text=True, encoding='utf-8', errors='replace')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
