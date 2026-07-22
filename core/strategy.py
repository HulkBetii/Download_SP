"""Escalation rungs below the browser sniffer.

``auto_pipeline.run_auto_download`` already walks the first rungs: probe with
yt-dlp, download directly, then fall back to the browser sniffer. That flow and
its return shape are pinned by ``test_auto_pipeline`` and are left alone. This
module adds the two rungs that sit underneath it, and the rule for when to stop
climbing at all.

The stopping rule matters more than the rungs. ``FailureKind.DRM_PROTECTED`` is
terminal: once content protection is confirmed, every further attempt is a
request against a server that is correctly refusing us, so the ladder ends there
rather than working through the remaining options. Suspicion is not confirmation
though - an unconfirmed license hint must not be reported as DRM, which is why
``escalate`` checks the classified failure rather than the presence of a
diagnostic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .errors import Strategy

__all__ = [
    "STRATEGY_LEGACY_NAMES",
    "escalate",
    "legacy_strategy_name",
]

EventCallback = Callable[[dict[str, Any]], None]
StatusCallback = Callable[[str, str], None]

# The public ``strategy`` field in results predates the Strategy enum and is
# asserted on by tests, so the old spellings are preserved.
STRATEGY_LEGACY_NAMES: dict[Strategy, str] = {
    Strategy.DIRECT: "yt-dlp",
    Strategy.PLUGIN: "yt-dlp-plugin",
    Strategy.COOKIE: "yt-dlp-cookies",
    Strategy.SNIFF: "browser-sniff",
    Strategy.MSE: "mse-capture",
    Strategy.RECORD: "ffmpeg-record",
}


def legacy_strategy_name(strategy: Strategy) -> str:
    return STRATEGY_LEGACY_NAMES.get(strategy, strategy.value)


def escalate(
    source_url: str,
    output_folder: str,
    sniff_error: BaseException | str,
    browser_name: str = "chrome",
    browser_path: str = "",
    browser_profile: str = "",
    browser_mode: str = "visible",
    timeout_seconds: int = 45,
    headers: dict[str, str] | None = None,
    drm_confirmed: bool = False,
    emit: EventCallback | None = None,
    status_callback: StatusCallback | None = None,
) -> dict[str, Any]:
    """Try MSE capture, then a real-time recording, after the sniffer failed.

    Raises the original ``sniff_error`` unchanged when no rung applies, so the
    caller keeps reporting the failure the user can actually act on rather than
    a message about the last thing we tried.

    ``drm_confirmed`` must come from the caller's inspection of the candidates
    (``MediaCandidate.drm_signals`` / manifest ContentProtection), never from
    classifying ``sniff_error``. The selection-failure text we generate ourselves
    says things like "nghi la license/DRM nhung chua du ket luan" - running that
    through ``classify`` reports DRM_PROTECTED and would abandon the ladder on a
    suspicion, which is exactly the distinction this codebase draws elsewhere.
    """
    if drm_confirmed:
        _emit(emit, "failed", "Phat hien DRM da xac nhan; khong thu them cach nao.", "error", 100)
        raise _as_exception(sniff_error)

    result = _try_mse(
        source_url,
        output_folder,
        browser_name=browser_name,
        browser_path=browser_path,
        browser_profile=browser_profile,
        browser_mode=browser_mode,
        timeout_seconds=timeout_seconds,
        headers=headers,
        emit=emit,
        status_callback=status_callback,
    )
    if result:
        return result

    result = _try_record(
        source_url,
        output_folder,
        headers=headers,
        emit=emit,
        status_callback=status_callback,
    )
    if result:
        return result

    raise _as_exception(sniff_error)


def _try_mse(
    source_url: str,
    output_folder: str,
    browser_name: str,
    browser_path: str,
    browser_profile: str,
    browser_mode: str,
    timeout_seconds: int,
    headers: dict[str, str] | None,
    emit: EventCallback | None,
    status_callback: StatusCallback | None,
) -> dict[str, Any] | None:
    from .mse_capture import MseCaptureError, capture_mse_streams, download_mse_streams

    _emit(emit, "mse", "Thu bat segment qua MediaSource.", "info", 80)
    try:
        capture = capture_mse_streams(
            source_url,
            browser_name=browser_name,
            browser_path=browser_path,
            profile_path=browser_profile,
            browser_mode=browser_mode,
            timeout_seconds=timeout_seconds,
            status_callback=status_callback,
        )
    except Exception as exc:
        _emit(emit, "mse", f"Bat MSE khong thanh cong: {exc}", "warning", 85)
        return None

    streams = capture.get("_streams") or []
    if not any(getattr(stream, "segments", None) for stream in streams):
        _emit(emit, "mse", "Khong co segment nao tai duoc tu MediaSource.", "warning", 85)
        return None

    try:
        output_path = download_mse_streams(
            streams,
            output_folder,
            title=_title_from_url(source_url),
            headers=headers,
            status_callback=status_callback,
        )
    except MseCaptureError as exc:
        _emit(emit, "mse", f"Ghep segment that bai: {exc}", "warning", 90)
        return None

    _emit(emit, "done", "Tai xong bang MSE capture.", "success", 100)
    return {
        "ok": True,
        "strategy": legacy_strategy_name(Strategy.MSE),
        "sourceUrl": source_url,
        "outputPath": output_path,
        "streams": capture.get("streams", []),
    }


def _try_record(
    source_url: str,
    output_folder: str,
    headers: dict[str, str] | None,
    emit: EventCallback | None,
    status_callback: StatusCallback | None,
) -> dict[str, Any] | None:
    from .recorder import RecorderError, ffmpeg_available, probe_duration, record_stream

    if not ffmpeg_available():
        return None

    duration = probe_duration(source_url, headers)
    if duration:
        _emit(
            emit,
            "record",
            f"Ghi truc tiep se ton khoang {int(duration // 60)} phut vi chay theo thoi gian thuc.",
            "warning",
            92,
        )
    else:
        _emit(emit, "record", "Thu ghi truc tiep bang ffmpeg (chay theo thoi gian thuc).", "warning", 92)

    target = Path(output_folder) / f"{_title_from_url(source_url)}.mp4"
    try:
        output_path = record_stream(
            source_url,
            str(target),
            headers=headers,
            duration_limit=duration,
            status_callback=status_callback,
        )
    except RecorderError as exc:
        _emit(emit, "record", f"Ghi truc tiep that bai: {exc}", "warning", 95)
        return None

    _emit(emit, "done", "Tai xong bang ffmpeg record.", "success", 100)
    return {
        "ok": True,
        "strategy": legacy_strategy_name(Strategy.RECORD),
        "sourceUrl": source_url,
        "outputPath": output_path,
    }


def _emit(emit: EventCallback | None, stage: str, message: str, level: str, progress: int) -> None:
    if emit:
        emit({"stage": stage, "message": message, "level": level, "progress": progress})


def _as_exception(error: BaseException | str) -> BaseException:
    return error if isinstance(error, BaseException) else RuntimeError(str(error))


def _title_from_url(url: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(str(url or ""))
    stem = Path(parsed.path or "").stem or parsed.netloc or "video"
    cleaned = "".join(ch for ch in stem if ch.isalnum() or ch in " ._-").strip()
    return (cleaned or "video")[:120]
