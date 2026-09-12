# Trip Behavior 分析

比較同一台車、同一個既有 route group 內，各趟行程的怠速、高 RPM、高 RPM 低速、急加減速、stop-go、時間與油耗差異。

## 執行方式

在專案根目錄執行：

```powershell
python trip_behavior\analyze_trip_behavior.py `
  --source "hino\output data_Hotai_20260511.xlsx" `
  --route-members "data\processed\analysis\route_group_members.csv" `
  --route-groups "data\processed\analysis\route_groups.csv" `
  --output-dir "trip_behavior\output"
```

預設門檻：

- 高 RPM：`RPM >= 2000`
- 高 RPM 低速：`RPM >= 2000` 且 CAN 車速 `<= 20 km/h`
- stop-go：完整的 `>= 10 → <= 3 → >= 10 km/h` 循環
- 有效資料間隔：`0 < dt <= 120 秒`

所有門檻皆可由命令列參數調整；可執行 `python trip_behavior\analyze_trip_behavior.py --help` 查看。

## 產出

- `output/trip_behavior_metrics.csv`：每趟一列的完整指標與資料品質欄位。
- `output/trip_behavior_report.html`：可依車輛與路徑群組篩選的離線比較報告。
- `output/trip_behavior_summary.json`：門檻、來源身分、處理筆數、缺值及對帳結果。

## 計算原則

- 行為分鐘數採左端觀測值時間加權，不用資料列數推估時間。
- `idle_ratio`、`high_rpm_ratio` 以對應訊號的有效觀測時間為分母。
- 快速加速與減速分別使用 Excel 事件 6、7，並以事件開始時間去重。
- 油耗與時間偏差以同車同路徑群組的中位數為基準；正值表示高於中位數。
- `complete_pair` 表示群組內每兩趟都有直接通過路線驗證；`chain_connected` 表示部分趟次可能只透過其他趟次間接相連。

分析是關聯比較，不能單獨證明某項駕駛行為造成油耗差異。載重、交通、天氣與駕駛人仍可能影響結果。
