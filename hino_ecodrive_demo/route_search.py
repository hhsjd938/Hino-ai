"""Directional origin/destination lookup over existing trip summaries."""
from __future__ import annotations

import csv
import io
import json
import math
import re
import statistics
from pathlib import Path

from engine import number


def coordinate(lat, lon) -> tuple[float, float]:
    lat, lon = number(lat), number(lon)
    if lat is None or lon is None or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError("緯度必須介於 −90～90，經度必須介於 −180～180，且為有效數字")
    return lat, lon


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return 6371000 * 2 * math.asin(math.sqrt(min(1.0, max(0.0, h))))


def csv_rows(path: Path, required: set[str]) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = required.difference(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path.name} 缺少欄位：{', '.join(sorted(missing))}")
        return list(reader)


def load_catalog(analysis_dir: Path, fingerprint: tuple = ()) -> dict:
    """fingerprint participates in Streamlit's cache key when source files change."""
    trips = csv_rows(analysis_dir/'trip_metrics.csv', {'vehicle','journey','start_lat','start_lon','end_lat','end_lon','screened'})
    members = csv_rows(analysis_dir/'route_group_members.csv', {'vehicle','journey','route_group'})
    groups = csv_rows(analysis_dir/'route_groups.csv', {'route_group','vehicle','trip_count','direct_pair_density'})
    group_index = {row['route_group']: row for row in groups}
    member_index = {(row['vehicle'],row['journey']): row['route_group'] for row in members}
    rows, invalid = [], 0
    for trip in trips:
        try:
            origin = coordinate(trip['start_lat'],trip['start_lon'])
            destination = coordinate(trip['end_lat'],trip['end_lon'])
        except ValueError:
            invalid += 1
            continue
        group_id = member_index.get((trip['vehicle'],trip['journey']))
        if group_id and (group_id not in group_index or group_index[group_id]['vehicle'] != trip['vehicle']):
            raise ValueError(f"路線群組與車輛對應不一致：{group_id}")
        rows.append({**trip, 'origin': origin, 'destination': destination,
                     'screened': str(trip['screened']).lower() == 'true', 'route_group': group_id,
                     **{key:number(trip.get(key)) for key in ('minutes','km','fuel_l','l_per_100km')}})
    return {'trips': rows, 'groups': group_index, 'total': len(trips), 'invalid_coordinates': invalid}


def search(trips: list[dict], origin, destination, radius_m=1000, *, screened_only=True, vehicles=()) -> list[dict]:
    origin, destination = coordinate(*origin), coordinate(*destination)
    radius_m = number(radius_m)
    if radius_m is None or radius_m <= 0:
        raise ValueError("搜尋半徑必須大於 0")
    selected_vehicles = set(vehicles)
    found = []
    for trip in trips:
        if screened_only and not trip['screened']:
            continue
        if selected_vehicles and trip['vehicle'] not in selected_vehicles:
            continue
        start_gap = distance_m(origin, trip['origin'])
        end_gap = distance_m(destination, trip['destination'])
        if start_gap <= radius_m and end_gap <= radius_m:
            found.append({**trip,'origin_gap_m':start_gap,'destination_gap_m':end_gap})
    return sorted(found, key=lambda row:(row['origin_gap_m']+row['destination_gap_m'],row['vehicle'],row['journey']))


def summarize(found: list[dict], groups: dict) -> list[dict]:
    buckets: dict[str,list[dict]] = {}
    for row in found:
        if row['route_group']:
            buckets.setdefault(row['route_group'],[]).append(row)
    result = []
    def median(rows,key):
        values = [row[key] for row in rows if row.get(key) is not None]
        return statistics.median(values) if values else None
    for group_id, rows in sorted(buckets.items()):
        group = groups[group_id]
        result.append({'路線群組':group_id,'車輛':group['vehicle'],'符合搜尋趟數':len(rows),
                       '群組總趟數':int(group['trip_count']), '中位里程 km':median(rows,'km'),
                       '中位時間 min':median(rows,'minutes'),'中位耗油 L':median(rows,'fuel_l'),
                       '中位 L/100km':median(rows,'l_per_100km'),
                       '配對品質':'完整配對' if math.isclose(float(group['direct_pair_density']),1) else '鏈式連接'})
    return result


