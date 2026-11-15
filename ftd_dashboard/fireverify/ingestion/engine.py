"""Ingestion engine orchestration.

    sources (live | simulated)
        -> parse (registry)          -- failure: REJECT R_PARSE
        -> validate (rules)          -- REJECT / QUARANTINE / ACCEPT
        -> origin lane (live | simulated tables)
    accepted hotspots -> spatio-temporal clusters -> per-cluster enrichment
        (Sentinel-2 NDVI/NBR, Sentinel-1 SAR, Sentinel-3 SLSTR coverage, FIRMS archive)
        -> same parse/validate/route path
    -> derived lane (evidence features per cluster)

Live and simulated runs never share a table, and every raw record is checked
against the run's lane before it is written.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fireverify.config import Settings
from fireverify.ingestion.base import FetchRequest, ParseError, Scope, Source, SourceError
from fireverify.ingestion.derived import build_target, cluster_hotspots, compute_features
from fireverify.ingestion.http import HttpClient
from fireverify.ingestion.parsers import PARSERS
from fireverify.ingestion.simulated import (
    SimulatedContextSource, SimulatedFirmsHistoricalSource, SimulatedFirmsSource, SimulatedSentinel1Source,
    SimulatedSentinel2Source, SimulatedSentinel3Source, SimWorld,
)
from fireverify.ingestion.sources.context import GeoJsonContextSource, OverpassContextSource, bundled_context_files
from fireverify.ingestion.sources.firms import FirmsArchiveCsvSource, FirmsHistoricalSource, FirmsSource
from fireverify.ingestion.sources.sentinel1 import Sentinel1SarSource
from fireverify.ingestion.sources.sentinel2 import Sentinel2NdviSource
from fireverify.ingestion.sources.sentinel3 import sentinel3_frp_source
from fireverify.ingestion.store import Store
from fireverify.ingestion.validation import Reason, ValidationContext, validate
from fireverify.models import (
    Action, BBox, IngestedRecord, Lane, Mode, RawRecord, RecordKind, record_id_for, stable_hash, utcnow,
)

log = logging.getLogger(__name__)

WORLD = BBox(-180, -90, 180, 90)


class LaneViolation(RuntimeError):
    """A source produced records for a different lane than the run's mode."""


@dataclass
class SourceStats:
    status: str = "pending"
    detail: str = ""
    fetched: int = 0
    accepted: int = 0
    already_ingested: int = 0
    quarantined: int = 0
    rejected: int = 0
    reasons: Counter = field(default_factory=Counter)

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "detail": self.detail, "fetched": self.fetched, "accepted": self.accepted,
                "already_ingested": self.already_ingested, "quarantined": self.quarantined,
                "rejected": self.rejected, "reasons": dict(self.reasons)}


@dataclass
class RunReport:
    run_id: str
    mode: str
    aoi: str
    start: datetime
    end: datetime
    sources: dict[str, SourceStats] = field(default_factory=dict)
    clusters_found: int = 0
    clusters_enriched: list[str] = field(default_factory=list)
    derived_written: int = 0
    derivation_errors: list[str] = field(default_factory=list)

    def totals(self) -> dict[str, int]:
        keys = ("fetched", "accepted", "already_ingested", "quarantined", "rejected")
        return {k: sum(getattr(s, k) for s in self.sources.values()) for k in keys}

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "mode": self.mode, "aoi": self.aoi,
            "start": self.start.isoformat(), "end": self.end.isoformat(),
            "totals": self.totals(), "sources": {k: v.as_dict() for k, v in self.sources.items()},
            "clusters_found": self.clusters_found, "clusters_enriched": self.clusters_enriched,
            "derived_written": self.derived_written, "derivation_errors": self.derivation_errors,
        }


