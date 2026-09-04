import os
import sys
import unittest

# Ensure parent directory is in sys.path when running script directly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from firmware import FirmwareManager


class TestFirmwareManager(unittest.TestCase):
    def test_semver_parsing(self):
        fm = FirmwareManager(current_version="1.0.0")
        self.assertEqual(fm._parse_semver("v1.2.3"), (1, 2, 3))
        self.assertEqual(fm._parse_semver("0.4"), (0, 4, 0))
        self.assertEqual(fm._parse_semver("v2.1"), (2, 1, 0))
        self.assertIsNone(fm._parse_semver("invalid-tag"))

    def test_version_comparison_with_update(self):
        fm = FirmwareManager(current_version="1.0.0")
        fm.fetch_tags = lambda: ["v1.0.0", "v1.1.0", "v0.9.0"]
        res = fm.check_updates()
        self.assertTrue(res["update_available"])
        self.assertEqual(res["current_version"], "v1.0.0")
        self.assertEqual(res["latest_version"], "v1.1.0")
        self.assertEqual(len(res["versions"]), 3)

    def test_version_comparison_up_to_date(self):
        fm = FirmwareManager(current_version="1.1.0")
        fm.fetch_tags = lambda: ["v1.0.0", "v1.1.0", "v0.9.0"]
        res = fm.check_updates()
        self.assertFalse(res["update_available"])
        self.assertEqual(res["current_version"], "v1.1.0")
        self.assertEqual(res["latest_version"], "v1.1.0")

    def test_empty_tags_graceful_fallback(self):
        fm = FirmwareManager(current_version="1.0.0")
        fm.fetch_tags = lambda: []
        res = fm.check_updates()
        self.assertFalse(res["update_available"])
        self.assertEqual(res["current_version"], "v1.0.0")
        self.assertEqual(res["latest_version"], "v1.0.0")
        self.assertEqual(res["versions"], [])


if __name__ == "__main__":
    unittest.main()
