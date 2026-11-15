"""Trends and anomalies on the harmonized daily fire record.

Order of operations
-------------------
1. Sensor gaps: a family reports nothing while the other family, scaled by
   the cross-sensor ratio, says it should have seen several cells. Outages
   and missing granules look exactly like "no fire" in FIRMS, so this is the
   only way to catch them. Gap days are re-estimated from the working sensor
   before the steps below.
2. Spikes and quiet spells: robust z-score of the variance-stabilized count
   (Anscombe: 2*sqrt(x + 3/8), unit variance for Poisson counts) against the
   median and MAD of the surrounding +/-21 days. The 3 days on each side are
   left out (a guard band), so a multi-day outbreak doesn't inflate its own
   baseline. Fire counts swing a lot within a season, so a day at least 4x
   its baseline also counts as a spike at z >= 2.5. A spike outside the fire
   season becomes an "off-season" anomaly.
3. High intensity: day FRP far above its surroundings on a log scale, when
   the day isn't already a spike.
4. Low agreement: MODIS cells mostly unconfirmed by VIIRS, compared with the
   area's usual agreement rate (possible MODIS false detections or a VIIRS
   artefact).
5. Trend:
   * at least two years of data: seasonal Kendall test on monthly totals
     (Hirsch 1982), so the fire season isn't mistaken for a trend
   * otherwise: Mann-Kendall test on weekly totals plus Sen's slope, with a
     caveat that the seasonal cycle isn't removed
6. Fire season: dates reaching 10 / 50 / 90 % of cumulative activity in each
   365-day window, plus the peak week.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from fireverify.calendar.harmonize import DayStats, fill_estimate

WINDOW = 21            # +/- days for the local baseline
GUARD = 3              # days next to the target left out of its baseline, so a multi-day outbreak can't hide itself
MIN_BASELINE_DAYS = 10
Z_SPIKE = 3.5
Z_SPIKE_RELATIVE = 2.5  # weaker z accepted when the day is also >= SPIKE_RATIO x its baseline
SPIKE_RATIO = 4.0
MIN_RELATIVE_SPIKE_CELLS = 20
Z_QUIET = -3.5
MIN_SPIKE_CELLS = 10
COUNT_SCALE_FLOOR = 1.5  # Anscombe sd floor: cells cluster by fire, so counts are overdispersed vs Poisson (sd 1)
MIN_QUIET_BASELINE = 8.0
GAP_P_SINGLE = 1e-4    # P(0 | expected) for one silent day to count as a gap
GAP_P_RUN = 1e-3       # joint P for a run of >= 2 silent days
MIN_SEASON_CELLS = 50
ANALYSIS_VERSION = "trends-1.0"


def anscombe(x: float) -> float:
    return 2.0 * math.sqrt(max(x, 0.0) + 0.375)


def inv_anscombe(y: float) -> float:
    return max(0.0, (y / 2.0) ** 2 - 0.375)


def normal_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2))


# --------------------------------------------------------------------------
# Trend tests
# --------------------------------------------------------------------------

def _mk_s_var(x: list[float]) -> tuple[float, float]:
    n = len(x)
    s = 0
    for i in range(n - 1):
        for j in range(i + 1, n):
            s += (x[j] > x[i]) - (x[j] < x[i])
    ties: dict[float, int] = defaultdict(int)
    for v in x:
        ties[v] += 1
    var = (n * (n - 1) * (2 * n + 5) - sum(t * (t - 1) * (2 * t + 5) for t in ties.values())) / 18.0
    return float(s), var


def _z(s: float, var: float) -> float:
    if var <= 0:
        return 0.0
    return (s - 1) / math.sqrt(var) if s > 0 else (s + 1) / math.sqrt(var) if s < 0 else 0.0


def mann_kendall(x: list[float]) -> dict[str, Any]:
    n = len(x)
    if n < 4:
        return {"n": n, "s": 0, "z": 0.0, "p_value": 1.0, "tau": 0.0}
    s, var = _mk_s_var(x)
    z = _z(s, var)
    return {"n": n, "s": s, "z": round(z, 3), "p_value": round(2 * normal_sf(abs(z)), 4),
            "tau": round(s / (n * (n - 1) / 2), 3)}


def sens_slope(x: list[float]) -> float:
    slopes = [(x[j] - x[i]) / (j - i) for i in range(len(x) - 1) for j in range(i + 1, len(x))]
    return statistics.median(slopes) if slopes else 0.0


def seasonal_kendall(series: dict[int, list[tuple[int, float]]]) -> dict[str, Any]:
    """series: month -> [(year, value)]. Kendall S and variance summed over months."""
    s_tot, var_tot, slopes, n = 0.0, 0.0, [], 0
    for pts in series.values():
        pts = sorted(pts)
        if len(pts) < 2:
            continue
        vals = [v for _, v in pts]
        s, var = _mk_s_var(vals)
        s_tot += s
        var_tot += var
        n += len(vals)
        slopes += [(pts[j][1] - pts[i][1]) / (pts[j][0] - pts[i][0])
                   for i in range(len(pts) - 1) for j in range(i + 1, len(pts))]
    z = _z(s_tot, var_tot)
    return {"n": n, "s": s_tot, "z": round(z, 3), "p_value": round(2 * normal_sf(abs(z)), 4),
            "slope_per_year": statistics.median(slopes) if slopes else 0.0}


def _direction(p: float, slope: float) -> str:
    if p >= 0.05 or slope == 0:
        return "no significant trend"
    return "increasing" if slope > 0 else "decreasing"


def trend(days: list[DayStats]) -> dict[str, Any]:
    covered = [d for d in days if d.estimate is not None]
    if len(covered) < 28:
        return {"status": "insufficient", "detail": "needs at least 4 weeks of data"}
    span_days = (date.fromisoformat(covered[-1].day) - date.fromisoformat(covered[0].day)).days + 1

    monthly: dict[tuple[int, int], list[float]] = defaultdict(list)
    for d in covered:
        dd = date.fromisoformat(d.day)
        monthly[(dd.year, dd.month)].append(d.estimate)
    full_months = {k: sum(v) * 30.4 / len(v) for k, v in monthly.items() if len(v) >= 20}
    years = {y for y, _ in full_months}
    by_month: dict[int, list[tuple[int, float]]] = defaultdict(list)
    for (y, m), v in full_months.items():
        by_month[m].append((y, v))
    repeated = sum(1 for pts in by_month.values() if len(pts) >= 2)

    if span_days >= 700 and len(years) >= 2 and repeated >= 6:
        sk = seasonal_kendall(by_month)
        mean_month = statistics.mean(full_months.values()) or 1.0
        pct = 100 * sk["slope_per_year"] / mean_month
        return {
            "status": "ok", "method": "Seasonal Kendall test on monthly totals (seasonal cycle removed)",
            "direction": _direction(sk["p_value"], sk["slope_per_year"]), "p_value": sk["p_value"], "z": sk["z"],
            "slope": round(sk["slope_per_year"], 2), "slope_unit": "cells per month, per year",
            "pct_per_year": round(pct, 1), "months_compared": repeated, "caveat": None,
        }

    # Weekly totals (7-day bins from the first covered day, scaled when a bin is partly covered).
    bins: dict[int, list[float]] = defaultdict(list)
    first = date.fromisoformat(covered[0].day)
    for d in covered:
        bins[(date.fromisoformat(d.day) - first).days // 7].append(d.estimate)
    weekly = [sum(v) * 7 / len(v) for _, v in sorted(bins.items()) if len(v) >= 4]
    mk = mann_kendall(weekly)
    slope = sens_slope(weekly)
    mean_week = statistics.mean(weekly) if weekly else 0.0
    pct_month = 100 * slope * (30.4 / 7) / mean_week if mean_week else 0.0
    n = len(weekly)
    intercept = statistics.median(v - slope * i for i, v in enumerate(weekly)) if n else 0.0
    line = [round(intercept, 2), round(intercept + slope * (n - 1), 2)] if n else None
    return {
        "status": "ok", "method": "Mann-Kendall test with Sen's slope on weekly totals",
        "direction": _direction(mk["p_value"], slope), "p_value": mk["p_value"], "z": mk["z"], "tau": mk["tau"],
        "slope": round(slope, 2), "slope_unit": "cells per week, per week",
        "pct_per_month": round(pct_month, 1), "weeks": n, "weekly_line": line,
        "caveat": "Less than two years of data: the seasonal cycle isn't removed, so a rising or falling "
                  "fire season can look like a trend. Fetch 24 months for a seasonally adjusted test.",
    }


def recent_change(days: list[DayStats], window: int = 30) -> dict[str, Any] | None:
    covered = [d for d in days if d.estimate is not None]
    if len(covered) < 2 * window:
        return None
    last = covered[-window:]
    prev = covered[-2 * window:-window]
    a, b = sum(d.estimate for d in last), sum(d.estimate for d in prev)
    out = {"window_days": window, "last": round(a, 1), "previous": round(b, 1),
           "ratio": round(a / b, 2) if b else None}
    by_day = {d.day: d for d in days}
    ly = []
    for d in last:
        dd = date.fromisoformat(d.day)
        try:
            k = dd.replace(year=dd.year - 1).isoformat()
        except ValueError:
            k = dd.replace(year=dd.year - 1, day=28).isoformat()
        if k in by_day and by_day[k].estimate is not None:
            ly.append(by_day[k].estimate)
    if len(ly) >= window * 0.8:
        c = sum(ly) * window / len(ly)
        out["last_year"] = round(c, 1)
        out["ratio_last_year"] = round(a / c, 2) if c else None
    return out


# --------------------------------------------------------------------------
# Fire season
# --------------------------------------------------------------------------

def fire_seasons(days: list[DayStats]) -> list[dict[str, Any]]:
    """10/50/90 % cumulative dates and the peak week, per 365-day window anchored at the range end."""
    out = []
    end = len(days)
    while end > 0:
        window = days[max(0, end - 365):end]
        end -= 365
        covered = [d for d in window if d.estimate is not None]
        if len(covered) < 300:
            continue
        total = sum(d.estimate for d in covered)
        if total < MIN_SEASON_CELLS:
            continue
        marks, cum = {}, 0.0
        for d in covered:
            cum += d.estimate
            for q in (0.1, 0.5, 0.9):
                if q not in marks and cum >= q * total:
                    marks[q] = d.day
        vals = [d.estimate or 0.0 for d in window]
        best, best_i = -1.0, 0
        for i in range(len(vals)):
            s = sum(vals[max(0, i - 3):i + 4])
            if s > best:
                best, best_i = s, i
        out.append({"from": window[0].day, "to": window[-1].day, "start": marks[0.1], "middle": marks[0.5],
                    "end": marks[0.9], "peak_week_center": window[best_i].day,
                    "peak_week_cells": round(best, 1), "total_cells": round(total, 1)})
    return sorted(out, key=lambda s: s["from"])


def _in_season(day: str, seasons: list[dict[str, Any]]) -> bool | None:
    for s in seasons:
        if s["from"] <= day <= s["to"]:
            return s["start"] <= day <= s["end"]
    return None


# --------------------------------------------------------------------------
# Anomalies
# --------------------------------------------------------------------------

def _baseline(values: list[float | None], i: int, window: int = WINDOW, guard: int = GUARD) -> list[float] | None:
    lo, hi = max(0, i - window), min(len(values), i + window + 1)
    base = [v for j, v in enumerate(values[lo:hi], start=lo) if abs(j - i) > guard and v is not None]
    return base if len(base) >= MIN_BASELINE_DAYS else None


def _robust_z(y: float, base: list[float], floor: float) -> tuple[float, float]:
    med = statistics.median(base)
    mad = statistics.median(abs(b - med) for b in base)
    return (y - med) / max(1.4826 * mad, floor), med


def _sensor_gaps(days: list[DayStats], cal: dict[str, Any]) -> list[dict[str, Any]]:
    """Runs of days where one family is silent although the other implies it should see fires."""
    v_per_m = cal.get("viirs_cells_per_modis_cell")
    if not v_per_m:
        return []
    found = []
    for silent, other, ratio in (("MODIS", "VIIRS", 1.0 / v_per_m), ("VIIRS", "MODIS", v_per_m)):
        run: list[tuple[DayStats, float]] = []

        def close() -> None:
            if not run:
                return
            joint = math.prod(math.exp(-e) for _, e in run)
            if (len(run) >= 2 and joint < GAP_P_RUN) or (len(run) == 1 and joint < GAP_P_SINGLE):
                found.append({
                    "type": "sensor_gap", "sensor": silent, "start": run[0][0].day, "end": run[-1][0].day,
                    "days": len(run), "expected_cells": round(sum(e for _, e in run), 1),
                    "observed_cells": 0, "p_value": float(f"{joint:.2e}"),
                    "other_cells": sum((d.viirs_cells if other == "VIIRS" else d.modis_cells) for d, _ in run),
                })
            run.clear()

        for d in days:
            silent_cells = d.modis_cells if silent == "MODIS" else d.viirs_cells
            other_cells = d.viirs_cells if silent == "MODIS" else d.modis_cells
            expected = other_cells * ratio
            if d.complete and silent_cells == 0 and other_cells > 0 and expected >= 1.0:
                run.append((d, expected))
            elif d.complete and silent_cells == 0 and other_cells == 0:
                continue   # nothing burning: neither confirms nor breaks a gap
            else:
                close()
        close()
    return found


def _merge(flags: list[dict[str, Any]], days: list[DayStats]) -> list[dict[str, Any]]:
    """Merge consecutive daily flags of the same type into one anomaly."""
    flags.sort(key=lambda f: (f["type"], f["day"]))
    out: list[dict[str, Any]] = []
    for f in flags:
        prev = out[-1] if out else None
        if prev and prev["type"] == f["type"] and \
                (date.fromisoformat(f["day"]) - date.fromisoformat(prev["end"])).days <= 2:
            prev["days"] += (date.fromisoformat(f["day"]) - date.fromisoformat(prev["end"])).days
            prev["end"] = f["day"]
            if abs(f["z"]) > abs(prev["z"]):
                prev.update(z=f["z"], peak_day=f["day"], value=f["value"], baseline=f["baseline"])
            continue
        out.append({"type": f["type"], "start": f["day"], "end": f["day"], "days": 1, "peak_day": f["day"],
                    "z": f["z"], "value": f["value"], "baseline": f["baseline"], "metric": f["metric"]})
    return out


def anomalies(days: list[DayStats], cal: dict[str, Any], seasons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found = _sensor_gaps(days, cal)
    # Re-estimate gap days from the working sensor so the gap isn't read as a lull.
    by_day = {d.day: d for d in days}
    for gap in found:
        d = date.fromisoformat(gap["start"])
        while d.isoformat() <= gap["end"]:
            st = by_day[d.isoformat()]
            fill_estimate(st, cal, only="VIIRS" if gap["sensor"] == "MODIS" else "MODIS")
            st.estimated = f"{gap['sensor']} data gap: " + (st.estimated or "")
            d += timedelta(days=1)

    est = [d.estimate for d in days]
    y = [anscombe(v) if v is not None else None for v in est]
    flags: list[dict[str, Any]] = []
    for i, d in enumerate(days):
        if est[i] is None:
            continue
        base = _baseline(y, i)
        if base is None:
            continue
        z, med = _robust_z(y[i], base, COUNT_SCALE_FLOOR)
        base_cells = inv_anscombe(med)
        strong = z >= Z_SPIKE and est[i] >= MIN_SPIKE_CELLS and est[i] >= 2 * base_cells
        relative = z >= Z_SPIKE_RELATIVE and est[i] >= MIN_RELATIVE_SPIKE_CELLS and est[i] >= SPIKE_RATIO * base_cells
        if strong or relative:
            off = _in_season(d.day, seasons) is False
            flags.append({"type": "off_season" if off else "spike", "day": d.day, "z": round(z, 2),
                          "value": round(est[i], 1), "baseline": round(base_cells, 1), "metric": "harmonized cells"})
        elif z <= Z_QUIET and base_cells >= MIN_QUIET_BASELINE and est[i] <= 0.35 * base_cells:
            flags.append({"type": "quiet", "day": d.day, "z": round(z, 2), "value": round(est[i], 1),
                          "baseline": round(base_cells, 1), "metric": "harmonized cells"})

    spike_days = {f["day"] for f in flags if f["type"] in ("spike", "off_season")}
    lfrp = [math.log1p(d.frp_mw) if d.has_data else None for d in days]
    for i, d in enumerate(days):
        if lfrp[i] is None or d.day in spike_days or d.frp_mw < 500:
            continue
        base = _baseline(lfrp, i)
        if base is None:
            continue
        z, med = _robust_z(lfrp[i], base, 0.35)
        if z >= Z_SPIKE + 0.5:
            flags.append({"type": "high_intensity", "day": d.day, "z": round(z, 2), "value": round(d.frp_mw, 1),
                          "baseline": round(math.expm1(med), 1), "metric": "peak overpass FRP (MW)"})

    usual = cal.get("modis_confirmed_by_viirs")
    if usual:
        for d in days:
            if d.complete and d.modis_cells >= 15 and d.viirs_cells > 0:
                rate = d.modis_confirmed / d.modis_cells
                if rate < usual / 3:
                    flags.append({"type": "low_agreement", "day": d.day, "z": round(rate - usual, 2),
                                  "value": round(rate, 2), "baseline": usual,
                                  "metric": "share of MODIS cells confirmed by VIIRS"})

    merged = _merge(flags, days) + [{**g, "peak_day": g["start"], "metric": f"{g['sensor']} cells"} for g in found]
    for a in merged:
        a["severity"] = _severity(a)
        a["summary"] = describe(a)
        if a["type"] != "sensor_gap" and any(by_day[k].estimated for k in _days_between(a["start"], a["end"])):
            a["estimated"] = True
            a["summary"] += " Part of this period is estimated from one sensor, so treat the size with care."
    return sorted(merged, key=lambda a: a["start"])


def _days_between(start: str, end: str) -> list[str]:
    d, last, out = date.fromisoformat(start), date.fromisoformat(end), []
    while d <= last:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _severity(a: dict[str, Any]) -> str:
    if a["type"] == "sensor_gap":
        return "high" if a["days"] >= 3 else "medium"
    if a["type"] == "low_agreement":
        return "medium"
    big = a.get("baseline") is not None and a["value"] >= max(30, 6 * a["baseline"])
    return "high" if abs(a["z"]) >= 6 or a["days"] >= 3 or big else "medium"


def _span(a: dict[str, Any]) -> str:
    return a["start"] if a["start"] == a["end"] else f"{a['start']} to {a['end']} ({a['days']} days)"


def describe(a: dict[str, Any]) -> str:
    t = a["type"]
    if t == "sensor_gap":
        other = "VIIRS" if a["sensor"] == "MODIS" else "MODIS"
        return (f"{a['sensor']} reported no fires on {_span(a)} while {other} saw {a['other_cells']} cells "
                f"(about {a['expected_cells']:g} expected from {a['sensor']}). This is likely missing "
                f"{a['sensor']} data, not a lull; those days are estimated from {other}.")
    if t == "spike":
        return (f"Fire activity spike on {_span(a)}: {a['value']:g} harmonized cells at peak ({a['peak_day']}), "
                f"against about {a['baseline']:g} on surrounding days.")
    if t == "off_season":
        return (f"Fires outside the usual season on {_span(a)}: {a['value']:g} cells at peak, against about "
                f"{a['baseline']:g} on surrounding days. Check for land clearing, escaped burns or false detections.")
    if t == "quiet":
        return (f"Unusually quiet on {_span(a)}: {a['value']:g} cells against about {a['baseline']:g} normally. "
                f"Usually rain or persistent cloud, sometimes a gap in both data feeds.")
    if t == "high_intensity":
        return (f"Unusually intense fires on {_span(a)}: peak overpass FRP {a['value']:,.0f} MW against about "
                f"{a['baseline']:,.0f} MW nearby.")
    if t == "low_agreement":
        return (f"Sensors disagree on {_span(a)}: only {a['value']:.0%} of MODIS cells had VIIRS support, "
                f"against {a['baseline']:.0%} usually. Possible MODIS false detections or a VIIRS artefact.")
    return t


# --------------------------------------------------------------------------

COMPATIBLE = {"spike": {"spike", "high_intensity"}, "off_season": {"off_season", "spike"},
              "modis_gap": {"sensor_gap"}, "quiet": {"quiet"}}


def score_injected(injected: list[dict[str, Any]], found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Simulated lane: which injected anomalies were detected (overlap within one day, compatible type)."""
    out = []
    for inj in injected:
        lo = (date.fromisoformat(inj["start"]) - timedelta(days=1)).isoformat()
        hi = (date.fromisoformat(inj["end"]) + timedelta(days=1)).isoformat()
        hit = next((a for a in found if a["type"] in COMPATIBLE.get(inj["type"], {inj["type"]})
                    and a["start"] <= hi and a["end"] >= lo
                    and (inj["type"] != "modis_gap" or a.get("sensor") == "MODIS")), None)
        out.append({**inj, "detected": hit is not None, "detected_as": hit["type"] if hit else None})
    return out


