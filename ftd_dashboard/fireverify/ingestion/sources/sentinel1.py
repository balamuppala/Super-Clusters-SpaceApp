"""Sentinel-1 GRD backscatter change (VV / VH, dB) before vs. after a fire event.

C-band SAR sees through smoke and cloud. Canopy loss lowers VH volume
scattering, so a VH drop is independent evidence of a burn scar on days when
Sentinel-2 is blind.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterator

from fireverify.config import Settings
from fireverify.ingestion.base import FetchRequest, Scope, Source, SourceError
from fireverify.ingestion.http import HttpClient
from fireverify.ingestion.sources.copernicus import (
    CdseAuth, extract_stats, iso_z, parse_iso, stats_body, statistical_request,
)
from fireverify.ingestion.sources.sentinel2 import change_windows
from fireverify.models import BBox, EnrichmentTarget, RawRecord, RecordKind, SarChange

S1_EVALSCRIPT = """//VERSION=3
function setup() {
  return {
    input: [{ bands: ["VV", "VH", "dataMask"] }],
    output: [
      { id: "vv", bands: 1, sampleType: "FLOAT32" },
      { id: "vh", bands: 1, sampleType: "FLOAT32" },
      { id: "dataMask", bands: 1 }
    ]
  };
}
function evaluatePixel(s) {
  const valid = s.dataMask === 1 && s.VV > 0 && s.VH > 0;
  return {
    vv: [valid ? 10 * Math.log(s.VV) / Math.LN10 : 0],
    vh: [valid ? 10 * Math.log(s.VH) / Math.LN10 : 0],
    dataMask: [valid ? 1 : 0]
  };
}
"""


class Sentinel1SarSource(Source):
    name = "S1_GRD_BACKSCATTER"
    parser = "s1_stats"
    kind = RecordKind.SAR_CHANGE
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
        return change_windows(target, now, s.s1_pre_days, 0, s.s1_post_days)

    def _query(self, bbox: BBox, start: datetime, end: datetime) -> dict[str, Any]:
        if self._auth is None:
            self._auth = CdseAuth(self.http, self.settings.cdse_client_id, self.settings.cdse_client_secret)
        body = stats_body(
            bbox, "sentinel-1-grd",
            {"acquisitionMode": "IW", "polarization": "DV", "resolution": "HIGH", "mosaickingOrder": "mostRecent"},
            S1_EVALSCRIPT, start, end, self.settings.s1_res_deg,
            processing={"backCoeff": "GAMMA0_ELLIPSOID", "orthorectify": True},
        )
        return statistical_request(self.http, self._auth, body)

    def _responses(self, target: EnrichmentTarget, bbox: BBox, w: dict[str, datetime]):
        pre = self._query(bbox, w["pre_from"], w["pre_to"])
        post = self._query(bbox, w["post_from"], w["post_to"]) if w["post_to"] > w["post_from"] else None
        return pre, post, None

    def fetch(self, req: FetchRequest) -> Iterator[RawRecord]:
        target = req.target
        if target is None:
            raise SourceError("Sentinel-1 change requires an enrichment target")
        buffer_m = max(self.settings.s1_buffer_m, target.radius_km * 1000)
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


def normalize_s1(raw: RawRecord) -> SarChange:
    p = raw.payload
    w = p.get("windows") or {}
    target = p.get("target") or {}
    vv_pre = extract_stats(p.get("pre_response"), "vv")
    vv_post = extract_stats(p.get("post_response"), "vv")
    vh_pre = extract_stats(p.get("pre_response"), "vh")
    vh_post = extract_stats(p.get("post_response"), "vh")
    return SarChange(
        source=raw.source,
        latitude=float(target["latitude"]),
        longitude=float(target["longitude"]),
        buffer_m=float(p.get("buffer_m", 0)),
        pre_from=parse_iso(w.get("pre_from"), "pre_from"),
        pre_to=parse_iso(w.get("pre_to"), "pre_to"),
        post_from=parse_iso(w.get("post_from"), "post_from"),
        post_to=parse_iso(w.get("post_to"), "post_to"),
        vv_pre_db=vv_pre["mean"],
        vv_post_db=vv_post["mean"],
        vh_pre_db=vh_pre["mean"],
        vh_post_db=vh_post["mean"],
        valid_fraction_pre=vv_pre["valid_fraction"],
        valid_fraction_post=vv_post["valid_fraction"],
        samples_pre=vv_pre["samples"],
        samples_post=vv_post["samples"],
    )
