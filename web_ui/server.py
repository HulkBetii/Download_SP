from __future__ import annotations

import argparse
import errno
import json
import mimetypes
import os
import queue
import re
import sys
import threading
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from core.analyzer import analyze_video
from core.auto_pipeline import (
    download_media_candidate,
    inspect_media as inspect_media_candidates,
    is_unsupported_error,
    run_auto_download,
)
from core.downloader import download_video, get_runtime_capabilities
from core.media_sniffer import MediaSnifferError, get_browser_capabilities, sniff_media_urls
from core.runtime_deps import detect as detect_dependencies, ensure_all as ensure_dependencies

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_PORT_MAX = 8785

STATUS_QUEUED = "Chờ"
STATUS_RUNNING = "Đang tải"
STATUS_PROCESSING = "Đang xử lý"
STATUS_DONE = "Hoàn tất"
STATUS_FAILED = "Lỗi"

VALID_OPTIMIZE_MODES = {"balanced", "speed", "quality"}
MAX_LOG_ITEMS = 500
# Parallel downloads per batch. Kept low on purpose: more connections to one
# host is the fastest way to earn a rate limit.
MAX_DOWNLOAD_WORKERS = 2
RATE_LIMITED_HOST_MARKER = "sharepoint.com"
STATIC_DIR = Path(__file__).resolve().parent / "static"
FAVICON_PATH = project_root / "favicon.ico"

DownloadFn = Callable[..., None]


class AppError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass(slots=True)
class QueueItem:
    item_id: int
    url: str
    host: str
    status: str = STATUS_QUEUED
    progress: int = 0
    speed: str = "-"
    eta: str = "-"
    update_text: str = "Sẵn sàng"
    error: str = ""
    last_logged_bucket: int = -1

    def to_dict(self, index: int) -> dict[str, Any]:
        return {
            "id": self.item_id,
            "index": index,
            "url": self.url,
            "host": self.host,
            "status": self.status,
            "progress": self.progress,
            "speed": self.speed,
            "eta": self.eta,
            "updateText": self.update_text,
            "error": self.error,
        }


