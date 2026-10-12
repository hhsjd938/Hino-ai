"建立資料快取並整理事件"


import argparse
from pathlib import Path
import pandas as pd
import numpy as np
from scipy.spatial import cKDTree
import json

parser=argparse.ArgumentParser(description='Validate candidate pairs with bidirectional GPS-route coverage.')
parser.add_argument('--analysis-dir',type=Path,required=True,help='Directory containing similar_endpoint_pairs.csv and source_records.parquet')
args=parser.parse_args()

p=args.analysis_dir.resolve()
pairs=pd.read_csv(p/'similar_endpoint_pairs.csv',dtype={'vehicle':str,'earlier_journey':str,'later_journey':str})
# Both trips need positive whole-trip deltas; the 5 L column is descriptive only.
positive_columns=['earlier_km','later_km','earlier_fuel_l','later_fuel_l']
pairs=pairs[pairs[positive_columns].gt(0).all(axis=1)].copy()
keys=set(zip(pairs.vehicle,pairs.earlier_journey))|set(zip(pairs.vehicle,pairs.later_journey))
d=pd.read_parquet(p/'source_records.parquet',columns=['enabledCode','journeyCode','gps.longitude','gps.latitude'])
take=pd.DataFrame(list(keys),columns=['enabledCode','journeyCode'])
d=d.merge(take,on=['enabledCode','journeyCode'],how='inner')
for c in ['gps.longitude','gps.latitude']: d[c]=pd.to_numeric(d[c],errors='coerce')
d=d[d['gps.longitude'].between(119,123)&d['gps.latitude'].between(21,26)]
grids={}; trees={}
for k,g in d.groupby(['enabledCode','journeyCode']):
    xy=np.column_stack([g['gps.longitude'].to_numpy()*111320*np.cos(np.radians(23.5)),g['gps.latitude'].to_numpy()*111320])
    xy=np.unique(np.round(xy/250)*250,axis=0)
    grids[k]=xy; trees[k]=cKDTree(xy)
results=[]
for r in pairs.to_dict('records'):
    ka=(r['vehicle'],r['earlier_journey']); kb=(r['vehicle'],r['later_journey'])
    if ka not in grids or kb not in grids: continue
    aa=trees[kb].query(grids[ka])[0]; bb=trees[ka].query(grids[kb])[0]
    r['earlier_route_coverage']=float((aa<=500).mean()); r['later_route_coverage']=float((bb<=500).mean())
    r['route_min_coverage']=min(r['earlier_route_coverage'],r['later_route_coverage'])
    r['rate_change_pct']=(r['later_l_per_100km']/r['earlier_l_per_100km']-1)*100
    results.append(r)
result=pd.DataFrame(results)
result.to_csv(p/'route_screened_pairs.csv',index=False,encoding='utf-8-sig')

# Keep the historical reference table in sync with the same directly verified
# pairs used for grouping. References include only the earlier trips in a pair.
verified=result[result.route_min_coverage.ge(.9)]
references=[]
for (vehicle,journey),g in verified.groupby(['vehicle','later_journey']):
    current=g.iloc[0]
    median=float(g['earlier_l_per_100km'].median())
    references.append({'vehicle':vehicle,'journey':journey,
                       'prior_count':int(g['earlier_journey'].nunique()),
                       'prior_dates':int(g['earlier_start'].str[:10].nunique()),
                       'median_prior_l_per_100km':median,
                       'current_l_per_100km':current['later_l_per_100km'],
                       'difference_from_median_pct':(current['later_l_per_100km']/median-1)*100,
                       'start':current['later_start'],'km':current['later_km'],
                       'fuel_l':current['later_fuel_l']})
reference_columns=['vehicle','journey','prior_count','prior_dates','median_prior_l_per_100km',
                   'current_l_per_100km','difference_from_median_pct','start','km','fuel_l']
pd.DataFrame(references,columns=reference_columns).to_csv(
    p/'historical_reference_coverage.csv',index=False,encoding='utf-8-sig')
def summary(f):
    return {'pairs':len(f),'vehicles':int(f.vehicle.nunique()),'distinct_trips':len(set(zip(f.vehicle,f.earlier_journey))|set(zip(f.vehicle,f.later_journey))),'later_trips_with_prior':len(f[['vehicle','later_journey']].drop_duplicates())}
report={'method':'Both trips have whole-trip distance > 0 km and fuel > 0 L; same-vehicle endpoint and mileage-ratio screening followed by bidirectional geographic overlap. GPS points reduced to occupied 250m approximate planar grid cells; fraction of occupied cells within 500m of the other trip. Retrieval heuristic, not road-level route identity or proof of comparable load/traffic.','coverage_threshold_results':{str(q):summary(result[result.route_min_coverage>=q]) for q in [.8,.9,.95]},'main_threshold':.9}
(p/'route_validation.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report,indent=2))
examples=result[result.route_min_coverage.ge(.9)&result.rate_change_pct.between(15,50)&(result.earlier_start.str[:10]!=result.later_start.str[:10])].sort_values('later_fuel_l',ascending=False).head(5)
print(examples.to_json(orient='records',indent=2))
