import argparse
from pathlib import Path
from collections import Counter, defaultdict
import json, time, math
import openpyxl
import numpy as np

parser=argparse.ArgumentParser(description='Profile the HINO source workbook without modifying it.')
parser.add_argument('--source',type=Path,required=True,help='Path to the source XLSX workbook')
parser.add_argument('--output',type=Path,required=True,help='Path for the generated profile JSON')
args=parser.parse_args()

start=time.time()
source=args.source.resolve()
wb=openpyxl.load_workbook(source, read_only=True, data_only=True)
ws=wb.worksheets[0]
rows=ws.iter_rows(values_only=True)
header=next(rows); idx={v:i for i,v in enumerate(header)}
fields=['gps.longitude','gps.latitude','gps.speed','gps.speedLimit','can.canStatus','can.totalMileage','can.canSpeed','can.engine.totalFuelUsed','can.engine.engineTotalTime','can.engine.rpm','can.engine.engineLoad','can.engine.engineCoolantTemp']
missing=Counter(); zeros=Counter(); status=Counter(); types=Counter(); vehicles=set(); trips=defaultdict(list)
event_rows=Counter(); events={}; time_min=None; time_max=None; mismatch=0; duplicates=0
def num(v):
    try:
        n=float(v)
        return n if math.isfinite(n) else None
    except (ValueError,TypeError): return None
for count,row in enumerate(rows,1):
    def get(k): return row[idx[k]]
    vehicle=str(get('enabledCode')); trip=str(get('journeyCode')); t=str(get('time'))
    vehicles.add(vehicle); types[str(get('Type'))]+=1; status[str(get('carStatus'))]+=1
    time_min=min(time_min,t) if time_min else t; time_max=max(time_max,t) if time_max else t
    vals={k:num(get(k)) for k in fields}
    for k,v in vals.items():
        missing[k]+=v is None; zeros[k]+=v==0
    if vals['gps.speed']==0 and (vals['can.canSpeed'] or 0)>5: mismatch+=1
    trips[(vehicle,trip)].append((t,vals['can.totalMileage'],vals['can.engine.totalFuelUsed'],str(get('carStatus')),vals['can.canStatus'],vals['gps.longitude'],vals['gps.latitude']))
    for slot in range(3):
        et=get(f'event[{slot}].type')
        if et in (None,''): continue
        et=str(et); event_rows[et]+=1
        st=str(get(f'event[{slot}].startTime') or '')
        key=(vehicle,et,st)
        en=str(get(f'event[{slot}].endTime') or '')
        duration=num(get(f'event[{slot}].info.duration'))
        old=events.get(key)
        if old is None: events[key]={'end':en,'duration':duration,'trip':trip}
        else:
            if en>old['end']: old['end']=en
            if duration is not None and (old['duration'] is None or duration>old['duration']): old['duration']=duration
    if count%100000==0: print(f'Processed {count} rows in {time.time()-start:.0f}s',flush=True)

from datetime import datetime
intervals=[]; trip_dist=[]; trip_fuel=[]; rates=[]; durations=[]; valid_trip=0; negative_fuel_trips=0; negative_mileage_trips=0; zero_fuel_positive_dist=0
fuel_steps=Counter(); coordinate_steps=Counter(); trip_days=Counter(); records_per_trip=[]; vehicle_trip_counts=Counter(); event_vehicle=defaultdict(set); event_trip=defaultdict(set)
for (vehicle,trip),rs in trips.items():
    rs.sort(key=lambda r:r[0]); records_per_trip.append(len(rs)); vehicle_trip_counts[vehicle]+=1
    ts=[datetime.fromisoformat(r[0]) for r in rs]; trip_days[str(ts[0].date())]+=1
    durations.append((ts[-1]-ts[0]).total_seconds()/60)
    bad_f=False; bad_m=False
    for a,b,ta,tb in zip(rs,rs[1:],ts,ts[1:]):
        delta=(tb-ta).total_seconds(); intervals.append(delta); duplicates+=delta==0
        if a[2] is not None and b[2] is not None:
            diff=b[2]-a[2]; fuel_steps['negative' if diff<0 else 'zero' if diff==0 else 'positive']+=1
            bad_f|=diff<0
        if a[1] is not None and b[1] is not None: bad_m|=b[1]<a[1]
        if a[5:]==b[5:] and a[1] is not None and b[1] is not None and b[1]>a[1]: coordinate_steps['unchanged_gps_with_increasing_mileage']+=1
    negative_fuel_trips+=bad_f; negative_mileage_trips+=bad_m
    a,b=rs[0],rs[-1]
    if None not in (a[1],b[1],a[2],b[2]):
        dist=b[1]-a[1]; fuel=b[2]-a[2]
        trip_dist.append(dist); trip_fuel.append(fuel)
        zero_fuel_positive_dist+=dist>0 and fuel==0
        if dist>=10 and fuel>0 and not bad_f and not bad_m:
            valid_trip+=1; rates.append(fuel/dist*100)
for (vehicle,et,st),data in events.items():
    event_vehicle[et].add(vehicle); event_trip[et].add((vehicle,data['trip']))
def summary(xs):
    return dict(zip(['min','p25','median','p75','p95','max'],[float(x) for x in np.percentile(xs,[0,25,50,75,95,100])])) if xs else {}
event_counts=Counter(k[1] for k in events)
idle=[v['duration'] for k,v in events.items() if k[1]=='2' and v['duration'] is not None]
result={'file':source.name,'rows':count,'columns':len(header),'vehicles':len(vehicles),'trips':len(trips),'time_range':[time_min,time_max],'types_rows':types,'car_status_rows':status,'missing_numeric':missing,'zero_numeric':zeros,'events_raw':event_rows,'events_unique_vehicle_type_start':event_counts,'events_vehicles':{k:len(v) for k,v in event_vehicle.items()},'events_trips':{k:len(v) for k,v in event_trip.items()},'sampling_seconds':summary(intervals),'intervals_over_120s':sum(x>120 for x in intervals),'duplicate_trip_timestamps':duplicates,'rows_per_trip':summary(records_per_trip),'trips_per_vehicle':summary(list(vehicle_trip_counts.values())),'trip_days':trip_days,'trip_minutes':summary(durations),'trip_distance_km':summary(trip_dist),'trip_fuel_liters':summary(trip_fuel),'trips_with_fuel_decrease':negative_fuel_trips,'trips_with_mileage_decrease':negative_mileage_trips,'positive_distance_zero_fuel_trips':zero_fuel_positive_dist,'candidate_fuel_trips_distance_at_least_10km':valid_trip,'candidate_fuel_l_per_100km':summary(rates),'fuel_step_counts':fuel_steps,'gps_zero_can_over_5_rows':mismatch,'coordinate_steps':coordinate_steps,'idle_event_reported_duration_seconds':summary(idle),'idle_events_with_reported_duration':len(idle),'elapsed_seconds':time.time()-start}
out=args.output.resolve(); out.parent.mkdir(parents=True,exist_ok=True); out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)
