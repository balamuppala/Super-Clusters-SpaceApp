"""Local dashboard: a small JSON API over the SQLite store plus a static single-page UI.

Standard library only (http.server). Binds to 127.0.0.1 by default. Every
request opens its own Store, because SQLite connections are per-thread.
State-changing requests (POST) must carry ``X-Requested-With: fireverify``
and a same-origin ``Origin`` header, so other websites can't trigger them.
When ``FTD_ADMIN_TOKEN`` is set, POST also requires that password
(``Authorization: Bearer …``, HTTP Basic, or ``X-Admin-Token``). GET stays public.
The same-origin check compares Origin's host to the request Host header, so it
already allows nasa-harmonized-hotspots.nimblesoft.ch and *.onrender.com when
the visitor is on that host.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import math
import mimetypes
import os
import re
import time
import threading
import traceback
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from fireverify.calendar import analyze, day_detail, harmonize, points_from_rows
from fireverify.calendar.actions import recommend, recommend_country, recommend_longterm
from fireverify.calendar.country import CountryArchive, area_vs_national
from fireverify.calendar.longterm import analyze_longterm
from fireverify.calendar.backfill import MAX_CALENDAR_AREA_DEG2, MAX_CALENDAR_DAYS, run_backfill, sampled_years
from fireverify.calendar.trends import score_injected
from fireverify.config import Settings
from fireverify.ingestion.engine import IngestionEngine
from fireverify.ingestion.http import HttpClient
from fireverify.ingestion.simulated import FIRE_PRONE_REGIONS, SCENARIOS, batch_aoi
from fireverify.ingestion.sources.firms import FIRMS_AREA_URL, parse_firms_csv
from fireverify.ingestion.store import Store
from fireverify.models import BBox, Lane, Mode, utcnow
from fireverify.verification import LikelihoodTable, Verifier

log = logging.getLogger(__name__)

STATIC_DIR = Path(str(resources.files("fireverify") / "dashboard" / "static"))
MAX_LIVE_AREA_DEG2 = 100.0
MAX_SIM_RUNS = 20
ACTIVE_CELL_DEG = 2.0
ACTIVE_CACHE_S = 1800


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Job:
    job_id: str
    kind: str
    params: dict[str, Any]
    status: str = "running"
    message: str = "starting"
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    result: dict[str, Any] | None = None
    progress: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"job_id": self.job_id, "kind": self.kind, "params": self.params, "status": self.status,
                "message": self.message, "progress": self.progress, "started_at": self.started_at.isoformat(),
                "finished_at": self.finished_at.isoformat() if self.finished_at else None, "result": self.result}


def _lane(query: dict[str, list[str]]) -> Lane:
    value = (query.get("lane") or ["simulated"])[0]
    try:
        return Mode(value).lane
    except ValueError as exc:
        raise ApiError(400, f"lane must be 'live' or 'simulated', got {value!r}") from exc


def _first(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    return values[0] if values else None


def _bbox_arg(value: Any) -> BBox:
    try:
        parts = value.split(",") if isinstance(value, str) else list(value)
        return BBox(*[float(v) for v in parts])
    except (AttributeError, TypeError, ValueError) as exc:
        raise ApiError(400, f"bbox must be west,south,east,north: {exc}") from exc


def _date_arg(value: Any, name: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ApiError(400, f"{name} must be YYYY-MM-DD, got {value!r}") from exc


def _calendar_range(start: date, end: date) -> None:
    if start > end:
        raise ApiError(400, "start must not be after end")
    if (end - start).days + 1 > MAX_CALENDAR_DAYS:
        raise ApiError(400, f"at most {MAX_CALENDAR_DAYS} days")


def _slim_day(d: dict[str, Any]) -> dict[str, Any]:
    cov = d["coverage"]
    return {k: d[k] for k in ("day", "modis_pixels", "viirs_pixels", "modis_cells", "viirs_cells", "modis_confirmed",
                              "viirs_confirmed", "harmonized_cells", "frp_mw", "modis_frp_mw", "viirs_frp_mw",
                              "complete", "has_data", "estimate", "estimated")} | {
        "modis": cov.get("MODIS"), "viirs": cov.get("VIIRS")}


class DashboardApp:
    def __init__(self, settings: Settings, db_path: str | Path) -> None:
        self.settings = settings
        self.db_path = str(db_path)
        self.jobs: dict[str, Job] = {}
        self._job_lock = threading.Lock()
        self._active_cache: tuple[float, list[dict[str, Any]]] | None = None
        self._longterm_cache: dict[tuple, dict[str, Any]] = {}
        self.country_archive = CountryArchive(settings.country_archive_dir)
        Store(self.db_path).close()  # create schema up front

    # ------------------------------------------------------------ helpers

    def _store(self) -> Store:
        return Store(self.db_path)

    def _verifier(self, store: Store) -> Verifier:
        return Verifier(store, LikelihoodTable.load_or_default(self.settings.likelihoods_path))

    def _ensure_verdicts(self, store: Store, lane: Lane) -> None:
        """Score clusters that have derived features but no verdict yet."""
        have = {v["cluster_id"] for v in store.latest_verdicts(lane, limit=100_000)}
        missing = [c["cluster_id"] for c in store.list_clusters(lane, limit=100_000) if c["cluster_id"] not in have]
        if missing:
            verifier = self._verifier(store)
            for cid in missing:
                verifier.verify_cluster(lane, cid)

    # ---------------------------------------------------------- read API

    def summary(self, lane: Lane) -> dict[str, Any]:
        store = self._store()
        try:
            self._ensure_verdicts(store, lane)
            verdicts = store.latest_verdicts(lane, limit=100_000)
            quarantine = store.list_quarantine(status="pending", lane=lane, limit=100_000)
            rejected = store.list_rejected(lane=lane, limit=100_000)
            runs = [r for r in store.list_runs(limit=200) if r["mode"] == lane.value]
            counts = store.counts()
            table = LikelihoodTable.load_or_default(self.settings.likelihoods_path)
            return {
                "lane": lane.value,
                "events": len(verdicts),
                "labels": dict(Counter(v["label"] for v in verdicts)),
                "causes": dict(Counter(v["attribution"] for v in verdicts if v["attribution"])),
                "quarantine_pending": len(quarantine),
                "rejected": len(rejected),
                "quarantine_reasons": dict(Counter(code for q in quarantine for code in q["reasons"])),
                "rejected_reasons": dict(Counter(code for r in rejected for code in r["reasons"])),
                "records": counts.get(lane.value, {}),
                "last_run": runs[0] if runs else None,
                "runs": len(runs),
                "likelihood_table": {"version": table.version, "hash": table.hash, "meta": table.data.get("meta", {})},
                "credentials": {"firms": self.settings.has_firms, "cdse": self.settings.has_cdse},
                "busy": any(j.status == "running" for j in self.jobs.values()),
            }
        finally:
            store.close()

    def events(self, lane: Lane) -> list[dict[str, Any]]:
        store = self._store()
        try:
            self._ensure_verdicts(store, lane)
            verdicts = {v["cluster_id"]: v for v in store.latest_verdicts(lane, limit=100_000)}
            out = []
            for c in store.list_clusters(lane, limit=100_000):
                v = verdicts.get(c["cluster_id"])
                if v is None:
                    continue
                out.append({
                    "cluster_id": c["cluster_id"], "lat": c["centroid_lat"], "lon": c["centroid_lon"],
                    "probability": v["probability"], "label": v["label"], "attribution": v["attribution"],
                    "n_detections": c["n_detections"], "n_overpasses": c["n_overpasses"], "sensors": c["sensors"],
                    "max_frp_mw": c["max_frp_mw"], "first_seen": c["first_seen"], "last_seen": c["last_seen"],
                    "radius_km": c["radius_km"], "evidence_observed": len(v["posterior"]["contributions"]),
                })
            return sorted(out, key=lambda e: e["last_seen"], reverse=True)
        finally:
            store.close()

    def event(self, lane: Lane, cluster_id: str) -> dict[str, Any]:
        store = self._store()
        try:
            verdict = self._verifier(store).verify_cluster(lane, cluster_id)
            if verdict is None:
                raise ApiError(404, f"no event {cluster_id}")
            engine = IngestionEngine(self.settings, store)
            bundle = engine.evidence(lane, cluster_id) or {}
            truth = Counter(d["sim_truth"]["label"] for d in bundle.get("detections", []) if d.get("sim_truth"))
            return {
                "verdict": verdict.as_dict(),
                "derived": bundle.get("derived", {}),
                "detections": [{
                    "record_id": d["record_id"], "lat": d["lat"], "lon": d["lon"], "observed_at": d["observed_at"],
                    "platform": d["data"]["platform"], "sensor": d["data"]["sensor"], "frp_mw": d["data"].get("frp_mw"),
                    "confidence": d["data"]["confidence_class"], "daynight": d["data"].get("daynight"),
                    "brightness_k": d["data"]["brightness_k"], "flags": d["flags"],
                } for d in bundle.get("detections", [])],
                "enrichment": [{"record_id": e["record_id"], "kind": e["kind"], "source": e["source"],
                                "observed_at": e["observed_at"], "flags": e["flags"], "data": e["data"]}
                               for e in bundle.get("enrichment", [])],
                "sim_truth": truth.most_common(1)[0][0] if truth else None,
            }
        finally:
            store.close()

    def quarantine(self, lane: Lane, status: str) -> list[dict[str, Any]]:
        store = self._store()
        try:
            rows = store.list_quarantine(status=None if status == "all" else status, lane=lane, limit=500)
            return [{k: r[k] for k in ("qid", "source", "kind", "reasons", "details", "status", "created_at",
                                       "resolved_at", "resolution_note", "target_id")}
                    | {"payload": r["raw_payload"], "sim_truth": r.get("sim_truth")} for r in rows]
        finally:
            store.close()

    def rejected(self, lane: Lane) -> list[dict[str, Any]]:
        store = self._store()
        try:
            return [{k: r[k] for k in ("rid", "source", "kind", "reasons", "details", "rejected_at", "target_id")}
                    | {"payload": r["raw_payload"], "sim_truth": r.get("sim_truth")}
                    for r in store.list_rejected(lane=lane, limit=500)]
        finally:
            store.close()

    def runs(self) -> list[dict[str, Any]]:
        store = self._store()
        try:
            return store.list_runs(limit=50)
        finally:
            store.close()

    def sources(self) -> dict[str, Any]:
        store = self._store()
        try:
            avail = IngestionEngine(self.settings, store).source_availability()
            return {name: {"available": ok, "detail": why} for name, (ok, why) in avail.items()}
        finally:
            store.close()

    def regions(self) -> list[dict[str, Any]]:
        """Fire-prone regions as 4x4 degree live-run presets."""
        out = []
        for name, south, west in FIRE_PRONE_REGIONS:
            lat, lon = south + 1, west + 1
            out.append({"name": name, "bbox": [round(lon - 2, 2), round(lat - 2, 2), round(lon + 2, 2), round(lat + 2, 2)]})
        return sorted(out, key=lambda r: r["name"])

    def active_areas(self, limit: int = 10) -> list[dict[str, Any]]:
        """Busiest 2x2 degree cells in the last ~24 h of VIIRS NOAA-20 detections (one FIRMS request, cached)."""
        if not self.settings.has_firms:
            raise ApiError(400, "FIRMS_MAP_KEY is not set - needed to find active fire areas")
        if self._active_cache and time.time() - self._active_cache[0] < ACTIVE_CACHE_S:
            return self._active_cache[1][:limit]
        http = HttpClient(60, self.settings.user_agent, 1, secrets=[self.settings.firms_map_key])
        try:
            start = (utcnow() - timedelta(days=1)).date().isoformat()
            rows = parse_firms_csv(http.get_text(
                f"{FIRMS_AREA_URL}/{self.settings.firms_map_key}/VIIRS_NOAA20_NRT/world/2/{start}"))
        except Exception as exc:
            raise ApiError(502, f"FIRMS request failed: {http.redact(str(exc))[:200]}") from exc
        finally:
            http.close()
        cells: Counter = Counter()
        frp: Counter = Counter()
        for r in rows:
            if str(r.get("confidence", "")).strip().lower() in ("l", "low"):
                continue
            try:
                lat, lon = float(r["latitude"]), float(r["longitude"])
            except (KeyError, ValueError):
                continue
            key = (math.floor(lat / ACTIVE_CELL_DEG), math.floor(lon / ACTIVE_CELL_DEG))
            cells[key] += 1
            try:
                frp[key] += float(r.get("frp") or 0)
            except ValueError:
                pass
        areas = []
        for (gy, gx), n in cells.most_common(50):
            s, w = gy * ACTIVE_CELL_DEG, gx * ACTIVE_CELL_DEG
            areas.append({"bbox": [w, s, w + ACTIVE_CELL_DEG, s + ACTIVE_CELL_DEG], "detections": n,
                          "total_frp_mw": round(frp[(gy, gx)]), "center": [s + ACTIVE_CELL_DEG / 2, w + ACTIVE_CELL_DEG / 2]})
        self._active_cache = (time.time(), areas)
        return areas[:limit]

    # ---------------------------------------------------------- calendar

    def _points(self, store: Store, lane: Lane, bbox: BBox, start: date, end: date):
        # Local solar dates reach up to +/-12 h either side of UTC.
        lo = datetime.combine(start - timedelta(days=1), dtime(0), timezone.utc)
        hi = datetime.combine(end + timedelta(days=2), dtime(0), timezone.utc)
        return points_from_rows(store.hotspot_points(lane, bbox, lo, hi))

    def _injected(self, store: Store, lane: Lane, bbox: BBox) -> list[dict[str, Any]] | None:
        if lane is not Lane.SIMULATED:
            return None
        seen, out = set(), []
        for r in store.list_runs(limit=500):
            if r["mode"] != "calendar_simulated" or r["params"].get("aoi") != bbox.as_list():
                continue
            for a in r["params"].get("injected") or []:
                key = (a["type"], a["start"])
                if key not in seen:
                    seen.add(key)
                    out.append(a)
        return sorted(out, key=lambda a: a["start"])

    def calendar(self, lane: Lane, query: dict[str, list[str]]) -> dict[str, Any]:
        bbox = _bbox_arg(_first(query, "bbox"))
        end = _date_arg(_first(query, "end") or (utcnow().date() - timedelta(days=1)).isoformat(), "end")
        start = _date_arg(_first(query, "start") or (end - timedelta(days=364)).isoformat(), "start")
        _calendar_range(start, end)
        include_low = _first(query, "low") in ("1", "true")
        store = self._store()
        try:
            points = self._points(store, lane, bbox, start, end)
            coverage = store.coverage_for(lane, bbox, start, end)
            h = harmonize(points, start, end, coverage, include_low=include_low)
            result = analyze(h.days, h.calibration)
            result["actions"] = recommend(result, utcnow().date())
            injected = self._injected(store, lane, bbox)
            if injected is not None:
                injected = score_injected([a for a in injected if start.isoformat() <= a["start"] <= end.isoformat()],
                                          result["anomalies"])
            return {
                "lane": lane.value, "bbox": bbox.as_list(), "start": h.start, "end": h.end,
                "include_low": include_low, "grid_deg": h.as_dict()["grid_deg"],
                "calibration": h.calibration, "analysis": result, "injected": injected,
                "days": [_slim_day(d.as_dict()) for d in h.days],
                "fetched_days": sum(1 for fam in coverage.values() for st in fam.values() if st == "ok"),
            }
        finally:
            store.close()

    def calendar_longterm(self, lane: Lane, query: dict[str, list[str]]) -> dict[str, Any]:
        """Annual/monthly record back to 2000 for an area. Cached until the lane's data changes."""
        bbox = _bbox_arg(_first(query, "bbox"))
        include_low = _first(query, "low") in ("1", "true")
        store = self._store()
        try:
            table = "live_records" if lane is Lane.LIVE else "simulated_records"
            sig = tuple(store.conn.execute(f"SELECT COUNT(*), MAX(ingested_at) FROM {table}").fetchone()) + tuple(
                store.conn.execute("SELECT COUNT(*), MAX(updated_at) FROM calendar_coverage WHERE lane=?",
                                   (lane.value,)).fetchone())
            key = (lane.value, bbox.key(), include_low, sig)
            with self._job_lock:
                hit = self._longterm_cache.get(key)
            if hit:
                return hit
            lt = analyze_longterm(store, lane, bbox, include_low, utcnow().date())
            if lt["status"] == "ok":
                lt["actions"] = recommend_longterm(lt, utcnow().date())
            with self._job_lock:
                if len(self._longterm_cache) > 16:
                    self._longterm_cache.clear()
                self._longterm_cache[key] = lt
            return lt
        finally:
            store.close()

    def country_list(self) -> dict[str, Any]:
        arch = self.country_archive
        if not arch.available:
            return {"available": False, "countries": [], "path": str(arch.path)}
        return {"available": True, "countries": arch.countries()}

    def country_profile(self, lane: Lane, query: dict[str, list[str]]) -> dict[str, Any]:
        """National context for a country (or the country guessed from ``bbox``), with area comparison and actions."""
        arch = self.country_archive
        if not arch.available:
            return {"status": "missing", "path": str(arch.path)}
        bbox_text = _first(query, "bbox")
        bbox = _bbox_arg(bbox_text) if bbox_text else None
        guess = arch.guess(bbox.as_list()) if bbox else None
        name = _first(query, "name") or guess
        if not name:
            return {"status": "no_country", "guess": None}
        prof = arch.profile(name)
        if prof is None:
            raise ApiError(404, f"no country {name!r} in the archive")
        prof["guess"] = guess
        prof["actions"] = recommend_country(prof, utcnow().date())
        if bbox:
            # The area's month-of-year distribution, from its stored long-term record (cached).
            try:
                lt = self.calendar_longterm(lane, {"bbox": [bbox_text]})
            except ApiError:
                lt = {}
            if lt.get("status") == "ok":
                monthly: dict[int, float] = defaultdict(float)
                for m in lt["months"]:
                    monthly[m["month"]] += m["harmonized"]
                prof["area_vs_national"] = area_vs_national(prof, monthly)
        return prof

    def calendar_day(self, lane: Lane, query: dict[str, list[str]]) -> dict[str, Any]:
        bbox = _bbox_arg(_first(query, "bbox"))
        day = _date_arg(_first(query, "date"), "date")
        include_low = _first(query, "low") in ("1", "true")
        store = self._store()
        try:
            return day_detail(self._points(store, lane, bbox, day, day), day, include_low)
        finally:
            store.close()

    def calendar_areas(self, lane: Lane) -> list[dict[str, Any]]:
        store = self._store()
        try:
            return store.calendar_areas(lane)
        finally:
            store.close()

    def start_calendar_job(self, body: dict[str, Any]) -> dict[str, Any]:
        mode = body.get("mode", "simulated")
        if mode not in ("live", "simulated"):
            raise ApiError(400, "mode must be 'live' or 'simulated'")
        if mode == "live" and not self.settings.has_firms:
            raise ApiError(400, "FIRMS_MAP_KEY is not set - fetching the MODIS/VIIRS archive needs it")
        bbox = _bbox_arg(body.get("bbox"))
        if bbox.area_deg2 > MAX_CALENDAR_AREA_DEG2 + 1e-9:
            raise ApiError(400, f"area {bbox.area_deg2:.1f} deg² is too large for a calendar; "
                                f"use <= {MAX_CALENDAR_AREA_DEG2:.0f} deg² (5° × 5°)")
        start, end = _date_arg(body.get("start"), "start"), _date_arg(body.get("end"), "end")
        if start > end:
            raise ApiError(400, "start must not be after end")
        if end > utcnow().date():
            raise ApiError(400, "end can't be in the future")
        if start < date(2000, 11, 1):
            raise ApiError(400, "the MODIS archive starts on 2000-11-01")
        params = {"mode": mode, "bbox": bbox.as_list(), "start": start.isoformat(), "end": end.isoformat(),
                  "seed": int(body.get("seed", 1)), "refresh": bool(body.get("refresh", False))}
        if body.get("sample_years"):
            params["years"] = [[y, list(f)] for y, f in sampled_years(utcnow().date())]
        return self._launch(Job(uuid.uuid4().hex[:12], "calendar", params))

    def _launch(self, job: Job) -> dict[str, Any]:
        with self._job_lock:
            if any(j.status == "running" for j in self.jobs.values()):
                raise ApiError(409, "a job is already running")
            self.jobs[job.job_id] = job
        threading.Thread(target=self._run_job, args=(job,), daemon=True).start()
        return job.as_dict()

    # --------------------------------------------------------- write API

    def resolve_quarantine(self, qid: str, action: str, note: str | None) -> dict[str, Any]:
        store = self._store()
        try:
            engine = IngestionEngine(self.settings, store)
            try:
                if action == "release":
                    outcome = engine.release_quarantined(qid, note)
                else:
                    engine.reject_quarantined(qid, note)
                    outcome = "rejected"
            except KeyError as exc:
                raise ApiError(404, str(exc)) from exc
            except ValueError as exc:
                raise ApiError(409, str(exc)) from exc
            return {"qid": qid, "outcome": outcome}
        finally:
            store.close()

    def start_job(self, body: dict[str, Any]) -> dict[str, Any]:
        mode = body.get("mode", "simulated")
        if mode not in ("live", "simulated"):
            raise ApiError(400, "mode must be 'live' or 'simulated'")
        days = float(body.get("days", 2.0))
        if not 0.1 <= days <= 10:
            raise ApiError(400, "days must be between 0.1 and 10")
        params: dict[str, Any] = {"mode": mode, "days": days}
        if mode == "live":
            if not self.settings.has_firms:
                raise ApiError(400, "FIRMS_MAP_KEY is not set - live ingestion needs it (see .env.example)")
            try:
                bbox = BBox(*[float(v) for v in body["bbox"]])
            except (KeyError, TypeError, ValueError) as exc:
                raise ApiError(400, f"bbox must be [west, south, east, north]: {exc}") from exc
            if bbox.area_deg2 > MAX_LIVE_AREA_DEG2:
                raise ApiError(400, f"area {bbox.area_deg2:.0f} deg² is too large; zoom in to <= {MAX_LIVE_AREA_DEG2:.0f} deg²")
            params["bbox"] = bbox.as_list()
        else:
            runs = int(body.get("runs", 1))
            if not 1 <= runs <= MAX_SIM_RUNS:
                raise ApiError(400, f"runs must be between 1 and {MAX_SIM_RUNS}")
            scenarios = body.get("scenarios") or None
            if scenarios and set(scenarios) - set(SCENARIOS):
                raise ApiError(400, f"unknown scenarios {sorted(set(scenarios) - set(SCENARIOS))}")
            params |= {"runs": runs, "seed": int(body.get("seed", 1)), "scenarios": scenarios}
        return self._launch(Job(uuid.uuid4().hex[:12], "ingest", params))

    def _run_calendar(self, job: Job, engine: IngestionEngine) -> None:
        p = job.params

        def progress(msg: str, frac: float) -> None:
            job.message = msg
            if frac >= 0:            # negative: status message only (e.g. pausing for the FIRMS quota)
                job.progress = round(frac, 3)

        if p.get("years"):
            totals, chunks, skipped, errors = Counter(), 0, 0, {}
            n = len(p["years"])
            for k, (year, fams) in enumerate(p["years"]):
                def yp(msg: str, frac: float, k=k, year=year) -> None:
                    progress(f"year {year} ({k + 1}/{n}): {msg}", (k + max(frac, 0)) / n if frac >= 0 else -1)
                o = run_backfill(engine, p["mode"], BBox(*p["bbox"]), date(year, 1, 1),
                                 min(date(year, 12, 31), utcnow().date() - timedelta(days=1)),
                                 seed=p["seed"], refresh=p["refresh"], progress=yp, families=tuple(fams))
                totals.update(o["totals"])
                chunks += o["chunks"]
                skipped += o["chunks_skipped"]
                errors.update({f"{name} {year}": st["detail"][:200] for name, st in o["sources"].items()
                               if st["status"] == "error"})
            job.result = {"chunks": chunks, "chunks_skipped": skipped, "totals": dict(totals),
                          "source_errors": errors, "years": [y for y, _ in p["years"]]}
            return
        out = run_backfill(engine, p["mode"], BBox(*p["bbox"]), date.fromisoformat(p["start"]),
                           date.fromisoformat(p["end"]), seed=p["seed"], refresh=p["refresh"], progress=progress)
        job.result = {
            "chunks": out["chunks"], "chunks_skipped": out["chunks_skipped"], "totals": out["totals"],
            "source_errors": {name: st["detail"][:200] for name, st in out["sources"].items() if st["status"] == "error"},
        }

    def _run_job(self, job: Job) -> None:
        store = self._store()
        try:
            engine = IngestionEngine(self.settings, store)
            p = job.params
            if job.kind == "calendar":
                self._run_calendar(job, engine)
                job.status, job.message, job.progress = "done", "finished", 1.0
                return
            reports = []
            if p["mode"] == "live":
                job.message = "fetching live detections and enrichment"
                reports.append(engine.run(Mode.LIVE, BBox(*p["bbox"]), days=p["days"]))
            else:
                for i in range(p["runs"]):
                    seed = p["seed"] + i
                    job.message = f"simulated run {i + 1} of {p['runs']} (seed {seed})"
                    reports.append(engine.run(Mode.SIMULATED, batch_aoi(seed), days=p["days"], seed=seed,
                                              scenarios=p["scenarios"]))
            job.message = "scoring events"
            lane = Mode(p["mode"]).lane
            verdicts = self._verifier(store).verify_all(lane)
            totals = Counter()
            for r in reports:
                totals.update(r.totals())
            job.result = {
                "runs": len(reports), "totals": dict(totals),
                "clusters_enriched": sum(len(r.clusters_enriched) for r in reports),
                "events_scored": len(verdicts),
                "source_errors": {name: st.detail[:200] for r in reports for name, st in r.sources.items()
                                  if st.status == "error"},
            }
            job.status, job.message = "done", "finished"
        except Exception as exc:
            log.exception("job %s failed", job.job_id)
            job.status, job.message = "error", f"{type(exc).__name__}: {exc}"
        finally:
            job.finished_at = utcnow()
            store.close()

    def job(self, job_id: str) -> dict[str, Any]:
        if job_id not in self.jobs:
            raise ApiError(404, f"no job {job_id}")
        return self.jobs[job_id].as_dict()

    def latest_job(self) -> dict[str, Any] | None:
        if not self.jobs:
            return None
        return max(self.jobs.values(), key=lambda j: j.started_at).as_dict()


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------

