"""yt-dlp plugin registration and JavaScript runtime resolution.

Two separate concerns live here, both about giving yt-dlp what it needs to
extract modern YouTube:

**JavaScript runtime.** Current yt-dlp deprecates YouTube extraction without an
external JS runtime (EJS) and only enables ``deno`` by default. On a machine with
Node but no Deno, extraction still works but logs a deprecation warning and warns
that "some formats may be missing". Detecting what is actually installed and
passing it through ``js_runtimes`` is the fix, and it is what unblocks YouTube in
practice.

**PO Token provider.** Widely described as mandatory for YouTube. Measured on
this codebase it is *not* needed for ordinary public videos - a probe returns the
full format list without any provider. Worse, installing
``bgutil-ytdlp-pot-provider`` without also running its Node server makes things
worse: the plugin loads, fails to reach ``127.0.0.1:4416`` and prints a warning on
every single request. So the provider is treated as an on-demand escalation,
wired to ``FailureKind.PO_TOKEN_REQUIRED``, not as a default dependency.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

__all__ = [
    "BUNDLED_PLUGIN_DIRNAME",
    "bundled_plugin_dir",
    "detect_js_runtimes",
    "pot_provider_installed",
    "register_plugin_dirs",
    "resolve_js_runtimes",
    "youtube_extractor_args",
]

BUNDLED_PLUGIN_DIRNAME = "plugins"

# Preference order. Deno first because it is what yt-dlp enables by default and
# is the best supported; node is the common case on Windows dev machines.
JS_RUNTIME_CANDIDATES: tuple[str, ...] = ("deno", "node", "bun", "qjs")

# Default base URL of the bgutil HTTP provider, as declared by the plugin itself
# (``BgUtilHTTPPTP.DEFAULT_BASE_URL``). Kept here so the ladder can tell the
# provider where to look without importing the plugin.
POT_PROVIDER_BASE_URL = "http://127.0.0.1:4416"


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def bundled_plugin_dir() -> Path:
    """Directory holding plugins shipped with the tool.

    Under PyInstaller the payload is unpacked to ``sys._MEIPASS``; elsewhere it
    sits next to the source tree. Mirrors what run.py/main.py already do.
    """
    meipass = getattr(sys, "_MEIPASS", "")
    base = Path(meipass) if meipass else _project_root()
    return base / BUNDLED_PLUGIN_DIRNAME


def register_plugin_dirs() -> list[str]:
    """Point yt-dlp at the bundled plugin directory.

    ``yt_dlp.plugins.plugin_dirs`` is an ``Indirect`` holder whose ``.value`` is
    a list, so assigning to it is the supported way to add a search path from
    Python. That is cleaner than mutating ``sys.path`` and keeps yt-dlp's own
    'default' discovery intact.

    Returns the resulting search list, or an empty list when unavailable.
    """
    directory = bundled_plugin_dir()
    if not directory.is_dir():
        return []

    try:
        from yt_dlp.plugins import plugin_dirs
    except ImportError:
        return []

    current = list(getattr(plugin_dirs, "value", ["default"]) or ["default"])
    target = str(directory)
    if target not in current:
        current.append(target)
        plugin_dirs.value = current
    return current


def detect_js_runtimes() -> list[str]:
    """Names of JavaScript runtimes present on PATH, in preference order."""
    return [name for name in JS_RUNTIME_CANDIDATES if shutil.which(name)]


def resolve_js_runtimes() -> dict[str, dict[str, Any]]:
    """Build the ``js_runtimes`` yt-dlp parameter from what is installed.

    yt-dlp defaults to ``{'deno': {}}``. On a machine with only Node that means
    no usable runtime, which downgrades YouTube extraction. Returning every
    detected runtime lets yt-dlp pick one that actually exists.

    An empty dict is returned when nothing is found; the caller should then leave
    the parameter unset so yt-dlp keeps its own default and warning.
    """
    return {name: {} for name in detect_js_runtimes()}


def pot_provider_installed() -> bool:
    """Whether the bgutil PO token provider plugin is importable.

    The distribution installs into the ``yt_dlp_plugins`` namespace rather than a
    top-level module, so the import name is not the distribution name - checking
    for ``bgutil_ytdlp_pot_provider`` would always report False.
    """
    import importlib.util

    try:
        return importlib.util.find_spec("yt_dlp_plugins.extractor.getpot_bgutil_http") is not None
    except (ImportError, ValueError):
        return False


def youtube_extractor_args(
    *,
    force_ump: bool = False,
    pot_base_url: str = "",
) -> dict[str, dict[str, list[str]]]:
    """Extractor args for the YouTube escalation rungs.

    ``force_ump`` is the response to a SABR-only failure: it asks yt-dlp for the
    UMP/SABR format set instead of the classic progressive URLs.
    """
    args: dict[str, dict[str, list[str]]] = {}
    if force_ump:
        args["youtube"] = {"formats": ["ump"]}
    if pot_base_url:
        args["youtubepot-bgutilhttp"] = {"base_url": [pot_base_url]}
    return args
