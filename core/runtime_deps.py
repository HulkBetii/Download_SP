"""Runtime dependency detection, self-installation and binary provisioning.

The tool aims to work without the user configuring anything, which means it has
to notice what is missing and fix it on its own. Two very different mechanisms
are involved:

* Python packages are installed with ``pip`` into the interpreter that is
  currently running. This is skipped entirely for frozen (PyInstaller) builds
  because there is no writable site-packages there.
* Standalone binaries (aria2c, N_m3u8DL-RE) are downloaded into a private
  directory. These are never placed on the global PATH and are always verified
  against a SHA-256 digest before being unpacked - see ``ensure_binary``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterable

ProgressCallback = Callable[[str, str], None]

PIP_TIMEOUT_SECONDS = 300
DOWNLOAD_TIMEOUT_SECONDS = 120
YT_DLP_MAX_AGE_DAYS = 14

# Packages the tool cannot do its job without. ``import_name`` differs from the
# distribution name often enough that both have to be recorded.
CORE_PACKAGES: tuple[tuple[str, str], ...] = (
    ("curl_cffi", "curl_cffi>=0.7.0"),
    ("brotli", "brotli>=1.1.0"),
    ("requests", "requests>=2.32.0"),
)

# yt-dlp plugins install into the ``yt_dlp_plugins`` namespace, so the import
# name is a submodule path and never matches the distribution name.
PLUGIN_PACKAGES: tuple[tuple[str, str], ...] = (
    ("yt_dlp_plugins.extractor.ChromeCookieUnlock", "yt-dlp-ChromeCookieUnlock"),
)

# Installed on demand only. Measured behaviour: YouTube extracts fine without a
# PO token provider, and installing this one *without* also running its Node
# server makes every request warn about an unreachable 127.0.0.1:4416. It is
# therefore an escalation for FailureKind.PO_TOKEN_REQUIRED, not a default.
POT_PROVIDER_PACKAGE = ("yt_dlp_plugins.extractor.getpot_bgutil_http", "bgutil-ytdlp-pot-provider")


class DependencyError(RuntimeError):
    pass


@dataclass(slots=True)
class BinarySpec:
    """A downloadable binary pinned to one exact archive.

    ``sha256`` is the digest of the *archive*, not of the extracted file. An
    empty value means "not pinned yet"; see ``ensure_binary`` for how that is
    handled - it is deliberately not treated as "skip verification".
    """

    name: str
    url: str
    sha256: str
    member_suffix: str
    executable_name: str


# Only these exact archives may be fetched. Adding an entry is a deliberate act:
# the URL and the digest have to be filled in together.
#
# The digests below were computed by downloading each archive over HTTPS from the
# release URL recorded here; the sizes matched what the GitHub API reports for
# those assets. Neither project publishes a checksum file, so TLS to github.com
# is the only trust anchor at first fetch. What pinning buys is everything after
# that: a substituted or tampered archive under the same URL is refused.
BINARY_MANIFEST: dict[str, BinarySpec] = {
    "aria2c": BinarySpec(
        name="aria2c",
        url="https://github.com/aria2/aria2/releases/download/release-1.37.0/aria2-1.37.0-win-64bit-build1.zip",
        sha256="67d015301eef0b612191212d564c5bb0a14b5b9c4796b76454276a4d28d9b288",
        member_suffix="aria2c.exe",
        executable_name="aria2c.exe",
    ),
    "N_m3u8DL-RE": BinarySpec(
        name="N_m3u8DL-RE",
        url="https://github.com/nilaoda/N_m3u8DL-RE/releases/download/v0.6.0-beta/N_m3u8DL-RE_v0.6.0-beta_win-x64_20260629.zip",
        sha256="3825fd42ee502f98a9378f6fdddb2f7822709f521806214f466db6935c950f1a",
        member_suffix="N_m3u8DL-RE.exe",
        executable_name="N_m3u8DL-RE.exe",
    ),
}


@dataclass(slots=True)
class DependencyReport:
    """Snapshot of what is installed, surfaced to the UI."""

    python_packages: dict[str, bool] = field(default_factory=dict)
    plugins: dict[str, bool] = field(default_factory=dict)
    binaries: dict[str, str] = field(default_factory=dict)
    js_runtimes: list[str] = field(default_factory=list)
    ffmpeg: bool = False
    node: bool = False
    yt_dlp_version: str = ""
    yt_dlp_stale: bool = False
    frozen: bool = False
    actions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pythonPackages": dict(self.python_packages),
            "plugins": dict(self.plugins),
            "binaries": dict(self.binaries),
            "jsRuntimes": list(self.js_runtimes),
            "ffmpeg": self.ffmpeg,
            "node": self.node,
            "ytDlpVersion": self.yt_dlp_version,
            "ytDlpStale": self.yt_dlp_stale,
            "frozen": self.frozen,
            "actions": list(self.actions),
            "warnings": list(self.warnings),
            "ready": self.is_ready,
        }

    @property
    def is_ready(self) -> bool:
        """Core packages present and ffmpeg available. Plugins are optional."""
        return all(self.python_packages.values()) and self.ffmpeg


def app_data_dir() -> Path:
    """Private directory for tool-managed state. Never a system location."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "VideoDownloaderTool"


