from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

MEDIA_EXTENSIONS = (
    ".m3u8",
    ".mpd",
    ".mp4",
    ".webm",
    ".m4v",
    ".mov",
    ".mkv",
    ".mp3",
    ".m4a",
    ".aac",
)
MEDIA_MIME_HINTS = (
    "video/",
    "audio/",
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "application/dash+xml",
)
# Unambiguous DRM systems - these names appear nowhere innocent.
DRM_SYSTEM_RE = re.compile(r"widevine|playready|fairplay|clearkey", re.IGNORECASE)

# License-acquisition endpoints. Anchored on separators so "license" only counts
# as its own path segment or filename part.
DRM_LICENSE_ENDPOINT_RE = re.compile(
    r"(?:^|[/._-])"
    r"(?:licen[cs]e(?:server|proxy|request)?|getlicen[cs]e|acquirelicen[cs]e|drm)"
    r"(?:[/._?&=-]|$)",
    re.IGNORECASE,
)

# Ordinary licensing links that are not DRM at all. Free-to-download video sites
# link to these constantly, and treating them as DRM makes the tool refuse
# perfectly downloadable content.
BENIGN_LICENSE_RE = re.compile(
    r"creativecommons\.org"
    r"|/licen[cs]es?/(?:by|cc|mit|apache|gpl|lgpl|bsd|mpl)"
    r"|licen[cs]e\.(?:txt|md|html?|php)$",
    re.IGNORECASE,
)
NOISY_URL_TOKENS = (
    "thumbnail",
    "thumb",
    "poster",
    "sprite",
    "preview",
    "avatar",
    "logo",
    "advert",
    "/ads/",
    "tracking",
    "analytics",
)
SENSITIVE_HEADER_NAMES = {"cookie", "authorization", "proxy-authorization", "x-csrf-token", "x-xsrf-token"}
MEDIA_TARGET_TYPES = {"page", "iframe", "worker", "shared_worker", "service_worker", "webview"}
PLAYBACK_TARGET_TYPES = {"page", "iframe", "webview"}
PLAYBACK_ACTIVATION_SCRIPT = r"""
(() => {
  const actions = [];
  const isVisible = (element) => {
    if (!element || !element.getBoundingClientRect) return false;
    const rect = element.getBoundingClientRect();
    const style = window.getComputedStyle(element);
    return rect.width > 8 && rect.height > 8 && style.visibility !== "hidden" && style.display !== "none";
  };
  document.querySelectorAll("video").forEach((video) => {
    try {
      video.muted = true;
      video.autoplay = true;
      video.playsInline = true;
      const result = video.play();
      if (result && result.catch) result.catch(() => {});
      actions.push("video.play");
    } catch (error) {}
  });
  const keywords = /play|watch|start|continue|resume|xem|phat|bat dau/i;
  const selectors = [
    "button",
    "[role='button']",
    ".play",
    ".play-button",
    ".vjs-big-play-button",
    ".jw-display-icon-container",
    ".jw-icon-playback",
    ".plyr__control--overlaid",
    "[class*='play']",
    "[id*='play']"
  ];
  const nodes = Array.from(document.querySelectorAll(selectors.join(",")));
  for (const node of nodes) {
    if (actions.length >= 6) break;
    if (!isVisible(node)) continue;
    const label = `${node.getAttribute("aria-label") || ""} ${node.getAttribute("title") || ""} ${node.id || ""} ${node.className || ""} ${node.textContent || ""}`;
    if (!keywords.test(label)) continue;
    try {
      node.click();
      actions.push("click.play");
    } catch (error) {}
  }
  return actions;
})()
"""
PLAYER_POINT_SCRIPT = r"""
(() => {
  const selectors = "video, iframe, [class*='player'], [id*='player'], [class*='video'], [id*='video']";
  const nodes = Array.from(document.querySelectorAll(selectors));
  const visible = nodes
    .map((node) => {
      const rect = node.getBoundingClientRect ? node.getBoundingClientRect() : null;
      if (!rect || rect.width < 120 || rect.height < 80) return null;
      const style = window.getComputedStyle(node);
      if (style.visibility === "hidden" || style.display === "none") return null;
      return { x: Math.round(rect.left + rect.width / 2), y: Math.round(rect.top + rect.height / 2), area: rect.width * rect.height };
    })
    .filter(Boolean)
    .sort((a, b) => b.area - a.area);
  return visible[0] || null;
})()
"""


@dataclass(slots=True)
class MediaCandidate:
    url: str
    kind: str
    mime_type: str = ""
    source: str = "network"
    status: int | None = None
    request_headers: dict[str, str] = field(default_factory=dict)
    response_headers: dict[str, str] = field(default_factory=dict)
    resource_type: str = ""
    content_length: int | None = None
    initiator: str = ""
    page_url: str = ""
    manifest_variant_count: int = 0
    drm_signals: list[str] = field(default_factory=list)
    score: int = 0
    reasons: list[str] = field(default_factory=list)

    @property
    def candidate_id(self) -> str:
        digest = hashlib.sha1(self.url.encode("utf-8", "ignore")).hexdigest()
        return digest[:14]

    @property
    def host(self) -> str:
        return urllib.parse.urlparse(self.url).netloc

    def to_dict(self, include_sensitive_headers: bool = False) -> dict[str, Any]:
        request_headers = _public_headers(self.request_headers, include_sensitive_headers)
        response_headers = _public_headers(self.response_headers, include_sensitive_headers)
        return {
            "id": self.candidate_id,
            "url": self.url,
            "pageUrl": self.page_url,
            "host": self.host,
            "kind": self.kind,
            "mimeType": self.mime_type,
            "source": self.source,
            "status": self.status,
            "resourceType": self.resource_type,
            "contentLength": self.content_length,
            "sizeText": _format_bytes(self.content_length),
            "initiator": self.initiator,
            "manifestVariantCount": self.manifest_variant_count,
            "drmSignals": list(self.drm_signals),
            "score": self.score,
            "reasons": list(self.reasons),
            "requestHeaders": request_headers,
            "responseHeaders": response_headers,
            "requestHeaderNames": sorted(self.request_headers),
            "responseHeaderNames": sorted(self.response_headers),
            "hasSensitiveHeaders": _has_sensitive_headers(self.request_headers),
        }


