from __future__ import annotations

import os
import unittest

from core.downloader import (
    _build_cookiesfrombrowser_spec,
    _build_retry_sleep_functions,
    _browser_cookie_database_error_message,
    _is_browser_cookie_database_error,
    _is_unsupported_url_error,
    _normalize_advanced_options,
    _unsupported_url_error_message,
    get_runtime_capabilities,
)


class DownloaderOptionTests(unittest.TestCase):
    def test_browser_cookie_spec_from_fields(self):
        options = _normalize_advanced_options(
            {
                "useBrowserCookies": True,
                "browserName": "Chrome",
                "browserProfile": "Default",
                "browserContainer": "none",
            }
        )
        self.assertEqual(options["browser_name"], "chrome")
        self.assertEqual(_build_cookiesfrombrowser_spec(options), ("chrome", "Default", None, "none"))

    def test_format_and_fragment_options_normalize(self):
        options = _normalize_advanced_options(
            {
                "formatSort": "res, quality ; ignored",
                "concurrentFragments": "12",
                "checkFormats": "true",
                "skipUnavailableFragments": 1,
                "downloadArchive": "%USERPROFILE%/archive.txt",
            }
        )
        self.assertTrue(options["check_formats"])
        self.assertEqual(options["concurrent_fragments"], 12)
        self.assertTrue(options["skip_unavailable_fragments"])
        self.assertEqual(options["format_sort"], ["res", "quality", "ignored"])
        self.assertTrue(os.path.isabs(options["download_archive"]) or "%USERPROFILE%" not in options["download_archive"])

    def test_runtime_capabilities_shape(self):
        capabilities = get_runtime_capabilities()
        self.assertIn("ytDlpVersion", capabilities)
        self.assertIn("ffmpegAvailable", capabilities)
        self.assertIn("curlCffiAvailable", capabilities)
        self.assertIn("supportedBrowsers", capabilities)

    def test_retry_sleep_functions_backoff_and_clamp(self):
        retry_sleep = _build_retry_sleep_functions()
        self.assertEqual(sorted(retry_sleep), ["extractor", "fragment", "http"])
        self.assertEqual(retry_sleep["http"](1), 1.0)
        self.assertEqual(retry_sleep["fragment"](2), 1.0)
        self.assertEqual(retry_sleep["http"](99), 20.0)

    def test_browser_cookie_database_error_is_detected(self):
        message = "ERROR: ERROR: Could not copy Chrome cookie database. See https://github.com/yt-dlp/yt-dlp/issues/7271"
        self.assertTrue(_is_browser_cookie_database_error(message))
        friendly = _browser_cookie_database_error_message(("chrome", "Default", None, None))
        self.assertIn("chrome:Default", friendly)
        self.assertIn("cookie database dang bi khoa", friendly)
        self.assertIn("mo UI bang trinh duyet khac", friendly)


    def test_unsupported_url_error_is_detected_and_explained(self):
        message = "ERROR: Unsupported URL: https://example.invalid/watch/1"
        self.assertTrue(_is_unsupported_url_error(message))
        friendly = _unsupported_url_error_message("https://example.invalid/watch/1")
        self.assertIn("khong duoc yt-dlp ho tro", friendly)
        self.assertIn("URL media truc tiep", friendly)

if __name__ == "__main__":
    unittest.main()
