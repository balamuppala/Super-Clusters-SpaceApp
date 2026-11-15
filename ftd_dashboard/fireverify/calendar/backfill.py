"""Calendar backfill: a continuous MODIS + VIIRS record for one area and date range.

The detection runs only keep a few days. The calendar needs every day, so this job:

* picks, per sensor family and per day, the best FIRMS product that covers it:
  standard processing (``*_SP``, science quality) where it exists, near
  real-time (``*_NRT``) for the last few months. For VIIRS, NOAA-20 is the
  primary platform and S-NPP fills dates before NOAA-20 data exists. Date
  ranges come from FIRMS' ``data_availability`` endpoint.
* fetches 5-day chunks in parallel and routes every row through the normal
  parse -> validate -> lane path as ``historical_hotspot`` records. Calendar
  data therefore also feeds the temporal-frequency evidence used for verification.
* records per-day coverage (``ok`` / ``unavailable`` / ``error``) so the
  calendar can tell "no fire" apart from "no data".
* skips chunks fetched before, unless they were still near real-time when
  fetched (late detections arrive for a few days).

Low-confidence pixels are kept with a ``low_confidence`` flag instead of being
quarantined: a year of archive would flood the review queue. The calendar
leaves them out by default.

The simulated lane uses ``SimFireRegime``: a seeded synthetic fire regime with
a fire season, a multi-year trend, a persistent gas flare and injected
anomalies (outbreak spike, off-season burst, MODIS outage). Its rows come out
in FIRMS CSV wire format, so the same parse/validate/harmonize code is tested.
"""

from __future__ import annotations

import json
import logging
import math
import random
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Iterator

from fireverify.config import Settings
from fireverify.geo import offset_point
from fireverify.ingestion.base import FetchRequest, Scope, SourceError
from fireverify.ingestion.engine import IngestionEngine, RunReport, SourceStats
from fireverify.ingestion.http import HttpClient
from fireverify.ingestion.simulated import _daynight, firms_row
from fireverify.ingestion.sources.firms import (
    FIRMS_AREA_URL, FIRMS_MAX_DAY_RANGE, SP_ARCHIVE_START, FirmsSource, parse_firms_csv,
)
from fireverify.ingestion.validation import ValidationContext
from fireverify.models import BBox, Lane, Mode, RecordKind, stable_hash, utcnow

log = logging.getLogger(__name__)

FIRMS_AVAILABILITY_URL = "https://firms.modaps.eosdis.nasa.gov/api/data_availability/csv"
# Priority order per family: the first product covering a day wins.
FAMILY_SOURCES: dict[str, tuple[str, ...]] = {
    "MODIS": ("MODIS_SP", "MODIS_NRT"),
    "VIIRS": ("VIIRS_NOAA20_SP", "VIIRS_NOAA20_NRT", "VIIRS_SNPP_SP", "VIIRS_SNPP_NRT"),
}
NRT_SETTLE_DAYS = 3      # re-fetch NRT chunks fetched less than this long after their last day
FETCH_WORKERS = 4
MAX_CALENDAR_DAYS = 731          # longest daily view
MODIS_START = date(2000, 11, 1)  # first day of the MODIS archive: the earliest a backfill can reach
FIRMS_STATUS_URL = "https://firms.modaps.eosdis.nasa.gov/mapserver/mapkey_status/"
COST_PER_REQUEST = 10            # measured: one 5-day area request costs about 10 transactions
QUOTA_HEADROOM = 0.85            # never plan past 85% of the key's 10-minute limit
MAX_CALENDAR_AREA_DEG2 = 25.0


def default_availability(today: date) -> dict[str, tuple[date, date]]:
    """Conservative fallback when the availability endpoint can't be reached."""
    sp_end = today - timedelta(days=120)
    out = {name: (start, sp_end) for name, start in SP_ARCHIVE_START.items()}
    for name in ("MODIS_NRT", "VIIRS_NOAA20_NRT", "VIIRS_SNPP_NRT"):
        out[name] = (sp_end + timedelta(days=1), today)
    return out