class MediaSnifferError(RuntimeError):
    pass


def find_browser_executable(browser_name: str = "coccoc", browser_path: str = "") -> str:
    if browser_path:
        expanded = os.path.expandvars(os.path.expanduser(browser_path))
        if os.path.isfile(expanded):
            return expanded

    name = (browser_name or "").strip().lower().replace("-", "").replace("_", "")
    candidates = _browser_candidates(name)
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate

    for executable_name in _browser_path_names(name):
        found = shutil.which(executable_name)
        if found:
            return found

    raise MediaSnifferError(f"Khong tim thay browser executable cho: {browser_name}")


def get_browser_capabilities(browser_names: Iterable[str] | None = None) -> dict[str, dict[str, Any]]:
    names = list(browser_names or ("coccoc", "chrome", "edge", "brave", "chromium"))
    capabilities: dict[str, dict[str, Any]] = {}
    for name in names:
        try:
            path = find_browser_executable(name)
        except MediaSnifferError:
            capabilities[name] = {"available": False, "path": "", "mode": "unavailable"}
        else:
            capabilities[name] = {"available": True, "path": path, "mode": "cdp-visible"}
    return capabilities


def sniff_media_urls(
    page_url: str,
    browser_name: str = "coccoc",
    browser_path: str = "",
    profile_path: str = "",
    timeout_seconds: int = 45,
    browser_mode: str = "visible",
    include_sensitive_headers: bool = False,
) -> dict[str, Any]:
    browser_exe = find_browser_executable(browser_name, browser_path)
    timeout_seconds = max(8, min(120, int(timeout_seconds or 45)))
    port = _find_free_port()
    temp_profile = None
    user_data_dir = _usable_profile_path(profile_path)
    if not user_data_dir:
        user_data_dir = _persistent_profile_dir(browser_name)
    if not user_data_dir:
        temp_profile = tempfile.TemporaryDirectory(prefix="video_downloader_sniff_")
        user_data_dir = temp_profile.name

    process = None
    ws = None
    started = time.time()
    try:
        process = _launch_browser(browser_exe, port, user_data_dir, browser_mode=browser_mode)
        target = _wait_for_page_target(port, timeout=10)
        ws = _CdpWebSocket(target["webSocketDebuggerUrl"])
        ws.connect()
        cdp = _CdpSession(ws)
        collector = _SniffCollector(page_url)
        attached_targets: dict[str, str] = {}
        _enable_sniff_domains(cdp, collector=collector)
        _enable_target_discovery(cdp, collector=collector)
        cdp.call("Page.navigate", {"url": page_url})

        deadline = time.time() + timeout_seconds
        activation_count = 0
        next_activation_at = started + 4
        while time.time() < deadline:
            event = cdp.recv(timeout=0.75)
            if event:
                _handle_attached_target_event(cdp, event, attached_targets, collector)
                collector.collect(event)
            now = time.time()
            if activation_count < 4 and now >= next_activation_at and not collector.candidates:
                _activate_known_targets(cdp, attached_targets)
                activation_count += 1
                next_activation_at = now + 6
            if len(collector.candidates) >= 16 and time.time() - started >= 8:
                break

        candidates = list(collector.candidates.values())
        _inspect_manifest_candidates(candidates)
        ordered = rank_media_candidates(candidates, page_url=page_url)
        selected = select_best_candidate(ordered, page_url=page_url)
        return {
            "ok": True,
            "sourceUrl": page_url,
            "browserName": browser_name,
            "browserPath": browser_exe,
            "browserMode": browser_mode or "visible",
            "elapsedSeconds": round(time.time() - started, 1),
            "mediaUrls": [candidate.to_dict(include_sensitive_headers) for candidate in ordered[:20]],
            "selectedCandidateId": selected.candidate_id if selected else "",
            "diagnostics": collector.diagnostics,
        }
    except MediaSnifferError:
        raise
    except Exception as exc:
        raise MediaSnifferError(f"Khong bat duoc media tu browser: {exc}") from exc
    finally:
        if ws:
            ws.close()
        if process:
            _stop_process(process)
        if temp_profile:
            temp_profile.cleanup()


def extract_media_candidates_from_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    collector = _SniffCollector()
    for event in events:
        collector.collect(event)
    ordered = rank_media_candidates(collector.candidates.values())
    return [candidate.to_dict() for candidate in ordered]


def rank_media_candidates(candidates: Iterable[MediaCandidate | dict[str, Any]], page_url: str = "") -> list[MediaCandidate]:
    deduped = _dedupe_candidates(_coerce_candidate(candidate) for candidate in candidates)
    has_video_candidate = any(candidate.kind in {"hls", "dash", "video"} for candidate in deduped)
    for candidate in deduped:
        score, reasons = _score_candidate(candidate, page_url, has_video_candidate)
        candidate.score = score
        candidate.reasons = reasons
    return sorted(deduped, key=lambda item: (-item.score, item.kind, item.url))


