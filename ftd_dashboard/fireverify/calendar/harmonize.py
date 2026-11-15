"""Harmonize MODIS and VIIRS active-fire detections into one daily, 1-km record.

Why the raw counts can't be compared directly
---------------------------------------------
* Pixel size: VIIRS I-band pixels are 375 m, MODIS pixels 1 km. One fire is
  often several VIIRS pixels but a single MODIS pixel.
* Sensitivity: VIIRS detects fires roughly ten times smaller, so it reports
  many small fires that MODIS never sees.
* Overpasses: Terra (10:30 / 22:30) plus Aqua (13:30 / 01:30) give MODIS four
  looks a day; S-NPP and NOAA-20 (about 13:30 / 01:30) give VIIRS two.
* Confidence scales differ (MODIS 0-100 vs VIIRS l/n/h). Ingestion already
  normalizes both to low / nominal / high.
* Geolocation: the same fire can land up to about 1 km apart in the two products.

Method
------
1. Day = local solar date (UTC + longitude/15 h), so a 01:30 night pass isn't
   split across two UTC days.
2. Drop low-confidence pixels (optional).
3. Snap every pixel to a common 0.01 deg (~1 km, MODIS-like) grid. VIIRS
   pixels that fall in one cell collapse into that cell.
4. Per day, compute:
   * MODIS cells and VIIRS cells
   * cells both sensors saw, allowing one neighbouring cell for geolocation offset
   * harmonized cells = MODIS cells + VIIRS cells with no MODIS cell next to them
     (no double counting)
5. FRP: sum pixel FRP within each overpass. The day's value is the largest
   single-overpass total; adding up several overpasses would count the same
   fire several times.
6. Cross-calibration over days where both sensors have data: how many VIIRS
   cells per MODIS cell, and harmonized/sensor ratios. When only one sensor
   has data for a day, these ratios give an estimated harmonized count.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable

GRID_DEG = 0.01            # ~1.1 km at the equator: the MODIS nominal footprint
PASS_GAP_MIN = 20          # detections of one platform further apart than this belong to different overpasses
FAMILIES = ("MODIS", "VIIRS")
ALGORITHM_VERSION = "harmonize-1.0"
_NEIGHBOURS = [(dy, dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1)]


@dataclass(frozen=True)
class Point:
    sensor: str          # MODIS | VIIRS
    platform: str        # Terra | Aqua | Suomi-NPP | NOAA-20 | NOAA-21
    t: datetime          # acquisition time, UTC
    lat: float
    lon: float
    frp: float
    confidence: str      # low | nominal | high

    @property
    def day(self) -> date:
        return solar_date(self.t, self.lon)

    @property
    def cell(self) -> tuple[int, int]:
        return cell_of(self.lat, self.lon)


def solar_date(t: datetime, lon: float) -> date:
    return (t + timedelta(hours=lon / 15.0)).date()


def cell_of(lat: float, lon: float) -> tuple[int, int]:
    return math.floor(lat / GRID_DEG), math.floor(lon / GRID_DEG)


def cell_center(cell: tuple[int, int]) -> tuple[float, float]:
    return round((cell[0] + 0.5) * GRID_DEG, 5), round((cell[1] + 0.5) * GRID_DEG, 5)


def points_from_rows(rows: Iterable[tuple]) -> list[Point]:
    """Build points from Store.hotspot_points rows.

    The same detection can be stored twice: once as an NRT hotspot and once as an
    archive or history record. Duplicates are dropped by platform, time and
    position (~100 m).
    """
    seen: set[tuple] = set()
    out = []
    for lat, lon, observed_at, _source, sensor, platform, frp, conf in rows:
        t = observed_at if isinstance(observed_at, datetime) else datetime.fromisoformat(observed_at)
        key = (platform, t.isoformat(timespec="minutes"), round(lat, 3), round(lon, 3))
        if key in seen or sensor not in FAMILIES:
            continue
        seen.add(key)
        out.append(Point(sensor, platform or "unknown", t, float(lat), float(lon), float(frp or 0.0), conf or "nominal"))
    return out


def _has_neighbour(cell: tuple[int, int], cells: set | dict) -> bool:
    y, x = cell
    return any((y + dy, x + dx) in cells for dy, dx in _NEIGHBOURS)


def _overpasses(points: list[Point]) -> list[dict[str, Any]]:
    """Group a day's points into overpasses (per platform, split on time gaps)."""
    by_platform: dict[str, list[Point]] = defaultdict(list)
    for p in points:
        by_platform[p.platform].append(p)
    passes = []
    for platform, pts in by_platform.items():
        pts.sort(key=lambda p: p.t)
        group = [pts[0]]
        for p in pts[1:]:
            if (p.t - group[-1].t).total_seconds() > PASS_GAP_MIN * 60:
                passes.append(group)
                group = []
            group.append(p)
        passes.append(group)
    out = []
    for g in passes:
        out.append({"platform": g[0].platform, "sensor": g[0].sensor, "time": g[0].t.isoformat(),
                    "pixels": len(g), "frp_mw": round(sum(p.frp for p in g), 1)})
    return sorted(out, key=lambda p: p["time"])


