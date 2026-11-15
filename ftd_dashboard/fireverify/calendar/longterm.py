"""Long-term record (back to November 2000): annual and monthly harmonized fire activity and its trends.

Why the main long-term trend uses MODIS only
--------------------------------------------
VIIRS starts in 2012 and sees about 1.5-3 times more fire cells than MODIS.
The harmonized series is therefore built from MODIS alone before 2012 (scaled
by the area's cross-sensor ratio) and from both sensors afterwards. A trend
fitted across that join could partly be a sensor change. The MODIS-only series
(Terra since 2000, Aqua since 2002) is one consistent instrument, so it
carries the headline trend test; the harmonized series is shown alongside
for magnitude.

Statistics
----------
* annual totals: Mann-Kendall test with Sen's slope (full years only, >= 300 days of data)
* monthly totals: seasonal Kendall test (the season itself is removed)
* fire-season timing per year (10% / 90% cumulative day of year, length):
  Sen's slope in days per decade, with a Mann-Kendall test
* unusual years: robust z-score of log annual totals (median / MAD)
* decade means
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from fireverify.calendar.harmonize import DayStats, calibrate, fill_estimate, harmonize, points_from_rows
from fireverify.calendar.trends import _direction, mann_kendall, seasonal_kendall
from fireverify.models import BBox, Lane

LONGTERM_VERSION = "longterm-1.0"
FULL_YEAR_DAYS = 300
MIN_SHOWN_DAYS = 28   # years/months with less data are edge padding from neighbouring fetches, not a record
MIN_SEASON_CELLS = 20


def _year_days(store, lane: Lane, bbox: BBox, start: date, end: date, include_low: bool) -> list[DayStats]:
    out: list[DayStats] = []
    y = start.year
    while y <= end.year:
        ys, ye = max(start, date(y, 1, 1)), min(end, date(y, 12, 31))
        lo = datetime.combine(ys - timedelta(days=1), time(0), timezone.utc)
        hi = datetime.combine(ye + timedelta(days=2), time(0), timezone.utc)
        pts = points_from_rows(store.hotspot_points(lane, bbox, lo, hi))
        cov = store.coverage_for(lane, bbox, ys, ye)
        out += harmonize(pts, ys, ye, cov, include_low=include_low).days
        y += 1
    return out


def data_range(store, lane: Lane, bbox: BBox) -> tuple[date, date] | None:
    cov = store.coverage_for(lane, bbox, "2000-01-01", "2100-01-01")
    days = sorted(d for fam in cov.values() for d, st in fam.items() if st == "ok")
    if not days:
        return None
    return date.fromisoformat(days[0]), date.fromisoformat(days[-1])


MIN_TREND_YEARS = 5   # Mann-Kendall can reach p < 0.05 from 5 points (perfectly monotonic: p = 0.027)


def sens_slope_xy(xs: list[float], ys: list[float]) -> float:
    """Sen's slope with real x spacing (years may be sampled, e.g. every 5th year)."""
    slopes = [(ys[j] - ys[i]) / (xs[j] - xs[i]) for i in range(len(xs)) for j in range(i + 1, len(xs)) if xs[j] != xs[i]]
    return statistics.median(slopes) if slopes else 0.0


def _trend(values: list[tuple[int, float]]) -> dict[str, Any] | None:
    if len(values) < MIN_TREND_YEARS:
        return None
    xs = [float(x) for x, _ in values]
    ys = [v for _, v in values]
    mk = mann_kendall(ys)
    slope = sens_slope_xy(xs, ys)
    mean = statistics.mean(ys) or 1.0
    intercept = statistics.median(v - slope * (x - xs[0]) for x, v in zip(xs, ys))
    return {"years": len(ys), "first_year": values[0][0], "last_year": values[-1][0],
            "sampled_years": [x for x, _ in values],
            "direction": _direction(mk["p_value"], slope), "p_value": mk["p_value"], "tau": mk["tau"],
            "slope_per_year": round(slope, 2), "pct_per_year": round(100 * slope / mean, 1),
            "line": [round(intercept, 1), round(intercept + slope * (xs[-1] - xs[0]), 1)]}


