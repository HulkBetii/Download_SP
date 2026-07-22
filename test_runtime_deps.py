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


class FreshnessTests(unittest.TestCase):
    def test_recent_version_is_not_stale(self):
        from datetime import date, timedelta

        recent = date.today() - timedelta(days=2)
        stamp = f"{recent.year}.{recent.month:02d}.{recent.day:02d}"
        with patch.object(runtime_deps, "_yt_dlp_freshness", wraps=runtime_deps._yt_dlp_freshness):
            version, stale = self._freshness_for(stamp)
        self.assertEqual(version, stamp)
        self.assertFalse(stale)

    def test_old_version_is_stale(self):
        from datetime import date, timedelta

        old = date.today() - timedelta(days=runtime_deps.YT_DLP_MAX_AGE_DAYS + 10)
        stamp = f"{old.year}.{old.month:02d}.{old.day:02d}"
        _version, stale = self._freshness_for(stamp)
        self.assertTrue(stale)

    def test_unparseable_version_is_not_reported_stale(self):
        """A surprising version string must not trigger an endless upgrade loop."""
        _version, stale = self._freshness_for("nightly")
        self.assertFalse(stale)

    def _freshness_for(self, version: str) -> tuple[str, bool]:
        import types

        fake_module = types.SimpleNamespace(__version__=version)
        with patch.dict("sys.modules", {"yt_dlp.version": fake_module}):
            return runtime_deps._yt_dlp_freshness()


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
