from __future__ import annotations

import tempfile
import unittest

from core.auto_pipeline import inspect_media, is_unsupported_error, run_auto_download


class AutoPipelineTests(unittest.TestCase):
    def test_unsupported_error_detection_accepts_normalized_downloader_message(self):
        message = (
            "Nguon/link nay hien khong duoc yt-dlp ho tro: "
            "https://9anime.or.at/show-episode-1-english-subbed/."
        )

        self.assertTrue(is_unsupported_error(message))

    def test_inspect_media_selects_best_candidate(self):
        def sniffer(url, **kwargs):
            return {
                "ok": True,
                "sourceUrl": url,
                "browserName": kwargs.get("browserName", "coccoc"),
                "browserPath": "C:/Program Files/CocCoc/Browser/Application/browser.exe",
                "elapsedSeconds": 1.2,
                "mediaUrls": [
                    {
                        "id": "video",
                        "url": "https://cdn.example.test/video.mp4",
                        "kind": "video",
                        "mimeType": "video/mp4",
                        "status": 200,
                        "requestHeaders": {"Referer": url, "User-Agent": "UA"},
                        "responseHeaders": {"Content-Length": "1048576"},
                    },
                    {
                        "id": "hls",
                        "url": "https://cdn.example.test/master.m3u8",
                        "kind": "hls",
                        "mimeType": "application/vnd.apple.mpegurl",
                        "status": 200,
                        "requestHeaders": {"Referer": url, "User-Agent": "UA"},
                    },
                ],
                "diagnostics": [],
            }

        result = inspect_media("https://example.test/watch", sniffer=sniffer)
        self.assertEqual(result["mediaUrls"][0]["kind"], "hls")
        self.assertEqual(result["selectedCandidateId"], result["mediaUrls"][0]["id"])

    def test_auto_download_falls_back_to_sniff_and_preserves_headers(self):
        events: list[dict] = []
        downloads: list[dict] = []

        def analyzer(*args, **kwargs):
            raise RuntimeError("Unsupported URL: https://example.test/watch")

        def sniffer(url, **kwargs):
            self.assertEqual(url, "https://example.test/watch")
            self.assertEqual(kwargs.get("browser_name"), "coccoc")
            self.assertTrue(kwargs.get("include_sensitive_headers"))
            return {
                "ok": True,
                "sourceUrl": url,
                "browserName": "coccoc",
                "browserPath": "C:/Program Files/CocCoc/Browser/Application/browser.exe",
                "elapsedSeconds": 1.5,
                "selectedCandidateId": "cand-hls",
                "mediaUrls": [
                    {
                        "id": "cand-hls",
                        "url": "https://cdn.example.test/master.m3u8",
                        "kind": "hls",
                        "mimeType": "application/vnd.apple.mpegurl",
                        "status": 200,
                        "requestHeaders": {
                            "Referer": url,
                            "User-Agent": "UA",
                            "Cookie": "session=abc",
                        },
                        "responseHeaders": {"Content-Length": "2048"},
                    }
                ],
                "diagnostics": [],
            }

        def downloader(url, output_folder, cookie_file=None, status_callback=None, optimize_mode="balanced", advanced_options=None, **kwargs):
            downloads.append(
                {
                    "url": url,
                    "output_folder": output_folder,
                    "cookie_file": cookie_file,
                    "optimize_mode": optimize_mode,
                    "advanced_options": advanced_options or {},
                }
            )
            if status_callback:
                status_callback("Hoan tat 100%", "green")

        result = run_auto_download(
            "https://example.test/watch",
            tempfile.gettempdir(),
            cookie_file=None,
            optimize_mode="quality",
            browser_name="coccoc",
            browser_mode="visible",
            event_callback=events.append,
            analyzer=analyzer,
            downloader=downloader,
            sniffer=sniffer,
        )

        self.assertEqual(result["strategy"], "browser-sniff")
        self.assertEqual(downloads[0]["url"], "https://cdn.example.test/master.m3u8")
        self.assertEqual(downloads[0]["advanced_options"]["httpHeaders"]["Referer"], "https://example.test/watch")
        self.assertEqual(downloads[0]["advanced_options"]["httpHeaders"]["User-Agent"], "UA")
        self.assertTrue(any(event["stage"] == "sniffing" for event in events))
        self.assertTrue(any(event["stage"] == "done" for event in events))


    def test_license_diagnostic_without_candidates_is_not_confirmed_drm(self):
        def analyzer(*args, **kwargs):
            raise RuntimeError("Unsupported URL: https://example.test/watch")

        def sniffer(url, **kwargs):
            return {
                "ok": True,
                "sourceUrl": url,
                "browserName": "coccoc",
                "browserPath": "C:/Program Files/CocCoc/Browser/Application/browser.exe",
                "elapsedSeconds": 1.5,
                "mediaUrls": [],
                "diagnostics": [
                    {
                        "type": "drm_license",
                        "message": "Phat hien request license/DRM.",
                        "url": "https://example.test/license",
                    }
                ],
            }

        # Failing to select a candidate now escalates to the MSE and recorder
        # rungs before giving up. Those launch a browser and shell out to
        # ffprobe, so they are stubbed out here to keep this a unit test - the
        # escalation itself is covered in test_strategy.
        from unittest.mock import patch

        from core import strategy

        with patch.object(strategy, "_try_mse", return_value=None), \
             patch.object(strategy, "_try_record", return_value=None):
            with self.assertRaises(Exception) as context:
                run_auto_download(
                    "https://example.test/watch",
                    tempfile.gettempdir(),
                    event_callback=lambda event: None,
                    analyzer=analyzer,
                    sniffer=sniffer,
                )

        message = str(context.exception)
        self.assertIn("Chua bat duoc media candidate", message)
        self.assertNotIn("Tool khong bypass DRM", message)

if __name__ == "__main__":
    unittest.main()