def fetch_availability(http: HttpClient, key: str) -> dict[str, tuple[date, date]]:
    text = http.get_text(f"{FIRMS_AVAILABILITY_URL}/{key}/ALL")
    out = {}
    for line in text.strip().splitlines()[1:]:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            try:
                out[parts[0]] = (date.fromisoformat(parts[1]), date.fromisoformat(parts[2]))
            except ValueError:
                continue
    if not out:
        raise SourceError(f"unexpected availability response: {text[:120]!r}")
    return out


@dataclass(frozen=True)
class Chunk:
    family: str
    source: str
    day: date
    span: int

    @property
    def days(self) -> list[date]:
        return [self.day + timedelta(days=i) for i in range(self.span)]

    @property
    def last(self) -> date:
        return self.day + timedelta(days=self.span - 1)


def plan(family: str, start: date, end: date,
         availability: dict[str, tuple[date, date]]) -> tuple[list[Chunk], list[date]]:
    """Split [start, end] into <=5-day chunks of the best product per day; also return uncovered days."""
    chunks: list[Chunk] = []
    uncovered: list[date] = []
    d = start
    while d <= end:
        src = next((s for s in FAMILY_SOURCES[family]
                    if s in availability and availability[s][0] <= d <= availability[s][1]), None)
        if src is None:
            uncovered.append(d)
        elif chunks and chunks[-1].source == src and chunks[-1].span < FIRMS_MAX_DAY_RANGE \
                and chunks[-1].last == d - timedelta(days=1):
            last = chunks[-1]
            chunks[-1] = replace(last, span=last.span + 1)
        else:
            chunks.append(Chunk(family, src, d, 1))
        d += timedelta(days=1)
    return chunks, uncovered


def sampled_years(today: date, step: int = 5) -> list[tuple[int, tuple[str, ...]]]:
    """Every ``step``-th full year since 2001 plus the last full year, MODIS for all and VIIRS for the
    two most recent (enough to calibrate the sensors against each other). Most informative order first."""
    last = today.year - 1
    years = sorted(set(range(2001, last + 1, step)) | {last})
    recent = set(years[-2:])
    order = [years[-1], years[0]] + [y for y in reversed(years[1:-1])]
    return [(y, ("MODIS", "VIIRS") if y in recent else ("MODIS",)) for y in order]


class FirmsQuota:
    """Paces FIRMS requests against the key's live transaction count (5,000 per 10 minutes by default).

    Every ``check_every`` requests it reads ``mapkey_status``. If the planned
    usage would pass QUOTA_HEADROOM of the limit, it waits until the
    10-minute window has drained. Thread-safe; workers block in ``acquire``.
    """

    def __init__(self, http: HttpClient, key: str, progress: Callable[[str, float], None] | None = None,
                 check_every: int = 40) -> None:
        self.http, self.key, self.progress = http, key, progress
        self.check_every = check_every
        self.lock = threading.Lock()
        self.limit, self.current, self.since_check, self.fresh = 5000, 0, 0, False
        self.waited_s = 0.0

    def _refresh(self) -> None:
        try:
            st = json.loads(self.http.get_text(f"{FIRMS_STATUS_URL}?MAP_KEY={self.key}"))
            self.limit = int(st.get("transaction_limit") or self.limit)
            self.current = int(st.get("current_transactions") or 0)
        except Exception as exc:  # status endpoint down: assume we're near the limit and keep pacing
            log.warning("FIRMS quota check failed: %s", exc)
            self.current = int(self.limit * 0.6)
        self.since_check, self.fresh = 0, True

    def acquire(self) -> None:
        with self.lock:
            while True:
                if not self.fresh or self.since_check >= self.check_every:
                    self._refresh()
                planned = self.current + (self.since_check + 1) * COST_PER_REQUEST
                if planned <= self.limit * QUOTA_HEADROOM:
                    self.since_check += 1
                    return
                if self.progress:
                    self.progress(f"pausing for the FIRMS quota ({self.current:,}/{self.limit:,} transactions "
                                  f"in the last 10 min)", -1)
                _time.sleep(30)
                self.waited_s += 30
                self._refresh()

    def backoff(self) -> None:
        with self.lock:
            _time.sleep(60)
            self.waited_s += 60
            self.fresh = False