def bin_dir() -> Path:
    return app_data_dir() / "bin"


def browser_profile_dir() -> Path:
    """Persistent sniffer profile so a login survives across runs."""
    return app_data_dir() / "browser-profile"


def _lockfile_path() -> Path:
    return app_data_dir() / "binaries.lock.json"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _module_available(import_name: str) -> bool:
    """Check importability without importing - importing curl_cffi is slow."""
    import importlib.util

    try:
        return importlib.util.find_spec(import_name) is not None
    except (ImportError, ValueError):
        # find_spec raises for some half-installed packages rather than
        # returning None, which would otherwise crash the whole probe.
        return False


def _emit(progress: ProgressCallback | None, message: str, level: str = "info") -> None:
    if progress:
        progress(message, level)


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

def detect() -> DependencyReport:
    """Probe the environment. Cheap enough to call on every UI refresh."""
    report = DependencyReport(frozen=is_frozen())

    for import_name, _requirement in CORE_PACKAGES:
        report.python_packages[import_name] = _module_available(import_name)

    for import_name, _requirement in PLUGIN_PACKAGES:
        report.plugins[import_name] = _module_available(import_name)

    for name in BINARY_MANIFEST:
        report.binaries[name] = resolve_binary(name)

    report.ffmpeg = bool(shutil.which("ffmpeg"))
    report.node = bool(shutil.which("node"))

    from .plugins import detect_js_runtimes

    report.js_runtimes = detect_js_runtimes()

    version, stale = _yt_dlp_freshness()
    report.yt_dlp_version = version
    report.yt_dlp_stale = stale

    if not report.ffmpeg:
        report.warnings.append(
            "Khong tim thay ffmpeg. Video va audio se khong ghep duoc, chat luong toi da bi gioi han."
        )
    if report.frozen:
        report.warnings.append(
            "Ban dong goi (.exe) khong tu cai duoc goi Python. Hay dung ban chay tu source neu thieu dependency."
        )
    return report


def _yt_dlp_freshness() -> tuple[str, bool]:
    """yt-dlp versions are date-stamped (``2026.07.04``), so age is readable."""
    try:
        from yt_dlp.version import __version__ as version
    except Exception:
        return "unknown", False

    parts = str(version).split(".")
    if len(parts) < 3:
        return version, False
    try:
        released = date(int(parts[0]), int(parts[1]), int(parts[2]))
    except (TypeError, ValueError):
        return version, False

    age_days = (datetime.now().date() - released).days
    return version, age_days > YT_DLP_MAX_AGE_DAYS


# --------------------------------------------------------------------------
# Python package installation
# --------------------------------------------------------------------------

