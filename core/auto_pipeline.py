from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any, Callable

from yt_dlp.utils import DownloadError

from .analyzer import analyze_video
from .downloader import download_video
from .media_sniffer import (
    MediaCandidate,
    MediaSnifferError,
    build_download_headers,
    rank_media_candidates,
    select_best_candidate,
    sniff_media_urls,
)

AutoEventCallback = Callable[[dict[str, Any]], None]
AnalyzeFn = Callable[..., dict[str, Any]]
DownloadFn = Callable[..., Any]
SniffFn = Callable[..., dict[str, Any]]

AUTO_STAGE_PROBING = "probing"
AUTO_STAGE_SNIFFING = "sniffing"
AUTO_STAGE_SELECTING = "selecting"
AUTO_STAGE_DOWNLOADING = "downloading"
AUTO_STAGE_RETRYING = "retrying"
AUTO_STAGE_DONE = "done"
AUTO_STAGE_FAILED = "failed"


def run_auto_download(
    url: str,
    output_folder: str,
    cookie_file: str | None = None,
    optimize_mode: str = "quality",
    advanced_options: dict[str, Any] | None = None,
    browser_name: str = "coccoc",
    browser_path: str = "",
    browser_profile: str = "",
    browser_mode: str = "visible",
    sniff_timeout_seconds: int = 45,
    auto_select: bool = True,
    event_callback: AutoEventCallback | None = None,
    status_callback: Callable[[str, str], None] | None = None,
    analyzer: AnalyzeFn = analyze_video,
    downloader: DownloadFn = download_video,
    sniffer: SniffFn = sniff_media_urls,
) -> dict[str, Any]:
    advanced = dict(advanced_options or {})
    emit = _make_emitter(event_callback)
    source_url = _clean_url(url)
    if not source_url:
        raise ValueError("URL khong hop le.")
    output_path = str(Path(output_folder).expanduser())

    emit(AUTO_STAGE_PROBING, "Dang thu yt-dlp probe truoc.", progress=5)
    probe_error = ""
    try:
        probe = analyzer(
            source_url,
            cookie_file=cookie_file,
            optimize_mode=optimize_mode,
            advanced_options=advanced,
        )
    except Exception as exc:
        probe_error = _clean_error(str(exc))
        emit(
            AUTO_STAGE_PROBING,
            f"yt-dlp probe chua tai duoc truc tiep: {probe_error}",
            level="warning",
            progress=12,
            diagnostics=[_diagnostic_from_error(probe_error)],
        )
    else:
        if probe.get("available", True):
            emit(AUTO_STAGE_DOWNLOADING, "yt-dlp ho tro URL nay, bat dau tai truc tiep.", progress=35)
            try:
                downloader(
                    source_url,
                    output_path,
                    cookie_file,
                    status_callback=status_callback,
                    optimize_mode=optimize_mode,
                    advanced_options=advanced,
                )
            except Exception as exc:
                download_error = _clean_error(str(exc))
                if _should_fallback_to_sniff(download_error):
                    emit(
                        AUTO_STAGE_SNIFFING,
                        f"Tai truc tiep loi, chuyen sang bat media: {download_error}",
                        level="warning",
                        progress=45,
                        diagnostics=[_diagnostic_from_error(download_error)],
                    )
                    return _sniff_select_download(
                        source_url,
                        output_path,
                        cookie_file,
                        optimize_mode,
                        advanced,
                        browser_name,
                        browser_path,
                        browser_profile,
                        browser_mode,
                        sniff_timeout_seconds,
                        auto_select,
                        emit,
                        status_callback,
                        downloader,
                        sniffer,
                        retry_allowed=True,
                    )
                emit(AUTO_STAGE_FAILED, f"yt-dlp tai loi: {download_error}", level="error", progress=100)
                raise
            emit(AUTO_STAGE_DONE, "Tai xong bang yt-dlp.", level="success", progress=100)
            return {"ok": True, "strategy": "yt-dlp", "sourceUrl": source_url, "probe": probe}

    return _sniff_select_download(
        source_url,
        output_path,
        cookie_file,
        optimize_mode,
        advanced,
        browser_name,
        browser_path,
        browser_profile,
        browser_mode,
        sniff_timeout_seconds,
        auto_select,
        emit,
        status_callback,
        downloader,
        sniffer,
        retry_allowed=True,
        probe_error=probe_error,
    )