def analyze(days: list[DayStats], cal: dict[str, Any]) -> dict[str, Any]:
    seasons = fire_seasons(days)
    found = anomalies(days, cal, seasons)       # may re-estimate gap days, so it runs before the trend
    if found and any(a["type"] == "sensor_gap" for a in found):
        seasons = fire_seasons(days)
    covered = [d for d in days if d.estimate is not None]
    total = sum(d.estimate for d in covered)
    return {
        "version": ANALYSIS_VERSION,
        "totals": {
            "days": len(days), "days_with_data": len(covered),
            "days_complete": sum(1 for d in days if d.complete),
            "active_days": sum(1 for d in covered if d.estimate >= 1),
            "harmonized_cells": round(total, 1),
            "modis_cells": sum(d.modis_cells for d in days), "viirs_cells": sum(d.viirs_cells for d in days),
            "modis_pixels": sum(d.modis_pixels for d in days), "viirs_pixels": sum(d.viirs_pixels for d in days),
            "peak_day": max(covered, key=lambda d: d.estimate).day if covered and total else None,
            "peak_frp_day": max(covered, key=lambda d: d.frp_mw).day if covered and total else None,
        },
        "trend": trend(days),
        "recent": recent_change(days),
        "seasons": seasons,
        "anomalies": found,
    }
