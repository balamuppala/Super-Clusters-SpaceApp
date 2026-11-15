"""National context from the burning-activity country archive (NASA FIRMS country files, 2000-2025).

The archive (``data/external/burning_activity_calendar``) is a country-level
aggregate of ~196 million FIRMS detections: annual and monthly counts per
sensor and satellite, seasonal calendars, screened anomaly candidates and
early/recent trend comparisons. It has no coordinates, so it can't feed the
1-km harmonization. It provides the national, multi-decade picture around an
area's own record:

* national trend: the archive's comparisons are descriptive only, so this
  module adds a Mann-Kendall test with Sen's slope on MODIS annual counts
  (real year gaps). It also checks robustness: the direction must agree for
  Aqua alone, Terra alone and the high-confidence subset.
* national seasonal calendar (monthly share, seasonal index, typical range)
* historical anomaly candidates and their 2025 VIIRS cross-checks
* caveats carried over from the archive: missing years are missing (never
  zero), there is no clear-sky denominator, and 2025 MODIS counts need an
  orbital-drift review

The archive's own text is treated as data; nothing in it is executed.
"""

from __future__ import annotations

import csv
import statistics
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any

from fireverify.calendar.trends import _direction, mann_kendall, seasonal_kendall

ARCHIVE_NAME = "burning_activity_calendar"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MIN_TREND_YEARS = 6

# Rough bounding boxes (west, south, east, north) used only to *guess* an area's country from its centre;
# the smallest box containing the centre wins and the user can always pick another country.
COUNTRY_BOXES: dict[str, tuple[float, float, float, float]] = {
    "United States": (-125.0, 24.5, -66.9, 49.4), "Canada": (-141.0, 41.7, -52.6, 83.1),
    "Mexico": (-117.1, 14.5, -86.7, 32.7), "Spain": (-9.4, 36.0, 3.3, 43.8), "Portugal": (-9.6, 36.9, -6.2, 42.2),
    "France": (-4.8, 42.3, 8.2, 51.1), "Italy": (6.6, 36.6, 18.5, 47.1), "Greece": (19.4, 34.8, 28.2, 41.8),
    "Turkey": (26.0, 35.8, 44.8, 42.1), "Russian Federation": (27.0, 41.2, 180.0, 77.7),
    "Kazakhstan": (46.5, 40.6, 87.3, 55.4), "Ukraine": (22.1, 44.4, 40.2, 52.4), "China": (73.5, 18.2, 134.8, 53.6),
    "India": (68.1, 6.7, 97.4, 35.5), "Thailand": (97.3, 5.6, 105.6, 20.5), "Myanmar": (92.2, 9.8, 101.2, 28.5),
    "Lao PDR": (100.1, 13.9, 107.7, 22.5), "Cambodia": (102.3, 10.4, 107.6, 14.7), "Vietnam": (102.1, 8.6, 109.5, 23.4),
    "Indonesia": (95.0, -11.0, 141.0, 6.1), "Australia": (113.3, -43.6, 153.6, -10.7),
    "Brazil": (-73.99, -33.8, -34.8, 5.3), "Bolivia": (-69.6, -22.9, -57.5, -9.7), "Paraguay": (-62.6, -27.6, -54.3, -19.3),
    "Argentina": (-73.6, -55.1, -53.6, -21.8), "Chile": (-75.6, -55.9, -66.9, -17.5), "Colombia": (-79.0, -4.2, -66.9, 12.5),
    "Venezuela": (-73.4, 0.6, -59.8, 12.2), "Angola": (11.7, -18.0, 24.1, -4.4), "Zambia": (21.99, -18.08, 33.7, -8.2),
    "Democratic Republic of the Congo": (12.2, -13.5, 31.3, 5.4), "Mozambique": (30.2, -26.9, 40.8, -10.5),
    "Zimbabwe": (25.2, -22.4, 33.1, -15.6), "Botswana": (20.0, -26.9, 29.4, -17.8), "Namibia": (11.7, -29.0, 25.3, -16.9),
    "South Africa": (16.5, -34.8, 32.9, -22.1), "Malawi": (32.7, -17.1, 35.9, -9.4), "Tanzania": (29.3, -11.7, 40.4, -1.0),
    "Madagascar": (43.2, -25.6, 50.5, -12.0), "Nigeria": (2.7, 4.3, 14.7, 13.9), "Ghana": (-3.3, 4.7, 1.2, 11.2),
    "Burkina Faso": (-5.5, 9.4, 2.4, 15.1), "Mali": (-12.2, 10.1, 4.3, 25.0), "Cameroon": (8.5, 1.7, 16.2, 13.1),
    "Central African Republic": (14.4, 2.2, 27.5, 11.0), "Chad": (13.5, 7.4, 24.0, 23.5), "Sudan": (21.8, 8.7, 38.6, 22.2),
    "South Sudan": (24.1, 3.5, 35.9, 12.2), "Ethiopia": (33.0, 3.4, 48.0, 14.9), "Uganda": (29.6, -1.5, 35.0, 4.2),
    "Kenya": (33.9, -4.7, 41.9, 5.0), "Senegal": (-17.5, 12.3, -11.4, 16.7), "Guinea": (-15.1, 7.2, -7.6, 12.7),
}