def select_best_candidate(
    candidates: Iterable[MediaCandidate | dict[str, Any]],
    page_url: str = "",
    min_score: int = 50,
) -> MediaCandidate | None:
    ranked = rank_media_candidates(candidates, page_url=page_url)
    for candidate in ranked:
        if candidate.score >= min_score and not candidate.drm_signals:
            return candidate
    return None


def build_download_headers(candidate: MediaCandidate | dict[str, Any], page_url: str = "") -> dict[str, str]:
    item = _coerce_candidate(candidate)
    headers: dict[str, str] = {}
    for name, value in item.request_headers.items():
        normalized_name = _canonical_header_name(name)
        if _should_forward_header(normalized_name):
            headers[normalized_name] = value

    if page_url:
        headers.setdefault("Referer", page_url)
        origin = _origin_from_url(page_url)
        if origin:
            headers.setdefault("Origin", origin)
    if "User-Agent" not in headers:
        headers["User-Agent"] = _default_user_agent()
    return headers


class _SniffCollector:
    def __init__(self, page_url: str = ""):
        self.page_url = page_url
        self.candidates: dict[str, MediaCandidate] = {}
        self.request_urls: dict[str, str] = {}
        self.pending_request_headers: dict[str, dict[str, str]] = {}
        self.pending_response_headers: dict[str, dict[str, str]] = {}
        self.diagnostics: list[dict[str, Any]] = []
        self._diagnostic_keys: set[str] = set()

    def collect(self, event: dict[str, Any]) -> None:
        method = event.get("method")
        params = dict(event.get("params") or {})
        session_id = str(event.get("sessionId") or "")
        if session_id:
            params["_sessionId"] = session_id
        if method == "Network.requestWillBeSent":
            self._collect_request(params)
        elif method == "Network.requestWillBeSentExtraInfo":
            self._collect_request_extra(params)
        elif method == "Network.responseReceived":
            self._collect_response(params)
        elif method == "Network.responseReceivedExtraInfo":
            self._collect_response_extra(params)
        elif method == "Network.loadingFinished":
            self._collect_loading_finished(params)

    def _collect_request(self, params: dict[str, Any]) -> None:
        request = params.get("request") or {}
        request_id = _request_key(params)
        url = str(request.get("url") or "")
        resource_type = str(params.get("type") or "")
        request_headers = _normalize_headers(request.get("headers") or {})
        request_headers.update(self.pending_request_headers.pop(request_id, {}))
        if request_id and url:
            self.request_urls[request_id] = url
        if _is_license_url(url):
            self._add_diagnostic("drm_license", "Phat hien request license/DRM.", url)
        if url.startswith("blob:") and resource_type.lower() == "media":
            self._add_diagnostic("blob_media", "Player dung blob URL; can bat request media/manifest that su.", url)
        self._maybe_add_candidate(
            url,
            request_headers=request_headers,
            resource_type=resource_type,
            source="request",
            initiator=_format_initiator(params.get("initiator")),
        )

    def _collect_request_extra(self, params: dict[str, Any]) -> None:
        request_id = _request_key(params)
        headers = _normalize_headers(params.get("headers") or {})
        if not request_id or not headers:
            return
        url = self.request_urls.get(request_id)
        if url and url in self.candidates:
            self.candidates[url].request_headers.update(headers)
        else:
            self.pending_request_headers.setdefault(request_id, {}).update(headers)

    def _collect_response(self, params: dict[str, Any]) -> None:
        response = params.get("response") or {}
        request_id = _request_key(params)
        url = str(response.get("url") or "")
        if request_id and url:
            self.request_urls[request_id] = url
        mime_type = str(response.get("mimeType") or "")
        status = response.get("status")
        response_headers = _normalize_headers(response.get("headers") or {})
        response_headers.update(self.pending_response_headers.pop(request_id, {}))
        content_length = _content_length_from_headers(response_headers)
        resource_type = str(params.get("type") or "")
        self._maybe_add_candidate(
            url,
            mime_type=mime_type,
            resource_type=resource_type,
            status=int(status) if isinstance(status, (int, float)) else None,
            response_headers=response_headers,
            content_length=content_length,
            source="response",
        )

    def _collect_response_extra(self, params: dict[str, Any]) -> None:
        request_id = _request_key(params)
        headers = _normalize_headers(params.get("headers") or {})
        status = params.get("statusCode")
        if not request_id:
            return
        url = self.request_urls.get(request_id)
        if url and url in self.candidates:
            candidate = self.candidates[url]
            candidate.response_headers.update(headers)
            if isinstance(status, (int, float)):
                candidate.status = int(status)
            length = _content_length_from_headers(headers)
            if length is not None:
                candidate.content_length = length
        elif headers:
            self.pending_response_headers.setdefault(request_id, {}).update(headers)

    def _collect_loading_finished(self, params: dict[str, Any]) -> None:
        request_id = _request_key(params)
        url = self.request_urls.get(request_id)
        if not url or url not in self.candidates:
            return
        encoded_length = params.get("encodedDataLength")
        if isinstance(encoded_length, (int, float)) and encoded_length > 0:
            candidate = self.candidates[url]
            if candidate.content_length is None or encoded_length > candidate.content_length:
                candidate.content_length = int(encoded_length)

    def _maybe_add_candidate(
        self,
        url: str,
        mime_type: str = "",
        resource_type: str = "",
        status: int | None = None,
        source: str = "network",
        request_headers: dict[str, str] | None = None,
        response_headers: dict[str, str] | None = None,
        content_length: int | None = None,
        initiator: str = "",
    ) -> None:
        if not _is_media_url(url, mime_type, resource_type):
            return
        normalized_url = url.strip()
        if normalized_url in self.candidates:
            existing = self.candidates[normalized_url]
            if mime_type and not existing.mime_type:
                existing.mime_type = mime_type
            if status is not None:
                existing.status = status
            if resource_type and not existing.resource_type:
                existing.resource_type = resource_type
            if request_headers:
                existing.request_headers.update(request_headers)
            if response_headers:
                existing.response_headers.update(response_headers)
            if content_length is not None:
                existing.content_length = max(existing.content_length or 0, content_length)
            if initiator and not existing.initiator:
                existing.initiator = initiator
            if source == "response":
                existing.source = "response"
            return
        self.candidates[normalized_url] = MediaCandidate(
            url=normalized_url,
            kind=_media_kind(normalized_url, mime_type),
            mime_type=mime_type,
            source=source,
            status=status,
            request_headers=dict(request_headers or {}),
            response_headers=dict(response_headers or {}),
            resource_type=resource_type,
            content_length=content_length,
            initiator=initiator,
            page_url=self.page_url,
        )

    def _add_diagnostic(self, kind: str, message: str, url: str = "") -> None:
        key = f"{kind}:{url}"
        if key in self._diagnostic_keys:
            return
        self._diagnostic_keys.add(key)
        self.diagnostics.append({"type": kind, "message": message, "url": url})


