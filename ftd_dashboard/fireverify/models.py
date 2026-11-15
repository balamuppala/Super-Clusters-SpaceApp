"""Core data model: lanes, raw envelopes and canonical (normalized) records.

Normalization (raw -> canonical) only parses types. Physical / plausibility
checks live in ``ingestion.validation`` so every rejection carries an explicit
reason code instead of a generic pydantic error.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def stable_hash(*parts: Any, length: int = 24) -> str:
    blob = json.dumps(parts, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:length]


class Lane(str, Enum):
    """Where a record lives. LIVE and SIMULATED are *origin* lanes and never mix."""

    LIVE = "live"
    SIMULATED = "simulated"
    DERIVED = "derived"
    QUARANTINED = "quarantined"
    REJECTED = "rejected"


class Mode(str, Enum):
    LIVE = "live"
    SIMULATED = "simulated"

    @property
    def lane(self) -> Lane:
        return Lane.LIVE if self is Mode.LIVE else Lane.SIMULATED


class RecordKind(str, Enum):
    HOTSPOT = "hotspot"                        # VIIRS / MODIS active-fire pixel (NRT)
    HISTORICAL_HOTSPOT = "historical_hotspot"  # archive detection used for temporal frequency
    OPTICAL_CHANGE = "optical_change"          # Sentinel-2 NDVI / NBR pre vs post
    SAR_CHANGE = "sar_change"                  # Sentinel-1 backscatter pre vs post
    OVERPASS = "overpass"                      # scene / granule coverage (e.g. Sentinel-3 SLSTR FRP)
    CONTEXT_FEATURE = "context_feature"        # land cover, industry, volcanoes, urban areas...


class Action(str, Enum):
    ACCEPT = "accept"
    QUARANTINE = "quarantine"
    REJECT = "reject"


@dataclass(frozen=True)
class BBox:
    west: float
    south: float
    east: float
    north: float

    def __post_init__(self) -> None:
        for v in (self.west, self.south, self.east, self.north):
            if not math.isfinite(v):
                raise ValueError("bbox values must be finite")
        if not (-180 <= self.west < self.east <= 180):
            raise ValueError(f"invalid longitude range {self.west}..{self.east} (antimeridian crossing not supported)")
        if not (-90 <= self.south < self.north <= 90):
            raise ValueError(f"invalid latitude range {self.south}..{self.north}")

    @classmethod
    def parse(cls, text: str) -> "BBox":
        parts = [float(p) for p in text.split(",")]
        if len(parts) != 4:
            raise ValueError("bbox must be 'west,south,east,north'")
        return cls(*parts)

    @classmethod
    def around(cls, lat: float, lon: float, radius_km: float) -> "BBox":
        dlat = radius_km / 110.574
        dlon = radius_km / (111.320 * max(math.cos(math.radians(lat)), 0.01))
        return cls(
            max(-180.0, lon - dlon), max(-90.0, lat - dlat), min(180.0, lon + dlon), min(90.0, lat + dlat)
        )

    def expand(self, deg: float) -> "BBox":
        return BBox(
            max(-180.0, self.west - deg), max(-90.0, self.south - deg),
            min(180.0, self.east + deg), min(90.0, self.north + deg),
        )

    def contains(self, lat: float, lon: float, margin_deg: float = 0.0) -> bool:
        return (self.south - margin_deg <= lat <= self.north + margin_deg) and (
            self.west - margin_deg <= lon <= self.east + margin_deg
        )

    @property
    def area_deg2(self) -> float:
        return (self.east - self.west) * (self.north - self.south)

    @property
    def center(self) -> tuple[float, float]:
        return (self.south + self.north) / 2, (self.west + self.east) / 2

    def as_list(self) -> list[float]:
        return [self.west, self.south, self.east, self.north]

    def as_firms(self) -> str:
        return ",".join(f"{v:.5f}".rstrip("0").rstrip(".") for v in self.as_list())

    def as_wkt(self) -> str:
        w, s, e, n = self.as_list()
        return f"POLYGON(({w} {s},{e} {s},{e} {n},{w} {n},{w} {s}))"

    def key(self) -> str:
        return ",".join(f"{v:.4f}" for v in self.as_list())


@dataclass(frozen=True)
class EnrichmentTarget:
    """A clustered fire event that per-location sources (S1/S2/S3/history) are queried for."""

    cluster_id: str
    latitude: float
    longitude: float
    event_start: datetime
    event_end: datetime
    radius_km: float
    member_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "event_start": self.event_start.isoformat(),
            "event_end": self.event_end.isoformat(),
            "radius_km": self.radius_km,
        }


@dataclass
class RawRecord:
    """A record exactly as received from a source (live API or simulator)."""

    source: str              # e.g. VIIRS_SNPP_NRT, S2_L2A_NDVI
    parser: str              # key into the parser registry
    kind: RecordKind
    payload: dict[str, Any]
    lane: Lane               # origin lane: LIVE or SIMULATED
    fetched_at: datetime
    target_id: str | None = None
    sim_truth: dict[str, Any] | None = None
    raw_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.lane not in (Lane.LIVE, Lane.SIMULATED):
            raise ValueError("raw records must originate from the LIVE or SIMULATED lane")
        # target_id and sim_truth are deliberately excluded: the same archive
        # detection fetched for two clusters is still one observation.
        self.raw_hash = stable_hash(self.source, self.kind.value, self.payload)


# --------------------------------------------------------------------------
# Canonical records
# --------------------------------------------------------------------------


class _Canonical(BaseModel):
    model_config = ConfigDict(frozen=True)


class Hotspot(_Canonical):
    source: str
    sensor: str               # VIIRS | MODIS
    platform: str             # Suomi-NPP | NOAA-20 | NOAA-21 | Terra | Aqua
    latitude: float
    longitude: float
    acquired_at: datetime
    brightness_k: float       # VIIRS I4 / MODIS band 21-22 brightness temperature
    brightness_aux_k: float | None = None  # VIIRS I5 / MODIS band 31
    frp_mw: float | None = None
    scan_km: float | None = None
    track_km: float | None = None
    confidence_raw: str
    confidence: float         # normalized 0..1
    confidence_class: str     # low | nominal | high
    daynight: str | None = None
    version: str | None = None

    @property
    def observed_at(self) -> datetime:
        return self.acquired_at


class OpticalChange(_Canonical):
    source: str
    platform: str = "Sentinel-2"
    latitude: float
    longitude: float
    buffer_m: float
    pre_from: datetime
    pre_to: datetime
    post_from: datetime
    post_to: datetime
    ndvi_pre: float | None = None
    ndvi_post: float | None = None
    nbr_pre: float | None = None
    nbr_post: float | None = None
    valid_fraction_pre: float | None = None
    valid_fraction_post: float | None = None
    samples_pre: int = 0
    samples_post: int = 0

    @property
    def observed_at(self) -> datetime:
        return self.post_to


class SarChange(_Canonical):
    source: str
    platform: str = "Sentinel-1"
    latitude: float
    longitude: float
    buffer_m: float
    pre_from: datetime
    pre_to: datetime
    post_from: datetime
    post_to: datetime
    vv_pre_db: float | None = None
    vv_post_db: float | None = None
    vh_pre_db: float | None = None
    vh_post_db: float | None = None
    valid_fraction_pre: float | None = None
    valid_fraction_post: float | None = None
    samples_pre: int = 0
    samples_post: int = 0

    @property
    def observed_at(self) -> datetime:
        return self.post_to


class Overpass(_Canonical):
    source: str
    mission: str
    platform: str
    product_type: str
    product_id: str
    product_name: str
    sensing_start: datetime
    sensing_end: datetime | None = None
    latitude: float           # the target location the scene was searched for
    longitude: float
    cloud_cover: float | None = None

    @property
    def observed_at(self) -> datetime:
        return self.sensing_start


class ContextFeature(_Canonical):
    source: str
    feature_id: str
    category: str             # volcano | gas_flare | industrial | power_plant | landfill | urban | forest | cropland | ...
    name: str | None = None
    latitude: float
    longitude: float
    geometry: dict[str, Any] | None = None
    tags: dict[str, Any] = {}

    @property
    def observed_at(self) -> datetime | None:
        return None


CanonicalRecord = Hotspot | OpticalChange | SarChange | Overpass | ContextFeature


@dataclass
class IngestedRecord:
    record_id: str
    raw: RawRecord
    model: CanonicalRecord
    flags: list[str] = field(default_factory=list)

    @property
    def latitude(self) -> float:
        return self.model.latitude

    @property
    def longitude(self) -> float:
        return self.model.longitude

    @property
    def observed_at(self) -> datetime | None:
        return self.model.observed_at


_KIND_PREFIX = {
    RecordKind.HOTSPOT: "hs",
    RecordKind.HISTORICAL_HOTSPOT: "hh",
    RecordKind.OPTICAL_CHANGE: "oc",
    RecordKind.SAR_CHANGE: "sc",
    RecordKind.OVERPASS: "op",
    RecordKind.CONTEXT_FEATURE: "cf",
}


def record_id_for(raw: RawRecord) -> str:
    return f"{_KIND_PREFIX[raw.kind]}_{raw.raw_hash[:16]}"


@dataclass
class DerivedFeature:
    cluster_id: str
    feature: str
    value: dict[str, Any]
    inputs: list[str]
    origin_lane: Lane
    algorithm_version: str
    computed_at: datetime

    @property
    def derived_id(self) -> str:
        return "dv_" + stable_hash(self.cluster_id, self.feature, self.value, self.algorithm_version, length=16)
