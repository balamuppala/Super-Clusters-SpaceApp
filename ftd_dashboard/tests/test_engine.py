import unittest
from datetime import datetime, timezone

from fireverify.config import Settings
from fireverify.ingestion.engine import IngestionEngine, LaneViolation
from fireverify.ingestion.sources.firms import FirmsSource
from fireverify.ingestion.store import Store
from fireverify.models import BBox, Lane, Mode, RecordKind

NOW = datetime(2026, 11, 14, 18, 0, tzinfo=timezone.utc)
AOI = BBox(-121.0, 38.0, -119.0, 40.0)


def sim_engine(**overrides):
    settings = Settings(enable_overpass=False, **overrides)
    store = Store(":memory:")
    return IngestionEngine(settings, store), store


def label_of(bundle):
    return {d["sim_truth"]["label"] for d in bundle["detections"]}


class SimulatedRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine, cls.store = sim_engine()
        cls.report = cls.engine.run(Mode.SIMULATED, AOI, days=2, now=NOW, seed=7)
        cls.bundles = {}
        for c in cls.store.list_clusters(Lane.SIMULATED):
            b = cls.engine.evidence(Lane.SIMULATED, c["cluster_id"])
            (label,) = label_of(b)
            cls.bundles[label] = b

    def test_every_injected_anomaly_lands_with_its_expected_reason(self):
        audit = self.store.list_quarantine(status=None, limit=1000) + self.store.list_rejected(limit=1000)
        expected = {}
        for row in audit:
            truth = row.get("sim_truth") or {}
            if "expected" in truth:
                name = truth.get("anomaly_id", truth["label"])
                self.assertIn(truth["expected"], row["reasons"], name)
                lane = "quarantine" if "qid" in row else "rejected"
                self.assertEqual(lane, "quarantine" if truth["expected"].startswith("Q_") else "rejected", name)
                expected[name] = truth["expected"]
        self.assertGreaterEqual(len(expected), 16, sorted(expected))

    def test_lanes_never_mix(self):
        counts = self.store.counts()
        self.assertEqual(counts["live"], {})
        self.assertNotIn("live", counts["derived"])
        self.assertTrue(all(r["origin_lane"] == "simulated" for r in self.store.list_rejected(limit=1000)))

    def test_one_cluster_per_scenario_with_all_features(self):
        self.assertEqual(set(self.bundles), {"wildfire", "agricultural_burn", "industrial_gas_flare",
                                             "volcanic_activity", "false_alarm_hot_surface",
                                             "smoldering_vegetation_fire", "unmapped_gas_flare"})
        for b in self.bundles.values():
            self.assertEqual(set(b["derived"]), {
                "cluster_summary", "cross_sensor_corroboration", "vegetation_loss", "sar_backscatter_change",
                "temporal_frequency", "context_proximity", "evidence_completeness"})

    def test_wildfire_evidence(self):
        d = self.bundles["wildfire"]["derived"]
        self.assertGreater(d["vegetation_loss"]["dndvi"], 0.3)
        self.assertTrue(d["sar_backscatter_change"]["vh_drop"])
        self.assertEqual(d["temporal_frequency"]["recurrence_class"], "none")
        self.assertIn("forest", d["context_proximity"]["land_cover"])
        self.assertTrue(d["cross_sensor_corroboration"]["multi_sensor"])

    def test_false_positive_sources_are_distinguishable(self):
        flare = self.bundles["industrial_gas_flare"]["derived"]
        self.assertEqual(flare["temporal_frequency"]["recurrence_class"], "persistent")
        self.assertTrue(flare["context_proximity"]["near_gas_flare"])
        self.assertEqual(flare["vegetation_loss"]["burn_severity"], "unburned")

        volcano = self.bundles["volcanic_activity"]["derived"]
        self.assertTrue(volcano["context_proximity"]["near_volcano"])
        self.assertEqual(volcano["vegetation_loss"]["status"], "quarantined")
        self.assertIn("Q_CLOUDY", volcano["vegetation_loss"]["quarantine_reasons"])

        urban = self.bundles["false_alarm_hot_surface"]["derived"]
        self.assertTrue(urban["context_proximity"]["in_urban_area"])
        self.assertEqual(urban["cluster_summary"]["n_detections"], 1)

        ag = self.bundles["agricultural_burn"]["derived"]
        self.assertEqual(ag["temporal_frequency"]["recurrence_class"], "seasonal")