Route = tuple[str, re.Pattern, Callable[..., Any]]



def _normalize_host(value: str) -> str:
    """Hostname (+ non-default port) from a Host header or Origin URL."""
    raw = (value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else f"http://{raw}")
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if port in (80, 443, None):
        return host
    return f"{host}:{port}"


def _configured_admin_token(override: str | None = None) -> str:
    if override is not None:
        return override.strip()
    return (os.environ.get("FTD_ADMIN_TOKEN") or "").strip()


def _presented_admin_token(handler: BaseHTTPRequestHandler) -> str | None:
    header = handler.headers.get("X-Admin-Token")
    if header and header.strip():
        return header.strip()
    auth = handler.headers.get("Authorization") or ""
    scheme, _, rest = auth.partition(" ")
    if scheme.lower() == "bearer" and rest.strip():
        return rest.strip()
    if scheme.lower() == "basic" and rest.strip():
        try:
            decoded = base64.b64decode(rest.strip(), validate=True).decode()
        except (ValueError, UnicodeDecodeError):
            return None
        user, _, password = decoded.partition(":")
        return password or user
    return None


def _check_admin(handler: BaseHTTPRequestHandler, expected: str) -> None:
    if not expected:
        return
    presented = _presented_admin_token(handler)
    if presented is None or not hmac.compare_digest(presented, expected):
        raise ApiError(401, "admin password required")