@dataclass
class DayStats:
    day: str
    modis_pixels: int = 0
    viirs_pixels: int = 0
    modis_cells: int = 0
    viirs_cells: int = 0
    modis_confirmed: int = 0      # MODIS cells with a VIIRS cell within one neighbour
    viirs_confirmed: int = 0      # VIIRS cells with a MODIS cell within one neighbour
    harmonized_cells: int = 0     # MODIS cells + VIIRS cells not next to any MODIS cell
    frp_mw: float = 0.0           # largest single-overpass FRP total of the day
    modis_frp_mw: float = 0.0
    viirs_frp_mw: float = 0.0
    overpasses: int = 0
    platforms: dict[str, int] = field(default_factory=dict)
    coverage: dict[str, str] = field(default_factory=dict)   # family -> ok | unavailable | error | unknown
    complete: bool = False        # both families have data for the day
    has_data: bool = False        # at least one family has data
    estimate: float | None = None  # harmonized cells, gap-filled from one sensor when needed
    estimated: str | None = None   # why the estimate isn't a plain observation

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _day_stats(day: date, points: list[Point]) -> DayStats:
    st = DayStats(day.isoformat())
    modis: dict[tuple, float] = {}
    viirs: dict[tuple, float] = {}
    for p in points:
        if p.sensor == "MODIS":
            st.modis_pixels += 1
            modis[p.cell] = modis.get(p.cell, 0.0) + p.frp
        else:
            st.viirs_pixels += 1
            viirs[p.cell] = viirs.get(p.cell, 0.0) + p.frp
    st.modis_cells, st.viirs_cells = len(modis), len(viirs)
    st.modis_confirmed = sum(1 for c in modis if _has_neighbour(c, viirs))
    st.viirs_confirmed = sum(1 for c in viirs if _has_neighbour(c, modis))
    st.harmonized_cells = st.modis_cells + (st.viirs_cells - st.viirs_confirmed)
    passes = _overpasses(points) if points else []
    st.overpasses = len(passes)
    st.modis_frp_mw = max((p["frp_mw"] for p in passes if p["sensor"] == "MODIS"), default=0.0)
    st.viirs_frp_mw = max((p["frp_mw"] for p in passes if p["sensor"] == "VIIRS"), default=0.0)
    st.frp_mw = max(st.modis_frp_mw, st.viirs_frp_mw)
    st.platforms = dict(Counter(p.platform for p in points))
    return st


@dataclass
class Harmonized:
    start: str
    end: str
    days: list[DayStats]
    calibration: dict[str, Any]
    include_low: bool

    def as_dict(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "include_low": self.include_low,
                "algorithm_version": ALGORITHM_VERSION, "grid_deg": GRID_DEG,
                "calibration": self.calibration, "days": [d.as_dict() for d in self.days]}


def _ratio(num: float, den: float) -> float | None:
    return round(num / den, 3) if den else None


def calibrate(days: list[DayStats]) -> dict[str, Any]:
    """Cross-sensor ratios from days where both families have data and saw something."""
    both = [d for d in days if d.complete and (d.modis_cells or d.viirs_cells)]
    s = lambda attr: sum(getattr(d, attr) for d in both)  # noqa: E731
    return {
        "days_used": len(both),
        "viirs_cells_per_modis_cell": _ratio(s("viirs_cells"), s("modis_cells")),
        "viirs_pixels_per_cell": _ratio(s("viirs_pixels"), s("viirs_cells")),
        "modis_pixels_per_cell": _ratio(s("modis_pixels"), s("modis_cells")),
        "harmonized_per_modis_cell": _ratio(s("harmonized_cells"), s("modis_cells")),
        "harmonized_per_viirs_cell": _ratio(s("harmonized_cells"), s("viirs_cells")),
        "modis_confirmed_by_viirs": _ratio(s("modis_confirmed"), s("modis_cells")),
        "viirs_confirmed_by_modis": _ratio(s("viirs_confirmed"), s("viirs_cells")),
        "modis_share_of_harmonized": _ratio(s("modis_cells"), s("harmonized_cells")),
    }


