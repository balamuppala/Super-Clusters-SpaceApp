import copy
import math
import unittest
from datetime import date, datetime, timezone

from fireverify.config import Settings
from fireverify.ingestion.engine import IngestionEngine
from fireverify.ingestion.sources.firms import FirmsHistoricalSource
from fireverify.ingestion.store import Store
from fireverify.models import BBox, Lane, Mode
from fireverify.verification import DecisionPolicy, LikelihoodTable, NaiveBayesFusion, Verifier
from fireverify.verification.calibration import Example, evaluate, fit, split
from fireverify.verification.decision import attribution
from fireverify.verification.evidence import EvidenceVector, extract_evidence
from fireverify.verification.model import DEFAULT_TABLE

NOW = datetime(2026, 11, 14, 18, 0, tzinfo=timezone.utc)
AOI = BBox(-121.0, 38.0, -119.0, 40.0)


def derived(**overrides):
    base = {
        "cluster_summary": {"n_detections": 30, "n_overpasses": 6, "sensors": ["MODIS", "VIIRS"],
                            "max_frp_mw": 80.0, "radius_km": 3.0, "duration_hours": 20},
        "vegetation_loss": {"status": "complete", "ndvi_pre": 0.7, "dndvi": 0.4, "dnbr": 0.55},
        "sar_backscatter_change": {"status": "complete", "dvh_db": -2.6},
        "temporal_frequency": {"recurrence_class": "none", "history_checked": True},
        "context_proximity": {"features_considered": 3, "nearest": {}, "land_cover": ["forest"]},
    }
    for k, v in overrides.items():
        base[k] = {**base[k], **v}
    return base


class EvidenceTests(unittest.TestCase):
    def test_states(self):
        ev = extract_evidence("c", derived())
        self.assertEqual(ev.states, {"optical": "high", "sar": "strong_drop",
                                     "temporal": "none", "heat_source": "none",
                                     "thermal": "multi_sensor_repeat", "spread": "spreading"})
        self.assertEqual(ev.land_cover, ["forest"])

    def test_missing_and_special_states(self):
        ev = extract_evidence("c", derived(
            vegetation_loss={"status": "quarantined", "quarantine_reasons": ["Q_CLOUDY"]},
            context_proximity={"near_gas_flare": True, "near_industrial": True,
                               "nearest": {"gas_flare": {"distance_km": 0.1, "feature_id": "f1", "name": "F"}}},
            cluster_summary={"n_overpasses": 1, "n_detections": 1, "max_frp_mw": 1.0, "radius_km": 0.5}))
        self.assertIsNone(ev.states["optical"])
        self.assertIn("Q_CLOUDY", ev.details["optical"])
        self.assertEqual(ev.states["heat_source"], "gas_flare")   # strongest source wins, counted once
        self.assertEqual(ev.states["thermal"], "single_weak")
        ev = extract_evidence("c", derived(vegetation_loss={"ndvi_pre": 0.12}))
        self.assertEqual(ev.states["optical"], "non_vegetated")


class FusionTests(unittest.TestCase):
    def test_log_odds_is_prior_plus_sum_of_log_lrs(self):
        table = LikelihoodTable.default()
        ev = extract_evidence("c", derived())
        post = NaiveBayesFusion(table).fuse(ev)
        expected = math.log(0.55 / 0.45) + sum(math.log(table.lr(g, s)) for g, s in ev.observed.items())
        self.assertAlmostEqual(post.log_odds, expected)
        self.assertAlmostEqual(post.probability, 1 / (1 + math.exp(-expected)))

    def test_missing_evidence_is_neutral(self):
        ev = EvidenceVector("c", states={"optical": None, "sar": None, "temporal": None, "heat_source": None,
                                         "thermal": None, "spread": None})
        post = NaiveBayesFusion().fuse(ev)
        self.assertAlmostEqual(post.probability, DEFAULT_TABLE["default_prior"])
        self.assertEqual(len(post.missing), 6)

    def test_table_validation(self):
        bad = copy.deepcopy(DEFAULT_TABLE)
        bad["groups"]["sar"]["fire"]["drop"] = 0.9
        with self.assertRaises(ValueError):
            LikelihoodTable(bad)
        bad = copy.deepcopy(DEFAULT_TABLE)
        bad["groups"]["sar"]["fire"]["drop"] = 0.0
        with self.assertRaises(ValueError):
            LikelihoodTable(bad)

    def test_lr_is_capped(self):
        data = copy.deepcopy(DEFAULT_TABLE)
        data["max_lr"] = 5.0
        self.assertEqual(LikelihoodTable(data).lr("optical", "high"), 5.0)


