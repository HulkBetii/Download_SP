from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from web_ui.server import AppState, create_server


class FakeDownloader:
    def __init__(self):
        self.calls: list[dict] = []
        self.failed_once: set[str] = set()

    def __call__(self, url, output_folder, cookie_file=None, status_callback=None, optimize_mode="balanced", advanced_options=None, **kwargs):
        self.calls.append(
            {
                "url": url,
                "output_folder": output_folder,
                "cookie_file": cookie_file,
                "optimize_mode": optimize_mode,
                "advanced_options": advanced_options or {},
            }
        )
        if status_callback:
            status_callback("Dang tai 25% | Speed: 1.0 MiB/s | ETA: 00:10", "blue")
        time.sleep(0.03)
        if "fail" in url and url not in self.failed_once:
            self.failed_once.add(url)
            raise RuntimeError("mock fail")
        if status_callback:
            status_callback("Hoan tat 100%", "green")


class WebUiServerTests(unittest.TestCase):
    def setUp(self):
        self.downloader = FakeDownloader()
        self.state = AppState(downloader=self.downloader)
        self.server = create_server("127.0.0.1", 0, self.state)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_queue_and_retry_workflow(self):
        result = self.post_json(
            "/api/queue/add",
            {
                "urls": [
                    "https://example.com/a.mp4",
                    "not-a-url",
                    "https://example.com/a.mp4",
                    "https://example.com/fail.mp4",
                ],
                "source": "clipboard",
            },
        )
        self.assertEqual(result["result"], {"added": 2, "duplicates": 1, "invalid": 1})

        state = self.get_state()
        self.assertEqual(len(state["items"]), 2)

        remove_result = self.post_json("/api/queue/remove", {"ids": [state["items"][0]["id"]]})
        self.assertEqual(remove_result["result"], {"removed": 1})

        state = self.get_state()
        self.assertEqual(len(state["items"]), 1)

        self.post_json(
            "/api/queue/add",
            {"urls": ["https://example.com/a.mp4"], "source": "manual"},
        )
        state = self.get_state()
        self.assertEqual(len(state["items"]), 2)

        self.post_json(
            "/api/download/start",
            {
                "outputFolder": str(Path(tempfile.gettempdir()) / "video_downloader_web_test"),
                "useCookies": False,
                "cookieFile": "",
                "optimizeMode": "speed",
            },
        )
        self.wait_for_batch_complete()

        state = self.get_state()
        self.assertEqual(state["batch"]["processed"], 2)
        self.assertEqual(state["batch"]["success"], 1)
        self.assertEqual(state["batch"]["failed"], 1)
        self.assertCountEqual([item["status"] for item in state["items"]], ["Hoàn tất", "Lỗi"])

        self.post_json(
            "/api/download/start",
            {
                "outputFolder": str(Path(tempfile.gettempdir()) / "video_downloader_web_test"),
                "useCookies": False,
                "cookieFile": "",
                "optimizeMode": "speed",
            },
        )
        self.wait_for_batch_complete()

        state = self.get_state()
        self.assertTrue(all(item["status"] == "Hoàn tất" for item in state["items"]))
        self.assertGreaterEqual(len(self.downloader.calls), 3)

        self.post_json("/api/log/clear", {})
        state = self.get_state()
        self.assertEqual(state["logs"], [])

    def test_server_lifecycle_on_ephemeral_port(self):
        self.assertTrue(self.server.server_address[1] > 0)
        state = self.get_state()
        self.assertIn("summary", state)

    def test_download_cookie_payload_defaults_and_explicit_empty(self):
        output_folder = str(Path(tempfile.gettempdir()) / "video_downloader_web_cookie_test")
        saved_cookie = str(Path(tempfile.gettempdir()) / "saved-cookies.txt")
        self.state.cookie_file = saved_cookie
        self.state.use_cookies = True

        self.post_json("/api/queue/add", {"url": "https://example.com/default-cookie.mp4"})
        self.post_json("/api/download/start", {"outputFolder": output_folder, "optimizeMode": "speed"})
        self.wait_for_batch_complete()
        self.assertEqual(self.downloader.calls[-1]["cookie_file"], saved_cookie)

        self.post_json("/api/queue/add", {"url": "https://example.com/empty-cookie.mp4"})
        self.post_json(
            "/api/download/start",
            {
                "outputFolder": output_folder,
                "useCookies": True,
                "cookieFile": "",
                "optimizeMode": "speed",
            },
        )
        self.wait_for_batch_complete()
        self.assertEqual(self.downloader.calls[-1]["cookie_file"], "")

    def test_dialog_endpoints_return_selected_paths(self):
        with patch("web_ui.server.choose_output_folder", return_value="C:/Downloads/Picked") as folder_dialog:
            result = self.post_json("/api/dialog/output-folder", {"initialPath": "C:/Downloads"})
        self.assertEqual(result["path"], "C:/Downloads/Picked")
        folder_dialog.assert_called_once_with("C:/Downloads")

        with patch("web_ui.server.choose_cookie_file", return_value="C:/cookies.txt") as cookie_dialog:
            result = self.post_json("/api/dialog/cookie-file", {"initialPath": "C:/old.txt"})
        self.assertEqual(result["path"], "C:/cookies.txt")
        cookie_dialog.assert_called_once_with("C:/old.txt")

    def test_analyze_endpoint_returns_probe_summary(self):
        with patch(
            "web_ui.server.analyze_video",
            return_value={
                "ok": True,
                "title": "Probe sample",
                "formatCount": 3,
                "formats": [{"format_id": "18"}],
            },
        ) as analyzer:
            result = self.post_json(
                "/api/analyze",
                {
                    "url": "https://example.com/probe.mp4",
                    "useCookies": False,
                    "cookieFile": "",
                    "optimizeMode": "quality",
                    "useBrowserCookies": True,
                    "browserName": "chrome",
                    "browserProfile": "Default",
                    "proxy": "http://127.0.0.1:8080",
                    "impersonate": "chrome:windows-10",
                    "downloadArchive": "archive.txt",
                    "checkFormats": True,
                    "formatSort": "res,quality,br",
                    "concurrentFragments": "8",
                    "skipUnavailableFragments": True,
                },
            )
        self.assertEqual(result["result"]["title"], "Probe sample")
        self.assertEqual(result["result"]["formatCount"], 3)
        analyzer.assert_called_once()

    def test_analyze_endpoint_returns_user_error_not_server_error(self):
        with patch(
            "web_ui.server.analyze_video",
            side_effect=RuntimeError("ERROR: ERROR: Could not copy Chrome cookie database. See https://github.com/yt-dlp/yt-dlp/issues/7271"),
        ):
            status, body = self.post_json_error(
                "/api/analyze",
                {
                    "url": "https://example.com/probe.mp4",
                    "useCookies": False,
                    "optimizeMode": "quality",
                    "useBrowserCookies": True,
                    "browserName": "chrome",
                },
            )
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])
        self.assertIn("Could not copy Chrome cookie database", body["error"])
        self.assertNotIn("Lỗi server", body["error"])

    def test_media_sniff_endpoint_adds_detected_media_urls(self):
        with patch(
            "web_ui.server.sniff_media_urls",
            return_value={
                "ok": True,
                "sourceUrl": "https://example.com/watch",
                "browserName": "coccoc",
                "browserPath": "C:/Program Files/CocCoc/Browser/Application/browser.exe",
                "elapsedSeconds": 2.5,
                "mediaUrls": [
                    {
                        "url": "https://cdn.example.com/master.m3u8",
                        "kind": "hls",
                        "mimeType": "application/vnd.apple.mpegurl",
                    }
                ],
            },
        ) as sniffer:
            result = self.post_json(
                "/api/media/sniff",
                {
                    "url": "https://example.com/watch",
                    "browserName": "coccoc",
                    "sniffTimeoutSeconds": 8,
                },
            )

        self.assertEqual(result["result"]["queue"]["added"], 1)
        self.assertEqual(result["state"]["items"][0]["url"], "https://cdn.example.com/master.m3u8")
        sniffer.assert_called_once()

    def test_media_inspect_endpoint_returns_ranked_candidates(self):
        with patch(
            "web_ui.server.inspect_media_candidates",
            return_value={
                "ok": True,
                "sourceUrl": "https://example.com/watch",
                "browserName": "coccoc",
                "browserPath": "C:/Program Files/CocCoc/Browser/Application/browser.exe",
                "elapsedSeconds": 2.5,
                "selectedCandidateId": "cand-hls",
                "mediaUrls": [
                    {
                        "id": "cand-hls",
                        "url": "https://cdn.example.com/master.m3u8",
                        "kind": "hls",
                        "host": "cdn.example.com",
                        "score": 120,
                        "reasons": ["HLS candidate"],
                        "requestHeaders": {"Referer": "https://example.com/watch"},
                    }
                ],
                "diagnostics": [],
            },
        ) as inspector:
            result = self.post_json(
                "/api/media/inspect",
                {
                    "url": "https://example.com/watch",
                    "browserName": "coccoc",
                    "browserMode": "visible",
                    "sniffTimeoutSeconds": 8,
                },
            )

        self.assertEqual(result["result"]["selectedCandidateId"], "cand-hls")
        self.assertEqual(result["state"]["lastCandidates"][0]["kind"], "hls")
        inspector.assert_called_once()

    def test_batch_download_falls_back_to_auto_for_unsupported_urls(self):
        class UnsupportedThenMediaDownloader:
            def __init__(self):
                self.calls: list[dict] = []

            def __call__(self, url, output_folder, cookie_file=None, status_callback=None, optimize_mode="balanced", advanced_options=None, **kwargs):
                self.calls.append(
                    {
                        "url": url,
                        "advanced_options": advanced_options or {},
                    }
                )
                if "9anime.or.at" in url:
                    raise RuntimeError(f"Nguon/link nay hien khong duoc yt-dlp ho tro: {url}.")
                if status_callback:
                    status_callback("Hoan tat 100%", "green")

        downloader = UnsupportedThenMediaDownloader()
        self.state.downloader = downloader

        self.post_json("/api/queue/add", {"url": "https://9anime.or.at/show-episode-1-english-subbed/"})
        with patch(
            "web_ui.server.sniff_media_urls",
            return_value={
                "ok": True,
                "sourceUrl": "https://9anime.or.at/show-episode-1-english-subbed/",
                "browserName": "coccoc",
                "browserPath": "C:/Program Files/CocCoc/Browser/Application/browser.exe",
                "elapsedSeconds": 1.0,
                "mediaUrls": [
                    {
                        "url": "https://cdn.example.com/show/master.m3u8",
                        "kind": "hls",
                        "mimeType": "application/vnd.apple.mpegurl",
                        "status": 200,
                        "requestHeaders": {"Referer": "https://9anime.or.at/show-episode-1-english-subbed/"},
                    }
                ],
                "diagnostics": [],
            },
        ) as sniffer:
            self.post_json(
                "/api/download/start",
                {
                    "outputFolder": str(Path(tempfile.gettempdir()) / "video_downloader_web_test"),
                    "useCookies": False,
                    "cookieFile": "",
                    "optimizeMode": "quality",
                    "browserName": "coccoc",
                },
            )
            self.wait_for_batch_complete()

        state = self.get_state()
        self.assertEqual(state["batch"]["success"], 1)
        self.assertEqual(state["items"][0]["status"], "Hoàn tất")
        self.assertEqual(downloader.calls[-1]["url"], "https://cdn.example.com/show/master.m3u8")
        self.assertEqual(downloader.calls[-1]["advanced_options"]["httpHeaders"]["Referer"], "https://9anime.or.at/show-episode-1-english-subbed/")
        sniffer.assert_called_once()

    def test_auto_download_endpoint_starts_job(self):
        def fake_auto(*args, **kwargs):
            event_callback = kwargs.get("event_callback")
            if event_callback:
                event_callback(
                    {
                        "stage": "probing",
                        "message": "dang probe",
                        "level": "info",
                        "progress": 10,
                        "diagnostics": [],
                    }
                )
            time.sleep(0.15)
            return {"ok": True, "strategy": "yt-dlp", "sourceUrl": args[0]}

        self.post_json("/api/queue/add", {"url": "https://example.com/watch"})
        with patch("web_ui.server.run_auto_download", side_effect=fake_auto) as runner:
            result = self.post_json(
                "/api/download/auto",
                {
                    "url": "https://example.com/watch",
                    "outputFolder": str(Path(tempfile.gettempdir()) / "video_downloader_web_test"),
                    "useCookies": False,
                    "cookieFile": "",
                    "optimizeMode": "quality",
                    "browserName": "coccoc",
                    "browserMode": "visible",
                    "autoSelect": True,
                },
            )

        self.assertEqual(result["started"], 1)
        self.assertTrue(result["state"]["autoRunning"])
        runner.assert_called_once()
        self.wait_for_batch_complete()
        state = self.get_state()
        self.assertFalse(state["autoRunning"])
        self.assertEqual(state["autoJob"]["stage"], "done")

    def post_json(self, path: str, payload: dict) -> dict:
        request = Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
        if not body.get("ok"):
            raise AssertionError(body)
        return body

    def post_json_error(self, path: str, payload: dict) -> tuple[int, dict]:
        request = Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=10) as response:
                body = json.loads(response.read().decode("utf-8"))
                return response.status, body
        except HTTPError as exc:
            body = json.loads(exc.read().decode("utf-8"))
            return exc.code, body

    def get_state(self) -> dict:
        with urlopen(f"{self.base_url}/api/state", timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
        self.assertTrue(body["ok"])
        return body["state"]

    def wait_for_batch_complete(self) -> None:
        deadline = time.time() + 10
        while time.time() < deadline:
            state = self.get_state()
            if not state["running"]:
                return
            time.sleep(0.05)
        self.fail("Batch did not finish in time")


if __name__ == "__main__":
    unittest.main()
