import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fireverify.calendar import analyze, day_detail, harmonize, points_from_rows
from fireverify.calendar.actions import next_season, recommend
from fireverify.calendar.backfill import default_availability, plan, run_backfill
from fireverify.calendar.harmonize import Point, solar_date
from fireverify.calendar.trends import mann_kendall, score_injected, seasonal_kendall, sens_slope
from fireverify.config import Settings
from fireverify.dashboard.server import ApiError, DashboardApp
from fireverify.ingestion.engine import IngestionEngine
from fireverify.ingestion.store import Store
from fireverify.models import BBox, Lane

UTC = timezone.utc


def pt(sensor, lat, lon, hour=11, day=date(2026, 8, 1), frp=10.0, conf="nominal", platform=None):
    t = datetime(day.year, day.month, day.day, hour, 0, tzinfo=UTC)
    return Point(sensor, platform or ("Aqua" if sensor == "MODIS" else "NOAA-20"), t, lat, lon, frp, conf)


class PlanTests(unittest.TestCase):
    def test_sp_then_nrt_and_viirs_fallback(self):
        av = {"MODIS_SP": (date(2000, 11, 1), date(2026, 8, 31)), "MODIS_NRT": (date(2026, 9, 1), date(2026, 11, 15)),
              "VIIRS_NOAA20_SP": (date(2018, 4, 1), date(2026, 7, 31)),
              "VIIRS_NOAA20_NRT": (date(2026, 8, 1), date(2026, 11, 15)),
              "VIIRS_SNPP_SP": (date(2012, 1, 20), date(2026, 7, 31))}
        chunks, uncovered = plan("MODIS", date(2026, 8, 25), date(2026, 9, 6), av)
        self.assertEqual(uncovered, [])
        self.assertTrue(all(c.span <= 5 for c in chunks))
        self.assertEqual({c.source for c in chunks if c.last <= date(2026, 8, 31)}, {"MODIS_SP"})
        self.assertEqual({c.source for c in chunks if c.day >= date(2026, 9, 1)}, {"MODIS_NRT"})
        self.assertEqual(sum(c.span for c in chunks), 13)
        chunks, _ = plan("VIIRS", date(2017, 1, 1), date(2017, 1, 3), av)
        self.assertEqual(chunks[0].source, "VIIRS_SNPP_SP")          # before NOAA-20 existed
        _, uncovered = plan("VIIRS", date(2026, 11, 16), date(2026, 11, 17), av)
        self.assertEqual(len(uncovered), 2)