class IngestionEngine:
    def __init__(self, settings: Settings, store: Store, http: HttpClient | None = None) -> None:
        self.settings = settings
        self.store = store
        self._http = http

    @property
    def http(self) -> HttpClient:
        if self._http is None:
            s = self.settings
            self._http = HttpClient(s.http_timeout_s, s.user_agent, s.http_max_retries,
                                    secrets=[s.firms_map_key, s.cdse_client_secret])
        return self._http

    # ------------------------------------------------------------ sources

    def build_sources(self, mode: Mode, world: SimWorld | None = None) -> tuple[list[Source], list[Source]]:
        s = self.settings
        if mode is Mode.SIMULATED:
            if world is None:
                raise ValueError("simulated mode requires a SimWorld")
            aoi_sources: list[Source] = [SimulatedFirmsSource(n, s, world) for n in s.firms_sources]
            aoi_sources.append(SimulatedContextSource(world))
            target_sources: list[Source] = [
                SimulatedSentinel2Source(s, world), SimulatedSentinel1Source(s, world), SimulatedSentinel3Source(s, world),
                *[SimulatedFirmsHistoricalSource(a, s, world) for a in s.history_sources],
            ]
            return aoi_sources, target_sources
        http = self.http
        aoi_sources = [FirmsSource(n, s, http) for n in s.firms_sources]
        aoi_sources.append(GeoJsonContextSource([*bundled_context_files(), *s.context_paths]))
        aoi_sources.append(OverpassContextSource(s, http))
        target_sources = [
            Sentinel2NdviSource(s, http), Sentinel1SarSource(s, http), sentinel3_frp_source(s, http),
            *[FirmsHistoricalSource(a, s, http) for a in s.history_sources],
        ]
        return aoi_sources, target_sources

    def source_availability(self) -> dict[str, tuple[bool, str]]:
        aoi_sources, target_sources = self.build_sources(Mode.LIVE)
        return {src.name: src.available() for src in [*aoi_sources, *target_sources]}

    # -------------------------------------------------------------- core

    def _process(self, raw: RawRecord, lane: Lane, ctx: ValidationContext, stats: SourceStats,
                 seen: set[str], run_id: str) -> str | None:
        """Parse, validate and route one raw record. Returns the record id if newly accepted."""
        if raw.lane is not lane:
            raise LaneViolation(f"{raw.source} produced a {raw.lane.value} record during a {lane.value} run")
        stats.fetched += 1

        def reject(codes: list[str], details: list[str]) -> None:
            stats.rejected += 1
            stats.reasons.update(codes)
            self.store.reject(raw, codes, details, run_id)

        if raw.raw_hash in seen:
            reject([Reason.DUPLICATE.value], [f"{Reason.DUPLICATE.value}: hash {raw.raw_hash} repeated in response"])
            return None
        seen.add(raw.raw_hash)

        try:
            model = PARSERS[raw.parser](raw)
        except ParseError as exc:
            reject([Reason.PARSE_ERROR.value], [f"{Reason.PARSE_ERROR.value}: {exc}"])
            return None
        except Exception as exc:  # malformed payloads must never abort a run
            reject([Reason.PARSE_ERROR.value], [f"{Reason.PARSE_ERROR.value}: {type(exc).__name__}: {exc}"])
            return None

        verdict = validate(raw.kind, model, ctx)
        if verdict.action is Action.REJECT:
            reject(verdict.codes, verdict.details)
            return None
        if verdict.action is Action.QUARANTINE:
            stats.quarantined += 1
            stats.reasons.update(verdict.codes)
            self.store.quarantine(raw, verdict.codes, verdict.details, run_id)
            return None

        rec = IngestedRecord(record_id_for(raw), raw, model, verdict.flags)
        if self.store.insert_record(rec, run_id):
            stats.accepted += 1
            return rec.record_id
        stats.already_ingested += 1
        return None

    def _run_source(self, src: Source, req: FetchRequest, lane: Lane, ctx: ValidationContext,
                    report: RunReport, run_id: str, accepted: list[str] | None = None) -> str:
        stats = report.sources.setdefault(src.name, SourceStats())
        ok, why = src.available()
        if not ok:
            stats.status, stats.detail = "unavailable", why
            return "unavailable"
        cache_key = req.aoi.key()
        if src.scope is Scope.AOI and src.refresh_interval:
            last = self.store.last_fetch(lane, src.name, cache_key)
            if last and utcnow() - last < src.refresh_interval:
                stats.status, stats.detail = "cached", f"fetched {last.isoformat()}"
                return "cached"
        seen: set[str] = set()
        try:
            for raw in src.fetch(req):
                rid = self._process(raw, lane, ctx, stats, seen, run_id)
                if rid and accepted is not None:
                    accepted.append(rid)
        except LaneViolation:
            raise
        except SourceError as exc:
            stats.status, stats.detail = "error", str(exc)
            log.warning("%s failed: %s", src.name, exc)
            return "error"
        except Exception as exc:  # a broken connector must not take the run down
            stats.status, stats.detail = "error", f"{type(exc).__name__}: {exc}"
            log.exception("%s crashed", src.name)
            return "error"
        if stats.status != "error":
            stats.status = "ok"
        if src.scope is Scope.AOI and src.refresh_interval:
            self.store.mark_fetched(lane, src.name, cache_key)
        return "ok"

    def run(self, mode: Mode | str, aoi: BBox, *, days: float = 2.0, start: datetime | None = None,
            end: datetime | None = None, now: datetime | None = None, seed: int = 42,
            scenarios: list[str] | None = None, enrich: bool = True, force_enrich: bool = False) -> RunReport:
        mode = Mode(mode)
        lane = mode.lane
        s = self.settings
        now = now or utcnow()
        end = end or now
        start = start or end - timedelta(days=days)
        if start >= end:
            raise ValueError("start must be before end")
        run_id = f"run_{now:%Y%m%dT%H%M%S}_{stable_hash(mode.value, aoi.key(), start, end, seed, utcnow(), length=6)}"
        world = SimWorld(aoi, start, end, now, seed, scenarios) if mode is Mode.SIMULATED else None
        aoi_sources, target_sources = self.build_sources(mode, world)

        params = {"aoi": aoi.as_list(), "start": start.isoformat(), "end": end.isoformat(), "now": now.isoformat(),
                  "seed": seed if world else None, "scenarios": list(world.scenarios) if world else None,
                  "enrich": enrich, "force_enrich": force_enrich}
        self.store.start_run(run_id, mode.value, params)
        report = RunReport(run_id, mode.value, aoi.key(), start, end)
        log.info("%s: %s run over %s, %s -> %s", run_id, mode.value, aoi.key(), start.isoformat(), end.isoformat())

        # Phase 1: AOI-wide detections and context layers.
        new_hotspots: list[str] = []
        aoi_ctx = ValidationContext(s, now, aoi=aoi, window_start=start, check_window=True)
        aoi_req = FetchRequest(aoi, start, end, now)
        for src in aoi_sources:
            self._run_source(src, aoi_req, lane, aoi_ctx, report, run_id,
                             accepted=new_hotspots if src.kind is RecordKind.HOTSPOT else None)

        # Phase 2: cluster accepted detections (including recent ones from earlier runs).
        recent = self.store.query_records(
            lane, kinds=[RecordKind.HOTSPOT], bbox=aoi,
            start=start - timedelta(hours=s.cluster_window_hours), end=end + timedelta(hours=1))
        targets = [build_target(c) for c in cluster_hotspots(recent, s.cluster_radius_km, s.cluster_window_hours)]
        report.clusters_found = len(targets)
        fresh = set(new_hotspots)
        selected = [t for t in targets if force_enrich or fresh.intersection(t.member_ids)]
        selected.sort(key=lambda t: len(t.member_ids), reverse=True)
        if len(selected) > s.max_enrich_clusters:
            log.warning("enriching %d of %d clusters (max_enrich_clusters)", s.max_enrich_clusters, len(selected))
            selected = selected[: s.max_enrich_clusters]

        # Phase 3: per-cluster enrichment, then derived features.
        target_ctx = ValidationContext(s, now, check_window=False)
        for target in selected:
            status: dict[str, str] = {}
            if enrich:
                req = FetchRequest(aoi, start, end, now, target)
                for src in target_sources:
                    status[src.name] = self._run_source(src, req, lane, target_ctx, report, run_id)
            try:
                for feat in compute_features(self.store, lane, target, s, status):
                    report.derived_written += int(self.store.insert_derived(feat, run_id))
                report.clusters_enriched.append(target.cluster_id)
            except Exception as exc:
                msg = f"{target.cluster_id}: {type(exc).__name__}: {exc}"
                log.exception("derivation failed for %s", target.cluster_id)
                report.derivation_errors.append(msg)
                self.store.reject_derivation(lane, target.cluster_id, msg, run_id)

        self.store.finish_run(run_id, {**report.totals(), "clusters_found": report.clusters_found,
                                       "clusters_enriched": len(report.clusters_enriched),
                                       "derived_written": report.derived_written},
                              {k: v.as_dict() for k, v in report.sources.items()})
        return report

    # --------------------------------------------------- historical import

    def import_archive(self, path: Path, archive_source: str, now: datetime | None = None) -> RunReport:
        now = now or utcnow()
        src = FirmsArchiveCsvSource(path, archive_source)
        run_id = f"import_{now:%Y%m%dT%H%M%S}_{stable_hash(str(path), utcnow(), length=6)}"
        self.store.start_run(run_id, "archive_import", {"path": str(path), "source": archive_source})
        report = RunReport(run_id, "archive_import", WORLD.key(), now, now)
        ctx = ValidationContext(self.settings, now, check_window=False)
        self._run_source(src, FetchRequest(WORLD, now, now, now), Lane.LIVE, ctx, report, run_id)
        self.store.finish_run(run_id, report.totals(), {k: v.as_dict() for k, v in report.sources.items()})
        return report

    # ------------------------------------------------- quarantine review

    def _raw_from_audit(self, q: dict[str, Any]) -> RawRecord:
        return RawRecord(
            source=q["source"], parser=q["parser"], kind=RecordKind(q["kind"]), payload=q["raw_payload"],
            lane=Lane(q["origin_lane"]), fetched_at=datetime.fromisoformat(q["fetched_at"]),
            target_id=q["target_id"], sim_truth=q.get("sim_truth"),
        )

    def release_quarantined(self, qid: str, note: str | None = None) -> str:
        """Reviewer override: re-parse, re-run hard rules only, write to the origin lane."""
        q = self.store.get_quarantine(qid)
        if q is None:
            raise KeyError(f"no quarantine entry {qid}")
        if q["status"] != "pending":
            raise ValueError(f"{qid} already {q['status']}")
        raw = self._raw_from_audit(q)
        model = PARSERS[raw.parser](raw)
        verdict = validate(raw.kind, model, ValidationContext(self.settings, utcnow(), ignore_soft=True))
        if verdict.action is Action.REJECT:
            self.store.reject(raw, verdict.codes, verdict.details, q["run_id"])
            self.store.resolve_quarantine(qid, "rejected", f"hard rule failed on release: {verdict.codes}")
            return "rejected"
        rec = IngestedRecord(record_id_for(raw), raw, model, [*verdict.flags, "released_from_quarantine"])
        self.store.insert_record(rec, q["run_id"])
        self.store.resolve_quarantine(qid, "released", note)
        return rec.record_id

    def reject_quarantined(self, qid: str, note: str | None = None) -> None:
        q = self.store.get_quarantine(qid)
        if q is None:
            raise KeyError(f"no quarantine entry {qid}")
        if q["status"] != "pending":
            raise ValueError(f"{qid} already {q['status']}")
        raw = self._raw_from_audit(q)
        codes = [*q["reasons"], Reason.MANUAL_REJECT.value]
        self.store.reject(raw, codes, [*q["details"], f"R_MANUAL: {note or 'rejected by reviewer'}"], q["run_id"])
        self.store.resolve_quarantine(qid, "rejected", note)

    # ----------------------------------------------------- evidence bundle

    def evidence(self, lane: Lane, cluster_id: str) -> dict[str, Any] | None:
        """Everything the verification stage needs for one fire event."""
        derived = self.store.latest_derived(lane, cluster_id)
        if not derived:
            return None
        summary = derived["cluster_summary"]["value"]

        def slim(r: dict[str, Any]) -> dict[str, Any]:
            out = {"record_id": r["record_id"], "kind": r["kind"], "source": r["source"],
                   "observed_at": r["observed_at"].isoformat() if r["observed_at"] else None,
                   "lat": r["lat"], "lon": r["lon"], "flags": r["flags"], "data": r["data"]}
            if r.get("sim_truth"):
                out["sim_truth"] = r["sim_truth"]
            return out

        return {
            "cluster_id": cluster_id,
            "lane": lane.value,
            "derived": {name: d["value"] for name, d in derived.items()},
            "detections": [slim(r) for r in self.store.query_records(lane, ids=summary["member_ids"])],
            "enrichment": [slim(r) for r in self.store.query_records(lane, target_id=cluster_id)],
        }
