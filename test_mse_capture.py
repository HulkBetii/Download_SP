from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import mse_capture
from core.mse_capture import MseCaptureError, MseSegment, MseStream


def record(streams, seen, **fields):
    mse_capture._record_binding_payload(json.dumps(fields), streams, seen)


class RecordPayloadTests(unittest.TestCase):
    def setUp(self):
        self.streams: dict[str, MseStream] = {}
        self.seen: set[int] = set()

    def test_sourcebuffer_registers_mime(self):
        record(self.streams, self.seen, type="sourcebuffer", id="sb1", mime="video/mp4")
        self.assertEqual(self.streams["sb1"].mime_type, "video/mp4")
        self.assertEqual(self.streams["sb1"].segments, [])

    def test_append_collects_segment(self):
        record(self.streams, self.seen, type="sourcebuffer", id="sb1", mime="video/mp4")
        record(self.streams, self.seen, type="append", id="sb1", order=1, url="https://x/1.m4s", byteLength=99)
        segments = self.streams["sb1"].segments
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].url, "https://x/1.m4s")
        self.assertEqual(segments[0].byte_length, 99)

    def test_append_without_prior_sourcebuffer_still_works(self):
        """Injection can miss addSourceBuffer if the player created it very early."""
        record(self.streams, self.seen, type="append", id="sb9", order=1, url="https://x/1.m4s", mime="audio/mp4")
        self.assertIn("sb9", self.streams)
        self.assertEqual(self.streams["sb9"].mime_type, "audio/mp4")

    def test_segment_without_url_is_counted_not_dropped(self):
        """In-page generated segments cannot be fetched; the gap must be visible."""
        record(self.streams, self.seen, type="append", id="sb1", order=1, url="", byteLength=500)
        self.assertEqual(self.streams["sb1"].missing_url_count, 1)
        self.assertEqual(self.streams["sb1"].segments, [])

    def test_repeated_order_is_ignored(self):
        record(self.streams, self.seen, type="append", id="sb1", order=1, url="https://x/1.m4s")
        record(self.streams, self.seen, type="append", id="sb1", order=1, url="https://x/other.m4s")
        self.assertEqual(len(self.streams["sb1"].segments), 1)

    def test_malformed_payloads_are_ignored(self):
        for payload in ("not json", "", None, "[1,2,3]", '"a string"'):
            with self.subTest(payload=payload):
                mse_capture._record_binding_payload(payload, self.streams, self.seen)
        self.assertEqual(self.streams, {})


class FinaliseTests(unittest.TestCase):
    def test_segments_sorted_by_append_order(self):
        stream = MseStream("sb1", "video/mp4", segments=[
            MseSegment(3, "https://x/c.m4s", 1),
            MseSegment(1, "https://x/a.m4s", 1),
            MseSegment(2, "https://x/b.m4s", 1),
        ])
        result = mse_capture._finalise_streams({"sb1": stream})
        self.assertEqual([s.url for s in result[0].segments], ["https://x/a.m4s", "https://x/b.m4s", "https://x/c.m4s"])

    def test_duplicate_urls_removed(self):
        """A seek replays segments already appended; keeping them corrupts output."""
        stream = MseStream("sb1", "video/mp4", segments=[
            MseSegment(1, "https://x/a.m4s", 1),
            MseSegment(2, "https://x/b.m4s", 1),
            MseSegment(3, "https://x/a.m4s", 1),
        ])
        result = mse_capture._finalise_streams({"sb1": stream})
        self.assertEqual(len(result[0].segments), 2)

    def test_video_stream_sorts_before_audio(self):
        streams = {
            "sb2": MseStream("sb2", "audio/mp4", segments=[MseSegment(1, "https://x/a", 1)]),
            "sb1": MseStream("sb1", "video/mp4", segments=[MseSegment(2, "https://x/v", 1)]),
        }
        self.assertEqual([s.kind for s in mse_capture._finalise_streams(streams)], ["video", "audio"])

    def test_empty_streams_are_dropped(self):
        streams = {"sb1": MseStream("sb1", "video/mp4")}
        self.assertEqual(mse_capture._finalise_streams(streams), [])

    def test_stream_with_only_missing_urls_is_kept_for_reporting(self):
        streams = {"sb1": MseStream("sb1", "video/mp4", missing_url_count=5)}
        self.assertEqual(len(mse_capture._finalise_streams(streams)), 1)


