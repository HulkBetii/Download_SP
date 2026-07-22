from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import plugins


class JsRuntimeTests(unittest.TestCase):
    """yt-dlp only enables deno by default; a node-only machine looks empty to it."""

    def test_detects_runtimes_in_preference_order(self):
        present = {"node", "deno"}
        with patch.object(plugins.shutil, "which", side_effect=lambda name: name if name in present else None):
            self.assertEqual(plugins.detect_js_runtimes(), ["deno", "node"])

    def test_detects_node_only_machine(self):
        with patch.object(plugins.shutil, "which", side_effect=lambda name: name if name == "node" else None):
            self.assertEqual(plugins.detect_js_runtimes(), ["node"])

    def test_resolve_returns_ytdlp_parameter_shape(self):
        with patch.object(plugins, "detect_js_runtimes", return_value=["node"]):
            self.assertEqual(plugins.resolve_js_runtimes(), {"node": {}})

    def test_resolve_is_empty_when_nothing_installed(self):
        """Empty means 'leave yt-dlp's default alone', not 'disable runtimes'."""
        with patch.object(plugins, "detect_js_runtimes", return_value=[]):
            self.assertEqual(plugins.resolve_js_runtimes(), {})


class PotProviderDetectionTests(unittest.TestCase):
    def test_checks_namespace_submodule_not_distribution_name(self):
        """The distribution installs into yt_dlp_plugins.*; the top-level name never exists."""
        import importlib.util

        checked: list[str] = []

        def fake_find_spec(name):
            checked.append(name)
            return None

        with patch.object(importlib.util, "find_spec", side_effect=fake_find_spec):
            plugins.pot_provider_installed()

        self.assertTrue(checked)
        self.assertTrue(
            all(name.startswith("yt_dlp_plugins.") for name in checked),
            f"expected a namespace submodule, checked {checked}",
        )

    def test_missing_provider_reports_false(self):
        import importlib.util

        with patch.object(importlib.util, "find_spec", return_value=None):
            self.assertFalse(plugins.pot_provider_installed())

    def test_broken_install_does_not_raise(self):
        import importlib.util

        with patch.object(importlib.util, "find_spec", side_effect=ValueError("half installed")):
            self.assertFalse(plugins.pot_provider_installed())


class ExtractorArgsTests(unittest.TestCase):
    def test_no_args_by_default(self):
        self.assertEqual(plugins.youtube_extractor_args(), {})

    def test_force_ump_targets_youtube_formats(self):
        args = plugins.youtube_extractor_args(force_ump=True)
        self.assertEqual(args["youtube"]["formats"], ["ump"])

    def test_pot_base_url_uses_provider_namespace(self):
        args = plugins.youtube_extractor_args(pot_base_url=plugins.POT_PROVIDER_BASE_URL)
        self.assertEqual(args["youtubepot-bgutilhttp"]["base_url"], [plugins.POT_PROVIDER_BASE_URL])

    def test_both_escalations_can_combine(self):
        args = plugins.youtube_extractor_args(force_ump=True, pot_base_url="http://127.0.0.1:9999")
        self.assertIn("youtube", args)
        self.assertIn("youtubepot-bgutilhttp", args)


class PluginDirTests(unittest.TestCase):
    def test_missing_directory_registers_nothing(self):
        with patch.object(plugins, "bundled_plugin_dir", return_value=Path("does-not-exist-xyz")):
            self.assertEqual(plugins.register_plugin_dirs(), [])

    def test_existing_directory_is_appended_once(self):
        with tempfile.TemporaryDirectory() as workspace:
            directory = Path(workspace)

            class FakeIndirect:
                value = ["default"]

            fake_module = type(sys)("yt_dlp.plugins")
            fake_module.plugin_dirs = FakeIndirect()

            with patch.object(plugins, "bundled_plugin_dir", return_value=directory), \
                 patch.dict("sys.modules", {"yt_dlp.plugins": fake_module}):
                first = plugins.register_plugin_dirs()
                second = plugins.register_plugin_dirs()

            self.assertEqual(first, ["default", str(directory)])
            self.assertEqual(second, first, "directory should not be added twice")

    def test_bundled_dir_follows_meipass_when_frozen(self):
        with tempfile.TemporaryDirectory() as workspace:
            with patch.object(sys, "_MEIPASS", workspace, create=True):
                self.assertEqual(
                    plugins.bundled_plugin_dir(),
                    Path(workspace) / plugins.BUNDLED_PLUGIN_DIRNAME,
                )


class DownloaderIntegrationTests(unittest.TestCase):
    def test_downloader_sets_js_runtimes_when_available(self):
        """Regression guard: without this, YouTube silently loses formats."""
        from core import downloader

        captured: dict = {}

        class FakeYoutubeDL:
            def __init__(self, opts):
                captured.update(opts)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download=False):
                return {"formats": [], "title": "x", "_type": "video"}

        with patch.object(downloader, "YoutubeDL", FakeYoutubeDL), \
             patch.object(downloader, "resolve_js_runtimes", return_value={"node": {}}):
            downloader.download_video("https://example.com/v.mp4", ".", dry_run=True)

        self.assertEqual(captured.get("js_runtimes"), {"node": {}})

    def test_downloader_omits_js_runtimes_when_none_found(self):
        from core import downloader

        captured: dict = {}

        class FakeYoutubeDL:
            def __init__(self, opts):
                captured.update(opts)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download=False):
                return {"formats": [], "title": "x", "_type": "video"}

        with patch.object(downloader, "YoutubeDL", FakeYoutubeDL), \
             patch.object(downloader, "resolve_js_runtimes", return_value={}):
            downloader.download_video("https://example.com/v.mp4", ".", dry_run=True)

        self.assertNotIn("js_runtimes", captured)


if __name__ == "__main__":
    unittest.main()
