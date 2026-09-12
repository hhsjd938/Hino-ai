from __future__ import annotations

import importlib.util
import math
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[2] / "trip_behavior" / "analyze_trip_behavior.py"
SPEC = importlib.util.spec_from_file_location("analyze_trip_behavior", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

Observation = MODULE.Observation
Thresholds = MODULE.Thresholds


def point(second: int, *, state: float = 1, speed: float = 0, rpm: float = 1000, mileage: float = 0, fuel: float = 0):
    return Observation(
        timestamp=datetime(2025, 1, 1) + timedelta(seconds=second),
        car_status=state,
        mileage=mileage,
        speed=speed,
        fuel=fuel,
        rpm=rpm,
    )


class TripBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.thresholds = Thresholds()

    def test_time_weighted_metrics_and_threshold_boundaries(self) -> None:
        points = [
            point(0, state=2, speed=0, rpm=2000, mileage=10, fuel=100),
            point(60, state=1, speed=30, rpm=1000, mileage=11, fuel=100.5),
            point(120, state=1, speed=30, rpm=1000, mileage=12, fuel=101),
        ]
        result = MODULE.compute_trip_metrics(points, {"6": set(), "7": set()}, self.thresholds)
        self.assertAlmostEqual(result["distance_km"], 2)
        self.assertAlmostEqual(result["duration_min"], 2)
        self.assertAlmostEqual(result["fuel_l"], 1)
        self.assertAlmostEqual(result["l_per_100km"], 50)
        self.assertAlmostEqual(result["idle_minutes"], 1)
        self.assertAlmostEqual(result["idle_ratio"], 0.5)
        self.assertAlmostEqual(result["high_rpm_minutes"], 1)
        self.assertAlmostEqual(result["high_rpm_ratio"], 0.5)
        self.assertAlmostEqual(result["high_rpm_low_speed_minutes"], 1)
        self.assertAlmostEqual(result["speed_std"], 15)

    def test_stop_go_counts_only_complete_cycles(self) -> None:
        points = [
            point(0, speed=0),
            point(10, speed=12),
            point(20, speed=3),
            point(30, speed=6),
            point(40, speed=10),
            point(50, speed=2),
        ]
        self.assertEqual(MODULE.stop_go_count(points, self.thresholds), 1)

    def test_long_gap_resets_stop_go_cycle_and_is_excluded(self) -> None:
        points = [point(0, speed=12), point(10, speed=0), point(200, speed=12)]
        result = MODULE.compute_trip_metrics(points, {"6": set(), "7": set()}, self.thresholds)
        self.assertEqual(result["stop_go_count"], 0)
        self.assertEqual(result["long_gap_count"], 1)
        self.assertAlmostEqual(result["valid_duration_min"], 10 / 60)

    def test_rapid_events_are_deduplicated_and_missing_start_is_reported(self) -> None:
        events = MODULE.defaultdict(lambda: {"6": set(), "7": set()})
        seen = set()
        key = ("V1", "T1")
        self.assertFalse(MODULE.add_rapid_event(events, key, "6", "2025-01-01 00:00:10", seen))
        self.assertFalse(MODULE.add_rapid_event(events, key, 6, "2025-01-01 00:00:10", seen))
        self.assertFalse(MODULE.add_rapid_event(events, key, "7", "2025-01-01 00:00:20", seen))
        self.assertTrue(MODULE.add_rapid_event(events, key, "7", None, seen))
        self.assertEqual(len(events[key]["6"]), 1)
        self.assertEqual(len(events[key]["7"]), 1)

        other_trip = ("V1", "T2")
        MODULE.add_rapid_event(events, other_trip, "6", "2025-01-01 00:00:10", seen)
        self.assertEqual(len(events[other_trip]["6"]), 0)

    def test_group_median_deviations(self) -> None:
        rows = [
            {"route_group_id": "R1", "fuel_l": 10.0, "duration_min": 20.0},
            {"route_group_id": "R1", "fuel_l": 20.0, "duration_min": 40.0},
            {"route_group_id": "R1", "fuel_l": 30.0, "duration_min": 60.0},
        ]
        MODULE.add_group_deviations(rows)
        self.assertAlmostEqual(rows[0]["fuel_deviation_pct"], -50)
        self.assertAlmostEqual(rows[1]["fuel_deviation_pct"], 0)
        self.assertAlmostEqual(rows[2]["fuel_deviation_pct"], 50)
        self.assertAlmostEqual(rows[1]["time_deviation_pct"], 0)

    def test_report_is_self_contained(self) -> None:
        row = {column: 0 for column in MODULE.CORE_COLUMNS + MODULE.QUALITY_COLUMNS}
        row.update({"trip_id": "T1", "route_group_id": "R1", "vehicle_id": "V1", "group_quality": "complete_pair"})
        summary = {"generated_at": "2025-01-01T00:00:00+08:00"}
        report = MODULE.build_report([row], self.thresholds, summary)
        self.assertIn("const DATA=", report)
        self.assertNotIn("https://", report)
        self.assertNotIn("http://", report)


if __name__ == "__main__":
    unittest.main()
