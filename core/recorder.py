"""Last-resort capture: let ffmpeg read the stream directly.

This is the bottom rung of the strategy ladder. It only runs once yt-dlp, the
browser sniffer and MSE capture have all failed, because it has a cost none of
the others do: a live stream is recorded in real time, so a two hour video takes
two hours. Quality is not affected - the default is a stream copy, no re-encode.

Nothing here attempts to defeat content protection. ffmpeg is handed a URL that
was already observed being fetched in the clear; if the stream is encrypted this
fails like any other rung and the ladder reports it.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

__all__ = [
    "RecorderError",
    "ffmpeg_available",
    "record_stream",
    "probe_duration",
]

StatusCallback = Callable[[str, str], None]

# Protocols ffmpeg can read directly. Anything else is not worth attempting.
SUPPORTED_SCHEMES = ("http://", "https://", "rtmp://", "rtmps://", "rtsp://", "srt://")

# A recording that has produced nothing for this long is stalled, not slow.
STALL_TIMEOUT_SECONDS = 90
# Hard ceiling so a live stream cannot record forever.
MAX_RECORD_SECONDS = 4 * 60 * 60

_DURATION_RE = re.compile(r"time=(\d+):(\d\d):(\d\d(?:\.\d+)?)")


class RecorderError(RuntimeError):
    pass


def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg"))


def probe_duration(url: str, headers: dict[str, str] | None = None, timeout: int = 20) -> float | None:
    """Duration in seconds, or None for a live stream / unknown.

    Used to warn the user before committing to a real-time recording.
    """
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None

    command = [ffprobe, "-v", "error"]
    command.extend(_header_args(headers))
    command.extend([
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        url,
    ])
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return None

    value = (completed.stdout or "").strip()
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return None
    return duration if duration > 0 else None


def record_stream(
    url: str,
    output_path: str,
    headers: dict[str, str] | None = None,
    duration_limit: float | None = None,
    status_callback: StatusCallback | None = None,
) -> str:
    """Record ``url`` to ``output_path`` with a stream copy.

    Returns the output path. Raises :class:`RecorderError` if ffmpeg is missing,
    the URL is not a protocol ffmpeg reads, or the recording produced no output.
    """
    if not ffmpeg_available():
        raise RecorderError("Khong tim thay ffmpeg nen khong ghi duoc stream.")
    if not str(url or "").lower().startswith(SUPPORTED_SCHEMES):
        raise RecorderError(f"ffmpeg khong doc duoc giao thuc nay: {url[:80]}")

    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    limit = min(duration_limit or MAX_RECORD_SECONDS, MAX_RECORD_SECONDS)

    command = [shutil.which("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-stats"]
    command.extend(_header_args(headers))
    command.extend([
        "-i", url,
        "-c", "copy",
        # Timestamps from a live source are often not zero-based.
        "-avoid_negative_ts", "make_zero",
        "-t", str(int(limit)),
        str(target),
    ])

    if status_callback:
        status_callback(
            "Dang ghi truc tiep bang ffmpeg. Cach nay chay theo thoi gian thuc, "
            "video dai bao nhieu se ton bay nhieu thoi gian.",
            "orange",
        )

    return _run_recording(command, target, status_callback)


def _run_recording(command: list[str], target: Path, status_callback: StatusCallback | None) -> str:
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        raise RecorderError(f"Khong chay duoc ffmpeg: {exc}") from exc

    last_progress = time.time()
    last_reported = 0.0

    try:
        for line in process.stderr or ():
            seconds = _parse_progress(line)
            if seconds is None:
                continue
            last_progress = time.time()
            if status_callback and seconds - last_reported >= 10:
                last_reported = seconds
                status_callback(f"Da ghi {_format_clock(seconds)}", "blue")

            if time.time() - last_progress > STALL_TIMEOUT_SECONDS:
                process.kill()
                raise RecorderError("Stream dung lai qua lau, da huy ghi.")

        process.wait(timeout=30)
    except RecorderError:
        raise
    except subprocess.TimeoutExpired:
        process.kill()
    finally:
        if process.poll() is None:
            process.kill()
        # Popen does not close the pipe for us; leaving it open leaks a handle
        # per recording attempt, and the ladder may make several.
        if process.stderr:
            process.stderr.close()

    # ffmpeg returns non-zero when interrupted, which is normal for a live
    # stream that we stopped on purpose. What matters is whether the file is
    # usable, so judge by the output rather than the exit code.
    if not target.exists() or target.stat().st_size == 0:
        raise RecorderError("ffmpeg khong ghi duoc du lieu nao.")

    if status_callback:
        size_mb = target.stat().st_size / (1024 * 1024)
        status_callback(f"Ghi xong: {target.name} ({size_mb:.1f} MB)", "green")
    return str(target)


def _header_args(headers: dict[str, str] | None) -> list[str]:
    """Render captured request headers into ffmpeg's single -headers option."""
    if not headers:
        return []
    lines = [f"{name}: {value}" for name, value in headers.items() if name and value]
    if not lines:
        return []
    return ["-headers", "\r\n".join(lines) + "\r\n"]


def _parse_progress(line: str) -> float | None:
    match = _DURATION_RE.search(line or "")
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    try:
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (TypeError, ValueError):
        return None


def _format_clock(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"
