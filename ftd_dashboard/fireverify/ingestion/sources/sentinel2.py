"""Sentinel-2 L2A vegetation change (NDVI / NBR) before vs. after a fire event.

Clouds, cloud shadow and cirrus are masked with the Scene Classification Layer
so smoke/cloud contamination shows up as a low valid-pixel fraction instead of
a fake NDVI drop.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterator

from fireverify.config import Settings
from fireverify.ingestion.base import FetchRequest, Scope, Source, SourceError
from fireverify.ingestion.http import HttpClient
from fireverify.ingestion.sources.copernicus import (
    CdseAuth, extract_stats, iso_z, parse_iso, stats_body, statistical_request,
)
from fireverify.models import BBox, EnrichmentTarget, OpticalChange, RawRecord, RecordKind

S2_EVALSCRIPT = """//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B04", "B08", "B12", "SCL", "dataMask"] }],
    output: [
      { id: "ndvi", bands: 1, sampleType: "FLOAT32" },
      { id: "nbr", bands: 1, sampleType: "FLOAT32" },
      { id: "dataMask", bands: 1 }
    ]
  };
}
function evaluatePixel(s) {
  // SCL: 3 cloud shadow, 8/9 cloud medium/high probability, 10 thin cirrus
  const cloudy = [3, 8, 9, 10].includes(s.SCL);
  const nir_red = s.B08 + s.B04;
  const nir_swir = s.B08 + s.B12;
  const valid = s.dataMask === 1 && !cloudy && nir_red > 0 && nir_swir > 0;
  return {
    ndvi: [valid ? (s.B08 - s.B04) / nir_red : 0],
    nbr: [valid ? (s.B08 - s.B12) / nir_swir : 0],
    dataMask: [valid ? 1 : 0]
  };
}
"""


def change_windows(target: EnrichmentTarget, now: datetime, pre_days: int, gap_days: int,
                   post_days: int) -> dict[str, datetime]:
    pre_to = target.event_start - timedelta(days=gap_days)
    return {
        "pre_from": pre_to - timedelta(days=pre_days),
        "pre_to": pre_to,
        "post_from": target.event_start,
        "post_to": min(now, target.event_end + timedelta(days=post_days)),
    }


class Sentinel2NdviSource(Source):
    name = "S2_L2A_NDVI"
    parser = "s2_stats"
    kind = RecordKind.OPTICAL_CHANGE
    scope = Scope.TARGET

    def __init__(self, settings: Settings, http: HttpClient | None) -> None:
        self.settings = settings
        self.http = http
        self._auth: CdseAuth | None = None

    def available(self) -> tuple[bool, str]:
        if not self.settings.has_cdse:
            return False, "CDSE_CLIENT_ID / CDSE_CLIENT_SECRET not set"
        return True, "ok"

    def windows(self, target: EnrichmentTarget, now: datetime) -> dict[str, datetime]:
        s = self.settings
        return change_windows(target, now, s.s2_pre_days, s.s2_gap_days, s.s2_post_days)

    def _query(self, bbox: BBox, start: datetime, end: datetime) -> dict[str, Any]:
        if self._auth is None:
            self._auth = CdseAuth(self.http, self.settings.cdse_client_id, self.settings.cdse_client_secret)
        body = stats_body(
            bbox, "sentinel-2-l2a",
            {"mosaickingOrder": "leastCC", "maxCloudCoverage": self.settings.s2_max_cloud},
            S2_EVALSCRIPT, start, end, self.settings.s2_res_deg,
        )
        return statistical_request(self.http, self._auth, body)

    def _responses(self, target: EnrichmentTarget, bbox: BBox, w: dict[str, datetime]):
        pre = self._query(bbox, w["pre_from"], w["pre_to"])
        post = self._query(bbox, w["post_from"], w["post_to"]) if w["post_to"] > w["post_from"] else None
        return pre, post, None

    def fetch(self, req: FetchRequest) -> Iterator[RawRecord]:
        target = req.target
        if target is None:
            raise SourceError("Sentinel-2 change requires an enrichment target")
        buffer_m = max(self.settings.s2_buffer_m, target.radius_km * 1000)
        bbox = BBox.around(target.latitude, target.longitude, buffer_m / 1000)
        w = self.windows(target, req.now)
        pre, post, truth = self._responses(target, bbox, w)
        payload = {
            "target": target.as_dict(),
            "buffer_m": buffer_m,
            "windows": {k: iso_z(v) for k, v in w.items()},
            "pre_response": pre,
            "post_response": post,
        }
        yield self._raw(payload, target_id=target.cluster_id, sim_truth=truth)


def normalize_s2(raw: RawRecord) -> OpticalChange:
    p = raw.payload
    w = p.get("windows") or {}
    target = p.get("target") or {}
    ndvi_pre = extract_stats(p.get("pre_response"), "ndvi")
    ndvi_post = extract_stats(p.get("post_response"), "ndvi")
    nbr_pre = extract_stats(p.get("pre_response"), "nbr")
    nbr_post = extract_stats(p.get("post_response"), "nbr")
    return OpticalChange(
        source=raw.source,
        latitude=float(target["latitude"]),
        longitude=float(target["longitude"]),
        buffer_m=float(p.get("buffer_m", 0)),
        pre_from=parse_iso(w.get("pre_from"), "pre_from"),
        pre_to=parse_iso(w.get("pre_to"), "pre_to"),
        post_from=parse_iso(w.get("post_from"), "post_from"),
        post_to=parse_iso(w.get("post_to"), "post_to"),
        ndvi_pre=ndvi_pre["mean"],
        ndvi_post=ndvi_post["mean"],
        nbr_pre=nbr_pre["mean"],
        nbr_post=nbr_post["mean"],
        valid_fraction_pre=ndvi_pre["valid_fraction"],
        valid_fraction_post=ndvi_post["valid_fraction"],
        samples_pre=ndvi_pre["samples"],
        samples_post=ndvi_post["samples"],
    )
