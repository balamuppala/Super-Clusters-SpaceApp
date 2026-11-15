"""Simulated data lane.

``SimWorld`` builds a deterministic synthetic world (seeded) with labelled
scenarios, then emits raw payloads *in the exact wire format of each live
source* (FIRMS CSV rows, Sentinel Hub Statistical API JSON, CDSE OData
products, GeoJSON). Simulated connectors subclass the live ones and only swap
the transport, so the real parsers and validators are exercised end to end.

Every simulated raw record carries ``sim_truth`` (scenario label and, for
injected anomalies, the reason code it is expected to trigger) so the
verification app can be scored against ground truth.

Scenarios
---------
wildfire            spreading vegetation fire, multi-sensor, strong NDVI/NBR and SAR drop
agricultural_burn   short crop-residue burn, seasonal recurrence, moderate NDVI drop
industrial_flare    persistent gas flare next to an industrial site, no vegetation change
volcano             thermal anomaly near a volcanic vent; post-event optical is cloudy
urban_false_alarm   single hot-surface detection inside an urban area
low_confidence      single low-confidence detection (exercises quarantine)
smoldering_fire     small, weak real fire: few VIIRS detections, faint NDVI/SAR change
unmapped_flare      persistent gas flare that is missing from the context layers
corrupt             malformed / impossible / duplicated records (exercises reject + quarantine)
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterator

from fireverify.config import Settings
from fireverify.geo import haversine_km, offset_point, solar_elevation_deg, square_polygon
from fireverify.ingestion.base import FetchRequest
from fireverify.ingestion.sources.context import GeoJsonContextSource
from fireverify.ingestion.sources.copernicus import iso_z
from fireverify.ingestion.sources.firms import FirmsHistoricalSource, FirmsSource
from fireverify.ingestion.sources.sentinel1 import Sentinel1SarSource
from fireverify.ingestion.sources.sentinel2 import Sentinel2NdviSource
from fireverify.ingestion.sources.sentinel3 import CdseSceneSource
from fireverify.models import BBox, EnrichmentTarget, Lane, stable_hash

SCENARIOS = (
    "wildfire", "agricultural_burn", "industrial_flare", "volcano",
    "urban_false_alarm", "low_confidence", "smoldering_fire", "unmapped_flare", "corrupt",
)

# Chance that a site's post-fire Sentinel-2 window is hidden by smoke / cloud.
S2_OBSCURED_P = 0.2

# scenario -> (fraction of AOI width, fraction of AOI height, label, is_vegetation_fire)
SITE_LAYOUT: dict[str, tuple[float, float, str, bool]] = {
    "wildfire": (0.30, 0.62, "wildfire", True),
    "agricultural_burn": (0.72, 0.30, "agricultural_burn", True),
    "industrial_flare": (0.82, 0.80, "industrial_gas_flare", False),
    "volcano": (0.18, 0.20, "volcanic_activity", False),
    "urban_false_alarm": (0.50, 0.45, "false_alarm_hot_surface", False),
    "low_confidence": (0.55, 0.75, "false_alarm_low_confidence", False),
    "smoldering_fire": (0.35, 0.90, "smoldering_vegetation_fire", True),
    "unmapped_flare": (0.90, 0.55, "unmapped_gas_flare", False),
}

# FIRMS source -> overpasses as (platform, satellite code, local solar hour)
PASSES: dict[str, list[tuple[str, str, float]]] = {
    "VIIRS_SNPP_NRT": [("Suomi-NPP", "N", 13.5), ("Suomi-NPP", "N", 1.5)],
    "VIIRS_NOAA20_NRT": [("NOAA-20", "N20", 12.67), ("NOAA-20", "N20", 0.67)],
    "VIIRS_NOAA21_NRT": [("NOAA-21", "N21", 14.33), ("NOAA-21", "N21", 2.33)],
    "MODIS_NRT": [("Terra", "T", 10.5), ("Terra", "T", 22.5), ("Aqua", "A", 13.5), ("Aqua", "A", 1.5)],
}

# scenario -> (ndvi_pre, ndvi_post, nbr_pre, nbr_post, clear_fraction_pre, clear_fraction_post)
S2_PROFILES = {
    "wildfire": (0.68, 0.29, 0.46, -0.18, 0.90, 0.75),
    "agricultural_burn": (0.44, 0.27, 0.21, -0.04, 0.95, 0.85),
    "industrial_flare": (0.22, 0.21, 0.06, 0.05, 0.92, 0.90),
    "volcano": (0.28, 0.27, 0.08, 0.06, 0.80, 0.12),
    "urban_false_alarm": (0.16, 0.16, 0.02, 0.02, 0.90, 0.88),
    "low_confidence": (0.16, 0.16, 0.02, 0.02, 0.90, 0.88),
    "smoldering_fire": (0.62, 0.55, 0.40, 0.29, 0.90, 0.85),
    "unmapped_flare": (0.24, 0.23, 0.07, 0.06, 0.92, 0.90),
    "background": (0.50, 0.50, 0.25, 0.25, 0.90, 0.90),
}

# scenario -> (vv_pre, vv_post, vh_pre, vh_post) in dB
S1_PROFILES = {
    "wildfire": (-7.5, -8.7, -13.2, -16.0),
    "agricultural_burn": (-9.0, -9.8, -15.5, -16.6),
    "industrial_flare": (-6.0, -6.1, -12.0, -12.1),
    "volcano": (-8.0, -8.3, -14.0, -14.4),
    "urban_false_alarm": (-3.0, -3.0, -9.5, -9.6),
    "low_confidence": (-3.0, -3.0, -9.5, -9.6),
    "smoldering_fire": (-7.8, -8.1, -13.5, -14.3),
    "unmapped_flare": (-6.2, -6.2, -12.2, -12.3),
    "background": (-8.0, -8.0, -14.0, -14.0),
}

# scenario -> probability of an archive detection on any given historical day
HISTORY_DAILY_P = {
    "industrial_flare": 0.75,
    "unmapped_flare": 0.75,
    "volcano": 0.35,
    "agricultural_burn": 0.15,
}


@dataclass(frozen=True)
class SimSite:
    site_id: str
    scenario: str
    label: str
    is_vegetation_fire: bool
    lat: float
    lon: float

    def truth(self, **extra: Any) -> dict[str, Any]:
        return {
            "site_id": self.site_id, "scenario": self.scenario, "label": self.label,
            "is_vegetation_fire": self.is_vegetation_fire, **extra,
        }


def _daynight(lat: float, lon: float, t: datetime) -> str:
    return "D" if solar_elevation_deg(lat, lon, t) > 5 else "N"


def firms_row(source: str, sat: str, t: datetime, lat: float, lon: float, frp: float,
              conf_class: str, rng: random.Random, daynight: str | None = None,
              version: str | None = None) -> dict[str, str]:
    """One detection in the FIRMS CSV wire format of ``source`` (MODIS or VIIRS columns)."""
    dn = daynight or _daynight(lat, lon, t)
    night = dn == "N"
    root = math.sqrt(max(frp, 0.0))
    common = {
        "latitude": f"{lat:.5f}", "longitude": f"{lon:.5f}",
        "acq_date": t.strftime("%Y-%m-%d"), "acq_time": t.strftime("%H%M"),
        "satellite": sat, "frp": f"{frp:.2f}", "daynight": dn,
    }
    if source.startswith("MODIS"):
        conf = {"low": rng.randint(5, 25), "nominal": rng.randint(40, 79), "high": rng.randint(80, 100)}[conf_class]
        return {
            **common, "brightness": f"{(300 if night else 315) + 10 * root + rng.gauss(0, 3):.2f}",
            "scan": f"{rng.uniform(1.0, 3.2):.2f}", "track": f"{rng.uniform(1.0, 1.7):.2f}",
            "instrument": "MODIS", "confidence": str(conf), "version": version or "6.1NRT",
            "bright_t31": f"{(275 if night else 292) + 1.5 * root + rng.gauss(0, 1.5):.2f}",
        }
    return {
        **common,
        "bright_ti4": f"{min(367.0, (295 if night else 315) + 9 * root + rng.gauss(0, 2)):.2f}",
        "scan": f"{rng.uniform(0.39, 0.72):.2f}", "track": f"{rng.uniform(0.36, 0.55):.2f}",
        "instrument": "VIIRS", "confidence": {"low": "l", "nominal": "n", "high": "h"}[conf_class],
        "version": version or "2.0NRT",
        "bright_ti5": f"{(275 if night else 290) + 1.8 * root + rng.gauss(0, 1.5):.2f}",
    }


class SimWorld:
    def __init__(self, aoi: BBox, start: datetime, end: datetime, now: datetime, seed: int = 42,
                 scenarios: list[str] | tuple[str, ...] | None = None) -> None:
        unknown = set(scenarios or ()) - set(SCENARIOS)
        if unknown:
            raise ValueError(f"unknown scenarios: {sorted(unknown)}; choose from {SCENARIOS}")
        self.aoi, self.start, self.end, self.now, self.seed = aoi, start, end, now, seed
        self.scenarios = tuple(scenarios or SCENARIOS)
        self.sites = [
            SimSite(
                site_id=f"SIM-{name.upper()}-{seed}",
                scenario=name, label=label, is_vegetation_fire=veg,
                lat=aoi.south + fy * (aoi.north - aoi.south),
                lon=aoi.west + fx * (aoi.east - aoi.west),
            )
            for name, (fx, fy, label, veg) in SITE_LAYOUT.items() if name in self.scenarios
        ]
        self._rows: dict[str, list[tuple[dict[str, str], dict[str, Any]]]] = {s: [] for s in PASSES}
        self._anomalies: dict[str, list[tuple[dict[str, str], dict[str, Any]]]] = {s: [] for s in PASSES}
        for site in self.sites:
            getattr(self, f"_gen_{site.scenario}")(site)
        for rows in self._rows.values():
            rows.sort(key=lambda rt: (rt[0]["acq_date"], rt[0]["acq_time"]))
        if "corrupt" in self.scenarios:
            self._gen_corrupt()

    # ------------------------------------------------------------------ utils

    def _rng(self, *keys: Any) -> random.Random:
        return random.Random(stable_hash(self.seed, *keys))

    def site(self, scenario: str) -> SimSite | None:
        return next((s for s in self.sites if s.scenario == scenario), None)

    def nearest_site(self, lat: float, lon: float, max_km: float = 5.0) -> SimSite | None:
        best = min(self.sites, key=lambda s: haversine_km(lat, lon, s.lat, s.lon), default=None)
        if best and haversine_km(lat, lon, best.lat, best.lon) <= max_km:
            return best
        return None

    def pass_times(self, source: str, lon: float, start: datetime, end: datetime,
                   rng: random.Random) -> list[tuple[datetime, str, str]]:
        out = []
        day = (start - timedelta(days=1)).date()
        while day <= (end + timedelta(days=1)).date():
            for platform, sat, local_h in PASSES[source]:
                utc_h = local_h - lon / 15 + rng.uniform(-0.3, 0.3)
                t = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(hours=utc_h)
                t = t.replace(second=0, microsecond=0)
                if start <= t <= end:
                    out.append((t, platform, sat))
            day += timedelta(days=1)
        return sorted(out)

    def _row(self, source: str, sat: str, t: datetime, lat: float, lon: float, frp: float,
             conf_class: str, rng: random.Random, daynight: str | None = None,
             version: str | None = None) -> dict[str, str]:
        return firms_row(source, sat, t, lat, lon, frp, conf_class, rng, daynight, version)

    def _emit(self, source: str, row: dict[str, str], truth: dict[str, Any]) -> None:
        self._rows[source].append((row, truth))

    def _single_pass(self, source: str, site: SimSite, target_time: datetime, want_day: bool = True):
        rng = self._rng(site.site_id, source, "single")
        passes = self.pass_times(source, site.lon, self.start, self.end, rng)
        if want_day:
            passes = [p for p in passes if _daynight(site.lat, site.lon, p[0]) == "D"] or passes
        if not passes:
            return None, rng
        return min(passes, key=lambda p: abs((p[0] - target_time).total_seconds())), rng

    # -------------------------------------------------------------- scenarios

    def _gen_wildfire(self, site: SimSite) -> None:
        ignition = max(self.start + timedelta(hours=1), self.end - timedelta(hours=30))
        for source in PASSES:
            rng = self._rng(site.site_id, source)
            viirs = source.startswith("VIIRS")
            for t, _platform, sat in self.pass_times(source, site.lon, ignition, self.end, rng):
                if rng.random() > 0.9:  # obscured by smoke / cloud on this pass
                    continue
                elapsed_h = (t - ignition).total_seconds() / 3600
                radius = 0.3 + 0.12 * elapsed_h
                pixel_km = 0.375 if viirs else 1.0
                n = max(1, min(40, int(2 * math.pi * radius / pixel_km * 0.6)))
                for _ in range(n):
                    ang = rng.uniform(0, 2 * math.pi)
                    r = radius * rng.uniform(0.55, 1.0)
                    lat, lon = offset_point(site.lat, site.lon, r * math.cos(ang), r * math.sin(ang))
                    dn = _daynight(lat, lon, t)
                    frp = rng.lognormvariate(math.log(18 if viirs else 45), 0.7) * (0.6 if dn == "N" else 1.0)
                    row = self._row(source, sat, t, lat, lon, frp, "high" if frp > 15 else "nominal", rng, dn)
                    self._emit(source, row, site.truth())

    def _gen_agricultural_burn(self, site: SimSite) -> None:
        # Burn at 13:00 local solar time on the most recent day that fits in the window.
        local_noon_offset = timedelta(hours=13 - site.lon / 15)
        day = self.end.date()
        burn = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + local_noon_offset
        while burn > self.end - timedelta(hours=2):
            burn -= timedelta(days=1)
        candidates = []
        for source in PASSES:
            rng = self._rng(site.site_id, source)
            for t, _p, sat in self.pass_times(source, site.lon, burn - timedelta(hours=3), burn + timedelta(hours=5), rng):
                candidates.append((abs((t - burn).total_seconds()), source, t, sat))
        for _dist, source, t, sat in sorted(candidates)[:3]:
            rng = self._rng(site.site_id, source, t.isoformat())
            for _ in range(rng.randint(1, 3)):
                lat, lon = offset_point(site.lat, site.lon, rng.uniform(-0.3, 0.3), rng.uniform(-0.3, 0.3))
                self._emit(source, self._row(source, sat, t, lat, lon, rng.uniform(1.5, 9.0), "nominal", rng), site.truth())

    def _gen_industrial_flare(self, site: SimSite) -> None:
        for source in PASSES:
            rng = self._rng(site.site_id, source)
            viirs = source.startswith("VIIRS")
            for t, _p, sat in self.pass_times(source, site.lon, self.start, self.end, rng):
                night = _daynight(site.lat, site.lon, t) == "N"
                p = (0.85 if night else 0.45) if viirs else 0.35
                if rng.random() > p:
                    continue
                lat, lon = offset_point(site.lat, site.lon, rng.uniform(-0.1, 0.1), rng.uniform(-0.1, 0.1))
                row = self._row(source, sat, t, lat, lon, rng.uniform(6, 28), "high" if night else "nominal", rng)
                self._emit(source, row, site.truth())

    def _gen_unmapped_flare(self, site: SimSite) -> None:
        self._gen_industrial_flare(site)

    def _gen_smoldering_fire(self, site: SimSite) -> None:
        # Two night-time VIIRS passes, one weak pixel each.
        rng = self._rng(site.site_id, "smolder")
        passes = []
        for source in ("VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT"):
            for t, _p, sat in self.pass_times(source, site.lon, self.end - timedelta(hours=26), self.end, rng):
                if _daynight(site.lat, site.lon, t) == "N":
                    passes.append((t, source, sat))
        for t, source, sat in sorted(passes)[-2:]:
            lat, lon = offset_point(site.lat, site.lon, rng.uniform(-0.2, 0.2), rng.uniform(-0.2, 0.2))
            self._emit(source, self._row(source, sat, t, lat, lon, rng.uniform(2.0, 5.0), "nominal", rng, "N"),
                       site.truth())

    def _gen_volcano(self, site: SimSite) -> None:
        for source in PASSES:
            rng = self._rng(site.site_id, source)
            for t, _p, sat in self.pass_times(source, site.lon, self.start, self.end, rng):
                if rng.random() > 0.7:
                    continue
                for _ in range(rng.randint(1, 3)):
                    lat, lon = offset_point(site.lat, site.lon, rng.uniform(-0.8, 0.8), rng.uniform(-0.8, 0.8))
                    frp = rng.lognormvariate(math.log(40), 0.6)
                    self._emit(source, self._row(source, sat, t, lat, lon, frp, "high", rng), site.truth())

    def _gen_urban_false_alarm(self, site: SimSite) -> None:
        chosen, rng = self._single_pass("VIIRS_SNPP_NRT", site, self.end - timedelta(hours=6))
        if chosen:
            t, _p, sat = chosen
            self._emit("VIIRS_SNPP_NRT", self._row("VIIRS_SNPP_NRT", sat, t, site.lat, site.lon, 1.4, "nominal", rng), site.truth())

    def _gen_low_confidence(self, site: SimSite) -> None:
        chosen, rng = self._single_pass("VIIRS_NOAA20_NRT", site, self.end - timedelta(hours=8))
        if chosen:
            t, _p, sat = chosen
            row = self._row("VIIRS_NOAA20_NRT", sat, t, site.lat, site.lon, 0.9, "low", rng)
            self._emit("VIIRS_NOAA20_NRT", row, site.truth(expected="Q_LOW_CONF"))

    def _gen_corrupt(self) -> None:
        rng = self._rng("corrupt")
        clat, clon = self.aoi.center
        base_t = (self.end - timedelta(hours=5)).replace(second=0, microsecond=0)

        def viirs(t: datetime = base_t, dn: str | None = None, **mods: str) -> dict[str, str]:
            lat, lon = offset_point(clat, clon, rng.uniform(-3, 3), rng.uniform(-3, 3))
            row = self._row("VIIRS_SNPP_NRT", "N", t, lat, lon, rng.uniform(3, 30), "nominal", rng, dn)
            row.update(mods)
            return row

        def modis(**mods: str) -> dict[str, str]:
            lat, lon = offset_point(clat, clon, rng.uniform(-3, 3), rng.uniform(-3, 3))
            row = self._row("MODIS_NRT", "T", base_t, lat, lon, rng.uniform(5, 40), "nominal", rng)
            row.update(mods)
            return row

        # Most recent local solar 00:30 inside the window -> the sun is well below the horizon.
        day = self.end.date()
        midnight = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(hours=0.5 - clon / 15)
        while midnight > self.end:
            midnight -= timedelta(days=1)

        cases: list[tuple[str, str, dict[str, str]]] = [
            ("lat_out_of_range", "R_COORD", viirs(latitude="95.30000")),
            ("lon_not_numeric", "R_PARSE", viirs(longitude="not-a-number")),
            ("missing_acq_time", "R_PARSE", viirs(acq_time="")),
            ("far_future", "R_FUTURE", viirs(t=self.now + timedelta(days=3))),
            ("clock_skew", "Q_CLOCK_SKEW", viirs(t=self.now + timedelta(hours=2))),
            ("negative_frp", "R_PHYSICS", viirs(frp="-4.20")),
            ("impossible_brightness", "R_PHYSICS", viirs(bright_ti4="9999.00")),
            ("missing_frp", "Q_MISSING_FRP", viirs(frp="")),
            ("daynight_mismatch", "Q_DAYNIGHT", viirs(t=midnight.replace(second=0, microsecond=0), dn="D")),
            ("stale_record", "Q_OUT_OF_WINDOW", viirs(t=self.start - timedelta(days=8))),
            ("outside_aoi", "R_AOI", viirs(latitude=f"{min(self.aoi.north + 0.5, 89.9):.5f}")),
        ]
        for anomaly_id, expected, row in cases:
            self._anomalies["VIIRS_SNPP_NRT"].append((row, {"label": "corrupt", "anomaly_id": anomaly_id, "expected": expected}))
        for anomaly_id, expected, row in [
            ("modis_confidence_out_of_range", "R_PARSE", modis(confidence="142")),
            ("modis_negative_scan", "R_PHYSICS", modis(scan="-1.00")),
        ]:
            self._anomalies["MODIS_NRT"].append((row, {"label": "corrupt", "anomaly_id": anomaly_id, "expected": expected}))
        # Exact re-delivery of an earlier row (feeds do this): first copy accepted, second rejected.
        if self._rows["VIIRS_SNPP_NRT"]:
            original = dict(self._rows["VIIRS_SNPP_NRT"][0][0])
        else:
            original = viirs()
            self._anomalies["VIIRS_SNPP_NRT"].append((dict(original), {"label": "corrupt", "anomaly_id": "duplicate_original"}))
        self._anomalies["VIIRS_SNPP_NRT"].append(
            (original, {"label": "corrupt", "anomaly_id": "exact_duplicate", "expected": "R_DUPLICATE"})
        )

    # ------------------------------------------------------------- emitters

    def firms_rows(self, source: str, aoi: BBox) -> Iterator[tuple[dict[str, str], dict[str, Any]]]:
        for row, truth in self._rows.get(source, []):
            if aoi.contains(float(row["latitude"]), float(row["longitude"])):
                yield dict(row), truth
        for row, truth in self._anomalies.get(source, []):
            yield dict(row), truth

    def history_rows(self, archive_source: str, bbox: BBox, day: date, span: int):
        clat, clon = bbox.center
        site = self.nearest_site(clat, clon, max_km=3.0)
        if site is None or site.scenario not in HISTORY_DAILY_P:
            return []
        p = HISTORY_DAILY_P[site.scenario]
        sat = "T" if archive_source.startswith("MODIS") else "N"
        season_doy = self.end.timetuple().tm_yday
        out = []
        for i in range(span):
            d = day + timedelta(days=i)
            rng = self._rng("history", site.site_id, archive_source, d.isoformat())
            off_season = min(abs(d.timetuple().tm_yday - season_doy), 365 - abs(d.timetuple().tm_yday - season_doy)) > 45
            if site.scenario == "agricultural_burn" and off_season:
                continue  # crop-residue burning only happens around harvest
            if rng.random() >= p:
                continue
            t = datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(hours=(1.5 - site.lon / 15) % 24)
            t = t.replace(second=0, microsecond=0)
            lat, lon = offset_point(site.lat, site.lon, rng.uniform(-0.25, 0.25), rng.uniform(-0.25, 0.25))
            row = self._row(archive_source, sat, t, lat, lon, rng.uniform(4, 25), "nominal", rng,
                            version="2" if sat == "N" else "6.1")
            out.append((row, site.truth(historical=True)))
        return out

    def context_features(self) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        feats: list[tuple[dict[str, Any], dict[str, Any]]] = []

        def add(fid: str, category: str, name: str, geometry: dict[str, Any], truth: dict[str, Any]) -> None:
            feats.append(({"type": "Feature", "id": fid, "properties": {"category": category, "name": name},
                           "geometry": geometry}, truth))

        for s in self.sites:
            t = s.truth(layer="context")
            point = {"type": "Point", "coordinates": [s.lon, s.lat]}
            if s.scenario == "wildfire":
                add(f"{s.site_id}-forest", "forest", "SIM mixed conifer forest", square_polygon(s.lat, s.lon, 6.0), t)
            elif s.scenario == "smoldering_fire":
                add(f"{s.site_id}-forest", "forest", "SIM oak woodland", square_polygon(s.lat, s.lon, 4.0), t)
            elif s.scenario == "agricultural_burn":
                add(f"{s.site_id}-crop", "cropland", "SIM irrigated cropland", square_polygon(s.lat, s.lon, 3.0), t)
            elif s.scenario == "industrial_flare":
                add(f"{s.site_id}-flare", "gas_flare", "SIM flare stack", point, t)
                add(f"{s.site_id}-works", "industrial", "SIM gas processing plant", square_polygon(s.lat, s.lon, 0.8), t)
            elif s.scenario == "volcano":
                add(f"{s.site_id}-vent", "volcano", "SIM stratovolcano", point, t)
            elif s.scenario in ("urban_false_alarm", "low_confidence"):
                add(f"{s.site_id}-urban", "urban", "SIM city", square_polygon(s.lat, s.lon, 3.0), t)
        plat, plon = (self.aoi.south + 0.1 * (self.aoi.north - self.aoi.south),
                      self.aoi.west + 0.9 * (self.aoi.east - self.aoi.west))
        add("SIM-power-plant", "power_plant", "SIM combined-cycle plant",
            {"type": "Point", "coordinates": [plon, plat]}, {"label": "background"})
        if "corrupt" in self.scenarios:
            clat, clon = self.aoi.center
            feats.append(({"type": "Feature", "id": "SIM-bad-1", "properties": {"name": "no category"},
                           "geometry": {"type": "Point", "coordinates": [clon, clat]}},
                          {"label": "corrupt", "anomaly_id": "context_missing_category", "expected": "R_PARSE"}))
            feats.append(({"type": "Feature", "id": "SIM-bad-2", "properties": {"category": "industrial"},
                           "geometry": {"type": "Circle", "coordinates": [clon, clat]}},
                          {"label": "corrupt", "anomaly_id": "context_bad_geometry", "expected": "R_PARSE"}))
        return feats

    @staticmethod
    def _stats_response(outputs: dict[str, tuple[float, int, int]], t_from: datetime, t_to: datetime) -> dict[str, Any]:
        def stats(mean: float, samples: int, nodata: int) -> dict[str, Any]:
            valid = samples - nodata > 0
            return {"min": round(mean - 0.15, 4) if valid else "NaN", "max": round(mean + 0.15, 4) if valid else "NaN",
                    "mean": round(mean, 5) if valid else "NaN", "stDev": 0.05 if valid else "NaN",
                    "sampleCount": samples, "noDataCount": nodata}
        return {
            "data": [{
                "interval": {"from": iso_z(t_from), "to": iso_z(t_to)},
                "outputs": {oid: {"bands": {"B0": {"stats": stats(*v)}}} for oid, v in outputs.items()},
            }],
            "status": "OK",
        }

    def _profile(self, target: EnrichmentTarget, table: dict[str, tuple]) -> tuple[SimSite | None, tuple]:
        site = self.nearest_site(target.latitude, target.longitude)
        return site, table[site.scenario if site else "background"]

    def s2_responses(self, target: EnrichmentTarget, w: dict[str, datetime]):
        site, (n0, n1, b0, b1, c0, c1) = self._profile(target, S2_PROFILES)
        rng = self._rng("s2", target.cluster_id)
        if site and self._rng("s2-obscured", site.site_id).random() < S2_OBSCURED_P:
            c1 = 0.1  # smoke / cloud over the post-fire window -> quarantined (Q_CLOUDY)
        samples = 22500
        noise = lambda: rng.gauss(0, 0.015)  # noqa: E731
        nd0, nd1 = int(samples * (1 - c0)), int(samples * (1 - c1))
        pre = self._stats_response({"ndvi": (n0 + noise(), samples, nd0), "nbr": (b0 + noise(), samples, nd0),
                                    "dataMask": (1.0, samples, nd0)}, w["pre_from"], w["pre_to"])
        post = None
        if w["post_to"] > w["post_from"]:
            post = self._stats_response({"ndvi": (n1 + noise(), samples, nd1), "nbr": (b1 + noise(), samples, nd1),
                                         "dataMask": (1.0, samples, nd1)}, w["post_from"], w["post_to"])
        return pre, post, (site.truth() if site else {"label": "background"})

    def s1_responses(self, target: EnrichmentTarget, w: dict[str, datetime]):
        site, (vv0, vv1, vh0, vh1) = self._profile(target, S1_PROFILES)
        rng = self._rng("s1", target.cluster_id)
        samples = 5600
        noise = lambda: rng.gauss(0, 0.2)  # noqa: E731
        pre = self._stats_response({"vv": (vv0 + noise(), samples, 0), "vh": (vh0 + noise(), samples, 0)},
                                   w["pre_from"], w["pre_to"])
        post = None
        if w["post_to"] > w["post_from"]:
            post = self._stats_response({"vv": (vv1 + noise(), samples, 0), "vh": (vh1 + noise(), samples, 0)},
                                        w["post_from"], w["post_to"])
        return pre, post, (site.truth() if site else {"label": "background"})

    def s3_products(self, lat: float, lon: float, start: datetime, end: datetime) -> list[dict[str, Any]]:
        out = []
        for plat, offset_h in (("S3A", 0.0), ("S3B", 0.6)):
            rng = self._rng("s3", plat, round(lat, 2), round(lon, 2))
            day = (start - timedelta(days=1)).date()
            while day <= (end + timedelta(days=1)).date():
                for local_h in (10.0, 22.0):
                    t = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(
                        hours=local_h + offset_h - lon / 15 + rng.uniform(-0.4, 0.4))
                    t = t.replace(microsecond=0)
                    if start <= t <= end and rng.random() < 0.8:
                        h = stable_hash(plat, t.isoformat(), length=32)
                        t_end = t + timedelta(minutes=3)
                        out.append({
                            "Id": f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}",
                            "Name": f"{plat}_SL_2_FRP____{t:%Y%m%dT%H%M%S}_{t_end:%Y%m%dT%H%M%S}_"
                                    f"{self.now:%Y%m%dT%H%M%S}_0179_000_000_SIM_O_NR_004.SEN3",
                            "ContentDate": {"Start": t.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                                            "End": t_end.strftime("%Y-%m-%dT%H:%M:%S.000Z")},
                        })
                day += timedelta(days=1)
        return sorted(out, key=lambda p: p["ContentDate"]["Start"])


# South-west corners of 2x2 degree boxes over fire-prone land, so simulated
# batches land where real fires happen (and not in the ocean on the map).
FIRE_PRONE_REGIONS: tuple[tuple[str, float, float], ...] = (
    ("California", 38.0, -122.0), ("Portugal", 39.5, -8.5), ("Greece", 37.5, 21.5), ("Siberia", 60.0, 110.0),
    ("Amazon", -10.0, -55.0), ("Cerrado", -15.0, -48.0), ("NSW Australia", -34.0, 149.0), ("Sahel", 11.0, 5.0),
    ("Northern Thailand", 18.0, 99.0), ("Alberta", 55.0, -116.0), ("Kalimantan", -2.5, 112.0), ("Zambia", -15.0, 26.0),
    ("Punjab", 30.0, 74.5), ("Central Chile", -37.0, -72.5), ("Turkey", 37.0, 30.0), ("Kazakhstan", 50.0, 70.0),
    ("Mexico", 19.0, -100.0), ("Spain", 38.5, -6.5), ("Western Cape", -34.0, 19.0), ("Victoria Australia", -38.0, 144.0),
)


def batch_aoi(index: int, size_deg: float = 2.0) -> BBox:
    """Non-overlapping AOIs for multi-run simulation batches, cycling through fire-prone regions."""
    _name, south, west = FIRE_PRONE_REGIONS[index % len(FIRE_PRONE_REGIONS)]
    shift = (index // len(FIRE_PRONE_REGIONS)) * (size_deg + 0.5)   # next lap: step east, still inland-ish
    west = min(178.0 - size_deg, west + shift)
    return BBox(west, south, west + size_deg, south + size_deg)


# --------------------------------------------------------------------------
# Simulated connectors: live classes with the transport swapped for SimWorld.
# --------------------------------------------------------------------------


class SimulatedFirmsSource(FirmsSource):
    lane = Lane.SIMULATED

    def __init__(self, source_name: str, settings: Settings, world: SimWorld) -> None:
        super().__init__(source_name, settings, None)
        self.world = world

    def available(self) -> tuple[bool, str]:
        return True, "simulated"

    def fetch(self, req: FetchRequest):
        for row, truth in self.world.firms_rows(self.name, req.aoi):
            yield self._raw(row, sim_truth=truth)


class SimulatedFirmsHistoricalSource(FirmsHistoricalSource):
    lane = Lane.SIMULATED

    def __init__(self, archive_source: str, settings: Settings, world: SimWorld) -> None:
        super().__init__(archive_source, settings, None)
        self.world = world

    def available(self) -> tuple[bool, str]:
        return True, "simulated"

    def _fetch_window(self, source: str, bbox: BBox, day: date, span: int):
        return self.world.history_rows(source, bbox, day, span)


class SimulatedSentinel2Source(Sentinel2NdviSource):
    lane = Lane.SIMULATED

    def __init__(self, settings: Settings, world: SimWorld) -> None:
        super().__init__(settings, None)
        self.world = world

    def available(self) -> tuple[bool, str]:
        return True, "simulated"

    def _responses(self, target, bbox, w):
        return self.world.s2_responses(target, w)


class SimulatedSentinel1Source(Sentinel1SarSource):
    lane = Lane.SIMULATED

    def __init__(self, settings: Settings, world: SimWorld) -> None:
        super().__init__(settings, None)
        self.world = world

    def available(self) -> tuple[bool, str]:
        return True, "simulated"

    def _responses(self, target, bbox, w):
        return self.world.s1_responses(target, w)


class SimulatedSentinel3Source(CdseSceneSource):
    lane = Lane.SIMULATED

    def __init__(self, settings: Settings, world: SimWorld) -> None:
        super().__init__("S3_SLSTR_FRP", "SENTINEL-3", "SL_2_FRP", settings, None, settings.s3_window_hours)
        self.world = world

    def available(self) -> tuple[bool, str]:
        return True, "simulated"

    def _search(self, bbox, start, end):
        lat, lon = bbox.center
        return self.world.s3_products(lat, lon, start, end)


class SimulatedContextSource(GeoJsonContextSource):
    name = "SIM_CONTEXT"
    lane = Lane.SIMULATED

    def __init__(self, world: SimWorld) -> None:
        super().__init__([])
        self.world = world

    def available(self) -> tuple[bool, str]:
        return True, "simulated"

    def _features(self, req):
        for feature, truth in self.world.context_features():
            yield feature, "sim_context", truth
