# HINO Python scripts

The scripts are grouped by responsibility. Source workbooks are inputs and are never modified.

## `data_pipeline`

- `profile_hino.py`: scans the source workbook and writes a JSON data profile.
- `verify_topics.py`: creates the Parquet cache, trip metrics, deduplicated events, route candidates, idle-location candidates, and topic-validation summary.
- `verify_route_pairs.py`: calculates bidirectional GPS coverage for candidate route pairs.

All source and output paths are explicit command-line arguments so the scripts work independently of the repository location.

```powershell
python backend/scripts/data_pipeline/profile_hino.py `
  --source "C:\path\to\output data_Hotai_20260511.xlsx" `
  --output "data\processed\hino_profile.json"

python backend/scripts/data_pipeline/verify_topics.py `
  --source "C:\path\to\output data_Hotai_20260511.xlsx" `
  --output-dir "data\processed\analysis"

python backend/scripts/data_pipeline/verify_route_pairs.py `
  --analysis-dir "data\processed\analysis"
```

## `route_poc`

- `extract_trip.py`: streams the large XLSX and extracts one vehicle journey for the route POC.
- `build_route_poc.py`: obtains truck routes and elevation profiles, estimates fuel and cost, and generates JSON plus an HTML comparison page.

`build_route_poc.py` sends the selected trip endpoints and configured truck dimensions to the selected Valhalla endpoint. Review the endpoint and data-sharing requirements before running it.

```powershell
python backend/scripts/route_poc/extract_trip.py `
  --source "C:\path\to\output data_Hotai_20260511.xlsx" `
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
