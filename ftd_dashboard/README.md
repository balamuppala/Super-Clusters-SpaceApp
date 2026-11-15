# ftd.io

*Install once with `python3 -m pip install --user -e .`, then start it from any folder with `ftd dashboard` (the long form `fireverify` / `python3 -m fireverify` also works). Your `.env` and `data/` are always read from this project folder.*

The data-ingestion layer for an app that checks whether a satellite fire hotspot is a real vegetation fire.
It collects thermal detections and the evidence around them, and sorts every record into one of five lanes.

| Lane | Table | What goes there |
|---|---|---|
| **Live** | `live_records` | Accepted real observations: FIRMS NRT and archive, Sentinel-1/2/3, context layers |
| **Simulated** | `simulated_records` | Accepted synthetic observations with ground-truth labels (`sim_truth`) |
| **Derived** | `derived_records` | Evidence features per fire event, tagged with their origin lane |
| **Quarantined** | `quarantine` | Plausible but suspect records (`Q_*`), waiting for review (release or reject) |
| **Rejected** | `rejected` | Unusable records (`R_*`), kept with their raw payload for audit |

Live and simulated records never share a table. The engine raises `LaneViolation` if a source sends a record for the wrong lane.

## Pipeline

```
FIRMS VIIRS (S-NPP/NOAA-20/NOAA-21) + MODIS ─┐
Context: OSM flares/industry/volcanoes,      ├─> parse ─> validate ─> live | simulated
         GeoJSON land cover / urban          ┘                  └──> quarantine | rejected
accepted hotspots ─> spatio-temporal clusters (2 km / 48 h)
   └─> per cluster: Sentinel-2 NDVI/NBR · Sentinel-1 VV/VH · Sentinel-3 SLSTR FRP granules · FIRMS archive
         └─> parse ─> validate ─> lanes
   └─> derived: cluster_summary, cross_sensor_corroboration, vegetation_loss (dNDVI/dNBR),
                sar_backscatter_change, temporal_frequency, context_proximity, evidence_completeness
```

Simulated connectors subclass the live ones and replace only the transport layer. The simulator writes data in each API's real format (FIRMS CSV, Statistical API JSON, OData, GeoJSON), so the real parsers and validators get tested.

## Quick start

```bash
python3 -m fireverify ingest --mode simulated          # offline, no keys
python3 -m fireverify clusters
python3 -m fireverify evidence <cluster_id>
python3 -m fireverify quarantine list
python3 -m fireverify rejected
```

Live mode: copy `.env.example` to `.env` and fill in the keys, then run:

```bash
python3 -m fireverify sources
python3 -m fireverify ingest --mode live --bbox -123,37,-119,40 --days 1
python3 -m fireverify import-archive fire_archive_SV-C2.csv --source VIIRS_SNPP_SP
```

| Source | Needs |
|---|---|
| VIIRS / MODIS NRT, FIRMS historical archive | `FIRMS_MAP_KEY` |
| Sentinel-2 NDVI/NBR, Sentinel-1 backscatter | `CDSE_CLIENT_ID` / `CDSE_CLIENT_SECRET` (Sentinel Hub on CDSE) |
| Sentinel-3 SLSTR FRP granules, OSM context, bundled volcano list | nothing |

Missing credentials mark a source `unavailable`. A failing source is marked `error`. Neither one stops the run.

## Dashboard

```bash
python3 -m fireverify dashboard          # then open http://127.0.0.1:8765
```

It's a local web app: a standard-library server in front of the same SQLite database, with no extra Python packages. The page loads Leaflet from unpkg and keyless Esri basemaps, so it needs internet access for the map.

- **Simulated / Live switch:** each lane is shown on its own; the two never mix.
- **Summary tiles and verdict bar:** events scored, fires, uncertain, false alarms (with the top cause), quarantine backlog.
- **Map** (gray canvas or satellite imagery): one marker per event, coloured on a red (fire) to blue (false alarm) scale and sized by detection count. Click a marker to see its detections and extent.
- **Event panel:** P(fire) and verdict, an evidence chart (how far each piece of evidence pushed the score towards fire or false alarm), before/after NDVI, NBR and radar VH, a detection timeline by satellite (day/night, FRP), and key facts.
- **Tabs:** sortable and filterable event table · quarantine with **Release / Reject** buttons · rejected records · run history · source status.
- **Run ingestion:** start a simulated batch, or a live run (up to 100 deg², about 10° × 10°). For the live area you can pick:
  - the current map view
  - a box you draw on the map
  - one of today's most active fire areas (busiest 2° cells in the last 24 h of VIIRS data; one FIRMS request, cached for 30 min)
  - a fire-prone region preset
  - typed coordinates

  The size is checked as you go, and Start stays disabled until the area is valid. Runs happen in the background with a progress indicator.
- **Fire calendar tab:**
  - month-by-month heatmap (harmonized cells, MODIS, VIIRS or peak FRP)
  - daily chart with 7-day averages per sensor, a trend line and numbered anomaly bands
  - click a day for its sensor breakdown and a map of 1-km cells coloured by which sensor saw them
  - trend, fire-season and harmonization summary
  - anomaly list
  - government action options
