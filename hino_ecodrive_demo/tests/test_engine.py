from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from engine import Config, SCENARIOS, analyze, read_replay, scenario
from prepare_replay import extract


def points(speeds, *, step=1, rpm=1000, limit=80):
    return [{"timestamp": datetime(2026, 1, 1) + timedelta(seconds=i * step), "speed": speed,
             "rpm": rpm, "car_status": 2 if speed == 0 else 1, "can_status": 0, "speed_limit": limit,
             "vehicle": "V1", "journey": "T1", "mileage": 100 + i, "fuel": 10 + i / 2, "event_reports": []}
            for i, speed in enumerate(speeds)]


def events(result, code, phase="start"):
    return [event for event in result["events"] if event["code"] == code and event["phase"] == phase]


class EngineTests(unittest.TestCase):
    def test_accel_requires_all_three_strict_conditions(self):
        self.assertEqual(len(events(analyze(points([20, 24, 28, 45])), "6")), 1)
        for speeds in ([20, 24, 28, 38], [20, 24, 25, 40], [20, 50, 28, 45], [20, 10, 20, 35]):
            with self.subTest(speeds=speeds):
                self.assertFalse(events(analyze(points(speeds)), "6"))

    def test_accel_and_decel_require_exact_one_second_valid_samples(self):
        for step in (0, 0.5, 2, 60):
            self.assertFalse(events(analyze(points([20, 24, 28, 45], step=step)), "6"))
            self.assertFalse(events(analyze(points([80, 70, 60, 50], step=step)), "7"))
        for invalid in (None, -1):
            rows = points([20, 24, 28, 45]); rows[1]["speed"] = invalid
            self.assertFalse(events(analyze(rows), "6"))
        rows = points([20, 24, 28, 45]); rows[2]["can_status"] = 9
        self.assertFalse(events(analyze(rows), "6"))

    def test_decel_inclusive_two_window_boundary_and_cooldown(self):
        self.assertEqual(len(events(analyze(points([80, 75, 65, 60])), "7")), 1)
        self.assertFalse(events(analyze(points([80, 75, 66, 60])), "7"))
        rows = points([200 - i * 10 for i in range(18)])
        self.assertEqual([e["at"] for e in events(analyze(rows), "7")], [3, 13])

    def test_idle_exact_300_seconds_and_release(self):
        rows = points([0] * 301 + [10] * 31)
        result = analyze(rows)
        self.assertEqual([e["at"] for e in events(result, "2")], [300])
        self.assertEqual([e["at"] for e in events(result, "2", "end")], [331])
        self.assertFalse(events(analyze(points([0] * 300)), "2"))

    def test_idle_low_speed_pauses_accumulation_fast_speed_resets(self):
        cfg = replace(Config(), idle_seconds=5)
        result = analyze(points([0, 0, 0, 5, 5, 0, 0, 0, 0]), cfg)
        self.assertEqual([e["at"] for e in events(result, "2")], [8])
        self.assertFalse(events(analyze(points([0, 0, 0, 11, 0, 0, 0, 0]), cfg), "2"))
        rows = points([0] * 10); rows[4]["rpm"] = 0
        self.assertFalse(events(analyze(rows, cfg), "2"))

    def test_gaps_missing_signals_and_can_errors_break_duration(self):
        rows = points([0, 0], step=301)
        result = analyze(rows)
        self.assertFalse(events(result, "2")); self.assertEqual(result["gaps"], 1)
        self.assertIsNone(result["frames"][-1]["acceleration"])
        cfg = replace(Config(), idle_seconds=5, rpm_hold=5)
        for field, value in (("rpm", None), ("can_status", 9), ("car_status", None)):
            rows = points([0] * 10, rpm=2800); rows[4][field] = value
            result = analyze(rows, cfg)
            self.assertFalse(events(result, "2")); self.assertFalse(events(result, "high_rpm"))

    def test_high_rpm_strict_boundaries_and_rearming(self):
        self.assertFalse(events(analyze(points([20]*12, rpm=2500)), "high_rpm"))
        result = analyze(points([30]*12, rpm=2800))
        self.assertEqual(len(events(result, "high_rpm")), 1)
        self.assertFalse(events(result, "high_rpm_low_speed"))
        rows = points([20]*20, rpm=2800); rows[8]["rpm"] = 1000
        result = analyze(rows)
        self.assertEqual([e["at"] for e in events(result, "high_rpm")], [5, 14])
        self.assertNotIn("high_rpm", result["frames"][5]["active"])
        self.assertIn("high_rpm_low_speed", result["frames"][5]["active"])

    def test_speeding_road_and_absolute_branches_release_independently(self):
        cfg = replace(Config(), speed_hold=2)
        self.assertEqual(len(events(analyze(points([90]*4), cfg), "8")), 1)
        self.assertFalse(events(analyze(points([89]*4), cfg), "8"))
        # Both branches activate at 2. Absolute releases at 5, road only at 8.
        rows = points([110]*3 + [94]*3 + [80]*3)
        result = analyze(rows, cfg)
        self.assertEqual([e["at"] for e in events(result, "8")], [2])
        self.assertEqual([e["at"] for e in events(result, "8", "end")], [8])
        result = analyze(points([107]*4, limit=0), cfg)
        self.assertEqual(len(events(result, "8")), 1)

    def test_real_official_events_are_deduplicated_and_not_backdated(self):
        rows = points([20,24,28,45,50,50])
        report = {"type":"6", "startTime":"2026-01-01 00:00:01"}
        rows[3]["event_reports"] = [report, report]
        rows[4]["event_reports"] = [report]
        result = analyze(rows, real=True)
        self.assertEqual([e["at"] for e in events(result, "6")], [3])
        self.assertEqual(events(result, "6")[0]["source"], "車機回報")
        self.assertFalse(events(analyze(points([20,24,28,45]), real=True), "6"))
        self.assertFalse(events(analyze(points([0]*310), real=True), "2"))

    def test_official_end_only_appears_when_report_is_received(self):
        rows = points([0]*8)
        report = {"type":"2", "startTime":"2026-01-01 00:00:00"}
        rows[2]["event_reports"] = [report]
        rows[6]["event_reports"] = [{**report, "endTime":"2026-01-01 00:00:05"}]
        result = analyze(rows, real=True)
        self.assertEqual([e["at"] for e in events(result, "2", "end")], [6])
        self.assertIn("2", result["frames"][5]["active"])
        self.assertNotIn("2", result["frames"][6]["active"])

    def test_switches_and_configuration_validation(self):
        self.assertFalse(events(analyze(points([20,24,28,45]), replace(Config(), enabled=())), "6"))
        for cfg in (replace(Config(), max_gap=0), replace(Config(), decel_two=0), replace(Config(), speed_release=107)):
            with self.assertRaises(ValueError): cfg.validate()

    def test_scenarios_have_expected_events_and_no_fake_fuel(self):
        expected = {"normal":None, "high_rpm":"high_rpm", "high_rpm_low_speed":"high_rpm_low_speed",
                    "acceleration":"6", "deceleration":"7", "idle":"2", "speeding":"8"}
        for key in SCENARIOS:
            result = analyze(scenario(key))
            with self.subTest(key=key):
                if expected[key]: self.assertEqual(result["counts"][expected[key]], 1)
                else: self.assertEqual(result["counts"], {})
                self.assertIsNone(result["frames"][-1]["fuel_l"])
                json.dumps(result, allow_nan=False)

    def test_reading_rejects_invalid_time_and_mixed_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"replay.csv"
            fields = ["timestamp","speed","rpm","car_status","can_status","vehicle","journey"]
            def write(rows):
                with path.open('w',newline='',encoding='utf-8') as handle:
                    writer=csv.writer(handle); writer.writerow(fields); writer.writerows(rows)
            write([["bad",20,1000,1,0,"V1","T1"]]*2)
            with self.assertRaises(ValueError): read_replay(path)
            write([["2026-01-01 00:00:00",20,1000,1,0,"V1","T1"],["2026-01-01 00:00:01",20,1000,1,0,"V2","T1"]])
            with self.assertRaises(ValueError): read_replay(path)

    def test_extracts_columns_by_header_and_preserves_event_payload(self):
        # Tiny inline-string XLSX fixture; source column positions may change.
        from xml.sax.saxutils import escape
        headers = ['enabledCode','journeyCode','time','can.canSpeed','can.engine.rpm','carStatus','can.canStatus',
                   'can.totalMileage','can.engine.totalFuelUsed','gps.speedLimit','gps.longitude','gps.latitude','event[0].type','event[0].startTime']
        values = ['V1','T1','2026-01-01 00:00:00','30','1000','1','0','100','10','80','121','24','6','2026-01-01 00:00:00']
        def xml_row(i, vals):
            return '<row r="%d">'%i + ''.join(f'<c r="{chr(65+j)}{i}" t="inlineStr"><is><t>{escape(value)}</t></is></c>' for j,value in enumerate(vals)) + '</row>'
        xml = '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + xml_row(1,headers)+xml_row(2,values)+xml_row(3,[*values[:2],'2026-01-01 00:00:01',*values[3:]])+'</sheetData></worksheet>'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'fixture.xlsx'
            with zipfile.ZipFile(path,'w') as book: book.writestr('xl/worksheets/sheet1.xml',xml)
            rows, scanned = extract(path,'V1','T1')
            self.assertEqual(scanned,2); self.assertEqual(rows[0]['speed'],'30')
            self.assertEqual(json.loads(rows[0]['event_reports'])[0]['type'],'6')

    def test_bundled_real_trip_matches_existing_summary(self):
        path = HERE/'replay.csv'
        if not path.exists(): self.skipTest('real sample not exported')
        result = analyze(read_replay(path),real=True)
        self.assertEqual(result['counts']['6'],2); self.assertEqual(result['counts']['7'],14)
        self.assertAlmostEqual(result['duration']/60,129.81666666666666)
        with (HERE.parent/'trip_behavior/output/trip_behavior_metrics.csv').open(encoding='utf-8-sig',newline='') as handle:
            reference = next(row for row in csv.DictReader(handle) if row['vehicle_id']=='C1N6TWBTVB' and row['trip_id']=='250731030003')
        self.assertAlmostEqual(result['frames'][-1]['fuel_l'],float(reference['fuel_l']))
        self.assertAlmostEqual(result['frames'][-1]['distance_km'],float(reference['distance_km']))


if __name__ == '__main__':
    unittest.main()