class DecisionTests(unittest.TestCase):
    def test_labels_and_thin_evidence_downgrade(self):
        p = DecisionPolicy()
        self.assertEqual(p.label(0.95, 6), "confirmed_fire")
        self.assertEqual(p.label(0.95, 2), "likely_fire")
        self.assertEqual(p.label(0.45, 6), "uncertain")
        self.assertEqual(p.label(0.01, 6), "false_alarm")
        self.assertEqual(p.label(0.01, 2), "likely_false_alarm")

    def test_attribution_picks_strongest_negative_term(self):
        ev = extract_evidence("c", derived(
            vegetation_loss={"dnbr": 0.02, "dndvi": 0.01},
            temporal_frequency={"recurrence_class": "persistent"},
            context_proximity={"near_gas_flare": True}))
        self.assertEqual(attribution(NaiveBayesFusion().fuse(ev)), "gas_flare")
        self.assertIsNone(attribution(NaiveBayesFusion().fuse(extract_evidence("c", derived()))))


class CalibrationTests(unittest.TestCase):
    def test_fit_moves_towards_counts_without_zeros(self):
        base = LikelihoodTable.default()
        examples = [Example(extract_evidence(f"f{i}", derived()), True) for i in range(20)]
        examples += [Example(extract_evidence(f"n{i}", derived(temporal_frequency={"recurrence_class": "persistent"})),
                             False) for i in range(20)]
        fitted = fit(examples, base, strength=10)
        self.assertGreater(fitted.p("temporal", "persistent", "not_fire"), base.p("temporal", "persistent", "not_fire"))
        self.assertGreater(fitted.p("temporal", "seasonal", "fire"), 0)   # never zero
        self.assertEqual(fitted.data["meta"]["fitted_examples"], 40)
        m = evaluate(examples, fitted)
        self.assertEqual(m["confusion"]["tp"] + m["confusion"]["tn"], 40)

    def test_split_is_deterministic_and_disjoint(self):
        examples = [Example(EvidenceVector(f"evt_{i}"), i % 2 == 0) for i in range(100)]
        a1, b1 = split(examples, 0.3)
        a2, b2 = split(examples, 0.3)
        self.assertEqual([e.evidence.cluster_id for e in b1], [e.evidence.cluster_id for e in b2])
        self.assertFalse({e.evidence.cluster_id for e in a1} & {e.evidence.cluster_id for e in b1})


class HistoryWindowTests(unittest.TestCase):
    def test_offseason_windows(self):
        src = FirmsHistoricalSource("VIIRS_SNPP_SP", Settings(history_years=3), None)
        windows = src.history_windows(date(2026, 11, 14))
        self.assertEqual(len(windows), 6)
        self.assertTrue(any(w[0].month in (5, 6) for w in windows))   # ~6 months off-season
        self.assertTrue(all(w[0] < date(2026, 11, 14) for w in windows))


class VerifierEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = Store(":memory:")
        engine = IngestionEngine(Settings(enable_overpass=False), cls.store)
        engine.run(Mode.SIMULATED, AOI, now=NOW, seed=11)
        cls.verifier = Verifier(cls.store)
        cls.verdicts = cls.verifier.verify_all(Lane.SIMULATED)
        cls.by_truth = {}
        for v in cls.verdicts:
            members = cls.store.latest_derived(Lane.SIMULATED, v.cluster_id)["cluster_summary"]["value"]["member_ids"]
            cls.by_truth[cls.verifier._sim_truth(Lane.SIMULATED, members)[1]] = v

    def test_scenarios_get_the_right_verdicts(self):
        bt = self.by_truth
        self.assertEqual(bt["wildfire"].label, "confirmed_fire")
        self.assertIn(bt["agricultural_burn"].label, ("confirmed_fire", "likely_fire"))
        self.assertEqual((bt["industrial_gas_flare"].label, bt["industrial_gas_flare"].attribution),
                         ("false_alarm", "gas_flare"))
        self.assertEqual(bt["volcanic_activity"].attribution, "volcanic_activity")
        self.assertEqual(bt["false_alarm_hot_surface"].attribution, "urban_hot_surface")
        self.assertLess(bt["unmapped_gas_flare"].probability, 0.1)   # caught without a context layer
        self.assertEqual(bt["unmapped_gas_flare"].attribution, "persistent_heat_source")

    def test_verdicts_are_stored_once_and_lane_tagged(self):
        self.assertEqual(len(self.store.latest_verdicts(Lane.SIMULATED, 100)), len(self.verdicts))
        self.assertEqual(self.store.latest_verdicts(Lane.LIVE, 100), [])
        again = self.verifier.verify_all(Lane.SIMULATED)
        rows = self.store.conn.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0]
        self.assertEqual(rows, len(again))   # identical inputs -> no duplicate verdict rows

    def test_simulation_truth_examples(self):
        m = evaluate(self.verifier.examples(Lane.SIMULATED), self.verifier.table)
        self.assertEqual(m["n"], 7)
        self.assertGreaterEqual(m["accuracy"], 6 / 7)


if __name__ == "__main__":
    unittest.main()
