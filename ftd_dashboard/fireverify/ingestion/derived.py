"""Derived lane: features computed from accepted records, per clustered fire event.

These are *evidence features*, not a verdict. The verification stage consumes them.

cluster_summary            size, duration, FRP, overpass count of the detection cluster
cross_sensor_corroboration distinct sensors / platforms / overpasses, Sentinel-3 SLSTR coverage
vegetation_loss            Sentinel-2 dNDVI, dNBR and USGS burn-severity class
sar_backscatter_change     Sentinel-1 dVV / dVH (dB)
temporal_frequency         historical recurrence at the location (persistent heat source?)
context_proximity          distance to flares / industry / volcanoes, containing land-cover class
evidence_completeness      which evidence streams were available for this event
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from fireverify.config import Settings
from fireverify.geo import distance_to_geometry_km, haversine_km, point_in_geometry
from fireverify.models import BBox, DerivedFeature, EnrichmentTarget, Lane, RecordKind, utcnow

ALGORITHM_VERSION = "1.1.0"
MAX_INPUT_IDS = 500


# --------------------------------------------------------------------------
# Clustering
# --------------------------------------------------------------------------

def cluster_hotspots(records: list[dict[str, Any]], radius_km: float, window_hours: float) -> list[list[dict[str, Any]]]:
    """Single-linkage spatio-temporal clustering with a grid index (O(n) average)."""
    if not records:
        return []
    max_abs_lat = min(85.0, max(abs(r["lat"]) for r in records))
    cell_lat = radius_km / 110.574
    cell_lon = radius_km / (111.320 * math.cos(math.radians(max_abs_lat)))
    parent = list(range(len(records)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, r in enumerate(records):
        grid[(math.floor(r["lat"] / cell_lat), math.floor(r["lon"] / cell_lon))].append(i)
    window = timedelta(hours=window_hours)
    for (gy, gx), members in grid.items():
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for j in grid.get((gy + dy, gx + dx), ()):
                    for i in members:
                        if i >= j:
                            continue
                        a, b = records[i], records[j]
                        if abs(a["observed_at"] - b["observed_at"]) <= window and \
                                haversine_km(a["lat"], a["lon"], b["lat"], b["lon"]) <= radius_km:
                            parent[find(i)] = find(j)
    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for i, r in enumerate(records):
        groups[find(i)].append(r)
    return [sorted(g, key=lambda r: (r["observed_at"], r["record_id"])) for g in groups.values()]


def build_target(members: list[dict[str, Any]]) -> EnrichmentTarget:
    """Cluster id = earliest member's id, so it stays stable as later detections join."""
    weights = [max(m["data"].get("frp_mw") or 1.0, 1.0) for m in members]
    total = sum(weights)
    lat = sum(m["lat"] * w for m, w in zip(members, weights)) / total
    lon = sum(m["lon"] * w for m, w in zip(members, weights)) / total
    extent = max(haversine_km(lat, lon, m["lat"], m["lon"]) for m in members)
    return EnrichmentTarget(
        cluster_id="evt_" + members[0]["record_id"].split("_", 1)[1],
        latitude=lat,
        longitude=lon,
        event_start=members[0]["observed_at"],
        event_end=max(m["observed_at"] for m in members),
        radius_km=round(extent + 0.5, 3),
        member_ids=tuple(m["record_id"] for m in members),
    )


# --------------------------------------------------------------------------
# Feature computations
# --------------------------------------------------------------------------

