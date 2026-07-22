from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from core import runtime_deps


def _make_archive(directory: Path, member_name: str = "aria2c.exe", payload: bytes = b"MZfake") -> Path:
    archive = directory / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(f"aria2-1.37.0/{member_name}", payload)
    return archive


class DigestVerificationTests(unittest.TestCase):
    """The security requirement: never unpack an archive we cannot vouch for."""

    def setUp(self):
        self._workspace = tempfile.TemporaryDirectory(prefix="vdt_test_")
        self.workspace = Path(self._workspace.name)
        self.archive = _make_archive(self.workspace)
        self.actual_digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.addCleanup(self._workspace.cleanup)

    def _spec(self, sha256: str) -> runtime_deps.BinarySpec:
        return runtime_deps.BinarySpec(
            name="aria2c",
            url="https://example.invalid/aria2.zip",
            sha256=sha256,
            member_suffix="aria2c.exe",
            executable_name="aria2c.exe",
        )

    def test_manifest_digest_mismatch_is_rejected(self):
        spec = self._spec("0" * 64)
        with self.assertRaises(runtime_deps.DependencyError) as ctx:
            runtime_deps._verify_digest("aria2c", spec, self.archive, None)
        self.assertIn("khong khop", str(ctx.exception).lower())

    def test_manifest_digest_match_is_accepted(self):
        spec = self._spec(self.actual_digest)
        runtime_deps._verify_digest("aria2c", spec, self.archive, None)

    def test_manifest_digest_is_case_insensitive(self):
        spec = self._spec(self.actual_digest.upper())
        runtime_deps._verify_digest("aria2c", spec, self.archive, None)

    def test_unpinned_first_download_records_pin(self):
        spec = self._spec("")
        recorded: dict[str, str] = {}
        with patch.object(runtime_deps, "_load_pins", return_value={}), \
             patch.object(runtime_deps, "_save_pin", side_effect=lambda n, d: recorded.__setitem__(n, d)):
            runtime_deps._verify_digest("aria2c", spec, self.archive, None)
        self.assertEqual(recorded, {"aria2c": self.actual_digest})

    def test_pin_mismatch_on_later_download_is_rejected(self):
        """A changed archive under a stable URL is exactly what pinning catches."""
        spec = self._spec("")
        with patch.object(runtime_deps, "_load_pins", return_value={"aria2c": "1" * 64}):
            with self.assertRaises(runtime_deps.DependencyError) as ctx:
                runtime_deps._verify_digest("aria2c", spec, self.archive, None)
        self.assertIn("can thiep", str(ctx.exception).lower())

    def test_pin_match_on_later_download_is_accepted(self):
        spec = self._spec("")
        with patch.object(runtime_deps, "_load_pins", return_value={"aria2c": self.actual_digest}):
            runtime_deps._verify_digest("aria2c", spec, self.archive, None)


class DownloadGuardTests(unittest.TestCase):
    def test_plain_http_url_is_refused(self):
        with tempfile.TemporaryDirectory() as workspace:
            target = Path(workspace) / "out.zip"
            with self.assertRaises(runtime_deps.DependencyError):
                runtime_deps._download("http://example.invalid/a.zip", target, None)
            self.assertFalse(target.exists())

    def test_ensure_binary_reports_failure_without_raising(self):
        """Optional accelerators must degrade quietly, never abort a download."""
        spec = runtime_deps.BinarySpec(
            name="aria2c",
            url="https://example.invalid/aria2.zip",
            sha256="0" * 64,
            member_suffix="aria2c.exe",
            executable_name="aria2c.exe",
        )
        messages: list[tuple[str, str]] = []
        with patch.dict(runtime_deps.BINARY_MANIFEST, {"aria2c": spec}), \
             patch.object(runtime_deps, "resolve_binary", return_value=""), \
             patch.object(runtime_deps, "_download", side_effect=OSError("network down")):
            result = runtime_deps.ensure_binary("aria2c", lambda m, l="info": messages.append((l, m)))
        self.assertEqual(result, "")
        self.assertTrue(any(level == "warning" for level, _ in messages))

    def test_unknown_binary_name_returns_empty(self):
        with patch.object(runtime_deps, "resolve_binary", return_value=""):
            self.assertEqual(runtime_deps.ensure_binary("not-a-real-binary"), "")


