import unittest
from datetime import datetime, timedelta, timezone

from fireverify.config import Settings
from fireverify.geo import haversine_km, point_in_geometry, solar_elevation_deg, square_polygon
from fireverify.ingestion.base import ParseError, SourceError
from fireverify.ingestion.sources.context import normalize_geojson, normalize_overpass
from fireverify.ingestion.sources.firms import normalize_firms, parse_firms_csv
from fireverify.ingestion.sources.sentinel2 import normalize_s2
from fireverify.ingestion.sources.sentinel3 import normalize_cdse_scene
from fireverify.ingestion.validation import ValidationContext, validate
from fireverify.models import Action, BBox, Lane, RawRecord, RecordKind

UTC = timezone.utc
VIIRS_CSV = (
    "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,bright_ti5,frp,daynight\n"
    "38.51234,-120.11111,345.20,0.41,0.37,2026-08-01,930,N,VIIRS,n,2.0NRT,290.10,12.40,N\n"
)


def raw(source, payload, kind=RecordKind.HOTSPOT, parser="firms"):
    return RawRecord(source, parser, kind, payload, Lane.LIVE, datetime.now(UTC))


class FirmsParsingTests(unittest.TestCase):
    def test_viirs_row_with_unpadded_time(self):
        row = parse_firms_csv(VIIRS_CSV)[0]
        h = normalize_firms(raw("VIIRS_SNPP_NRT", row))
        self.assertEqual(h.acquired_at, datetime(2026, 8, 1, 9, 30, tzinfo=UTC))
        self.assertEqual((h.sensor, h.platform, h.confidence_class), ("VIIRS", "Suomi-NPP", "nominal"))
        self.assertAlmostEqual(h.frp_mw, 12.4)

    def test_modis_confidence_classes(self):
        base = {"latitude": "1", "longitude": "2", "brightness": "320", "acq_date": "2026-08-01",
                "acq_time": "1030", "satellite": "A", "frp": "5"}
        for conf, cls in (("10", "low"), ("55", "nominal"), ("91", "high")):
            h = normalize_firms(raw("MODIS_NRT", {**base, "confidence": conf}))
            self.assertEqual((h.platform, h.confidence_class), ("Aqua", cls))
        with self.assertRaises(ParseError):
            normalize_firms(raw("MODIS_NRT", {**base, "confidence": "142"}))

    def test_firms_error_text_is_a_source_error(self):
        with self.assertRaises(SourceError):
            parse_firms_csv("Invalid MAP_KEY.")


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        self.now = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)

    def hotspot(self, **mods):
        row = {**parse_firms_csv(VIIRS_CSV)[0], **mods}
        return normalize_firms(raw("VIIRS_SNPP_NRT", row))

    def verdict(self, h, **ctx):
        return validate(RecordKind.HOTSPOT, h, ValidationContext(self.settings, self.now, **ctx))

    def test_clean_record_accepted(self):
        self.assertEqual(self.verdict(self.hotspot()).action, Action.ACCEPT)

    def test_hard_rules_reject(self):
        self.assertIn("R_COORD", self.verdict(self.hotspot(latitude="91")).codes)
        self.assertIn("R_PHYSICS", self.verdict(self.hotspot(frp="-1")).codes)
        self.assertIn("R_FUTURE", self.verdict(self.hotspot(acq_date="2026-08-05")).codes)
        v = self.verdict(self.hotspot(), aoi=BBox(0, 0, 10, 10))
        self.assertEqual((v.action, v.codes), (Action.REJECT, ["R_AOI"]))

    def test_soft_rules_quarantine(self):
        self.assertEqual(self.verdict(self.hotspot(confidence="l")).codes, ["Q_LOW_CONF"])
        # 23:30 UTC at -120 lon is ~15:30 local: sun up, so an N flag is contradictory.
        self.assertEqual(self.verdict(self.hotspot(acq_time="2330", daynight="N")).codes, ["Q_DAYNIGHT"])

    def test_saa_artifact(self):
        row = {"latitude": "-25", "longitude": "-45", "brightness": "305", "acq_date": "2026-08-01",
               "acq_time": "0300", "satellite": "T", "confidence": "20", "frp": "4", "daynight": "N"}
        h = normalize_firms(raw("MODIS_NRT", row))
        v = validate(RecordKind.HOTSPOT, h, ValidationContext(Settings(quarantine_low_confidence=False), self.now))
        self.assertEqual(v.codes, ["Q_SAA"])

    def test_release_ignores_soft_but_keeps_hard_rules(self):
        h = self.hotspot(confidence="l")
        v = validate(RecordKind.HOTSPOT, h, ValidationContext(self.settings, self.now, ignore_soft=True))
        self.assertEqual(v.action, Action.ACCEPT)
        self.assertIn("released:Q_LOW_CONF", v.flags)
        bad = self.hotspot(confidence="l", frp="-3")
        v = validate(RecordKind.HOTSPOT, bad, ValidationContext(self.settings, self.now, ignore_soft=True))
        self.assertEqual(v.action, Action.REJECT)


