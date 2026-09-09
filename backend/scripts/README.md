# HINO Python scripts

The scripts are grouped by responsibility. The original workbook stays on the data owner's local disk and is never modified.

## Collaboration data flow

One data owner runs the pipeline against the local XLSX workbook. Generated, reviewable outputs are written under `data/processed` and can be committed to the private GitHub repository for the model, backend, and frontend teams. Rebuildable caches belong under `data/cache` and should not be committed.

## `data_pipeline`

- `profile_hino.py`: scans the local workbook and writes a JSON data profile.
- `verify_topics.py`: creates the Parquet cache, trip metrics, deduplicated events, route candidates, idle-location candidates, and topic-validation summary.
- `verify_route_pairs.py`: calculates bidirectional GPS coverage for candidate route pairs.
- `summarize_route_groups.py`: groups trips connected by validated route overlap and writes repeated-route summaries.

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
```

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
