"""Turn derived features into discrete evidence states, one per *evidence group*.

Naive Bayes assumes evidence terms are independent given the true class.
Strongly correlated features are therefore merged into one group, so each
physical signal is counted once:

optical      dNBR / dNDVI from the *same* Sentinel-2 scenes -> one state
heat_source  flare + industrial polygon usually co-occur -> strongest one wins
thermal      detection count, sensor count and FRP describe the same thermal signal

Land cover is used for the prior, not as evidence, to avoid counting it twice.
A group whose data is missing, quarantined or incomplete gets state ``None``
and drops out of the product (likelihood ratio 1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

GROUP_STATES: dict[str, tuple[str, ...]] = {
    "optical": ("high", "moderate", "low", "unburned", "non_vegetated"),
    "sar": ("strong_drop", "drop", "no_change", "increase"),
    "temporal": ("none", "sporadic", "seasonal", "persistent"),
    "heat_source": ("gas_flare", "volcano", "industrial", "none"),
    "thermal": ("single_weak", "single", "repeat", "multi_sensor_repeat"),
    "spread": ("stationary", "compact", "spreading"),
}

NON_VEGETATED_NDVI = 0.2
WEAK_FRP_MW = 3.0


@dataclass
class EvidenceVector:
    cluster_id: str
    states: dict[str, str | None] = field(default_factory=dict)
    details: dict[str, str] = field(default_factory=dict)   # what was observed, or why it is missing
    land_cover: list[str] = field(default_factory=list)

    @property
    def observed(self) -> dict[str, str]:
        return {g: s for g, s in self.states.items() if s is not None}

    def as_dict(self) -> dict[str, Any]:
        return {"states": self.states, "details": self.details, "land_cover": self.land_cover}


def _optical(v: dict[str, Any]) -> tuple[str | None, str]:
    status = v.get("status")
    if status != "complete":
        reasons = ",".join(v.get("quarantine_reasons", []))
        return None, f"{status}{' (' + reasons + ')' if reasons else ''}"
    ndvi_pre = v.get("ndvi_pre")
    if ndvi_pre is not None and ndvi_pre < NON_VEGETATED_NDVI:
        return "non_vegetated", f"pre-fire NDVI {ndvi_pre:.2f} < {NON_VEGETATED_NDVI}"
    dnbr = v.get("dnbr")
    if dnbr is not None:
        state = "high" if dnbr >= 0.44 else "moderate" if dnbr >= 0.27 else "low" if dnbr >= 0.10 else "unburned"
        return state, f"dNBR {dnbr:+.3f}, dNDVI {v.get('dndvi', 0):+.3f}"
    dndvi = v.get("dndvi")
    if dndvi is None:
        return None, "no index values"
    state = "high" if dndvi >= 0.30 else "moderate" if dndvi >= 0.15 else "low" if dndvi >= 0.07 else "unburned"
    return state, f"dNDVI {dndvi:+.3f} (no NBR)"


def _sar(v: dict[str, Any]) -> tuple[str | None, str]:
    if v.get("status") != "complete":
        reasons = ",".join(v.get("quarantine_reasons", []))
        return None, f"{v.get('status')}{' (' + reasons + ')' if reasons else ''}"
    dvh = v["dvh_db"]
    state = "strong_drop" if dvh <= -2.0 else "drop" if dvh <= -1.0 else "no_change" if dvh < 1.0 else "increase"
    return state, f"dVH {dvh:+.2f} dB"


def _temporal(v: dict[str, Any]) -> tuple[str | None, str]:
    cls = v.get("recurrence_class")
    if cls not in GROUP_STATES["temporal"]:
        return None, "history not checked" if not v.get("history_checked") else f"class {cls}"
    return cls, (f"{v.get('distinct_days', 0)} prior days over {v.get('distinct_years', 0)} of "
                 f"{v.get('years_sampled')} years within {v.get('radius_km')} km")


def _heat_source(c: dict[str, Any]) -> tuple[str | None, str]:
    if not c.get("features_considered"):
        return None, "no contextual layers in range"
    nearest = c.get("nearest", {})

    def dist(cat: str) -> str:
        n = nearest.get(cat)
        return f"{cat} {n['distance_km']:.2f} km ({n.get('name') or n['feature_id']})" if n else cat

    if c.get("near_gas_flare"):
        return "gas_flare", dist("gas_flare")
    if c.get("near_volcano"):
        return "volcano", dist("volcano")
    for cat in ("industrial", "power_plant", "landfill"):
        if c.get(f"near_{cat}"):
            return "industrial", dist(cat)
    return "none", f"no known heat source within buffers ({c['features_considered']} features checked)"


def _thermal(summary: dict[str, Any]) -> tuple[str, str]:
    n_over = summary.get("n_overpasses", 1)
    n_sensors = len(summary.get("sensors", []))
    max_frp = summary.get("max_frp_mw")
    detail = f"{summary.get('n_detections')} detections, {n_over} overpasses, sensors {'+'.join(summary.get('sensors', []))}, max FRP {max_frp} MW"
    if n_over <= 1:
        return ("single_weak" if max_frp is None or max_frp < WEAK_FRP_MW else "single"), detail
    return ("multi_sensor_repeat" if n_sensors >= 2 else "repeat"), detail


def _spread(summary: dict[str, Any]) -> tuple[str, str]:
    extent = max(0.0, summary.get("radius_km", 0.5) - 0.5)   # radius_km includes a 0.5 km pixel margin
    n_over = summary.get("n_overpasses", 1)
    detail = f"extent {extent:.2f} km over {n_over} overpasses / {summary.get('duration_hours')} h"
    if n_over >= 4 and extent <= 1.0:
        return "stationary", detail
    if extent >= 1.5:
        return "spreading", detail
    return "compact", detail


def extract_evidence(cluster_id: str, derived: dict[str, dict[str, Any]]) -> EvidenceVector:
    """``derived`` maps feature name -> feature value (the derived lane for one cluster)."""
    ev = EvidenceVector(cluster_id)
    summary = derived["cluster_summary"]
    extractors = {
        "optical": lambda: _optical(derived.get("vegetation_loss", {"status": "unavailable"})),
        "sar": lambda: _sar(derived.get("sar_backscatter_change", {"status": "unavailable"})),
        "temporal": lambda: _temporal(derived.get("temporal_frequency", {})),
        "heat_source": lambda: _heat_source(derived.get("context_proximity", {})),
        "thermal": lambda: _thermal(summary),
        "spread": lambda: _spread(summary),
    }
    for group, fn in extractors.items():
        ev.states[group], ev.details[group] = fn()
    ev.land_cover = list(derived.get("context_proximity", {}).get("land_cover", []))
    return ev
