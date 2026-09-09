# HINO Python scripts

The scripts are grouped by responsibility. They read the latest workbook revision directly from Google Drive and never modify the source.

## Google Drive source

The default Drive file is `output data_Hotai_20260511.xlsx` (`1F68Xl4TACl7iGvES-0g0HeqDGxoBT639`). Override it with `--drive-file` or the `HINO_DRIVE_FILE` environment variable. The value can be a file ID or sharing URL.

Authentication uses Google Application Default Credentials with read-only Drive access. In production, attach a service account and share the Drive file with that account. For local development, configure ADC for the Google account that can read the file. Do not commit credential JSON files.

At startup, each script reads Drive metadata. `verify_topics.py` compares the Drive revision with `source_cache.json`; it downloads and rebuilds the Parquet cache only when the source changed. Downloads are temporary and removed when the process exits.

## `data_pipeline`

- `profile_hino.py`: scans the source workbook and writes a JSON data profile.
- `verify_topics.py`: creates the Parquet cache, trip metrics, deduplicated events, route candidates, idle-location candidates, and topic-validation summary.
- `verify_route_pairs.py`: calculates bidirectional GPS coverage for candidate route pairs.

```powershell
python backend/scripts/data_pipeline/profile_hino.py `
  --output "data\processed\hino_profile.json"

python backend/scripts/data_pipeline/verify_topics.py `
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