class KindTests(unittest.TestCase):
    def test_kind_from_mime(self):
        cases = [
            ('video/mp4; codecs="avc1.4d401f"', "video"),
            ('audio/mp4; codecs="mp4a.40.2"', "audio"),
            ("", "unknown"),
            ("application/octet-stream", "unknown"),
        ]
        for mime, expected in cases:
            with self.subTest(mime=mime):
                self.assertEqual(MseStream("sb", mime).kind, expected)


class DiagnosticsTests(unittest.TestCase):
    def test_no_streams_explains_why(self):
        kinds = [d["type"] for d in mse_capture._build_diagnostics([])]
        self.assertIn("mse_no_streams", kinds)

    def test_generated_segments_reported(self):
        stream = MseStream("sb1", "video/mp4", missing_url_count=10)
        kinds = [d["type"] for d in mse_capture._build_diagnostics([stream])]
        self.assertIn("mse_generated_segments", kinds)

    def test_partial_capture_reported(self):
        stream = MseStream("sb1", "video/mp4", segments=[MseSegment(1, "https://x/a", 1)], missing_url_count=3)
        kinds = [d["type"] for d in mse_capture._build_diagnostics([stream])]
        self.assertIn("mse_partial", kinds)

    def test_clean_capture_has_no_diagnostics(self):
        stream = MseStream("sb1", "video/mp4", segments=[MseSegment(1, "https://x/a", 1)])
        self.assertEqual(mse_capture._build_diagnostics([stream]), [])


class DownloadTests(unittest.TestCase):
    def test_refuses_when_nothing_has_a_url(self):
        stream = MseStream("sb1", "video/mp4", missing_url_count=4)
        with tempfile.TemporaryDirectory() as workspace:
            with self.assertRaises(MseCaptureError):
                mse_capture.download_mse_streams([stream], workspace)

    def test_segments_concatenated_in_order(self):
        stream = MseStream("sb1", "video/mp4", segments=[
            MseSegment(1, "https://x/a", 3),
            MseSegment(2, "https://x/b", 3),
            MseSegment(3, "https://x/c", 3),
        ])
        payloads = {"https://x/a": b"AAA", "https://x/b": b"BBB", "https://x/c": b"CCC"}

        with tempfile.TemporaryDirectory() as workspace:
            target = Path(workspace) / "out.bin"
            with patch.object(mse_capture, "_fetch_segment", side_effect=lambda u, h: payloads.get(u)):
                written = mse_capture._download_stream(stream, target, {}, None)

            self.assertEqual(written, 9)
            self.assertEqual(target.read_bytes(), b"AAABBBCCC")

    def test_failed_segment_is_reported(self):
        stream = MseStream("sb1", "video/mp4", segments=[
            MseSegment(1, "https://x/a", 3),
            MseSegment(2, "https://x/bad", 3),
        ])
        messages: list[tuple[str, str]] = []

        with tempfile.TemporaryDirectory() as workspace:
            target = Path(workspace) / "out.bin"
            with patch.object(
                mse_capture,
                "_fetch_segment",
                side_effect=lambda u, h: None if "bad" in u else b"AAA",
            ):
                mse_capture._download_stream(stream, target, {}, lambda m, c="info": messages.append((c, m)))

        self.assertTrue(any(colour == "orange" for colour, _ in messages), "a gap must be surfaced")

    def test_mux_without_ffmpeg_keeps_first_stream(self):
        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            first = root / "a.bin"
            first.write_bytes(b"VIDEO")
            output = root / "out.mp4"

            messages: list[tuple[str, str]] = []
            with patch.object(mse_capture.shutil, "which", return_value=None):
                mse_capture._mux([first], output, lambda m, c="info": messages.append((c, m)))

            self.assertEqual(output.read_bytes(), b"VIDEO")
            self.assertTrue(any(colour == "orange" for colour, _ in messages))

    def test_mux_failure_raises(self):
        class Result:
            returncode = 1
            stderr = "Invalid data found"
            stdout = ""

        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            part = root / "a.bin"
            part.write_bytes(b"x")
            with patch.object(mse_capture.shutil, "which", return_value="ffmpeg"), \
                 patch.object(mse_capture.subprocess, "run", return_value=Result()):
                with self.assertRaises(MseCaptureError):
                    mse_capture._mux([part], root / "out.mp4", None)