def _browser_candidates(name: str) -> list[str]:
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    mapping = {
        "coccoc": [
            rf"{program_files}\CocCoc\Browser\Application\browser.exe",
            rf"{program_files_x86}\CocCoc\Browser\Application\browser.exe",
            rf"{local_app_data}\CocCoc\Browser\Application\browser.exe",
        ],
        "chrome": [
            rf"{program_files}\Google\Chrome\Application\chrome.exe",
            rf"{program_files_x86}\Google\Chrome\Application\chrome.exe",
            rf"{local_app_data}\Google\Chrome\Application\chrome.exe",
        ],
        "edge": [
            rf"{program_files}\Microsoft\Edge\Application\msedge.exe",
            rf"{program_files_x86}\Microsoft\Edge\Application\msedge.exe",
            rf"{local_app_data}\Microsoft\Edge\Application\msedge.exe",
        ],
        "brave": [
            rf"{program_files}\BraveSoftware\Brave-Browser\Application\brave.exe",
            rf"{program_files_x86}\BraveSoftware\Brave-Browser\Application\brave.exe",
            rf"{local_app_data}\BraveSoftware\Brave-Browser\Application\brave.exe",
        ],
        "chromium": [
            rf"{program_files}\Chromium\Application\chrome.exe",
            rf"{program_files_x86}\Chromium\Application\chrome.exe",
            rf"{local_app_data}\Chromium\Application\chrome.exe",
        ],
    }
    return mapping.get(name, [])


def _browser_path_names(name: str) -> list[str]:
    mapping = {
        "coccoc": ["browser.exe", "coccoc.exe"],
        "chrome": ["chrome.exe", "chrome"],
        "edge": ["msedge.exe", "msedge"],
        "brave": ["brave.exe", "brave-browser", "brave"],
        "chromium": ["chromium.exe", "chromium", "chrome.exe"],
    }
    return mapping.get(name, [name])


def _usable_profile_path(profile_path: str) -> str:
    path = (profile_path or "").strip()
    if not path or path.lower() in {"default", "profile 1", "profile1"}:
        return ""
    expanded = os.path.expandvars(os.path.expanduser(path))
    return expanded if os.path.isdir(expanded) else ""


def _persistent_profile_dir(browser_name: str) -> str:
    """Tool-owned browser profile that survives between sniff runs.

    Previously every sniff got a fresh ``TemporaryDirectory``, so any login the
    user completed in the sniffer window was thrown away the moment it closed -
    sites behind a login could never be captured twice. A stable directory lets
    them sign in once.

    Kept per browser because Chromium refuses to share a profile between
    different builds. Falls back to "" (caller uses a temp dir) if the location
    is not writable.
    """
    try:
        from .runtime_deps import browser_profile_dir

        safe_name = re.sub(r"[^a-z0-9_-]", "", str(browser_name or "chrome").lower()) or "chrome"
        directory = browser_profile_dir() / safe_name
        directory.mkdir(parents=True, exist_ok=True)
        return str(directory)
    except (ImportError, OSError):
        return ""


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# Chrome ships background services that are useless for sniffing but expensive on
# disk: a fresh profile pulled ~49MB of optimization-guide ML models plus an
# uncapped HTTP cache. The profile is now persistent, so that would accumulate
# in the user's AppData indefinitely.
DISABLED_BROWSER_FEATURES = (
    "LockProfileCookieDatabase",
    "OptimizationGuideModelDownloading",
    "OptimizationHints",
)
BROWSER_DISK_CACHE_BYTES = 50 * 1024 * 1024


def _launch_browser(browser_exe: str, port: int, user_data_dir: str, browser_mode: str = "visible") -> subprocess.Popen:
    args = [
        browser_exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-popup-blocking",
        "--autoplay-policy=no-user-gesture-required",
        # Chrome honours only the last --disable-features, so these must be one flag.
        f"--disable-features={','.join(DISABLED_BROWSER_FEATURES)}",
        f"--disk-cache-size={BROWSER_DISK_CACHE_BYTES}",
        "--disable-component-update",
        "--disable-background-networking",
        "about:blank",
    ]
    if str(browser_mode or "").lower() == "headless":
        args.insert(-1, "--headless=new")
        args.insert(-1, "--disable-gpu")
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def _enable_target_discovery(cdp: "_CdpSession", collector: "_SniffCollector") -> None:
    _safe_cdp_call(cdp, "Target.setDiscoverTargets", {"discover": True}, collector)
    _safe_cdp_call(
        cdp,
        "Target.setAutoAttach",
        {
            "autoAttach": True,
            "waitForDebuggerOnStart": False,
            "flatten": True,
        },
        collector,
    )