def fill_estimate(d: DayStats, cal: dict[str, Any], only: str | None = None) -> None:
    """Set the harmonized estimate, scaling up from a single family when the other has no data.

    ``only`` forces a one-sensor estimate. Use it when the other sensor was
    reachable but turned out to be in an outage.
    """
    fams = [only] if only else [f for f in FAMILIES if d.coverage.get(f) == "ok"]
    if not only and len(fams) == 2:
        d.estimate, d.estimated = float(d.harmonized_cells), None
        return
    if not fams:
        d.estimate, d.estimated = None, None
        return
    fam = fams[0]
    factor = cal.get("harmonized_per_modis_cell" if fam == "MODIS" else "harmonized_per_viirs_cell")
    cells = d.modis_cells if fam == "MODIS" else d.viirs_cells
    if factor:
        d.estimate = round(cells * factor, 2)
        d.estimated = f"{fam} only, scaled by {factor:g}"
    else:
        d.estimate, d.estimated = float(cells), f"{fam} only (no calibration yet)"


def harmonize(points: Iterable[Point], start: date, end: date,
              coverage: dict[str, dict[str, str]] | None = None, include_low: bool = False) -> Harmonized:
    coverage = coverage or {}
    by_day: dict[date, list[Point]] = defaultdict(list)
    for p in points:
        if not include_low and p.confidence == "low":
            continue
        d = p.day
        if start <= d <= end:
            by_day[d].append(p)
    days = []
    d = start
    while d <= end:
        st = _day_stats(d, by_day.get(d, []))
        for fam in FAMILIES:
            status = coverage.get(fam, {}).get(st.day, "unknown")
            seen = st.modis_pixels if fam == "MODIS" else st.viirs_pixels
            st.coverage[fam] = "ok" if seen else status   # detections prove the family had data
        st.complete = all(st.coverage[f] == "ok" for f in FAMILIES)
        st.has_data = any(st.coverage[f] == "ok" for f in FAMILIES)
        days.append(st)
        d += timedelta(days=1)
    cal = calibrate(days)
    for st in days:
        fill_estimate(st, cal)
    return Harmonized(start.isoformat(), end.isoformat(), days, cal, include_low)


def day_detail(points: Iterable[Point], day: date, include_low: bool = False) -> dict[str, Any]:
    """Cell-level view of one day: which sensor saw each 1-km cell, plus overpasses."""
    pts = [p for p in points if p.day == day and (include_low or p.confidence != "low")]
    modis: dict[tuple, list[Point]] = defaultdict(list)
    viirs: dict[tuple, list[Point]] = defaultdict(list)
    for p in pts:
        (modis if p.sensor == "MODIS" else viirs)[p.cell].append(p)
    cells = []
    for c, ps in modis.items():
        lat, lon = cell_center(c)
        near = [p for dy, dx in _NEIGHBOURS for p in viirs.get((c[0] + dy, c[1] + dx), [])]
        cells.append({"lat": lat, "lon": lon, "seen_by": "both" if near else "modis",
                      "modis_pixels": len(ps), "viirs_pixels": len(near),
                      "frp_mw": round(max(sum(p.frp for p in ps), sum(p.frp for p in near)), 1)})
    for c, ps in viirs.items():
        if _has_neighbour(c, modis):
            continue   # merged into the neighbouring MODIS cell, as in the harmonized count
        lat, lon = cell_center(c)
        cells.append({"lat": lat, "lon": lon, "seen_by": "viirs",
                      "modis_pixels": 0, "viirs_pixels": len(ps), "frp_mw": round(sum(p.frp for p in ps), 1)})
    stats = _day_stats(day, pts)
    return {"day": day.isoformat(), "cells": cells, "overpasses": _overpasses(pts) if pts else [],
            "stats": stats.as_dict()}