class FilenameTests(unittest.TestCase):
    def test_unsafe_characters_stripped(self):
        self.assertNotIn("/", mse_capture._safe_filename("a/b:c*d"))

    def test_empty_title_gets_default(self):
        self.assertEqual(mse_capture._safe_filename("   "), "mse_capture")

    def test_existing_file_gets_suffix(self):
        with tempfile.TemporaryDirectory() as workspace:
            path = Path(workspace) / "v.mp4"
            path.write_bytes(b"x")
            self.assertEqual(mse_capture._unique_path(path).name, "v (1).mp4")


class SegmentCollectorTests(unittest.TestCase):
    """Segment URLs come off the wire because the page hook cannot see workers."""

    def _finish(self, collector, url, size, mime="video/mp4", request_id="1", session=""):
        collector.handle({
            "method": "Network.responseReceived",
            "sessionId": session,
            "params": {"requestId": request_id, "response": {"url": url, "mimeType": mime}},
        })
        collector.handle({
            "method": "Network.loadingFinished",
            "sessionId": session,
            "params": {"requestId": request_id, "encodedDataLength": size},
        })

    def test_segment_extensions_the_candidate_sniffer_ignores_are_collected(self):
        collector = mse_capture._SegmentCollector()
        for index, url in enumerate([
            "https://x/v/seg-1.m4s",
            "https://x/v/seg-2.ts",
            "https://x/v/chunk.cmfv",
        ]):
            self._finish(collector, url, 500_000, request_id=str(index))
        self.assertEqual(len(collector.segments), 3)

    def test_manifests_are_not_segments(self):
        collector = mse_capture._SegmentCollector()
        self._finish(collector, "https://x/master.m3u8", 500_000, mime="application/vnd.apple.mpegurl")
        self._finish(collector, "https://x/manifest.mpd", 500_000, mime="application/dash+xml", request_id="2")
        self.assertEqual(collector.segments, [])

    def test_tiny_responses_are_ignored(self):
        """Keys, playlists and tracking pixels are not media."""
        collector = mse_capture._SegmentCollector()
        self._finish(collector, "https://x/v/key.bin", 16)
        self.assertEqual(collector.segments, [])

    def test_duplicate_urls_recorded_once(self):
        collector = mse_capture._SegmentCollector()
        self._finish(collector, "https://x/v/seg-1.m4s", 500_000, request_id="1")
        self._finish(collector, "https://x/v/seg-1.m4s", 500_000, request_id="2")
        self.assertEqual(len(collector.segments), 1)

    def test_worker_and_page_requests_do_not_collide(self):
        """Request ids are only unique per session, so the key must include it."""
        collector = mse_capture._SegmentCollector()
        self._finish(collector, "https://x/v/a.m4s", 500_000, request_id="1", session="")
        self._finish(collector, "https://x/v/b.m4s", 500_000, request_id="1", session="worker1")
        self.assertEqual(len(collector.segments), 2)