def _overpass_key(m: dict[str, Any]) -> tuple[str, str]:
    t: datetime = m["observed_at"]
    return m["data"]["platform"], t.replace(minute=(t.minute // 15) * 15, second=0, microsecond=0).isoformat()


def cluster_summary(target: EnrichmentTarget, members: list[dict[str, Any]]) -> dict[str, Any]:
    frps = [m["data"].get("frp_mw") for m in members if m["data"].get("frp_mw") is not None]
    duration_h = (target.event_end - target.event_start).total_seconds() / 3600
    return {
        "n_detections": len(members),
        "n_overpasses": len({_overpass_key(m) for m in members}),
        "sensors": sorted({m["data"]["sensor"] for m in members}),
        "platforms": sorted({m["data"]["platform"] for m in members}),
        "first_seen": target.event_start.isoformat(),
        "last_seen": target.event_end.isoformat(),
        "duration_hours": round(duration_h, 2),
        "detections_per_day": round(len(members) / max(duration_h / 24, 1 / 24), 2),
        "max_frp_mw": round(max(frps), 2) if frps else None,
        "total_frp_mw": round(sum(frps), 2) if frps else None,
        "mean_confidence": round(sum(m["data"]["confidence"] for m in members) / len(members), 3),
        "day_detections": sum(1 for m in members if m["data"].get("daynight") == "D"),
        "night_detections": sum(1 for m in members if m["data"].get("daynight") == "N"),
        "centroid_lat": round(target.latitude, 6),
        "centroid_lon": round(target.longitude, 6),
        "radius_km": target.radius_km,
        "member_ids": list(target.member_ids),
    }


def cross_sensor(members: list[dict[str, Any]], overpasses: list[dict[str, Any]]) -> dict[str, Any]:
    sensors = sorted({m["data"]["sensor"] for m in members})
    platforms = sorted({m["data"]["platform"] for m in members})
    n_overpasses = len({_overpass_key(m) for m in members})
    # The catalogue lists NRT and reprocessed versions of one acquisition; count acquisitions.
    s3 = {(o["data"]["platform"], o["data"]["sensing_start"][:16]) for o in overpasses
          if o["data"].get("product_type", "").startswith("SL_2_FRP")}
    return {
        "n_sensors": len(sensors),
        "n_platforms": len(platforms),
        "sensors": sensors,
        "platforms": platforms,
        "multi_sensor": len(sensors) > 1,
        "multi_platform": len(platforms) > 1,
        "repeat_detection": n_overpasses > 1,
        "s3_slstr_frp_granules": len(s3),
        "s3_slstr_coverage": bool(s3),
        "s3_slstr_detection": None,  # requires FRP NetCDF pixel extraction (not part of ingestion)
    }


def _burn_severity(dnbr: float | None) -> str | None:
    if dnbr is None:
        return None
    if dnbr < 0.10:
        return "unburned"
    if dnbr < 0.27:
        return "low"
    if dnbr < 0.44:
        return "moderate_low"
    if dnbr < 0.66:
        return "moderate_high"
    return "high"


def vegetation_loss(optical: dict[str, Any] | None) -> dict[str, Any]:
    if optical is None:
        return {"status": "unavailable"}
    d = optical["data"]
    pre, post = d.get("ndvi_pre"), d.get("ndvi_post")
    nbr_pre, nbr_post = d.get("nbr_pre"), d.get("nbr_post")
    if pre is None:
        return {"status": "missing_pre", "source_record": optical["record_id"]}
    if post is None:
        return {"status": "missing_post", "ndvi_pre": round(pre, 4), "source_record": optical["record_id"]}
    dndvi = pre - post
    dnbr = (nbr_pre - nbr_post) if nbr_pre is not None and nbr_post is not None else None
    return {
        "status": "complete",
        "ndvi_pre": round(pre, 4),
        "ndvi_post": round(post, 4),
        "dndvi": round(dndvi, 4),
        "relative_ndvi_loss": round(dndvi / pre, 4) if pre > 0.1 else None,
        "dnbr": round(dnbr, 4) if dnbr is not None else None,
        "burn_severity": _burn_severity(dnbr),
        "vegetated_before": pre >= 0.3,
        "clear_fraction_pre": d.get("valid_fraction_pre"),
        "clear_fraction_post": d.get("valid_fraction_post"),
        "source_record": optical["record_id"],
    }


def sar_change(sar: dict[str, Any] | None) -> dict[str, Any]:
    if sar is None:
        return {"status": "unavailable"}
    d = sar["data"]
    if d.get("vh_pre_db") is None or d.get("vh_post_db") is None:
        return {"status": "incomplete", "source_record": sar["record_id"]}
    dvh = d["vh_post_db"] - d["vh_pre_db"]
    dvv = (d["vv_post_db"] - d["vv_pre_db"]) if d.get("vv_pre_db") is not None and d.get("vv_post_db") is not None else None
    return {
        "status": "complete",
        "vh_pre_db": round(d["vh_pre_db"], 3),
        "vh_post_db": round(d["vh_post_db"], 3),
        "dvh_db": round(dvh, 3),
        "dvv_db": round(dvv, 3) if dvv is not None else None,
        "vh_drop": dvh <= -1.5,
        "source_record": sar["record_id"],
    }


def temporal_frequency(target: EnrichmentTarget, history: list[dict[str, Any]], settings: Settings,
                       history_checked: bool) -> dict[str, Any]:
    cutoff = target.event_start - timedelta(hours=24)
    near = [h for h in history
            if h["observed_at"] < cutoff
            and haversine_km(target.latitude, target.longitude, h["lat"], h["lon"]) <= settings.history_radius_km]
    days = {h["observed_at"].date() for h in near}
    years = {d.year for d in days}
    months = {d.month for d in days}
    event_doy = target.event_start.timetuple().tm_yday

    def off_season(d) -> bool:
        diff = abs(d.timetuple().tm_yday - event_doy)
        return min(diff, 365 - diff) > settings.season_halfwidth_days

    off_days = {d for d in days if off_season(d)}
    in_years = {d.year for d in days - off_days}
    off_years = {d.year for d in off_days}
    windows = 2 if settings.history_offseason else 1
    sampled_days = max(1, settings.history_years * settings.history_window_days * windows)
    persistence = min(1.0, len(days) / sampled_days)
    min_years = max(2, math.ceil(0.4 * settings.history_years))
    if not near:
        recurrence = "none"
    elif len(off_years) >= min_years and len(in_years) >= min_years:
        recurrence = "persistent"          # active in and out of season: year-round heat source
    elif len(in_years) >= 2:
        recurrence = "seasonal"            # recurs at this time of year only
    else:
        recurrence = "sporadic"
    last = max((h["observed_at"] for h in near), default=None)
    return {
        "history_checked": history_checked,
        "radius_km": settings.history_radius_km,
        "years_sampled": settings.history_years,
        "historical_detections": len(near),
        "distinct_days": len(days),
        "distinct_years": len(years),
        "distinct_months": sorted(months),
        "in_season_years": len(in_years),
        "off_season_years": len(off_years),
        "off_season_sampled": settings.history_offseason,
        "persistence_index": round(persistence, 3),
        "recurrence_class": recurrence if history_checked or near else "unknown",
        "last_prior_detection": last.isoformat() if last else None,
        "days_since_prior_detection": (target.event_start - last).days if last else None,
    }


def context_proximity(target: EnrichmentTarget, features: list[dict[str, Any]], settings: Settings) -> dict[str, Any]:
    nearest: dict[str, dict[str, Any]] = {}
    containing: list[str] = []
    for f in features:
        d = f["data"]
        geom = d.get("geometry")
        dist = distance_to_geometry_km(target.latitude, target.longitude, geom, fallback=(d["latitude"], d["longitude"]))
        cat = d["category"]
        if geom and geom.get("type") in ("Polygon", "MultiPolygon") and point_in_geometry(target.latitude, target.longitude, geom):
            containing.append(cat)
        if cat not in nearest or dist < nearest[cat]["distance_km"]:
            nearest[cat] = {"distance_km": round(dist, 3), "feature_id": d["feature_id"], "name": d.get("name")}
    flags = {
        f"near_{cat}": cat in nearest and nearest[cat]["distance_km"] <= buf
        for cat, buf in settings.proximity_buffers_km.items()
    }
    flags["in_urban_area"] = "urban" in containing
    return {
        "search_radius_km": settings.context_search_km,
        "features_considered": len(features),
        "nearest": nearest,
        "land_cover": sorted(set(containing)),
        **flags,
    }


def evidence_completeness(veg: dict[str, Any], sar: dict[str, Any], temporal: dict[str, Any],
                          ctx: dict[str, Any], xs: dict[str, Any]) -> dict[str, Any]:
    streams = {
        "thermal_detections": True,
        "optical_vegetation": veg.get("status") == "complete",
        "sar_backscatter": sar.get("status") == "complete",
        "historical_frequency": bool(temporal.get("history_checked")),
        "contextual_layers": ctx.get("features_considered", 0) > 0,
        "s3_slstr_coverage": bool(xs.get("s3_slstr_coverage")),
    }
    return {"streams": streams, "score": round(sum(streams.values()) / len(streams), 3)}


# --------------------------------------------------------------------------
# Orchestration for one cluster
# --------------------------------------------------------------------------

def _finite_tree(obj: Any) -> bool:
    if isinstance(obj, float):
        return math.isfinite(obj)
    if isinstance(obj, dict):
        return all(_finite_tree(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return all(_finite_tree(v) for v in obj)
    return True


def _latest(records: list[dict[str, Any]], kind: RecordKind) -> dict[str, Any] | None:
    of_kind = [r for r in records if r["kind"] == kind.value]
    return max(of_kind, key=lambda r: r["ingested_at"]) if of_kind else None


def compute_features(store, lane: Lane, target: EnrichmentTarget, settings: Settings,
                     enrichment_status: dict[str, str]) -> list[DerivedFeature]:
    members = store.query_records(lane, ids=target.member_ids)
    enrichment = store.query_records(lane, target_id=target.cluster_id,
                                     kinds=[RecordKind.OPTICAL_CHANGE, RecordKind.SAR_CHANGE, RecordKind.OVERPASS])
    hist_box = BBox.around(target.latitude, target.longitude, settings.history_radius_km + 0.5)
    history = store.query_records(lane, kinds=[RecordKind.HISTORICAL_HOTSPOT, RecordKind.HOTSPOT], bbox=hist_box,
                                  end=target.event_start)
    ctx_box = BBox.around(target.latitude, target.longitude, settings.context_search_km)
    context = store.query_records(lane, kinds=[RecordKind.CONTEXT_FEATURE], bbox=ctx_box)
    # Large polygons (land cover) may have their centroid outside the search box.
    context += [c for c in store.query_records(lane, kinds=[RecordKind.CONTEXT_FEATURE])
                if c["record_id"] not in {x["record_id"] for x in context}
                and point_in_geometry(target.latitude, target.longitude, c["data"].get("geometry"))]

    optical = _latest(enrichment, RecordKind.OPTICAL_CHANGE)
    sar = _latest(enrichment, RecordKind.SAR_CHANGE)
    overpasses = [r for r in enrichment if r["kind"] == RecordKind.OVERPASS.value]
    history_checked = any(k.endswith("_HISTORY") and v == "ok" for k, v in enrichment_status.items())

    veg = vegetation_loss(optical)
    sarc = sar_change(sar)
    # Say *why* a stream is missing when its record is sitting in quarantine.
    held = store.pending_quarantine_for_target(lane, target.cluster_id)
    for feature, kind in ((veg, RecordKind.OPTICAL_CHANGE), (sarc, RecordKind.SAR_CHANGE)):
        q = [h for h in held if h["kind"] == kind.value]
        if feature["status"] == "unavailable" and q:
            feature.update(status="quarantined", quarantine_ids=[h["qid"] for h in q],
                           quarantine_reasons=sorted({c for h in q for c in h["reasons"]}))
    temporal = temporal_frequency(target, history, settings, history_checked)
    ctxp = context_proximity(target, context, settings)
    xs = cross_sensor(members, overpasses)
    completeness = evidence_completeness(veg, sarc, temporal, ctxp, xs)
    completeness["enrichment_status"] = dict(sorted(enrichment_status.items()))

    member_ids = [m["record_id"] for m in members]
    now = utcnow()
    specs: list[tuple[str, dict[str, Any], list[str]]] = [
        ("cluster_summary", cluster_summary(target, members), member_ids),
        ("cross_sensor_corroboration", xs, member_ids + [o["record_id"] for o in overpasses]),
        ("vegetation_loss", veg, [optical["record_id"]] if optical else []),
        ("sar_backscatter_change", sarc, [sar["record_id"]] if sar else []),
        ("temporal_frequency", temporal, [h["record_id"] for h in history][:MAX_INPUT_IDS]),
        ("context_proximity", ctxp, [c["record_id"] for c in context][:MAX_INPUT_IDS]),
        ("evidence_completeness", completeness, []),
    ]
    out = []
    for name, value, inputs in specs:
        if not _finite_tree(value):
            raise ValueError(f"derived feature {name} produced non-finite values")
        out.append(DerivedFeature(target.cluster_id, name, value, inputs, lane, ALGORITHM_VERSION, now))
    return out