class HarmonizeTests(unittest.TestCase):
    def test_solar_date(self):
        # 23:30 UTC at 30°E is 01:30 local the next day.
        self.assertEqual(solar_date(datetime(2026, 8, 1, 23, 30, tzinfo=UTC), 30.0), date(2026, 8, 2))
        self.assertEqual(solar_date(datetime(2026, 8, 2, 7, 0, tzinfo=UTC), -115.0), date(2026, 8, 1))

    def test_cells_neighbour_matching_and_frp(self):
        d = date(2026, 8, 1)
        points = [
            pt("MODIS", 10.005, 20.005, frp=30),                    # cell A
            pt("VIIRS", 10.006, 20.016, frp=5), pt("VIIRS", 10.004, 20.017, frp=6),   # neighbour of A (2 px, 1 cell)
            pt("VIIRS", 10.505, 20.505, frp=2),                     # VIIRS-only fire
            pt("MODIS", 11.005, 21.005, frp=20),                    # MODIS-only
            pt("VIIRS", 12.0, 22.0, conf="low"),                    # dropped: low confidence
        ]
        h = harmonize(points, d, d, {"MODIS": {d.isoformat(): "ok"}, "VIIRS": {d.isoformat(): "ok"}})
        day = h.days[0]
        self.assertEqual((day.modis_cells, day.viirs_cells), (2, 2))
        self.assertEqual(day.viirs_pixels, 3)
        self.assertEqual(day.modis_confirmed, 1)
        self.assertEqual(day.harmonized_cells, 3)               # 2 MODIS + 1 VIIRS cell MODIS missed
        self.assertEqual(day.frp_mw, 50.0)                      # one overpass per platform: max(MODIS 50, VIIRS 13)
        self.assertTrue(day.complete)
        detail = day_detail(points, d)
        self.assertEqual(sorted(c["seen_by"] for c in detail["cells"]), ["both", "modis", "viirs"])

    def test_one_sensor_days_are_estimated(self):
        d0 = date(2026, 8, 1)
        pts = [pt("MODIS", 10.005, 20.005, day=d0), pt("VIIRS", 10.005, 20.005, day=d0),
               pt("VIIRS", 10.505, 20.505, day=d0), pt("VIIRS", 10.805, 20.805, day=d0 + timedelta(days=1))]
        cov = {"MODIS": {d0.isoformat(): "ok", (d0 + timedelta(days=1)).isoformat(): "unavailable"},
               "VIIRS": {d0.isoformat(): "ok", (d0 + timedelta(days=1)).isoformat(): "ok"}}
        h = harmonize(pts, d0, d0 + timedelta(days=2), cov)
        self.assertEqual(h.calibration["harmonized_per_viirs_cell"], 1.0)
        self.assertEqual(h.days[1].estimate, 1.0)
        self.assertIn("VIIRS only", h.days[1].estimated)
        self.assertFalse(h.days[2].has_data)                    # nothing fetched, nothing seen
        self.assertIsNone(h.days[2].estimate)

    def test_duplicate_detections_collapse(self):
        row = (10.0, 20.0, datetime(2026, 8, 1, 11, tzinfo=UTC), "VIIRS_NOAA20_NRT", "VIIRS", "NOAA-20", 5.0, "nominal")
        self.assertEqual(len(points_from_rows([row, row[:3] + ("VIIRS_NOAA20_SP",) + row[4:]])), 1)


class TrendTests(unittest.TestCase):
    def test_mann_kendall_and_sen(self):
        self.assertLess(mann_kendall(list(range(20)))["p_value"], 0.001)
        self.assertGreater(mann_kendall([5, 3, 6, 4, 5, 3, 6, 4] * 2)["p_value"], 0.2)
        self.assertAlmostEqual(sens_slope([1, 3, 5, 7, 9]), 2.0)
        sk = seasonal_kendall({m: [(2024, 10 + m), (2025, 20 + m)] for m in range(1, 13)})
        self.assertLess(sk["p_value"], 0.01)
        self.assertAlmostEqual(sk["slope_per_year"], 10.0)

    def test_score_injected(self):
        found = [{"type": "sensor_gap", "sensor": "MODIS", "start": "2026-07-01", "end": "2026-07-05"},
                 {"type": "spike", "start": "2026-08-10", "end": "2026-08-12"}]
        inj = [{"type": "modis_gap", "start": "2026-07-01", "end": "2026-07-07"},
               {"type": "spike", "start": "2026-08-13", "end": "2026-08-14"},
               {"type": "off_season", "start": "2026-02-01", "end": "2026-02-03"}]
        self.assertEqual([i["detected"] for i in score_injected(inj, found)], [True, True, False])


