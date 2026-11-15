"""Small, dependency-free geodesy helpers (good enough at hotspot scales)."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def offset_point(lat: float, lon: float, north_km: float, east_km: float) -> tuple[float, float]:
    dlat = north_km / 110.574
    dlon = east_km / (111.320 * max(math.cos(math.radians(lat)), 0.01))
    return lat + dlat, lon + dlon


def solar_elevation_deg(lat: float, lon: float, when: datetime) -> float:
    """NOAA general solar position approximation (accuracy ~0.5 deg)."""
    when = when.astimezone(timezone.utc)
    doy = when.timetuple().tm_yday
    hour = when.hour + when.minute / 60 + when.second / 3600
    g = 2 * math.pi / 365 * (doy - 1 + (hour - 12) / 24)
    eqtime = 229.18 * (
        0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
        - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g)
    )
    decl = (
        0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
        - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
        - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g)
    )
    true_solar_min = hour * 60 + eqtime + 4 * lon
    hour_angle = math.radians(true_solar_min / 4 - 180)
    phi = math.radians(lat)
    cos_zen = math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * math.cos(hour_angle)
    return 90 - math.degrees(math.acos(max(-1.0, min(1.0, cos_zen))))


# --------------------------------------------------------------------------
# GeoJSON-ish geometry helpers. Coordinates are [lon, lat] as in GeoJSON.
# --------------------------------------------------------------------------

def _rings(geometry: dict[str, Any]) -> list[list[Sequence[float]]]:
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "Polygon":
        return list(coords)
    if gtype == "MultiPolygon":
        return [ring for poly in coords for ring in poly]
    return []


def _point_in_ring(lat: float, lon: float, ring: Sequence[Sequence[float]]) -> bool:
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def point_in_geometry(lat: float, lon: float, geometry: dict[str, Any] | None) -> bool:
    if not geometry:
        return False
    gtype = geometry.get("type")
    polys: list = []
    if gtype == "Polygon":
        polys = [geometry["coordinates"]]
    elif gtype == "MultiPolygon":
        polys = geometry["coordinates"]
    for poly in polys:
        if poly and _point_in_ring(lat, lon, poly[0]) and not any(
            _point_in_ring(lat, lon, hole) for hole in poly[1:]
        ):
            return True
    return False


def _segment_distance_km(lat: float, lon: float, a: Sequence[float], b: Sequence[float]) -> float:
    kx = 111.320 * math.cos(math.radians(lat))
    ky = 110.574
    ax, ay = (a[0] - lon) * kx, (a[1] - lat) * ky
    bx, by = (b[0] - lon) * kx, (b[1] - lat) * ky
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    t = 0.0 if seg2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg2))
    px, py = ax + t * dx, ay + t * dy
    return math.hypot(px, py)


def distance_to_geometry_km(lat: float, lon: float, geometry: dict[str, Any] | None,
                            fallback: tuple[float, float] | None = None) -> float:
    """Distance from a point to a Point / LineString / (Multi)Polygon, 0 if inside."""
    if not geometry:
        if fallback is None:
            return math.inf
        return haversine_km(lat, lon, *fallback)
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "Point":
        return haversine_km(lat, lon, coords[1], coords[0])
    if gtype == "MultiPoint":
        return min(haversine_km(lat, lon, c[1], c[0]) for c in coords)
    if gtype in ("Polygon", "MultiPolygon"):
        if point_in_geometry(lat, lon, geometry):
            return 0.0
        segments = _ring_segments(_rings(geometry))
    elif gtype == "LineString":
        segments = zip(coords[:-1], coords[1:])
    elif gtype == "MultiLineString":
        segments = (s for line in coords for s in zip(line[:-1], line[1:]))
    else:
        return math.inf if fallback is None else haversine_km(lat, lon, *fallback)
    return min((_segment_distance_km(lat, lon, a, b) for a, b in segments), default=math.inf)


def _ring_segments(rings: Iterable[Sequence[Sequence[float]]]):
    for ring in rings:
        for a, b in zip(ring[:-1], ring[1:]):
            yield a, b


def geometry_centroid(geometry: dict[str, Any]) -> tuple[float, float]:
    """Return (lat, lon). Vertex mean of the outer ring(s); fine for small features."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "Point":
        return float(coords[1]), float(coords[0])
    if gtype == "Polygon":
        pts = coords[0][:-1] or coords[0]
    elif gtype == "MultiPolygon":
        pts = [p for poly in coords for p in (poly[0][:-1] or poly[0])]
    elif gtype in ("LineString", "MultiPoint"):
        pts = coords
    elif gtype == "MultiLineString":
        pts = [p for line in coords for p in line]
    else:
        raise ValueError(f"unsupported geometry type {gtype!r}")
    if not pts:
        raise ValueError("empty geometry")
    return sum(p[1] for p in pts) / len(pts), sum(p[0] for p in pts) / len(pts)


VALID_GEOMETRY_TYPES = {"Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon"}


def square_polygon(lat: float, lon: float, half_km: float) -> dict[str, Any]:
    n, e = offset_point(lat, lon, half_km, half_km)
    s, w = offset_point(lat, lon, -half_km, -half_km)
    return {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}