def _run_pip(args: list[str], progress: ProgressCallback | None) -> bool:
    if is_frozen():
        _emit(progress, "Ban dong goi khong ho tro tu cai goi Python.", "warning")
        return False

    command = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *args]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=PIP_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _emit(progress, f"pip qua thoi gian cho khi cai {' '.join(args)}.", "error")
        return False
    except OSError as exc:
        _emit(progress, f"Khong chay duoc pip: {exc}", "error")
        return False

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        reason = detail[-1] if detail else f"exit code {completed.returncode}"
        _emit(progress, f"Cai that bai ({' '.join(args)}): {reason}", "error")
        return False
    return True


def ensure_python_packages(
    packages: Iterable[tuple[str, str]] = CORE_PACKAGES,
    progress: ProgressCallback | None = None,
) -> list[str]:
    """Install any missing package. Returns the distributions actually installed."""
    installed: list[str] = []
    for import_name, requirement in packages:
        if _module_available(import_name):
            continue
        _emit(progress, f"Dang cai {requirement}...", "info")
        if _run_pip([requirement], progress):
            installed.append(requirement)
            _emit(progress, f"Da cai {requirement}.", "success")
    return installed


def ensure_ytdlp_fresh(progress: ProgressCallback | None = None) -> bool:
    """Upgrade yt-dlp when the bundled build is older than the age threshold."""
    version, stale = _yt_dlp_freshness()
    if not stale:
        return False
    _emit(progress, f"yt-dlp {version} da cu, dang cap nhat...", "info")
    if not _run_pip(["--upgrade", "yt-dlp"], progress):
        return False
    _emit(progress, "Da cap nhat yt-dlp.", "success")
    return True


# --------------------------------------------------------------------------
# Binary provisioning
# --------------------------------------------------------------------------

def resolve_binary(name: str) -> str:
    """Find a usable binary: system PATH first, then our private directory."""
    spec = BINARY_MANIFEST.get(name)
    executable = spec.executable_name if spec else name

    on_path = shutil.which(name) or shutil.which(executable)
    if on_path:
        return on_path

    local = bin_dir() / executable
    return str(local) if local.is_file() else ""


def _load_pins() -> dict[str, str]:
    try:
        return json.loads(_lockfile_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_pin(name: str, digest: str) -> None:
    pins = _load_pins()
    pins[name] = digest
    path = _lockfile_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(pins, indent=2), encoding="utf-8")
    except OSError:
        # A missing pin only costs us the trust-on-first-use guarantee on the
        # next run; it must not abort an otherwise working download.
        pass


def _download(url: str, destination: Path, progress: ProgressCallback | None) -> None:
    if not url.lower().startswith("https://"):
        raise DependencyError(f"Chi cho phep tai qua HTTPS: {url}")

    request = urllib.request.Request(url, headers={"User-Agent": "VideoDownloaderTool"})
    with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
        # A redirect off the pinned host would defeat the point of pinning.
        final_url = response.geturl()
        if not final_url.lower().startswith("https://"):
            raise DependencyError(f"Bi chuyen huong sang giao thuc khong an toan: {final_url}")
        with destination.open("wb") as handle:
            shutil.copyfileobj(response, handle)


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_digest(name: str, spec: BinarySpec, archive: Path, progress: ProgressCallback | None) -> None:
    """Reject anything whose digest does not match what we expect.

    Precedence: the manifest digest wins. If the manifest has none, fall back to
    a locally recorded pin (trust on first use). Only a first-ever download with
    no pin on record is accepted unverified, and the digest is reported so it can
    be checked against the publisher and committed to the manifest.
    """
    actual = _sha256_of(archive)
    expected = (spec.sha256 or "").strip().lower()

    if expected:
        if actual != expected:
            raise DependencyError(
                f"SHA-256 cua {name} khong khop. Cho doi {expected}, nhan duoc {actual}. Da huy file tai ve."
            )
        return

    pinned = _load_pins().get(name, "").strip().lower()
    if pinned:
        if actual != pinned:
            raise DependencyError(
                f"SHA-256 cua {name} khac lan tai truoc ({pinned} -> {actual}). "
                "Co the ban phat hanh da thay doi hoac file bi can thiep. Da huy file tai ve."
            )
        return

    _save_pin(name, actual)
    _emit(
        progress,
        f"{name} chua duoc ghim SHA-256 trong manifest. Da ghi nhan {actual} cho lan tai dau tien; "
        "hay doi chieu voi trang phat hanh chinh thuc roi dien vao BINARY_MANIFEST.",
        "warning",
    )