def inspect_media(
    url: str,
    browser_name: str = "coccoc",
    browser_path: str = "",
    browser_profile: str = "",
    browser_mode: str = "visible",
    sniff_timeout_seconds: int = 45,
    sniffer: SniffFn = sniff_media_urls,
) -> dict[str, Any]:
    source_url = _clean_url(url)
    result = sniffer(
        source_url,
        browser_name=browser_name,
        browser_path=browser_path,
        profile_path=browser_profile,
        timeout_seconds=sniff_timeout_seconds,
        browser_mode=browser_mode,
        include_sensitive_headers=True,
    )
    ranked = rank_media_candidates(result.get("mediaUrls", []), page_url=source_url)
    selected = select_best_candidate(ranked, page_url=source_url)
    result["mediaUrls"] = [candidate.to_dict(include_sensitive_headers=True) for candidate in ranked]
    result["selectedCandidateId"] = selected.candidate_id if selected else ""
    return result


def download_media_candidate(
    candidate: MediaCandidate | dict[str, Any],
    source_url: str,
    output_folder: str,
    cookie_file: str | None,
    optimize_mode: str,
    advanced_options: dict[str, Any] | None,
    status_callback: Callable[[str, str], None] | None = None,
    downloader: DownloadFn = download_video,
) -> None:
    media_candidate = candidate if isinstance(candidate, MediaCandidate) else _candidate_from_dict(candidate)
    advanced = _advanced_with_candidate_headers(advanced_options or {}, media_candidate, source_url)
    downloader(
        media_candidate.url,
        output_folder,
        cookie_file,
        status_callback=status_callback,
        optimize_mode=optimize_mode,
        advanced_options=advanced,
    )


def is_retryable_media_error(message: str) -> bool:
    text = str(message or "").lower()
    return any(
        token in text
        for token in (
            "http error 401",
            "http error 403",
            "unauthorized",
            "forbidden",
            "expired",
            "token",
            "signature",
        )
    )


def is_unsupported_error(message: str) -> bool:
    text = str(message or "").lower()
    folded_text = _fold_ascii(text)
    return (
        "unsupported url" in text
        or "no suitable extractor" in text
        or "khong duoc yt-dlp ho tro" in folded_text
    )

