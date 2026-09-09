"確認有多少路線是一樣的"

import argparse
from pathlib import Path
from collections import Counter, defaultdict
from datetime import datetime
import json, math, time
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import openpyxl

parser=argparse.ArgumentParser(description='Build HINO trip, event, route-candidate, and topic-validation datasets.')
parser.add_argument('--source',type=Path,required=True,help='Path to the source XLSX workbook')
parser.add_argument('--output-dir',type=Path,required=True,help='Directory for generated analysis files')
args=parser.parse_args()

OUT=args.output_dir.resolve()
OUT.mkdir(parents=True,exist_ok=True)
src=args.source.resolve()
cache=OUT/'source_records.parquet'
meta=OUT/'source_cache.json'
identity={'name':src.name,'size':src.stat().st_size,'mtime_ns':src.stat().st_mtime_ns}
started=time.time()
if not cache.exists() or not meta.exists() or json.loads(meta.read_text('utf-8'))!=identity:
    wb=openpyxl.load_workbook(src,read_only=True,data_only=True)
    it=wb.worksheets[0].iter_rows(values_only=True)
    names=list(next(it)); schema=pa.schema([(n,pa.string()) for n in names])
    writer=pq.ParquetWriter(cache,schema,compression='zstd')
    batch=[]
    for count,row in enumerate(it,1):
        batch.append([None if v is None or v=='' else str(v) for v in row])
        if len(batch)==20000:
            writer.write_table(pa.Table.from_arrays([pa.array(c,type=pa.string()) for c in zip(*batch)],schema=schema)); batch=[]
        if count%100000==0: print(f'Cached {count} rows ({time.time()-started:.0f}s)',flush=True)
    if batch: writer.write_table(pa.Table.from_arrays([pa.array(c,type=pa.string()) for c in zip(*batch)],schema=schema))
    writer.close(); wb.close(); meta.write_text(json.dumps(identity,ensure_ascii=False),encoding='utf-8')

print('Analyzing cached real records',flush=True)
df=pd.read_parquet(cache)
df['time']=pd.to_datetime(df['time'],errors='raise')
numeric=['Type','carStatus','can.canStatus','can.totalMileage','can.engine.totalFuelUsed','can.canSpeed','gps.speed','gps.longitude','gps.latitude','gps.speedLimit','can.engine.rpm','can.engine.engineLoad']
for col in numeric: df[col]=pd.to_numeric(df[col],errors='coerce')
df.sort_values(['enabledCode','journeyCode','time'],inplace=True,kind='stable')
parts=[]
for slot in range(3):
    cols=['type','startTime','endTime','info.duration']
    part=df[['enabledCode','journeyCode','time','gps.longitude','gps.latitude']+[f'event[{slot}].{c}' for c in cols]].copy()
    part.rename(columns={f'event[{slot}].{c}':c for c in cols},inplace=True)
    parts.append(part[part['type'].notna()])
