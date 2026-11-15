"""Verification stage: derived lane -> evidence -> naive Bayes posterior -> verdict."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from fireverify.ingestion.store import Store
from fireverify.models import Lane, stable_hash, utcnow
from fireverify.verification.calibration import Example
from fireverify.verification.decision import DecisionPolicy, attribution, drivers
from fireverify.verification.evidence import EvidenceVector, extract_evidence
from fireverify.verification.model import LikelihoodTable, NaiveBayesFusion, Posterior

MODEL_VERSION = "naive-bayes-1.0"


@dataclass
class Verdict:
    cluster_id: str
    lane: Lane
    probability: float
    label: str
    attribution: str | None
    top_drivers: list[dict[str, Any]]
    posterior: Posterior
    evidence: EvidenceVector
    table_version: str
    table_hash: str
    derived_ids: list[str]
    policy: dict[str, Any]
    computed_at: datetime = field(default_factory=utcnow)
    model_version: str = MODEL_VERSION

    @property
    def verdict_id(self) -> str:
        return "vd_" + stable_hash(self.lane.value, self.cluster_id, sorted(self.derived_ids), self.table_hash,
                                   self.policy, self.model_version, length=16)

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict_id": self.verdict_id, "cluster_id": self.cluster_id, "lane": self.lane.value,
            "probability": round(self.probability, 6), "label": self.label, "attribution": self.attribution,
            "top_drivers": self.top_drivers, "posterior": self.posterior.as_dict(),
            "evidence": self.evidence.as_dict(), "model_version": self.model_version,
            "table_version": self.table_version, "table_hash": self.table_hash,
            "derived_ids": self.derived_ids, "policy": self.policy, "computed_at": self.computed_at.isoformat(),
        }


class Verifier:
    def __init__(self, store: Store, table: LikelihoodTable | None = None,
                 policy: DecisionPolicy | None = None) -> None:
        self.store = store
        self.table = table or LikelihoodTable.default()
        self.policy = policy or DecisionPolicy()
        self.fusion = NaiveBayesFusion(self.table)

    def _derived(self, lane: Lane, cluster_id: str) -> tuple[dict[str, Any], list[str]] | None:
        rows = self.store.latest_derived(lane, cluster_id)
        if "cluster_summary" not in rows:
            return None
        return {k: v["value"] for k, v in rows.items()}, [v["derived_id"] for v in rows.values()]

    def evidence_for(self, lane: Lane, cluster_id: str) -> EvidenceVector | None:
        got = self._derived(lane, cluster_id)
        return extract_evidence(cluster_id, got[0]) if got else None

    def verify_cluster(self, lane: Lane, cluster_id: str, persist: bool = True) -> Verdict | None:
        got = self._derived(lane, cluster_id)
        if got is None:
            return None
        derived, derived_ids = got
        ev = extract_evidence(cluster_id, derived)
        post = self.fusion.fuse(ev)
        verdict = Verdict(
            cluster_id=cluster_id, lane=lane, probability=post.probability,
            label=self.policy.label(post.probability, len(post.contributions)),
            attribution=attribution(post), top_drivers=drivers(post), posterior=post, evidence=ev,
            table_version=self.table.version, table_hash=self.table.hash, derived_ids=derived_ids,
            policy=self.policy.as_dict(),
        )
        if persist:
            self.store.insert_verdict(verdict.as_dict())
        return verdict

    def verify_all(self, lane: Lane, limit: int = 10_000) -> list[Verdict]:
        out = []
        for c in self.store.list_clusters(lane, limit=limit):
            v = self.verify_cluster(lane, c["cluster_id"])
            if v:
                out.append(v)
        return out

    # ------------------------------------------------------- labelled data

    def _sim_truth(self, lane: Lane, member_ids: list[str]) -> tuple[bool, str] | None:
        rows = self.store.query_records(lane, ids=member_ids)
        truths = [r["sim_truth"] for r in rows if r.get("sim_truth") and "is_vegetation_fire" in r["sim_truth"]]
        if not truths:
            return None
        label, _ = Counter(t["label"] for t in truths).most_common(1)[0]
        is_fire = sum(t["is_vegetation_fire"] for t in truths) * 2 >= len(truths)
        return is_fire, label

    def examples(self, lane: Lane, labels: dict[str, bool] | None = None) -> list[Example]:
        """Labelled examples: explicit ``labels`` (cluster_id -> is_fire), else simulation ground truth."""
        out = []
        for c in self.store.list_clusters(lane, limit=100_000):
            cid = c["cluster_id"]
            ev = self.evidence_for(lane, cid)
            if ev is None:
                continue
            if labels is not None:
                if cid in labels:
                    out.append(Example(ev, labels[cid]))
                continue
            truth = self._sim_truth(lane, c["member_ids"])
            if truth:
                out.append(Example(ev, truth[0], truth[1]))
        return out
