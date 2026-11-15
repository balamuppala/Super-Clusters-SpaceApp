"""Turn a posterior probability into an operational label and an explanation.

Thresholds encode the cost trade-off: missing a real fire is usually worse
than a false alert, so "likely_fire" starts at 0.60, not 0.90.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from fireverify.verification.model import Posterior

LABELS = ("confirmed_fire", "likely_fire", "uncertain", "likely_false_alarm", "false_alarm")

# (group, state) -> probable non-fire cause when that term pushes hardest against "fire"
CAUSES = {
    ("heat_source", "gas_flare"): "gas_flare",
    ("heat_source", "volcano"): "volcanic_activity",
    ("heat_source", "industrial"): "industrial_heat_source",
    ("temporal", "persistent"): "persistent_heat_source",
    ("spread", "stationary"): "stationary_heat_source",
    ("thermal", "single_weak"): "weak_single_detection",
    ("thermal", "single"): "single_detection",
    ("optical", "non_vegetated"): "non_vegetated_surface",
    ("optical", "unburned"): "no_vegetation_change",
    ("sar", "no_change"): "no_backscatter_change",
}
PRIOR_CAUSES = {"urban": "urban_hot_surface", "water": "water_surface_glint", "bare": "bare_hot_surface"}


@dataclass(frozen=True)
class DecisionPolicy:
    confirmed: float = 0.85
    likely: float = 0.60
    likely_false: float = 0.30
    false_alarm: float = 0.10
    min_groups_for_firm_label: int = 3   # fewer observed groups -> never "confirmed"/"false_alarm"

    def label(self, p: float, observed_groups: int) -> str:
        if p >= self.confirmed:
            label = "confirmed_fire"
        elif p >= self.likely:
            label = "likely_fire"
        elif p >= self.likely_false:
            label = "uncertain"
        elif p >= self.false_alarm:
            label = "likely_false_alarm"
        else:
            label = "false_alarm"
        if observed_groups < self.min_groups_for_firm_label:
            label = {"confirmed_fire": "likely_fire", "false_alarm": "likely_false_alarm"}.get(label, label)
        return label

    def as_dict(self) -> dict[str, Any]:
        return {"confirmed": self.confirmed, "likely": self.likely, "likely_false": self.likely_false,
                "false_alarm": self.false_alarm, "min_groups_for_firm_label": self.min_groups_for_firm_label}


def drivers(post: Posterior, n: int = 3) -> list[dict[str, Any]]:
    terms = [{"factor": f"{c.group}:{c.state}", "log_lr": round(c.log_lr, 3), "lr": round(c.lr, 3),
              "direction": "fire" if c.log_lr > 0 else "not_fire", "detail": c.detail}
             for c in post.contributions if abs(c.log_lr) > 1e-9]
    prior_shift = post.prior_log_odds  # relative to an uninformative 0.5 prior
    if abs(prior_shift) > 1e-9:
        terms.append({"factor": f"prior:{post.prior_basis}", "log_lr": round(prior_shift, 3),
                      "lr": round(math.exp(prior_shift), 3), "direction": "fire" if prior_shift > 0 else "not_fire",
                      "detail": f"P(fire) prior {post.prior:.2f}"})
    return sorted(terms, key=lambda t: -abs(t["log_lr"]))[:n]


def attribution(post: Posterior) -> str | None:
    """Most likely non-fire explanation, when the posterior leans against fire."""
    if post.probability >= 0.5:
        return None
    candidates = [(c.log_lr, CAUSES.get((c.group, c.state))) for c in post.contributions]
    if post.prior_basis in PRIOR_CAUSES:
        candidates.append((post.prior_log_odds, PRIOR_CAUSES[post.prior_basis]))
    candidates = [(score, cause) for score, cause in candidates if cause and score < 0]
    return min(candidates)[1] if candidates else "unexplained_non_fire"
