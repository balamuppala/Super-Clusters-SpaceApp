"""NASA FIRMS: VIIRS (S-NPP / NOAA-20 / NOAA-21) and MODIS (Terra / Aqua) active fire.

* ``FirmsSource``            near-real-time detections for an AOI (live lane)
* ``FirmsHistoricalSource``  standard-processing archive around a fire event,
                             same calendar window in previous years (temporal frequency)
* ``FirmsArchiveCsvSource``  bulk archive CSVs downloaded from FIRMS (historical backfill)

API: https://firms.modaps.eosdis.nasa.gov/api/area/
"""

from __future__ import annotations

import csv
import io
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from fireverify.config import Settings
from fireverify.ingestion.base import FetchRequest, ParseError, Scope, Source, SourceError
from fireverify.ingestion.http import HttpClient
from fireverify.models import BBox, Hotspot, RawRecord, RecordKind

FIRMS_AREA_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
FIRMS_MAX_DAY_RANGE = 5  # the area API accepts 1..5 days per request

# First date each standard-processing archive has data.
SP_ARCHIVE_START = {
    "MODIS_SP": date(2000, 11, 1),
    "VIIRS_SNPP_SP": date(2012, 1, 20),
    "VIIRS_NOAA20_SP": date(2018, 4, 1),
}

_VIIRS_PLATFORMS = {"VIIRS_SNPP": "Suomi-NPP", "VIIRS_NOAA20": "NOAA-20", "VIIRS_NOAA21": "NOAA-21"}
_MODIS_PLATFORMS = {"T": "Terra", "A": "Aqua", "TERRA": "Terra", "AQUA": "Aqua"}
_VIIRS_CONFIDENCE = {
    "l": (0.2, "low"), "low": (0.2, "low"),
    "n": (0.6, "nominal"), "nominal": (0.6, "nominal"),
    "h": (0.9, "high"), "high": (0.9, "high"),
}


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def parse_firms_csv(text: str) -> list[dict[str, str]]:
    text = text.lstrip("﻿")
    head = text.lstrip()[:200].lower()
    if not head.startswith("latitude"):
        # FIRMS answers bad keys / quota exhaustion with a 200 and a plain-text message.
        raise SourceError(f"unexpected FIRMS response: {text.strip()[:160]!r}")
    return [dict(row) for row in csv.DictReader(io.StringIO(text))]


def _float(row: dict[str, Any], key: str, required: bool = True) -> float | None:
    value = row.get(key)
    if value is None or str(value).strip() == "":
        if required:
            raise ParseError(f"missing field {key!r}")
        return None
    try:
        out = float(str(value).strip())
    except ValueError as exc:
        raise ParseError(f"field {key!r} is not numeric: {value!r}") from exc
    if math.isnan(out):
        if required:
            raise ParseError(f"field {key!r} is NaN")
        return None
    return out


def parse_acquisition(acq_date: Any, acq_time: Any) -> datetime:
    d = str(acq_date or "").strip()
    t = str(acq_time or "").strip()
    if not d:
        raise ParseError("missing field 'acq_date'")
    if not t:
        raise ParseError("missing field 'acq_time'")
    t = t.replace(":", "")
    if not t.isdigit() or len(t) > 4:
        raise ParseError(f"invalid acq_time {acq_time!r}")
    t = t.zfill(4)
    try:
        day = datetime.strptime(d, "%Y-%m-%d")
        return datetime(day.year, day.month, day.day, int(t[:2]), int(t[2:]), tzinfo=timezone.utc)
    except ValueError as exc:
        raise ParseError(f"invalid acquisition date/time {acq_date!r} {acq_time!r}") from exc


def normalize_confidence(sensor: str, raw: str) -> tuple[float, str]:
    value = raw.strip().lower()
    if sensor == "VIIRS":
        if value not in _VIIRS_CONFIDENCE:
            raise ParseError(f"unrecognized VIIRS confidence {raw!r}")
        return _VIIRS_CONFIDENCE[value]
    try:
        pct = float(value)
    except ValueError as exc:
        raise ParseError(f"unrecognized MODIS confidence {raw!r}") from exc
    if not 0 <= pct <= 100:
        raise ParseError(f"MODIS confidence out of 0..100: {raw!r}")
    cls = "low" if pct < 30 else "nominal" if pct < 80 else "high"
    return pct / 100.0, cls


def _sensor_and_platform(source: str, row: dict[str, Any]) -> tuple[str, str]:
    src = source.upper()
    if src.startswith("MODIS") or str(row.get("instrument", "")).upper() == "MODIS":
        sat = str(row.get("satellite", "")).strip().upper()
        return "MODIS", _MODIS_PLATFORMS.get(sat, sat or "unknown")
    for prefix, platform in _VIIRS_PLATFORMS.items():
        if src.startswith(prefix):
            return "VIIRS", platform
    if src.startswith("VIIRS") or str(row.get("instrument", "")).upper() == "VIIRS":
        return "VIIRS", str(row.get("satellite") or "unknown")
    raise ParseError(f"cannot determine sensor for source {source!r}")