def _f(v: str) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _sen_xy(xs: list[float], ys: list[float]) -> float:
    s = [(ys[j] - ys[i]) / (xs[j] - xs[i]) for i in range(len(xs)) for j in range(i + 1, len(xs)) if xs[j] != xs[i]]
    return statistics.median(s) if s else 0.0


def _trend(points: list[tuple[int, float]]) -> dict[str, Any] | None:
    if len(points) < MIN_TREND_YEARS:
        return None
    xs, ys = [float(x) for x, _ in points], [v for _, v in points]
    mk = mann_kendall(ys)
    slope = _sen_xy(xs, ys)
    mean = statistics.mean(ys) or 1.0
    intercept = statistics.median(v - slope * (x - xs[0]) for x, v in zip(xs, ys))
    return {"years": [x for x, _ in points], "p_value": mk["p_value"], "tau": mk["tau"],
            "direction": _direction(mk["p_value"], slope), "slope_per_year": round(slope, 1),
            "pct_per_year": round(100 * slope / mean, 1),
            "line": [round(intercept, 1), round(intercept + slope * (xs[-1] - xs[0]), 1)]}


def _sign(t: dict[str, Any] | None) -> int:
    if not t or t["slope_per_year"] == 0:
        return 0
    return 1 if t["slope_per_year"] > 0 else -1


