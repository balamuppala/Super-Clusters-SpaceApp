import csv
import tempfile
import unittest
from datetime import date
from pathlib import Path

from fireverify.calendar.actions import _windows, recommend_country
from fireverify.calendar.country import CountryArchive, area_vs_national

YEARS = [2003, 2004, 2005, 2007, 2008, 2009, 2012, 2013, 2015, 2019, 2021, 2025]


def write(path: Path, name: str, header: list[str], rows: list[list]) -> None:
    with (path / name).open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def make_archive(root: Path) -> None:
    annual, monthly, season = [], [], []
    for i, y in enumerate(YEARS):
        rising = 1000 + 100 * i            # "Risingland": clean, robust increase
        mixed = 1000 + (50 if i % 2 else -50)
        annual += [["modis", "Risingland", y, rising, rising // 2, rising, 10, rising, 1.0],
                   ["modis", "Flatland", y, mixed, mixed // 2, mixed, 10, mixed, 1.0]]
        for sat in ("Aqua", "Terra"):
            for m in range(1, 13):
                monthly.append(["modis", "Risingland", y, m, sat, 0, 0, rising // 24, 0, 0, 0, 0])
                monthly.append(["modis", "Flatland", y, m, sat, 0, 0, mixed // 24, 0, 0, 0, 0])
    annual.append(["viirs-jpss1", "Risingland", 2025, 9000, 4000, 9000, 10, 9000, 1.0])
    shares = {1: 0.02, 2: 0.03, 3: 0.20, 4: 0.15, 5: 0.03, 6: 0.02, 7: 0.03, 8: 0.05, 9: 0.05, 10: 0.17, 11: 0.15, 12: 0.10}
    for c in ("Risingland", "Flatland"):
        for m, sh in shares.items():
            idx = 12 * sh
            band = "high" if idx >= 1.5 else "low" if idx < 0.5 else "moderate"
            season.append(["modis", c, "descriptive_all_full_years", 2003, 2025, 12, 12, m, "x", 100 * sh, 100 * sh,
                           1, 200, 250, sh, idx, band, "not_provided"])
    write(root, "annual_country_counts.csv", ["sensor", "country", "year", "retained", "high_confidence",
          "vegetation_all_confidence", "night_retained", "frp_valid_retained", "frp_sum_samples_mw"], annual)
    write(root, "monthly_counts_by_satellite.csv", ["sensor", "country", "year", "month", "satellite", "raw_valid",
          "vegetation_all_confidence", "retained", "high_confidence", "night_retained", "frp_valid_retained",
          "frp_sum_samples_mw"], monthly)
    write(root, "seasonal_calendar.csv", ["sensor", "country", "baseline", "first_year", "last_year", "years",
          "years_with_retained_detections", "month", "month_name", "mean_detections", "median_detections", "p10",
          "p90", "p95", "mean_annual_share", "seasonal_index", "activity_band", "observation_coverage"], season)
    write(root, "historical_anomaly_candidates.csv", ["sensor", "country", "year", "month", "month_name", "detections",
          "prior_same_month_years", "historical_median", "ratio_to_median", "excess_over_median", "robust_z_log1p",
          "high_confidence_ratio", "interpretation", "modis_orbital_drift_review"],
          [["modis", "Risingland", 2025, 8, "Aug", 5000, 11, 400, 12.5, 4600, 5.1, 10.0, "candidate", "True"]])


class CountryArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        make_archive(Path(cls.tmp.name))
        cls.arch = CountryArchive(Path(cls.tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_missing_archive_is_reported(self):
        self.assertFalse(CountryArchive(Path(self.tmp.name) / "nope").available)

    def test_robust_rising_trend(self):
        p = self.arch.profile("Risingland")
        self.assertEqual(p["trend"]["direction"], "increasing")
        self.assertTrue(p["robustness"]["robust"])
        self.assertAlmostEqual(p["trend"]["slope_per_year"], 100 * 11 / 22, delta=15)   # real year gaps
        self.assertEqual(p["annual_viirs"]["viirs-jpss1"][0]["retained"], 9000)
        ids = [g["id"] for g in recommend_country(p, date(2026, 11, 15))["groups"]]
        self.assertEqual(ids[0], "national_trend")              # high priority, robust rise
        self.assertIn("national_anomalies", ids)
        self.assertIn("national_calendar", ids)

    def test_no_trend_and_bimodal_calendar(self):
        p = self.arch.profile("Flatland")
        self.assertFalse(p["robustness"]["robust"])
        out = recommend_country(p, date(2026, 11, 15))
        cal = next(g for g in out["groups"] if g["id"] == "national_calendar")
        self.assertIn("Mar–Apr and Oct–Nov", cal["finding"])     # two windows kept apart
        self.assertTrue(any("more than one season" in a["title"] for a in cal["actions"]))

    def test_windows_wrap_year_end(self):
        self.assertEqual(sorted(_windows([12, 1, 2, 7])), [[7], [12, 1, 2]])

    def test_area_vs_national(self):
        p = self.arch.profile("Flatland")
        a = area_vs_national(p, {8: 80.0, 9: 20.0})
        self.assertEqual(a["area_peak"], "Aug")
        self.assertEqual(a["national_peak"], "Mar")
        self.assertLess(a["overlap"], 0.2)

    def test_guess_from_bbox(self):
        real = CountryArchive(Path(self.tmp.name))
        self.assertIsNone(real.guess([0, -1, 1, 0]))            # centre in the Gulf of Guinea: no box


@unittest.skipUnless((Path(__file__).resolve().parent.parent / "data/external/burning_activity_calendar/annual_country_counts.csv").is_file(),
                     "country archive not unpacked")
class RealArchiveTests(unittest.TestCase):
    def test_matches_handoff_numbers(self):
        arch = CountryArchive(Path(__file__).resolve().parent.parent / "data/external/burning_activity_calendar")
        self.assertEqual(arch.guess([-9, 41.8, -5, 43.8]), "Spain")
        self.assertEqual(arch.guess([-123, 37, -119, 41]), "United States")
        spain = arch.profile("Spain")
        self.assertAlmostEqual(spain["comparison"]["pct"], -21.3, delta=0.1)
        self.assertFalse(spain["robustness"]["agree"])             # high-confidence subset flips direction
        self.assertTrue(arch.profile("India")["robustness"]["robust"])


if __name__ == "__main__":
    unittest.main()
