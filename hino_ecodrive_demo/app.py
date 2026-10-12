"""Run: python -m streamlit run hino_ecodrive_demo/app.py"""
from __future__ import annotations

import hashlib
import json
from html import escape
from pathlib import Path

import streamlit as st

from engine import Config, NAMES, SCENARIOS, analyze, read_replay, scenario
from csv_import import uploaded_data
from xlsx_import import read_sheet, sheet_info, trip_data
from route_search import render_route_search

HERE = Path(__file__).resolve().parent
st.set_page_config(page_title="車隊行程分析與路線比較平台", page_icon="🚛", layout="wide")
st.markdown(f'<style>{(HERE / "app.css").read_text(encoding="utf-8")}</style>', unsafe_allow_html=True)

with st.sidebar:
    st.markdown('<div class="sidebar-brand"><span>HINO · DRIVING INTELLIGENCE</span><h1>工作台</h1><p>歷史行程分析與駕駛教練</p></div>', unsafe_allow_html=True)

feature = st.sidebar.radio("功能", ["駕駛回放", "共同起終點搜尋"], key="feature")
if feature == "共同起終點搜尋":
    st.markdown(f'<style>{(HERE / "route_search.css").read_text(encoding="utf-8")}</style>', unsafe_allow_html=True)
    with st.container(key="od_page"):
        render_route_search(HERE.parent)
    st.stop()

with st.sidebar:
    mode = st.radio("資料模式", ["真實 HINO 行程", "模擬情境"],key="data_mode")
    choice = st.selectbox("展示情境", list(SCENARIOS), format_func=SCENARIOS.get, disabled=mode.startswith("真實"),key="scenario")
    st.info("真實行程的急加減速、怠速與超速沿用車機事件；模擬情境使用下方參數判定。")
    enabled = st.multiselect("啟用提醒", list(Config().enabled), default=list(Config().enabled), format_func=NAMES.get)
    with st.expander("急加速 / 急減速參數"):
        accel_one = st.number_input("V4−V3 >（km/h）", min_value=0.0, value=10.0)
        accel_two = st.number_input("V4−V2、V3−V1 都 >（km/h）", min_value=0.0, value=5.0)
        decel_two = st.number_input("V4−V2、V3−V1 都 ≤（km/h）", max_value=-0.1, value=-15.0)
        rapid_cooldown = st.number_input("同類事件最短間隔（資料秒）", min_value=1.0, value=10.0)
    with st.expander("怠速參數"):
        idle_seconds = st.number_input("怠速累計時間（秒）", min_value=1.0, value=300.0)
        idle_stop_speed = st.number_input("怠速車速等於（km/h）", min_value=0.0, value=0.0)
        idle_reset_speed = st.number_input("未觸發前，車速超過則歸零", min_value=0.0, value=10.0)
        idle_release_speed = st.number_input("解除怠速的車速下限", min_value=0.0, value=10.0)
        idle_release_seconds = st.number_input("解除怠速持續時間（秒）", min_value=1.0, value=30.0)
    with st.expander("超速參數"):
        speed_margin = st.number_input("路段限速加值（km/h）", min_value=0.0, value=10.0)
        speed_absolute = st.number_input("固定超速觸發車速（km/h）", min_value=1.0, value=107.0)
        speed_release = st.number_input("固定超速解除車速（km/h）", min_value=0.0, value=95.0)
        speed_hold = st.number_input("超速觸發 / 解除持續時間（秒）", min_value=1.0, value=5.0)
    with st.expander("自訂高 RPM / 資料品質"):
        high_rpm = st.number_input("高 RPM 嚴格大於", min_value=1.0, value=2500.0)
        low_speed = st.number_input("低速嚴格小於（km/h）", min_value=0.0, value=30.0)
        rpm_hold = st.number_input("高 RPM 持續時間（秒）", min_value=1.0, value=5.0)
        max_gap = st.number_input("持續觀測的最大間隔（秒）", min_value=1.0, value=10.0)
    st.caption("切換模式或調整參數將重設播放。語音節流不影響事件統計。")