class ActionTests(unittest.TestCase):
    def test_next_season_projection(self):
        seasons = [{"start": "2026-06-01", "end": "2026-09-30", "peak_week_center": "2026-08-10"}]
        nxt = next_season(seasons, date(2026, 11, 15))
        self.assertEqual((nxt["start"], nxt["peak"]), (date(2027, 6, 1), date(2027, 8, 10)))
        self.assertFalse(nxt["ongoing"])
        self.assertTrue(next_season(seasons, date(2026, 7, 1))["ongoing"])

    def test_groups_follow_findings(self):
        analysis = {
            "trend": {"status": "ok", "direction": "increasing", "p_value": 0.001, "pct_per_year": 30.0,
                      "caveat": None},
            "recent": {"window_days": 30, "last": 900, "previous": 300, "ratio": 3.0},
            "totals": {"harmonized_cells": 3650, "days_with_data": 365},
            "seasons": [{"start": "2026-06-01", "end": "2026-09-30", "peak_week_center": "2026-08-10"}],
            "anomalies": [{"type": "sensor_gap", "start": "2026-07-01", "end": "2026-07-05", "severity": "high"}],
        }
        out = recommend(analysis, date(2026, 8, 1))
        ids = [g["id"] for g in out["groups"]]
        self.assertEqual(ids[0], "recent_surge")                # urgent first
        self.assertIn("trend_rising", ids)
        self.assertIn("anomaly_sensor_gap", ids)
        self.assertTrue(all(a["lead"] and a["timeframe"] for g in out["groups"] for a in g["actions"]))
        # A rise from a tiny off-season base is not a surge.
        analysis["recent"] = {"window_days": 30, "last": 40, "previous": 10, "ratio": 4.0}
        self.assertNotIn("recent_surge", [g["id"] for g in recommend(analysis, date(2026, 8, 1))["groups"]])


