from __future__ import annotations

import csv
import io
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

HERE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(HERE))
from route_search import coordinate, distance_m, export_csv, load_catalog, load_tracks, result_table, search, summarize


def trip(vehicle='V1',journey='T1',origin=(23,120),destination=(24,121),screened=True,group='G1',km=100):
    return {'vehicle':vehicle,'journey':journey,'origin':origin,'destination':destination,'screened':screened,
            'route_group':group,'start':'2025-01-01 08:00:00','km':km,'minutes':60,'fuel_l':10,'l_per_100km':10}


class RouteSearchTests(unittest.TestCase):
    def test_haversine_units_and_coordinate_validation(self):
        self.assertEqual(distance_m((23,120),(23,120)),0)
        self.assertAlmostEqual(distance_m((0,0),(0,1)),111194.92664455874,places=5)
        for lat,lon in ((None,120),(math.nan,120),(91,120),(23,181),(23,math.inf)):
            with self.assertRaises(ValueError):coordinate(lat,lon)

    def test_both_endpoints_and_direction_must_match(self):
        good=trip()
        bad_start=trip(journey='start',origin=(25,120))
        bad_end=trip(journey='end',destination=(25,121))
        reverse=trip(journey='reverse',origin=(24,121),destination=(23,120))
        found=search([good,bad_start,bad_end,reverse],(23,120),(24,121))
        self.assertEqual([row['journey'] for row in found],['T1'])
        self.assertEqual(search([reverse],(24,121),(23,120))[0]['journey'],'reverse')

    def test_radius_boundary_and_no_hidden_distance_ratio_filter(self):
        origin=(23,120); shifted=(23.005,120)
        boundary=distance_m(origin,shifted)
        row=trip(origin=shifted,km=400)
        self.assertEqual(len(search([row],origin,(24,121),boundary)),1)
        self.assertEqual(search([row],origin,(24,121),boundary-0.001),[])
        for radius in (0,-1,None,math.nan):
            with self.assertRaises(ValueError):search([row],origin,(24,121),radius)

    def test_vehicle_and_quality_filters_and_unclassified_trips(self):
        rows=[trip(),trip('V2','T2',group=None),trip('V3','T3',screened=False,group=None)]
        found=search(rows,(23,120),(24,121))
        self.assertEqual(len(found),2)
        self.assertEqual(len(search(rows,(23,120),(24,121),screened_only=False)),3)
        self.assertEqual([row['vehicle'] for row in search(rows,(23,120),(24,121),vehicles=['V2'])],['V2'])
        self.assertIsNone(found[1]['route_group'])

    def test_summary_uses_only_matched_trips_and_does_not_invent_routes(self):
        rows=[trip(),trip(journey='T2',km=200),trip(journey='T3',group=None)]
        groups={'G1':{'vehicle':'V1','trip_count':'5','direct_pair_density':'0.8'}}
        result=summarize(rows,groups)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['符合搜尋趟數'],2)
        self.assertEqual(result[0]['群組總趟數'],5)
        self.assertEqual(result[0]['中位里程 km'],150)
        self.assertEqual(result[0]['配對品質'],'鏈式連接')

    def test_export_is_utf8_bom_and_preserves_trip_ids(self):
        rows=search([trip(journey='000123')],(23,120),(24,121))
        payload=export_csv(result_table(rows))
        self.assertTrue(payload.startswith(b'\xef\xbb\xbf'))
        restored=list(csv.DictReader(io.StringIO(payload.decode('utf-8-sig'))))
        self.assertEqual(restored[0]['行程'],'000123')
        self.assertEqual(restored[0]['起點距離 m'],'0.0')

    def test_existing_map_parser_only_reads_the_json_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'map.html'
            payload=[{'vehicle':'V1','journey':'T1','points':[[23,120],[24,121]]}]
            path.write_text('const trips='+json.dumps(payload)+';\nconst groupSel=bad();',encoding='utf-8')
            self.assertEqual(load_tracks(path)[('V1','T1')]['points'],payload[0]['points'])
            path.write_text('const trips=not_json; const groupSel=bad();',encoding='utf-8')
            with self.assertRaises(ValueError):load_tracks(path)
            self.assertEqual(load_tracks(Path(directory)/'missing.html'),{})

    def test_real_catalog_and_repeated_route_example(self):
        analysis=HERE.parent/'data/processed/analysis'
        catalog=load_catalog(analysis)
        self.assertEqual(catalog['total'],3541)
        self.assertEqual(sum(row['screened'] for row in catalog['trips']),2301)
        seed=next(row for row in catalog['trips'] if row['vehicle']=='AHMPUL0C13' and row['journey']=='250111000001')
        found=search(catalog['trips'],seed['origin'],seed['destination'])
        summary=summarize(found,catalog['groups'])
        group=next(row for row in summary if row['路線群組']=='AHMPUL0C13-R001')
        self.assertEqual(group['符合搜尋趟數'],36)
        tracks=load_tracks(analysis/'route_stop_map.html')
        self.assertEqual(len(tracks),999)
        self.assertTrue(all(key in {(row['vehicle'],row['journey']) for row in catalog['trips']} for key in tracks))


if __name__=='__main__':unittest.main()