class ExtractionTests(unittest.TestCase):
    def test_member_is_extracted_by_suffix(self):
        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            archive = _make_archive(root, payload=b"binary-content")
            spec = runtime_deps.BinarySpec(
                name="aria2c",
                url="https://example.invalid/a.zip",
                sha256="",
                member_suffix="aria2c.exe",
                executable_name="aria2c.exe",
            )
            installed = runtime_deps._extract_member(archive, spec, root / "bin")
            self.assertTrue(installed.is_file())
            self.assertEqual(installed.read_bytes(), b"binary-content")

    def test_missing_member_raises(self):
        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            archive = _make_archive(root, member_name="something-else.exe")
            spec = runtime_deps.BinarySpec(
                name="aria2c",
                url="https://example.invalid/a.zip",
                sha256="",
                member_suffix="aria2c.exe",
                executable_name="aria2c.exe",
            )
            with self.assertRaises(runtime_deps.DependencyError):
                runtime_deps._extract_member(archive, spec, root / "bin")


class VersionParsingTests(unittest.TestCase):
    """The same release is spelled differently depending on the source."""

    def test_zero_padding_does_not_change_the_version(self):
        # yt-dlp reports 2026.07.04; PyPI lists 2026.7.4. Comparing the strings
        # would report a phantom difference.
        self.assertEqual(
            runtime_deps._parse_version("2026.07.04"),
            runtime_deps._parse_version("2026.7.4"),
        )

    def test_ordering(self):
        newer = runtime_deps._parse_version("2026.8.1")
        older = runtime_deps._parse_version("2026.07.04")
        self.assertGreater(newer, older)

    def test_unparseable_version_is_handled(self):
        self.assertEqual(runtime_deps._parse_version("nightly"), ())
        self.assertEqual(runtime_deps._parse_version(""), ())


class UpdateAvailabilityTests(unittest.TestCase):
    """Age is not the same question as 'is there a newer release'.

    The previous rule flagged anything older than a fortnight as stale. yt-dlp
    had simply not published for 18 days, so the UI nagged about an update that
    did not exist and the button reported success while changing nothing.
    """

    def setUp(self):
        runtime_deps._yt_dlp_update_available.cache_clear()
        self.addCleanup(runtime_deps._yt_dlp_update_available.cache_clear)

    def _availability(self, installed: str, pypi: str | None) -> bool:
        with patch.object(runtime_deps, "_installed_yt_dlp_version", return_value=installed), \
             patch.object(runtime_deps, "_latest_pypi_version", return_value=pypi):
            return runtime_deps._yt_dlp_update_available()

    def test_current_release_reports_no_update(self):
        self.assertFalse(self._availability("2026.07.04", "2026.7.4"))

    def test_newer_release_reports_an_update(self):
        self.assertTrue(self._availability("2026.07.04", "2026.8.1"))

    def test_unreachable_pypi_stays_quiet(self):
        """Nagging about something unverifiable is worse than saying nothing."""
        self.assertFalse(self._availability("2026.07.04", None))

    def test_unknown_installed_version_stays_quiet(self):
        self.assertFalse(self._availability("unknown", "2026.8.1"))


