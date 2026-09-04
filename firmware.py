import json
import re
import sys
import urllib.request

class FirmwareManager:
    """Manages software/firmware version checking against GitHub release tags."""

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
