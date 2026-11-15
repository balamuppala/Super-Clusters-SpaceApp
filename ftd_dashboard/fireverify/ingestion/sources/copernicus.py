"""Copernicus Data Space Ecosystem (CDSE) plumbing shared by the Sentinel connectors.

* OData catalogue search (no credentials) -> scene / granule discovery
* Sentinel Hub Statistical API (OAuth client credentials) -> zonal statistics
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Any

from fireverify.ingestion.base import ParseError, SourceError
from fireverify.ingestion.http import HttpClient
from fireverify.models import BBox

CDSE_TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
CDSE_ODATA_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
CDSE_STATS_URL = "https://sh.dataspace.copernicus.eu/api/v1/statistics"
WGS84 = "http://www.opengis.net/def/crs/EPSG/0/4326"


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: Any, field: str) -> datetime:
    if not value:
        raise ParseError(f"missing field {field!r}")
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ParseError(f"invalid timestamp in {field!r}: {value!r}") from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class CdseAuth:
    def __init__(self, http: HttpClient, client_id: str, client_secret: str) -> None:
        self.http = http
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: str | None = None
        self._expires_at = 0.0

    def token(self) -> str:
        if self._token and time.time() < self._expires_at - 60:
            return self._token
        data = self.http.post_form(CDSE_TOKEN_URL, {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        })
        if "access_token" not in data:
            raise SourceError(f"CDSE token response missing access_token: {list(data)}")
        self._token = data["access_token"]
        self._expires_at = time.time() + float(data.get("expires_in", 600))
        return self._token

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}"}


def odata_search(http: HttpClient, collection: str, bbox: BBox, start: datetime, end: datetime,
                 name_contains: str | None = None, top: int = 50) -> list[dict[str, Any]]:
    clauses = [
        f"Collection/Name eq '{collection}'",
        f"OData.CSC.Intersects(area=geography'SRID=4326;{bbox.as_wkt()}')",
        f"ContentDate/Start ge {start.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')}",
        f"ContentDate/Start le {end.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')}",
    ]
    if name_contains:
        clauses.append(f"contains(Name,'{name_contains}')")
    params = {"$filter": " and ".join(clauses), "$orderby": "ContentDate/Start asc", "$top": str(top)}
    data = http.get_json(CDSE_ODATA_URL, params=params)
    if not isinstance(data, dict) or "value" not in data:
        raise SourceError("unexpected OData response shape")
    return list(data["value"])


def stats_body(bbox: BBox, data_type: str, data_filter: dict[str, Any], evalscript: str,
               start: datetime, end: datetime, res_deg: float,
               processing: dict[str, Any] | None = None) -> dict[str, Any]:
    days = max(1, math.ceil((end - start).total_seconds() / 86400))
    data: dict[str, Any] = {"type": data_type, "dataFilter": data_filter}
    if processing:
        data["processing"] = processing
    return {
        "input": {"bounds": {"bbox": bbox.as_list(), "properties": {"crs": WGS84}}, "data": [data]},
        "aggregation": {
            "timeRange": {"from": iso_z(start), "to": iso_z(end)},
            "aggregationInterval": {"of": f"P{days}D", "lastIntervalBehavior": "SHORTEN"},
            "evalscript": evalscript,
            "resx": res_deg,
            "resy": res_deg,
        },
        "calculations": {"default": {}},
    }


def statistical_request(http: HttpClient, auth: CdseAuth, body: dict[str, Any]) -> dict[str, Any]:
    return http.post_json(CDSE_STATS_URL, body, headers=auth.headers())


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def extract_stats(response: dict[str, Any] | None, output_id: str, band: str = "B0") -> dict[str, Any]:
    """Collapse a Statistical API response into a valid-pixel-weighted mean for one output."""
    empty = {"mean": None, "samples": 0, "nodata": 0, "valid_fraction": None}
    if response is None:
        return empty
    if not isinstance(response, dict) or not isinstance(response.get("data"), list):
        raise ParseError("statistical response missing 'data' list")
    samples = nodata = valid_total = 0
    weighted = 0.0
    for interval in response["data"]:
        output = (interval.get("outputs") or {}).get(output_id)
        if not output:
            continue
        try:
            stats = output["bands"][band]["stats"]
        except (KeyError, TypeError) as exc:
            raise ParseError(f"output {output_id!r} missing band stats") from exc
        s = int(stats.get("sampleCount") or 0)
        nd = int(stats.get("noDataCount") or 0)
        mean = _num(stats.get("mean"))
        samples += s
        nodata += nd
        if mean is not None and s - nd > 0:
            weighted += mean * (s - nd)
            valid_total += s - nd
    return {
        "mean": weighted / valid_total if valid_total else None,
        "samples": samples,
        "nodata": nodata,
        "valid_fraction": (samples - nodata) / samples if samples else None,
    }
