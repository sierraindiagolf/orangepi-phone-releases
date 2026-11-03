import io
import os
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch, MagicMock

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

    def _create_mock_tarball(self, files_dict, root_dir="repo-release-12345"):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for path, content in files_dict.items():
                full_path = f"{root_dir}/{path}" if root_dir else path
                data = content.encode("utf-8")
                ti = tarfile.TarInfo(name=full_path)
                ti.size = len(data)
                tar.addfile(ti, io.BytesIO(data))
        buf.seek(0)
        return buf.getvalue()

    def test_apply_update_success_and_preservation(self):
        fm = FirmwareManager(current_version="1.0.0")
        test_tar = self._create_mock_tarball({
            "app.py": "VERSION = '1.0.3'\n",
            "modem.py": "# new modem\n",
            "static/index.html": "<html>new</html>\n",
            "data/phone.db": "CORRUPT OVERWRITE\n",
            "recordings/call1.wav": "RECORDING OVERWRITE\n",
        })

        with tempfile.TemporaryDirectory() as target_dir:
            # Pre-create database and recording that must survive
            data_dir = os.path.join(target_dir, "data")
            rec_dir = os.path.join(target_dir, "recordings")
            os.makedirs(data_dir, exist_ok=True)
            os.makedirs(rec_dir, exist_ok=True)
            db_file = os.path.join(data_dir, "phone.db")
            rec_file = os.path.join(rec_dir, "saved.wav")
            with open(db_file, "w") as f:
                f.write("ORIGINAL DB")
            with open(rec_file, "w") as f:
                f.write("ORIGINAL REC")

            mock_resp = MagicMock()
            mock_resp.read.return_value = test_tar
            mock_resp.__enter__.return_value = mock_resp

            with patch("urllib.request.urlopen", return_value=mock_resp):
                res = fm.apply_update(target_version="v1.0.3", app_dir=target_dir)

            self.assertTrue(res["success"])
            self.assertEqual(res["installed_version"], "v1.0.3")

            # Verify files were updated
            with open(os.path.join(target_dir, "app.py"), "r") as f:
                self.assertEqual(f.read(), "VERSION = '1.0.3'\n")
            with open(os.path.join(target_dir, "static", "index.html"), "r") as f:
                self.assertEqual(f.read(), "<html>new</html>\n")

            # Verify persistent data was NOT overwritten
            with open(db_file, "r") as f:
                self.assertEqual(f.read(), "ORIGINAL DB")
            with open(rec_file, "r") as f:
                self.assertEqual(f.read(), "ORIGINAL REC")

    def test_apply_update_validation_missing_critical(self):
        fm = FirmwareManager(current_version="1.0.0")
        test_tar = self._create_mock_tarball({
            "modem.py": "# only modem, missing app.py\n",
        })

        with tempfile.TemporaryDirectory() as target_dir:
            mock_resp = MagicMock()
            mock_resp.read.return_value = test_tar
            mock_resp.__enter__.return_value = mock_resp

            with patch("urllib.request.urlopen", return_value=mock_resp):
                res = fm.apply_update(target_version="v1.0.3", app_dir=target_dir)

            self.assertFalse(res["success"])
            self.assertIn("missing app.py", res["error"])


if __name__ == "__main__":
    unittest.main()
