"""Capture video that a player feeds through Media Source Extensions.

Many modern players never expose a manifest or a plain file URL: they fetch
segments with ``fetch``/``XHR`` and push them straight into a ``SourceBuffer``,
leaving ``<video src>`` as an opaque ``blob:`` URL. The network sniffer sees
those requests but deliberately discards them - ``_is_media_url`` filters out
``.m4s``/``.ts``/``.cmfv`` and byte-range URLs so the candidate list stays a
short list of manifests rather than thousands of segments.

So the segment URLs have to be collected another way. Rather than shipping
segment *bytes* over the DevTools websocket (slow, and it stresses the framing
layer for no benefit), a single injected script patches ``fetch``, ``XHR`` and
``appendBuffer`` together. The page itself therefore knows which ArrayBuffer came
from which URL, and only compact metadata crosses the wire:
``{sourceBufferId, order, url, byteLength}``. Python then downloads those URLs
over ordinary HTTP with the headers the sniffer already captured.

Segments generated entirely in-page (decrypted or synthesised in JavaScript) have
no URL to report. Those are counted and surfaced, not silently dropped - but they
are not downloadable this way, and this module never attempts to defeat content
protection.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .media_sniffer import (
    MediaSnifferError,
    _CdpSession,
    _CdpWebSocket,
    _activate_page_playback,
    _enable_sniff_domains,
    _find_free_port,
    _launch_browser,
    _persistent_profile_dir,
    _stop_process,
    _wait_for_page_target,
    find_browser_executable,
)

__all__ = [
    "MseCaptureError",
    "MseSegment",
    "MseStream",
    "capture_mse_streams",
    "download_mse_streams",
]

BINDING_NAME = "__vdtSegment"

# Segment extensions the ordinary candidate sniffer deliberately ignores. It
# filters these out so the candidate list stays a handful of manifests instead of
# thousands of segments; here they are exactly what we want.
SEGMENT_EXTENSIONS = (
    ".m4s", ".ts", ".cmfv", ".cmfa", ".cmft", ".mp4", ".m4v", ".m4a",
    ".aac", ".webm", ".vtt", ".dash", ".seg", ".fmp4",
)
SEGMENT_MIME_HINTS = ("video/", "audio/", "application/octet-stream", "application/mp4")
# Anything smaller is almost certainly a playlist, key or tracking pixel.
MIN_SEGMENT_BYTES = 2048

# Cap on how many segment records are kept. A long stream can emit thousands;
# beyond this the capture is almost certainly not what the user wanted.
MAX_SEGMENTS = 20000

SEGMENT_DOWNLOAD_TIMEOUT = 30
StatusCallback = Callable[[str, str], None]


class MseCaptureError(RuntimeError):
    pass


# The injected script. Runs before page scripts so the player sees the patched
# APIs. Everything is wrapped defensively: throwing here would break playback and
# we would capture nothing at all.
MSE_CAPTURE_SCRIPT = r"""
(() => {
  if (window.__vdtMseInstalled) return;
  window.__vdtMseInstalled = true;

  const report = (payload) => {
    try {
      if (typeof window.__vdtSegment === "function") {
        window.__vdtSegment(JSON.stringify(payload));
      }
    } catch (error) {}
  };

  // Exact ArrayBuffer -> URL association. Survives as long as the player keeps
  // the buffer alive, which covers the normal "fetch then append" flow.
  const bufferUrls = new WeakMap();
  // Fallback for players that copy or slice the buffer before appending: match
  // on byte length against recently fetched responses, newest first.
  const recent = [];
  const RECENT_LIMIT = 64;

  const remember = (buffer, url) => {
    if (!buffer || !url) return;
    try { bufferUrls.set(buffer, url); } catch (error) {}
    recent.push({ url, byteLength: buffer.byteLength, used: false });
    if (recent.length > RECENT_LIMIT) recent.shift();
  };

  const lookup = (data) => {
    const buffer = data && data.buffer ? data.buffer : data;
    try {
      const exact = bufferUrls.get(buffer);
      if (exact) return exact;
    } catch (error) {}
    const byteLength = data ? (data.byteLength || 0) : 0;
    for (let i = recent.length - 1; i >= 0; i -= 1) {
      if (!recent[i].used && recent[i].byteLength === byteLength) {
        recent[i].used = true;
        return recent[i].url;
      }
    }
    return "";
  };

  const absolute = (url) => {
    try { return new URL(url, document.baseURI).href; } catch (error) { return String(url || ""); }
  };

  const originalFetch = window.fetch;
  if (originalFetch) {
    window.fetch = function (...args) {
      // The buffer must be recorded BEFORE the caller can read the body,
      // otherwise the player appends the segment while the association is still
      // pending and the append is reported with no URL. Awaiting the clone here
      // holds the returned promise until remember() has run.
      return originalFetch.apply(this, args).then(async (response) => {
        try {
          const url = absolute(response.url || (args[0] && args[0].url) || args[0]);
          remember(await response.clone().arrayBuffer(), url);
        } catch (error) {}
        return response;
      });
    };
  }

  const XHR = window.XMLHttpRequest;
  if (XHR && XHR.prototype) {
    const open = XHR.prototype.open;
    XHR.prototype.open = function (method, url, ...rest) {
      this.__vdtUrl = absolute(url);
      // Registered here rather than in send(): players assign their own onload
      // between open() and send(), and listeners fire in registration order, so
      // hooking in send() would run after the segment was already appended.
      try {
        this.addEventListener("load", () => {
          try {
            const body = this.response;
            if (body instanceof ArrayBuffer) remember(body, this.__vdtUrl);
            else if (body && body.buffer instanceof ArrayBuffer) remember(body.buffer, this.__vdtUrl);
          } catch (error) {}
        });
      } catch (error) {}
      return open.call(this, method, url, ...rest);
    };
  }

  let sourceBufferSeq = 0;
  let appendSeq = 0;

  if (window.MediaSource && MediaSource.prototype) {
    const addSourceBuffer = MediaSource.prototype.addSourceBuffer;
    MediaSource.prototype.addSourceBuffer = function (mimeType) {
      const sourceBuffer = addSourceBuffer.call(this, mimeType);
      try {
        sourceBufferSeq += 1;
        sourceBuffer.__vdtId = "sb" + sourceBufferSeq;
        sourceBuffer.__vdtMime = String(mimeType || "");
        report({ type: "sourcebuffer", id: sourceBuffer.__vdtId, mime: sourceBuffer.__vdtMime });
      } catch (error) {}
      return sourceBuffer;
    };
  }

  if (window.SourceBuffer && SourceBuffer.prototype) {
    const appendBuffer = SourceBuffer.prototype.appendBuffer;
    SourceBuffer.prototype.appendBuffer = function (data) {
      try {
        appendSeq += 1;
        report({
          type: "append",
          id: this.__vdtId || "sb0",
          mime: this.__vdtMime || "",
          order: appendSeq,
          url: lookup(data),
          byteLength: data ? (data.byteLength || 0) : 0,
        });
      } catch (error) {}
      return appendBuffer.call(this, data);
    };
  }
})()
"""


@dataclass(slots=True)
class MseSegment:
    order: int
    url: str
    byte_length: int


class _SegmentCollector:
    """Records segment responses seen on the wire, including inside workers.

    This exists because the page-side hook cannot see the fetches at all: players
    such as hls.js run their loader in a Web Worker, and
    ``Page.addScriptToEvaluateOnNewDocument`` only reaches page and frame
    contexts. ``MediaSource`` lives on the main thread, so the appends are
    visible while the requests that produced them are not.

    Matching appends to URLs by byte length does not work either - hls.js demuxes
    MPEG-TS into fragmented MP4 before appending, so the appended buffer bears no
    size relationship to the segment that was downloaded. Network arrival order
    grouped by URL prefix is what actually holds across players.
    """

    def __init__(self) -> None:
        self.request_urls: dict[str, str] = {}
        self.request_mimes: dict[str, str] = {}
        self.segments: list[tuple[str, int]] = []
        self._seen_urls: set[str] = set()

    def handle(self, event: dict[str, Any]) -> None:
        method = event.get("method")
        params = event.get("params") or {}
        session = str(event.get("sessionId") or "")

        if method == "Network.responseReceived":
            response = params.get("response") or {}
            key = f"{session}:{params.get('requestId')}"
            self.request_urls[key] = str(response.get("url") or "")
            self.request_mimes[key] = str(response.get("mimeType") or "")
        elif method == "Network.loadingFinished":
            key = f"{session}:{params.get('requestId')}"
            url = self.request_urls.pop(key, "")
            mime = self.request_mimes.pop(key, "")
            size = int(params.get("encodedDataLength") or 0)
            if url and self._is_segment(url, mime, size) and url not in self._seen_urls:
                self._seen_urls.add(url)
                self.segments.append((url, size))

    def _is_segment(self, url: str, mime: str, size: int) -> bool:
        if size < MIN_SEGMENT_BYTES:
            return False
        lowered = url.lower().split("?", 1)[0]
        if lowered.endswith((".m3u8", ".mpd")):
            return False
        if any(lowered.endswith(ext) for ext in SEGMENT_EXTENSIONS):
            return True
        return any(hint in mime.lower() for hint in SEGMENT_MIME_HINTS)


def group_segments_by_rendition(segments: list[tuple[str, int]]) -> dict[str, list[tuple[str, int]]]:
    """Bucket segments by URL directory prefix.

    Segments of one rendition share a path prefix (``.../url_8/...``), so this
    separates video from audio and one bitrate from another without having to
    parse a manifest. Arrival order inside a bucket is playback order.
    """
    groups: dict[str, list[tuple[str, int]]] = {}
    for url, size in segments:
        base = url.split("?", 1)[0]
        prefix = base.rsplit("/", 1)[0] if "/" in base else base
        groups.setdefault(prefix, []).append((url, size))
    return groups


@dataclass(slots=True)
class MseStream:
    source_buffer_id: str
    mime_type: str
    segments: list[MseSegment] = field(default_factory=list)
    missing_url_count: int = 0

    @property
    def kind(self) -> str:
        lowered = self.mime_type.lower()
        if lowered.startswith("audio/"):
            return "audio"
        if lowered.startswith("video/"):
            return "video"
        return "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "sourceBufferId": self.source_buffer_id,
            "mimeType": self.mime_type,
            "kind": self.kind,
            "segmentCount": len(self.segments),
            "missingUrlCount": self.missing_url_count,
            "totalBytes": sum(segment.byte_length for segment in self.segments),
        }


def capture_mse_streams(
    page_url: str,
    browser_name: str = "chrome",
    browser_path: str = "",
    profile_path: str = "",
    browser_mode: str = "visible",
    timeout_seconds: int = 45,
    status_callback: StatusCallback | None = None,
) -> dict[str, Any]:
    """Open the page, let it play, and record what the player appends.

    Returns the ordered per-SourceBuffer segment lists plus enough diagnostics to
    explain an empty result.
    """
    browser_exe = find_browser_executable(browser_name, browser_path)
    timeout_seconds = max(10, min(180, int(timeout_seconds or 45)))
    port = _find_free_port()

    temp_profile = None
    user_data_dir = profile_path or _persistent_profile_dir(browser_name)
    if not user_data_dir:
        temp_profile = tempfile.TemporaryDirectory(prefix="video_downloader_mse_")
        user_data_dir = temp_profile.name

    process = None
    websocket = None
    started = time.time()
    streams: dict[str, MseStream] = {}
    seen_orders: set[int] = set()

    try:
        process = _launch_browser(browser_exe, port, user_data_dir, browser_mode=browser_mode)
        target = _wait_for_page_target(port, timeout=10)
        websocket = _CdpWebSocket(target["webSocketDebuggerUrl"])
        websocket.connect()
        cdp = _CdpSession(websocket)

        _enable_sniff_domains(cdp, collector=None)
        cdp.call("Runtime.addBinding", {"name": BINDING_NAME})
        cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": MSE_CAPTURE_SCRIPT})
        # Workers must be attached before navigation: players like hls.js fetch
        # every segment from a worker, so without this the requests are invisible.
        cdp.call("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True})
        cdp.call("Page.navigate", {"url": page_url})

        if status_callback:
            status_callback(
                f"Da mo {browser_name} de bat MSE. Neu trang can bam play, hay bam trong cua so vua mo.",
                "blue",
            )

        collector = _SegmentCollector()
        deadline = time.time() + timeout_seconds
        next_activation = time.time() + 3
        while time.time() < deadline:
            event = cdp.recv(timeout=0.5)
            if event:
                method = event.get("method")
                if method == "Target.attachedToTarget":
                    _attach_worker_session(cdp, event)
                elif method == "Runtime.bindingCalled":
                    params = event.get("params") or {}
                    if params.get("name") == BINDING_NAME:
                        _record_binding_payload(params.get("payload"), streams, seen_orders)
                else:
                    collector.handle(event)
                if len(collector.segments) >= MAX_SEGMENTS:
                    break
            now = time.time()
            if now >= next_activation and not collector.segments:
                _activate_page_playback(cdp)
                next_activation = now + 5

        ordered = _merge_network_segments(streams, collector)
        return {
            "ok": True,
            "sourceUrl": page_url,
            "elapsedSeconds": round(time.time() - started, 1),
            "usesMediaSource": bool(streams),
            "streams": [stream.to_dict() for stream in ordered],
            "_streams": ordered,
            "diagnostics": _build_diagnostics(ordered),
        }
    except MediaSnifferError:
        raise
    except Exception as exc:
        raise MseCaptureError(f"Bat MSE that bai: {exc}") from exc
    finally:
        if websocket:
            websocket.close()
        if process:
            _stop_process(process)
        if temp_profile:
            temp_profile.cleanup()


def _record_binding_payload(payload: Any, streams: dict[str, MseStream], seen_orders: set[int]) -> None:
    try:
        record = json.loads(str(payload or ""))
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(record, dict):
        return

    buffer_id = str(record.get("id") or "sb0")
    mime_type = str(record.get("mime") or "")

    stream = streams.get(buffer_id)
    if stream is None:
        stream = MseStream(source_buffer_id=buffer_id, mime_type=mime_type)
        streams[buffer_id] = stream
    elif mime_type and not stream.mime_type:
        stream.mime_type = mime_type

    if record.get("type") != "append":
        return

    order = int(record.get("order") or 0)
    if order in seen_orders:
        return
    seen_orders.add(order)

    url = str(record.get("url") or "")
    if not url:
        # Generated in-page; there is nothing to fetch. Counted so the caller can
        # explain why the result is short rather than reporting a clean success.
        stream.missing_url_count += 1
        return

    stream.segments.append(
        MseSegment(order=order, url=url, byte_length=int(record.get("byteLength") or 0))
    )


def _attach_worker_session(cdp: _CdpSession, event: dict[str, Any]) -> None:
    """Enable Network on a newly attached worker so its fetches become visible."""
    params = event.get("params") or {}
    session_id = str(params.get("sessionId") or "")
    if not session_id:
        return
    try:
        cdp.call("Network.enable", None, session_id=session_id, timeout=2.0)
        cdp.call(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
            session_id=session_id,
            timeout=2.0,
        )
    except MediaSnifferError:
        # Not every target type accepts these; losing one worker is survivable.
        pass


def _merge_network_segments(streams: dict[str, MseStream], collector: _SegmentCollector) -> list[MseStream]:
    """Build downloadable streams from network segments, labelled by MSE codecs.

    The MSE hook supplies the codec strings and confirms MediaSource is in play;
    the URLs come from the wire. They are joined by position - the largest
    rendition group is the video track, the next is audio - rather than by size
    matching, which demuxing players break.
    """
    mse_streams = _finalise_streams(streams)
    if any(stream.segments for stream in mse_streams):
        # The page-side hook already resolved URLs (fMP4 players fetching on the
        # main thread), so its ordering is authoritative.
        return mse_streams

    groups = group_segments_by_rendition(collector.segments)
    if not groups:
        return mse_streams

    ranked = sorted(groups.items(), key=lambda item: sum(size for _, size in item[1]), reverse=True)
    # Read codecs from the raw hook data: addSourceBuffer reports them even when
    # no append was captured, and _finalise_streams drops those empty streams.
    codecs = [stream.mime_type for stream in streams.values() if stream.mime_type]

    # How many groups are actually separate tracks rather than alternate
    # bitrates. MPEG-TS carries video and audio in one segment, so every group is
    # a rendition of the same content and taking two would mux the video with a
    # second copy of itself. Fragmented MP4 splits tracks, so there one group per
    # SourceBuffer codec is right.
    wanted = 1 if _groups_are_muxed(ranked) else max(1, len(codecs) or 1)

    result: list[MseStream] = []
    for index, (_prefix, entries) in enumerate(ranked[:wanted]):
        mime = codecs[index] if index < len(codecs) else ""
        stream = MseStream(source_buffer_id=f"net{index + 1}", mime_type=mime)
        stream.segments = [
            MseSegment(order=order, url=url, byte_length=size)
            for order, (url, size) in enumerate(entries, start=1)
        ]
        result.append(stream)
    return result


def _groups_are_muxed(ranked: list[tuple[str, list[tuple[str, int]]]]) -> bool:
    """True when segments are MPEG-TS, which carries all tracks in one file."""
    for _prefix, entries in ranked:
        for url, _size in entries:
            if url.lower().split("?", 1)[0].endswith(".ts"):
                return True
    return False


def _finalise_streams(streams: dict[str, MseStream]) -> list[MseStream]:
    """Sort by append order and drop repeats a seek or ABR switch introduced."""
    result: list[MseStream] = []
    for stream in streams.values():
        stream.segments.sort(key=lambda segment: segment.order)
        deduped: list[MseSegment] = []
        seen_urls: set[str] = set()
        for segment in stream.segments:
            if segment.url in seen_urls:
                continue
            seen_urls.add(segment.url)
            deduped.append(segment)
        stream.segments = deduped
        if stream.segments or stream.missing_url_count:
            result.append(stream)

    # Video first so the muxer gets a predictable input order.
    result.sort(key=lambda item: (item.kind != "video", item.source_buffer_id))
    return result


def _build_diagnostics(streams: list[MseStream]) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    if not streams:
        diagnostics.append({
            "type": "mse_no_streams",
            "message": "Khong thay SourceBuffer nao. Trang co the khong dung MSE, hoac chua bam play.",
            "url": "",
        })
        return diagnostics

    for stream in streams:
        if stream.missing_url_count and not stream.segments:
            diagnostics.append({
                "type": "mse_generated_segments",
                "message": (
                    f"Luong {stream.source_buffer_id} ({stream.mime_type}) co "
                    f"{stream.missing_url_count} segment sinh trong trang, khong co URL de tai."
                ),
                "url": "",
            })
        elif stream.missing_url_count:
            diagnostics.append({
                "type": "mse_partial",
                "message": (
                    f"Luong {stream.source_buffer_id} thieu URL cho "
                    f"{stream.missing_url_count}/{len(stream.segments) + stream.missing_url_count} segment."
                ),
                "url": "",
            })
    return diagnostics


def download_mse_streams(
    streams: list[MseStream],
    output_folder: str,
    title: str = "mse_capture",
    headers: dict[str, str] | None = None,
    status_callback: StatusCallback | None = None,
) -> str:
    """Fetch every captured segment, concatenate per stream, then mux.

    Returns the path of the muxed file. Raises if nothing usable was captured.
    """
    usable = [stream for stream in streams if stream.segments]
    if not usable:
        raise MseCaptureError("Khong co segment nao co URL de tai.")

    destination = Path(output_folder)
    destination.mkdir(parents=True, exist_ok=True)
    safe_title = _safe_filename(title)

    with tempfile.TemporaryDirectory(prefix="vdt_mse_dl_") as workspace:
        workspace_path = Path(workspace)
        stream_files: list[Path] = []

        for index, stream in enumerate(usable):
            part = workspace_path / f"stream_{index}_{stream.kind}.bin"
            written = _download_stream(stream, part, headers or {}, status_callback)
            if written:
                stream_files.append(part)

        if not stream_files:
            raise MseCaptureError("Tai segment that bai cho moi luong.")

        output_path = destination / f"{safe_title}.mp4"
        output_path = _unique_path(output_path)
        _mux(stream_files, output_path, status_callback)

    return str(output_path)


def _download_stream(
    stream: MseStream,
    target: Path,
    headers: dict[str, str],
    status_callback: StatusCallback | None,
) -> int:
    """Concatenate segments in append order. Returns bytes written."""
    total = 0
    failures = 0
    segment_count = len(stream.segments)

    with target.open("wb") as handle:
        for index, segment in enumerate(stream.segments, start=1):
            data = _fetch_segment(segment.url, headers)
            if data is None:
                failures += 1
                # A missing middle segment corrupts playback from that point, so
                # this is reported rather than quietly producing a broken file.
                continue
            handle.write(data)
            total += len(data)

            if status_callback and (index % 25 == 0 or index == segment_count):
                status_callback(
                    f"MSE {stream.kind}: {index}/{segment_count} segment ({total // 1024} KB)",
                    "blue",
                )

    if failures and status_callback:
        status_callback(
            f"Canh bao: {failures}/{segment_count} segment cua luong {stream.kind} tai that bai.",
            "orange",
        )
    return total


def _fetch_segment(url: str, headers: dict[str, str]) -> bytes | None:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=SEGMENT_DOWNLOAD_TIMEOUT) as response:
            return response.read()
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _mux(stream_files: list[Path], output_path: Path, status_callback: StatusCallback | None) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        # Without ffmpeg the best we can do is hand back the first stream as-is.
        shutil.copyfile(stream_files[0], output_path)
        if status_callback:
            status_callback(
                "Khong co ffmpeg nen khong ghep duoc audio/video; chi luu luong dau tien.",
                "orange",
            )
        return

    command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    for path in stream_files:
        command.extend(["-i", str(path)])
    command.extend(["-c", "copy", str(output_path)])

    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=600, check=False)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise MseCaptureError(f"ffmpeg khong ghep duoc: {exc}") from exc

    if completed.returncode != 0 or not output_path.exists():
        detail = (completed.stderr or "").strip().splitlines()
        reason = detail[-1] if detail else f"exit {completed.returncode}"
        raise MseCaptureError(f"ffmpeg khong ghep duoc: {reason}")

    if status_callback:
        status_callback(f"Da ghep {len(stream_files)} luong thanh {output_path.name}", "green")


def _safe_filename(title: str) -> str:
    cleaned = "".join(ch for ch in str(title or "") if ch.isalnum() or ch in " ._-").strip()
    return (cleaned or "mse_capture")[:120]


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for index in range(1, 1000):
        candidate = path.with_name(f"{stem} ({index}){suffix}")
        if not candidate.exists():
            return candidate
    return path