raw_events=pd.concat(parts,ignore_index=True)
raw_events['startTime']=pd.to_datetime(raw_events['startTime'],errors='coerce')
raw_events['endTime']=pd.to_datetime(raw_events['endTime'],errors='coerce')
raw_events['info.duration']=pd.to_numeric(raw_events['info.duration'],errors='coerce')
ekeys=['enabledCode','type','startTime']
raw_events.sort_values('time',inplace=True)
events=raw_events.groupby(ekeys,dropna=False).agg(journeyCode=('journeyCode','first'),first_received=('time','min'),last_received=('time','max'),endTime=('endTime','max'),duration=('info.duration','max'),longitude=('gps.longitude','first'),latitude=('gps.latitude','first'),reports=('time','size')).reset_index()
events.to_csv(OUT/'events_deduplicated.csv',index=False,encoding='utf-8-sig')
event_groups={(a,b):g for (a,b),g in events.groupby(['enabledCode','journeyCode'])}
steps=Counter(); records=[]
for (vehicle,journey),g in df.groupby(['enabledCode','journeyCode'],sort=False):
    times=g['time']; ts=times.astype('int64').to_numpy()/1e9; dt=np.diff(ts)
    mile=g['can.totalMileage'].to_numpy(); fuel=g['can.engine.totalFuelUsed'].to_numpy()
    dm=np.diff(mile); fuel_diff=np.diff(fuel)
    steps.update(np.round(fuel_diff[fuel_diff>0],6).tolist())
    dist=float(mile[-1]-mile[0]); used=float(fuel[-1]-fuel[0]); elapsed=float(ts[-1]-ts[0])
    speed=g['can.canSpeed'].to_numpy(); states=g['carStatus'].to_numpy(); load=g['can.engine.engineLoad'].to_numpy(); rpm=g['can.engine.rpm'].to_numpy()
    good=(dt>0)&(dt<=120); duration=float(dt[good].sum()); weights=np.where(good,dt,0)
    idle=float(weights[states[:-1]==2].sum()); speed_integral=float((speed[:-1]*weights).sum()/3600)
    ev=event_groups.get((vehicle,journey)); counts=Counter(ev['type']) if ev is not None else Counter()
    engine_start=False; parked_end=False
    if ev is not None:
        on=ev[ev['type']=='4']['startTime']; off=ev[ev['type']=='3']['startTime']
        engine_start=bool(((on-times.iloc[0]).dt.total_seconds().abs()<=120).any())
        parked_end=bool(((off-times.iloc[-1]).dt.total_seconds().abs()<=120).any())
    monotonic=bool(np.all(dm>=0) and np.all(fuel_diff>=0))
    candidate=bool(dist>=10 and used>0 and monotonic)
    clean=bool(candidate and g['can.canStatus'].eq(0).all() and np.all(dt>0) and np.all(dt<=120) and engine_start and parked_end)
    r={'vehicle':vehicle,'journey':journey,'type':int(g['Type'].iloc[0]),'start':times.iloc[0].isoformat(),'end':times.iloc[-1].isoformat(),'records':len(g),'km':dist,'fuel_l':used,'l_per_100km':used/dist*100 if dist>0 else None,'minutes':elapsed/60,'coverage_fraction':duration/elapsed if elapsed else 0,'max_gap_s':float(max(dt)) if len(dt) else 0,'can_abnormal_rows':int(g['can.canStatus'].ne(0).sum()),'duplicate_times':int((dt==0).sum()),'fuel_decreases':int((fuel_diff<0).sum()),'mileage_decreases':int((dm<0).sum()),'engine_start_near_boundary':engine_start,'parked_end_near_boundary':parked_end,'candidate':candidate,'screened':clean,'idle_estimated_minutes':idle/60,'idle_estimated_fraction':idle/duration if duration else None,'mean_can_speed_time_weighted':float((speed[:-1]*weights).sum()/duration) if duration else None,'mean_rpm_time_weighted':float((rpm[:-1]*weights).sum()/duration) if duration else None,'mean_engine_load_time_weighted':float((load[:-1]*weights).sum()/duration) if duration else None,'speed_integrated_km':speed_integral,'start_lon':g['gps.longitude'].iloc[0],'start_lat':g['gps.latitude'].iloc[0],'end_lon':g['gps.longitude'].iloc[-1],'end_lat':g['gps.latitude'].iloc[-1]}
    for et in ['2','6','7','8','9','11','12','14','16','19','20']: r['event_'+et]=counts[et]
    records.append(r)
trips=pd.DataFrame(records)
trips.to_csv(OUT/'trip_metrics.csv',index=False,encoding='utf-8-sig')

def describe(vals):
    s=pd.Series(vals).dropna()
    return {str(q):float(s.quantile(q)) for q in [0,.25,.5,.75,.95,1]} if len(s) else {}
def population(frame):
    return {'trips':len(frame),'vehicles':int(frame['vehicle'].nunique()),'by_type':{str(k):int(v) for k,v in frame.groupby('type').size().items()},'l_per_100km':describe(frame['l_per_100km'])}
def hav(lon1,lat1,lon2,lat2):
    a,b,c,d=map(math.radians,[lon1,lat1,lon2,lat2]); x=math.sin((d-b)/2)**2+math.cos(b)*math.cos(d)*math.sin((c-a)/2)**2
    return 6371*2*math.asin(min(1,math.sqrt(x)))

pool=trips[trips['screened']].copy()
pairs=[]
for vehicle,g in pool.groupby('vehicle'):
    rr=g.sort_values('start').to_dict('records')
    for i,b in enumerate(rr):
        for a in rr[:i]:
            if a['end']>=b['start']: continue
            if not .8<=b['km']/a['km']<=1.2: continue
            coords=[a[k] for k in ['start_lon','start_lat','end_lon','end_lat']]+[b[k] for k in ['start_lon','start_lat','end_lon','end_lat']]
            if not all(math.isfinite(x) for x in coords): continue
            start_gap=hav(a['start_lon'],a['start_lat'],b['start_lon'],b['start_lat']); end_gap=hav(a['end_lon'],a['end_lat'],b['end_lon'],b['end_lat'])
            if start_gap>1 or end_gap>1: continue
            pairs.append({'vehicle':vehicle,'earlier_journey':a['journey'],'later_journey':b['journey'],'earlier_start':a['start'],'later_start':b['start'],'earlier_km':a['km'],'later_km':b['km'],'earlier_fuel_l':a['fuel_l'],'later_fuel_l':b['fuel_l'],'earlier_l_per_100km':a['l_per_100km'],'later_l_per_100km':b['l_per_100km'],'earlier_idle_minutes_estimated':a['idle_estimated_minutes'],'later_idle_minutes_estimated':b['idle_estimated_minutes'],'earlier_event8':a['event_8'],'later_event8':b['event_8'],'start_gap_km':start_gap,'end_gap_km':end_gap,'both_fuel_at_least_5l':a['fuel_l']>=5 and b['fuel_l']>=5})
pairdf=pd.DataFrame(pairs)
pairdf.to_csv(OUT/'similar_endpoint_pairs.csv',index=False,encoding='utf-8-sig')
strong=pairdf[pairdf['both_fuel_at_least_5l']] if len(pairdf) else pairdf

