"""Runtime settings, loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

# The project folder (holds .env and data/), so the app behaves the same whichever folder it's started from.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
from typing import Mapping

DEFAULT_FIRMS_SOURCES = (
    "VIIRS_SNPP_NRT",
    "VIIRS_NOAA20_NRT",
    "VIIRS_NOAA21_NRT",
    "MODIS_NRT",
)


@dataclass(frozen=True)
class Settings:
    # --- credentials -------------------------------------------------------
    firms_map_key: str | None = None
    cdse_client_id: str | None = None
    cdse_client_secret: str | None = None

    # --- storage / transport -----------------------------------------------
    db_path: Path = PROJECT_ROOT / "data" / "fireverify.db"
    likelihoods_path: Path = PROJECT_ROOT / "data" / "likelihoods.json"
    # National context: the burning-activity country archive (unzipped handoff); optional
    country_archive_dir: Path = PROJECT_ROOT / "data" / "external" / "burning_activity_calendar"  # fitted table; expert defaults if absent
    user_agent: str = "fireverify-ingestion/0.1"
    http_timeout_s: float = 60.0
    http_max_retries: int = 3

    # --- detection sources -------------------------------------------------
    firms_sources: tuple[str, ...] = DEFAULT_FIRMS_SOURCES

    # --- historical archive (temporal frequency) ---------------------------
    history_sources: tuple[str, ...] = ("VIIRS_SNPP_SP",)
    history_years: int = 5
    history_window_days: int = 5
    history_radius_km: float = 1.0
    history_offseason: bool = True   # also sample a window 6 months away: seasonal vs year-round
    season_halfwidth_days: int = 45

    # --- clustering ---------------------------------------------------------
    cluster_radius_km: float = 2.0
    cluster_window_hours: float = 48.0
    max_enrich_clusters: int = 25

    # --- Sentinel-2 vegetation change --------------------------------------
    s2_pre_days: int = 45
    s2_gap_days: int = 2
    s2_post_days: int = 20
    s2_buffer_m: float = 750.0
    s2_max_cloud: int = 80
    s2_res_deg: float = 0.0001  # ~10 m

    # --- Sentinel-1 SAR change ---------------------------------------------
    s1_pre_days: int = 36
    s1_post_days: int = 12
    s1_buffer_m: float = 750.0
    s1_res_deg: float = 0.0002  # ~20 m

    # --- Sentinel-3 SLSTR FRP coverage -------------------------------------
    s3_window_hours: float = 12.0

    # --- contextual layers --------------------------------------------------
    context_paths: tuple[Path, ...] = ()
    enable_overpass: bool = True
    overpass_url: str = "https://overpass-api.de/api/interpreter"
    overpass_max_area_deg2: float = 4.0
    context_refresh_hours: float = 168.0
    context_search_km: float = 25.0

    # --- validation thresholds ---------------------------------------------
    clock_skew_tolerance_min: float = 15.0
    future_reject_hours: float = 24.0
    out_of_window_tolerance_hours: float = 24.0
    quarantine_low_confidence: bool = True
    min_clear_fraction: float = 0.3
    min_sar_valid_fraction: float = 0.3

    # --- context proximity buffers (km) -------------------------------------
    proximity_buffers_km: Mapping[str, float] = field(
        default_factory=lambda: {
            "gas_flare": 1.0,
            "industrial": 1.0,
            "power_plant": 1.0,
            "landfill": 1.0,
            "volcano": 10.0,
        }
    )

    @property
    def has_firms(self) -> bool:
        return bool(self.firms_map_key)

    @property
    def has_cdse(self) -> bool:
        return bool(self.cdse_client_id and self.cdse_client_secret)

    def with_overrides(self, **kwargs) -> "Settings":
        known = {f.name for f in fields(self)}
        return replace(self, **{k: v for k, v in kwargs.items() if k in known and v is not None})

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, dotenv: str | Path | None = ".env") -> "Settings":
        merged: dict[str, str] = {}
        if dotenv and not Path(dotenv).is_file() and not Path(dotenv).is_absolute():
            dotenv = PROJECT_ROOT / dotenv     # started from another folder: use the project's .env
        if dotenv and Path(dotenv).is_file():
            merged.update(_read_dotenv(Path(dotenv)))
        merged.update(env if env is not None else os.environ)

        def get(name: str) -> str | None:
            value = merged.get(name, "").strip()
            return value or None

        kwargs: dict = {
            "firms_map_key": get("FIRMS_MAP_KEY"),
            "cdse_client_id": get("CDSE_CLIENT_ID"),
            "cdse_client_secret": get("CDSE_CLIENT_SECRET"),
        }
        if get("FIREVERIFY_DB"):
            kwargs["db_path"] = Path(get("FIREVERIFY_DB"))
        if get("FIREVERIFY_COUNTRY_ARCHIVE"):
            kwargs["country_archive_dir"] = Path(get("FIREVERIFY_COUNTRY_ARCHIVE"))
        if get("FIREVERIFY_LIKELIHOODS"):
            kwargs["likelihoods_path"] = Path(get("FIREVERIFY_LIKELIHOODS"))
        if get("FIREVERIFY_CONTEXT_PATHS"):
            kwargs["context_paths"] = tuple(
                Path(p.strip()) for p in get("FIREVERIFY_CONTEXT_PATHS").split(",") if p.strip()
            )
        if get("FIREVERIFY_OVERPASS") is not None:
            kwargs["enable_overpass"] = get("FIREVERIFY_OVERPASS") not in {"0", "false", "no", "off"}
        if get("FIREVERIFY_FIRMS_SOURCES"):
            kwargs["firms_sources"] = tuple(s.strip() for s in get("FIREVERIFY_FIRMS_SOURCES").split(","))
        return cls(**kwargs)


def _read_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out