def _enable_sniff_domains(cdp: "_CdpSession", collector: "_SniffCollector", session_id: str | None = None) -> None:
    _safe_cdp_call(cdp, "Network.enable", collector=collector, session_id=session_id)
    _safe_cdp_call(cdp, "Runtime.enable", collector=collector, session_id=session_id)
    _safe_cdp_call(cdp, "Page.enable", collector=collector, session_id=session_id)

def _handle_attached_target_event(
    cdp: "_CdpSession",
    event: dict[str, Any],
    attached_targets: dict[str, str],
    collector: "_SniffCollector",
) -> None:
    if event.get("method") != "Target.attachedToTarget":
        return
    params = event.get("params") or {}
    session_id = str(params.get("sessionId") or "")
    target_info = params.get("targetInfo") or {}
    target_type = str(target_info.get("type") or "").lower()
    target_url = str(target_info.get("url") or "")
    if not session_id or target_type not in MEDIA_TARGET_TYPES or session_id in attached_targets:
        return
    attached_targets[session_id] = target_type
    _enable_sniff_domains(cdp, collector=collector, session_id=session_id)
    _safe_cdp_call(
        cdp,
        "Target.setAutoAttach",
        {
            "autoAttach": True,
            "waitForDebuggerOnStart": False,
            "flatten": True,
        },
        collector=collector,
        session_id=session_id,
        diagnostic_url=target_url,
    )

def _safe_cdp_call(
    cdp: "_CdpSession",
    method: str,
    params: dict[str, Any] | None = None,
    collector: "_SniffCollector" | None = None,
    session_id: str | None = None,
    diagnostic_url: str = "",
) -> bool:
    try:
        cdp.call(method, params, session_id=session_id)
    except MediaSnifferError as exc:
        if collector:
            collector._add_diagnostic("cdp_capability", f"{method} khong kha dung: {exc}", diagnostic_url)
        return False
    return True

def _activate_known_targets(cdp: "_CdpSession", attached_targets: dict[str, str]) -> None:
    _activate_page_playback(cdp)
    for session_id, target_type in list(attached_targets.items()):
        if target_type in PLAYBACK_TARGET_TYPES:
            _activate_page_playback(cdp, session_id=session_id)

def _activate_page_playback(cdp: "_CdpSession", session_id: str | None = None) -> None:
    try:
        cdp.call(
            "Runtime.evaluate",
            {
                "expression": PLAYBACK_ACTIVATION_SCRIPT,
                "awaitPromise": False,
                "returnByValue": True,
                "userGesture": True,
            },
            session_id=session_id,
        )
        point_result = cdp.call(
            "Runtime.evaluate",
            {
                "expression": PLAYER_POINT_SCRIPT,
                "awaitPromise": False,
                "returnByValue": True,
                "userGesture": True,
            },
            session_id=session_id,
        )
        point = ((point_result or {}).get("result") or {}).get("value") or {}
        x = _coerce_optional_int(point.get("x"))
        y = _coerce_optional_int(point.get("y"))
        if x is None or y is None:
            return
        cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y, "button": "none"}, session_id=session_id)
        cdp.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1}, session_id=session_id)
        cdp.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1}, session_id=session_id)
    except Exception:
        return


def _wait_for_page_target(port: int, timeout: int = 10) -> dict[str, Any]:
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            targets = _json_get(f"http://127.0.0.1:{port}/json/list")
            for target in targets:
                if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                    return target
        except Exception as exc:
            last_error = exc
        time.sleep(0.25)
    raise MediaSnifferError(f"Khong ket noi duoc DevTools browser: {last_error}")