class UpgradeOutcomeTests(unittest.TestCase):
    def setUp(self):
        runtime_deps._yt_dlp_update_available.cache_clear()
        self.addCleanup(runtime_deps._yt_dlp_update_available.cache_clear)

    def test_no_upgrade_attempted_when_already_current(self):
        with patch.object(runtime_deps, "_yt_dlp_freshness", return_value=("2026.7.4", False)), \
             patch.object(runtime_deps, "_run_pip") as fake_pip:
            self.assertFalse(runtime_deps.ensure_ytdlp_fresh())
        fake_pip.assert_not_called()

    def test_unchanged_version_is_not_reported_as_success(self):
        """pip exits 0 when already current, so the exit code cannot be trusted."""
        messages: list[tuple[str, str]] = []
        with patch.object(runtime_deps, "_yt_dlp_freshness", return_value=("2026.7.4", True)), \
             patch.object(runtime_deps, "_run_pip", return_value=True), \
             patch.object(runtime_deps, "_installed_yt_dlp_version", return_value="2026.7.4"):
            result = runtime_deps.ensure_ytdlp_fresh(lambda m, l="info": messages.append((l, m)))

        self.assertFalse(result, "an unchanged version is not an upgrade")
        self.assertTrue(any(level == "warning" for level, _ in messages))

    def test_real_upgrade_is_reported(self):
        messages: list[tuple[str, str]] = []
        with patch.object(runtime_deps, "_yt_dlp_freshness", return_value=("2026.7.4", True)), \
             patch.object(runtime_deps, "_run_pip", return_value=True), \
             patch.object(runtime_deps, "_installed_yt_dlp_version", return_value="2026.8.1"):
            result = runtime_deps.ensure_ytdlp_fresh(lambda m, l="info": messages.append((l, m)))

        self.assertTrue(result)
        self.assertTrue(any(level == "success" for level, _ in messages))

    def test_failed_pip_is_not_reported_as_success(self):
        with patch.object(runtime_deps, "_yt_dlp_freshness", return_value=("2026.7.4", True)), \
             patch.object(runtime_deps, "_run_pip", return_value=False):
            self.assertFalse(runtime_deps.ensure_ytdlp_fresh())


class PluginPackageTests(unittest.TestCase):
    def test_every_declared_plugin_resolves_on_pypi(self):
        """A fabricated distribution name errors on every setup run.

        This list previously held "yt-dlp-ChromeCookieUnlock", which PyPI answers
        with a 404.
        """
        import json as json_module
        import urllib.error
        import urllib.request

        for _import_name, distribution in runtime_deps.PLUGIN_PACKAGES:
            with self.subTest(distribution=distribution):
                try:
                    with urllib.request.urlopen(
                        f"https://pypi.org/pypi/{distribution}/json", timeout=10
                    ) as response:
                        json_module.load(response)
                except urllib.error.HTTPError as exc:
                    self.fail(f"{distribution} is not on PyPI (HTTP {exc.code})")
                except Exception:
                    self.skipTest("PyPI unreachable")


class FrozenBuildTests(unittest.TestCase):
    def test_pip_is_not_invoked_in_frozen_builds(self):
        """PyInstaller builds have no writable site-packages; pip would corrupt state."""
        messages: list[tuple[str, str]] = []
        with patch.object(runtime_deps, "is_frozen", return_value=True), \
             patch("subprocess.run") as fake_run:
            result = runtime_deps._run_pip(["anything"], lambda m, l="info": messages.append((l, m)))
        self.assertFalse(result)
        fake_run.assert_not_called()


class LockfileTests(unittest.TestCase):
    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as workspace:
            lockfile = Path(workspace) / "nested" / "binaries.lock.json"
            with patch.object(runtime_deps, "_lockfile_path", return_value=lockfile):
                runtime_deps._save_pin("aria2c", "abc123")
                self.assertEqual(runtime_deps._load_pins(), {"aria2c": "abc123"})

    def test_corrupt_lockfile_is_treated_as_empty(self):
        with tempfile.TemporaryDirectory() as workspace:
            lockfile = Path(workspace) / "binaries.lock.json"
            lockfile.write_text("{not json", encoding="utf-8")
            with patch.object(runtime_deps, "_lockfile_path", return_value=lockfile):
                self.assertEqual(runtime_deps._load_pins(), {})


class ReportTests(unittest.TestCase):
    def test_ready_requires_core_packages_and_ffmpeg(self):
        report = runtime_deps.DependencyReport(
            python_packages={"curl_cffi": True, "brotli": True, "requests": True},
            ffmpeg=True,
        )
        self.assertTrue(report.is_ready)

        report.ffmpeg = False
        self.assertFalse(report.is_ready)

    def test_missing_plugin_does_not_block_ready(self):
        report = runtime_deps.DependencyReport(
            python_packages={"curl_cffi": True, "brotli": True, "requests": True},
            plugins={"bgutil_ytdlp_pot_provider": False},
            ffmpeg=True,
        )
        self.assertTrue(report.is_ready)

    def test_to_dict_is_json_serialisable(self):
        payload = runtime_deps.detect().to_dict()
        json.dumps(payload)
        self.assertIn("pythonPackages", payload)
        self.assertIn("ready", payload)


if __name__ == "__main__":
    unittest.main()
