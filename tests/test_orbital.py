import unittest
from datetime import timedelta

from lofgren_intelligence.orbital import (
    TLEError,
    find_passes,
    format_tle,
    look_angles,
    nodal_precession_deg_per_day,
    parse_tle,
    parse_tle_text,
    subpoint,
    tle_checksum,
)

from .helpers import EPOCH, PHOENIX, SUN_SYNC


class TLETests(unittest.TestCase):
    def test_round_trip(self):
        l1, l2 = format_tle(SUN_SYNC)
        self.assertEqual(tle_checksum(l1), int(l1[68]))
        t = parse_tle(l1, l2, "SENTINEL-2A")
        self.assertEqual(t.norad_id, 40697)
        self.assertEqual(t.epoch, EPOCH)
        self.assertAlmostEqual(t.inclination_deg, 98.5692)
        self.assertAlmostEqual(t.eccentricity, 0.0001)
        self.assertAlmostEqual(t.period_minutes, 1440 / 14.30818)

    def test_three_line_text(self):
        l1, l2 = format_tle(SUN_SYNC)
        tles = parse_tle_text(f"SENTINEL-2A\n{l1}\n{l2}\n")
        self.assertEqual(tles[0].name, "SENTINEL-2A")

    def test_bad_checksum_rejected(self):
        l1, l2 = format_tle(SUN_SYNC)
        bad = l1[:68] + str((int(l1[68]) + 1) % 10)
        with self.assertRaises(TLEError):
            parse_tle(bad, l2)


class PropagationTests(unittest.TestCase):
    def test_altitude_matches_sentinel2(self):
        _, _, alt = subpoint(SUN_SYNC, EPOCH + timedelta(minutes=10))
        self.assertTrue(770 < alt < 820, alt)

    def test_overhead_from_subpoint(self):
        t = EPOCH + timedelta(minutes=17)
        lat, lon, alt = subpoint(SUN_SYNC, t)
        el, _, rng = look_angles(SUN_SYNC, t, lat, lon)
        self.assertGreater(el, 89.9)
        self.assertAlmostEqual(rng, alt, delta=1.0)

    def test_sun_synchronous_precession(self):
        # A sun-synchronous orbit's node drifts ~0.9856 deg/day eastward.
        self.assertAlmostEqual(nodal_precession_deg_per_day(SUN_SYNC), 0.9856, delta=0.02)

    def test_passes_over_phoenix(self):
        passes = find_passes(SUN_SYNC, *PHOENIX, EPOCH, hours=48, min_elevation_deg=30)
        self.assertGreaterEqual(len(passes), 2)
        for p in passes:
            self.assertLess(p.rise, p.culmination)
            self.assertLess(p.culmination, p.set)
            self.assertGreaterEqual(p.max_elevation_deg, 30)
            self.assertLess(p.duration_s, 20 * 60)
            el_mid, _, _ = look_angles(SUN_SYNC, p.culmination, *PHOENIX)
            self.assertGreaterEqual(el_mid, 30)


if __name__ == "__main__":
    unittest.main()