def _json_get(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def _is_media_url(url: str, mime_type: str = "", resource_type: str = "") -> bool:
    if not url or url.startswith(("blob:", "data:", "chrome:", "devtools:")):
        return False
    lower_url = url.lower()
    parsed = urllib.parse.urlparse(lower_url)
    path = parsed.path
    if any(path.endswith(ext) or f"{ext}/" in path for ext in MEDIA_EXTENSIONS):
        return True
    if any(ext in lower_url for ext in (".m3u8?", ".mpd?", ".mp4?", ".webm?", ".m4a?")):
        return True
    lower_mime = (mime_type or "").lower()
    if any(hint in lower_mime for hint in MEDIA_MIME_HINTS):
        return True
    return resource_type.lower() == "media" and not any(token in lower_url for token in NOISY_URL_TOKENS)


def _request_key(params: dict[str, Any]) -> str:
    request_id = str(params.get("requestId") or "")
    session_id = str(params.get("_sessionId") or "")
    if session_id and request_id:
        return f"{session_id}:{request_id}"
    return request_id


def _media_kind(url: str, mime_type: str = "") -> str:
    lower_url = url.lower()
    lower_mime = (mime_type or "").lower()
    if ".m3u8" in lower_url or "mpegurl" in lower_mime:
        return "hls"
    if ".mpd" in lower_url or "dash+xml" in lower_mime:
        return "dash"
    if "audio/" in lower_mime or any(ext in lower_url for ext in (".mp3", ".m4a", ".aac")):
        return "audio"
    return "video"


def _is_license_url(url: str) -> bool:
    """Whether a URL looks like a DRM license acquisition request.

    Deliberately conservative. The earlier version matched bare substrings, so
    "eme" hit inside "themes", "drm" hit any hostname containing those letters,
    and a Creative Commons link counted as DRM. That is not a harmless
    over-report: a license diagnostic changes the failure message the user sees
    and pushes the ladder toward "we do not bypass DRM" for content that has no
    protection at all.
    """
    text = (url or "").strip()
    if not text:
        return False
    if DRM_SYSTEM_RE.search(text):
        return True
    if BENIGN_LICENSE_RE.search(text):
        return False
    return bool(DRM_LICENSE_ENDPOINT_RE.search(text))


def _inspect_manifest_candidates(candidates: list[MediaCandidate]) -> None:
    for candidate in candidates:
        if candidate.kind not in {"hls", "dash"}:
            continue
        text = _fetch_manifest_text(candidate)
        if not text:
            continue
        if candidate.kind == "hls":
            candidate.manifest_variant_count = text.count("#EXT-X-STREAM-INF")
        if _manifest_has_drm(text):
            candidate.drm_signals.append("manifest_content_protection")


def _fetch_manifest_text(candidate: MediaCandidate) -> str:
    headers = build_download_headers(candidate, candidate.page_url)
    request = urllib.request.Request(candidate.url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = response.read(2_000_000)
    except Exception:
        return ""
    try:
        return payload.decode("utf-8", "ignore")
    except Exception:
        return ""


def _manifest_has_drm(text: str) -> bool:
    lower = text.lower()
    return (
        "<contentprotection" in lower
        or "widevine" in lower
        or "playready" in lower
        or "com.widevine.alpha" in lower
        or "cenc:pssh" in lower
    )


def _dedupe_candidates(candidates: Iterable[MediaCandidate]) -> list[MediaCandidate]:
    deduped: dict[str, MediaCandidate] = {}
    for candidate in candidates:
        key = candidate.url
        if key not in deduped:
            deduped[key] = candidate
            continue
        existing = deduped[key]
        existing.request_headers.update(candidate.request_headers)
        existing.response_headers.update(candidate.response_headers)
        existing.status = candidate.status if candidate.status is not None else existing.status
        existing.mime_type = candidate.mime_type or existing.mime_type
        existing.content_length = max(existing.content_length or 0, candidate.content_length or 0) or None
        existing.drm_signals = sorted(set(existing.drm_signals + candidate.drm_signals))
    return list(deduped.values())


def _coerce_candidate(candidate: MediaCandidate | dict[str, Any]) -> MediaCandidate:
    if isinstance(candidate, MediaCandidate):
        return candidate
    url = str(candidate.get("url") or "")
    return MediaCandidate(
        url=url,
        kind=str(candidate.get("kind") or _media_kind(url, str(candidate.get("mimeType") or ""))),
        mime_type=str(candidate.get("mimeType") or candidate.get("mime_type") or ""),
        source=str(candidate.get("source") or "network"),
        status=_coerce_optional_int(candidate.get("status")),
        request_headers=_normalize_headers(candidate.get("requestHeaders") or candidate.get("request_headers") or {}),
        response_headers=_normalize_headers(candidate.get("responseHeaders") or candidate.get("response_headers") or {}),
        resource_type=str(candidate.get("resourceType") or candidate.get("resource_type") or ""),
        content_length=_coerce_optional_int(candidate.get("contentLength") or candidate.get("content_length")),
        initiator=str(candidate.get("initiator") or ""),
        page_url=str(candidate.get("pageUrl") or candidate.get("page_url") or ""),
        manifest_variant_count=_coerce_optional_int(candidate.get("manifestVariantCount") or candidate.get("manifest_variant_count")) or 0,
        drm_signals=list(candidate.get("drmSignals") or candidate.get("drm_signals") or []),
        score=_coerce_optional_int(candidate.get("score")) or 0,
        reasons=list(candidate.get("reasons") or []),
    )


def _score_candidate(candidate: MediaCandidate, page_url: str, has_video_candidate: bool) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    kind_score = {"hls": 120, "dash": 112, "video": 92, "audio": 35}.get(candidate.kind, 20)
    score += kind_score
    reasons.append(f"{candidate.kind} candidate")

    if candidate.status is None:
        score += 5
        reasons.append("chua co status")
    elif 200 <= candidate.status < 300:
        score += 25
        reasons.append("HTTP 2xx")
    elif 300 <= candidate.status < 400:
        score += 10
        reasons.append("HTTP redirect")
    else:
        score -= 80
        reasons.append(f"HTTP {candidate.status}")

    lower_mime = candidate.mime_type.lower()
    if "video/" in lower_mime:
        score += 8
        reasons.append("mime video")
    elif "audio/" in lower_mime:
        score -= 10
        reasons.append("mime audio")
    elif "mpegurl" in lower_mime or "dash+xml" in lower_mime:
        score += 8
        reasons.append("mime manifest")

    if candidate.resource_type.lower() == "media":
        score += 8
        reasons.append("browser Media request")
    elif candidate.resource_type.lower() in {"xhr", "fetch"}:
        score += 3
        reasons.append("XHR/fetch request")

    if candidate.content_length:
        size_score = min(25, int(max(0, math.log2(max(candidate.content_length, 1) / 1_000_000 + 1)) * 8))
        score += size_score
        if candidate.content_length < 256_000:
            score -= 18
            reasons.append("kich thuoc nho")
        else:
            reasons.append(f"size {_format_bytes(candidate.content_length)}")

    if candidate.manifest_variant_count:
        score += min(30, candidate.manifest_variant_count * 5)
        reasons.append(f"{candidate.manifest_variant_count} variant")

    if _same_site(candidate.url, page_url):
        score += 8
        reasons.append("gan host trang")
    elif _same_root_domain(candidate.url, page_url):
        score += 5
        reasons.append("cung root domain")

    lower_url = candidate.url.lower()
    if any(token in lower_url for token in NOISY_URL_TOKENS):
        score -= 35
        reasons.append("co dau hieu thumbnail/ads")
    if candidate.kind == "audio" and has_video_candidate:
        score -= 25
        reasons.append("audio-only bi uu tien thap")
    if candidate.drm_signals:
        score -= 120
        reasons.append("co dau hieu DRM")

    return score, reasons


def _same_site(url: str, page_url: str) -> bool:
    if not url or not page_url:
        return False
    return urllib.parse.urlparse(url).netloc == urllib.parse.urlparse(page_url).netloc


def _same_root_domain(url: str, page_url: str) -> bool:
    left = _root_domain(urllib.parse.urlparse(url).hostname or "")
    right = _root_domain(urllib.parse.urlparse(page_url).hostname or "")
    return bool(left and right and left == right)


def _root_domain(hostname: str) -> str:
    parts = [part for part in hostname.lower().split(".") if part]
    if len(parts) <= 2:
        return ".".join(parts)
    return ".".join(parts[-2:])


def _normalize_headers(headers: Any) -> dict[str, str]:
    if not isinstance(headers, dict):
        return {}
    normalized = {}
    for name, value in headers.items():
        if value is None:
            continue
        normalized[_canonical_header_name(str(name))] = str(value)
    return normalized


def _canonical_header_name(name: str) -> str:
    lowered = name.strip().lower()
    if lowered == "dnt":
        return "DNT"
    return "-".join(part[:1].upper() + part[1:] for part in lowered.split("-") if part)


def _public_headers(headers: dict[str, str], include_sensitive_headers: bool) -> dict[str, str]:
    if include_sensitive_headers:
        return dict(headers)
    public: dict[str, str] = {}
    for name, value in headers.items():
        if _is_sensitive_header(name):
            public[name] = "<redacted>"
        else:
            public[name] = value
    return public


def _has_sensitive_headers(headers: dict[str, str]) -> bool:
    return any(_is_sensitive_header(name) for name in headers)


def _is_sensitive_header(name: str) -> bool:
    lowered = name.lower()
    return lowered in SENSITIVE_HEADER_NAMES or "token" in lowered or "secret" in lowered


def _should_forward_header(name: str) -> bool:
    lowered = name.lower()
    if lowered.startswith(":"):
        return False
    blocked = {
        "host",
        "connection",
        "content-length",
        "accept-encoding",
        "sec-fetch-dest",
        "sec-fetch-mode",
        "sec-fetch-site",
        "sec-fetch-user",
    }
    if lowered in blocked:
        return False
    return True


def _content_length_from_headers(headers: dict[str, str]) -> int | None:
    for key in ("Content-Length", "content-length"):
        value = headers.get(key)
        if value:
            return _coerce_optional_int(value)
    return None


def _coerce_optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _format_initiator(initiator: Any) -> str:
    if not isinstance(initiator, dict):
        return ""
    if initiator.get("url"):
        return str(initiator.get("url"))
    stack = initiator.get("stack") or {}
    call_frames = stack.get("callFrames") if isinstance(stack, dict) else None
    if isinstance(call_frames, list) and call_frames:
        frame = call_frames[0] or {}
        return str(frame.get("url") or "")
    return str(initiator.get("type") or "")


def _format_bytes(value: int | None) -> str:
    if not value:
        return "-"
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(value)
    unit = 0
    while size >= 1024 and unit < len(units) - 1:
        size /= 1024
        unit += 1
    return f"{size:.1f} {units[unit]}" if unit else f"{int(size)} {units[unit]}"


def _origin_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def _default_user_agent() -> str:
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )


class _CdpSession:
    DEFAULT_CALL_TIMEOUT = 5.0
    # Events that arrive while waiting for a command result are buffered. During
    # a long capture that can be thousands of messages, so the buffer is capped
    # and drops oldest-first rather than growing without bound.
    MAX_EVENT_BUFFER = 2000

    def __init__(self, websocket: "_CdpWebSocket"):
        self.websocket = websocket
        self.next_id = 1
        self.event_buffer: list[dict[str, Any]] = []
        self.dropped_events = 0

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        command_id = self.next_id
        self.next_id += 1
        payload = {"id": command_id, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        self.websocket.send_json(payload)

        deadline = time.time() + (timeout if timeout is not None else self.DEFAULT_CALL_TIMEOUT)
        while time.time() < deadline:
            message = self.websocket.recv_json(timeout=min(1.0, max(0.05, deadline - time.time())))
            if message and message.get("id") == command_id:
                if "error" in message:
                    raise MediaSnifferError(str(message["error"]))
                return message.get("result")
            if message and message.get("method"):
                self._buffer_event(message)
        raise MediaSnifferError(f"CDP command timeout: {method}")

    def _buffer_event(self, message: dict[str, Any]) -> None:
        self.event_buffer.append(message)
        if len(self.event_buffer) > self.MAX_EVENT_BUFFER:
            overflow = len(self.event_buffer) - self.MAX_EVENT_BUFFER
            del self.event_buffer[:overflow]
            self.dropped_events += overflow

    def recv(self, timeout: float = 1.0) -> dict[str, Any] | None:
        if self.event_buffer:
            return self.event_buffer.pop(0)
        return self.websocket.recv_json(timeout=timeout)


def _xor_mask(payload: bytes, mask: bytes) -> bytes:
    """Apply a WebSocket mask.

    Done as a single big-integer XOR rather than a per-byte generator: the
    previous approach cost a Python-level loop iteration per byte, which is fine
    for small JSON commands but unusable once media payloads are involved.
    """
    if not mask or not payload:
        return payload
    repeated = (mask * (len(payload) // len(mask) + 1))[: len(payload)]
    masked = int.from_bytes(payload, "big") ^ int.from_bytes(repeated, "big")
    return masked.to_bytes(len(payload), "big")


class _CdpWebSocket:
    OPCODE_CONTINUATION = 0x0
    OPCODE_TEXT = 0x1
    OPCODE_BINARY = 0x2
    OPCODE_CLOSE = 0x8
    OPCODE_PING = 0x9
    OPCODE_PONG = 0xA

    def __init__(self, ws_url: str):
        parsed = urllib.parse.urlparse(ws_url)
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 80
        self.path = parsed.path
        if parsed.query:
            self.path += f"?{parsed.query}"
        self.sock: socket.socket | None = None
        # Bytes received but not yet forming a complete frame.
        self._buffer = bytearray()
        # Payload accumulated across continuation frames.
        self._fragments = bytearray()

    def connect(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        sock = socket.create_connection((self.host, self.port), timeout=5)
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(request.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            sock.close()
            raise MediaSnifferError("DevTools websocket handshake failed")
        accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        if f"Sec-WebSocket-Accept: {accept}".encode("ascii").lower() not in response.lower():
            sock.close()
            raise MediaSnifferError("DevTools websocket accept key mismatch")
        sock.settimeout(1)
        self.sock = sock

    def send_json(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._send_frame(data, opcode=self.OPCODE_TEXT)

    def recv_json(self, timeout: float = 1.0) -> dict[str, Any] | None:
        """Return the next complete CDP message, or None if none arrived in time.

        Never leaves the stream mid-frame: bytes accumulate in a persistent
        buffer and are only consumed once a whole frame is present. The previous
        implementation read directly from the socket, so a timeout partway
        through a frame discarded the exception but kept the consumed bytes, and
        every subsequent read started at a frame boundary that no longer existed.
        """
        payload = self._recv_message(timeout)
        if not payload:
            return None
        try:
            return json.loads(payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def close(self) -> None:
        if not self.sock:
            return
        try:
            self.sock.close()
        finally:
            self.sock = None
            self._buffer.clear()
            self._fragments.clear()

    # -- framing ----------------------------------------------------------

    def _send_frame(self, payload: bytes, opcode: int) -> None:
        if not self.sock:
            raise MediaSnifferError("Websocket is not connected")
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        self.sock.sendall(bytes(header) + _xor_mask(payload, mask))

    def _recv_message(self, timeout: float) -> bytes:
        """Assemble one application message, handling fragments and control frames."""
        deadline = time.time() + max(0.0, timeout)
        while True:
            frame = self._take_frame()
            if frame is None:
                if not self._fill(deadline):
                    return b""
                continue

            fin, opcode, payload = frame

            if opcode == self.OPCODE_CLOSE:
                # Surfacing this as an error matters: previously a close frame
                # became an empty payload, indistinguishable from "nothing yet",
                # so a dropped connection looked like an idle one forever.
                self.close()
                raise MediaSnifferError("DevTools websocket closed by browser")
            if opcode == self.OPCODE_PING:
                self._send_frame(payload, opcode=self.OPCODE_PONG)
                continue
            if opcode == self.OPCODE_PONG:
                continue

            if opcode == self.OPCODE_CONTINUATION:
                self._fragments.extend(payload)
            else:
                self._fragments = bytearray(payload)

            if fin:
                message = bytes(self._fragments)
                self._fragments.clear()
                return message

    def _take_frame(self) -> tuple[bool, int, bytes] | None:
        """Pop one complete frame from the buffer, or None if it is incomplete.

        Nothing is consumed unless a whole frame is available, which is what
        makes a mid-frame timeout harmless.
        """
        buffer = self._buffer
        if len(buffer) < 2:
            return None

        length = buffer[1] & 0x7F
        offset = 2
        if length == 126:
            if len(buffer) < offset + 2:
                return None
            length = struct.unpack("!H", bytes(buffer[offset:offset + 2]))[0]
            offset += 2
        elif length == 127:
            if len(buffer) < offset + 8:
                return None
            length = struct.unpack("!Q", bytes(buffer[offset:offset + 8]))[0]
            offset += 8

        masked = bool(buffer[1] & 0x80)
        mask = b""
        if masked:
            if len(buffer) < offset + 4:
                return None
            mask = bytes(buffer[offset:offset + 4])
            offset += 4

        if len(buffer) < offset + length:
            return None

        fin = bool(buffer[0] & 0x80)
        opcode = buffer[0] & 0x0F
        payload = bytes(buffer[offset:offset + length])
        del buffer[: offset + length]
        return fin, opcode, _xor_mask(payload, mask) if masked else payload

    def _fill(self, deadline: float) -> bool:
        """Read more bytes into the buffer. False when nothing arrived in time."""
        if not self.sock:
            return False
        remaining = deadline - time.time()
        if remaining <= 0:
            return False
        self.sock.settimeout(remaining)
        try:
            chunk = self.sock.recv(65536)
        except socket.timeout:
            return False
        except OSError as exc:
            raise MediaSnifferError(f"DevTools websocket read failed: {exc}") from exc
        if not chunk:
            self.close()
            raise MediaSnifferError("DevTools websocket connection closed")
        self._buffer.extend(chunk)
        return True


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=4)
    except subprocess.TimeoutExpired:
        process.kill()
