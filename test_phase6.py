from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import recorder
from core.config import DEFAULT_PLAYLIST_LIMIT, resolve_playlist_mode
from core.recorder import RecorderError


class PlaylistModeTests(unittest.TestCase):
    """Guards the worst footgun in the redesign.

    With a one-button UI there is no place for the user to intervene, so
    resolving a plain video URL to "download the whole playlist" would pull down
    hundreds of videos with no warning.
    """

    def test_watch_url_with_list_downloads_one_video(self):
        for url in (
            "https://www.youtube.com/watch?v=abc123&list=PL0000",
            "https://www.youtube.com/watch?list=PL0000&v=abc123",
            "https://m.youtube.com/watch?v=abc123&list=RDMM&index=4",
        ):
            with self.subTest(url=url):
                self.assertTrue(resolve_playlist_mode(url)["noplaylist"])

    def test_plain_video_url_downloads_one_video(self):
        for url in (
            "https://www.youtube.com/watch?v=abc123",
            "https://vimeo.com/12345",
            "https://example.com/video.mp4",
        ):
            with self.subTest(url=url):
                self.assertTrue(resolve_playlist_mode(url)["noplaylist"])

    def test_playlist_url_downloads_playlist(self):
        mode = resolve_playlist_mode("https://www.youtube.com/playlist?list=PL0000")
        self.assertFalse(mode["noplaylist"])
        self.assertEqual(mode["playlistend"], DEFAULT_PLAYLIST_LIMIT)

    def test_channel_forms_download_playlist(self):
        for url in (
            "https://www.youtube.com/@somehandle",
            "https://www.youtube.com/channel/UC0000",
            "https://www.youtube.com/c/SomeName",
            "https://www.youtube.com/user/SomeName",
        ):
            with self.subTest(url=url):
                self.assertFalse(resolve_playlist_mode(url)["noplaylist"])

    def test_bare_list_without_video_downloads_playlist(self):
        self.assertFalse(resolve_playlist_mode("https://www.youtube.com/?list=PL0000")["noplaylist"])

    def test_playlist_downloads_are_capped(self):
        """A channel can hold thousands of videos; an uncapped pull is unusable."""
        mode = resolve_playlist_mode("https://www.youtube.com/@somehandle")
        self.assertEqual(mode["playlistend"], DEFAULT_PLAYLIST_LIMIT)

    def test_malformed_input_is_safe(self):
        for url in ("", "   ", None, "not a url", "http://["):
            with self.subTest(url=url):
                self.assertTrue(resolve_playlist_mode(url)["noplaylist"])


class DownloaderPlaylistWiringTests(unittest.TestCase):
    def _captured_opts(self, url: str) -> dict:
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

        with patch.object(downloader, "YoutubeDL", FakeYoutubeDL):
            downloader.download_video(url, ".", dry_run=True)
        return captured

    def test_watch_url_keeps_noplaylist(self):
        self.assertTrue(self._captured_opts("https://www.youtube.com/watch?v=a&list=PL1")["noplaylist"])

    def test_playlist_url_enables_playlist_with_cap(self):
        opts = self._captured_opts("https://www.youtube.com/playlist?list=PL1")
        self.assertFalse(opts["noplaylist"])
        self.assertEqual(opts["playlistend"], DEFAULT_PLAYLIST_LIMIT)


class ExternalDownloaderTests(unittest.TestCase):
    def test_aria2c_used_when_available(self):
        from core import downloader

        opts: dict = {}
        with patch("core.runtime_deps.resolve_binary", return_value="C:/bin/aria2c.exe"):
            downloader._apply_external_downloader(opts, is_sharepoint=False)
        self.assertEqual(opts["external_downloader"]["default"], "C:/bin/aria2c.exe")
        self.assertIn("-x", opts["external_downloader_args"]["default"])

    def test_absent_aria2c_leaves_options_untouched(self):
        from core import downloader

        opts: dict = {}
        with patch("core.runtime_deps.resolve_binary", return_value=""):
            downloader._apply_external_downloader(opts, is_sharepoint=False)
        self.assertEqual(opts, {})

    def test_sharepoint_never_uses_aria2c(self):
        """SharePoint is deliberately throttled; parallel connections undo that."""
        from core import downloader

        opts: dict = {}
        with patch("core.runtime_deps.resolve_binary", return_value="C:/bin/aria2c.exe"):
            downloader._apply_external_downloader(opts, is_sharepoint=True)
        self.assertEqual(opts, {})


