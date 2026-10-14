import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import urllib.error

from df2pq import updates


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class VersionTests(unittest.TestCase):
    def test_is_newer_handles_prefix_and_length(self):
        self.assertTrue(updates.is_newer("v0.2.0", "0.1.9"))
        self.assertTrue(updates.is_newer("1.0", "0.9.9"))
        self.assertFalse(updates.is_newer("v0.1.0", "0.1"))
        self.assertFalse(updates.is_newer("0.1.0", "0.2.0"))
        self.assertTrue(updates.parse_version("junk") == (0,))

    def test_changelog_is_bundled(self):
        self.assertIn("## [0.1.0]", updates.changelog_text())


class FetchTests(unittest.TestCase):
    def test_parses_release(self):
        body = json.dumps({"tag_name": "v0.2.0", "body": "Notes", "html_url": "https://x/y",
                           "published_at": "2026-10-20T10:00:00Z"}).encode()
        release = updates.fetch_latest(opener=lambda *_a, **_k: _Response(body))
        self.assertEqual((release.version, release.notes, release.published),
                         ("0.2.0", "Notes", "2026-10-20"))

    def test_no_releases_returns_none(self):
        def opener(*_a, **_k):
            raise urllib.error.HTTPError("u", 404, "nf", {}, None)
        self.assertIsNone(updates.fetch_latest(opener=opener))

    def test_network_failure_is_user_presentable(self):
        def opener(*_a, **_k):
            raise urllib.error.URLError("offline")
        with self.assertRaises(updates.UpdateError):
            updates.fetch_latest(opener=opener)


class GitUpdateTests(unittest.TestCase):
    def test_refuses_dirty_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "f.txt").write_text("x")
            with self.assertRaisesRegex(updates.UpdateError, "local changes"):
                updates.apply_git_update(root)


if __name__ == "__main__":
    unittest.main()