- Light and dark mode, keyboard-accessible tables and charts, phone layout.

The server binds to 127.0.0.1. Requests that change data need a custom header and a same-origin `Origin`, so other websites can't trigger them.

## Fire calendar: harmonized MODIS + VIIRS, trends and anomalies

Open the **Fire calendar** tab in the dashboard, or use the CLI:

```bash
python3 -m fireverify calendar backfill --mode live --bbox 25,-16,27,-14 --start 2025-11-15 --end 2026-11-14
python3 -m fireverify calendar show     --mode live --bbox 25,-16,27,-14 --start 2025-11-15 --end 2026-11-14
```

**Backfill** (`fireverify/calendar/backfill.py`) builds a continuous daily record for one area (up to 25 deg²) over up to 24 months:

- For each day it picks the best FIRMS product: standard processing (`*_SP`) where it exists, near real-time (`*_NRT`) for recent weeks. For VIIRS, NOAA-20 is primary and S-NPP fills dates before NOAA-20 data existed. Date ranges come from FIRMS' `data_availability` endpoint.
- It fetches 5-day chunks four at a time, through the normal parse/validate path, stored as `historical_hotspot` records. The calendar data therefore also feeds the temporal-frequency evidence used for verification.
- It records which days each sensor actually covered, so "no fire" and "no data" stay distinct.
- Re-running skips chunks already stored. Only chunks that were still near real-time when fetched are refreshed.
- Low-confidence pixels are kept, flagged rather than quarantined; the calendar excludes them unless you tick the box.

**Harmonization** (`harmonize.py`):

1. Assigns each detection to its local solar day.
2. Snaps both sensors to a common ~1 km grid (0.01°), so several 375 m VIIRS pixels collapse into one cell.
3. Matches MODIS and VIIRS cells, allowing one neighbouring cell for geolocation offsets.
4. Harmonized count = MODIS cells + the VIIRS cells MODIS missed.
5. Daily FRP = the largest single-overpass total; adding up overpasses would count the same fire several times.
6. Cross-sensor ratios (VIIRS cells per MODIS cell, agreement rates) are calibrated per area. They let a day with only one sensor's data be estimated, shown hatched.

**Trends and anomalies** (`trends.py`):

- **Trend:**
  - 24 months or more: seasonal Kendall test on monthly totals, which removes the fire season.
  - Otherwise: Mann-Kendall test with Sen's slope on weekly totals, shown with a caveat that the season isn't removed.
- **Fire season:** 10/50/90% cumulative dates and the peak week; also last-30-days against the previous 30 days and last year.
- **Anomalies:**
  - *sensor data gaps*: one sensor silent while the other implies it should see fires; those days are re-estimated from the working sensor
  - *spikes*, *off-season fires* and *quiet spells*: robust z-scores of variance-stabilized counts against the surrounding ±3 weeks, with a ±3-day guard band
  - *high-intensity* days
  - *sensor disagreement*

**Government action options** (`actions.py`) turn each major finding into concrete options, each with a lead agency type and a timeframe. Findings covered: rising, falling or flat trend; next season's projected dates (campaign, burn restrictions, pre-positioning for the peak, recovery); a recent surge; each anomaly type (for example, field verification for off-season fires, or MODIS-retirement planning for data gaps). Groups are ordered urgent → low and carry a disclaimer: they are decision support, not orders.