class _RowsSource(FirmsSource):
    """Feeds already-fetched FIRMS rows into the engine's parse/validate/route path."""

    kind = RecordKind.HISTORICAL_HOTSPOT
    scope = Scope.AOI

    def __init__(self, name: str, lane: Lane, rows: list[tuple[dict[str, str], dict | None]]) -> None:
        super().__init__(name, Settings(), None)
        self.lane = lane
        self.rows = rows

    def available(self) -> tuple[bool, str]:
        return True, "ok"

    def fetch(self, req: FetchRequest) -> Iterator:
        for row, truth in self.rows:
            yield self._raw(row, sim_truth=truth)


# --------------------------------------------------------------------------
# Simulated fire regime
# --------------------------------------------------------------------------

CAL_PASSES: dict[str, list[tuple[str, str, float]]] = {
    "MODIS": [("Terra", "T", 10.5), ("Terra", "T", 22.5), ("Aqua", "A", 13.5), ("Aqua", "A", 1.5)],
    "VIIRS_NOAA20": [("NOAA-20", "N20", 12.67), ("NOAA-20", "N20", 0.67)],
    "VIIRS_SNPP": [("Suomi-NPP", "N", 13.5), ("Suomi-NPP", "N", 1.5)],
}


def _pass_key(source: str) -> str:
    return "MODIS" if source.startswith("MODIS") else "VIIRS_NOAA20" if "NOAA20" in source else "VIIRS_SNPP"


@dataclass(frozen=True)
class SimFire:
    fire_id: str
    lat: float
    lon: float
    start: date
    duration: int
    cells: float        # median active 1-km cells per day
    frp: float          # median FRP per cell (MW)
    anomaly: str | None


