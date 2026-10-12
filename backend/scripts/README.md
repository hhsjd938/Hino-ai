# HINO Python scripts

The scripts are grouped by responsibility. The original workbook stays on the data owner's local disk and is never modified.

## Collaboration data flow

One data owner runs the pipeline against the local XLSX workbook. Generated, reviewable outputs are written under `data/processed` and can be committed to the private GitHub repository for the model, backend, and frontend teams. Rebuildable caches belong under `data/cache` and should not be committed.

## `data_pipeline`

- `profile_hino.py`: scans the local workbook and writes a JSON data profile.
- `verify_topics.py`: creates the Parquet cache, trip metrics, deduplicated events, route candidates, idle-location candidates, and topic-validation summary.
- `verify_route_pairs.py`: calculates bidirectional GPS coverage for candidate route pairs.
- `summarize_route_groups.py`: groups trips connected by validated route overlap and writes repeated-route summaries.
- `build_route_stop_map.py`: streams GPS points for the repeated-route trips and creates an interactive route-and-stop map. A suspected intermediate stop means CAN speed stayed at or below 3 km/h for at least 3 minutes and the location is more than 300 metres from both trip endpoints; it does not prove a customer visit.
- `build_od_route_map.py`: creates a separate OpenStreetMap page with origin/destination coordinate search across vehicles and route groups. It preserves the original map's appearance, trip checkboxes and suspected stops; the original map is not overwritten.

```powershell
python backend/scripts/data_pipeline/profile_hino.py `
  --source "F:\path\to\output data_Hotai_20260511.xlsx" `
  --output "data\processed\hino_profile.json"

python backend/scripts/data_pipeline/verify_topics.py `
  --source "F:\path\to\output data_Hotai_20260511.xlsx" `
  --output-dir "data\processed\analysis"

python backend/scripts/data_pipeline/verify_route_pairs.py `
  --analysis-dir "data\processed\analysis"

python backend/scripts/data_pipeline/summarize_route_groups.py `
  --pairs "data\processed\analysis\route_screened_pairs.csv" `
  --output-dir "data\processed\analysis" `
  --threshold 0.9

python backend/scripts/data_pipeline/build_route_stop_map.py `
  --source "F:\path\to\output data_Hotai_20260511.xlsx" `
  --members "data\processed\analysis\route_group_members.csv" `
  --output "data\processed\analysis\route_stop_map.html"
```

地圖產生後請從專案根目錄執行 `python -m http.server 8000`，再開啟
`http://localhost:8000/data/processed/analysis/route_stop_map.html`。不要直接以
`file://` 開啟，否則 OpenStreetMap tile 請求可能缺少有效的網頁來源資訊。

## 獨立的共同起終點 OpenStreetMap

從專案根目錄建立新頁面（會串流讀取 Excel，需等待數分鐘）：

```powershell
.venv\Scripts\python.exe backend/scripts/data_pipeline/build_od_route_map.py
```

預設讀取 `hino/output data_Hotai_20260511.xlsx`、現有 `trip_metrics.csv` 與群組 CSV，輸出 **`data/processed/analysis/od_route_map.html`**。可用 `--source`、`--analysis-dir`、`--output` 指定路徑；程式拒絕覆寫同資料夾中的原本 `route_stop_map.html`。

沿用目前 8001 靜態伺服器即可開啟：
<http://localhost:8001/data/processed/analysis/od_route_map.html>。
如果尚未啟動伺服器，從專案根目錄執行 `python -m http.server 8001 --bind 127.0.0.1`。

1. 輸入起點／終點緯度及經度，或選既有行程後按「帶入所選行程」。
2. 設定兩端各自容許距離，預設 1,000 公尺，再按「搜尋共同起終點」。起點和終點都必須符合，往返方向分開比對。
3. 右側查看符合趟數、車輛、所屬群組及行程明細。點群組或行程可單獨顯示；頂部可勾選多趟或「顯示全部符合行程」。
4. 可交換方向、收合搜尋面板、清除結果、下載全部符合行程 CSV。

搜尋預設涵蓋所有起終點有效的行程，**不限制同車、同群組或 ≥10 km**。可選擇勾選完整品質篩選（整趟里程 >0、整趟耗油 >0，並保留 CAN、時間及行程邊界檢查），但沒有隱藏的里程比例限制。此次資料共 3,541 趟，3,538 趟兩端座標位於既有 HINO 台灣資料範圍（經度 119～123、緯度 21～26）；其中 3,536 趟有可繪線的 GPS。其餘只標起終點，不補畫路線。

群組只是沿用既有結果，不重新分群，也不把「未分群」當作同一路徑或不同道路走法。耗油中位數只使用本次符合行程。GPS 軌跡每 60 秒或位移 100 公尺保留一點；缺少座標、重複時間或間隔超過 120 秒會分段，不跨資料缺口連線。疑似停靠沿用車速 ≤3 km/h、至少 3 分鐘、排除起終點 300 公尺的規則。

搜尋與資料都在本機；底圖使用 **Leaflet 1.9.4 + OpenStreetMap**，需要網路載入 Leaflet 與 OSM 圖磚。程式沒有使用 CARTO、導航 API 或地理編碼服務。產生的 HTML 內含 GPS 資料，沒有從原始 Excel 重新讀取的執行期需求。

```powershell
.venv\Scripts\python.exe -m unittest discover -s backend/tests -p test_od_route_map.py -v
node --test backend/tests/od_route_map.test.cjs
```

測試涵蓋兩端距離、方向、跨車／跨群組與短行程、可選品質條件、分段軌跡、群組統計、CSV 與地圖控制流程。DOM／Leaflet 以替身驗證，不等同實際瀏覽器視覺驗收。

## 路線分群門檻

每趟須符合 **整趟里程 >0 km 且整趟耗油 >0 L**（依累積值最後一筆減第一筆計算），不再限制 ≥10 km 或兩趟耗油都 ≥5 L。其他品質條件、同車、時間不重疊、里程比 0.8～1.2、兩端各 1 km、雙向 GPS 覆蓋率 ≥90% 均保留。`both_fuel_at_least_5l` 與相關油量門檻統計僅供描述，不再用於篩選。

重新執行上方 `verify_topics.py`、`verify_route_pairs.py`、`summarize_route_groups.py`，接著產生兩張地圖即可套用新結果。

## `route_poc`

- `extract_trip.py`: streams the local XLSX and extracts one vehicle journey for the route POC.
- `build_route_poc.py`: obtains truck routes and elevation profiles, estimates fuel and cost, and generates JSON plus an HTML comparison page.

`build_route_poc.py` sends the selected trip endpoints and configured truck dimensions to the selected Valhalla endpoint. Review the endpoint and data-sharing requirements before running it.

```powershell
python backend/scripts/route_poc/extract_trip.py `
  --source "F:\path\to\output data_Hotai_20260511.xlsx" `
  --vehicle "AHMPUL0C13" `
  --journey "251121060007" `
  --output "data\processed\route_poc\trip.json"

python backend/scripts/route_poc/build_route_poc.py `
  --trip-json "data\processed\route_poc\trip.json" `
  --coverage-csv "data\processed\analysis\historical_reference_coverage.csv" `
  --diesel-price 29.3 `
  --output-json "data\processed\route_poc\route_comparison.json" `
  --output-html "data\processed\route_poc\route_comparison.html"
```