config = Config(accel_one=accel_one, accel_two=accel_two, decel_two=decel_two, rapid_cooldown=rapid_cooldown,
                idle_seconds=idle_seconds, idle_stop_speed=idle_stop_speed, idle_reset_speed=idle_reset_speed,
                idle_release_speed=idle_release_speed, idle_release_seconds=idle_release_seconds,
                speed_margin=speed_margin, speed_absolute=speed_absolute, speed_release=speed_release, speed_hold=speed_hold,
                high_rpm=high_rpm, low_speed=low_speed, rpm_hold=rpm_hold, max_gap=max_gap, enabled=tuple(enabled))


@st.cache_data(show_spinner=False)
def real_data(csv_bytes: bytes, meta_text: str, config: Config):
    # File content is part of the cache key; re-exporting invalidates the cached timeline.
    rows = read_replay(HERE / "replay.csv")
    metadata = json.loads(meta_text)
    if hashlib.sha256(csv_bytes).hexdigest() != metadata.get("replay_sha256"):
        raise ValueError("CSV 雜湊值與來源資訊不一致，請重新匯出")
    if metadata.get("mode") != "real" or len(rows) != metadata.get("records"):
        raise ValueError("replay.csv 與來源資訊不一致，請重新執行 prepare_replay.py")
    if any(row.get("vehicle") != metadata.get("vehicle") or row.get("journey") != metadata.get("journey") for row in rows):
        raise ValueError("CSV 與來源資訊的車輛／行程不一致")
    return analyze(rows, config, real=True), metadata


import_data = st.cache_data(show_spinner=False, max_entries=8)(uploaded_data)
xlsx_info = st.cache_data(show_spinner=False, max_entries=2)(sheet_info)
xlsx_records = st.cache_data(show_spinner="正在讀取工作表並整理車輛與行程；大型 Excel 首次讀取需等待數分鐘…", max_entries=2)(read_sheet)

# Reserve the header above native import widgets; fill its trip details after loading.
replay_header = st.empty()


def show_replay_header(data=None):
    real = data['real'] if data is not None else mode.startswith("真實")
    badge = "真實 HINO 歷史回放" if real else "模擬資料 · 非真實量測"
    identity_text = "尚未載入行程"
    dataset_text = "請選擇下方行程資料"
    if data is not None:
        identity_text = f"{data['metadata']['vehicle']} / {data['metadata']['journey']}"
        seconds = int(data['duration'])
        dataset_text = f"{len(data['frames']):,} 筆 · {seconds // 60}:{seconds % 60:02d} · {data['gaps']} 處時間缺口"
    replay_header.markdown(f'''<header class="replay-hero">
      <div><div class="replay-eyebrow">HINO · DRIVING INTELLIGENCE</div>
      <h1>車隊行程分析與路線比較平台</h1><p>看見駕駛行為，聽見即時提醒。</p></div>
      <div class="replay-identity"><span class="replay-badge">{badge}</span>
      <strong>{escape(identity_text)}</strong><small>{dataset_text}</small></div>
    </header>''', unsafe_allow_html=True)