class RenditionGroupingTests(unittest.TestCase):
    def test_grouped_by_directory_prefix(self):
        groups = mse_capture.group_segments_by_rendition([
            ("https://x/url_8/a.ts", 10),
            ("https://x/url_8/b.ts", 10),
            ("https://x/url_0/a.ts", 5),
        ])
        self.assertEqual(len(groups), 2)
        self.assertEqual(len(groups["https://x/url_8"]), 2)

    def test_query_string_does_not_split_a_group(self):
        groups = mse_capture.group_segments_by_rendition([
            ("https://x/v/a.m4s?t=1", 10),
            ("https://x/v/b.m4s?t=2", 10),
        ])
        self.assertEqual(len(groups), 1)

    def test_transport_stream_is_detected_as_muxed(self):
        ranked = [("https://x/url_8", [("https://x/url_8/a.ts", 10)])]
        self.assertTrue(mse_capture._groups_are_muxed(ranked))

    def test_fragmented_mp4_is_not_muxed(self):
        ranked = [("https://x/v", [("https://x/v/a.m4s", 10)])]
        self.assertFalse(mse_capture._groups_are_muxed(ranked))


class MergeNetworkSegmentsTests(unittest.TestCase):
    def _collector(self, pairs):
        collector = mse_capture._SegmentCollector()
        collector.segments = list(pairs)
        return collector

    def test_muxed_ts_yields_one_stream_not_two_bitrates(self):
        """Two TS renditions are the same content; muxing both duplicates video."""
        streams = {
            "sb1": MseStream("sb1", "video/mp4;codecs=avc1"),
            "sb2": MseStream("sb2", "audio/mp4;codecs=mp4a"),
        }
        collector = self._collector([
            ("https://x/url_8/fhd.ts", 13_000_000),
            ("https://x/url_0/hd.ts", 1_900_000),
        ])
        result = mse_capture._merge_network_segments(streams, collector)
        self.assertEqual(len(result), 1)
        self.assertIn("url_8", result[0].segments[0].url, "should keep the highest bitrate")

    def test_fragmented_mp4_yields_one_stream_per_codec(self):
        streams = {
            "sb1": MseStream("sb1", "video/mp4;codecs=avc1"),
            "sb2": MseStream("sb2", "audio/mp4;codecs=mp4a"),
        }
        collector = self._collector([
            ("https://x/video/1.m4s", 9_000_000),
            ("https://x/video/2.m4s", 9_000_000),
            ("https://x/audio/1.m4s", 500_000),
        ])
        result = mse_capture._merge_network_segments(streams, collector)
        self.assertEqual(len(result), 2)
        self.assertEqual(len(result[0].segments), 2)

    def test_page_hook_results_win_when_present(self):
        """If the page hook resolved URLs itself, its ordering is authoritative."""
        streams = {"sb1": MseStream("sb1", "video/mp4", segments=[MseSegment(1, "https://x/hook.m4s", 1)])}
        collector = self._collector([("https://x/net/other.m4s", 500_000)])
        result = mse_capture._merge_network_segments(streams, collector)
        self.assertEqual(result[0].segments[0].url, "https://x/hook.m4s")

    def test_no_network_segments_returns_mse_streams_unchanged(self):
        streams = {"sb1": MseStream("sb1", "video/mp4", missing_url_count=3)}
        result = mse_capture._merge_network_segments(streams, self._collector([]))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].missing_url_count, 3)


class InjectedScriptTests(unittest.TestCase):
    """The script runs before page code; a syntax slip means capturing nothing."""

    def test_script_patches_all_required_apis(self):
        source = mse_capture.MSE_CAPTURE_SCRIPT
        for needle in ("window.fetch", "XMLHttpRequest", "addSourceBuffer", "appendBuffer"):
            self.assertIn(needle, source)

    def test_script_reports_through_the_binding(self):
        self.assertIn(mse_capture.BINDING_NAME, mse_capture.MSE_CAPTURE_SCRIPT)

    def test_script_is_reentrancy_guarded(self):
        self.assertIn("__vdtMseInstalled", mse_capture.MSE_CAPTURE_SCRIPT)

    def test_script_has_balanced_brackets(self):
        source = mse_capture.MSE_CAPTURE_SCRIPT
        for opener, closer in (("(", ")"), ("{", "}"), ("[", "]")):
            self.assertEqual(source.count(opener), source.count(closer), f"unbalanced {opener}{closer}")


if __name__ == "__main__":
    unittest.main()
