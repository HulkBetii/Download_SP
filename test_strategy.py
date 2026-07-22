from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from core import strategy
from core.errors import Strategy


class LegacyNameTests(unittest.TestCase):
    """The public `strategy` field predates the enum and is asserted on."""

    def test_direct_and_sniff_keep_their_old_spellings(self):
        self.assertEqual(strategy.legacy_strategy_name(Strategy.DIRECT), "yt-dlp")
        self.assertEqual(strategy.legacy_strategy_name(Strategy.SNIFF), "browser-sniff")

    def test_every_strategy_has_a_name(self):
        for item in Strategy:
            self.assertTrue(strategy.legacy_strategy_name(item))


class DrmGateTests(unittest.TestCase):
    """Confirmed protection stops the ladder; a suspicion must not.

    The selection-failure text this codebase generates says things like
    "nghi la license/DRM nhung chua du ket luan". Running that through
    classify() reports DRM_PROTECTED, so deciding fatality from the message
    would abandon the ladder on a mere hint - the exact confusion
    test_auto_pipeline already guards against.
    """

    def _run(self, drm_confirmed: bool) -> list[str]:
        tried: list[str] = []

        def spy_mse(*args, **kwargs):
            tried.append("mse")
            return None

        def spy_record(*args, **kwargs):
            tried.append("record")
            return None

        with patch.object(strategy, "_try_mse", spy_mse), \
             patch.object(strategy, "_try_record", spy_record):
            with self.assertRaises(Exception):
                strategy.escalate(
                    "https://example.test/watch",
                    tempfile.gettempdir(),
                    RuntimeError("Co request nghi la license/DRM nhung chua du ket luan"),
                    drm_confirmed=drm_confirmed,
                )
        return tried

    def test_confirmed_drm_tries_nothing(self):
        self.assertEqual(self._run(drm_confirmed=True), [])

    def test_unconfirmed_suspicion_still_escalates(self):
        self.assertEqual(self._run(drm_confirmed=False), ["mse", "record"])

    def test_original_error_is_reraised_when_no_rung_works(self):
        original = RuntimeError("khong chon duoc candidate")
        with patch.object(strategy, "_try_mse", return_value=None), \
             patch.object(strategy, "_try_record", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                strategy.escalate("https://x/watch", tempfile.gettempdir(), original)
        self.assertIs(ctx.exception, original, "callers need the actionable error, not the last attempt")


class LadderOrderTests(unittest.TestCase):
    def test_mse_is_tried_before_recording(self):
        """Recording costs real time; segment capture is always preferable."""
        order: list[str] = []

        def spy_mse(*args, **kwargs):
            order.append("mse")
            return None

        def spy_record(*args, **kwargs):
            order.append("record")
            return {"ok": True, "strategy": "ffmpeg-record", "sourceUrl": "x"}

        with patch.object(strategy, "_try_mse", spy_mse), \
             patch.object(strategy, "_try_record", spy_record):
            result = strategy.escalate("https://x/w", tempfile.gettempdir(), RuntimeError("boom"))

        self.assertEqual(order, ["mse", "record"])
        self.assertEqual(result["strategy"], "ffmpeg-record")

    def test_recording_is_skipped_when_mse_succeeds(self):
        def spy_record(*args, **kwargs):
            raise AssertionError("recording must not run after a successful capture")

        with patch.object(strategy, "_try_mse", return_value={"ok": True, "strategy": "mse-capture"}), \
             patch.object(strategy, "_try_record", spy_record):
            result = strategy.escalate("https://x/w", tempfile.gettempdir(), RuntimeError("boom"))

        self.assertEqual(result["strategy"], "mse-capture")

    def test_events_are_emitted_for_the_ui(self):
        events: list[dict] = []
        with patch.object(strategy, "_try_mse", return_value=None), \
             patch.object(strategy, "_try_record", return_value=None):
            with self.assertRaises(Exception):
                strategy.escalate("https://x/w", tempfile.gettempdir(), RuntimeError("boom"), emit=events.append)
        # No rung succeeded, so nothing should claim completion.
        self.assertFalse(any(event.get("stage") == "done" for event in events))


class TitleTests(unittest.TestCase):
    def test_title_taken_from_url_path(self):
        self.assertEqual(strategy._title_from_url("https://x.com/videos/my-clip.mp4"), "my-clip")

    def test_falls_back_to_host(self):
        self.assertEqual(strategy._title_from_url("https://example.com/"), "example.com")

    def test_unsafe_characters_removed(self):
        self.assertNotIn("/", strategy._title_from_url("https://x.com/a:b*c.mp4"))

    def test_empty_url_is_safe(self):
        self.assertEqual(strategy._title_from_url(""), "video")


class PipelineIntegrationTests(unittest.TestCase):
    def _sniffer(self, drm: bool):
        signals = ["manifest_content_protection"] if drm else []

        def sniffer(url, **kwargs):
            media = []
            if drm:
                media = [{
                    "id": "c1",
                    "url": "https://cdn.test/v.m3u8",
                    "kind": "hls",
                    "status": 200,
                    "drmSignals": signals,
                    "requestHeaders": {},
                    "responseHeaders": {},
                }]
            return {
                "ok": True,
                "sourceUrl": url,
                "browserName": "coccoc",
                "browserPath": "x",
                "elapsedSeconds": 1.0,
                "mediaUrls": media,
                "diagnostics": [{"type": "drm_license", "message": "m", "url": "https://x/license"}],
            }

        return sniffer

    def _analyzer(self, *args, **kwargs):
        raise RuntimeError("Unsupported URL: https://example.test/watch")

    def _tried_rungs(self, drm: bool) -> list[str]:
        from core.auto_pipeline import run_auto_download

        tried: list[str] = []
        with patch.object(strategy, "_try_mse", side_effect=lambda *a, **k: tried.append("mse")), \
             patch.object(strategy, "_try_record", side_effect=lambda *a, **k: tried.append("record")):
            try:
                run_auto_download(
                    "https://example.test/watch",
                    tempfile.gettempdir(),
                    event_callback=lambda event: None,
                    analyzer=self._analyzer,
                    sniffer=self._sniffer(drm),
                )
            except Exception:
                pass
        return tried

    def test_no_candidate_escalates_below_the_sniffer(self):
        self.assertEqual(self._tried_rungs(drm=False), ["mse", "record"])

    def test_candidate_with_drm_signals_stops_the_ladder(self):
        self.assertEqual(self._tried_rungs(drm=True), [])


if __name__ == "__main__":
    unittest.main()