show_replay_header()
source = "內建範例"
uploaded = None
if mode.startswith("真實"):
    with st.container(border=True, key="replay_import"):
        st.subheader("行程資料")
        source = st.radio("資料來源", ["內建範例", "上傳 CSV", "上傳 XLSX"], horizontal=True, key="replay_source")
        if source == "上傳 CSV":
            uploaded = st.file_uploader("匯入單趟行程 CSV", type=["csv"], max_upload_size=20, key="replay_upload")
            st.caption("需包含逐筆時間、CAN 車速、RPM 與狀態；一份檔案只包含同一台車、同一趟行程。")
        elif source == "上傳 XLSX":
            uploaded = st.file_uploader("匯入 Excel 活頁簿", type=["xlsx"], max_upload_size=200, key="replay_upload_xlsx")
            st.caption("可直接使用原始 HINO Excel，或回放欄位格式的工作表。上傳後選工作表、車輛及行程；上限 200 MB。")
        else:
            st.caption("目前使用內建 replay.csv：車輛 C1N6TWBTVB，行程 250731030003。")
        with st.expander("支援格式與範例下載"):
            st.code("timestamp,speed,rpm,car_status,can_status\n2025-07-31 11:31:30,0,801.625,1,0\n2025-07-31 11:31:36,10,1200,1,0", language="csv")
            st.caption("必填：timestamp（日期時間）、speed（km/h）、rpm、car_status（0 停車／1 行駛／2 怠速）、can_status（0 正常）。時間保留實際間隔。")
            st.caption("選填：vehicle、journey、mileage（累積 km）、fuel（累積 L）、speed_limit、longitude、latitude、event_reports（車機事件 JSON 陣列）。")
            st.caption("XLSX 第一列需為欄位名稱；支援以上回放欄位，或 HINO 原始欄位 time、can.canSpeed、can.engine.rpm、carStatus、can.canStatus，並自動帶入 enabledCode、journeyCode 與 event[0..2]。Excel 日期儲存格也可使用。")
            st.caption("沒有車機事件欄位時不會建立急加減速、怠速及超速的車機事件，仍可回放車況與自訂高 RPM。trip_metrics.csv 是行程摘要，不能直接回放。")
            st.download_button("下載可播放的範例 CSV", (HERE / "replay.csv").read_bytes(), file_name="replay_example.csv", mime="text/csv", key="replay_example")
    if source.startswith("上傳") and uploaded is None:
        st.info(f"請先選擇 {source.split()[-1]} 檔案，上傳成功後會顯示回放儀表板。")
        st.stop()


try:
    config.validate()
    if mode.startswith("真實"):
        if source == "上傳 CSV":
            payload, metadata = import_data(uploaded.getvalue(), uploaded.name, config)
        elif source == "上傳 XLSX":
            content = uploaded.getvalue()
            info = xlsx_info(content)
            with st.container(border=True, key="xlsx_selection"):
                sheet = st.selectbox("工作表", list(info['sheets']), key="xlsx_sheet")
                data = xlsx_records(content, sheet)
                vehicles = sorted({trip['vehicle'] for trip in data['trips']})
                vehicle = st.selectbox("車輛", vehicles, format_func=lambda value: value or "未提供車輛", key="xlsx_vehicle")
                trips = {trip['journey']: trip for trip in data['trips'] if trip['vehicle'] == vehicle}
                journey = st.selectbox("行程", list(trips), key="xlsx_journey",
                                       format_func=lambda value: f"{value or '未提供行程編號'}｜{trips[value]['start']}｜{trips[value]['records']:,} 筆")
                st.caption(f"工作表共 {data['records']:,} 筆、{len(vehicles)} 台車、{len(data['trips']):,} 趟行程；只回放目前選取的行程。")
            payload, metadata = trip_data(data, uploaded.name, vehicle, journey, config)
        else:
            csv_path, meta_path = HERE / "replay.csv", HERE / "replay.meta.json"
            payload, metadata = real_data(csv_path.read_bytes(), meta_path.read_text(encoding="utf-8"), config)
            title = "真實行程 · 車機事件回放"
        if source.startswith("上傳"):
            title = "匯入行程 · 車況與車機事件回放"
            st.success(f"已匯入 {uploaded.name}，共 {len(payload['frames']):,} 筆。切換檔案會重新開始回放。")
            if not any(event['source'] == '車機回報' for event in payload['events']):
                st.info("這份資料沒有可列出的車機事件；車況與自訂高 RPM 仍可回放。")
    else:
        payload = analyze(scenario(choice), config)
        metadata = {"vehicle": "SIMULATED", "journey": choice, "reference": None}
        title = SCENARIOS[choice] + " · 逐秒模擬情境"
except (OSError, ValueError, TypeError) as error:
    st.error(f"無法準備回放資料：{error}")
    st.caption("匯入檔請依「支援格式與範例下載」修正；內建資料可執行 python hino_ecodrive_demo/prepare_replay.py 重新匯出。")
    st.stop()

payload.update(metadata=metadata, title=title)
show_replay_header(payload)
identity = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
dashboard = st.components.v2.component(
    "hino_ecodrive_dashboard", html=(HERE / "dashboard.html").read_text(encoding="utf-8"),
    css=(HERE / "dashboard.css").read_text(encoding="utf-8"), js=(HERE / "frontend.mjs").read_text(encoding="utf-8"),
)
dashboard(data=payload, key=f"ecodrive_{identity}")