def load_tracks(path: Path, fingerprint: tuple = ()) -> dict:
    """Read embedded JSON only; never execute the existing map's JavaScript."""
    if not path.exists():
        return {}
    match = re.search(r'const trips=(\[.*?\]);\s*const groupSel=',path.read_text(encoding='utf-8'),re.S)
    if not match:
        raise ValueError("既有地圖中找不到 GPS 軌跡資料")
    trips = json.loads(match.group(1))
    return {(row['vehicle'],row['journey']):row for row in trips}


def result_table(found: list[dict]) -> list[dict]:
    return [{'車輛':row['vehicle'],'行程':row['journey'],'出發時間':row.get('start',''),
             '路線群組':row['route_group'] or '未分群（僅符合起終點）',
             '起點距離 m':round(row['origin_gap_m'],1),'終點距離 m':round(row['destination_gap_m'],1),
             '里程 km':row['km'],'時間 min':row['minutes'],'耗油 L':row['fuel_l'],'L/100km':row['l_per_100km'],
             '通過品質篩選':row['screened'], '起點緯度':row['origin'][0],'起點經度':row['origin'][1],
             '終點緯度':row['destination'][0],'終點經度':row['destination'][1]} for row in found]


def export_csv(rows: list[dict]) -> bytes:
    if not rows:
        return b''
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    return buffer.getvalue().encode('utf-8-sig')