class SimFireRegime:
    """Deterministic synthetic fire regime for one area (seeded)."""

    def __init__(self, bbox: BBox, start: date, end: date, seed: int = 1) -> None:
        self.bbox, self.start, self.end, self.seed = bbox, start, end, seed
        rng = self._rng("regime")
        lat = bbox.center[0]
        # Fire-season peak (day of year) by latitude band.
        self.peak_doy = 220 if lat > 23 else 35 if lat > 0 else 250 if lat > -23 else 15
        self.peak_doy += rng.randint(-12, 12)
        self.sigma = rng.uniform(28.0, 40.0)
        self.peak_rate = rng.uniform(6.0, 8.0)          # new fires per day at the season peak
        self.base_rate = 0.08
        self.trend_per_year = rng.choice([0.45, -0.35, 0.3])
        self.zones = [(rng.uniform(bbox.south, bbox.north), rng.uniform(bbox.west, bbox.east)) for _ in range(4)]
        self.flare = (rng.uniform(bbox.south, bbox.north), rng.uniform(bbox.west, bbox.east))
        # Weather: AR(1) log-multiplier, so dry/wet spells last several days.
        self.weather: dict[date, float] = {}
        a, d = 0.0, start - timedelta(days=20)
        wrng = self._rng("weather")
        while d <= end:
            a = 0.8 * a + 0.45 * wrng.gauss(0, 1)
            self.weather[d] = math.exp(a)
            d += timedelta(days=1)
        self.injected = self._plan_anomalies()
        self._fires: dict[date, list[SimFire]] = {}

    def _rng(self, *keys: Any) -> random.Random:
        return random.Random(stable_hash(self.seed, self.bbox.key(), *keys))

    def seasonal(self, d: date) -> float:
        doy = d.timetuple().tm_yday
        diff = min(abs(doy - self.peak_doy), 365 - abs(doy - self.peak_doy))
        return math.exp(-0.5 * (diff / self.sigma) ** 2)

    def _plan_anomalies(self) -> list[dict[str, Any]]:
        days = [self.start + timedelta(days=i) for i in range((self.end - self.start).days + 1)]
        inner = days[20:-20] or days
        if len(inner) < 30:
            return []
        out = []
        # Outbreak spike: near the last season peak in range.
        peak = max(reversed(inner), key=self.seasonal)
        spike = peak + timedelta(days=6)
        if spike in self.weather and spike <= inner[-1]:
            out.append({"type": "spike", "start": spike.isoformat(), "end": (spike + timedelta(days=1)).isoformat()})
        # MODIS outage: a week of high season, away from the spike.
        gap_start = peak - timedelta(days=24)
        if gap_start >= inner[0] and self.seasonal(gap_start) > 0.4:
            out.append({"type": "modis_gap", "start": gap_start.isoformat(),
                        "end": (gap_start + timedelta(days=6)).isoformat()})
        # Off-season burst: deepest part of the off-season.
        low = min(inner, key=self.seasonal)
        if self.seasonal(low) < 0.05:
            out.append({"type": "off_season", "start": low.isoformat(), "end": (low + timedelta(days=2)).isoformat()})
        return out

    def _anomaly_on(self, d: date, kind: str) -> bool:
        s = d.isoformat()
        return any(a["type"] == kind and a["start"] <= s <= a["end"] for a in self.injected)

    def rate(self, d: date) -> float:
        years = (d - self.start).days / 365.25
        trend = min(3.0, max(0.25, (1 + self.trend_per_year) ** years))   # bounded for multi-decade runs
        r = (self.base_rate + self.peak_rate * self.seasonal(d)) * trend
        return r * self.weather.get(d, 1.0)

    def fires_started(self, d: date) -> list[SimFire]:
        if d not in self._fires:
            self._fires[d] = self._make_fires(d)
        return self._fires[d]

    def _make_fires(self, d: date) -> list[SimFire]:
        rng = self._rng("fires", d.isoformat())
        lam = self.rate(d)
        anomaly = None
        if self._anomaly_on(d, "spike"):
            lam, anomaly = lam * 6 + 12, "spike"
        elif self._anomaly_on(d, "off_season"):
            lam, anomaly = 8.0, "off_season"
        n = _poisson(rng, lam)
        out = []
        for k in range(n):
            zlat, zlon = rng.choice(self.zones)
            lat, lon = offset_point(zlat, zlon, rng.gauss(0, 25), rng.gauss(0, 25))
            lat = min(max(lat, self.bbox.south + 0.02), self.bbox.north - 0.02)
            lon = min(max(lon, self.bbox.west + 0.02), self.bbox.east - 0.02)
            big = anomaly == "spike"
            out.append(SimFire(
                fire_id=f"F{d:%Y%m%d}-{k}", lat=lat, lon=lon, start=d,
                duration=1 + min(12, int(rng.expovariate(1 / (3.0 if big else 1.6)))),
                cells=min(40.0, rng.lognormvariate(math.log(6 if big else 2.5), 0.7)),
                frp=rng.lognormvariate(math.log(14 if big else 9), 0.6), anomaly=anomaly))
        return out

    def rows(self, source: str, d: date) -> list[tuple[dict[str, str], dict[str, Any]]]:
        """FIRMS CSV rows a product would return for one (local solar) day."""
        modis = source.startswith("MODIS")
        if modis and self._anomaly_on(d, "modis_gap"):
            return []                       # outage: FIRMS returns an empty CSV, just like "no fire"
        nrt = source.endswith("_NRT")
        version = ("6.1NRT" if nrt else "6.1") if modis else ("2.0NRT" if nrt else "2")
        passes = CAL_PASSES[_pass_key(source)]
        out = []
        active = [f for k in range(13) for f in self.fires_started(d - timedelta(days=k))
                  if f.start + timedelta(days=f.duration) > d]
        for f in active:
            age = (d - f.start).days
            frng = self._rng("cells", f.fire_id, d.isoformat())
            n_cells = max(1, int(round(f.cells * frng.uniform(0.5, 1.5) * (1 + 0.3 * age) / (1 + 0.15 * age ** 2))))
            cells = [(offset_point(f.lat, f.lon, frng.gauss(0, 0.8 + 0.5 * age), frng.gauss(0, 0.8 + 0.5 * age)),
                      frng.lognormvariate(math.log(f.frp), 0.8)) for _ in range(n_cells)]
            for platform, sat, local_h in passes:
                prng = self._rng("pass", f.fire_id, d.isoformat(), source, local_h)
                if prng.random() < 0.22:          # cloud / smoke over this fire on this pass
                    continue
                t = _local_time(d, local_h, f.lon, prng)
                night = _daynight(f.lat, f.lon, t) == "N"
                for (clat, clon), cfrp in cells:
                    frp = cfrp * (0.45 if night else 1.0)
                    p_detect = 1 - math.exp(-frp / (22.0 if modis else 3.0))
                    if prng.random() > p_detect:
                        continue
                    if modis:
                        pix = [(clat + prng.gauss(0, 0.004), clon + prng.gauss(0, 0.004), frp)]
                    else:
                        k = 1 + int(prng.random() * 3)
                        pix = [(clat + prng.uniform(-0.004, 0.004), clon + prng.uniform(-0.004, 0.004), frp / k)
                               for _ in range(k)]
                    for plat, plon, pfrp in pix:
                        conf = "low" if prng.random() < 0.05 else "high" if pfrp > 20 else "nominal"
                        row = firms_row(source, sat, t, plat, plon, pfrp, conf, prng,
                                        "N" if night else "D", version)
                        out.append((row, {"scenario": "calendar", "fire_id": f.fire_id, "anomaly": f.anomaly}))
        # Persistent gas flare: small, hot, mostly at night; VIIRS sees it, MODIS rarely.
        flat, flon = self.flare
        for platform, sat, local_h in passes:
            prng = self._rng("flare", d.isoformat(), source, local_h)
            t = _local_time(d, local_h, flon, prng)
            night = _daynight(flat, flon, t) == "N"
            p = (0.12 if night else 0.03) if modis else (0.8 if night else 0.35)
            if prng.random() < p:
                row = firms_row(source, sat, t, flat + prng.uniform(-0.002, 0.002), flon + prng.uniform(-0.002, 0.002),
                                prng.uniform(2.0, 6.0), "nominal", prng, "N" if night else "D", version)
                out.append((row, {"scenario": "calendar", "site": "gas_flare", "anomaly": None}))
        return out


