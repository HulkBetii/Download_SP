from __future__ import annotations

import unittest

from core.media_sniffer import _CdpSession, extract_media_candidates_from_events, rank_media_candidates, select_best_candidate


class MediaSnifferTests(unittest.TestCase):
    def test_cdp_session_buffers_events_received_during_command_call(self):
        class FakeWebSocket:
            def __init__(self):
                self.sent = []
                self.messages = [
                    {"method": "Network.responseReceived", "params": {"response": {"url": "https://cdn.example.test/master.m3u8"}}},
                    {"id": 1, "result": {"ok": True}},
                ]

            def send_json(self, payload):
                self.sent.append(payload)

            def recv_json(self, timeout=1.0):
                return self.messages.pop(0) if self.messages else None

        session = _CdpSession(FakeWebSocket())

        self.assertEqual(session.call("Runtime.evaluate", session_id="iframe-session"), {"ok": True})
        self.assertEqual(session.websocket.sent[0]["sessionId"], "iframe-session")
        self.assertEqual(session.recv(), {"method": "Network.responseReceived", "params": {"response": {"url": "https://cdn.example.test/master.m3u8"}}})

    def test_network_request_ids_are_scoped_by_cdp_session(self):
        candidates = extract_media_candidates_from_events(
            [
                {
                    "sessionId": "main",
                    "method": "Network.requestWillBeSent",
                    "params": {
                        "requestId": "1",
                        "type": "XHR",
                        "request": {
                            "url": "https://main.example.test/master.m3u8",
                            "headers": {"Referer": "https://main.example.test/watch"},
                        },
                    },
                },
                {
                    "sessionId": "iframe",
                    "method": "Network.requestWillBeSent",
                    "params": {
                        "requestId": "1",
                        "type": "XHR",
                        "request": {
                            "url": "https://player.example.test/master.m3u8",
                            "headers": {"Referer": "https://player.example.test/embed"},
                        },
                    },
                },
                {
                    "sessionId": "iframe",
                    "method": "Network.requestWillBeSentExtraInfo",
                    "params": {"requestId": "1", "headers": {"Cookie": "session=iframe"}},
                },
            ]
        )

        by_url = {item["url"]: item for item in candidates}
        self.assertNotIn("Cookie", by_url["https://main.example.test/master.m3u8"]["requestHeaderNames"])
        self.assertIn("Cookie", by_url["https://player.example.test/master.m3u8"]["requestHeaderNames"])

    def test_extracts_manifest_and_direct_media_from_network_events(self):
        candidates = extract_media_candidates_from_events(
            [
                {
                    "method": "Network.responseReceived",
                    "params": {
                        "type": "XHR",
                        "response": {
                            "url": "https://cdn.example.test/master.m3u8?token=abc",
                            "mimeType": "application/vnd.apple.mpegurl",
                            "status": 200,
                        },
                    },
                },
                {
                    "method": "Network.requestWillBeSent",
                    "params": {
                        "type": "Media",
                        "request": {"url": "https://cdn.example.test/video.mp4"},
                    },
                },
                {
                    "method": "Network.responseReceived",
                    "params": {
                        "type": "Image",
                        "response": {"url": "https://cdn.example.test/poster.jpg", "mimeType": "image/jpeg"},
                    },
                },
                {
                    "method": "Network.requestWillBeSent",
                    "params": {"type": "Media", "request": {"url": "blob:https://example.test/123"}},
                },
            ]
        )

        self.assertEqual([item["kind"] for item in candidates], ["hls", "video"])
        self.assertEqual(candidates[0]["url"], "https://cdn.example.test/master.m3u8?token=abc")
        self.assertEqual(candidates[1]["url"], "https://cdn.example.test/video.mp4")


    def test_ranking_prefers_manifest_and_penalizes_noise(self):
        ranked = rank_media_candidates(
            [
                {"url": "https://cdn.example.test/poster.jpg", "kind": "video", "mimeType": "image/jpeg"},
                {"url": "https://cdn.example.test/video.mp4", "kind": "video", "mimeType": "video/mp4", "status": 200},
                {"url": "https://cdn.example.test/master.m3u8", "kind": "hls", "mimeType": "application/vnd.apple.mpegurl", "status": 200},
            ],
            page_url="https://example.test/watch",
        )

        self.assertEqual(ranked[0].kind, "hls")
        self.assertEqual(select_best_candidate(ranked).kind, "hls")

    def test_detects_drm_like_candidates(self):
        ranked = rank_media_candidates(
            [
                {
                    "url": "https://cdn.example.test/protected.mpd",
                    "kind": "dash",
                    "mimeType": "application/dash+xml",
                    "drmSignals": ["manifest_content_protection"],
                }
            ]
        )

        self.assertTrue(ranked[0].drm_signals)
        self.assertIsNone(select_best_candidate(ranked))

if __name__ == "__main__":
    unittest.main()
