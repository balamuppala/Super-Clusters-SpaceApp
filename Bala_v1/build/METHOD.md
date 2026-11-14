# Method: harmonized MODIS + VIIRS hotspot calendar, metropolitan France (first pass)

Built 2026-11-14 (Europe/Paris). Data pulled 2026-11-14 12:02–12:05 Europe/Paris. Every raw file is listed in `data/sources.json` with URL, dataset id, pull time and licence.

## Region and period
- **Region:** metropolitan France including Corsica. The polygon is Natural Earth 10 m Admin-0 "France", limited to the parts inside the bbox W −5.5, S 41.2, E 9.8, N 51.2, with a 0.01° (~1 km) buffer so that coastal pixels are kept. Every detection is clipped to this polygon, including the FIRMS country files.
- **MODIS (Terra + Aqua, C6.1, 1 km):** 2000-11-01 → 2026-11-14.
- **VIIRS S-NPP (375 m, VNP14IMG):** 2012-01-20 → 2026-11-02. The VIIRS_SNPP_NRT feed ends on 2026-11-02 (checked with the FIRMS data_availability API on 2026-11-14).

## Sources
| Period | MODIS | VIIRS S-NPP |
|---|---|---|
| through 2025 | FIRMS yearly country CSV `data/country/modis/<y>/modis_<y>_France.csv` | `data/country/viirs-snpp/<y>/viirs-snpp_<y>_France.csv` |
| 2026 SP | area API `MODIS_SP` 2026-01-01..08-31 | area API `VIIRS_SNPP_SP` 2026-01-01..07-31 |
| 2026 NRT | area API `MODIS_NRT` 2026-09-01..11-14 | area API `VIIRS_SNPP_NRT` 2026-08-01..11-02 |

FIRMS has no 2026 country files yet, so all of 2026 comes from the area API in 5-day chunks over the bbox. Spot check: for 2025-07-20..24 the area API and the country archive returned identical row counts inside the polygon (MODIS 7/7, VIIRS 98/98). NOAA-20 and NOAA-21 VIIRS are not used in this pass.

## Filtering
1. Keep **type = 0** (presumed vegetation fire). Drop type 2 (other static land source, e.g. industrial) and type 3 (offshore).
2. The **NRT feeds have no `type` field.** NRT detections are dropped when they fall in a 3×3 block of 0.01° cells (~3 km) around any historical SP detection of type 2 or 3, which acts as a static-source mask. This approximates the type filter but is not identical to it.
3. Drop **low confidence**: MODIS confidence < 30, VIIRS confidence `l`.
4. Row counts: 275,670 raw → 113,383 kept (17,268 outside the polygon, 137,397 non-vegetation type, 2,387 removed by the NRT static mask, 5,235 low confidence). Kept: MODIS 38,360, VIIRS 75,023.

## Monthly counts and gaps
Counts are detections per calendar month per sensor; MODIS combines Terra and Aqua. Coverage comes from **dataset availability, not from detections**. A month that has data but no fires is 0. A month with no data (MODIS before 2000-11, VIIRS before 2012-01-20, months after the last data day) is a **gap**, shown as a hatched cell. Months with only partial coverage (2012-01 VIIRS, 2026-11) are marked with a dot. Nothing is interpolated or gap-filled, and partial months are not scaled up.

## Harmonization (VIIRS → MODIS-equivalent)
The fit uses the overlap of complete months for both sensors, 2012-02 → 2026-10 (n = 177). Monthly MODIS and VIIRS counts correlate with Pearson r = 0.953 and Spearman ρ = 0.917. VIIRS S-NPP detects about 3.6× more hotspots than MODIS (global ratio k = 0.281).

| model | R² in-sample | R² leave-one-year-out | RMSE LOYO (detections/month) | R² log-space LOYO |
|---|---|---|---|---|
| A: global ratio, MODIS = k·VIIRS | 0.904 | 0.903 | 52.5 | 0.776 |
| **B: calendar-month ratio, MODIS = k_m·VIIRS (chosen)** | **0.941** | **0.926** | **45.7** | **0.874** |
| C: log-log OLS, log1p(M) = a + b·log1p(V) | 0.754 | 0.768 | 81.2 | 0.840 |

The k_m factors are ratios of sums over the overlap:

| Jan | Feb | Mar | Apr | May | Jun | Jul | Aug | Sep | Oct | Nov | Dec |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 0.185 | 0.330 | 0.386 | 0.228 | 0.173 | 0.186 | 0.258 | 0.288 | 0.303 | 0.354 | 0.209 | 0.145 |

Model B was chosen because it validates best out of sample. It is also **scale-free** (no intercept), so the same factors can be applied to any user-drawn AOI on the client side. One limit: the factors were fit on France-wide totals, so they may not transfer well to small AOIs or to fire regimes that differ from France as a whole.

**Harmonized series (MODIS-equivalent):**
- 2000-11 → 2012-01: MODIS observed.
- 2012-02 onward: the mean of MODIS observed and k_m·VIIRS.
- When the two sensors cover different parts of a month, as in 2026-11 (MODIS to the 14th, VIIRS to the 2nd), only MODIS is used.

The app also offers the raw MODIS, raw VIIRS and VIIRS→MODIS-equivalent series.

**Current month vs median:** the month-to-date count for days 1..D of the current month, where D is the last covered day, is compared with the same days in every earlier year that has full coverage. The panel reports the median, IQR, anomaly (count − median, also as %), and rank. The calendar's anomaly view uses each calendar month's median over full-coverage years.

## Results (France metro, data as filtered above)
- Top months, MODIS observed: 2026-07 (1,379), 2022-07 (947), 2019-02 (943). VIIRS observed: 2026-07 (5,117), 2022-07 (3,264), 2019-02 (2,668). Harmonized: 2026-07 (1,348.6), 2019-02 (911.9), 2022-07 (893.9).
- Top years, MODIS observed: 2003 (3,499), 2026 to 11-14 (2,750), 2022 (2,570). VIIRS: 2026 (10,057), 2022 (8,110), 2019 (6,766). Harmonized: 2003 (3,499), 2026 (2,764.9), 2022 (2,386.4).

## Known limits
- Hotspot counts are not burned area, and detection depends on overpass times. Terra's orbit has drifted since about 2020–22, and Terra/Aqua are nearing end of life, so the MODIS baseline itself may change in the latest years. The fit does not model this.
- The NRT static mask is only an approximation of the `type` filter, and 2026-08+ (VIIRS) and 2026-09+ (MODIS) are NRT, which will later be replaced by SP data.
- The country-file archive mixes MODIS `version` strings (6.03, 6.2, 61.03). These were used as delivered.
- Partial months are not scaled up. Current-month comparisons use matched day windows instead.
- The k_m factors are national. AOI-level harmonized values (especially the fractional values in small AOIs) are indicative only.
- Map grid cells (0.1°) can extend slightly beyond a drawn AOI edge, but the counts include only detections inside the AOI.
