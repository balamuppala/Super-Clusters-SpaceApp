"""Contextual layers: known non-wildfire heat sources and land cover.

* ``OverpassContextSource``  OpenStreetMap gas flares, power plants, industry, landfills, volcanoes
* ``GeoJsonContextSource``   local GeoJSON (bundled volcano seed + user layers, e.g. land cover, urban extents)
"""

from __future__ import annotations

import json
from datetime import timedelta
from importlib import resources
from pathlib import Path
from typing import Any, Iterator

from fireverify.config import Settings
from fireverify.geo import VALID_GEOMETRY_TYPES, geometry_centroid
from fireverify.ingestion.base import FetchRequest, ParseError, Scope, Source, SourceError
from fireverify.ingestion.http import HttpClient
from fireverify.models import ContextFeature, RawRecord, RecordKind

# Order matters: first match wins.
OSM_CATEGORY_RULES: list[tuple[str, str, str]] = [
    ("natural", "volcano", "volcano"),
    ("man_made", "flare", "gas_flare"),
    ("power", "plant", "power_plant"),
    ("landuse", "landfill", "landfill"),
    ("man_made", "works", "industrial"),
    ("landuse", "industrial", "industrial"),
]


def overpass_query(south: float, west: float, north: float, east: float) -> str:
    b = f"({south},{west},{north},{east})"
    return f"""[out:json][timeout:90];
(
  nwr["natural"="volcano"]{b};
  nwr["man_made"="flare"]{b};
  nwr["power"="plant"]{b};
  nwr["landuse"="landfill"]{b};
  nwr["man_made"="works"]{b};
  way["landuse"="industrial"]{b};
);
out center tags;"""


class OverpassContextSource(Source):
    name = "OSM_OVERPASS_CONTEXT"
    parser = "overpass_context"
    kind = RecordKind.CONTEXT_FEATURE
    scope = Scope.AOI

    def __init__(self, settings: Settings, http: HttpClient | None) -> None:
        self.settings = settings
        self.http = http
        self.refresh_interval = timedelta(hours=settings.context_refresh_hours)

    def available(self) -> tuple[bool, str]:
        if not self.settings.enable_overpass:
            return False, "disabled (FIREVERIFY_OVERPASS=0)"
        return True, "ok"

    def _elements(self, req: FetchRequest) -> list[dict[str, Any]]:
        bbox = req.aoi.expand(0.25)
        if bbox.area_deg2 > self.settings.overpass_max_area_deg2:
            raise SourceError(
                f"AOI too large for Overpass ({bbox.area_deg2:.1f} deg² > {self.settings.overpass_max_area_deg2}); "
                "supply context via FIREVERIFY_CONTEXT_PATHS instead"
            )
        data = self.http.post_form(self.settings.overpass_url,
                                   {"data": overpass_query(bbox.south, bbox.west, bbox.north, bbox.east)})
        return list(data.get("elements", []))

    def fetch(self, req: FetchRequest) -> Iterator[RawRecord]:
        for element in self._elements(req):
            yield self._raw(element)


def normalize_overpass(raw: RawRecord) -> ContextFeature:
    el = raw.payload
    tags = el.get("tags") or {}
    category = next((cat for key, val, cat in OSM_CATEGORY_RULES if tags.get(key) == val), None)
    if category is None:
        raise ParseError("OSM element matches no context category")
    if "lat" in el and "lon" in el:
        lat, lon = el["lat"], el["lon"]
    elif isinstance(el.get("center"), dict):
        lat, lon = el["center"].get("lat"), el["center"].get("lon")
    else:
        raise ParseError("OSM element has no coordinates")
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError) as exc:
        raise ParseError("OSM element coordinates not numeric") from exc
    return ContextFeature(
        source=raw.source,
        feature_id=f"osm:{el.get('type', 'node')}/{el.get('id')}",
        category=category,
        name=tags.get("name"),
        latitude=lat,
        longitude=lon,
        geometry={"type": "Point", "coordinates": [lon, lat]},
        tags={k: v for k, v in tags.items() if k in {"name", "operator", "plant:source", "industrial", "product"}},
    )


def bundled_context_files() -> list[Path]:
    root = resources.files("fireverify") / "data" / "context"
    return sorted(Path(str(p)) for p in root.iterdir() if str(p).endswith(".geojson"))


class GeoJsonContextSource(Source):
    name = "GEOJSON_CONTEXT"
    parser = "geojson_context"
    kind = RecordKind.CONTEXT_FEATURE
    scope = Scope.AOI

    def __init__(self, paths: list[Path]) -> None:
        self.paths = [Path(p) for p in paths]

    def available(self) -> tuple[bool, str]:
        missing = [str(p) for p in self.paths if not p.is_file()]
        if missing:
            return False, f"missing files: {', '.join(missing)}"
        return (True, "ok") if self.paths else (False, "no GeoJSON layers configured")

    def _features(self, req: FetchRequest) -> Iterator[tuple[dict[str, Any], str, dict | None]]:
        """Yield (feature, layer_name, sim_truth)."""
        for path in self.paths:
            doc = json.loads(path.read_text())
            for feature in doc.get("features", []):
                yield feature, path.stem, None

    def fetch(self, req: FetchRequest) -> Iterator[RawRecord]:
        search = req.aoi.expand(0.25)
        for feature, layer, truth in self._features(req):
            try:
                lat, lon = geometry_centroid(feature.get("geometry") or {})
                if not search.contains(lat, lon):
                    continue  # outside the area of interest; not an error
            except (ValueError, TypeError, KeyError, IndexError):
                pass  # let validation reject it with a reason code
            yield self._raw({"layer": layer, "feature": feature}, sim_truth=truth)


def normalize_geojson(raw: RawRecord) -> ContextFeature:
    feature = raw.payload.get("feature") or {}
    props = feature.get("properties") or {}
    geometry = feature.get("geometry")
    category = str(props.get("category") or "").strip().lower()
    if not category:
        raise ParseError("GeoJSON feature missing properties.category")
    if not isinstance(geometry, dict) or geometry.get("type") not in VALID_GEOMETRY_TYPES:
        raise ParseError(f"unsupported or missing geometry: {type(geometry).__name__}")
    try:
        lat, lon = geometry_centroid(geometry)
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise ParseError(f"invalid geometry coordinates: {exc}") from exc
    fid = feature.get("id") or props.get("id") or props.get("name") or f"{lat:.5f},{lon:.5f}"
    return ContextFeature(
        source=raw.source,
        feature_id=f"{raw.payload.get('layer', 'geojson')}:{fid}",
        category=category,
        name=props.get("name"),
        latitude=lat,
        longitude=lon,
        geometry=geometry,
        tags={k: v for k, v in props.items() if k not in {"category", "name"}},
    )
