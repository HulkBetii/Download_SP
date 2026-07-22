from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import media_sniffer
from web_ui.server import AppState


class PersistentProfileTests(unittest.TestCase):
    """A sniffer profile must survive between runs or logins are lost each time."""

    def test_profile_is_per_browser(self):
        with tempfile.TemporaryDirectory() as workspace:
            with patch("core.runtime_deps.browser_profile_dir", return_value=Path(workspace)):
                chrome = media_sniffer._persistent_profile_dir("chrome")
                coccoc = media_sniffer._persistent_profile_dir("coccoc")

        self.assertNotEqual(chrome, coccoc, "Chromium builds cannot share a profile")
        self.assertTrue(chrome.endswith("chrome"))
        self.assertTrue(coccoc.endswith("coccoc"))

    def test_profile_name_is_sanitised(self):
        """Browser name reaches here from client input, so it must not escape the dir."""
        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            with patch("core.runtime_deps.browser_profile_dir", return_value=root):
                result = Path(media_sniffer._persistent_profile_dir("../../evil"))

            self.assertEqual(result.parent, root)
            self.assertEqual(result.name, "evil")

    def test_empty_name_falls_back_to_chrome(self):
        with tempfile.TemporaryDirectory() as workspace:
            with patch("core.runtime_deps.browser_profile_dir", return_value=Path(workspace)):
                self.assertTrue(media_sniffer._persistent_profile_dir("!!!").endswith("chrome"))

    def test_unwritable_location_falls_back_to_empty(self):
        """Caller then uses a temp dir; a bad AppData path must not break sniffing."""
        with patch("core.runtime_deps.browser_profile_dir", side_effect=OSError("read-only")):
            self.assertEqual(media_sniffer._persistent_profile_dir("chrome"), "")

    def test_explicit_profile_path_still_wins(self):
        with tempfile.TemporaryDirectory() as workspace:
            self.assertEqual(media_sniffer._usable_profile_path(workspace), workspace)

    def test_placeholder_profile_names_are_ignored(self):
        for value in ("", "   ", "default", "Profile 1"):
            with self.subTest(value=value):
                self.assertEqual(media_sniffer._usable_profile_path(value), "")


class BrowserLaunchTests(unittest.TestCase):
    def _launch_args(self, mode: str = "visible") -> list[str]:
        captured: dict = {}

        class FakePopen:
            def __init__(self, args, **kwargs):
                captured["args"] = args

        with patch.object(media_sniffer.subprocess, "Popen", FakePopen):
            media_sniffer._launch_browser("chrome.exe", 9222, "C:/profile", browser_mode=mode)
        return captured["args"]

    def test_disable_features_is_a_single_flag(self):
        """Chrome honours only the last --disable-features; duplicates silently lose."""
        args = self._launch_args()
        feature_flags = [a for a in args if a.startswith("--disable-features=")]
        self.assertEqual(len(feature_flags), 1)
        for feature in media_sniffer.DISABLED_BROWSER_FEATURES:
            self.assertIn(feature, feature_flags[0])

    def test_cookie_database_lock_workaround_survives(self):
        args = self._launch_args()
        self.assertIn("LockProfileCookieDatabase", next(a for a in args if a.startswith("--disable-features=")))

    def test_disk_cache_is_capped(self):
        """The profile is persistent now, so an uncapped cache would grow forever."""
        args = self._launch_args()
        self.assertIn(f"--disk-cache-size={media_sniffer.BROWSER_DISK_CACHE_BYTES}", args)

    def test_headless_mode_adds_headless_flag(self):
        self.assertIn("--headless=new", self._launch_args(mode="headless"))

    def test_visible_mode_has_no_headless_flag(self):
        self.assertNotIn("--headless=new", self._launch_args(mode="visible"))


class CookieAutoPickTests(unittest.TestCase):
    def test_app_state_does_not_auto_pick_a_cookie_file(self):
        """Silently defaulting to a stray cookie file is a credential leak.

        The previous behaviour scanned the project directory for any *.json/*.txt
        whose name contained "cookie" or "sharepoint" and used it by default, so
        a session file dropped in the folder was sent to whatever host the user
        typed next.
        """
        state = AppState(downloader=lambda *a, **k: None)
        self.assertEqual(state.cookie_file, "")
        self.assertEqual(state.snapshot()["config"]["cookieFile"], "")

    def test_auto_pick_helper_is_gone(self):
        self.assertFalse(
            hasattr(AppState, "_find_default_cookie_file"),
            "the scanning helper should be removed, not just unused",
        )

    def test_explicit_cookie_file_is_still_honoured(self):
        state = AppState(downloader=lambda *a, **k: None)
        resolved = state._read_cookie_file_option({"cookieFile": "C:/tmp/my.txt"}, use_cookies=True)
        self.assertTrue(resolved.endswith("my.txt"))

    def test_cookie_file_ignored_when_cookies_disabled(self):
        state = AppState(downloader=lambda *a, **k: None)
        self.assertIsNone(state._read_cookie_file_option({"cookieFile": "C:/tmp/my.txt"}, use_cookies=False))


if __name__ == "__main__":
    unittest.main()