def _poisson(rng: random.Random, lam: float) -> int:
    if lam > 30:
        return max(0, int(round(rng.gauss(lam, math.sqrt(lam)))))
    k, p, limit = 0, 1.0, math.exp(-lam)
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def _local_time(d: date, local_h: float, lon: float, rng: random.Random) -> datetime:
    """UTC time of a pass at ``local_h`` local solar time on local date ``d``."""
    utc = datetime.combine(d, time(0), tzinfo=timezone.utc) + timedelta(hours=local_h - lon / 15 + rng.uniform(-0.3, 0.3))
    return utc.replace(second=0, microsecond=0)


# --------------------------------------------------------------------------
# Job
# --------------------------------------------------------------------------

Progress = Callable[[str, float], None]


def run_backfill(engine: IngestionEngine, mode: Mode | str, bbox: BBox, start: date, end: date, *,
                 seed: int = 1, progress: Progress | None = None, refresh: bool = False,
                 availability: dict[str, tuple[date, date]] | None = None,
                 families: tuple[str, ...] = tuple(FAMILY_SOURCES)) -> dict[str, Any]:
    mode = Mode(mode)
    lane = mode.lane
    s = engine.settings
    store = engine.store
    today = utcnow().date()
    if start > end:
        raise ValueError("start must not be after end")
    if end > today:
        raise ValueError("end can't be in the future")
    if start < MODIS_START:
        raise ValueError(f"the MODIS archive starts on {MODIS_START.isoformat()}")
    progress = progress or (lambda msg, frac: None)

    regime = None
    http = None
    if mode is Mode.SIMULATED:
        regime = SimFireRegime(bbox, start, end, seed)
        availability = availability or default_availability(today)
    else:
        if not s.has_firms:
            raise ValueError("FIRMS_MAP_KEY is not set")
        http = engine.http
        if availability is None:
            try:
                availability = fetch_availability(http, s.firms_map_key)
            except SourceError as exc:
                log.warning("FIRMS availability lookup failed (%s); using defaults", exc)
                availability = default_availability(today)

    now = utcnow()
    run_id = f"cal_{now:%Y%m%dT%H%M%S}_{stable_hash(mode.value, bbox.key(), start, end, seed, now, length=6)}"
    params: dict[str, Any] = {"kind": "calendar_backfill", "aoi": bbox.as_list(), "start": start.isoformat(),
                              "end": end.isoformat(), "seed": seed if regime else None}
    if regime:
        params["injected"] = regime.injected
        params["trend_per_year"] = regime.trend_per_year
    store.start_run(run_id, f"calendar_{mode.value}", params)
    report = RunReport(run_id, f"calendar_{mode.value}", bbox.key(), datetime.combine(start, time(0), timezone.utc),
                       datetime.combine(end, time(23, 59), timezone.utc))

    # FIRMS selects by UTC date, the calendar by local solar date: one extra UTC day on each side
    # makes the first and last local days complete (chunks in between partition the UTC days).
    fetch_start, fetch_end = start - timedelta(days=1), min(end + timedelta(days=1), today)
    todo: list[Chunk] = []
    skipped = 0
    for family in families:
        chunks, uncovered = plan(family, fetch_start, fetch_end, availability)
        if uncovered:
            store.mark_coverage(lane, family, bbox, uncovered, "unavailable", None, run_id)
        for c in chunks:
            last = store.last_fetch(lane, c.source, f"cal|{bbox.key()}|{c.day}|{c.span}")
            settled = last and last.date() >= c.last + timedelta(days=NRT_SETTLE_DAYS)
            if settled and not refresh:
                skipped += 1
                continue
            todo.append(c)

    def fetch(c: Chunk) -> list[tuple[dict[str, str], dict | None]]:
        if regime:
            cutoff = now.strftime("%Y-%m-%d%H%M")   # no simulated passes later than "now"
            return [rt for d in c.days for rt in regime.rows(c.source, d)
                    if rt[0]["acq_date"] + rt[0]["acq_time"] <= cutoff]
        url = f"{FIRMS_AREA_URL}/{s.firms_map_key}/{c.source}/{bbox.as_firms()}/{c.span}/{c.day.isoformat()}"
        for attempt in range(3):
            quota.acquire()
            try:
                return [(r, None) for r in parse_firms_csv(http.get_text(url))]
            except SourceError as exc:
                # FIRMS answers an exhausted quota with a 200 and a plain-text message.
                if "xceed" not in str(exc) or attempt == 2:
                    raise
                quota.backoff()
        return []

    quota = FirmsQuota(http, s.firms_map_key, progress) if http else None

    ctx = ValidationContext(replace(s, quarantine_low_confidence=False), now, check_window=False)
    done = 0
    progress(f"{len(todo)} chunks to fetch ({skipped} already up to date)", 0.0)

    def handle(c: Chunk, rows: list | None, error: str | None) -> None:
        nonlocal done
        done += 1
        stats = report.sources.setdefault(c.source, SourceStats())
        if error is not None:
            stats.status, stats.detail = "error", error[:300]
            store.mark_coverage(lane, c.family, bbox, c.days, "error", c.source, run_id)
        else:
            src = _RowsSource(c.source, lane, rows or [])
            seen: set[str] = set()   # duplicates within one response are rejected; across chunks they dedupe
            for raw in src.fetch(FetchRequest(bbox, now, now, now)):
                engine._process(raw, lane, ctx, stats, seen, run_id)
            if stats.status != "error":
                stats.status = "ok"
            store.mark_coverage(lane, c.family, bbox, c.days, "ok", c.source, run_id)
            store.mark_fetched(lane, c.source, f"cal|{bbox.key()}|{c.day}|{c.span}")
        progress(f"{c.source} {c.day.isoformat()} ({done}/{len(todo)} chunks)", done / max(1, len(todo)))

    if regime:
        for c in todo:
            handle(c, fetch(c), None)
    else:
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
            futures = [(c, pool.submit(fetch, c)) for c in todo]
            for c, fut in futures:
                try:
                    handle(c, fut.result(), None)
                except SourceError as exc:
                    handle(c, None, str(exc))
                except Exception as exc:  # one bad chunk must not abort the backfill
                    log.exception("calendar chunk %s %s failed", c.source, c.day)
                    handle(c, None, f"{type(exc).__name__}: {exc}")

    totals = report.totals()
    store.finish_run(run_id, {**totals, "chunks": len(todo), "chunks_skipped": skipped},
                     {k: v.as_dict() for k, v in report.sources.items()})
    return {"run_id": run_id, "chunks": len(todo), "chunks_skipped": skipped, "totals": totals,
            "sources": {k: v.as_dict() for k, v in report.sources.items()},
            "injected": regime.injected if regime else None}
