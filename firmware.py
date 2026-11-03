import io
import json
import logging
import os
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.request

log = logging.getLogger("firmware")

PROTECTED_DIRS = {"data", "recordings", "audio", ".git", "__pycache__"}
PROTECTED_PATTERNS = [
    re.compile(r"^.*\.db(-wal|-shm)?$"),
    re.compile(r"^.*\.env.*$"),
    re.compile(r"^.*\.bak.*$"),
]
CRITICAL_FILES = ["app.py", "modem.py", "static/index.html"]


class FirmwareManager:
    """Manages software/firmware version checking and updates against GitHub release tags."""

    def __init__(self, current_version="1.0.0", repo="sierraindiagolf/orangepi-phone-releases"):
        self.current_version = str(current_version).strip().lstrip('v')
        self.repo = repo

    def fetch_tags(self):
        url = f"https://api.github.com/repos/{self.repo}/tags"
        req = urllib.request.Request(
            url,
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Python-urllib/3.x'}
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode('utf-8'))
                return [tag.get('name') for tag in data if isinstance(tag, dict) and 'name' in tag]
        except Exception as e:
            sys.stderr.write(f"Error fetching github tags for {self.repo}: {e}\n")
            return []

    def _parse_semver(self, tag):
        clean = str(tag).strip().lstrip('v')
        m = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", clean)
        if not m:
            return None
        return tuple(int(x or 0) for x in m.groups())

    def check_updates(self):
        tags = self.fetch_tags()
        curr_tuple = self._parse_semver(self.current_version) or (0, 0, 0)
        latest_tag = self.current_version
        latest_tuple = curr_tuple
        update_available = False
        versions_list = []

        for tag in tags:
            versions_list.append(tag)
            t_tuple = self._parse_semver(tag)
            if t_tuple and t_tuple > latest_tuple:
                latest_tuple = t_tuple
                latest_tag = tag
                update_available = True

        formatted_current = f"v{self.current_version}"
        formatted_latest = latest_tag if str(latest_tag).startswith('v') else f"v{latest_tag}"

        return {
            "current_version": formatted_current,
            "latest_version": formatted_latest,
            "update_available": update_available,
            "versions": versions_list,
            "repo": self.repo,
        }

    def apply_update(self, target_version=None, app_dir=None):
        """Download and install a release archive from GitHub.

        Protects user database, recordings, and audio files from being touched.
        """
        if not target_version:
            status = self.check_updates()
            if not status.get("update_available"):
                return {
                    "success": False,
                    "error": f"No update available (current: {status.get('current_version')})",
                }
            target_version = status.get("latest_version")

        clean_tag = target_version.strip()
        if not clean_tag.startswith("v") and self._parse_semver(clean_tag):
            clean_tag = f"v{clean_tag}"

        if app_dir is None:
            app_dir = os.path.dirname(os.path.abspath(__file__))

        tarball_url = f"https://api.github.com/repos/{self.repo}/tarball/{clean_tag}"
        log.info("Downloading firmware update %s from %s", clean_tag, tarball_url)

        req = urllib.request.Request(
            tarball_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Python-urllib/3.x"}
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                archive_data = resp.read()
        except Exception as exc:
            log.error("Failed to download release archive: %s", exc)
            return {"success": False, "error": f"Download failed: {exc}"}

        if len(archive_data) < 50:
            return {"success": False, "error": "Downloaded archive is too small or invalid"}

        with tempfile.TemporaryDirectory(prefix="phone_update_") as tmpdir:
            try:
                with tarfile.open(fileobj=io.BytesIO(archive_data), mode="r:*") as tar:
                    tar.extractall(path=tmpdir)
            except Exception as exc:
                log.error("Failed to extract tarball: %s", exc)
                return {"success": False, "error": f"Extraction failed: {exc}"}

            entries = os.listdir(tmpdir)
            if len(entries) == 1 and os.path.isdir(os.path.join(tmpdir, entries[0])):
                src_dir = os.path.join(tmpdir, entries[0])
            else:
                src_dir = tmpdir

            # Validate that critical application files exist in the release archive
            for crit in CRITICAL_FILES:
                crit_path = os.path.join(src_dir, *crit.split("/"))
                if not os.path.exists(crit_path):
                    return {
                        "success": False,
                        "error": f"Release archive validation failed: missing {crit}",
                    }

            copied_files = []
            for root, dirs, files in os.walk(src_dir):
                rel_root = os.path.relpath(root, src_dir)
                rel_parts = rel_root.split(os.sep) if rel_root != "." else []

                dirs[:] = [d for d in dirs if d not in PROTECTED_DIRS]
                if any(p in PROTECTED_DIRS for p in rel_parts):
                    continue

                for f in files:
                    if any(pat.match(f) for pat in PROTECTED_PATTERNS):
                        continue

                    src_file = os.path.join(root, f)
                    if rel_root == ".":
                        dest_file = os.path.join(app_dir, f)
                    else:
                        dest_dir = os.path.join(app_dir, rel_root)
                        os.makedirs(dest_dir, exist_ok=True)
                        dest_file = os.path.join(dest_dir, f)

                    src_mode = os.stat(src_file).st_mode
                    shutil.copy2(src_file, dest_file)
                    try:
                        os.chmod(dest_file, src_mode)
                    except OSError:
                        pass
                    copied_files.append(os.path.relpath(dest_file, app_dir))

        return {
            "success": True,
            "installed_version": clean_tag,
            "files_updated": len(copied_files),
        }
