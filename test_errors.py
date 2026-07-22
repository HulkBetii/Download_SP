from __future__ import annotations

import unittest

from core.errors import (
    FailureKind,
    STRATEGY_ORDER,
    Strategy,
    classify,
    fold_ascii,
    is_fatal,
    next_strategy,
    normalize_message,
    should_change_cookie_source,
    should_fallback_to_sniff,
    should_retry_same_source,
)


class NormalizationTests(unittest.TestCase):
    def test_strips_repeated_ytdlp_error_prefix(self):
        self.assertEqual(normalize_message("ERROR: ERROR: boom"), "boom")

    def test_collapses_newlines_and_whitespace(self):
        self.assertEqual(normalize_message("a\r\n   b\n\tc"), "a b c")

    def test_empty_input_classifies_as_unknown(self):
        for value in ("", None, "   "):
            self.assertIs(classify(value), FailureKind.UNKNOWN)

    def test_accepts_exception_objects(self):
        self.assertIs(classify(RuntimeError("HTTP Error 429: Too Many Requests")), FailureKind.RATE_LIMITED)

    def test_fold_ascii_removes_vietnamese_diacritics(self):
        self.assertEqual(fold_ascii("Lỗi xác thực"), "loi xac thuc")


class ClassificationTests(unittest.TestCase):
    def test_known_messages(self):
        cases = [
            ("ERROR: Unsupported URL: https://example.com/x", FailureKind.UNSUPPORTED_URL),
            ("No suitable extractor found", FailureKind.UNSUPPORTED_URL),
            ("HTTP Error 429: Too Many Requests", FailureKind.RATE_LIMITED),
            ("HTTP Error 401: Unauthorized", FailureKind.AUTH_REQUIRED),
            ("HTTP Error 403: Forbidden", FailureKind.AUTH_REQUIRED),
            ("HTTP Error 404: Not Found", FailureKind.NOT_FOUND),
            ("Could not copy Chrome cookie database", FailureKind.COOKIE_DB_LOCKED),
            ("This video is DRM protected", FailureKind.DRM_PROTECTED),
            ("Widevine license request failed", FailureKind.DRM_PROTECTED),
            ("A PO Token is required for this client", FailureKind.PO_TOKEN_REQUIRED),
            ("YouTube is forcing SABR streaming for this client", FailureKind.SABR_ONLY),
            ("The read operation timed out", FailureKind.NETWORK),
            ("something entirely unexpected", FailureKind.UNKNOWN),
        ]
        for message, expected in cases:
            with self.subTest(message=message):
                self.assertIs(classify(message), expected)

    def test_matches_unaccented_vietnamese_downloader_message(self):
        message = (
            "Nguon/link nay hien khong duoc yt-dlp ho tro: https://example.com/x. "
            "Hay dung URL tu website nam trong danh sach yt-dlp ho tro"
        )
        self.assertIs(classify(message), FailureKind.UNSUPPORTED_URL)

    def test_matches_accented_vietnamese_message(self):
        self.assertIs(
            classify("Nguồn/link này hiện không được yt-dlp hỗ trợ"),
            FailureKind.UNSUPPORTED_URL,
        )


class RuleOrderingTests(unittest.TestCase):
    """Real yt-dlp messages mention several things at once; order decides."""

    def test_signed_url_in_404_is_not_mistaken_for_expired_token(self):
        # yt-dlp embeds the failing URL, and signed URLs carry token= / expires=.
        message = "HTTP Error 404: Not Found (https://cdn.example.com/v.mp4?token=abc&expires=123)"
        self.assertIs(classify(message), FailureKind.NOT_FOUND)

    def test_signed_url_in_403_is_classified_as_auth(self):
        message = "HTTP Error 403: Forbidden (https://cdn.example.com/v.m3u8?token=xyz)"
        self.assertIs(classify(message), FailureKind.AUTH_REQUIRED)

    def test_sabr_wins_over_accompanying_403(self):
        message = "HTTP Error 403: YouTube is forcing SABR streaming for this client"
        self.assertIs(classify(message), FailureKind.SABR_ONLY)

    def test_po_token_wins_over_generic_token_wording(self):
        message = "A PO Token is required; the token has expired"
        self.assertIs(classify(message), FailureKind.PO_TOKEN_REQUIRED)

    def test_drm_wins_over_accompanying_403(self):
        message = "HTTP Error 403: Forbidden - Widevine protected content"
        self.assertIs(classify(message), FailureKind.DRM_PROTECTED)

    def test_bare_drm_substring_does_not_trigger_drm(self):
        """'drm' shows up in unrelated CDN hostnames; it must not be fatal alone."""
        message = "HTTP Error 404: Not Found (https://drm-cdn.example.com/v.mp4)"
        self.assertIsNot(classify(message), FailureKind.DRM_PROTECTED)

    def test_expired_signature_still_classifies_as_token_expired(self):
        self.assertIs(classify("Signature extraction failed, link expired"), FailureKind.TOKEN_EXPIRED)