def _extract_member(archive: Path, spec: BinarySpec, target_dir: Path) -> Path:
    with zipfile.ZipFile(archive) as bundle:
        member = next(
            (item for item in bundle.namelist() if item.endswith(spec.member_suffix)),
            None,
        )
        if member is None:
            raise DependencyError(f"Khong tim thay {spec.member_suffix} trong archive cua {spec.name}.")
        target_dir.mkdir(parents=True, exist_ok=True)
        destination = target_dir / spec.executable_name
        with bundle.open(member) as source, destination.open("wb") as handle:
            shutil.copyfileobj(source, handle)
    destination.chmod(destination.stat().st_mode | 0o755)
    return destination


def ensure_binary(name: str, progress: ProgressCallback | None = None) -> str:
    """Return a path to ``name``, downloading it if needed. Empty string on failure.

    Download failures are never fatal: every binary handled here is an optional
    accelerator, so the caller falls back to the built-in downloader.
    """
    existing = resolve_binary(name)
    if existing:
        return existing

    spec = BINARY_MANIFEST.get(name)
    if spec is None:
        return ""

    _emit(progress, f"Dang tai {name}...", "info")
    with tempfile.TemporaryDirectory(prefix="vdt_binary_") as workspace:
        archive = Path(workspace) / f"{name}.zip"
        try:
            _download(spec.url, archive, progress)
            _verify_digest(name, spec, archive, progress)
            installed = _extract_member(archive, spec, bin_dir())
        except (DependencyError, OSError, zipfile.BadZipFile) as exc:
            _emit(progress, f"Khong cai duoc {name}: {exc}", "warning")
            return ""

    _emit(progress, f"Da cai {name}.", "success")
    return str(installed)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def ensure_all(
    progress: ProgressCallback | None = None,
    include_plugins: bool = True,
    include_binaries: bool = True,
) -> DependencyReport:
    """Bring the environment up to spec, then report what it looks like."""
    actions: list[str] = []

    actions.extend(ensure_python_packages(CORE_PACKAGES, progress))
    if include_plugins:
        actions.extend(ensure_python_packages(PLUGIN_PACKAGES, progress))
    if ensure_ytdlp_fresh(progress):
        actions.append("yt-dlp upgrade")
    if include_binaries:
        for name in BINARY_MANIFEST:
            if ensure_binary(name, progress):
                actions.append(name)

    invalidate_capability_cache()
    report = detect()
    report.actions = actions
    return report


def invalidate_capability_cache() -> None:
    """Drop cached capability probes after the environment has changed."""
    try:
        from .downloader import check_ffmpeg_available, get_runtime_capabilities
    except ImportError:
        return
    for cached in (get_runtime_capabilities, check_ffmpeg_available):
        clear = getattr(cached, "cache_clear", None)
        if clear:
            clear()


__all__ = [
    "BINARY_MANIFEST",
    "BinarySpec",
    "DependencyError",
    "DependencyReport",
    "app_data_dir",
    "bin_dir",
    "browser_profile_dir",
    "detect",
    "ensure_all",
    "ensure_binary",
    "ensure_python_packages",
    "ensure_ytdlp_fresh",
    "invalidate_capability_cache",
    "is_frozen",
    "resolve_binary",
]
