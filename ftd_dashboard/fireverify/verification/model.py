"""Naive Bayes evidence fusion in log-odds form.

    log-odds(fire | E) = log-odds(prior | land cover) + Σ_g log LR_g(state_g)
    LR_g(s) = P(s | fire) / P(s | not fire)

Hypothesis H: *the hotspot cluster is an active vegetation fire* (wildfire or
agricultural/prescribed burn). Gas flares, volcanoes, hot roofs and sensor
artefacts are "not fire".

The default tables are EXPERT ELICITED starting points, not measured values.
Refit them with ``calibration.fit`` on labelled real events before relying on
the probabilities.
"""

from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fireverify.models import stable_hash
from fireverify.verification.evidence import GROUP_STATES, EvidenceVector

DEFAULT_TABLE: dict[str, Any] = {
    "version": "expert-1.0",
    "hypothesis": "active vegetation fire (wildfire, agricultural or prescribed burn)",
    "default_prior": 0.30,
    # Checked in this order; the first land cover the event lies in sets the prior.
    "land_cover_priority": ["water", "urban", "bare", "wetland", "cropland", "grassland", "shrubland", "forest"],
    "land_cover_priors": {
        "water": 0.05, "urban": 0.10, "bare": 0.10, "wetland": 0.35,
        "cropland": 0.50, "grassland": 0.55, "shrubland": 0.55, "forest": 0.55,
    },
    "max_lr": 50.0,
    "groups": {
        # P(state | fire) and P(state | not fire); each row sums to 1.
        "optical": {
            "fire":     {"high": 0.25, "moderate": 0.25, "low": 0.22, "unburned": 0.20, "non_vegetated": 0.08},
            "not_fire": {"high": 0.015, "moderate": 0.035, "low": 0.10, "unburned": 0.50, "non_vegetated": 0.35},
        },
        "sar": {
            "fire":     {"strong_drop": 0.35, "drop": 0.30, "no_change": 0.30, "increase": 0.05},
            "not_fire": {"strong_drop": 0.04, "drop": 0.10, "no_change": 0.76, "increase": 0.10},
        },
        "temporal": {
            "fire":     {"none": 0.55, "sporadic": 0.20, "seasonal": 0.20, "persistent": 0.05},
            "not_fire": {"none": 0.25, "sporadic": 0.15, "seasonal": 0.15, "persistent": 0.45},
        },
        "heat_source": {
            "fire":     {"gas_flare": 0.005, "volcano": 0.01, "industrial": 0.05, "none": 0.935},
            "not_fire": {"gas_flare": 0.25, "volcano": 0.12, "industrial": 0.18, "none": 0.45},
        },
        "thermal": {
            "fire":     {"single_weak": 0.05, "single": 0.15, "repeat": 0.30, "multi_sensor_repeat": 0.50},
            "not_fire": {"single_weak": 0.30, "single": 0.25, "repeat": 0.20, "multi_sensor_repeat": 0.25},
        },
        "spread": {
            "fire":     {"stationary": 0.10, "compact": 0.50, "spreading": 0.40},
            "not_fire": {"stationary": 0.45, "compact": 0.45, "spreading": 0.10},
        },
    },
    "meta": {"source": "expert elicitation", "fitted_examples": 0},
}


def _logit(p: float) -> float:
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1 / (1 + math.exp(-x))
    z = math.exp(x)
    return z / (1 + z)


class LikelihoodTable:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.validate()

    @classmethod
    def default(cls) -> "LikelihoodTable":
        return cls(copy.deepcopy(DEFAULT_TABLE))

    @classmethod
    def load(cls, path: str | Path) -> "LikelihoodTable":
        return cls(json.loads(Path(path).read_text()))

    @classmethod
    def load_or_default(cls, path: str | Path | None) -> "LikelihoodTable":
        if path and Path(path).is_file():
            return cls.load(path)
        return cls.default()

    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(self.data, indent=2, sort_keys=True))

    def validate(self) -> None:
        groups = self.data.get("groups", {})
        missing = set(GROUP_STATES) - set(groups)
        if missing:
            raise ValueError(f"likelihood table missing groups: {sorted(missing)}")
        for group, states in GROUP_STATES.items():
            for cls in ("fire", "not_fire"):
                row = groups[group].get(cls, {})
                if set(row) != set(states):
                    raise ValueError(f"{group}/{cls}: states {sorted(row)} != {sorted(states)}")
                if any(p <= 0 for p in row.values()):
                    raise ValueError(f"{group}/{cls}: probabilities must be > 0")
                if abs(sum(row.values()) - 1) > 1e-6:
                    raise ValueError(f"{group}/{cls}: probabilities sum to {sum(row.values()):.4f}, not 1")
        for p in [self.data["default_prior"], *self.data["land_cover_priors"].values()]:
            if not 0 < p < 1:
                raise ValueError("priors must be strictly between 0 and 1")

    @property
    def version(self) -> str:
        return self.data.get("version", "unknown")

    @property
    def hash(self) -> str:
        return stable_hash(self.data, length=12)

    def p(self, group: str, state: str, cls: str) -> float:
        return self.data["groups"][group][cls][state]

    def lr(self, group: str, state: str) -> float:
        cap = float(self.data.get("max_lr", 50.0))
        raw = self.p(group, state, "fire") / self.p(group, state, "not_fire")
        return min(cap, max(1 / cap, raw))

    def prior(self, land_cover: list[str]) -> tuple[float, str]:
        priors = self.data["land_cover_priors"]
        for lc in self.data["land_cover_priority"]:
            if lc in land_cover and lc in priors:
                return priors[lc], lc
        return self.data["default_prior"], "default"


@dataclass
class Contribution:
    group: str
    state: str
    lr: float
    log_lr: float
    p_given_fire: float
    p_given_not_fire: float
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"group": self.group, "state": self.state, "lr": round(self.lr, 4), "log_lr": round(self.log_lr, 4),
                "p_given_fire": self.p_given_fire, "p_given_not_fire": self.p_given_not_fire, "detail": self.detail}


@dataclass
class Posterior:
    probability: float
    prior: float
    prior_basis: str
    log_odds: float
    contributions: list[Contribution] = field(default_factory=list)
    missing: dict[str, str] = field(default_factory=dict)

    @property
    def prior_log_odds(self) -> float:
        return _logit(self.prior)

    def as_dict(self) -> dict[str, Any]:
        return {"probability": self.probability, "prior": self.prior, "prior_basis": self.prior_basis,
                "log_odds": round(self.log_odds, 4), "contributions": [c.as_dict() for c in self.contributions],
                "missing": self.missing}


class NaiveBayesFusion:
    def __init__(self, table: LikelihoodTable | None = None) -> None:
        self.table = table or LikelihoodTable.default()

    def fuse(self, ev: EvidenceVector) -> Posterior:
        prior, basis = self.table.prior(ev.land_cover)
        log_odds = _logit(prior)
        contributions, missing = [], {}
        for group in GROUP_STATES:
            state = ev.states.get(group)
            if state is None:
                missing[group] = ev.details.get(group, "missing")
                continue
            lr = self.table.lr(group, state)
            log_lr = math.log(lr)
            log_odds += log_lr
            contributions.append(Contribution(
                group, state, lr, log_lr, self.table.p(group, state, "fire"),
                self.table.p(group, state, "not_fire"), ev.details.get(group, "")))
        return Posterior(_sigmoid(log_odds), prior, basis, log_odds, contributions, missing)