class AppState:
    def __init__(self, downloader: DownloadFn | None = None):
        self.downloader = downloader or download_video
        self.analyzer = None
        self.sniffer = None
        self.lock = threading.RLock()
        self.subscribers: set[queue.Queue[dict[str, Any]]] = set()
        self.event_version = 0

        self.queue_items: list[QueueItem] = []
        self.items_by_id: dict[int, QueueItem] = {}
        self.next_item_id = 1
        self.logs: list[dict[str, str]] = []
        self.last_candidates: list[dict[str, Any]] = []
        self._last_candidates_full: list[dict[str, Any]] = []
        self.diagnostics: list[dict[str, Any]] = []
        self.auto_job: dict[str, Any] = self._empty_auto_job()

        self.output_folder = str(Path.home() / "Downloads")
        self.use_cookies = True
        self.cookie_file = ""
        self.optimize_mode = "quality"
        self.capabilities = get_runtime_capabilities()
        self.browser_capabilities = get_browser_capabilities()
        self.dependencies = detect_dependencies().to_dict()
        self.setup_running = False
        self.use_browser_cookies = False
        self.browser_name = "coccoc"
        self.browser_profile = ""
        self.browser_keyring = ""
        self.browser_container = ""
        self.proxy = ""
        self.impersonate = ""
        self.download_archive = ""
        self.check_formats = False
        self.format_sort = ""
        self.concurrent_fragments = ""
        self.skip_unavailable_fragments = False

        self.batch_running = False
        self.batch_total = 0
        self.batch_processed = 0
        self.batch_success = 0
        self.batch_failed = 0
        self.batch_errors: list[str] = []

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return self._snapshot_locked()

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        subscriber: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=20)
        with self.lock:
            self.subscribers.add(subscriber)
            subscriber.put({"version": self.event_version, "state": self._snapshot_locked()})
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[dict[str, Any]]) -> None:
        with self.lock:
            self.subscribers.discard(subscriber)

    def _empty_auto_job(self) -> dict[str, Any]:
        return {
            "id": "",
            "running": False,
            "stage": "idle",
            "message": "San sang.",
            "level": "info",
            "progress": 0,
            "itemId": None,
            "sourceUrl": "",
            "selectedCandidateId": "",
            "updatedAt": "",
        }

    def add_urls(self, raw_urls: list[str], source_label: str = "web") -> dict[str, int]:
        with self.lock:
            if self.batch_running:
                raise AppError(HTTPStatus.CONFLICT, "Đang tải, chưa thể thay đổi hàng đợi.")

            added = 0
            duplicates = 0
            invalid = 0

            for raw_url in raw_urls:
                url = self._clean_url(raw_url)
                if not url:
                    continue
                if not self._is_valid_url(url):
                    invalid += 1
                    self._append_log_locked(f"Bỏ qua URL không hợp lệ: {self._compact_text(url, 90)}", "warning")
                    continue
                if any(item.url == url for item in self.queue_items):
                    duplicates += 1
                    continue

                item = QueueItem(item_id=self.next_item_id, url=url, host=self._extract_host(url))
                self.next_item_id += 1
                self.queue_items.append(item)
                self.items_by_id[item.item_id] = item
                added += 1

            if added:
                self._append_log_locked(f"Đã thêm {added} URL từ {source_label}.", "success")
            elif duplicates or invalid:
                self._append_log_locked(f"Không thêm URL mới. Trùng: {duplicates}, không hợp lệ: {invalid}.", "warning")

            if duplicates:
                self._append_log_locked(f"Bỏ qua {duplicates} URL trùng.", "warning")

            self._publish_locked()
            return {"added": added, "duplicates": duplicates, "invalid": invalid}

    def remove_items(self, item_ids: list[int]) -> dict[str, int]:
        with self.lock:
            if self.batch_running:
                raise AppError(HTTPStatus.CONFLICT, "Đang tải, chưa thể xóa mục trong hàng đợi.")

            wanted = set(item_ids)
            before = len(self.queue_items)
            self.queue_items = [item for item in self.queue_items if item.item_id not in wanted]
            for item_id in wanted:
                self.items_by_id.pop(item_id, None)
            removed = before - len(self.queue_items)

            if removed:
                self._append_log_locked(f"Đã xóa {removed} mục khỏi hàng đợi.", "info")
            if not self.queue_items:
                self._reset_batch_counters_locked()
            self._publish_locked()
            return {"removed": removed}

    def clear_queue(self) -> dict[str, int]:
        with self.lock:
            if self.batch_running:
                raise AppError(HTTPStatus.CONFLICT, "Đang tải, chưa thể xóa hàng đợi.")
            cleared = len(self.queue_items)
            self.queue_items.clear()
            self.items_by_id.clear()
            if cleared:
                self._append_log_locked("Đã xóa toàn bộ hàng đợi.", "info")
            self._reset_batch_counters_locked()
            self._publish_locked()
            return {"cleared": cleared}

    def run_setup(self) -> dict[str, Any]:
        """Install whatever the environment is missing, reporting progress live."""
        with self.lock:
            if self.setup_running:
                raise AppError(HTTPStatus.CONFLICT, "Dang cai dat, vui long doi.")
            self.setup_running = True
            self._append_log_locked("Bat dau kiem tra va cai dat thanh phan con thieu.", "info")
            self._publish_locked()

        def progress(message: str, level: str = "info") -> None:
            with self.lock:
                self._append_log_locked(self._compact_text(message, 180), level)
                self._publish_locked()

        try:
            report = ensure_dependencies(progress)
        except Exception as exc:
            with self.lock:
                self.setup_running = False
                self._append_log_locked(f"Cai dat that bai: {self._clean_error_message(str(exc))}", "error")
                self._publish_locked()
            raise AppError(HTTPStatus.INTERNAL_SERVER_ERROR, f"Cai dat that bai: {exc}") from exc

        with self.lock:
            self.setup_running = False
            self.dependencies = report.to_dict()
            self.capabilities = get_runtime_capabilities()
            summary = ", ".join(report.actions) if report.actions else "khong co gi phai cai them"
            self._append_log_locked(f"Kiem tra xong: {summary}.", "success")
            self._publish_locked()
        return self.dependencies

    def clear_log(self) -> dict[str, int]:
        with self.lock:
            cleared = len(self.logs)
            self.logs.clear()
            self._publish_locked()
            return {"cleared": cleared}

    def analyze_url(self, options: dict[str, Any]) -> dict[str, Any]:
        url = self._clean_url(str(options.get("url") or ""))
        if not url:
            raise AppError(HTTPStatus.BAD_REQUEST, "Vui lòng nhập URL để phân tích.")

        optimize_mode = str(options.get("optimizeMode") or self.optimize_mode)
        if optimize_mode not in VALID_OPTIMIZE_MODES:
            raise AppError(HTTPStatus.BAD_REQUEST, "Chế độ tải không hợp lệ.")

        use_cookies = self._read_bool_option(options, "useCookies", self.use_cookies)
        advanced_options = self._read_advanced_options(options)
        cookie_file = self._read_cookie_file_option(options, use_cookies)
        self._append_log_locked(f"Phân tích URL: {self._compact_text(url, 120)}", "info")
        self._publish_locked()

        try:
            result = analyze_video(
                url,
                cookie_file=cookie_file,
                optimize_mode=optimize_mode,
                advanced_options=advanced_options,
            )
        except Exception as exc:
            message = self._clean_error_message(str(exc)) or "Khong phan tich duoc URL."
            self._append_log_locked(f"Probe loi: {message}", "error")
            self._publish_locked()
            raise AppError(HTTPStatus.BAD_REQUEST, message) from exc

        if not isinstance(result, dict):
            message = "Khong phan tich duoc URL (khong co ket qua tra ve)."
            self._append_log_locked(f"Probe loi: {message}", "error")
            self._publish_locked()
            raise AppError(HTTPStatus.BAD_REQUEST, message)

        self._append_log_locked(
            f"Probe xong: {self._compact_text(result.get('title') or url, 120)} · {result.get('formatCount', 0)} format",
            "success",
        )
        self._publish_locked()
        return result

    def sniff_media(self, options: dict[str, Any]) -> dict[str, Any]:
        url = self._clean_url(str(options.get("url") or ""))
        if not url:
            raise AppError(HTTPStatus.BAD_REQUEST, "Vui lòng nhập URL để bắt media.")

        browser_name = self._read_text_option(options, "browserName", self.browser_name or "coccoc")
        browser_profile = self._read_text_option(options, "browserProfile", self.browser_profile)
        browser_path = self._read_text_option(options, "browserPath", "")
        timeout_seconds = self._read_int_option(options, "sniffTimeoutSeconds", 45, minimum=8, maximum=120)

        with self.lock:
            if self.batch_running:
                raise AppError(HTTPStatus.CONFLICT, "Đang tải, chưa thể bắt media.")
            self._append_log_locked(
                f"Mở {browser_name} để bắt media: {self._compact_text(url, 120)}. Hãy bấm play trong cửa sổ mới nếu cần.",
                "info",
            )
            self._publish_locked()

        try:
            sniffer = self.sniffer or sniff_media_urls
            result = sniffer(
                url,
                browser_name=browser_name,
                browser_path=browser_path,
                profile_path=browser_profile,
                timeout_seconds=timeout_seconds,
                include_sensitive_headers=True,
            )
        except MediaSnifferError as exc:
            message = str(exc)
            with self.lock:
                self._append_log_locked(f"Bắt media lỗi: {message}", "error")
                self._publish_locked()
            raise AppError(HTTPStatus.BAD_REQUEST, message) from exc

        media_urls = [item["url"] for item in result.get("mediaUrls", []) if item.get("url")]
        queue_result = {"added": 0, "duplicates": 0, "invalid": 0}
        queue_conflict = False
        if media_urls:
            try:
                queue_result = self.add_urls(media_urls, f"media sniffer/{browser_name}")
            except AppError:
                # A batch may have started during the long sniff; keep the sniff result
                # instead of discarding it, and let the user re-add later.
                queue_conflict = True

        with self.lock:
            self._store_candidates_locked(result)
            if queue_conflict:
                self._append_log_locked(
                    "Đã bắt được media nhưng hàng đợi đang bận (đang tải), chưa thêm link. Hãy thử lại khi tải xong.",
                    "warning",
                )
            elif media_urls:
                self._append_log_locked(
                    f"Bắt media xong: tìm thấy {len(media_urls)} link, thêm {queue_result['added']} link mới vào hàng đợi.",
                    "success" if queue_result["added"] else "warning",
                )
            else:
                self._append_log_locked(
                    "Bắt media không thấy link .mp4/.m3u8/.mpd. Hãy mở lại và bấm play trong cửa sổ browser khi sniffer đang chạy.",
                    "warning",
                )
            self._publish_locked()

        return {**self._public_sniff_result(result), "queue": queue_result}

    def inspect_media(self, options: dict[str, Any]) -> dict[str, Any]:
        url = self._clean_url(str(options.get("url") or ""))
        if not url:
            raise AppError(HTTPStatus.BAD_REQUEST, "Vui long nhap URL de inspect media.")

        browser_name = self._read_text_option(options, "browserName", self.browser_name or "coccoc")
        browser_profile = self._read_text_option(options, "browserProfile", self.browser_profile)
        browser_path = self._read_text_option(options, "browserPath", "")
        browser_mode = self._read_text_option(options, "browserMode", "visible") or "visible"
        timeout_seconds = self._read_int_option(options, "sniffTimeoutSeconds", 45, minimum=8, maximum=120)

        with self.lock:
            if self.batch_running or self._auto_running_locked():
                raise AppError(HTTPStatus.CONFLICT, "Dang tai, chua the inspect media.")
            self._append_log_locked(
                f"Inspect media bang {browser_name}: {self._compact_text(url, 120)}. Hay bam play neu can.",
                "info",
            )
            self._publish_locked()

        try:
            result = inspect_media_candidates(
                url,
                browser_name=browser_name,
                browser_path=browser_path,
                browser_profile=browser_profile,
                browser_mode=browser_mode,
                sniff_timeout_seconds=timeout_seconds,
                sniffer=self.sniffer or sniff_media_urls,
            )
        except MediaSnifferError as exc:
            message = str(exc)
            with self.lock:
                self.diagnostics = [{"type": "sniff_error", "message": message, "url": url}]
                self._append_log_locked(f"Inspect media loi: {message}", "error")
                self._publish_locked()
            raise AppError(HTTPStatus.BAD_REQUEST, message) from exc

        with self.lock:
            self._store_candidates_locked(result)
            count = len(self.last_candidates)
            if count:
                selected = result.get("selectedCandidateId") or ""
                suffix = f", chon mac dinh #{selected}" if selected else ""
                self._append_log_locked(f"Inspect media xong: {count} candidate{suffix}.", "success")
            else:
                self._append_log_locked("Inspect media khong thay candidate tai duoc.", "warning")
            self._publish_locked()

        return self._public_sniff_result(result)

    def _read_advanced_options(self, options: dict[str, Any]) -> dict[str, Any]:
        return {
            "useBrowserCookies": self._read_bool_option(options, "useBrowserCookies", self.use_browser_cookies),
            "browserName": self._read_text_option(options, "browserName", self.browser_name or "coccoc"),
            "browserProfile": self._read_text_option(options, "browserProfile", self.browser_profile),
            "browserKeyring": self._read_text_option(options, "browserKeyring", self.browser_keyring),
            "browserContainer": self._read_text_option(options, "browserContainer", self.browser_container),
            "proxy": self._read_text_option(options, "proxy", self.proxy),
            "impersonate": self._read_text_option(options, "impersonate", self.impersonate),
            "downloadArchive": self._read_text_option(options, "downloadArchive", self.download_archive),
            "checkFormats": self._read_bool_option(options, "checkFormats", self.check_formats),
            "formatSort": self._read_text_option(options, "formatSort", self.format_sort),
            "concurrentFragments": self._read_text_option(options, "concurrentFragments", self.concurrent_fragments),
            "skipUnavailableFragments": self._read_bool_option(options, "skipUnavailableFragments", self.skip_unavailable_fragments),
        }

    def _read_cookie_file_option(self, options: dict[str, Any], use_cookies: bool) -> str | None:
        if not use_cookies:
            return None
        raw_cookie_file = options.get("cookieFile") if "cookieFile" in options else self.cookie_file
        return self._clean_path(str(raw_cookie_file or ""))

    def _store_advanced_options_locked(self, advanced: dict[str, Any]) -> None:
        self.use_browser_cookies = bool(advanced.get("useBrowserCookies"))
        self.browser_name = str(advanced.get("browserName") or "chrome")
        self.browser_profile = str(advanced.get("browserProfile") or "")
        self.browser_keyring = str(advanced.get("browserKeyring") or "")
        self.browser_container = str(advanced.get("browserContainer") or "")
        self.proxy = str(advanced.get("proxy") or "")
        self.impersonate = str(advanced.get("impersonate") or "")
        self.download_archive = str(advanced.get("downloadArchive") or "")
        self.check_formats = bool(advanced.get("checkFormats"))
        self.format_sort = str(advanced.get("formatSort") or "")
        self.concurrent_fragments = str(advanced.get("concurrentFragments") or "")
        self.skip_unavailable_fragments = bool(advanced.get("skipUnavailableFragments"))

    def _read_bool_option(self, options: dict[str, Any], key: str, default: bool) -> bool:
        if key not in options:
            return bool(default)
        value = options.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _read_text_option(self, options: dict[str, Any], key: str, default: str = "") -> str:
        if key not in options:
            return str(default or "").strip()
        return str(options.get(key) or "").strip()

    def _read_int_option(self, options: dict[str, Any], key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(float(str(options.get(key, default)).strip()))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    def start_download(self, options: dict[str, Any]) -> dict[str, Any]:
        output_folder = self._clean_path(str(options.get("outputFolder") or self.output_folder))
        if not output_folder:
            raise AppError(HTTPStatus.BAD_REQUEST, "Vui lòng nhập thư mục lưu.")

        optimize_mode = str(options.get("optimizeMode") or self.optimize_mode)
        if optimize_mode not in VALID_OPTIMIZE_MODES:
            raise AppError(HTTPStatus.BAD_REQUEST, "Chế độ tải không hợp lệ.")

        use_cookies = self._read_bool_option(options, "useCookies", self.use_cookies)
        cookie_file = self._read_cookie_file_option(options, use_cookies)
        advanced_options = self._read_advanced_options(options)

        try:
            os.makedirs(output_folder, exist_ok=True)
        except OSError as exc:
            raise AppError(HTTPStatus.BAD_REQUEST, f"Không thể sử dụng thư mục lưu: {exc}") from exc

        with self.lock:
            if self.batch_running:
                raise AppError(HTTPStatus.CONFLICT, "Đang có lô tải đang chạy.")

            pending_items = [item for item in self.queue_items if item.status in (STATUS_QUEUED, STATUS_FAILED)]
            if not pending_items:
                raise AppError(HTTPStatus.BAD_REQUEST, "Không có URL đang chờ tải hoặc cần thử lại.")

            self.output_folder = output_folder
            self.use_cookies = use_cookies
            self.cookie_file = cookie_file or ""
            self.optimize_mode = optimize_mode
            self._store_advanced_options_locked(advanced_options)

            self.batch_running = True
            self.batch_total = len(pending_items)
            self.batch_processed = 0
            self.batch_success = 0
            self.batch_failed = 0
            self.batch_errors = []

            for item in pending_items:
                item.status = STATUS_PROCESSING
                item.progress = 0
                item.speed = "-"
                item.eta = "-"
                item.update_text = "Đang chờ trong lô"
                item.error = ""
                item.last_logged_bucket = -1

            self._append_log_locked(f"Bắt đầu lô tải: {self.batch_total} mục, chế độ {optimize_mode}.", "info")
            work_items = [(item.item_id, item.url) for item in pending_items]
            self._publish_locked()

        worker = threading.Thread(
            target=self._batch_download_worker,
            args=(work_items, output_folder, cookie_file, optimize_mode, advanced_options),
            daemon=True,
        )
        worker.start()

        return {"started": len(work_items), "state": self.snapshot()}

    def start_auto_download(self, options: dict[str, Any]) -> dict[str, Any]:
        url = self._clean_url(str(options.get("url") or ""))
        if not url:
            raise AppError(HTTPStatus.BAD_REQUEST, "Vui long nhap URL de tai tu dong.")
        if not self._is_valid_url(url):
            raise AppError(HTTPStatus.BAD_REQUEST, "URL khong hop le.")

        output_folder = self._clean_path(str(options.get("outputFolder") or self.output_folder))
        if not output_folder:
            raise AppError(HTTPStatus.BAD_REQUEST, "Vui long nhap thu muc luu.")

        optimize_mode = str(options.get("optimizeMode") or self.optimize_mode)
        if optimize_mode not in VALID_OPTIMIZE_MODES:
            raise AppError(HTTPStatus.BAD_REQUEST, "Che do tai khong hop le.")

        use_cookies = self._read_bool_option(options, "useCookies", self.use_cookies)
        cookie_file = self._read_cookie_file_option(options, use_cookies)
        advanced_options = self._read_advanced_options(options)
        browser_name = self._read_text_option(options, "browserName", self.browser_name or "coccoc")
        browser_profile = self._read_text_option(options, "browserProfile", self.browser_profile)
        browser_path = self._read_text_option(options, "browserPath", "")
        browser_mode = self._read_text_option(options, "browserMode", "visible") or "visible"
        sniff_timeout_seconds = self._read_int_option(options, "sniffTimeoutSeconds", 45, minimum=8, maximum=120)
        auto_select = self._read_bool_option(options, "autoSelect", True)

        try:
            os.makedirs(output_folder, exist_ok=True)
        except OSError as exc:
            raise AppError(HTTPStatus.BAD_REQUEST, f"Khong the su dung thu muc luu: {exc}") from exc

        with self.lock:
            if self.batch_running or self._auto_running_locked():
                raise AppError(HTTPStatus.CONFLICT, "Dang co tac vu tai dang chay.")

            item = QueueItem(
                item_id=self.next_item_id,
                url=url,
                host=self._extract_host(url),
                status=STATUS_PROCESSING,
                progress=0,
                update_text="Auto: dang probe",
            )
            self.next_item_id += 1
            self.queue_items.append(item)
            self.items_by_id[item.item_id] = item

            self.output_folder = output_folder
            self.use_cookies = use_cookies
            self.cookie_file = cookie_file or ""
            self.optimize_mode = optimize_mode
            self._store_advanced_options_locked(advanced_options)

            self.batch_running = True
            self.batch_total = 1
            self.batch_processed = 0
            self.batch_success = 0
            self.batch_failed = 0
            self.batch_errors = []
            self.last_candidates = []
            self._last_candidates_full = []
            self.diagnostics = []
            self.auto_job = {
                **self._empty_auto_job(),
                "id": f"auto-{item.item_id}",
                "running": True,
                "stage": "probing",
                "message": "Dang thu yt-dlp probe.",
                "progress": 5,
                "itemId": item.item_id,
                "sourceUrl": url,
                "updatedAt": datetime.now().isoformat(timespec="seconds"),
            }
            self._append_log_locked(f"Bat dau tai tu dong: {self._compact_text(url, 120)}", "info")
            self._publish_locked()

        worker = threading.Thread(
            target=self._auto_download_worker,
            args=(
                item.item_id,
                url,
                output_folder,
                cookie_file,
                optimize_mode,
                advanced_options,
                browser_name,
                browser_path,
                browser_profile,
                browser_mode,
                sniff_timeout_seconds,
                auto_select,
            ),
            daemon=True,
        )
        worker.start()
        return {"started": 1, "state": self.snapshot()}

    def start_candidate_download(self, options: dict[str, Any]) -> dict[str, Any]:
        candidate_id = str(options.get("candidateId") or options.get("id") or "").strip()
        candidate_url = str(options.get("url") or "").strip()
        with self.lock:
            if self.batch_running or self._auto_running_locked():
                raise AppError(HTTPStatus.CONFLICT, "Dang co tac vu tai dang chay.")
            candidate = self._find_candidate_locked(candidate_id, candidate_url)
            if not candidate:
                raise AppError(HTTPStatus.NOT_FOUND, "Khong tim thay media candidate.")

        output_folder = self._clean_path(str(options.get("outputFolder") or self.output_folder))
        optimize_mode = str(options.get("optimizeMode") or self.optimize_mode)
        use_cookies = self._read_bool_option(options, "useCookies", self.use_cookies)
        cookie_file = self._read_cookie_file_option(options, use_cookies)
        advanced_options = self._read_advanced_options(options)
        source_url = str(candidate.get("pageUrl") or options.get("sourceUrl") or "")

        try:
            os.makedirs(output_folder, exist_ok=True)
        except OSError as exc:
            raise AppError(HTTPStatus.BAD_REQUEST, f"Khong the su dung thu muc luu: {exc}") from exc

        with self.lock:
            item = QueueItem(
                item_id=self.next_item_id,
                url=str(candidate.get("url") or ""),
                host=self._extract_host(str(candidate.get("url") or "")),
                status=STATUS_RUNNING,
                progress=0,
                update_text="Dang tai candidate da chon",
            )
            self.next_item_id += 1
            self.queue_items.append(item)
            self.items_by_id[item.item_id] = item
            self.batch_running = True
            self.batch_total = 1
            self.batch_processed = 0
            self.batch_success = 0
            self.batch_failed = 0
            self.batch_errors = []
            self._append_log_locked(f"Tai media candidate: {self._compact_text(item.url, 120)}", "info")
            self._publish_locked()

        worker = threading.Thread(
            target=self._candidate_download_worker,
            args=(item.item_id, candidate, source_url, output_folder, cookie_file, optimize_mode, advanced_options),
            daemon=True,
        )
        worker.start()
        return {"started": 1, "state": self.snapshot()}

    def _auto_download_worker(
        self,
        item_id: int,
        url: str,
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
    ) -> None:
        def event_callback(event: dict[str, Any]) -> None:
            self.handle_auto_event(item_id, event)

        def status_callback(status_text: str, color: str = "blue") -> None:
            self.handle_download_status(item_id, status_text, color)

        try:
            run_auto_download(
                url,
                output_folder,
                cookie_file=cookie_file,
                optimize_mode=optimize_mode,
                advanced_options=advanced_options,
                browser_name=browser_name,
                browser_path=browser_path,
                browser_profile=browser_profile,
                browser_mode=browser_mode,
                sniff_timeout_seconds=sniff_timeout_seconds,
                auto_select=auto_select,
                event_callback=event_callback,
                status_callback=status_callback,
                analyzer=self.analyzer or analyze_video,
                downloader=self.downloader,
                sniffer=self.sniffer or sniff_media_urls,
            )
        except Exception as exc:
            self.handle_auto_event(item_id, {"stage": "failed", "message": str(exc), "level": "error", "progress": 100})
            self.handle_download_done(item_id, success=False, error=str(exc))
        else:
            self.handle_auto_event(item_id, {"stage": "done", "message": "Tai tu dong hoan tat.", "level": "success", "progress": 100})
            self.handle_download_done(item_id, success=True)

    def _candidate_download_worker(
        self,
        item_id: int,
        candidate: dict[str, Any],
        source_url: str,
        output_folder: str,
        cookie_file: str | None,
        optimize_mode: str,
        advanced_options: dict[str, Any],
    ) -> None:
        def status_callback(status_text: str, color: str = "blue") -> None:
            self.handle_download_status(item_id, status_text, color)

        try:
            download_media_candidate(
                candidate,
                source_url,
                output_folder,
                cookie_file,
                optimize_mode,
                advanced_options,
                status_callback=status_callback,
                downloader=self.downloader,
            )
        except Exception as exc:
            self.handle_download_done(item_id, success=False, error=str(exc))
        else:
            self.handle_download_done(item_id, success=True)

    def _batch_download_worker(self, work_items: list[tuple[int, str]], output_folder: str, cookie_file: str | None, optimize_mode: str, advanced_options: dict[str, Any]) -> None:
        pending: queue.Queue[tuple[int, str]] = queue.Queue()
        for entry in work_items:
            pending.put(entry)

        def consume() -> None:
            while True:
                try:
                    item_id, url = pending.get_nowait()
                except queue.Empty:
                    return
                try:
                    with self.lock:
                        item = self.items_by_id.get(item_id)
                        if not item:
                            continue
                        item.status = STATUS_RUNNING
                        item.progress = 0
                        item.speed = "-"
                        item.eta = "-"
                        item.update_text = "Đang tải"
                        item.error = ""
                        self._publish_locked()
                    self._download_worker(item_id, url, output_folder, cookie_file, optimize_mode, advanced_options)
                finally:
                    pending.task_done()

        worker_count = self._resolve_worker_count(work_items)
        if worker_count <= 1:
            consume()
            return

        threads = [
            threading.Thread(target=consume, daemon=True, name=f"vdt-download-{index}")
            for index in range(worker_count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    def _resolve_worker_count(self, work_items: list[tuple[int, str]]) -> int:
        """Parallelism for a batch, dropped to 1 for rate-limit sensitive hosts.

        SharePoint downloads are deliberately throttled in the downloader to
        avoid 429s; running several at once would defeat that. A host appearing
        repeatedly in one batch is also a reason to stay sequential.
        """
        if len(work_items) <= 1:
            return 1

        hosts = [self._extract_host(url).lower() for _item_id, url in work_items]
        if any(RATE_LIMITED_HOST_MARKER in host for host in hosts):
            return 1
        return min(MAX_DOWNLOAD_WORKERS, len(work_items))

    def _download_worker(self, item_id: int, url: str, output_folder: str, cookie_file: str | None, optimize_mode: str, advanced_options: dict[str, Any]) -> None:
        def status_callback(status_text: str, color: str = "blue") -> None:
            self.handle_download_status(item_id, status_text, color)

        try:
            self.downloader(
                url,
                output_folder,
                cookie_file,
                status_callback=status_callback,
                optimize_mode=optimize_mode,
                advanced_options=advanced_options,
            )
        except Exception as exc:
            error_message = str(exc)
            if is_unsupported_error(error_message):
                self._download_unsupported_with_auto_fallback(
                    item_id,
                    url,
                    output_folder,
                    cookie_file,
                    optimize_mode,
                    advanced_options,
                    error_message,
                )
                return
            self.handle_download_done(item_id, success=False, error=error_message)
        else:
            self.handle_download_done(item_id, success=True)

    def _download_unsupported_with_auto_fallback(
        self,
        item_id: int,
        url: str,
        output_folder: str,
        cookie_file: str | None,
        optimize_mode: str,
        advanced_options: dict[str, Any],
        original_error: str,
    ) -> None:
        def unsupported_probe(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError(original_error)

        def event_callback(event: dict[str, Any]) -> None:
            self.handle_auto_event(item_id, event)

        def status_callback(status_text: str, color: str = "blue") -> None:
            self.handle_download_status(item_id, status_text, color)

        browser_name = str(advanced_options.get("browserName") or self.browser_name or "coccoc")
        browser_profile = str(advanced_options.get("browserProfile") or self.browser_profile or "")
        self.handle_download_status(
            item_id,
            f"yt-dlp không hỗ trợ URL này, chuyển sang bắt media bằng {browser_name}.",
            "orange",
        )

        try:
            run_auto_download(
                url,
                output_folder,
                cookie_file=cookie_file,
                optimize_mode=optimize_mode,
                advanced_options=advanced_options,
                browser_name=browser_name,
                browser_profile=browser_profile,
                browser_mode="visible",
                sniff_timeout_seconds=45,
                auto_select=True,
                event_callback=event_callback,
                status_callback=status_callback,
                analyzer=unsupported_probe,
                downloader=self.downloader,
                sniffer=self.sniffer or sniff_media_urls,
            )
        except Exception as fallback_error:
            self.handle_auto_event(item_id, {"stage": "failed", "message": str(fallback_error), "level": "error", "progress": 100})
            self.handle_download_done(item_id, success=False, error=str(fallback_error))
        else:
            self.handle_auto_event(item_id, {"stage": "done", "message": "Auto fallback hoàn tất.", "level": "success", "progress": 100})
            self.handle_download_done(item_id, success=True)

    def handle_download_status(self, item_id: int, message: str, color: str = "blue") -> None:
        with self.lock:
            item = self.items_by_id.get(item_id)
            if not item:
                return

            clean_message = self._sanitize_message(message)
            parsed = self._parse_download_message(clean_message)

            if parsed["status"]:
                item.status = parsed["status"]
            if parsed["progress"] is not None:
                item.progress = parsed["progress"]
            if parsed["speed"]:
                item.speed = parsed["speed"]
            if parsed["eta"]:
                item.eta = parsed["eta"]

            item.update_text = self._build_update_text(item, clean_message)
            if self._should_log_status(item, clean_message, parsed["progress"]):
                log_limit = 320 if "cookie database" in clean_message.lower() else 140
                self._append_log_locked(f"#{item.item_id} {self._compact_text(clean_message, log_limit)}", self._tag_from_color(color))
            self._publish_locked()

    def handle_download_done(self, item_id: int, success: bool, error: str = "") -> None:
        with self.lock:
            item = self.items_by_id.get(item_id)
            if not item:
                return

            self.batch_processed += 1
            if success:
                self.batch_success += 1
                item.status = STATUS_DONE
                item.progress = 100
                item.error = ""
                item.update_text = "Đã tải xong"
                self._append_log_locked(f"#{item.item_id} Hoàn tất: {self._compact_text(item.url, 100)}", "success")
            else:
                self.batch_failed += 1
                item.status = STATUS_FAILED
                item.error = error
                item.update_text = self._compact_text(error, 80) or "Tải thất bại"
                self.batch_errors.append(f"#{item.item_id}: {self._compact_text(error, 140)}")
                self._append_log_locked(f"#{item.item_id} Lỗi: {self._compact_text(error, 180)}", "error")

            if self.batch_running and self.batch_processed >= self.batch_total:
                self.batch_running = False
                if self.batch_failed:
                    self._append_log_locked(
                        f"Lô tải hoàn tất: {self.batch_success} thành công, {self.batch_failed} lỗi.",
                        "warning",
                    )
                else:
                    self._append_log_locked(f"Lô tải hoàn tất: {self.batch_success} mục thành công.", "success")

            self._publish_locked()

    def handle_auto_event(self, item_id: int, event: dict[str, Any]) -> None:
        with self.lock:
            stage = str(event.get("stage") or self.auto_job.get("stage") or "idle")
            message = self._sanitize_message(str(event.get("message") or ""))
            level = str(event.get("level") or "info")
            progress = event.get("progress")
            if isinstance(progress, (int, float)):
                progress_value = max(0, min(100, int(progress)))
            else:
                progress_value = int(self.auto_job.get("progress") or 0)

            candidates = event.get("candidates")
            if isinstance(candidates, list):
                self._store_candidates_locked({"mediaUrls": candidates, "diagnostics": event.get("diagnostics") or self.diagnostics})
            diagnostics = event.get("diagnostics")
            if isinstance(diagnostics, list):
                self.diagnostics = [dict(item) for item in diagnostics if isinstance(item, dict)]

            selected = event.get("selectedCandidate")
            selected_id = ""
            if isinstance(selected, dict):
                selected_id = str(selected.get("id") or "")
                self._merge_candidate_locked(selected)
            elif self.auto_job.get("selectedCandidateId"):
                selected_id = str(self.auto_job.get("selectedCandidateId") or "")

            running = stage not in {"done", "failed", "idle"}
            self.auto_job = {
                **self.auto_job,
                "running": running,
                "stage": stage,
                "message": message or str(self.auto_job.get("message") or ""),
                "level": level,
                "progress": progress_value,
                "itemId": item_id,
                "selectedCandidateId": selected_id,
                "updatedAt": datetime.now().isoformat(timespec="seconds"),
            }

            item = self.items_by_id.get(item_id)
            if item:
                if stage in {"probing", "sniffing", "selecting", "retrying"}:
                    item.status = STATUS_PROCESSING
                elif stage == "downloading":
                    item.status = STATUS_RUNNING
                elif stage == "done":
                    item.status = STATUS_DONE
                    item.progress = 100
                elif stage == "failed":
                    item.status = STATUS_FAILED
                if progress_value:
                    item.progress = progress_value
                if message:
                    item.update_text = self._compact_text(message, 80)

            if message:
                self._append_log_locked(f"Auto/{stage}: {self._compact_text(message, 180)}", level if level in {"info", "success", "warning", "error"} else "info")
            self._publish_locked()

    def _snapshot_locked(self) -> dict[str, Any]:
        counts = {
            STATUS_QUEUED: 0,
            STATUS_RUNNING: 0,
            STATUS_PROCESSING: 0,
            STATUS_DONE: 0,
            STATUS_FAILED: 0,
        }
        for item in self.queue_items:
            counts[item.status] = counts.get(item.status, 0) + 1

        total = len(self.queue_items)
        active = counts[STATUS_RUNNING] + counts[STATUS_PROCESSING]
        done = counts[STATUS_DONE]
        auto_running = self._auto_running_locked()
        if auto_running:
            progress_value = int(self.auto_job.get("progress") or 0)
            detail = str(self.auto_job.get("stage") or "auto")
        elif self.batch_running and self.batch_total:
            progress_value = round((self.batch_processed / self.batch_total) * 100)
            detail = f"{self.batch_processed} / {self.batch_total}"
        elif total:
            progress_value = round((done / total) * 100)
            detail = f"{done} / {total}"
        else:
            progress_value = 0
            detail = "0 / 0"

        summary = (
            f"Tổng {total} · Chờ {counts[STATUS_QUEUED]} · Đang tải {active} · "
            f"Hoàn tất {done} · Lỗi {counts[STATUS_FAILED]}"
        )
        if auto_running:
            footer_status = str(self.auto_job.get("message") or "Dang tai tu dong...")
        elif self.batch_running:
            footer_status = "Đang tải..."
        elif self.batch_total and self.batch_processed >= self.batch_total:
            footer_status = "Hoàn tất với lỗi." if self.batch_failed else "Đã tải xong tất cả mục."
        else:
            footer_status = "Sẵn sàng."

        return {
            "summary": summary,
            "footerStatus": footer_status,
            "running": self.batch_running or auto_running,
            "autoRunning": auto_running,
            "canStart": (not self.batch_running and not auto_running) and any(item.status in (STATUS_QUEUED, STATUS_FAILED) for item in self.queue_items),
            "items": [item.to_dict(index + 1) for index, item in enumerate(self.queue_items)],
            "logs": list(self.logs),
            "autoJob": dict(self.auto_job),
            "lastCandidates": list(self.last_candidates),
            "diagnostics": list(self.diagnostics),
            "progress": {"value": progress_value, "detail": detail},
            "batch": {
                "total": self.batch_total,
                "processed": self.batch_processed,
                "success": self.batch_success,
                "failed": self.batch_failed,
                "errors": list(self.batch_errors),
            },
            "config": {
                "outputFolder": self.output_folder,
                "useCookies": self.use_cookies,
                "cookieFile": self.cookie_file,
                "optimizeMode": self.optimize_mode,
                "useBrowserCookies": self.use_browser_cookies,
                "browserName": self.browser_name,
                "browserProfile": self.browser_profile,
                "browserKeyring": self.browser_keyring,
                "browserContainer": self.browser_container,
                "proxy": self.proxy,
                "impersonate": self.impersonate,
                "downloadArchive": self.download_archive,
                "checkFormats": self.check_formats,
                "formatSort": self.format_sort,
                "concurrentFragments": self.concurrent_fragments,
                "skipUnavailableFragments": self.skip_unavailable_fragments,
            },
            "capabilities": dict(self.capabilities),
            "browserCapabilities": dict(self.browser_capabilities),
            "dependencies": dict(self.dependencies),
        }

    def _reset_batch_counters_locked(self) -> None:
        self.batch_running = False
        self.batch_total = 0
        self.batch_processed = 0
        self.batch_success = 0
        self.batch_failed = 0
        self.batch_errors = []
        if not self._auto_running_locked():
            self.auto_job = self._empty_auto_job()

    def _auto_running_locked(self) -> bool:
        return bool(self.auto_job.get("running"))

    def _store_candidates_locked(self, result: dict[str, Any]) -> None:
        candidates = [dict(item) for item in result.get("mediaUrls", []) if isinstance(item, dict)]
        self._last_candidates_full = candidates
        self.last_candidates = [self._redact_candidate_for_client(candidate) for candidate in candidates]
        diagnostics = result.get("diagnostics")
        if isinstance(diagnostics, list):
            self.diagnostics = [dict(item) for item in diagnostics if isinstance(item, dict)]

    def _merge_candidate_locked(self, candidate: dict[str, Any]) -> None:
        candidate_id = str(candidate.get("id") or "")
        if not candidate_id:
            return
        replaced = False
        for index, existing in enumerate(self._last_candidates_full):
            if str(existing.get("id") or "") == candidate_id:
                self._last_candidates_full[index] = {**existing, **candidate}
                replaced = True
                break
        if not replaced:
            self._last_candidates_full.append(candidate)
        self.last_candidates = [self._redact_candidate_for_client(item) for item in self._last_candidates_full]

    def _find_candidate_locked(self, candidate_id: str = "", candidate_url: str = "") -> dict[str, Any] | None:
        for candidate in self._last_candidates_full:
            if candidate_id and str(candidate.get("id") or "") == candidate_id:
                return dict(candidate)
            if candidate_url and str(candidate.get("url") or "") == candidate_url:
                return dict(candidate)
        return None

    def _public_sniff_result(self, result: dict[str, Any]) -> dict[str, Any]:
        public_result = dict(result)
        public_result["mediaUrls"] = [self._redact_candidate_for_client(candidate) for candidate in result.get("mediaUrls", []) if isinstance(candidate, dict)]
        return public_result

    def _redact_candidate_for_client(self, candidate: dict[str, Any]) -> dict[str, Any]:
        public = dict(candidate)
        public["requestHeaders"] = self._redact_headers_for_client(candidate.get("requestHeaders") or {})
        public["responseHeaders"] = self._redact_headers_for_client(candidate.get("responseHeaders") or {})
        return public

    def _redact_headers_for_client(self, headers: Any) -> dict[str, str]:
        if not isinstance(headers, dict):
            return {}
        redacted = {}
        for name, value in headers.items():
            lowered = str(name).lower()
            if lowered in {"cookie", "authorization", "proxy-authorization"} or "token" in lowered or "secret" in lowered:
                redacted[str(name)] = "<redacted>"
            else:
                redacted[str(name)] = str(value)
        return redacted

    def _publish_locked(self) -> None:
        self.event_version += 1
        payload = {"version": self.event_version, "state": self._snapshot_locked()}
        for subscriber in list(self.subscribers):
            self._offer_event(subscriber, payload)

    def _offer_event(self, subscriber: queue.Queue[dict[str, Any]], payload: dict[str, Any]) -> None:
        try:
            subscriber.put_nowait(payload)
        except queue.Full:
            try:
                subscriber.get_nowait()
            except queue.Empty:
                pass
            try:
                subscriber.put_nowait(payload)
            except queue.Full:
                pass

    def _append_log_locked(self, message: str, level: str = "info") -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.logs.append({"timestamp": timestamp, "message": message, "level": level})
        if len(self.logs) > MAX_LOG_ITEMS:
            del self.logs[: len(self.logs) - MAX_LOG_ITEMS]

    def _clean_url(self, value: str) -> str:
        text = (value or "").strip()
        text = re.sub(r"^\s*(?:[-*•]\s*)?\d*\.?\s*", "", text)
        if not text:
            return ""
        if "@" in text:
            text = text.split("@", 1)[0].strip()
        if text.lower().startswith("www."):
            text = f"https://{text}"
        return text

    def _is_valid_url(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    def _extract_host(self, url: str) -> str:
        return urlparse(url).netloc or "Không rõ"

    def _clean_path(self, value: str) -> str:
        path = (value or "").strip()
        if "@" in path:
            path = path.split("@", 1)[0].strip()
        if path.startswith("~"):
            path = os.path.expanduser(path)
        elif "%USERPROFILE%" in path:
            path = path.replace("%USERPROFILE%", os.path.expanduser("~"))
        return os.path.normpath(path) if path else ""

    def _sanitize_message(self, message: str) -> str:
        clean = (message or "").replace("\r", " ").replace("\n", " ").strip()
        clean = re.sub(r"^[^\wÀ-ỹ]+", "", clean).strip()
        return re.sub(r"\s+", " ", clean)

    def _compact_text(self, text: str, limit: int) -> str:
        text = (text or "").strip()
        if len(text) <= limit:
            return text
        return f"{text[: max(0, limit - 3)]}..."

    def _clean_error_message(self, message: str) -> str:
        clean = self._sanitize_message(message)
        while clean.lower().startswith("error:"):
            clean = clean[6:].strip()
        if clean.lower().startswith("loi server:"):
            clean = clean.split(":", 1)[1].strip()
        return clean

    def _parse_download_message(self, message: str) -> dict[str, Any]:
        lower = message.lower()
        progress = None
        speed = None
        eta = None
        status = None

        progress_match = re.search(r"(\d+(?:[.,]\d+)?)\s*%", message)
        if progress_match:
            progress = int(float(progress_match.group(1).replace(",", ".")))
            progress = max(0, min(100, progress))

        speed_match = re.search(r"(?:Tốc độ|Speed):\s*([^|]+)", message, flags=re.IGNORECASE)
        if speed_match:
            speed = speed_match.group(1).strip()

        eta_match = re.search(r"(?:Còn lại|ETA):\s*([^|]+)", message, flags=re.IGNORECASE)
        if eta_match:
            eta = eta_match.group(1).strip()

        if "đang tải" in lower or "dang tai" in lower or "downloading" in lower:
            status = STATUS_RUNNING
        elif "hoàn tất" in lower or "hoan tat" in lower or "finished" in lower:
            status = STATUS_DONE
            progress = 100
        elif "sharepoint" in lower or "throttle" in lower or "cookie" in lower or "ffmpeg" in lower:
            status = STATUS_PROCESSING
        elif "lỗi" in lower or "loi" in lower or "error" in lower:
            status = STATUS_PROCESSING

        return {"status": status, "progress": progress, "speed": speed, "eta": eta}

    def _should_log_status(self, item: QueueItem, message: str, progress: int | None) -> bool:
        lower = message.lower()
        important_tokens = ("cookie", "ffmpeg", "sharepoint", "throttle", "lỗi", "loi", "error", "warning", "hoàn tất", "hoan tat")
        if any(token in lower for token in important_tokens):
            return True
        if progress is None:
            return False
        bucket = progress // 25
        if bucket != item.last_logged_bucket:
            item.last_logged_bucket = bucket
            return True
        return False

    def _build_update_text(self, item: QueueItem, fallback_message: str) -> str:
        parts = []
        if item.speed and item.speed != "-":
            parts.append(item.speed)
        if item.eta and item.eta != "-":
            parts.append(f"Còn {item.eta}")
        if parts:
            return " · ".join(parts)
        return self._compact_text(fallback_message, 68)

    def _tag_from_color(self, color: str) -> str:
        return {
            "green": "success",
            "orange": "warning",
            "red": "error",
            "blue": "info",
        }.get(color, "info")


class VideoDownloaderHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], app_state: AppState | None = None):
        super().__init__(server_address, VideoDownloaderRequestHandler)
        self.app_state = app_state or AppState()


class VideoDownloaderRequestHandler(BaseHTTPRequestHandler):
    server_version = "VideoDownloaderWeb/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def app_state(self) -> AppState:
        return self.server.app_state  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in {"/", "/index.html"}:
            self._serve_static("index.html")
            return
        if path == "/favicon.ico":
            self._serve_file(FAVICON_PATH)
            return
        if path.startswith("/static/"):
            self._serve_static(unquote(path.removeprefix("/static/")))
            return
        if path == "/api/state":
            self._send_json({"ok": True, "state": self.app_state.snapshot()})
            return
        if path == "/api/events":
            self._handle_events()
            return
        self._send_error(HTTPStatus.NOT_FOUND, "Không tìm thấy endpoint.")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            if path == "/api/dialog/output-folder":
                selected_path = choose_output_folder(payload.get("initialPath"))
                self._send_json({"ok": True, "path": selected_path})
                return
            if path == "/api/dialog/cookie-file":
                selected_path = choose_cookie_file(payload.get("initialPath"))
                self._send_json({"ok": True, "path": selected_path})
                return
            if path == "/api/analyze":
                result = self.app_state.analyze_url(payload)
                self._send_json({"ok": True, "result": result, "state": self.app_state.snapshot()})
                return
            if path == "/api/media/sniff":
                result = self.app_state.sniff_media(payload)
                self._send_json({"ok": True, "result": result, "state": self.app_state.snapshot()})
                return
            if path == "/api/media/inspect":
                result = self.app_state.inspect_media(payload)
                self._send_json({"ok": True, "result": result, "state": self.app_state.snapshot()})
                return
            if path == "/api/media/download":
                result = self.app_state.start_candidate_download(payload)
                self._send_json({"ok": True, **result})
                return
            if path == "/api/queue/add":
                urls = self._extract_urls(payload)
                result = self.app_state.add_urls(urls, str(payload.get("source") or "web"))
                self._send_json({"ok": True, "result": result, "state": self.app_state.snapshot()})
                return
            if path == "/api/queue/remove":
                result = self.app_state.remove_items(self._extract_item_ids(payload))
                self._send_json({"ok": True, "result": result, "state": self.app_state.snapshot()})
                return
            if path == "/api/queue/clear":
                result = self.app_state.clear_queue()
                self._send_json({"ok": True, "result": result, "state": self.app_state.snapshot()})
                return
            if path == "/api/download/start":
                result = self.app_state.start_download(payload)
                self._send_json({"ok": True, **result})
                return
            if path == "/api/download/auto":
                result = self.app_state.start_auto_download(payload)
                self._send_json({"ok": True, **result})
                return
            if path == "/api/setup/ensure":
                result = self.app_state.run_setup()
                self._send_json({"ok": True, "dependencies": result, "state": self.app_state.snapshot()})
                return
            if path == "/api/log/clear":
                result = self.app_state.clear_log()
                self._send_json({"ok": True, "result": result, "state": self.app_state.snapshot()})
                return
            self._send_error(HTTPStatus.NOT_FOUND, "Không tìm thấy endpoint.")
        except AppError as exc:
            self._send_error(exc.status_code, exc.message)
        except json.JSONDecodeError:
            self._send_error(HTTPStatus.BAD_REQUEST, "JSON không hợp lệ.")
        except Exception as exc:
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Lỗi server: {exc}")

    def _handle_events(self) -> None:
        subscriber = self.app_state.subscribe()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        try:
            while True:
                try:
                    payload = subscriber.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self._write_sse("state", payload)
        except (BrokenPipeError, ConnectionError, OSError):
            pass
        finally:
            self.app_state.unsubscribe(subscriber)

    def _write_sse(self, event_name: str, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        frame = f"event: {event_name}\ndata: {data}\n\n".encode("utf-8")
        self.wfile.write(frame)
        self.wfile.flush()

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _extract_urls(self, payload: dict[str, Any]) -> list[str]:
        urls: list[str] = []
        url = payload.get("url")
        if isinstance(url, str):
            urls.append(url)
        raw_urls = payload.get("urls")
        if isinstance(raw_urls, list):
            urls.extend(str(item) for item in raw_urls)
        text = payload.get("text")
        if isinstance(text, str):
            urls.extend(text.splitlines())
        return urls

    def _extract_item_ids(self, payload: dict[str, Any]) -> list[int]:
        raw_ids = payload.get("ids", [])
        if not isinstance(raw_ids, list):
            raw_ids = [raw_ids]
        item_ids = []
        for raw_id in raw_ids:
            try:
                item_ids.append(int(raw_id))
            except (TypeError, ValueError):
                continue
        return item_ids

    def _serve_static(self, relative_path: str) -> None:
        try:
            target = (STATIC_DIR / relative_path).resolve()
            if STATIC_DIR.resolve() not in target.parents and target != STATIC_DIR.resolve():
                self._send_error(HTTPStatus.FORBIDDEN, "Đường dẫn static không hợp lệ.")
                return
        except OSError:
            self._send_error(HTTPStatus.NOT_FOUND, "Không tìm thấy static file.")
            return
        self._serve_file(target)

    def _serve_file(self, path: Path) -> None:
        if not path.is_file():
            self._send_error(HTTPStatus.NOT_FOUND, "Không tìm thấy file.")
            return
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict[str, Any], status_code: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status_code: int, message: str) -> None:
        self._send_json({"ok": False, "error": message}, status_code)

    def log_message(self, format: str, *args: Any) -> None:
        return


def create_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, app_state: AppState | None = None) -> VideoDownloaderHTTPServer:
    return VideoDownloaderHTTPServer((host, port), app_state=app_state)


def run_local_web_ui(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, open_browser: bool = True) -> int:
    server = _bind_server(host, port)
    actual_port = server.server_address[1]
    display_host = "127.0.0.1" if host in {"0.0.0.0", ""} else host
    url = f"http://{display_host}:{actual_port}/"

    print("Starting Video Downloader Web UI...")
    print(f"URL: {url}")
    print("Press Ctrl+C to stop.")

    if open_browser:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutdown requested. Stopping web server...")
    finally:
        server.shutdown()
        server.server_close()
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local web UI for Video Downloader Tool.")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Host to bind. Default: {DEFAULT_HOST}")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port to bind. Default: {DEFAULT_PORT}")
    parser.add_argument("--no-open", action="store_true", help="Do not open the browser automatically.")
    return parser


def _bind_server(host: str, port: int) -> VideoDownloaderHTTPServer:
    last_error: OSError | None = None
    for candidate_port in _candidate_ports(port):
        try:
            return create_server(host, candidate_port)
        except OSError as exc:
            if not _is_address_in_use(exc):
                raise
            last_error = exc
    raise OSError(f"Could not bind local web server. Last error: {last_error}")


def _candidate_ports(port: int) -> list[int]:
    if port == 0:
        return [0]
    if port == DEFAULT_PORT:
        return list(range(DEFAULT_PORT, DEFAULT_PORT_MAX + 1))
    return [port]


def _is_address_in_use(exc: OSError) -> bool:
    return getattr(exc, "errno", None) == errno.EADDRINUSE or getattr(exc, "winerror", None) == 10048


def choose_output_folder(initial_path: Any = None) -> str:
    tk, filedialog = _load_tk_dialogs()
    root = _create_dialog_root(tk)
    try:
        selected = filedialog.askdirectory(parent=root, title="Chọn thư mục lưu", initialdir=_dialog_initial_dir(initial_path))
        return str(selected or "")
    finally:
        root.destroy()

def choose_cookie_file(initial_path: Any = None) -> str:
    tk, filedialog = _load_tk_dialogs()
    root = _create_dialog_root(tk)
    try:
        initial_file = Path(str(initial_path)).name if initial_path else ""
        selected = filedialog.askopenfilename(
            parent=root,
            title="Chọn cookie file",
            initialdir=_dialog_initial_dir(initial_path),
            initialfile=initial_file,
            filetypes=[
                ("Cookie files", "*.txt *.json"),
                ("Text files", "*.txt"),
                ("JSON files", "*.json"),
                ("All files", "*.*"),
            ],
        )
        return str(selected or "")
    finally:
        root.destroy()

def _load_tk_dialogs() -> tuple[Any, Any]:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:
        raise AppError(HTTPStatus.SERVICE_UNAVAILABLE, f"Không mở được hộp chọn file: {exc}") from exc
    return tk, filedialog

def _create_dialog_root(tk: Any) -> Any:
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        root.update()
        return root
    except Exception as exc:
        raise AppError(HTTPStatus.SERVICE_UNAVAILABLE, f"Không mở được hộp chọn file: {exc}") from exc

def _dialog_initial_dir(initial_path: Any = None) -> str:
    raw_path = str(initial_path or "").strip()
    if raw_path:
        expanded = os.path.expandvars(os.path.expanduser(raw_path))
        if os.path.isdir(expanded):
            return expanded
        parent = os.path.dirname(expanded)
        if parent and os.path.isdir(parent):
            return parent
    return str(Path.home())

def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    return run_local_web_ui(host=args.host, port=args.port, open_browser=not args.no_open)


__all__ = ["AppState", "create_server", "run_local_web_ui", "main", "choose_output_folder", "choose_cookie_file"]
