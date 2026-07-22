from __future__ import annotations

import json
import socket
import struct
import unittest

from core.media_sniffer import MediaSnifferError, _CdpSession, _CdpWebSocket, _xor_mask


def build_frame(payload: bytes, opcode: int = 0x1, fin: bool = True, mask: bytes = b"") -> bytes:
    """Build a server->client frame (unmasked by default, as real servers send)."""
    header = bytearray([(0x80 if fin else 0x00) | opcode])
    length = len(payload)
    flag = 0x80 if mask else 0x00
    if length < 126:
        header.append(flag | length)
    elif length < 65536:
        header.append(flag | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(flag | 127)
        header.extend(struct.pack("!Q", length))
    if mask:
        header.extend(mask)
        payload = _xor_mask(payload, mask)
    return bytes(header) + payload


class FakeSocket:
    """Feeds a scripted byte stream, optionally timing out between chunks."""

    def __init__(self, chunks: list[bytes | None]):
        # A None entry represents a socket timeout at that point in the stream.
        self.chunks = list(chunks)
        self.sent = bytearray()
        self.closed = False
        self._timeout = None

    def settimeout(self, value):
        self._timeout = value

    def gettimeout(self):
        return self._timeout

    def recv(self, _size):
        if not self.chunks:
            raise socket.timeout()
        chunk = self.chunks.pop(0)
        if chunk is None:
            raise socket.timeout()
        return chunk

    def sendall(self, data):
        self.sent.extend(data)

    def close(self):
        self.closed = True


def make_socket(chunks: list[bytes | None]) -> _CdpWebSocket:
    ws = _CdpWebSocket("ws://127.0.0.1:9222/devtools/page/x")
    ws.sock = FakeSocket(chunks)  # type: ignore[assignment]
    return ws


class MaskingTests(unittest.TestCase):
    def test_mask_roundtrip(self):
        payload = b"hello websocket" * 100
        mask = b"\x01\x02\x03\x04"
        self.assertEqual(_xor_mask(_xor_mask(payload, mask), mask), payload)

    def test_mask_matches_naive_per_byte_implementation(self):
        payload = bytes(range(256)) * 7
        mask = b"\xde\xad\xbe\xef"
        naive = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.assertEqual(_xor_mask(payload, mask), naive)

    def test_empty_payload_and_empty_mask(self):
        self.assertEqual(_xor_mask(b"", b"\x01\x02\x03\x04"), b"")
        self.assertEqual(_xor_mask(b"abc", b""), b"abc")


class FrameParsingTests(unittest.TestCase):
    def test_simple_message(self):
        ws = make_socket([build_frame(json.dumps({"id": 1}).encode())])
        self.assertEqual(ws.recv_json(timeout=1), {"id": 1})

    def test_extended_16bit_length(self):
        payload = json.dumps({"d": "x" * 1000}).encode()
        ws = make_socket([build_frame(payload)])
        self.assertEqual(ws.recv_json(timeout=1)["d"], "x" * 1000)

    def test_extended_64bit_length(self):
        payload = json.dumps({"d": "y" * 70000}).encode()
        ws = make_socket([build_frame(payload)])
        self.assertEqual(ws.recv_json(timeout=1)["d"], "y" * 70000)

    def test_masked_server_frame_is_unmasked(self):
        payload = json.dumps({"ok": True}).encode()
        ws = make_socket([build_frame(payload, mask=b"\x0a\x0b\x0c\x0d")])
        self.assertEqual(ws.recv_json(timeout=1), {"ok": True})

    def test_two_messages_in_one_chunk(self):
        frames = build_frame(b'{"id":1}') + build_frame(b'{"id":2}')
        ws = make_socket([frames])
        self.assertEqual(ws.recv_json(timeout=1), {"id": 1})
        self.assertEqual(ws.recv_json(timeout=1), {"id": 2})

    def test_message_split_across_chunks(self):
        frame = build_frame(json.dumps({"id": 7, "pad": "z" * 500}).encode())
        ws = make_socket([frame[:3], frame[3:40], frame[40:]])
        self.assertEqual(ws.recv_json(timeout=1)["id"], 7)


class DesyncRegressionTests(unittest.TestCase):
    """The bug that made large payloads unusable: a timeout mid-frame."""

    def test_timeout_midframe_does_not_corrupt_stream(self):
        frame = build_frame(json.dumps({"id": 42, "pad": "q" * 2000}).encode())
        # Header + part of the payload, then a timeout, then the remainder.
        ws = make_socket([frame[:100], None, frame[100:]])

        self.assertIsNone(ws.recv_json(timeout=0.01), "should report nothing yet")
        self.assertEqual(ws.recv_json(timeout=1)["id"], 42, "frame must survive the timeout")

    def test_repeated_timeouts_still_recover(self):
        frame = build_frame(json.dumps({"id": 9}).encode())
        # Timeouts before the frame, mid-header, and mid-payload.
        ws = make_socket([None, None, frame[:2], None, frame[2:]])

        received = None
        for _ in range(6):
            received = ws.recv_json(timeout=0.01)
            if received is not None:
                break
        self.assertEqual(received, {"id": 9})

    def test_next_message_intact_after_partial_read(self):
        first = build_frame(b'{"id":1}')
        second = build_frame(b'{"id":2}')
        ws = make_socket([first + second[:1], None, second[1:]])
        self.assertEqual(ws.recv_json(timeout=1), {"id": 1})
        self.assertIsNone(ws.recv_json(timeout=0.01))
        self.assertEqual(ws.recv_json(timeout=1), {"id": 2})


class FragmentationTests(unittest.TestCase):
    def test_continuation_frames_are_reassembled(self):
        payload = json.dumps({"id": 5, "big": "a" * 100}).encode()
        half = len(payload) // 2
        ws = make_socket([
            build_frame(payload[:half], opcode=0x1, fin=False),
            build_frame(payload[half:], opcode=0x0, fin=True),
        ])
        self.assertEqual(ws.recv_json(timeout=1)["id"], 5)

    def test_three_way_fragmentation(self):
        payload = json.dumps({"v": "b" * 300}).encode()
        a, b = len(payload) // 3, 2 * len(payload) // 3
        ws = make_socket([
            build_frame(payload[:a], opcode=0x1, fin=False),
            build_frame(payload[a:b], opcode=0x0, fin=False),
            build_frame(payload[b:], opcode=0x0, fin=True),
        ])
        self.assertEqual(ws.recv_json(timeout=1)["v"], "b" * 300)

    def test_ping_interleaved_between_fragments(self):
        payload = json.dumps({"id": 11}).encode()
        half = len(payload) // 2
        ws = make_socket([
            build_frame(payload[:half], opcode=0x1, fin=False),
            build_frame(b"ka", opcode=0x9),
            build_frame(payload[half:], opcode=0x0, fin=True),
        ])
        self.assertEqual(ws.recv_json(timeout=1), {"id": 11})


class ControlFrameTests(unittest.TestCase):
    def test_ping_is_answered_with_pong(self):
        ws = make_socket([build_frame(b"hb", opcode=0x9), build_frame(b'{"id":3}')])
        self.assertEqual(ws.recv_json(timeout=1), {"id": 3})

        sent = bytes(ws.sock.sent)  # type: ignore[union-attr]
        self.assertTrue(sent, "a pong should have been sent")
        self.assertEqual(sent[0] & 0x0F, 0xA, "reply opcode should be PONG")

    def test_pong_is_ignored(self):
        ws = make_socket([build_frame(b"", opcode=0xA), build_frame(b'{"id":4}')])
        self.assertEqual(ws.recv_json(timeout=1), {"id": 4})

    def test_close_frame_raises_instead_of_looking_idle(self):
        """Previously a close became an empty payload - indistinguishable from idle."""
        ws = make_socket([build_frame(b"", opcode=0x8)])
        with self.assertRaises(MediaSnifferError):
            ws.recv_json(timeout=1)

    def test_peer_disconnect_raises(self):
        ws = make_socket([b""])
        with self.assertRaises(MediaSnifferError):
            ws.recv_json(timeout=1)


class SendFrameTests(unittest.TestCase):
    def test_sent_frame_is_masked_and_text_opcode(self):
        ws = make_socket([])
        ws.send_json({"id": 1, "method": "Page.enable"})
        sent = bytes(ws.sock.sent)  # type: ignore[union-attr]
        self.assertEqual(sent[0], 0x81, "FIN + text opcode")
        self.assertTrue(sent[1] & 0x80, "client frames must be masked")

    def test_large_payload_uses_extended_length(self):
        ws = make_socket([])
        ws.send_json({"data": "x" * 70000})
        sent = bytes(ws.sock.sent)  # type: ignore[union-attr]
        self.assertEqual(sent[1] & 0x7F, 127, "should use 64-bit length")


class SessionTests(unittest.TestCase):
    def test_event_buffer_is_bounded(self):
        """A long capture emits far more events than any command wait needs."""
        ws = make_socket([])
        session = _CdpSession(ws)
        for index in range(_CdpSession.MAX_EVENT_BUFFER + 250):
            session._buffer_event({"method": "Network.requestWillBeSent", "n": index})

        self.assertEqual(len(session.event_buffer), _CdpSession.MAX_EVENT_BUFFER)
        self.assertEqual(session.dropped_events, 250)
        # Oldest dropped first, so the newest event is always retained.
        self.assertEqual(session.event_buffer[-1]["n"], _CdpSession.MAX_EVENT_BUFFER + 249)

    def test_call_returns_result_and_buffers_interleaved_events(self):
        event = build_frame(json.dumps({"method": "Network.requestWillBeSent", "params": {}}).encode())
        result = build_frame(json.dumps({"id": 1, "result": {"ok": True}}).encode())
        ws = make_socket([event, result])

        session = _CdpSession(ws)
        self.assertEqual(session.call("Page.enable"), {"ok": True})
        self.assertEqual(len(session.event_buffer), 1)

    def test_call_raises_on_cdp_error(self):
        frame = build_frame(json.dumps({"id": 1, "error": {"message": "nope"}}).encode())
        session = _CdpSession(make_socket([frame]))
        with self.assertRaises(MediaSnifferError):
            session.call("Bad.method")

    def test_call_honours_custom_timeout(self):
        session = _CdpSession(make_socket([]))
        with self.assertRaises(MediaSnifferError) as ctx:
            session.call("Slow.method", timeout=0.1)
        self.assertIn("timeout", str(ctx.exception).lower())

    def test_recv_drains_buffer_before_socket(self):
        session = _CdpSession(make_socket([]))
        session._buffer_event({"method": "A"})
        self.assertEqual(session.recv(timeout=0.01), {"method": "A"})


class DrmDetectionTests(unittest.TestCase):
    """The DRM guard must be precise in both directions.

    Too loose and the tool refuses freely downloadable video (a license
    diagnostic steers the failure message toward "we do not bypass DRM"); too
    strict and it would attempt content it should decline.
    """

    def test_real_drm_endpoints_are_detected(self):
        from core.media_sniffer import _is_license_url

        for url in (
            "https://proxy.widevine.com/license",
            "https://lic.example.com/playready/rightsmanager.asmx",
            "https://fp.example.com/fairplay/ckc",
            "https://x.com/drm/license",
            "https://api.example.com/getLicense?id=1",
            "https://cdn.example.com/licenseserver/acquire",
        ):
            with self.subTest(url=url):
                self.assertTrue(_is_license_url(url))

    def test_ordinary_licensing_links_are_not_drm(self):
        from core.media_sniffer import _is_license_url

        for url in (
            "https://creativecommons.org/licenses/by/3.0/",
            "https://example.com/LICENSE.txt",
            "https://example.com/licenses/mit",
        ):
            with self.subTest(url=url):
                self.assertFalse(_is_license_url(url), "CC/OSS licence links are not DRM")

    def test_substring_collisions_are_not_drm(self):
        """'eme' used to match inside 'themes', 'drm' inside any hostname."""
        from core.media_sniffer import _is_license_url

        for url in (
            "https://site.com/wp-content/themes/foo/style.css",
            "https://cdn.example.com/elements/player.js",
            "https://scheme.example.com/video.mp4",
            "https://vid.example.com/stream/segment-1.m4s",
        ):
            with self.subTest(url=url):
                self.assertFalse(_is_license_url(url))

    def test_manifest_content_protection_still_detected(self):
        from core.media_sniffer import _manifest_has_drm

        self.assertTrue(_manifest_has_drm('<ContentProtection schemeIdUri="urn:mpeg:dash:mp4protection:2011"/>'))
        self.assertTrue(_manifest_has_drm("com.widevine.alpha"))
        self.assertTrue(_manifest_has_drm("<cenc:pssh>abc</cenc:pssh>"))
        self.assertFalse(_manifest_has_drm("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\na.m3u8"))


if __name__ == "__main__":
    unittest.main()
