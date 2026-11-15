"""Fit likelihood tables from labelled events, and score a table against labels.

Fitting is a Bayesian update of each P(state | class) row: the current table
acts as a Dirichlet prior worth ``strength`` pseudo-observations, so a few
labelled events nudge the expert values instead of replacing them, and no
probability can reach zero.

    p(s | c) = (count(s, c) + strength * p_prior(s | c)) / (n_c + strength)
"""

from __future__ import annotations

import copy
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

from fireverify.models import stable_hash, utcnow
from fireverify.verification.decision import DecisionPolicy
from fireverify.verification.evidence import GROUP_STATES, EvidenceVector
from fireverify.verification.model import LikelihoodTable, NaiveBayesFusion


@dataclass
class Example:
    evidence: EvidenceVector
    is_fire: bool
    truth_label: str | None = None


def fit(examples: list[Example], base: LikelihoodTable, strength: float = 10.0,
        prior_strength: float = 5.0, source: str = "labelled events") -> LikelihoodTable:
    if not examples:
        raise ValueError("no labelled examples to fit on")
    data = copy.deepcopy(base.data)
    for group, states in GROUP_STATES.items():
        for cls, flag in (("fire", True), ("not_fire", False)):
            observed = [e.evidence.states.get(group) for e in examples if e.is_fire is flag]
            counts = Counter(s for s in observed if s is not None)
            n = sum(counts.values())
            prior_row = base.data["groups"][group][cls]
            data["groups"][group][cls] = {
                s: (counts[s] + strength * prior_row[s]) / (n + strength) for s in states
            }
    # Land-cover priors: Beta update of P(fire | land cover).
    by_basis: dict[str, list[bool]] = defaultdict(list)
    for e in examples:
        _, basis = base.prior(e.evidence.land_cover)
        by_basis[basis].append(e.is_fire)
    for basis, labels in by_basis.items():
        old = data["default_prior"] if basis == "default" else data["land_cover_priors"][basis]
        new = (sum(labels) + prior_strength * old) / (len(labels) + prior_strength)
        new = min(0.99, max(0.01, new))
        if basis == "default":
            data["default_prior"] = new
        else:
            data["land_cover_priors"][basis] = new
    n_fire = sum(e.is_fire for e in examples)
    data["version"] = f"fitted-{utcnow():%Y%m%dT%H%M%S}"
    data["meta"] = {
        "source": source,
        "base_version": base.version,
        "fitted_examples": len(examples),
        "fire_examples": n_fire,
        "not_fire_examples": len(examples) - n_fire,
        "strength": strength,
        "prior_strength": prior_strength,
    }
    return LikelihoodTable(data)


def split(examples: list[Example], holdout: float, seed: int = 0) -> tuple[list[Example], list[Example]]:
    """Deterministic split by cluster id so a cluster never lands in both sets."""
    train, test = [], []
    for e in examples:
        bucket = int(stable_hash(seed, e.evidence.cluster_id, length=8), 16) / 0xFFFFFFFF
        (test if bucket < holdout else train).append(e)
    return train, test


def evaluate(examples: Iterable[Example], table: LikelihoodTable,
             policy: DecisionPolicy | None = None) -> dict[str, Any]:
    policy = policy or DecisionPolicy()
    fusion = NaiveBayesFusion(table)
    tp = fp = tn = fn = 0
    brier = log_loss = 0.0
    n = 0
    by_truth: dict[str, dict[str, Any]] = defaultdict(lambda: {"n": 0, "sum_p": 0.0, "labels": Counter()})
    decisions: Counter = Counter()
    for e in examples:
        post = fusion.fuse(e.evidence)
        p = post.probability
        label = policy.label(p, len(post.contributions))
        decisions[label] += 1
        y = 1.0 if e.is_fire else 0.0
        n += 1
        brier += (p - y) ** 2
        pc = min(1 - 1e-6, max(1e-6, p))
        log_loss -= y * math.log(pc) + (1 - y) * math.log(1 - pc)
        pred = p >= 0.5
        tp += pred and e.is_fire
        fp += pred and not e.is_fire
        tn += (not pred) and not e.is_fire
        fn += (not pred) and e.is_fire
        t = by_truth[e.truth_label or ("fire" if e.is_fire else "not_fire")]
        t["n"] += 1
        t["sum_p"] += p
        t["labels"][label] += 1
    if n == 0:
        return {"n": 0}
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = 2 * precision * recall / (precision + recall) if precision and recall else None
    return {
        "n": n,
        "table_version": table.version,
        "accuracy": round((tp + tn) / n, 4),
        "precision": round(precision, 4) if precision is not None else None,
        "recall": round(recall, 4) if recall is not None else None,
        "f1": round(f1, 4) if f1 is not None else None,
        "brier": round(brier / n, 4),
        "log_loss": round(log_loss / n, 4),
        "confusion": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        "decisions": dict(decisions),
        "by_truth": {k: {"n": v["n"], "mean_p": round(v["sum_p"] / v["n"], 4), "labels": dict(v["labels"])}
                     for k, v in sorted(by_truth.items())},
    }