def render_route_search(repo: Path):
    import pydeck as pdk
    import streamlit as st

    analysis = repo/'data/processed/analysis'
    paths = [analysis/name for name in ('trip_metrics.csv','route_group_members.csv','route_groups.csv')]
    try:
        fingerprint = tuple((str(path),path.stat().st_mtime_ns,path.stat().st_size) for path in paths)
        catalog = st.cache_data(show_spinner=False)(load_catalog)(analysis,fingerprint)
    except (OSError,ValueError) as error:
        st.error(f"無法載入歷史行程：{error}"); return
    screened = [row for row in catalog['trips'] if row['screened']]
    st.markdown(f'''<header class="od-hero">
      <div><div class="od-eyebrow">HINO · DRIVING INTELLIGENCE</div>
      <h1>車隊行程分析與路線比較平台</h1>
      <p>共同起終點搜尋 · 找出歷史行程，比較不同路線。</p></div>
      <div class="od-identity"><span class="od-badge">歷史行程資料</span>
      <strong>{catalog['total']:,} 趟行程 · {len(catalog['groups']):,} 個路線群組</strong>
      <small>{len(screened):,} 趟通過品質篩選</small></div>
    </header>''', unsafe_allow_html=True)
    st.markdown('''<div class="od-note"><span class="od-note-icon">!</span><div>
      <span class="od-eyebrow">ROUTE EXPLORER</span><h2>以起終點尋找歷史路線</h2>
      <p>輸入經緯度，找出同方向、起終點都在指定半徑內的行程。
      共同起終點不代表同一路徑；既有群組依同車 GPS 路徑驗證，群組數不等於道路走法數。</p>
    </div></div>''', unsafe_allow_html=True)
    st.caption(f"共 {catalog['total']:,} 趟；其中 {len(screened):,} 趟通過品質篩選。{catalog['invalid_coordinates']} 趟因起終點座標缺漏或無效而無法搜尋。")
    seeds = sorted(catalog['trips'],key=lambda row:(not row['screened'],not bool(row['route_group']),row['vehicle'],row.get('start','')))
    if not seeds:
        st.warning('目前沒有有效起終點資料。'); return
    seed_index = {(row['vehicle'],row['journey']):row for row in seeds}
    with st.container(key='od_seed_panel'):
        st.markdown('<span class="od-eyebrow">HISTORICAL TRIPS</span>', unsafe_allow_html=True)
        seed_id = st.selectbox('從既有行程帶入起終點',list(seed_index),key='od_seed',
                              format_func=lambda key:f"{key[0]}｜{seed_index[key].get('start','')}｜{seed_index[key]['km']} km｜{seed_index[key]['route_group'] or '未分群'}")
        seed = seed_index[seed_id]
        fields = ('od_start_lat','od_start_lon','od_end_lat','od_end_lon')
        for field,value in zip(fields,(*seeds[0]['origin'],*seeds[0]['destination'])):
            st.session_state.setdefault(field,value)
        left,right = st.columns(2)
        if left.button('帶入所選行程起終點',key='od_fill'):
            for field,value in zip(fields,(*seed['origin'],*seed['destination'])):
                st.session_state[field] = value
        if right.button('交換起點與終點',key='od_swap'):
            values = [st.session_state[field] for field in fields]
            for field,value in zip(fields,(*values[2:],*values[:2])):
                st.session_state[field] = value
    with st.form('od_form'):
        st.markdown('<span class="od-eyebrow">SEARCH PARAMETERS</span>', unsafe_allow_html=True)
        st.subheader('設定搜尋範圍')
        origin_col,destination_col = st.columns(2)
        with origin_col:
            st.subheader('起點')
            start_lat = st.number_input('起點緯度',min_value=-90.0,max_value=90.0,format='%.6f',step=0.0001,key=fields[0])
            start_lon = st.number_input('起點經度',min_value=-180.0,max_value=180.0,format='%.6f',step=0.0001,key=fields[1])
        with destination_col:
            st.subheader('終點')
            end_lat = st.number_input('終點緯度',min_value=-90.0,max_value=90.0,format='%.6f',step=0.0001,key=fields[2])
            end_lon = st.number_input('終點經度',min_value=-180.0,max_value=180.0,format='%.6f',step=0.0001,key=fields[3])
        radius = st.number_input('起點、終點各自的搜尋半徑（公尺）',min_value=50,max_value=50000,value=1000,step=50,key='od_radius')
        only_clean = st.checkbox('只查通過品質篩選的行程',value=True,key='od_clean')
        vehicles = st.multiselect('車輛篩選（留空代表全部）',sorted({row['vehicle'] for row in catalog['trips']}),key='od_vehicles')
        submitted = st.form_submit_button('搜尋共同起終點',type='primary')
    if submitted:
        st.session_state['od_query'] = {'origin':(start_lat,start_lon),'destination':(end_lat,end_lon),
                                        'radius_m':radius,'screened_only':only_clean,'vehicles':vehicles}
    query = st.session_state.get('od_query')
    if not query:
        st.success('已預填一趟具有重複路線群組的行程座標，按「搜尋共同起終點」即可試用。'); return
    found = search(catalog['trips'],**query)
    st.subheader('搜尋結果')
    st.caption(f"本次查詢：{query['origin']} → {query['destination']}；兩端各 {query['radius_m']:,} m。調整上方輸入後需再次按搜尋才會更新結果。")
    if not found:
        st.warning('沒有符合條件的歷史行程。可放寬半徑、交換方向或調整品質／車輛篩選。'); return
    summaries = summarize(found,catalog['groups'])
    ungrouped = sum(not row['route_group'] for row in found)
    for column,label,value in zip(st.columns(4),('符合行程','已驗證路線群組','未分群行程','涉及車輛'),
                                  (len(found),len(summaries),ungrouped,len({row['vehicle'] for row in found}))):
        column.metric(label,value)
    with st.container(key='od_results_panel'):
        st.markdown('<span class="od-eyebrow">MATCHED TRIPS</span>', unsafe_allow_html=True)
        if summaries:
            st.markdown('**已驗證的路線群組**')
            st.dataframe(summaries,hide_index=True,width='stretch')
            st.caption('表中時間與油耗中位數僅使用本次符合搜尋的行程。鏈式連接群組中，部分行程可能僅透過其他行程間接相連。')
        if ungrouped:
            st.caption(f'{ungrouped} 趟只確認起終點符合，尚未驗證道路走法；不把每趟行程當成一條不同路線。')
        table = result_table(found)
        st.dataframe(table,hide_index=True,width='stretch')
        st.download_button('下載符合行程 CSV',export_csv(table),file_name='common_od_trips.csv',mime='text/csv')
    with st.container(key='od_map_panel'):
        st.markdown('<span class="od-eyebrow">GPS ROUTE VIEW</span>', unsafe_allow_html=True)
        map_path = analysis/'route_stop_map.html'
        try:
            map_fingerprint = (map_path.stat().st_mtime_ns,map_path.stat().st_size) if map_path.exists() else ()
            tracks = st.cache_data(show_spinner=False)(load_tracks)(map_path,map_fingerprint)
        except (OSError,ValueError,json.JSONDecodeError) as error:
            st.warning(f'無法載入既有 GPS 軌跡，仍可查詢起終點：{error}'); tracks = {}
        available = [row for row in found if (row['vehicle'],row['journey']) in tracks]
        st.subheader('GPS 軌跡與起終點')
        st.caption(f'{len(available)} / {len(found)} 趟有現成 GPS 軌跡；其餘只標示起終點，沒有繪製推測路線。')
        map_groups = sorted({row['route_group'] for row in available if row['route_group']})
        selected = st.multiselect('地圖顯示的路線群組',map_groups,default=map_groups,key='od_map_groups')
        basemap = st.checkbox('顯示道路底圖（需連線至 CARTO 底圖服務）',value=False,key='od_basemap')
        palette = [[30,105,170],[142,68,173],[0,137,123],[230,81,0],[194,24,91],[79,100,40]]
        colors = {group:palette[i%len(palette)] for i,group in enumerate(map_groups)}
        paths_data, markers = [], []
        all_positions = []
        for row in found:
            label = f"{row['vehicle']} / {row['journey']} · {row['route_group'] or '未分群'}"
            for point,kind,color in ((row['origin'],'起點',[22,128,60]),(row['destination'],'終點',[201,42,42])):
                position = [point[1],point[0]]; all_positions.append(position)
                markers.append({'position':position,'color':color,'label':kind+' · '+label})
            track = tracks.get((row['vehicle'],row['journey']))
            if track and row['route_group'] in selected:
                path = [[point[1],point[0]] for point in track['points']]
                paths_data.append({'path':path,'color':colors[row['route_group']],'label':label})
                all_positions.extend(path)
        for point,kind,color in ((query['origin'],'輸入起點',[22,128,60]),(query['destination'],'輸入終點',[201,42,42])):
            markers.append({'position':[point[1],point[0]],'color':color,'label':kind})
        layers = [pdk.Layer('PathLayer',paths_data,get_path='path',get_color='color',width_min_pixels=3,pickable=True),
                  pdk.Layer('ScatterplotLayer',markers,get_position='position',get_fill_color='color',radius_min_pixels=5,radius_max_pixels=9,pickable=True),
                  pdk.Layer('ScatterplotLayer',[{'position':[p[1],p[0]],'color':c,'label':label} for p,c,label in
                            ((query['origin'],[22,128,60],'輸入起點搜尋範圍'),(query['destination'],[201,42,42],'輸入終點搜尋範圍'))],
                            get_position='position',get_line_color='color',get_radius=query['radius_m'],filled=False,stroked=True,line_width_min_pixels=2,pickable=True)]
        span = max(max(p[i] for p in all_positions)-min(p[i] for p in all_positions) for i in (0,1))
        zoom = min(14,max(4,math.log2(240/max(span,query['radius_m']/50000)) - 1))
        view = pdk.ViewState(latitude=statistics.mean(p[1] for p in all_positions),longitude=statistics.mean(p[0] for p in all_positions),zoom=zoom)
        deck = pdk.Deck(layers=layers,initial_view_state=view,map_provider='carto' if basemap else None,
                        map_style=pdk.map_styles.LIGHT if basemap else None,
                        parameters={'clearColor':[0.965,0.953,0.925,1]},
                        tooltip={'text':'{label}'},height=500)
        st.pydeck_chart(deck,key='od_map')
        st.caption('綠點為起點、紅點為終點；圓圈為搜尋半徑。相同群組的軌跡使用相同顏色，滑鼠移到軌跡可查看群組與行程。')