def normalize_firms(raw: RawRecord) -> Hotspot:
    row = raw.payload
    sensor, platform = _sensor_and_platform(raw.source, row)
    if sensor == "VIIRS":
        bright = _float(row, "bright_ti4")
        aux = _float(row, "bright_ti5", required=False)
    else:
        bright = _float(row, "brightness")
        aux = _float(row, "bright_t31", required=False)
    conf_raw = str(row.get("confidence", "")).strip()
    if not conf_raw:
        raise ParseError("missing field 'confidence'")
    confidence, conf_class = normalize_confidence(sensor, conf_raw)
    daynight = str(row.get("daynight", "")).strip().upper() or None
    if daynight not in (None, "D", "N"):
        raise ParseError(f"invalid daynight flag {daynight!r}")
    return Hotspot(
        source=raw.source,
        sensor=sensor,
        platform=platform,
        latitude=_float(row, "latitude"),
        longitude=_float(row, "longitude"),
        acquired_at=parse_acquisition(row.get("acq_date"), row.get("acq_time")),
        brightness_k=bright,
        brightness_aux_k=aux,
        frp_mw=_float(row, "frp", required=False),
        scan_km=_float(row, "scan", required=False),
        track_km=_float(row, "track", required=False),
        confidence_raw=conf_raw,
        confidence=confidence,
        confidence_class=conf_class,
        daynight=daynight,
        version=(str(row.get("version")).strip() or None) if row.get("version") is not None else None,
    )


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------

def _date_chunks(start: datetime, end: datetime) -> Iterator[tuple[date, int]]:
    day = start.date()
    last = end.date()
    while day <= last:
        span = min(FIRMS_MAX_DAY_RANGE, (last - day).days + 1)
        yield day, span
        day += timedelta(days=span)


class FirmsSource(Source):
    """Near-real-time FIRMS detections for the AOI."""

    parser = "firms"
    kind = RecordKind.HOTSPOT
    scope = Scope.AOI

    def __init__(self, source_name: str, settings: Settings, http: HttpClient | None) -> None:
        self.name = source_name
        self.settings = settings
        self.http = http

    def available(self) -> tuple[bool, str]:
        if not self.settings.has_firms:
            return False, "FIRMS_MAP_KEY not set"
        return True, "ok"

    def _fetch_window(self, source: str, bbox: BBox, day: date, span: int) -> list[tuple[dict[str, str], dict | None]]:
        """Return (csv_row, sim_truth) pairs; live data has no truth."""
        url = f"{FIRMS_AREA_URL}/{self.settings.firms_map_key}/{source}/{bbox.as_firms()}/{span}/{day.isoformat()}"
        return [(row, None) for row in parse_firms_csv(self.http.get_text(url))]

    def fetch(self, req: FetchRequest) -> Iterator[RawRecord]:
        for day, span in _date_chunks(req.start, req.end):
            for row, truth in self._fetch_window(self.name, req.aoi, day, span):
                yield self._raw(row, sim_truth=truth)


def _shift_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year - years)
    except ValueError:  # Feb 29 -> Feb 28
        return d.replace(year=d.year - years, day=28)


class FirmsHistoricalSource(FirmsSource):
    """Archive detections near a fire event, same calendar window in each previous year."""

    kind = RecordKind.HISTORICAL_HOTSPOT
    scope = Scope.TARGET

    def __init__(self, archive_source: str, settings: Settings, http: HttpClient | None) -> None:
        super().__init__(archive_source, settings, http)
        self.name = f"{archive_source}_HISTORY"
        self.archive_source = archive_source

    def history_windows(self, event_day: date) -> list[tuple[date, int]]:
        """Same calendar window in each past year, plus (optionally) one six months away.

        The off-season window separates seasonal recurrence (crop burning every
        autumn) from year-round heat sources (flares, volcanoes, industry).
        """
        days = max(1, min(FIRMS_MAX_DAY_RANGE, self.settings.history_window_days))
        earliest = SP_ARCHIVE_START.get(self.archive_source, date(2000, 11, 1))
        latest = event_day - timedelta(days=days + 1)
        windows = []
        for years_back in range(1, self.settings.history_years + 1):
            in_season = _shift_years(event_day, years_back) - timedelta(days=days // 2)
            candidates = [in_season]
            if self.settings.history_offseason:
                candidates.append(in_season + timedelta(days=182))
            windows += [(start, days) for start in candidates if earliest <= start <= latest]
        return sorted(windows)

    def fetch(self, req: FetchRequest) -> Iterator[RawRecord]:
        target = req.target
        if target is None:
            raise SourceError("historical source requires an enrichment target")
        radius = max(self.settings.history_radius_km, target.radius_km) + 0.5
        bbox = BBox.around(target.latitude, target.longitude, radius)
        for day, span in self.history_windows(target.event_start.date()):
            for row, truth in self._fetch_window(self.archive_source, bbox, day, span):
                yield self._raw(row, source=self.archive_source, target_id=target.cluster_id, sim_truth=truth)


class FirmsArchiveCsvSource(Source):
    """Bulk FIRMS archive CSV (https://firms.modaps.eosdis.nasa.gov/download/) as historical detections."""

    parser = "firms"
    kind = RecordKind.HISTORICAL_HOTSPOT
    scope = Scope.AOI

    def __init__(self, path: Path, archive_source: str) -> None:
        self.path = Path(path)
        self.name = archive_source

    def available(self) -> tuple[bool, str]:
        return (True, "ok") if self.path.is_file() else (False, f"{self.path} not found")

    def fetch(self, req: FetchRequest) -> Iterator[RawRecord]:
        with self.path.open(newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                yield self._raw(dict(row))