class CountryArchive:
    """Loads the archive's CSVs once (about 6 MB of the tables used) and answers per-country questions."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._loaded = False

    @property
    def available(self) -> bool:
        return (self.path / "annual_country_counts.csv").is_file()

    def _rows(self, name: str) -> list[dict[str, str]]:
        f = self.path / name
        if not f.is_file():
            return []
        with f.open(newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def _load(self) -> None:
        with self._lock:
            if self._loaded:
                return
            self.annual: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
            for r in self._rows("annual_country_counts.csv"):
                self.annual[r["country"]][r["sensor"]].append({
                    "year": int(r["year"]), "retained": int(_f(r["retained"])),
                    "high_confidence": int(_f(r["high_confidence"])), "night": int(_f(r["night_retained"])),
                    "frp_sum_mw": _f(r["frp_sum_samples_mw"])})
            for c in self.annual.values():
                for lst in c.values():
                    lst.sort(key=lambda r: r["year"])
            # monthly MODIS per satellite -> per-satellite annual totals and national monthly totals
            self.sat_year: dict[str, dict[str, dict[int, int]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
            self.month_counts: dict[str, dict[tuple[int, int], int]] = defaultdict(lambda: defaultdict(int))
            for r in self._rows("monthly_counts_by_satellite.csv"):
                if r["sensor"] != "modis":
                    continue
                y, m, n = int(r["year"]), int(r["month"]), int(_f(r["retained"]))
                self.sat_year[r["country"]][r["satellite"]][y] += n
                self.month_counts[r["country"]][(y, m)] += n
            self.season: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
            for r in self._rows("seasonal_calendar.csv"):
                if r["baseline"] != "descriptive_all_full_years":
                    continue
                self.season[r["country"]][r["sensor"]].append({
                    "month": int(r["month"]), "mean": round(_f(r["mean_detections"]), 1),
                    "median": round(_f(r["median_detections"]), 1), "p10": round(_f(r["p10"]), 1),
                    "p90": round(_f(r["p90"]), 1), "share": round(_f(r["mean_annual_share"]), 4),
                    "seasonal_index": round(_f(r["seasonal_index"]), 2), "band": r["activity_band"],
                    "years": int(_f(r["years"])), "first_year": r["first_year"], "last_year": r["last_year"]})
            self.patterns = {(r["country"], r["sensor"]): r for r in self._rows("country_seasonal_patterns.csv")}
            self.comparisons = {(r["country"], r["sensor"]): r for r in self._rows("country_trend_comparisons.csv")}
            self.anomalies: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for r in self._rows("historical_anomaly_candidates.csv"):
                self.anomalies[r["country"]].append({
                    "sensor": r["sensor"], "year": int(r["year"]), "month": int(r["month"]),
                    "month_name": r["month_name"], "detections": int(_f(r["detections"])),
                    "historical_median": _f(r["historical_median"]), "ratio": round(_f(r["ratio_to_median"]), 1),
                    "robust_z": round(_f(r["robust_z_log1p"]), 2),
                    "high_confidence_ratio": round(_f(r["high_confidence_ratio"]), 1),
                    "prior_years": int(_f(r["prior_same_month_years"])),
                    "drift_review": r.get("modis_orbital_drift_review") == "True"})
            self.crosschecks: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for r in self._rows("sensor_crosschecks_2025.csv"):
                self.crosschecks[r["country"]].append({
                    "month": int(r["month"]), "month_name": r["month_name"], "sensor": r["sensor"],
                    "detections": int(_f(r["detections"])), "prior_years": int(_f(r["n_prior_years"])),
                    "ratio": round(_f(r["ratio_to_prior_median"]), 1)})
            self._loaded = True

    # ------------------------------------------------------------------ API

    def countries(self) -> list[dict[str, Any]]:
        self._load()
        out = []
        for name, sensors in self.annual.items():
            modis = sensors.get("modis", [])
            if modis:
                out.append({"name": name, "years": len(modis),
                            "mean_annual_modis": round(statistics.mean(r["retained"] for r in modis))})
        return sorted(out, key=lambda c: c["name"])

    def guess(self, bbox: list[float]) -> str | None:
        lat, lon = (bbox[1] + bbox[3]) / 2, (bbox[0] + bbox[2]) / 2
        hits = [(abs((e - w) * (n - s)), name) for name, (w, s, e, n) in COUNTRY_BOXES.items()
                if w <= lon <= e and s <= lat <= n]
        if not hits:
            return None
        self._load()
        for _, name in sorted(hits):
            if name in self.annual:
                return name
        return None

    def profile(self, name: str) -> dict[str, Any] | None:
        self._load()
        if name not in self.annual:
            return None
        sensors = self.annual[name]
        modis = [r for r in sensors.get("modis", []) if r["year"] >= 2003]   # both Terra and Aqua flying all year
        main = _trend([(r["year"], r["retained"]) for r in modis])
        hc = _trend([(r["year"], r["high_confidence"]) for r in modis])
        sat = self.sat_year.get(name, {})
        years = {r["year"] for r in modis}
        aqua = _trend(sorted((y, v) for y, v in sat.get("Aqua", {}).items() if y in years))
        terra = _trend(sorted((y, v) for y, v in sat.get("Terra", {}).items() if y in years))
        signs = {s for s in (_sign(main), _sign(hc), _sign(aqua), _sign(terra)) if s != 0}
        comp = self.comparisons.get((name, "modis"))
        if comp:   # the archive's early/recent comparison must point the same way under every subset too
            for key in ("recent_vs_early_pct", "high_confidence_recent_vs_early_pct",
                        "Aqua_recent_vs_early_pct", "Terra_recent_vs_early_pct"):
                v = _f(comp.get(key, "0"))
                if abs(v) >= 1:
                    signs.add(1 if v > 0 else -1)
        robust = bool(main) and main["direction"] != "no significant trend" and len(signs) == 1
        sk_in: dict[int, list[tuple[int, float]]] = defaultdict(list)
        for (y, m), n in self.month_counts.get(name, {}).items():
            if y in years:
                sk_in[m].append((y, float(n)))
        sk = seasonal_kendall(sk_in) if sum(len(v) for v in sk_in.values()) >= 36 else None

        season = sorted(self.season.get(name, {}).get("modis", []), key=lambda r: r["month"])
        pat = self.patterns.get((name, "modis"))
        viirs = {p: [{"year": r["year"], "retained": r["retained"]} for r in rows]
                 for p, rows in sensors.items() if p.startswith("viirs")}
        return {
            "status": "ok", "country": name, "source": ARCHIVE_NAME,
            "annual_modis": modis, "annual_viirs": viirs,
            "trend": main,
            "robustness": {"high_confidence": hc, "aqua": aqua, "terra": terra, "robust": robust,
                           "agree": len(signs) <= 1},
            "seasonal_kendall": ({"p_value": sk["p_value"], "slope_per_year": round(sk["slope_per_year"], 1),
                                  "direction": _direction(sk["p_value"], sk["slope_per_year"])} if sk else None),
            "season": season,
            "pattern": ({"peak_month": pat["peak_month"], "top3": pat["top3_months"],
                         "top3_share": round(_f(pat["top3_share"]), 3),
                         "peak_agreement": round(_f(pat["peak_month_agreement"]), 2),
                         "high_confidence_peak": pat["high_confidence_peak_month"],
                         "peak_stable": pat["peak_stable_to_confidence_filter"] == "True"} if pat else None),
            "comparison": ({"early_years": comp["early_years"], "recent_years": comp["recent_years"],
                            "early_mean": round(_f(comp["early_mean"])), "recent_mean": round(_f(comp["recent_mean"])),
                            "pct": round(_f(comp["recent_vs_early_pct"]), 1),
                            "high_confidence_pct": round(_f(comp["high_confidence_recent_vs_early_pct"]), 1),
                            "aqua_pct": round(_f(comp["Aqua_recent_vs_early_pct"]), 1),
                            "terra_pct": round(_f(comp["Terra_recent_vs_early_pct"]), 1)} if comp else None),
            "anomalies": sorted(self.anomalies.get(name, []), key=lambda a: (a["year"], a["month"])),
            "crosschecks_2025": self.crosschecks.get(name, []),
            "caveats": [
                "Counts are satellite detections (hot pixels per overpass), not fires or burned area.",
                "Years missing from the archive are missing inputs, never zero activity; trends use the supplied years only.",
                "No clear-sky / valid-observation denominator was supplied, so cloudier years can look quieter.",
                "Terra/Aqua orbital drift changed MODIS sampling from late 2024; treat 2025 MODIS counts with care "
                "and check them against VIIRS.",
            ],
        }


def area_vs_national(profile: dict[str, Any], area_monthly: dict[int, float]) -> dict[str, Any] | None:
    """Compare an area's month-of-year distribution with the national seasonal calendar."""
    season = profile.get("season") or []
    total = sum(area_monthly.values())
    if not season or total <= 0:
        return None
    area_share = {m: area_monthly.get(m, 0.0) / total for m in range(1, 13)}
    nat_share = {r["month"]: r["share"] for r in season}
    area_peak = max(area_share, key=area_share.get)
    nat_peak = max(nat_share, key=nat_share.get)
    overlap = sum(min(area_share[m], nat_share.get(m, 0.0)) for m in range(1, 13))
    shift = (area_peak - nat_peak + 6) % 12 - 6
    return {"area_share": [round(area_share[m], 4) for m in range(1, 13)], "area_peak": MONTHS[area_peak - 1],
            "national_peak": MONTHS[nat_peak - 1], "peak_shift_months": shift, "overlap": round(overlap, 2)}