class BackfillEndToEndTests(unittest.TestCase):
    """Simulated year: planted anomalies must be found through the real parse/validate/harmonize path."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / "cal.db"
        cls.store = Store(cls.db)
        cls.engine = IngestionEngine(Settings(enable_overpass=False), cls.store)
        cls.bbox = BBox(26.0, -16.0, 28.0, -14.0)
        cls.start, cls.end = date(2025, 11, 15), date(2026, 11, 14)
        cls.out = run_backfill(cls.engine, "simulated", cls.bbox, cls.start, cls.end, seed=3)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.tmp.cleanup()

    def test_backfill_stores_and_covers(self):
        self.assertGreater(self.out["totals"]["accepted"], 10_000)
        self.assertEqual(self.out["totals"]["rejected"], 0)
        self.assertEqual(self.out["totals"]["quarantined"], 0)              # nothing simulated after "now"
        cov = self.store.coverage_for(Lane.SIMULATED, self.bbox, self.start, self.end)
        self.assertEqual(len(cov["MODIS"]), 365)
        self.assertEqual(self.store.query_records(Lane.LIVE), [])           # lanes stay separate
        again = run_backfill(self.engine, "simulated", self.bbox, self.start, self.end, seed=3)
        # Settled chunks are skipped; only the newest (still near real-time) ones are re-fetched, adding nothing.
        self.assertLessEqual(again["chunks"], 2)
        self.assertGreater(again["chunks_skipped"], 140)
        self.assertEqual(again["totals"]["accepted"], 0)

    def test_planted_anomalies_detected(self):
        app = DashboardApp(Settings(enable_overpass=False), self.db)
        res = app.calendar(Lane.SIMULATED, {"bbox": [",".join(map(str, self.bbox.as_list()))],
                                            "start": [self.start.isoformat()], "end": [self.end.isoformat()]})
        self.assertEqual(len(res["days"]), 365)
        self.assertEqual(len(res["injected"]), 3)
        self.assertTrue(all(i["detected"] for i in res["injected"]), res["injected"])
        cal = res["calibration"]
        self.assertGreater(cal["viirs_cells_per_modis_cell"], 1.0)          # VIIRS sees more small fires
        self.assertEqual(res["analysis"]["trend"]["status"], "ok")
        self.assertEqual(len(res["analysis"]["seasons"]), 1)
        self.assertTrue(res["analysis"]["actions"]["groups"])

    def test_api_validation(self):
        app = DashboardApp(Settings(enable_overpass=False), self.db)
        for body in ({"mode": "simulated", "bbox": [0, 0, 10, 10], "start": "2026-01-01", "end": "2026-02-01"},
                     {"mode": "simulated", "bbox": [0, 0, 1, 1], "start": "2026-02-01", "end": "2026-01-01"},
                     {"mode": "simulated", "bbox": [0, 0, 1, 1], "start": "1999-01-01", "end": "2026-01-01"},
                     {"mode": "live", "bbox": [0, 0, 1, 1], "start": "2026-01-01", "end": "2026-02-01"}):
            with self.assertRaises(ApiError, msg=body):
                app.start_calendar_job(body)

    def test_default_availability_switches_to_nrt(self):
        av = default_availability(date(2026, 11, 15))
        self.assertLess(av["MODIS_SP"][1], av["MODIS_NRT"][0])


if __name__ == "__main__":
    unittest.main()


class LongTermTests(unittest.TestCase):
    def test_sens_slope_uses_real_year_gaps(self):
        from fireverify.calendar.longterm import _trend, sens_slope_xy
        self.assertAlmostEqual(sens_slope_xy([2001, 2006, 2011], [100, 150, 200]), 10.0)
        t = _trend([(2001, 100.0), (2006, 160.0), (2011, 190.0), (2016, 260.0), (2021, 330.0), (2025, 360.0)])
        self.assertEqual(t["direction"], "increasing")
        self.assertLess(t["p_value"], 0.05)
        self.assertAlmostEqual(t["slope_per_year"], 11.75, delta=1.5)       # per year, not per sample
        self.assertIsNone(_trend([(2001, 1.0), (2006, 2.0)]))

    def test_sampled_years_plan(self):
        from fireverify.calendar.backfill import sampled_years
        plan_ = sampled_years(date(2026, 11, 15))
        years = [y for y, _ in plan_]
        self.assertEqual(sorted(years), [2001, 2006, 2011, 2016, 2021, 2025])
        self.assertEqual(years[:2], [2025, 2001])                            # widest spread first
        self.assertEqual({y for y, f in plan_ if "VIIRS" in f}, {2021, 2025})

    def test_longterm_actions_for_shifting_season(self):
        from fireverify.calendar.actions import recommend_longterm
        lt = {"trend_modis": {"direction": "increasing", "p_value": 0.01, "pct_per_year": 4.0, "slope_per_year": 20,
                              "first_year": 2001},
              "decades": [{"decade": "2001–2010", "mean_modis_cells": 500.0}],
              "season_shift": {"start": {"significant": True, "days_per_decade": -9.0},
                               "end": None, "length": {"significant": True, "days_per_decade": 12.0}},
              "unusual_years": [{"year": 2021, "kind": "extreme", "ratio_to_median": 3.1}]}
        ids = [g["id"] for g in recommend_longterm(lt)["groups"]]
        self.assertEqual(set(ids), {"longterm_trend_rising", "longterm_season_shift", "longterm_extreme_years"})

    def test_longterm_on_simulated_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "lt.db")
            engine = IngestionEngine(Settings(enable_overpass=False), store)
            bbox = BBox(26.0, -16.0, 27.0, -15.0)
            for y in (2021, 2025):
                run_backfill(engine, "simulated", bbox, date(y, 1, 1), date(y, 12, 31), seed=4,
                             families=("MODIS",) if y == 2021 else ("MODIS", "VIIRS"))
            from fireverify.calendar.longterm import analyze_longterm
            lt = analyze_longterm(store, Lane.SIMULATED, bbox)
            store.close()
        self.assertEqual(lt["status"], "ok")
        self.assertTrue(lt["sampled"])
        full = {y["year"]: y for y in lt["years"] if y["full"]}
        self.assertEqual(set(full), {2021, 2025})
        self.assertEqual(full[2021]["viirs_days"], 0)                         # MODIS-only year...
        self.assertGreater(full[2021]["harmonized"], full[2021]["modis"])     # ...scaled up by the VIIRS calibration