class FatalityTests(unittest.TestCase):
    def test_drm_is_fatal(self):
        self.assertTrue(is_fatal(FailureKind.DRM_PROTECTED))

    def test_nothing_else_is_fatal(self):
        for kind in FailureKind:
            if kind is not FailureKind.DRM_PROTECTED:
                with self.subTest(kind=kind):
                    self.assertFalse(is_fatal(kind))

    def test_drm_never_escalates_regardless_of_progress(self):
        for tried in ({}, {"direct"}, {"direct", "plugin", "cookie"}):
            with self.subTest(tried=tried):
                self.assertIsNone(next_strategy(FailureKind.DRM_PROTECTED, set(tried)))

    def test_drm_never_falls_back_to_sniff(self):
        self.assertFalse(should_fallback_to_sniff("Widevine protected content"))


class LadderTests(unittest.TestCase):
    def test_po_token_goes_to_plugin_rung(self):
        self.assertIs(next_strategy(FailureKind.PO_TOKEN_REQUIRED, {"direct"}), Strategy.PLUGIN)

    def test_sabr_goes_to_plugin_rung(self):
        self.assertIs(next_strategy(FailureKind.SABR_ONLY, {"direct"}), Strategy.PLUGIN)

    def test_auth_tries_cookie_before_sniff(self):
        self.assertIs(next_strategy(FailureKind.AUTH_REQUIRED, {"direct"}), Strategy.COOKIE)
        self.assertIs(next_strategy(FailureKind.AUTH_REQUIRED, {"direct", "cookie"}), Strategy.SNIFF)

    def test_blob_goes_to_mse_then_record(self):
        self.assertIs(next_strategy(FailureKind.BLOB_ONLY, {"direct", "sniff"}), Strategy.MSE)
        self.assertIs(next_strategy(FailureKind.BLOB_ONLY, {"direct", "sniff", "mse"}), Strategy.RECORD)

    def test_rate_limit_retries_the_same_rung(self):
        self.assertIs(next_strategy(FailureKind.RATE_LIMITED, set()), Strategy.DIRECT)

    def test_exhausted_preferences_fall_through_to_ladder_order(self):
        """An unusual error must keep escalating, not dead-end early."""
        result = next_strategy(FailureKind.PO_TOKEN_REQUIRED, {"direct", "plugin"})
        self.assertIs(result, Strategy.COOKIE)

    def test_returns_none_when_every_rung_is_exhausted(self):
        every = {strategy.value for strategy in STRATEGY_ORDER}
        self.assertIsNone(next_strategy(FailureKind.UNKNOWN, every))

    def test_ladder_terminates_for_every_failure_kind(self):
        """Guards against an infinite escalation loop for any classification."""
        for kind in FailureKind:
            with self.subTest(kind=kind):
                tried: set[str] = set()
                for _ in range(len(STRATEGY_ORDER) + 2):
                    step = next_strategy(kind, tried)
                    if step is None:
                        break
                    self.assertNotIn(step.value, tried, "ladder repeated a rung")
                    tried.add(step.value)
                else:
                    self.fail(f"ladder did not terminate for {kind}")


class PredicateTests(unittest.TestCase):
    def test_should_retry_same_source_for_transient_failures(self):
        self.assertTrue(should_retry_same_source("HTTP Error 429: Too Many Requests"))
        self.assertTrue(should_retry_same_source("connection reset by peer"))
        self.assertFalse(should_retry_same_source("Unsupported URL: x"))

    def test_should_change_cookie_source_for_credential_failures(self):
        self.assertTrue(should_change_cookie_source("HTTP Error 403: Forbidden"))
        self.assertTrue(should_change_cookie_source("Could not copy Chrome cookie database"))
        self.assertFalse(should_change_cookie_source("HTTP Error 429: Too Many Requests"))

    def test_should_fallback_to_sniff_for_extractor_failures(self):
        self.assertTrue(should_fallback_to_sniff("Unsupported URL: https://x"))
        self.assertTrue(should_fallback_to_sniff("HTTP Error 404: Not Found"))
        self.assertFalse(should_fallback_to_sniff("HTTP Error 429: Too Many Requests"))


class CompatibilityTests(unittest.TestCase):
    """The public predicates other modules import must keep their meaning."""

    def test_auto_pipeline_helpers_still_resolve(self):
        from core.auto_pipeline import is_retryable_media_error, is_unsupported_error

        self.assertTrue(is_unsupported_error("ERROR: Unsupported URL: https://x"))
        self.assertFalse(is_unsupported_error("HTTP Error 404: Not Found"))
        self.assertTrue(is_retryable_media_error("HTTP Error 403: Forbidden"))
        self.assertTrue(is_retryable_media_error("link expired"))
        self.assertFalse(is_retryable_media_error("Unsupported URL: https://x"))

    def test_downloader_helpers_still_resolve(self):
        from core.downloader import _is_browser_cookie_database_error, _is_unsupported_url_error

        self.assertTrue(_is_browser_cookie_database_error("ERROR: Could not copy Chrome cookie database"))
        self.assertFalse(_is_browser_cookie_database_error("HTTP Error 403"))
        self.assertTrue(_is_unsupported_url_error("ERROR: Unsupported URL: https://x"))


if __name__ == "__main__":
    unittest.main()
