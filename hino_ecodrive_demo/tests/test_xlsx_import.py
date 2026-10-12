from __future__ import annotations

import io
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook
from openpyxl.utils.datetime import CALENDAR_MAC_1904

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import Config
from xlsx_import import read_sheet, sheet_info, timestamp, trip_data


def workbook_bytes(sheets, *, mac_dates=False):
    book = Workbook()
    book.remove(book.active)
    if mac_dates:
        book.epoch = CALENDAR_MAC_1904
    for name, rows in sheets.items():
        sheet = book.create_sheet(name)
        for row in rows:
            sheet.append(row)
    output = io.BytesIO()
    book.save(output)
    book.close()
    return output.getvalue()


class XlsxImportTests(unittest.TestCase):
    def test_multiple_sheets_and_trips_are_selected_without_mixing(self):
        headers = ['timestamp','vehicle','journey','speed','rpm','car_status','can_status']
        start = datetime(2025,1,1,8)
        rows = [headers]
        for second in range(3):
            for vehicle, journey in [('A','001'),('A','002'),('B','001')]:
                rows.append([start + timedelta(seconds=second),vehicle,journey,20,1500,1,0])
        content = workbook_bytes({'說明':[['notes']], '車況':rows})
        self.assertEqual(list(sheet_info(content)['sheets']), ['說明','車況'])
        with patch('xlsx_import.CHUNK_ROWS',2):
            data = read_sheet(content,'車況')
        self.assertEqual(data['records'],9)
        self.assertEqual(len(data['trips']),3)
        self.assertEqual(len(data['chunks']),5)
        for vehicle,journey in [('A','001'),('A','002'),('B','001')]:
            result, metadata = trip_data(data,'multiple.xlsx',vehicle,journey,Config())
            self.assertEqual(len(result['frames']),3)
            self.assertEqual(result['duration'],2)
            self.assertEqual(metadata['vehicle'],vehicle)
            self.assertEqual(metadata['journey'],journey)
            self.assertEqual(metadata['worksheet'],'車況')
        with self.assertRaisesRegex(ValueError,'找不到'):
            trip_data(data,'multiple.xlsx','Z','001',Config())

    def test_original_hino_columns_native_excel_dates_and_official_event_dedup(self):
        start = datetime(2025,7,31,11,31,30)
        headers = ['journeyCode','carStatus','time','can.canStatus','can.canSpeed','enabledCode','can.engine.rpm',
                   'can.totalMileage','can.engine.totalFuelUsed','event[0].type','event[0].startTime','event[0].info.duration']
        rows = [headers]
        for i in range(3):
            rows.append([250731030003,1,start+timedelta(seconds=i),0,20+i,'V1',1500,100+i,20+i,6,start,2])
        content = workbook_bytes({'HINO':rows})
        data = read_sheet(content,'HINO')
        result, metadata = trip_data(data,'hino.xlsx','V1','250731030003',Config())
        official = [event for event in result['events'] if event['source']=='車機回報']
        self.assertEqual(len(official),1)
        self.assertEqual(official[0]['code'],'6')
        self.assertEqual(official[0]['original_time'],'2025-07-31 11:31:30')
        self.assertEqual(result['frames'][-1]['distance_km'],2)
        self.assertEqual(result['frames'][-1]['fuel_l'],2)
        self.assertEqual(metadata['journey'],'250731030003')

    def test_mac_excel_date_system_and_canonical_event_json(self):
        rows = [['timestamp','speed','rpm','car_status','can_status','event_reports'],
                [datetime(2025,1,1,8),20,1500,1,0,'[{"type":"7","startTime":"2025-01-01 08:00:00"}]'],
                [datetime(2025,1,1,8,0,1),10,1000,1,0,'[]']]
        data = read_sheet(workbook_bytes({'Data':rows},mac_dates=True),'Data')
        result,metadata = trip_data(data,'mac.xlsx','','',Config())
        self.assertEqual(result['frames'][0]['time'],'2025-01-01 08:00:00')
        self.assertEqual(result['events'][0]['code'],'7')
        self.assertEqual(metadata['vehicle'],'未提供車輛')

    def test_corrupt_missing_headers_empty_and_invalid_dates_are_rejected(self):
        with self.assertRaisesRegex(ValueError,'無法讀取 XLSX'):
            sheet_info(b'not an Excel file')
        for rows, expected in [([['notes']], '缺少欄位'),
                               ([['timestamp','speed','rpm','car_status','can_status']], '沒有可播放'),
                               ([['timestamp','speed','rpm','car_status','can_status'],['bad',20,1000,1,0]],'第 2 列')]:
            with self.subTest(expected=expected), self.assertRaisesRegex(ValueError,expected):
                read_sheet(workbook_bytes({'Data':rows}),'Data')
        with self.assertRaisesRegex(ValueError,'找不到所選'):
            read_sheet(workbook_bytes({'Data':[['notes']]}),'Missing')
        for value in ('nan','inf','',-100000000):
            with self.subTest(value=value), self.assertRaises(ValueError):
                timestamp(value)

    def test_one_record_trip_is_not_playable_and_duplicate_columns_are_rejected(self):
        rows = [['timestamp','speed','rpm','car_status','can_status'],['2025-01-01 08:00:00',20,1000,1,0]]
        data = read_sheet(workbook_bytes({'Data':rows}),'Data')
        with self.assertRaisesRegex(ValueError,'至少需要兩筆'):
            trip_data(data,'one.xlsx','','',Config())
        rows[0].append('time')
        with self.assertRaisesRegex(ValueError,'重複'):
            read_sheet(workbook_bytes({'Data':rows}),'Data')


if __name__ == '__main__':
    unittest.main()