idle=events[events['type']=='2'].copy()
idle['elapsed_s']=(idle['endTime']-idle['startTime']).dt.total_seconds()
idle['duration_matches']=idle['duration'].notna()&idle['elapsed_s'].notna()&((idle['duration']-idle['elapsed_s']).abs()<=2)&(idle['elapsed_s']>=0)
idle['location_valid']=idle['longitude'].between(119,123)&idle['latitude'].between(21,26)
idle['start_report_gap_s']=(idle['first_received']-idle['startTime']).dt.total_seconds()
loc=idle[idle['duration_matches']&idle['location_valid']&(idle['start_report_gap_s'].abs()<=120)].copy()
# Connected components of locations within 300 m; exploratory stop clusters only.
from scipy.spatial import cKDTree
if len(loc):
    la=np.radians(loc['latitude'].to_numpy()); lo=np.radians(loc['longitude'].to_numpy())
    xyz=6371000*np.column_stack([np.cos(la)*np.cos(lo),np.cos(la)*np.sin(lo),np.sin(la)])
    parent=list(range(len(loc)))
    def find(a):
        while parent[a]!=a: parent[a]=parent[parent[a]]; a=parent[a]
        return a
    for a,b in cKDTree(xyz).query_pairs(300):
        aa,bb=find(a),find(b)
        if aa!=bb: parent[aa]=bb
    loc['cluster']=[find(i) for i in range(len(loc))]
    loc['date']=loc['startTime'].dt.date
    clusters=loc.groupby('cluster').agg(events=('type','size'),days=('date','nunique'),vehicles=('enabledCode','nunique'),total_minutes=('duration',lambda s:s.sum()/60),median_minutes=('duration',lambda s:s.median()/60),longitude=('longitude','mean'),latitude=('latitude','mean')).reset_index()
    repeated=clusters[(clusters['events']>=3)&(clusters['days']>=2)].sort_values('total_minutes',ascending=False)
    repeated.to_csv(OUT/'repeated_idle_location_candidates.csv',index=False,encoding='utf-8-sig')
else: repeated=pd.DataFrame()

safety=events[events['type'].isin(['6','7','8','12','14','16','19','20'])]
presence=safety.groupby(['enabledCode','journeyCode'])['type'].nunique()
summary={'source':identity,'rows':len(df),'vehicles':df['enabledCode'].nunique(),'trips':len(trips),'observation_dates':int(df['time'].dt.date.nunique()),'trip_start_dates':int(pd.to_datetime(trips['start']).dt.date.nunique()),'candidate':population(trips[trips['candidate']]),'screened':population(pool),'screen_rules':['distance >= 10 km','fuel delta > 0; fuel and mileage never decrease within trip','all CAN status == 0','no duplicate timestamps; no gap above 120 seconds','engineOn within 120 seconds of first record; parked within 120 seconds of last record'],'screened_fuel_thresholds':{str(n):population(pool[pool['fuel_l']>=n]) for n in [1,2,5,10]},'positive_fuel_step_top':steps.most_common(10),'positive_fuel_steps':sum(steps.values()),'screened_mileage_speed_agreement_20pct_or_2km':int(((pool['km']-pool['speed_integrated_km']).abs()<=np.maximum(2,pool['km']*.2)).sum()),'similar_endpoint_pairs':len(pairdf),'pairs_both_fuel_ge5l':len(strong),'pairs_ge5l_vehicles':int(strong['vehicle'].nunique()) if len(strong) else 0,'pairs_ge5l_distinct_trips':len(set(zip(strong['vehicle'],strong['earlier_journey']))|set(zip(strong['vehicle'],strong['later_journey']))) if len(strong) else 0,'pairs_ge5l_later_trips_with_prior':int(strong[['vehicle','later_journey']].drop_duplicates().shape[0]) if len(strong) else 0,'pair_definition':'same vehicle; earlier trip ends before later trip; later/earlier mileage in [0.8,1.2]; origins and destinations each within 1 km. Does NOT establish same route, load, traffic or causal comparison.','idle_events':len(idle),'idle_duration_matches_within2s':int(idle['duration_matches'].sum()),'idle_start_location_candidates':len(loc),'repeated_idle_clusters':len(repeated),'repeated_idle_cluster_events':int(repeated['events'].sum()) if len(repeated) else 0,'idle_cluster_method':'300m connected components, >=3 events and >=2 observation dates; approximate locations, not verified unloading sites; chain-connected clusters may span more than 300m.','trips_two_or_more_safety_event_types':int((presence>=2).sum()),'trips_three_or_more_safety_event_types':int((presence>=3).sum()),'safety_event_types_by_vehicle':{str(v):sorted(g['type'].unique().tolist()) for v,g in safety.groupby('enabledCode')},'events_missing_start':int(events['startTime'].isna().sum()),'runtime_s':time.time()-started}
(OUT/'topic_validation.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2,default=lambda v:int(v) if isinstance(v,np.integer) else str(v)),encoding='utf-8')
print(json.dumps({k:v for k,v in summary.items() if k!='safety_event_types_by_vehicle'},ensure_ascii=False,indent=2,default=lambda v:int(v) if isinstance(v,np.integer) else str(v)),flush=True)
