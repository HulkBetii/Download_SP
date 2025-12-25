from __future__ import annotations

from pathlib import Path
from typing import Any

from .downloader import download_video


def analyze_video(
    url: str,
    cookie_file: str | None = None,
    status_callback=None,
    optimize_mode: str = "balanced",
    advanced_options: dict[str, Any] | None = None,
    **extra_options: Any,
) -> dict[str, Any]:
    """Probe a URL with the same yt-dlp option pipeline without downloading."""
    return download_video(
        url,
        str(Path.home() / "Downloads"),
        cookie_file=cookie_file,
        status_callback=status_callback,
        optimize_mode=optimize_mode,
        advanced_options=advanced_options,
        dry_run=True,
        **extra_options,
    )