class IdempotencyAndReviewTests(unittest.TestCase):
    def test_rerun_is_idempotent(self):
        engine, store = sim_engine()
        engine.run(Mode.SIMULATED, AOI, now=NOW, seed=3)
        before = store.counts()
        second = engine.run(Mode.SIMULATED, AOI, now=NOW, seed=3)
        self.assertEqual(second.totals()["accepted"], 0)
        self.assertEqual(second.clusters_enriched, [])
        self.assertEqual(store.counts(), before)

    def test_release_and_reject_from_quarantine(self):
        engine, store = sim_engine()
        engine.run(Mode.SIMULATED, AOI, now=NOW, seed=3, scenarios=["low_confidence", "corrupt"])
        pending = {q["reasons"][0]: q for q in store.list_quarantine()}
        lowconf = pending["Q_LOW_CONF"]
        rid = engine.release_quarantined(lowconf["qid"], "analyst confirmed")
        rec = store.query_records(Lane.SIMULATED, ids=[rid])[0]
        self.assertIn("released_from_quarantine", rec["flags"])
        self.assertEqual(store.get_quarantine(lowconf["qid"])["status"], "released")

        skew = pending["Q_CLOCK_SKEW"]
        engine.reject_quarantined(skew["qid"], "bad clock")
        self.assertIn("R_MANUAL", store.list_rejected(limit=1)[0]["reasons"])
        with self.assertRaises(ValueError):
            engine.reject_quarantined(skew["qid"])


class LiveModeTests(unittest.TestCase):
    def test_live_without_credentials_degrades_gracefully(self):
        settings = Settings(enable_overpass=False)  # no FIRMS / CDSE keys
        store = Store(":memory:")
        engine = IngestionEngine(settings, store)
        report = engine.run(Mode.LIVE, BBox(14.5, 37.5, 15.5, 38.0), now=NOW)  # around Etna
        self.assertEqual(report.sources["VIIRS_SNPP_NRT"].status, "unavailable")
        self.assertEqual(report.sources["GEOJSON_CONTEXT"].status, "ok")
        volcanoes = store.query_records(Lane.LIVE, kinds=[RecordKind.CONTEXT_FEATURE])
        self.assertEqual([v["data"]["name"] for v in volcanoes], ["Etna"])

    def test_live_firms_with_stubbed_http(self):
        csv_text = (
            "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,bright_ti5,frp,daynight\n"
            "38.5,-120.1,350.1,0.4,0.4,2026-11-14,0930,N,VIIRS,h,2.0NRT,295.0,22.0,N\n"
            "38.5,-120.1,350.1,0.4,0.4,2026-11-14,0930,N,VIIRS,h,2.0NRT,295.0,22.0,N\n"
        )

        class StubHttp:
            def __init__(self):
                self.urls = []

            def get_text(self, url, **kw):
                self.urls.append(url)
                return csv_text

        http = StubHttp()
        settings = Settings(firms_map_key="KEY", firms_sources=("VIIRS_SNPP_NRT",), enable_overpass=False)
        store = Store(":memory:")
        engine = IngestionEngine(settings, store, http=http)
        report = engine.run(Mode.LIVE, AOI, days=1, now=NOW, enrich=False)
        st = report.sources["VIIRS_SNPP_NRT"]
        # 1-day window spans two calendar days -> one request; second row is an in-response duplicate
        self.assertEqual(len(http.urls), 1)
        self.assertEqual((st.accepted, st.rejected), (1, 1))
        self.assertTrue(all("/VIIRS_SNPP_NRT/-121,38,-119,40/" in u for u in http.urls))
        self.assertEqual(report.clusters_found, 1)

    def test_simulated_source_in_live_run_is_refused(self):
        engine, _ = sim_engine()
        from fireverify.ingestion.base import FetchRequest
        from fireverify.ingestion.validation import ValidationContext
        from fireverify.ingestion.engine import RunReport
        from fireverify.ingestion.simulated import SimulatedFirmsSource, SimWorld

        world = SimWorld(AOI, NOW.replace(day=12), NOW, NOW, scenarios=["wildfire"])
        src = SimulatedFirmsSource("VIIRS_SNPP_NRT", engine.settings, world)
        report = RunReport("r", "live", AOI.key(), NOW, NOW)
        with self.assertRaises(LaneViolation):
            engine._run_source(src, FetchRequest(AOI, NOW.replace(day=12), NOW, NOW), Lane.LIVE,
                               ValidationContext(engine.settings, NOW), report, "r")
        self.assertIsInstance(src, FirmsSource)  # shares the live parser path


if __name__ == "__main__":
    unittest.main()