def make_handler(app: DashboardApp, admin_token: str | None = None) -> type[BaseHTTPRequestHandler]:
    expected_token = _configured_admin_token(admin_token)

    def body_of(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
        length = int(handler.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            raise ApiError(413, "request body too large")
        if not length:
            return {}
        try:
            data = json.loads(handler.rfile.read(length))
        except ValueError as exc:
            raise ApiError(400, "body must be JSON") from exc
        if not isinstance(data, dict):
            raise ApiError(400, "body must be a JSON object")
        return data

    routes: list[Route] = [
        ("GET", re.compile(r"^/api/summary$"), lambda h, q, m: app.summary(_lane(q))),
        ("GET", re.compile(r"^/api/events$"), lambda h, q, m: app.events(_lane(q))),
        ("GET", re.compile(r"^/api/events/(?P<cid>evt_[0-9a-f]+)$"), lambda h, q, m: app.event(_lane(q), m["cid"])),
        ("GET", re.compile(r"^/api/quarantine$"),
         lambda h, q, m: app.quarantine(_lane(q), (q.get("status") or ["pending"])[0])),
        ("GET", re.compile(r"^/api/rejected$"), lambda h, q, m: app.rejected(_lane(q))),
        ("GET", re.compile(r"^/api/runs$"), lambda h, q, m: app.runs()),
        ("GET", re.compile(r"^/api/sources$"), lambda h, q, m: app.sources()),
        ("GET", re.compile(r"^/api/regions$"), lambda h, q, m: app.regions()),
        ("GET", re.compile(r"^/api/active-areas$"), lambda h, q, m: app.active_areas()),
        ("GET", re.compile(r"^/api/calendar$"), lambda h, q, m: app.calendar(_lane(q), q)),
        ("GET", re.compile(r"^/api/calendar/day$"), lambda h, q, m: app.calendar_day(_lane(q), q)),
        ("GET", re.compile(r"^/api/calendar/areas$"), lambda h, q, m: app.calendar_areas(_lane(q))),
        ("GET", re.compile(r"^/api/calendar/longterm$"), lambda h, q, m: app.calendar_longterm(_lane(q), q)),
        ("GET", re.compile(r"^/api/country/list$"), lambda h, q, m: app.country_list()),
        ("GET", re.compile(r"^/api/country/profile$"), lambda h, q, m: app.country_profile(_lane(q), q)),
        ("POST", re.compile(r"^/api/calendar/backfill$"), lambda h, q, m: app.start_calendar_job(body_of(h))),
        ("GET", re.compile(r"^/api/jobs/latest$"), lambda h, q, m: app.latest_job()),
        ("GET", re.compile(r"^/api/jobs/(?P<jid>[0-9a-f]+)$"), lambda h, q, m: app.job(m["jid"])),
        ("POST", re.compile(r"^/api/ingest$"), lambda h, q, m: app.start_job(body_of(h))),
        ("POST", re.compile(r"^/api/quarantine/(?P<qid>q_[0-9a-f]+)/(?P<action>release|reject)$"),
         lambda h, q, m: app.resolve_quarantine(m["qid"], m["action"], body_of(h).get("note"))),
    ]

    class Handler(BaseHTTPRequestHandler):
        server_version = "fireverify-dashboard"

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: Any) -> None:
            self._send(status, json.dumps(payload, default=str).encode(), "application/json")

        def _static(self, path: str) -> None:
            rel = "index.html" if path in ("/", "/index.html") else path.removeprefix("/static/")
            target = (STATIC_DIR / rel).resolve()
            if STATIC_DIR.resolve() not in target.parents or not target.is_file():
                self._json(404, {"error": "not found"})
                return
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript",):
                ctype += "; charset=utf-8"
            self._send(200, target.read_bytes(), ctype)

        def _check_origin(self) -> None:
            if self.headers.get("X-Requested-With") != "fireverify":
                raise ApiError(403, "missing X-Requested-With header")
            origin = self.headers.get("Origin")
            host = self.headers.get("Host", "")
            if origin and _normalize_host(origin) != _normalize_host(host):
                raise ApiError(403, "cross-origin request refused")

        def _check_admin(self) -> None:
            _check_admin(self, expected_token)

        def _dispatch(self, method: str) -> None:
            url = urlparse(self.path)
            if method == "GET" and url.path in ("/health", "/api/health"):
                self._json(200, {"ok": True})
                return
            if method == "GET" and not url.path.startswith("/api/"):
                self._static(url.path)
                return
            try:
                if method == "POST":
                    self._check_origin()
                    self._check_admin()
                for verb, pattern, fn in routes:
                    m = pattern.match(url.path)
                    if verb == method and m:
                        self._json(200, fn(self, parse_qs(url.query), m.groupdict()))
                        return
                raise ApiError(404, f"no route {method} {url.path}")
            except ApiError as exc:
                if exc.status == 401:
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    body = json.dumps({"error": str(exc)}).encode()
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("WWW-Authenticate", 'Bearer realm="ftd admin"')
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self._json(exc.status, {"error": str(exc)})
            except Exception as exc:  # pragma: no cover - surfaced to the UI
                log.error("unhandled error: %s\n%s", exc, traceback.format_exc())
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

    return Handler


def serve(settings: Settings, db_path: str | Path, host: str = "127.0.0.1", port: int = 8765,
          admin_token: str | None = None) -> None:
    token = _configured_admin_token(admin_token)
    public = host not in {"127.0.0.1", "localhost", "::1"}
    if public and not token:
        raise SystemExit("Refusing to bind to a public interface without FTD_ADMIN_TOKEN")
    app = DashboardApp(settings, db_path)
    httpd = ThreadingHTTPServer((host, port), make_handler(app, token or None))
    auth = "POST routes password-protected" if token else "POST routes open (no FTD_ADMIN_TOKEN)"
    print(f"ftd.io dashboard on http://{host}:{port}  (database: {db_path}; {auth})  - Ctrl+C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