def _season_of(days: list[DayStats]) -> dict[str, Any] | None:
    vals = [(d, d.estimate or 0.0) for d in days if d.estimate is not None]
    total = sum(v for _, v in vals)
    if total < MIN_SEASON_CELLS:
        return None
    marks, cum = {}, 0.0
    for d, v in vals:
        cum += v
        for q in (0.1, 0.5, 0.9):
            if q not in marks and cum >= q * total:
                marks[q] = date.fromisoformat(d.day)
    seq = [v for _, v in vals]
    best_i = max(range(len(seq)), key=lambda i: sum(seq[max(0, i - 3):i + 4]))
    peak = date.fromisoformat(vals[best_i][0].day)
    doy = lambda d: d.timetuple().tm_yday  # noqa: E731
    return {"start": marks[0.1].isoformat(), "middle": marks[0.5].isoformat(), "end": marks[0.9].isoformat(),
            "peak": peak.isoformat(), "start_doy": doy(marks[0.1]), "end_doy": doy(marks[0.9]),
            "peak_doy": doy(peak), "length_days": (marks[0.9] - marks[0.1]).days + 1}


def _timing_trend(seasons: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    pts = [(s["year"], s[key]) for s in seasons if s.get(key) is not None]
    if len(pts) < MIN_TREND_YEARS:
        return None
    ys = [v for _, v in pts]
    mk = mann_kendall(ys)
    slope = sens_slope_xy([float(x) for x, _ in pts], ys)   # days per year
    return {"years": len(ys), "p_value": mk["p_value"], "days_per_decade": round(slope * 10, 1),
            "significant": mk["p_value"] < 0.05 and slope != 0}


def analyze_longterm(store, lane: Lane, bbox: BBox, include_low: bool = False,
                     today: date | None = None) -> dict[str, Any]:
    rng = data_range(store, lane, bbox)
    if rng is None:
        return {"status": "no_data", "version": LONGTERM_VERSION}
    start, end = rng
    days = _year_days(store, lane, bbox, start, end, include_low)
    cal = calibrate(days)
    for d in days:
        fill_estimate(d, cal)

    by_year: dict[int, list[DayStats]] = defaultdict(list)
    by_month: dict[tuple[int, int], list[DayStats]] = defaultdict(list)
    for d in days:
        dd = date.fromisoformat(d.day)
        by_year[dd.year].append(d)
        by_month[(dd.year, dd.month)].append(d)

    shown_years = {y for y, ds in by_year.items() if sum(1 for d in ds if d.estimate is not None) >= MIN_SHOWN_DAYS}
    months = []
    for (y, m), ds in sorted(by_month.items()):
        if y not in shown_years:
            continue
        covered = [d for d in ds if d.estimate is not None]
        months.append({"year": y, "month": m, "days_with_data": len(covered),
                       "harmonized": round(sum(d.estimate for d in covered), 1),
                       "modis": sum(d.modis_cells for d in ds), "viirs": sum(d.viirs_cells for d in ds),
                       "peak_frp_mw": round(max((d.frp_mw for d in ds), default=0.0), 1),
                       "viirs_available": any(d.coverage.get("VIIRS") == "ok" for d in ds)})

    years, seasons = [], []
    for y, ds in sorted(by_year.items()):
        if y not in shown_years:
            continue
        covered = [d for d in ds if d.estimate is not None]
        modis_days = sum(1 for d in ds if d.coverage.get("MODIS") == "ok")
        viirs_days = sum(1 for d in ds if d.coverage.get("VIIRS") == "ok")
        row = {"year": y, "days_with_data": len(covered), "full": len(covered) >= FULL_YEAR_DAYS,
               "harmonized": round(sum(d.estimate for d in covered), 1),
               "modis": sum(d.modis_cells for d in ds), "viirs": sum(d.viirs_cells for d in ds),
               "modis_days": modis_days, "viirs_days": viirs_days,
               "peak_frp_mw": round(max((d.frp_mw for d in ds), default=0.0), 1),
               "active_days": sum(1 for d in covered if d.estimate >= 1)}
        years.append(row)
        if row["full"]:
            s = _season_of(ds)
            if s:
                seasons.append({"year": y, **s})

    full = [y for y in years if y["full"]]
    modis_trend = _trend([(y["year"], y["modis"]) for y in full if y["modis_days"] >= FULL_YEAR_DAYS])
    harm_trend = _trend([(y["year"], y["harmonized"]) for y in full])
    sk_in: dict[int, list[tuple[int, float]]] = defaultdict(list)
    for m in months:
        if m["days_with_data"] >= 25:
            sk_in[m["month"]].append((m["year"], float(m["modis"])))
    seasonal = seasonal_kendall(sk_in) if sum(len(v) for v in sk_in.values()) >= 36 else None

    unusual = []
    if len(full) >= 6:
        logs = [math.log1p(y["harmonized"]) for y in full]
        med = statistics.median(logs)
        mad = statistics.median(abs(v - med) for v in logs) or 0.1
        for y, v in zip(full, logs):
            z = (v - med) / (1.4826 * mad)
            if abs(z) >= 2.0:
                unusual.append({"year": y["year"], "z": round(z, 2), "harmonized": y["harmonized"],
                                "ratio_to_median": round(math.expm1(v) / max(math.expm1(med), 1e-9), 2),
                                "kind": "extreme" if z > 0 else "quiet"})
    ranked = sorted(full, key=lambda y: y["harmonized"], reverse=True)

    decades: dict[str, list[float]] = defaultdict(list)
    for y in full:
        if y["modis_days"] >= FULL_YEAR_DAYS:
            label = "2001–2010" if y["year"] <= 2010 else "2011–2020" if y["year"] <= 2020 else "2021–now"
            decades[label].append(y["modis"])
    decade_means = [{"decade": k, "years": len(v), "mean_modis_cells": round(statistics.mean(v), 1)}
                    for k, v in sorted(decades.items()) if v]

    by_frp = sorted(full, key=lambda y: y["peak_frp_mw"], reverse=True)
    record_intensity = None
    if len(by_frp) >= 3 and by_frp[1]["peak_frp_mw"] > 0 and by_frp[0]["peak_frp_mw"] >= 2 * by_frp[1]["peak_frp_mw"]:
        record_intensity = {"year": by_frp[0]["year"], "peak_frp_mw": by_frp[0]["peak_frp_mw"],
                            "next_year": by_frp[1]["year"], "next_peak_frp_mw": by_frp[1]["peak_frp_mw"],
                            "ratio": round(by_frp[0]["peak_frp_mw"] / by_frp[1]["peak_frp_mw"], 1)}

    shift = {"start": _timing_trend(seasons, "start_doy"), "end": _timing_trend(seasons, "end_doy"),
             "length": _timing_trend(seasons, "length_days"), "peak": _timing_trend(seasons, "peak_doy")}

    full_years = [y["year"] for y in full]
    gaps = [b - a for a, b in zip(full_years, full_years[1:])]
    return {
        "status": "ok", "version": LONGTERM_VERSION,
        "sampled": bool(gaps) and max(gaps) > 1, "full_years": full_years, "bbox": bbox.as_list(), "lane": lane.value,
        "first_day": start.isoformat(), "last_day": end.isoformat(), "include_low": include_low,
        "calibration": cal, "years": years, "months": months, "seasons": seasons,
        "trend_modis": modis_trend, "trend_harmonized": harm_trend,
        "seasonal_kendall_modis": ({"p_value": seasonal["p_value"], "z": seasonal["z"],
                                    "slope_per_year": round(seasonal["slope_per_year"], 2),
                                    "direction": _direction(seasonal["p_value"], seasonal["slope_per_year"])}
                                   if seasonal else None),
        "season_shift": shift, "unusual_years": unusual, "record_intensity": record_intensity,
        "top_years": [{"year": y["year"], "harmonized": y["harmonized"], "modis": y["modis"]} for y in ranked[:5]],
        "decades": decade_means,
    }
