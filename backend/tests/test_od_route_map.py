from __future__ import annotations

import sys
import unittest
import zipfile
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend/scripts/data_pipeline'))
from build_od_route_map import catalog, continuous_runs, extract_tracks, render


class ODMapTests(unittest.TestCase):
    def test_catalog_includes_short_and_ungrouped_trips(self):
        data = catalog(ROOT / 'data/processed/analysis')
        self.assertEqual(data['total'], 3541)
        self.assertTrue(any(t['km'] < 10 for t in data['trips']))
        self.assertTrue(any(not t['group'] for t in data['trips']))
        self.assertEqual(len(data['groups']), 243)
        self.assertEqual(sum(bool(t['group']) for t in data['trips']), 999)

    def test_missing_coordinates_duplicates_and_long_gaps_split_tracks(self):
        def point(second, lat=23):
            return {'dt': datetime(2025,1,1) + timedelta(seconds=second), 'lat':lat, 'lon':120}
        runs = continuous_runs([point(0),point(60),point(60),point(61),point(62,None),point(63),point(184)], 120)
        self.assertEqual([len(run) for run in runs], [2,2,1,1])
        runs = continuous_runs([point(0),point(120)],120)
        self.assertEqual(len(runs),1)

    def test_xlsx_headers_are_used_and_invalid_can_does_not_create_stops(self):
        names = ['time','can.canStatus','journeyCode','gps.latitude','enabledCode','gps.longitude','can.canSpeed']
        rows = [names] + [[f'2025-01-01 08:0{i}:00','9','T1','23.1','V1','120.1','0'] for i in range(5)]
        xml_rows = []
        for i,row in enumerate(rows,1):
            cells = ''.join(f'<c r="{chr(65+j)}{i}" t="inlineStr"><is><t>{escape(v)}</t></is></c>' for j,v in enumerate(row))
            xml_rows.append(f'<row r="{i}">{cells}</row>')
        ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'source.xlsx'
            with zipfile.ZipFile(path,'w') as book:
                book.writestr('xl/sharedStrings.xml',f'<sst xmlns="{ns}"/>')
                book.writestr('xl/worksheets/sheet1.xml',f'<worksheet xmlns="{ns}"><sheetData>{"".join(xml_rows)}</sheetData></worksheet>')
            trip = {'vehicle':'V1','journey':'T1','origin':[23,120],'destination':[23.2,120.2]}
            self.assertEqual(extract_tracks(path,{'trips':[trip]}),5)
            self.assertTrue(trip['segments'])
            self.assertFalse(trip['stops'])

    def test_render_uses_osm_has_no_group_selector_and_escapes_data(self):
        html = render({'trips':[], 'groups':{}, 'total':0, 'invalid':0, 'source':'</script><script>alert(1)</script>'})
        self.assertIn('https://tile.openstreetmap.org/{z}/{x}/{y}.png',html)
        self.assertIn('OpenStreetMap</a> contributors',html)
        self.assertIn('id="searchForm"',html)
        self.assertNotIn('id="group"',html)
        self.assertNotIn('<script>alert(1)',html)
        self.assertNotIn('__DATA__',html)
        self.assertNotIn('__CSS__',html)
        self.assertNotIn('__JS__',html)


if __name__ == '__main__':
    unittest.main()
