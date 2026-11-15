"""Validation rules that route each normalized record to a lane.

REJECT (``R_*``)     impossible or unusable: wrong coordinates, unphysical values,
                     unparseable payloads, duplicates. Kept for audit only.
QUARANTINE (``Q_*``) plausible but untrustworthy until reviewed or re-fetched:
                     low confidence, clock skew, stale, day/night mismatch,
                     cloud-contaminated optical stats...
ACCEPT               written to the origin lane (live or simulated), possibly with
                     non-blocking quality ``flags``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable

from fireverify.config import Settings
from fireverify.geo import VALID_GEOMETRY_TYPES, solar_elevation_deg
from fireverify.models import (
    Action, BBox, CanonicalRecord, ContextFeature, Hotspot, OpticalChange, Overpass, RecordKind, SarChange,
)


class Reason(str, Enum):
    # hard -> rejected
    PARSE_ERROR = "R_PARSE"
    INVALID_COORDINATES = "R_COORD"
    OUT_OF_AOI = "R_AOI"
    FUTURE_TIMESTAMP = "R_FUTURE"
    UNPHYSICAL_VALUE = "R_PHYSICS"
    DUPLICATE = "R_DUPLICATE"
    NO_DATA = "R_NO_DATA"
    INVALID_GEOMETRY = "R_GEOMETRY"
    DERIVATION_FAILED = "R_DERIVATION"
    MANUAL_REJECT = "R_MANUAL"
    # soft -> quarantined
    LOW_CONFIDENCE = "Q_LOW_CONF"
    CLOCK_SKEW = "Q_CLOCK_SKEW"
    OUT_OF_WINDOW = "Q_OUT_OF_WINDOW"
    DAYNIGHT_MISMATCH = "Q_DAYNIGHT"
    SAA_ARTIFACT = "Q_SAA"
    MISSING_FRP = "Q_MISSING_FRP"
    INSUFFICIENT_CLEAR_SKY = "Q_CLOUDY"
    LOW_VALID_SAMPLES = "Q_LOW_SAMPLES"

    @property
    def hard(self) -> bool:
        return self.value.startswith("R_")


REASON_DESCRIPTIONS = {
    Reason.PARSE_ERROR: "payload could not be parsed into a canonical record",
    Reason.INVALID_COORDINATES: "latitude/longitude outside valid range or not finite",
    Reason.OUT_OF_AOI: "record lies outside the requested area of interest",
    Reason.FUTURE_TIMESTAMP: "acquisition time is far in the future",
    Reason.UNPHYSICAL_VALUE: "value outside physically possible range",
    Reason.DUPLICATE: "exact duplicate of a record already seen in this run",
    Reason.NO_DATA: "source returned no usable samples",
    Reason.INVALID_GEOMETRY: "geometry missing or malformed",
    Reason.DERIVATION_FAILED: "derived product could not be computed",
    Reason.MANUAL_REJECT: "rejected by a reviewer from quarantine",
    Reason.LOW_CONFIDENCE: "detection flagged low confidence by the provider",
    Reason.CLOCK_SKEW: "acquisition time slightly in the future (clock skew?)",
    Reason.OUT_OF_WINDOW: "acquired well before the requested window (stale feed?)",
    Reason.DAYNIGHT_MISMATCH: "day/night flag contradicts computed solar elevation",
    Reason.SAA_ARTIFACT: "weak night-time MODIS detection inside the South Atlantic Anomaly",
    Reason.MISSING_FRP: "fire radiative power missing",
    Reason.INSUFFICIENT_CLEAR_SKY: "too few cloud-free pixels for a reliable index",
    Reason.LOW_VALID_SAMPLES: "too few valid SAR samples",
}

# Rough South Atlantic Anomaly extent where MODIS night detections are often spurious.
SAA_BOX = BBox(-90.0, -50.0, 20.0, 0.0)


@dataclass
class Verdict:
    reasons: list[tuple[Reason, str]] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    def add(self, reason: Reason, detail: str) -> None:
        self.reasons.append((reason, detail))

    @property
    def action(self) -> Action:
        if any(r.hard for r, _ in self.reasons):
            return Action.REJECT
        if self.reasons:
            return Action.QUARANTINE
        return Action.ACCEPT

    @property
    def codes(self) -> list[str]:
        return [r.value for r, _ in self.reasons]

    @property
    def details(self) -> list[str]:
        return [f"{r.value}: {d}" for r, d in self.reasons]


@dataclass(frozen=True)
class ValidationContext:
    settings: Settings
    now: datetime
    aoi: BBox | None = None
    window_start: datetime | None = None
    check_window: bool = True
    ignore_soft: bool = False   # used when a reviewer releases a quarantined record


def _finite(*values: float | None) -> bool:
    return all(v is None or math.isfinite(v) for v in values)


def _check_coords(v: Verdict, lat: float, lon: float) -> bool:
    if not (_finite(lat, lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        v.add(Reason.INVALID_COORDINATES, f"lat={lat} lon={lon}")
        return False
    return True


def _check_time(v: Verdict, t: datetime, ctx: ValidationContext, label: str) -> None:
    s = ctx.settings
    if t > ctx.now + timedelta(hours=s.future_reject_hours):
        v.add(Reason.FUTURE_TIMESTAMP, f"{label} {t.isoformat()} is > {s.future_reject_hours}h ahead of now")
    elif t > ctx.now + timedelta(minutes=s.clock_skew_tolerance_min):
        v.add(Reason.CLOCK_SKEW, f"{label} {t.isoformat()} is ahead of now ({ctx.now.isoformat()})")


def validate_hotspot(h: Hotspot, ctx: ValidationContext, kind: RecordKind) -> Verdict:
    v = Verdict()
    s = ctx.settings
    if not _check_coords(v, h.latitude, h.longitude):
        return v
    if kind is RecordKind.HOTSPOT and ctx.aoi and not ctx.aoi.contains(h.latitude, h.longitude, margin_deg=0.01):
        v.add(Reason.OUT_OF_AOI, f"({h.latitude:.4f}, {h.longitude:.4f}) not in {ctx.aoi.key()}")
    _check_time(v, h.acquired_at, ctx, "acquired_at")
    if kind is RecordKind.HOTSPOT and ctx.check_window and ctx.window_start is not None:
        limit = ctx.window_start - timedelta(hours=s.out_of_window_tolerance_hours)
        if h.acquired_at < limit:
            v.add(Reason.OUT_OF_WINDOW, f"acquired {h.acquired_at.isoformat()} before {limit.isoformat()}")

    # Physics: brightness temperatures for fire channels are 200..600 K in practice.
    if not (_finite(h.brightness_k) and 200 <= h.brightness_k <= 600):
        v.add(Reason.UNPHYSICAL_VALUE, f"brightness {h.brightness_k} K outside 200..600")
    if h.brightness_aux_k is not None and not (_finite(h.brightness_aux_k) and 150 <= h.brightness_aux_k <= 400):
        v.add(Reason.UNPHYSICAL_VALUE, f"aux brightness {h.brightness_aux_k} K outside 150..400")
    if h.frp_mw is None:
        v.add(Reason.MISSING_FRP, "frp empty")
    elif not _finite(h.frp_mw) or h.frp_mw < 0 or h.frp_mw > 50_000:
        v.add(Reason.UNPHYSICAL_VALUE, f"frp {h.frp_mw} MW outside 0..50000")
    for name, val in (("scan", h.scan_km), ("track", h.track_km)):
        if val is not None and not (_finite(val) and 0 < val <= 10):
            v.add(Reason.UNPHYSICAL_VALUE, f"{name} {val} km outside (0, 10]")

    if h.confidence_class == "low":
        if s.quarantine_low_confidence:
            v.add(Reason.LOW_CONFIDENCE, f"confidence {h.confidence_raw!r}")
        else:
            v.flags.append("low_confidence")

    if h.daynight:
        elev = solar_elevation_deg(h.latitude, h.longitude, h.acquired_at)
        if (h.daynight == "D" and elev < -5) or (h.daynight == "N" and elev > 10):
            v.add(Reason.DAYNIGHT_MISMATCH, f"flag {h.daynight} but solar elevation {elev:.1f} deg")
        if h.daynight == "N" and h.sensor == "MODIS" and SAA_BOX.contains(h.latitude, h.longitude) \
                and (h.confidence < 0.5 or (h.frp_mw or 0) < 10):
            v.add(Reason.SAA_ARTIFACT, f"night MODIS conf={h.confidence_raw} frp={h.frp_mw}")

    # Non-blocking quality flags for the verifier.
    if h.sensor == "VIIRS" and h.brightness_k >= 367.0:
        v.flags.append("i4_saturated")
    if h.scan_km and ((h.sensor == "VIIRS" and h.scan_km > 0.6) or (h.sensor == "MODIS" and h.scan_km > 3.0)):
        v.flags.append("edge_of_scan")
    return v


def _check_index(v: Verdict, name: str, value: float | None) -> None:
    if value is not None and not (_finite(value) and -1.0 <= value <= 1.0):
        v.add(Reason.UNPHYSICAL_VALUE, f"{name}={value} outside -1..1")


def validate_optical(o: OpticalChange, ctx: ValidationContext, kind: RecordKind) -> Verdict:
    v = Verdict()
    if not _check_coords(v, o.latitude, o.longitude):
        return v
    for name in ("ndvi_pre", "ndvi_post", "nbr_pre", "nbr_post"):
        _check_index(v, name, getattr(o, name))
    if o.samples_pre == 0 and o.samples_post == 0:
        v.add(Reason.NO_DATA, "no Sentinel-2 samples in either window")
        return v
    min_clear = ctx.settings.min_clear_fraction
    for label, frac, samples in (("pre", o.valid_fraction_pre, o.samples_pre),
                                 ("post", o.valid_fraction_post, o.samples_post)):
        if samples and frac is not None and frac < min_clear:
            v.add(Reason.INSUFFICIENT_CLEAR_SKY, f"{label}-fire clear fraction {frac:.2f} < {min_clear}")
    if o.samples_pre == 0 or o.ndvi_pre is None:
        v.flags.append("no_pre_scene")
    if o.samples_post == 0 or o.ndvi_post is None:
        v.flags.append("no_post_scene")
    return v


def validate_sar(o: SarChange, ctx: ValidationContext, kind: RecordKind) -> Verdict:
    v = Verdict()
    if not _check_coords(v, o.latitude, o.longitude):
        return v
    for name in ("vv_pre_db", "vv_post_db", "vh_pre_db", "vh_post_db"):
        val = getattr(o, name)
        if val is not None and not (_finite(val) and -50 <= val <= 20):
            v.add(Reason.UNPHYSICAL_VALUE, f"{name}={val} dB outside -50..20")
    if o.samples_pre == 0 and o.samples_post == 0:
        v.add(Reason.NO_DATA, "no Sentinel-1 samples in either window")
        return v
    min_valid = ctx.settings.min_sar_valid_fraction
    for label, frac, samples in (("pre", o.valid_fraction_pre, o.samples_pre),
                                 ("post", o.valid_fraction_post, o.samples_post)):
        if samples and frac is not None and frac < min_valid:
            v.add(Reason.LOW_VALID_SAMPLES, f"{label} valid fraction {frac:.2f} < {min_valid}")
    if o.samples_pre == 0 or o.vh_pre_db is None:
        v.flags.append("no_pre_scene")
    if o.samples_post == 0 or o.vh_post_db is None:
        v.flags.append("no_post_scene")
    return v


def validate_overpass(o: Overpass, ctx: ValidationContext, kind: RecordKind) -> Verdict:
    v = Verdict()
    if not _check_coords(v, o.latitude, o.longitude):
        return v
    _check_time(v, o.sensing_start, ctx, "sensing_start")
    if o.cloud_cover is not None and not 0 <= o.cloud_cover <= 100:
        v.add(Reason.UNPHYSICAL_VALUE, f"cloud cover {o.cloud_cover} outside 0..100")
    return v


def validate_context(c: ContextFeature, ctx: ValidationContext, kind: RecordKind) -> Verdict:
    v = Verdict()
    if not _check_coords(v, c.latitude, c.longitude):
        return v
    if c.geometry is not None and c.geometry.get("type") not in VALID_GEOMETRY_TYPES:
        v.add(Reason.INVALID_GEOMETRY, f"geometry type {c.geometry.get('type')!r}")
    return v


_VALIDATORS: dict[RecordKind, Callable[[CanonicalRecord, ValidationContext, RecordKind], Verdict]] = {
    RecordKind.HOTSPOT: validate_hotspot,
    RecordKind.HISTORICAL_HOTSPOT: validate_hotspot,
    RecordKind.OPTICAL_CHANGE: validate_optical,
    RecordKind.SAR_CHANGE: validate_sar,
    RecordKind.OVERPASS: validate_overpass,
    RecordKind.CONTEXT_FEATURE: validate_context,
}


def validate(kind: RecordKind, record: CanonicalRecord, ctx: ValidationContext) -> Verdict:
    verdict = _VALIDATORS[kind](record, ctx, kind)
    if ctx.ignore_soft:
        released = [code for code in verdict.codes if not code.startswith("R_")]
        verdict.reasons = [(r, d) for r, d in verdict.reasons if r.hard]
        verdict.flags.extend(f"released:{code}" for code in released)
    return verdict