class CopernicusAndContextParsingTests(unittest.TestCase):
    def stats(self, mean, samples, nodata):
        return {"data": [{"interval": {}, "outputs": {
            "ndvi": {"bands": {"B0": {"stats": {"mean": mean, "sampleCount": samples, "noDataCount": nodata}}}},
            "nbr": {"bands": {"B0": {"stats": {"mean": mean if isinstance(mean, str) else mean - 0.2, "sampleCount": samples, "noDataCount": nodata}}}},
        }}]}

    def test_s2_statistics_response(self):
        payload = {
            "target": {"latitude": 38.5, "longitude": -120.1}, "buffer_m": 750,
            "windows": {"pre_from": "2026-06-01T00:00:00Z", "pre_to": "2026-07-15T00:00:00Z",
                        "post_from": "2026-07-17T00:00:00Z", "post_to": "2026-08-01T00:00:00Z"},
            "pre_response": self.stats(0.7, 1000, 100), "post_response": self.stats("NaN", 1000, 1000),
        }
        o = normalize_s2(RawRecord("S2_L2A_NDVI", "s2_stats", RecordKind.OPTICAL_CHANGE, payload, Lane.LIVE,
                                   datetime.now(UTC)))
        self.assertAlmostEqual(o.ndvi_pre, 0.7)
        self.assertAlmostEqual(o.valid_fraction_pre, 0.9)
        self.assertIsNone(o.ndvi_post)
        v = validate(RecordKind.OPTICAL_CHANGE, o, ValidationContext(Settings(), datetime.now(UTC)))
        self.assertEqual(v.codes, ["Q_CLOUDY"])  # post window fully clouded

    def test_cdse_scene(self):
        payload = {"collection": "SENTINEL-3", "target": {"latitude": 1.0, "longitude": 2.0}, "product": {
            "Id": "abc", "Name": "S3B_SL_2_FRP____20260801T101500_20260801T101800_X.SEN3",
            "ContentDate": {"Start": "2026-08-01T10:15:00.000Z", "End": "2026-08-01T10:18:00.000Z"}}}
        o = normalize_cdse_scene(RawRecord("S3_SLSTR_FRP", "cdse_scene", RecordKind.OVERPASS, payload, Lane.LIVE,
                                           datetime.now(UTC)))
        self.assertEqual((o.platform, o.product_type), ("Sentinel-3B", "SL_2_FRP___"))

    def test_overpass_and_geojson(self):
        f = normalize_overpass(raw("OSM", {"type": "way", "id": 7, "center": {"lat": 30.1, "lon": 47.5},
                                           "tags": {"man_made": "flare", "name": "Flare 7"}}, RecordKind.CONTEXT_FEATURE))
        self.assertEqual((f.category, f.feature_id), ("gas_flare", "osm:way/7"))
        with self.assertRaises(ParseError):
            normalize_geojson(raw("G", {"feature": {"properties": {}, "geometry": {"type": "Point", "coordinates": [0, 0]}}},
                                  RecordKind.CONTEXT_FEATURE))


class GeoTests(unittest.TestCase):
    def test_haversine(self):
        self.assertAlmostEqual(haversine_km(0, 0, 0, 1), 111.19, delta=0.1)

    def test_solar_elevation_day_and_night(self):
        noon = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)
        self.assertGreater(solar_elevation_deg(0, 0, noon), 60)
        self.assertLess(solar_elevation_deg(0, 0, noon + timedelta(hours=12)), -60)

    def test_point_in_polygon(self):
        poly = square_polygon(10, 10, 2)
        self.assertTrue(point_in_geometry(10, 10, poly))
        self.assertFalse(point_in_geometry(10.1, 10, poly))


if __name__ == "__main__":
    unittest.main()
