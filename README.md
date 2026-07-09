# Georgia USGS Streamflow Data & Explorer

**Evaristo Critical Zone Hydrology Lab · University of Georgia**

Complete daily discharge (streamflow) record for every USGS gaging station in
Georgia, plus an interactive offline viewer. To publish the viewer on the web
with automatic daily updates from USGS, see **[PUBLISHING.md](PUBLISHING.md)**.

**Retrieved:** 2026-07-09 from the USGS National Water Information System (NWIS)
daily-values web service (`waterservices.usgs.gov/nwis/dv`), parameter code
**00060** (discharge, cubic feet per second).

## Contents

| Path | What it is |
|---|---|
| `ga_streamflow_explorer.html` | **Interactive viewer** — open directly in any browser (double-click; no server needed). Keep the `data/` folder next to it. |
| `data/csv/USGS_<site>.csv` | Daily values per station, long format: `site_no, date, stat_cd, discharge_cfs, qualifiers` (qualifiers: `A`=approved, `P`=provisional, `e`=estimated). 499 files, ~5.42 M rows. |
| `data/sites_metadata.csv` | One row per station: name, coordinates, county, HUC, drainage area, altitude, period of record, record counts, stat codes, provisional/estimated counts. |
| `data/raw/ga_sites_expanded.rdb` | Original USGS site file (expanded metadata, tab-delimited RDB). |
| `data/raw/ga_series_catalog.rdb` | Original USGS series catalog (period of record per series). |
| `data/raw/json/<site>.json` | Raw, unmodified web-service responses (provenance). |
| `data/sites_index.js`, `data/sites/` | Compact data files used by the viewer. |
| `data/download_log.csv` | Per-site download status (all 499 `ok`). |
| `download_ga_discharge.py` | Script that downloaded the data (re-run to refresh). |
| `build_viewer_data.py` | Script that rebuilds the viewer data files from the CSVs. |

## Dataset summary

- **499** gaging stations (streams, tidal streams, 2 lake outlets), statewide
- **~5.42 million** daily discharge values, **1883-10-01 → 2026-07-09**
- Statistic codes: daily **mean** (00003) at 484 stations; tidal-filtered and
  max/min statistics (00001/00002/00021–00024) at 15 coastal Savannah-area
  stations where a daily mean is not published
- Tidal stations include negative discharges (flow reversal on flood tide)

## Viewer features

- Search box + sortable station list (number, name, record length, drainage
  area, recency) and a clickable Georgia map
- Full-period hydrograph with drag-to-zoom overview strip, range presets,
  linear/log scale, crosshair readout (mouse or arrow keys)
- Summary tiles, seasonal (monthly) flow regime, flow duration curve, and
  annual water-year means — all recomputed for the selected date range
- Data tables (annual / monthly / flow percentiles) and light/dark themes
- Deep links: `ga_streamflow_explorer.html#site=02336000`

## Refreshing the data

```
python download_ga_discharge.py   # refreshes the site catalog + re-downloads all daily values through today
python build_viewer_data.py       # rebuilds the viewer data files
```

The included GitHub Actions workflow (`.github/workflows/update-data.yml`) runs
these automatically every day — see [PUBLISHING.md](PUBLISHING.md).

**Note:** recent values are provisional and subject to revision by USGS.
Cite as: U.S. Geological Survey, National Water Information System (NWISWeb),
accessed 2026-07-09.