**Long-term record since 2000** (`longterm.py`, the dashboard's long-term panel, `GET /api/calendar/longterm`):
- **Data available:** MODIS archive data starts in November 2000, VIIRS in 2012.
- **Periods:**
  - *Since 2000, every 5th year (fast)*: 2001, 2006, 2011, 2016, 2021 and the last full year. MODIS is fetched for all of them, VIIRS for the two most recent (enough to calibrate the sensors). A 4°×4° area takes roughly 15 minutes.
  - *Every year (slow)*: the full record.
- **Quota pacing:** a 5-day FIRMS request costs about 10 of the key's 5,000 transactions per 10 minutes. Long fetches therefore check `mapkey_status` and pause before reaching 85% of the limit; the full record for one area takes about an hour.
- **Headline trend:** Mann-Kendall test with Sen's slope on **MODIS-only** annual totals. It uses real year gaps, so sampled years work. MODIS-only keeps the 2012 arrival of VIIRS from looking like a trend; the harmonized series (MODIS scaled before 2012) is shown for magnitude.
- **Also shown:**
  - seasonal Kendall test on monthly totals
  - fire-season start, end and length trends (days per decade)
  - unusual years and decade means
  - a years × months heatmap and an annual chart
  - long-term government options: season-shift measures (move restrictions and contracts earlier, fund longer seasons) and extreme-year planning

**National context** (`country.py`, the dashboard's national panel, `GET /api/country/profile`): uses the burning-activity country archive (NASA FIRMS country files 2000–2025, ~196 M detections, 238 countries). It's unzipped to `data/external/burning_activity_calendar/`; set `FIREVERIFY_COUNTRY_ARCHIVE` to use another folder. The panel appears only when the archive is present.
- **Country:** guessed from the area's centre, and can be changed.
- **National trend:** Mann-Kendall test with Sen's slope on 12 MODIS years. It counts as *robust* only if the direction also holds for Aqua alone, Terra alone, the high-confidence subset and the archive's early/recent comparison.
- **Seasonal Kendall test** on national monthly counts.
- **National seasonal calendar** (share, seasonal index, p10–p90), overlaid with the area's own monthly pattern.
- **Exceptional months** from the archive, with 2025 VIIRS cross-checks and MODIS orbital-drift flags.
- **National government options:** national strategy (robust trends only), the burning calendar with every high window kept separate, fire-danger triggers when the peak month varies, reviews of exceptional months, and VIIRS-based statistics when the trend isn't robust.
- **Caveats carried over from the archive:** missing years are missing (never zero), there's no clear-sky denominator, and counts are detections rather than fires.

**Simulated lane:** the backfill generates a seeded fire regime with a season, a multi-year trend, a persistent gas flare and three planted anomalies (outbreak spike, off-season burst, 7-day MODIS outage). Rows come out in FIRMS CSV format. The dashboard shows how many planted anomalies were found; tests require all three.

## Verification: naive Bayes fusion

`fireverify/verification/` turns each event's derived features into **P(active vegetation fire)**. "Active vegetation fire" covers wildfires and agricultural or prescribed burns. Gas flares, volcanoes, hot roofs and artefacts count as "not fire".

```
log-odds(fire | E) = log-odds(prior | land cover) + Σ log LR(group state)
```

| Evidence group | States | Why one group |
|---|---|---|
| optical | high / moderate / low / unburned / non_vegetated | dNBR and dNDVI come from the same Sentinel-2 scenes |
| sar | strong_drop / drop / no_change / increase | Sentinel-1 VH change |
| temporal | none / sporadic / seasonal / persistent | Same-season *and* off-season history, so annual crop burning isn't mistaken for a flare |
| heat_source | gas_flare / volcano / industrial / none | Co-located layers counted once (strongest wins) |
| thermal | single_weak / single / repeat / multi_sensor_repeat | Detection count, sensors and FRP |
| spread | stationary / compact / spreading | Fires grow; flares and vents stay put |

Land cover sets the prior instead of being evidence, so it isn't counted twice. Missing or quarantined evidence drops out with LR = 1.
The decision labels are `confirmed_fire ≥0.85`, `likely_fire ≥0.60`, `uncertain`, `likely_false_alarm <0.30` and `false_alarm <0.10`. If fewer than 3 evidence groups were observed, the verdict never gets a firm label. Verdicts that lean towards "not fire" carry a probable cause, such as `gas_flare` or `urban_hot_surface`.

```bash
python3 -m fireverify ingest --mode simulated --verify
python3 -m fireverify explain <cluster_id>          # per-evidence LR breakdown
python3 -m fireverify simulate-batch --runs 40       # 280 labelled events
python3 -m fireverify evaluate
python3 -m fireverify calibrate --holdout 0.3 --out data/likelihoods.json
python3 -m fireverify calibrate --mode live --labels labels.csv   # cluster_id,is_fire
```

**The default likelihood table is expert-elicited, not measured.** Calibrate it on labelled *real* events (burn perimeters such as MTBS, flare catalogues such as VIIRS Nightfire, analyst review). Calibrating on simulated data only teaches the model the simulator, and the CLI warns when you do that. Calibration treats the current table as a Dirichlet prior (`--strength` pseudo-counts), so a small labelled set adjusts the values without overfitting.

## Simulation scenarios

Scenarios: `wildfire`, `agricultural_burn`, `industrial_flare`, `volcano`, `urban_false_alarm`, `low_confidence`, `smoldering_fire` (weak real fire), `unmapped_flare` (flare missing from the context layers), `corrupt`. Each site has a 20% chance that smoke hides its post-fire Sentinel-2 window.
Pick a subset with `--scenarios`. Runs are deterministic for a given `--seed`.
The `corrupt` scenario injects bad records: bad coordinates, non-numeric fields, timestamps in the future or before the window, negative FRP, impossible brightness, contradictory day/night flags, records outside the AOI, duplicates and broken GeoJSON. Each one carries the reason code it should trigger.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

## Known limits

- Sentinel-3 is recorded as granule *coverage* only. Pixel-level FRP needs the NetCDF product download (`Overpass.product_id` is the handle).
- AOIs that cross the antimeridian are not supported.
- Historical frequency samples two windows per past year (same season and six months away). Import FIRMS archive CSVs, or run a calendar backfill over the area, for full coverage.
- The calendar can't tell an outage from "no fire" when *both* sensors go silent; that shows up as a quiet spell. Cloud masks aren't used yet.
- Government action options are generic fire-management practice; they don't know local law, budgets or tenure.
- Naive Bayes still assumes the six groups are independent given the class. Thermal and spread are mildly correlated.
