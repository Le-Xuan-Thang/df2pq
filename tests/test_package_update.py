"""In-app updates for RPM/DEB installs: pick, download, verify, install with pkexec."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from df2pq import updates

RPM = b"rpm package bytes" * 1000
DEB = b"deb package bytes" * 1000


class _Response(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def release(sums=True, rpm=RPM):
    assets = [("df2pq-0.3.0-1.x86_64.rpm", "https://h/rpm"),
              ("df2pq_0.3.0_amd64.deb", "https://h/deb"),
              ("df2pq-windows-x86_64.zip", "https://h/zip")]
    if sums:
        assets.append((updates.CHECKSUMS, "https://h/sums"))
    files = {"https://h/rpm": rpm, "https://h/deb": DEB, "https://h/sums": (
        f"{hashlib.sha256(RPM).hexdigest()}  df2pq-0.3.0-1.x86_64.rpm\n"
        f"{hashlib.sha256(DEB).hexdigest()} *df2pq_0.3.0_amd64.deb\n").encode()}
    opener = lambda request, timeout: _Response(files[request.full_url])  # noqa: E731
    return updates.Release("v0.3.0", "", "https://h", "", tuple(assets)), opener


class PackageUpdateTests(unittest.TestCase):
    def test_release_assets_are_parsed(self):
        body = json.dumps({"tag_name": "v0.3.0", "assets": [
            {"name": "SHA256SUMS", "browser_download_url": "https://h/sums"}]}).encode()
        got = updates.fetch_latest(opener=lambda *_a, **_k: _Response(body))
        self.assertEqual(got.assets, (("SHA256SUMS", "https://h/sums"),))

    def test_picks_the_package_for_this_system(self):
        rel, _ = release()
        self.assertEqual(updates.package_asset(rel, "rpm", "x86_64")[0],
                         "df2pq-0.3.0-1.x86_64.rpm")
        self.assertEqual(updates.package_asset(rel, "deb", "x86_64")[0], "df2pq_0.3.0_amd64.deb")
        with self.assertRaisesRegex(updates.UpdateError, "no RPM package for aarch64"):
            updates.package_asset(rel, "rpm", "aarch64")

    def test_download_is_verified(self):
        rel, opener = release()
        seen = []
        with tempfile.TemporaryDirectory() as tmp:
            path = updates.download_package(rel, "deb", Path(tmp), lambda d, t: seen.append(d),
                                            opener=opener, machine="x86_64")
            self.assertEqual(path.read_bytes(), DEB)
        self.assertEqual(seen[-1], len(DEB))

    def test_tampered_or_unverifiable_downloads_are_refused(self):
        rel, opener = release(rpm=RPM + b"x")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(updates.UpdateError, "does not match"):
                updates.download_package(rel, "rpm", Path(tmp), opener=opener, machine="x86_64")
            self.assertEqual(list(Path(tmp).iterdir()), [])  # the bad file is deleted
        rel, opener = release(sums=False)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(updates.UpdateError, "cannot be verified"):
                updates.download_package(rel, "rpm", Path(tmp), opener=opener, machine="x86_64")

    def test_install_runs_the_package_manager_through_pkexec(self):
        rel, opener = release()
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            self.assertTrue(Path(command[-1]).is_file())  # installed from the verified file
            return subprocess.CompletedProcess(command, 0, "", "")

        log = updates.apply_package_update(rel, "rpm", run=run, opener=opener, machine="x86_64")
        self.assertEqual(calls[0][:5], ["pkexec", "dnf", "install", "-y",
                                        calls[0][4]])
        self.assertTrue(calls[0][4].endswith("df2pq-0.3.0-1.x86_64.rpm"))
        self.assertFalse(Path(calls[0][4]).exists())  # the temporary download is removed
        self.assertIn("Installed df2pq-0.3.0-1.x86_64.rpm.", log)
        self.assertEqual(updates.install_command("deb", Path("/t/p.deb")),
                         ["pkexec", "apt-get", "install", "-y", "/t/p.deb"])

    def test_password_prompt_outcomes_are_explained(self):
        rel, opener = release()
        for code, message in ((126, "cancelled"), (127, "Not authorised"), (1, "reported an error")):
            run = lambda command, **kw: subprocess.CompletedProcess(command, code, "", "boom")  # noqa: E731,B023
            with self.subTest(code=code), self.assertRaisesRegex(updates.UpdateError, message):
                updates.apply_package_update(rel, "rpm", run=run, opener=opener, machine="x86_64")

    def test_detects_the_owning_package_manager(self):
        def run(command, **kwargs):
            owned = command[0] == "rpm"
            return subprocess.CompletedProcess(command, 0 if owned else 1, "df2pq" if owned else "")

        with patch("df2pq.updates.shutil.which", return_value="/usr/bin/x"):
            self.assertEqual(updates.system_package_kind("/opt/df2pq/df2pq", run=run), "rpm")
            self.assertIsNone(updates.system_package_kind(
                "/x", run=lambda c, **k: subprocess.CompletedProcess(c, 1, "")))

    def test_manual_command_and_restart(self):
        rel, _ = release()
        self.assertEqual(updates.manual_install_command("rpm", rel, "x86_64"),
                         "sudo dnf install ./df2pq-0.3.0-1.x86_64.rpm")
        self.assertEqual(updates.restart_command(), [sys.executable, "-m", "df2pq"])
        with patch.object(sys, "frozen", True, create=True):
            self.assertEqual(updates.restart_command(), [sys.executable])

    def test_frozen_library_path_is_not_passed_to_system_tools(self):
        with patch.dict("os.environ", {"LD_LIBRARY_PATH": "/opt/df2pq/_internal",
                                       "LD_LIBRARY_PATH_ORIG": "/usr/local/lib"}):
            self.assertEqual(updates.system_env()["LD_LIBRARY_PATH"], "/usr/local/lib")


if __name__ == "__main__":
    unittest.main()