class WorkerPoolTests(unittest.TestCase):
    def _state(self):
        from web_ui.server import AppState

        return AppState(downloader=lambda *a, **k: None)

    def test_single_item_runs_sequentially(self):
        state = self._state()
        self.assertEqual(state._resolve_worker_count([(1, "https://a.com/v")]), 1)

    def test_multiple_hosts_run_in_parallel(self):
        from web_ui.server import MAX_DOWNLOAD_WORKERS

        state = self._state()
        items = [(1, "https://a.com/v"), (2, "https://b.com/v"), (3, "https://c.com/v")]
        self.assertEqual(state._resolve_worker_count(items), MAX_DOWNLOAD_WORKERS)

    def test_sharepoint_forces_sequential(self):
        state = self._state()
        items = [
            (1, "https://x-my.sharepoint.com/:v:/g/a"),
            (2, "https://other.com/v"),
        ]
        self.assertEqual(state._resolve_worker_count(items), 1)

    def test_every_item_is_downloaded_exactly_once(self):
        """A pool that drops or repeats work is worse than the sequential loop."""
        import threading

        from web_ui.server import AppState

        seen: list[str] = []
        lock = threading.Lock()

        state = AppState(downloader=lambda *a, **k: None)
        urls = [f"https://host{index}.com/v" for index in range(6)]
        state.add_urls(urls, "test")
        work = [(item.item_id, item.url) for item in state.queue_items]

        def record(_item_id, url, *_args):
            with lock:
                seen.append(url)

        with patch.object(state, "_download_worker", side_effect=record):
            state._batch_download_worker(work, ".", None, "quality", {})

        self.assertEqual(len(seen), 6, "every queued item must be downloaded")
        self.assertEqual(len(set(seen)), 6, "no item may be downloaded twice")


class RecorderTests(unittest.TestCase):
    def test_missing_ffmpeg_reports_clearly(self):
        with patch.object(recorder, "ffmpeg_available", return_value=False):
            with self.assertRaises(RecorderError) as ctx:
                recorder.record_stream("https://x/s.m3u8", "out.mp4")
        self.assertIn("ffmpeg", str(ctx.exception).lower())

    def test_unsupported_scheme_refused(self):
        with patch.object(recorder, "ffmpeg_available", return_value=True):
            for url in ("blob:https://x/abc", "file:///etc/passwd", "data:video/mp4;base64,AAA"):
                with self.subTest(url=url):
                    with self.assertRaises(RecorderError):
                        recorder.record_stream(url, "out.mp4")

    def test_headers_rendered_as_single_option(self):
        args = recorder._header_args({"Referer": "https://x/", "User-Agent": "UA"})
        self.assertEqual(args[0], "-headers")
        self.assertIn("Referer: https://x/", args[1])
        self.assertIn("User-Agent: UA", args[1])

    def test_no_headers_yields_no_option(self):
        self.assertEqual(recorder._header_args(None), [])
        self.assertEqual(recorder._header_args({}), [])

    def test_progress_parsing(self):
        self.assertAlmostEqual(recorder._parse_progress("frame= 10 time=00:01:30.50 bitrate=1x"), 90.5)
        self.assertIsNone(recorder._parse_progress("no timing here"))

    def test_clock_formatting(self):
        self.assertEqual(recorder._format_clock(3661), "01:01:01")

    def test_empty_output_is_an_error(self):
        """ffmpeg exits non-zero when interrupted, so judge by the file."""
        with tempfile.TemporaryDirectory() as workspace:
            target = Path(workspace) / "out.mp4"

            class FakeStderr:
                """Mimics Popen.stderr: iterable *and* closable."""

                def __init__(self):
                    self.closed = False

                def __iter__(self):
                    return iter(())

                def close(self):
                    self.closed = True

            class FakeProcess:
                def __init__(self):
                    self.stderr = FakeStderr()

                def wait(self, timeout=None):
                    return 0

                def poll(self):
                    return 0

                def kill(self):
                    pass

            with patch.object(subprocess, "Popen", return_value=FakeProcess()):
                with self.assertRaises(RecorderError):
                    recorder._run_recording(["ffmpeg"], target, None)

    def test_probe_duration_without_ffprobe_returns_none(self):
        with patch.object(recorder.shutil, "which", return_value=None):
            self.assertIsNone(recorder.probe_duration("https://x/s.m3u8"))


if __name__ == "__main__":
    unittest.main()
