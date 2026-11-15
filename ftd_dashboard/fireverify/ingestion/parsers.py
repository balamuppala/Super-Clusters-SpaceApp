"""Parser registry: maps ``RawRecord.parser`` to its normalizer.

Kept as a registry (not methods on sources) so quarantined records can be
re-parsed on release without reconstructing the connector that fetched them.
"""

from __future__ import annotations

from typing import Callable

from fireverify.ingestion.sources.context import normalize_geojson, normalize_overpass
from fireverify.ingestion.sources.firms import normalize_firms
from fireverify.ingestion.sources.sentinel1 import normalize_s1
from fireverify.ingestion.sources.sentinel2 import normalize_s2
from fireverify.ingestion.sources.sentinel3 import normalize_cdse_scene
from fireverify.models import CanonicalRecord, RawRecord

PARSERS: dict[str, Callable[[RawRecord], CanonicalRecord]] = {
    "firms": normalize_firms,
    "s2_stats": normalize_s2,
    "s1_stats": normalize_s1,
    "cdse_scene": normalize_cdse_scene,
    "overpass_context": normalize_overpass,
    "geojson_context": normalize_geojson,
}
