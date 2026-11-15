"""Source abstraction shared by live and simulated connectors.

A simulated connector subclasses its live counterpart and only overrides how
raw payloads are obtained. Parsing/normalization is identical, so simulated
data exercises exactly the same code path that live data takes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Iterator

from fireverify.models import BBox, EnrichmentTarget, Lane, RawRecord, RecordKind, utcnow


class SourceError(Exception):
    """A source could not be reached or returned something unusable."""


class ParseError(Exception):
    """A raw payload could not be normalized into a canonical record."""


class Scope(str, Enum):
    AOI = "aoi"        # fetched once per run for the whole area of interest
    TARGET = "target"  # fetched per clustered fire event


@dataclass(frozen=True)
class FetchRequest:
    aoi: BBox
    start: datetime
    end: datetime
    now: datetime
    target: EnrichmentTarget | None = None


class Source(ABC):
    name: str
    parser: str
    kind: RecordKind
    scope: Scope
    lane: Lane = Lane.LIVE
    # When set, AOI sources are skipped if fetched for the same AOI within this interval.
    refresh_interval: timedelta | None = None

    def available(self) -> tuple[bool, str]:
        return True, "ok"

    @abstractmethod
    def fetch(self, req: FetchRequest) -> Iterator[RawRecord]:
        ...

    def _raw(
        self,
        payload: dict[str, Any],
        *,
        kind: RecordKind | None = None,
        source: str | None = None,
        target_id: str | None = None,
        sim_truth: dict[str, Any] | None = None,
    ) -> RawRecord:
        return RawRecord(
            source=source or self.name,
            parser=self.parser,
            kind=kind or self.kind,
            payload=payload,
            lane=self.lane,
            fetched_at=utcnow(),
            target_id=target_id,
            sim_truth=sim_truth,
        )

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name} lane={self.lane.value}>"
