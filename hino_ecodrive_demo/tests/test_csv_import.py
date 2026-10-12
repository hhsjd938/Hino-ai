from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from csv_import import MAX_UPLOAD_BYTES, uploaded_data
from engine import Config, analyze, read_replay


class CsvImportTests(unittest.TestCase):
    def test_exported_trip_preserves_official_events_and_timeline(self):
        content = (HERE / 'replay.csv').read_bytes()
        result, metadata = uploaded_data(content, 'my_trip.csv', Config())
        expected = analyze(read_replay(HERE / 'replay.csv'), real=True)
        self.assertEqual(result, expected)
        self.assertEqual(metadata['vehicle'], 'C1N6TWBTVB')
        self.assertEqual(metadata['records'], 413)
        self.assertIsNone(metadata['reference'])

    def test_minimal_bom_csv_sorts_time_and_does_not_invent_official_events(self):
        text = '\ufefftimestamp,speed,rpm,car_status,can_status\n'
        text += '2026-01-01 08:00:05,20,2800,1,0\n2026-01-01 08:00:00,20,2800,1,0\n'
        result, metadata = uploaded_data(text.encode('utf-8'), 'minimal.csv', Config())
        self.assertEqual(result['duration'], 5)
        self.assertCountEqual([event['code'] for event in result['events']], ['high_rpm', 'high_rpm_low_speed'])
        self.assertEqual(metadata['journey'], '匯入行程')

    def test_missing_fields_empty_csv_and_invalid_time_are_rejected(self):
        for content in (b'', b'vehicle,journey\nV1,T1\n',
                        b'timestamp,speed,rpm,car_status,can_status\nbad,20,1000,1,0\n'):
            with self.subTest(content=content), self.assertRaises(ValueError):
                uploaded_data(content, 'invalid.csv', Config())

    def test_multiple_trips_and_all_invalid_can_are_rejected(self):
        text = 'timestamp,speed,rpm,car_status,can_status,vehicle,journey\n'
        text += '2026-01-01 08:00:00,20,1000,1,0,V1,T1\n2026-01-01 08:00:01,20,1000,1,0,V1,T2\n'
        with self.assertRaisesRegex(ValueError, '同一趟行程'):
            uploaded_data(text.encode(), 'multi.csv', Config())
        text = text.replace(',0,V1,T1', ',9,V1,T1').replace(',0,V1,T2', ',9,V1,T1')
        with self.assertRaisesRegex(ValueError, '沒有有效車況'):
            uploaded_data(text.encode(), 'invalid_can.csv', Config())

    def test_oversize_and_non_utf8_are_rejected(self):
        with self.assertRaisesRegex(ValueError, '20 MB'):
            uploaded_data(b'x' * (MAX_UPLOAD_BYTES + 1), 'large.csv', Config())
        with self.assertRaisesRegex(ValueError, 'UTF-8'):
            uploaded_data(b'\xff\xff', 'wrong_encoding.csv', Config())


if __name__ == '__main__':
    unittest.main()
