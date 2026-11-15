"""Sentinel-3 SLSTR Fire Radiative Power (SL_2_FRP) granule discovery.

Finds S3A/S3B SLSTR FRP granules that covered a fire event within
±``s3_window_hours``. This records *coverage* (an independent thermal sensor
looked at the location at a different time of day than VIIRS/MODIS).
Pixel-level FRP extraction needs the NetCDF product download and is left to a
later processing stage; ``Overpass.product_id`` is the handle for it.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Iterator

from fireverify.config import Settings
from fireverify.ingestion.base import FetchRequest, ParseError, Scope, Source, SourceError
from fireverify.ingestion.http import HttpClient
from fireverify.ingestion.sources.copernicus import odata_search, parse_iso
from fireverify.models import BBox, Overpass, RawRecord, RecordKind


class CdseSceneSource(Source):
    parser = "cdse_scene"
    kind = RecordKind.OVERPASS
    scope = Scope.TARGET

    def __init__(self, name: str, collection: str, name_contains: str | None, settings: Settings,
                 http: HttpClient | None, window_hours: float) -> None:
        self.name = name
        self.collection = collection
        self.name_contains = name_contains
        self.settings = settings
        self.http = http
        self.window_hours = window_hours

    def _search(self, bbox: BBox, start, end) -> list[dict]:
        return odata_search(self.http, self.collection, bbox, start, end, self.name_contains)

    def fetch(self, req: FetchRequest) -> Iterator[RawRecord]:
        target = req.target
        if target is None:
            raise SourceError(f"{self.name} requires an enrichment target")
        start = target.event_start - timedelta(hours=self.window_hours)
        end = min(req.now, target.event_end + timedelta(hours=self.window_hours))
        bbox = BBox.around(target.latitude, target.longitude, max(1.0, target.radius_km))
        for product in self._search(bbox, start, end):
            payload = {
                "target": {"latitude": target.latitude, "longitude": target.longitude},
                "collection": self.collection,
                "product": product,
            }
            yield self._raw(payload, target_id=target.cluster_id)


def sentinel3_frp_source(settings: Settings, http: HttpClient | None) -> CdseSceneSource:
    return CdseSceneSource("S3_SLSTR_FRP", "SENTINEL-3", "SL_2_FRP", settings, http, settings.s3_window_hours)


def normalize_cdse_scene(raw: RawRecord) -> Overpass:
    p = raw.payload
    product = p.get("product") or {}
    name = str(product.get("Name") or "")
    if not name or not product.get("Id"):
        raise ParseError("scene product missing Id/Name")
    content = product.get("ContentDate") or {}
    parts = name.split("_")
    platform_code = parts[0] if parts else ""
    platform = {"S3A": "Sentinel-3A", "S3B": "Sentinel-3B", "S1A": "Sentinel-1A", "S1C": "Sentinel-1C",
                "S2A": "Sentinel-2A", "S2B": "Sentinel-2B", "S2C": "Sentinel-2C"}.get(platform_code, platform_code)
    product_type = "SL_2_FRP___" if "SL_2_FRP" in name else (parts[1] if len(parts) > 1 else "unknown")
    cloud = None
    for attr in product.get("Attributes") or []:
        if attr.get("Name") == "cloudCover":
            try:
                cloud = float(attr.get("Value"))
            except (TypeError, ValueError):
                cloud = None
    target = p.get("target") or {}
    return Overpass(
        source=raw.source,
        mission=str(p.get("collection") or ""),
        platform=platform,
        product_type=product_type,
        product_id=str(product["Id"]),
        product_name=name,
        sensing_start=parse_iso(content.get("Start"), "ContentDate.Start"),
        sensing_end=parse_iso(content["End"], "ContentDate.End") if content.get("End") else None,
        latitude=float(target["latitude"]),
        longitude=float(target["longitude"]),
        cloud_cover=cloud,
    )