def _fold_ascii(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    return normalized.encode("ascii", "ignore").decode("ascii").lower()


def _sniff_select_download(
    source_url: str,
    output_folder: str,
    cookie_file: str | None,
    optimize_mode: str,
    advanced_options: dict[str, Any],
    browser_name: str,
    browser_path: str,
    browser_profile: str,
    browser_mode: str,
    sniff_timeout_seconds: int,
    auto_select: bool,
    emit: Callable[..., None],
    status_callback: Callable[[str, str], None] | None,
    downloader: DownloadFn,
    sniffer: SniffFn,
    retry_allowed: bool,
    probe_error: str = "",
) -> dict[str, Any]:
    emit(
        AUTO_STAGE_SNIFFING,
        f"Mo {browser_name} de bat request media. Hay bam play neu trang can thao tac.",
        progress=45,
    )
    try:
        sniff_result = inspect_media(
            source_url,
            browser_name=browser_name,
            browser_path=browser_path,
            browser_profile=browser_profile,
            browser_mode=browser_mode,
            sniff_timeout_seconds=sniff_timeout_seconds,
            sniffer=sniffer,
        )
    except MediaSnifferError:
        raise
    except Exception as exc:
        raise MediaSnifferError(f"Bat media loi: {_clean_error(str(exc))}") from exc

    candidates = [_candidate_from_dict(item) for item in sniff_result.get("mediaUrls", [])]
    diagnostics = list(sniff_result.get("diagnostics") or [])
    emit(
        AUTO_STAGE_SELECTING,
        f"Da bat duoc {len(candidates)} media candidate, dang chon link tot nhat.",
        progress=62,
        candidates=[candidate.to_dict(include_sensitive_headers=True) for candidate in candidates],
        diagnostics=diagnostics,
    )
    selected = select_best_candidate(candidates, page_url=source_url)
    if not selected:
        message = _selection_failure_message(candidates, diagnostics, probe_error)
        emit(AUTO_STAGE_FAILED, message, level="error", progress=100, candidates=[c.to_dict(include_sensitive_headers=True) for c in candidates], diagnostics=diagnostics)
        raise DownloadError(message)

    if not auto_select and selected.score < 80:
        message = "AutoSelect dang tat hoac candidate chua du manh; hay chon trong Media Inspector."
        emit(AUTO_STAGE_FAILED, message, level="warning", progress=100, candidates=[c.to_dict(include_sensitive_headers=True) for c in candidates], diagnostics=diagnostics)
        raise DownloadError(message)

    emit(
        AUTO_STAGE_DOWNLOADING,
        f"Chon {selected.kind} tu {selected.host} (score {selected.score}), bat dau tai.",
        level="success",
        progress=72,
        candidates=[candidate.to_dict(include_sensitive_headers=True) for candidate in candidates],
        diagnostics=diagnostics,
        selectedCandidate=selected.to_dict(include_sensitive_headers=True),
    )
    try:
        download_media_candidate(
            selected,
            source_url,
            output_folder,
            cookie_file,
            optimize_mode,
            advanced_options,
            status_callback=status_callback,
            downloader=downloader,
        )
    except Exception as exc:
        error = _clean_error(str(exc))
        if retry_allowed and is_retryable_media_error(error):
            emit(
                AUTO_STAGE_RETRYING,
                f"Media link co the het token ({error}). Bat lai media mot lan nua.",
                level="warning",
                progress=82,
                candidates=[candidate.to_dict(include_sensitive_headers=True) for candidate in candidates],
                diagnostics=diagnostics,
            )
            return _sniff_select_download(
                source_url,
                output_folder,
                cookie_file,
                optimize_mode,
                advanced_options,
                browser_name,
                browser_path,
                browser_profile,
                browser_mode,
                sniff_timeout_seconds,
                auto_select=True,
                emit=emit,
                status_callback=status_callback,
                downloader=downloader,
                sniffer=sniffer,
                retry_allowed=False,
                probe_error=probe_error,
            )
        emit(AUTO_STAGE_FAILED, f"Tai media candidate loi: {error}", level="error", progress=100)
        raise

    emit(AUTO_STAGE_DONE, "Tai xong bang media sniffer.", level="success", progress=100, selectedCandidate=selected.to_dict(include_sensitive_headers=True))
    return {
        "ok": True,
        "strategy": "browser-sniff",
        "sourceUrl": source_url,
        "candidate": selected.to_dict(include_sensitive_headers=True),
        "diagnostics": diagnostics,
    }


def _advanced_with_candidate_headers(
    advanced_options: dict[str, Any],
    candidate: MediaCandidate,
    source_url: str,
) -> dict[str, Any]:
    advanced = dict(advanced_options or {})
    headers = build_download_headers(candidate, page_url=source_url)
    existing_headers = advanced.get("httpHeaders") or advanced.get("mediaHeaders") or {}
    if isinstance(existing_headers, dict):
        merged_headers = {**existing_headers, **headers}
    else:
        merged_headers = headers
    advanced["httpHeaders"] = merged_headers
    advanced["referer"] = source_url
    if "User-Agent" in headers:
        advanced["userAgent"] = headers["User-Agent"]
    return advanced


def _candidate_from_dict(candidate: dict[str, Any]) -> MediaCandidate:
    ranked = rank_media_candidates([candidate], page_url=str(candidate.get("pageUrl") or ""))
    return ranked[0]


def _make_emitter(callback: AutoEventCallback | None) -> Callable[..., None]:
    def emit(stage: str, message: str, level: str = "info", progress: int | None = None, **extra: Any) -> None:
        if not callback:
            return
        payload = {"stage": stage, "message": message, "level": level}
        if progress is not None:
            payload["progress"] = max(0, min(100, int(progress)))
        payload.update(extra)
        callback(payload)

    return emit


def _should_fallback_to_sniff(message: str) -> bool:
    if is_unsupported_error(message) or is_retryable_media_error(message):
        return True
    text = str(message or "").lower()
    return "extractor" in text or "unable to extract" in text


def _selection_failure_message(candidates: list[MediaCandidate], diagnostics: list[dict[str, Any]], probe_error: str) -> str:
    has_license_diagnostic = any(item.get("type") == "drm_license" for item in diagnostics)
    if any(candidate.drm_signals for candidate in candidates):
        return "Phat hien dau hieu DRM/license. Tool khong bypass DRM/Widevine/PlayReady."
    if any(item.get("type") == "blob_media" for item in diagnostics):
        return "Trang chi lo blob media, chua bat duoc manifest/direct URL tai duoc. Hay bam play roi Inspect lai."
    if not candidates and has_license_diagnostic:
        return (
            "Chua bat duoc media candidate. Co request nghi la license/DRM nhung chua du ket luan; "
            "hay bam play trong cua so browser, doi server/player neu co, roi thu Inspect/Tai tu dong lai."
        )
    if has_license_diagnostic:
        return (
            "Co request nghi la license/DRM nhung chua xac nhan DRM tren media candidate. "
            "Hay mo Media Inspector de chon candidate khac hoac thu lai sau khi bam play."
        )
    if probe_error:
        return f"Khong chon duoc media candidate sau khi yt-dlp loi: {probe_error}"
    return "Khong tim thay media candidate du manh de tai tu trang nay."


def _diagnostic_from_error(message: str) -> dict[str, Any]:
    error_type = "unsupported" if is_unsupported_error(message) else "probe_error"
    if is_retryable_media_error(message):
        error_type = "auth_or_token"
    return {"type": error_type, "message": message, "url": ""}


def _clean_error(message: str) -> str:
    clean = str(message or "").replace("\r", " ").replace("\n", " ").strip()
    clean = re.sub(r"\s+", " ", clean)
    while clean.lower().startswith("error:"):
        clean = clean[6:].strip()
    return clean


def _clean_url(url: str) -> str:
    return str(url or "").strip()
