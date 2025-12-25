# core/downloader.py
import os
import re
import subprocess
import sys
import time
from functools import lru_cache
from importlib import util as importlib_util
from pathlib import Path
from urllib.parse import unquote, urlparse, urlsplit, urlunsplit, parse_qsl, urlencode

try:
    import requests
except ImportError:
    requests = None
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError
try:
    from yt_dlp.cookies import SUPPORTED_BROWSERS as YT_DLP_SUPPORTED_BROWSERS
except Exception:
    YT_DLP_SUPPORTED_BROWSERS = {"brave", "chrome", "chromium", "edge", "firefox", "opera", "safari", "vivaldi"}
try:
    from yt_dlp.networking.impersonate import ImpersonateTarget
except Exception:
    ImpersonateTarget = None
try:
    from yt_dlp.version import __version__ as YT_DLP_VERSION
except Exception:
    YT_DLP_VERSION = "unknown"

from .config import (
    DOWNLOAD_CONFIG,
    FFMPEG_CONFIG,
    QUALITY_OPTIMIZED_CONFIG,
    SPEED_OPTIMIZED_CONFIG,
)

# Add the project root to the path for absolute imports
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Import cookie utilities with fallback
try:
    from utils.cookies import (
        convert_cookies_to_yt_dlp_format,
        is_valid_cookie_file,
        load_cookie_jar,
        validate_sharepoint_cookies,
    )
except ImportError:
    # Fallback functions if cookie utilities can't be imported
    def is_valid_cookie_file(path):
        """Fallback: just check if file exists"""
        return os.path.exists(path) if path else False
    
    def convert_cookies_to_yt_dlp_format(cookie_path):
        """Fallback: return basic cookie format"""
        return {'cookiefile': cookie_path} if cookie_path else {}
    
    def load_cookie_jar(cookie_path):
        return None
    
    def validate_sharepoint_cookies(cookie_path, url):
        """Fallback: skip validation"""
        return True, [], "Validation skipped (import error)"


@lru_cache(maxsize=1)
def check_ffmpeg_available():
    """
    Kiểm tra xem ffmpeg có sẵn không
    """
    try:
        # Refresh PATH from system environment to include newly installed programs
        import os
        if os.name == 'nt':  # Windows
            import winreg
            # Get system PATH
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment") as key:
                system_path = winreg.QueryValueEx(key, "PATH")[0]
            # Get user PATH
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
                    user_path = winreg.QueryValueEx(key, "PATH")[0]
            except FileNotFoundError:
                user_path = ""
            # Update current process PATH
            current_path = os.environ.get('PATH', '')
            combined_path = f"{system_path};{user_path}"
            if combined_path not in current_path:
                os.environ['PATH'] = f"{combined_path};{current_path}"
        
        subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True, timeout=5)
        return True
    except Exception:
        return False


def _build_headers_for_url(url, base_headers=None):
    """
    Thêm Origin/Referer và header đặc thù cho SharePoint để tránh bị chặn
    """
    headers = dict(base_headers or {})
    try:
        parsed = urlparse(url)
    except ValueError:
        return headers

    hostname = (parsed.hostname or '').lower()
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else None

    if origin:
        headers.setdefault('Origin', origin)
        headers.setdefault('Referer', url if parsed.path else f"{origin}/")

    if 'sharepoint.com' in hostname:
        headers.setdefault('Sec-Fetch-Site', 'same-origin')
        headers.setdefault('Sec-Fetch-Mode', 'navigate')
        headers.setdefault('Sec-Fetch-Dest', 'document')
        headers.setdefault('Sec-CH-UA-Platform', '"Windows"')
        headers.setdefault('Pragma', 'no-cache')
        headers.setdefault('Cache-Control', 'no-cache')

    return headers


def _resolve_sharepoint_throttle(url, headers, cookie_jar=None):
    """
    Try to extract the original :v:/... link from Throttle.htm when possible
    """
    if requests is None:
        return None
    try:
        response = requests.get(
            url,
            headers=headers,
            cookies=cookie_jar,
            timeout=10,
        )
        response.raise_for_status()
        match = re.search(r'g_spThrottleRedirectUrl\s*=\s*["\']([^"\']+)["\']', response.text)
        if match:
            return unquote(match.group(1))
    except Exception:
        return None
    return None


def _normalize_sharepoint_url(url):
    """
    Convert :v:/r links to :v:/g (works outside browser) and ensure proper encoding.
    Preserve original query string (no re-encoding) to avoid breaking signed URLs.
    Only append download=1 for video file extensions when missing.
    """
    try:
        split = urlsplit(url)
    except ValueError:
        return url

    path = split.path

    # Convert :v:/r/ to :v:/g/ for better compatibility
    if ':v:/r/' in path:
        path = path.replace(':v:/r/', ':v:/g/', 1)
    elif ':v:/g/' not in path and '/:v:/' in path:
        # If it has :v:/ but not r or g, try to convert
        path = path.replace('/:v:/', '/:v:/g/', 1)

    # Replace any stray whitespace in path with %20 to avoid malformed URLs
    if ' ' in path:
        path = path.replace(' ', '%20')

    video_extensions = ('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v')
    is_video = any(path.lower().endswith(ext) for ext in video_extensions)

    query = split.query or ''
    if is_video and 'download=' not in query:
        if query:
            query = query + '&download=1'
        else:
            query = 'download=1'

    normalized = urlunsplit(split._replace(path=path, query=query))
    return normalized


def _ensure_download_param(url):
    """
    Ensure download=1 exists in query (without altering other params or encoding).
    Useful for some SharePoint direct file links.
    """
    try:
        split = urlsplit(url)
    except ValueError:
        return url
    if 'download=' in (split.query or ''):
        return url
    query = (split.query + '&download=1') if split.query else 'download=1'
    return urlunsplit(split._replace(query=query))

@lru_cache(maxsize=1)
def get_runtime_capabilities():
    return {
        "ytDlpVersion": YT_DLP_VERSION,
        "ffmpegAvailable": check_ffmpeg_available(),
        "curlCffiAvailable": importlib_util.find_spec("curl_cffi") is not None,
        "brotliAvailable": importlib_util.find_spec("brotli") is not None or importlib_util.find_spec("brotlicffi") is not None,
        "browserCookiesAvailable": True,
        "impersonationAvailable": ImpersonateTarget is not None,
        "supportedBrowsers": sorted(YT_DLP_SUPPORTED_BROWSERS),
        "optionalDependencies": {
            "curl_cffi": importlib_util.find_spec("curl_cffi") is not None,
            "brotli": importlib_util.find_spec("brotli") is not None or importlib_util.find_spec("brotlicffi") is not None,
            "requests": requests is not None,
        },
    }

def _clean_optional_text(value):
    text = str(value or "").strip()
    return text

def _split_csv_like(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    if "\n" in text:
        parts = text.splitlines()
    else:
        parts = re.split(r"[;,]", text)
    return [part.strip() for part in parts if part.strip()]

def _normalize_http_headers(value):
    if not value:
        return {}
    if isinstance(value, dict):
        return {
            str(name).strip(): str(header_value)
            for name, header_value in value.items()
            if str(name).strip() and header_value is not None
        }
    if isinstance(value, (list, tuple)):
        lines = [str(item) for item in value]
    else:
        lines = str(value).splitlines()
    headers = {}
    for line in lines:
        if ":" not in line:
            continue
        name, header_value = line.split(":", 1)
        name = name.strip()
        if name:
            headers[name] = header_value.strip()
    return headers

def _coerce_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y", "t"}:
        return True
    if text in {"0", "false", "no", "off", "n", "f"}:
        return False
    return default

def _coerce_optional_int(value, minimum=1, maximum=None):
    if value in (None, ""):
        return None
    try:
        parsed = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed

def _bounded_retry_sleep(base_seconds, maximum_seconds):
    def sleep_seconds(attempt):
        try:
            attempt_number = int(attempt)
        except (TypeError, ValueError):
            attempt_number = 1
        return min(maximum_seconds, base_seconds * (2 ** max(0, attempt_number - 1)))
    return sleep_seconds

def _build_retry_sleep_functions():
    return {
        "http": _bounded_retry_sleep(1.0, 20.0),
        "fragment": _bounded_retry_sleep(0.5, 12.0),
        "extractor": _bounded_retry_sleep(1.0, 15.0),
    }

def _normalize_format_sort(value):
    return _split_csv_like(value)

_COOKIES_FROM_BROWSER_RE = re.compile(r"""(?x)
    (?P<name>[^+:]+)
    (?:\s*\+\s*(?P<keyring>[^:]+))?
    (?:\s*:\s*(?!:)(?P<profile>.+?))?
    (?:\s*::\s*(?P<container>.+))?
""")

def _build_cookiesfrombrowser_spec(options):
    explicit_spec = options.get("cookies_from_browser_spec")
    if explicit_spec:
        if isinstance(explicit_spec, (list, tuple)):
            parts = [None if item is None or str(item).strip() == "" else str(item).strip() for item in explicit_spec]
            if not parts:
                return None
            parts[0] = str(parts[0]).lower()
            while len(parts) < 4:
                parts.append(None)
            if parts[2]:
                parts[2] = str(parts[2]).upper()
            return tuple(parts[:4])

        spec_text = _clean_optional_text(explicit_spec)
        match = _COOKIES_FROM_BROWSER_RE.fullmatch(spec_text)
        if not match:
            raise ValueError(f"cookies_from_browser_spec khong hop le: {spec_text}")
        browser_name, keyring, profile, container = match.group("name", "keyring", "profile", "container")
        return (
            browser_name.lower(),
            profile.strip() if profile else None,
            keyring.strip().upper() if keyring else None,
            container.strip() if container else None,
        )

    if not _coerce_bool(options.get("use_browser_cookies")):
        return None
    browser_name = _clean_optional_text(options.get("browser_name") or options.get("browserName") or "chrome")
    if not browser_name:
        return None
    keyring = _clean_optional_text(options.get("browser_keyring") or options.get("browserKeyring"))
    profile = _clean_optional_text(options.get("browser_profile") or options.get("browserProfile"))
    container = _clean_optional_text(options.get("browser_container") or options.get("browserContainer"))
    return (
        browser_name.lower(),
        profile or None,
        keyring.upper() if keyring else None,
        container or None,
    )

def _format_cookiesfrombrowser_spec(spec):
    if not spec:
        return ""
    parts = list(spec)
    while len(parts) < 4:
        parts.append(None)
    browser_name, profile, keyring, container = parts[:4]
    display = str(browser_name)
    if keyring:
        display += f"+{keyring}"
    if profile:
        display += f":{profile}"
    if container:
        display += f"::{container}"
    return display

def _strip_ytdlp_error_prefix(message):
    text = str(message or "").strip()
    while text.lower().startswith("error:"):
        text = text[6:].strip()
    return text

def _is_browser_cookie_database_error(message):
    text = _strip_ytdlp_error_prefix(message).lower()
    return "could not copy chrome cookie database" in text

def _browser_cookie_database_error_message(browser_cookie_spec):
    browser_display = _format_cookiesfrombrowser_spec(browser_cookie_spec) or "trinh duyet"
    return (
        f"Khong doc duoc cookies truc tiep tu {browser_display} vi cookie database dang bi khoa. "
        "Neu UI dang mo bang cung trinh duyet nay, hay mo UI bang trinh duyet khac roi dong trinh duyet can lay cookies, "
        "hoac xuat cookies ra file .txt/.json va dung muc Cookie file."
    )

def _is_unsupported_url_error(message):
    text = _strip_ytdlp_error_prefix(message).lower()
    return "unsupported url" in text or "no suitable extractor" in text

def _unsupported_url_error_message(source_url):
    return (
        f"Nguon/link nay hien khong duoc yt-dlp ho tro: {source_url}. "
        "Hay dung URL tu website nam trong danh sach yt-dlp ho tro, URL media truc tiep (.mp4/.m3u8/.mpd), "
        "hoac link chia se co extractor hop le. Tool khong scraper rieng cho tung site/DRM/CAPTCHA."
    )

def _normalize_impersonate_target(value):
    if not value:
        return None
    if ImpersonateTarget is None:
        return None
    if isinstance(value, ImpersonateTarget):
        return value
    text = _clean_optional_text(value)
    if not text:
        return None
    return ImpersonateTarget.from_str(text.lower())

def _normalize_advanced_options(advanced_options):
    source = dict(advanced_options or {})
    normalized = {
        "use_browser_cookies": _coerce_bool(source.get("use_browser_cookies") or source.get("useBrowserCookies")),
        "cookies_from_browser_spec": _clean_optional_text(source.get("cookies_from_browser_spec") or source.get("cookiesFromBrowserSpec")),
        "browser_name": _clean_optional_text(source.get("browser_name") or source.get("browserName") or "chrome"),
        "browser_profile": _clean_optional_text(source.get("browser_profile") or source.get("browserProfile")),
        "browser_keyring": _clean_optional_text(source.get("browser_keyring") or source.get("browserKeyring")),
        "browser_container": _clean_optional_text(source.get("browser_container") or source.get("browserContainer")),
        "proxy": _clean_optional_text(source.get("proxy")),
        "impersonate": _clean_optional_text(source.get("impersonate")),
        "download_archive": _clean_optional_text(source.get("download_archive") or source.get("downloadArchive")),
        "check_formats": _coerce_bool(source.get("check_formats") or source.get("checkFormats")),
        "format_sort": _normalize_format_sort(source.get("format_sort") or source.get("formatSort")),
        "concurrent_fragments": _coerce_optional_int(source.get("concurrent_fragments") or source.get("concurrentFragments")),
        "skip_unavailable_fragments": _coerce_bool(source.get("skip_unavailable_fragments") or source.get("skipUnavailableFragments")),
        "http_headers": _normalize_http_headers(
            source.get("http_headers")
            or source.get("httpHeaders")
            or source.get("media_headers")
            or source.get("mediaHeaders")
        ),
        "referer": _clean_optional_text(source.get("referer") or source.get("referrer") or source.get("pageUrl") or source.get("sourceUrl")),
        "user_agent": _clean_optional_text(source.get("user_agent") or source.get("userAgent")),
    }
    if normalized["browser_name"]:
        normalized["browser_name"] = normalized["browser_name"].lower()
    if normalized["download_archive"]:
        normalized["download_archive"] = os.path.normpath(os.path.expandvars(os.path.expanduser(normalized["download_archive"])))
    return normalized

def _apply_context_headers(request_headers, advanced):
    headers = dict(request_headers or {})
    for name, value in (advanced.get("http_headers") or {}).items():
        if name and value is not None:
            headers[str(name)] = str(value)
    if advanced.get("referer"):
        headers["Referer"] = advanced["referer"]
        try:
            parsed = urlparse(advanced["referer"])
            if parsed.scheme and parsed.netloc:
                headers.setdefault("Origin", f"{parsed.scheme}://{parsed.netloc}")
        except ValueError:
            pass
    if advanced.get("user_agent"):
        headers["User-Agent"] = advanced["user_agent"]
    return headers

def _preview_formats(formats, limit=12):
    preview = []
    for fmt in list(formats or [])[:limit]:
        if not isinstance(fmt, dict):
            continue
        preview.append({
            "format_id": fmt.get("format_id"),
            "ext": fmt.get("ext"),
            "resolution": fmt.get("resolution") or fmt.get("height") or fmt.get("format_note"),
            "fps": fmt.get("fps"),
            "tbr": fmt.get("tbr"),
            "vcodec": fmt.get("vcodec"),
            "acodec": fmt.get("acodec"),
            "protocol": fmt.get("protocol"),
            "filesize": fmt.get("filesize") or fmt.get("filesize_approx"),
        })
    return preview

def _summarize_extract_info(info, candidate_url, source_url, requested_urls, ydl_opts, advanced_options, cookie_file):
    formats = info.get("formats") or []
    return {
        "ok": True,
        "title": info.get("title") or info.get("fulltitle") or candidate_url,
        "id": info.get("id") or "",
        "extractor": info.get("extractor") or info.get("ie_key") or "",
        "duration": info.get("duration"),
        "webpageUrl": info.get("webpage_url") or info.get("original_url") or candidate_url,
        "requestedUrl": source_url,
        "candidateUrls": list(requested_urls or []),
        "cookieFileUsed": bool(cookie_file),
        "browserCookiesUsed": bool(_build_cookiesfrombrowser_spec(advanced_options)),
        "proxy": advanced_options.get("proxy") or "",
        "impersonate": advanced_options.get("impersonate") or "",
        "formatCount": len(formats),
        "formats": _preview_formats(formats),
        "bestFormat": info.get("format_id") or info.get("format"),
        "available": bool(formats) or info.get("_type") == "video",
        "ydlOptions": {
            "check_formats": bool(ydl_opts.get("check_formats")),
            "format_sort": list(ydl_opts.get("format_sort") or []),
            "download_archive": ydl_opts.get("download_archive") or "",
            "http_header_names": sorted((ydl_opts.get("http_headers") or {}).keys()),
        },
    }


def download_video(url, output_folder, cookie_file=None, status_callback=None, optimize_mode='balanced', advanced_options=None, dry_run=False, **extra_options):
    """
    Tải video từ URL, sử dụng yt-dlp
    :param url: Đường dẫn video
    :param output_folder: Thư mục lưu video
    :param cookie_file: File cookies.txt hoặc .json nếu cần
    :param status_callback: Hàm callback để cập nhật trạng thái cho UI
    :param optimize_mode: Chế độ tối ưu hóa ('balanced', 'speed', 'quality')
    """
    
    # Kiểm tra ffmpeg
    combined_advanced_options = dict(advanced_options or {})
    if extra_options:
        combined_advanced_options.update(extra_options)
    advanced = _normalize_advanced_options(combined_advanced_options)
    ffmpeg_available = check_ffmpeg_available()
    
    # Chọn cấu hình dựa trên mode và ffmpeg availability
    if optimize_mode == 'speed':
        config = SPEED_OPTIMIZED_CONFIG.copy()
    elif optimize_mode == 'quality':
        config = QUALITY_OPTIMIZED_CONFIG.copy()
    else:
        config = DOWNLOAD_CONFIG.copy()
    
    # Đảm bảo http_headers không bị mutate giữa các lần gọi
    if 'http_headers' in config:
        config['http_headers'] = dict(config['http_headers'])
    config.setdefault('retry_sleep_functions', _build_retry_sleep_functions())
    
    # Nếu có ffmpeg và muốn sử dụng, thêm cấu hình ffmpeg
    if ffmpeg_available and optimize_mode != 'speed':
        ffmpeg_config = dict(FFMPEG_CONFIG)
        # Giữ format ưu tiên 1080p của chế độ quality, không để FFMPEG_CONFIG ghi đè
        if optimize_mode == 'quality':
            ffmpeg_config.pop('format', None)
        config.update(ffmpeg_config)
        if status_callback:
            status_callback("✅ Sử dụng ffmpeg để merge video", "green")
    else:
        # Nếu không có ffmpeg, đảm bảo không dùng format yêu cầu merge
        if optimize_mode == 'quality':
            # Fallback to format không cần merge
            config['format'] = 'best[ext=mp4]/best'
        if status_callback and not ffmpeg_available:
            status_callback("⚠️ ffmpeg không có sẵn, sử dụng format đơn giản", "orange")

    if advanced["concurrent_fragments"] is not None:
        config["concurrent_fragment_downloads"] = advanced["concurrent_fragments"]
    if advanced["skip_unavailable_fragments"]:
        config["skip_unavailable_fragments"] = True
    if advanced["check_formats"]:
        config["check_formats"] = True
    if advanced["format_sort"]:
        config["format_sort"] = advanced["format_sort"]

    def hook(d):
        if d['status'] == 'downloading':
            percent = d.get('_percent_str', '').strip()
            speed = d.get('_speed_str', '').strip()
            eta = d.get('_eta_str', '').strip()
            if status_callback:
                status_text = f"📥 Đang tải: {percent} | Tốc độ: {speed}"
                if eta:
                    status_text += f" | Còn lại: {eta}"
                status_callback(status_text, "blue")
        elif d['status'] == 'finished':
            if status_callback:
                status_callback("✅ Hoàn tất tải video!", "green")

    # Detect SharePoint early
    is_sharepoint = any(domain in url.lower() for domain in ('sharepoint.com', '1drv.ms'))
    
    # Load cookies FIRST before any URL processing (CRITICAL FIX)
    cookie_jar = None
    cookiefile_for_requests = None
    cookie_opts = None
    
    # Normalize cookie file path (remove any artifacts like @ symbols)
    if cookie_file:
        cookie_file = cookie_file.strip()
        # Remove @ symbol and anything after it (common paste artifact)
        if '@' in cookie_file:
            cookie_file = cookie_file.split('@')[0].strip()
        # Normalize path
        if os.path.sep in cookie_file:
            cookie_file = os.path.normpath(cookie_file)
        # Expand user path if needed (~ or %USERPROFILE%)
        if cookie_file.startswith('~'):
            cookie_file = os.path.expanduser(cookie_file)
        elif '%USERPROFILE%' in cookie_file:
            cookie_file = cookie_file.replace('%USERPROFILE%', os.path.expanduser('~'))
        
        # Check if file exists before validation
        if not os.path.exists(cookie_file):
            if is_sharepoint:
                error_msg = f"❌ Cookie file không tồn tại: {cookie_file}"
                if status_callback:
                    status_callback(error_msg, "red")
                raise ValueError(error_msg)
            else:
                if status_callback:
                    status_callback(f"⚠️ Cookie file không tồn tại: {os.path.basename(cookie_file)}", "orange")
                cookie_file = None  # Disable cookie usage
    
    if cookie_file:
        if is_valid_cookie_file(cookie_file):
            # Validate SharePoint cookies if needed
            if is_sharepoint:
                is_valid, missing, message = validate_sharepoint_cookies(cookie_file, url)
                if not is_valid:
                    error_msg = f"❌ Cookie validation failed: {message}"
                    if status_callback:
                        status_callback(error_msg, "red")
                    raise ValueError(error_msg)
                elif message and "Warning" in message:
                    if status_callback:
                        status_callback(f"⚠️ {message}", "orange")
            
            # Convert and load cookies
            cookie_opts = convert_cookies_to_yt_dlp_format(cookie_file)
            cookiefile_for_requests = cookie_opts.get('cookiefile', cookie_file)
            cookie_jar = load_cookie_jar(cookiefile_for_requests)
            
            if status_callback:
                file_ext = os.path.splitext(cookie_file)[1].lower()
                status_callback(f"🍪 Sử dụng cookie file: {os.path.basename(cookie_file)} ({file_ext})", "blue")
        else:
            if is_sharepoint:
                error_msg = "❌ Cookie file không hợp lệ hoặc không tồn tại. SharePoint yêu cầu cookies để tải video."
                if status_callback:
                    status_callback(error_msg, "red")
                raise ValueError(error_msg)
            else:
                if status_callback:
                    status_callback("⚠️ Cookie file không hợp lệ hoặc không tồn tại", "orange")
    elif is_sharepoint:
        # SharePoint typically requires cookies
        warning_msg = "⚠️ Cảnh báo: SharePoint thường yêu cầu cookies để tải video. Nếu gặp lỗi, hãy thêm cookie file."
        if status_callback:
            status_callback(warning_msg, "orange")

    # Build headers placeholder (finalized after URL normalization)
    request_headers = _build_headers_for_url(url, config.get('http_headers'))

    # Apply SharePoint-specific settings
    if is_sharepoint:
        config['concurrent_fragment_downloads'] = min(config.get('concurrent_fragment_downloads', 2), 2)
        config['sleep_interval_requests'] = 2
        config['sleep_interval_requests_min'] = 1
        config['throttledratelimit'] = 1 * 1024 * 1024  # 1 MB/s
        config['ratelimit'] = 2 * 1024 * 1024  # 2 MB/s
        config['retries'] = max(config.get('retries', 5), 8)
        config['fragment_retries'] = max(config.get('fragment_retries', 3), 15)
        if status_callback:
            status_callback("🐢 Bật chế độ chậm cho SharePoint để tránh bị 429", "orange")

    # Process URL: Check throttle FIRST, then normalize (CRITICAL FIX)
    original_url = url
    target_url = url
    if is_sharepoint:
        # Check for throttle page FIRST before normalization
        if 'throttle.htm' in url.lower():
            resolved = _resolve_sharepoint_throttle(url, request_headers, cookie_jar)
            if resolved and resolved != url:
                target_url = resolved
                request_headers = _build_headers_for_url(target_url, config.get('http_headers'))
                if status_callback:
                    status_callback("🔁 SharePoint trả về Throttle, đã lấy link gốc và thử lại.", "orange")
            else:
                if status_callback:
                    status_callback("⚠️ SharePoint trả về trang Throttle. Hãy mở video trong trình duyệt, chọn 'Open in browser' để lấy link dạng :v:/... rồi thử lại.", "orange")
        
        # Try original URL first, then normalize if needed
        # Some SharePoint instances work better with :v:/r/ than :v:/g/
        # We'll try both in the candidate list
        if status_callback:
            status_callback(f"🔧 Đang xử lý URL SharePoint (sẽ thử nhiều định dạng)", "blue")

        # Build candidate URL list to try on 404
        # Try original URL first (sometimes :v:/r/ works better than :v:/g/)
        candidate_urls = []
        # Try original URL first (before normalization) - often works better
        if original_url not in candidate_urls:
            candidate_urls.append(original_url)
        # Then try normalized URL (if different)
        normalized = _normalize_sharepoint_url(original_url)
        if normalized not in candidate_urls and normalized != original_url:
            candidate_urls.append(normalized)
        # Also try with download=1 appended for each
        for u in list(candidate_urls):
            with_dl = _ensure_download_param(u)
            if with_dl not in candidate_urls:
                candidate_urls.append(with_dl)
        
        # Use first candidate as target_url for initial attempt
        target_url = candidate_urls[0] if candidate_urls else original_url
        # Rebuild headers to match the target URL
        request_headers = _build_headers_for_url(target_url, config.get('http_headers'))
    else:
        candidate_urls = [target_url]

    request_headers = _apply_context_headers(request_headers, advanced)
    
    # Build yt-dlp options
    ydl_opts = {
        'outtmpl': os.path.join(output_folder, '%(title)s.%(ext)s'),
        'progress_hooks': [hook],
        **config,
    }
    
    # Update yt-dlp options with cookies and headers
    if cookie_file and cookiefile_for_requests and cookie_opts:
        # Reuse the cookie_opts from earlier conversion (avoid duplicate conversion)
        ydl_opts.update(cookie_opts)
        if status_callback:
            status_callback(f"🍪 Đã áp dụng cookies vào yt-dlp", "blue")
    
    browser_cookie_spec = _build_cookiesfrombrowser_spec(advanced)
    if browser_cookie_spec:
        supported_cookie_browsers = {str(browser).lower() for browser in YT_DLP_SUPPORTED_BROWSERS}
        browser_cookie_name = str(browser_cookie_spec[0]).lower()
        if browser_cookie_name not in supported_cookie_browsers:
            browser_cookie_spec = None
            if status_callback:
                status_callback(
                    f"yt-dlp chua ho tro lay cookies truc tiep tu {browser_cookie_name}; bo qua browser cookies. Hay dung Cookie file neu can dang nhap.",
                    "orange",
                )
        else:
            ydl_opts["cookiesfrombrowser"] = browser_cookie_spec
            if status_callback:
                status_callback(f"Cookies tu trinh duyet: {_format_cookiesfrombrowser_spec(browser_cookie_spec)}", "blue")

    if advanced["proxy"]:
        ydl_opts["proxy"] = advanced["proxy"]
    if advanced["download_archive"]:
        ydl_opts["download_archive"] = advanced["download_archive"]

    if advanced["impersonate"]:
        if not get_runtime_capabilities()["curlCffiAvailable"]:
            if status_callback:
                status_callback("curl_cffi chua duoc cai, bo qua impersonation.", "orange")
        else:
            ydl_opts["impersonate"] = _normalize_impersonate_target(advanced["impersonate"])
            if status_callback:
                status_callback(f"Impersonate target: {advanced['impersonate']}", "blue")

    ydl_opts['http_headers'] = request_headers
    
    # Force SharePoint extractor if available
    if is_sharepoint:
        # Try to use SharePoint extractor explicitly
        # yt-dlp should auto-detect SharePoint URLs, but we can help it
        if ':v:/' in target_url.lower():
            # Ensure SharePoint extractor is used for :v:/ URLs
            ydl_opts['extractor_args'] = {
                'sharepoint': {
                    'skip_auth': False,  # Ensure authentication is used
                }
            }
        # Add additional headers that SharePoint might need
        if 'http_headers' in ydl_opts:
            ydl_opts['http_headers'].setdefault('X-Requested-With', 'XMLHttpRequest')

    # Preflight check: skip for :v:/ URLs as they require yt-dlp extractor, not direct HTTP
    # Only check non-:v:/ SharePoint URLs (like direct file links)
    skip_preflight = is_sharepoint and ':v:/' in target_url.lower()
    
    if is_sharepoint and requests is not None and cookie_jar is not None and not skip_preflight:
        try:
            preflight = requests.get(
                target_url,
                headers=request_headers,
                cookies=cookie_jar,
                allow_redirects=True,
                timeout=12,
            )
            if preflight.status_code == 404:
                # For non-:v:/ URLs, 404 is a real error
                msg = ("SharePoint trả về 404 ngay ở bước kiểm tra. "
                       "URL có thể đã hết hạn hoặc cần mở trong trình duyệt để làm mới link. "
                       "Đảm bảo dùng link dạng :v:/... và cookies FedAuth/rtFa còn hiệu lực.")
                if status_callback:
                    status_callback(f"❌ {msg}", "red")
                raise DownloadError(msg)
            if preflight.status_code in (401, 403):
                msg = ("SharePoint từ chối truy cập (401/403) ngay ở bước kiểm tra. "
                       "Kiểm tra lại FedAuth/rtFa hoặc quyền truy cập.")
                if status_callback:
                    status_callback(f"❌ {msg}", "red")
                raise DownloadError(msg)
        except DownloadError:
            raise
        except Exception:
            # Ignore network errors in preflight; yt-dlp will attempt download
            pass
    elif skip_preflight and status_callback:
        # Inform user that :v:/ URLs will be handled by yt-dlp extractor
        status_callback("🔍 :v:/ URL được phát hiện, sẽ sử dụng SharePoint extractor của yt-dlp", "blue")

    max_attempts = 3 if is_sharepoint else 1

    # Iterate over candidate URLs; for non-SharePoint it's just one item.
    # Use an explicit index so a 429 backoff retries the SAME candidate URL,
    # while a 404 advances to the next candidate variant.
    download_succeeded = False
    total_candidates = len(candidate_urls)
    url_idx = 1
    attempt = 1
    backoff_seconds = 15
    while url_idx <= total_candidates:
        candidate_url = candidate_urls[url_idx - 1]
        try:
            with YoutubeDL(ydl_opts) as ydl:
                if dry_run:
                    info = ydl.extract_info(candidate_url, download=False)
                    return _summarize_extract_info(info, candidate_url, url, candidate_urls, ydl_opts, advanced, cookie_file)
                ydl.download([candidate_url])
            download_succeeded = True
            break
        except DownloadError as e:
            error_text = str(e)
            if _is_browser_cookie_database_error(error_text) and browser_cookie_spec:
                browser_cookie_message = _browser_cookie_database_error_message(browser_cookie_spec)
                if cookie_file and cookiefile_for_requests and cookie_opts:
                    if status_callback:
                        status_callback(
                            f"⚠️ {browser_cookie_message} Dang thu lai bang cookie file thu cong.",
                            "orange",
                        )
                    ydl_opts.pop("cookiesfrombrowser", None)
                    try:
                        with YoutubeDL(ydl_opts) as ydl:
                            if dry_run:
                                info = ydl.extract_info(candidate_url, download=False)
                                advanced_without_browser = dict(advanced)
                                advanced_without_browser["use_browser_cookies"] = False
                                return _summarize_extract_info(
                                    info,
                                    candidate_url,
                                    url,
                                    candidate_urls,
                                    ydl_opts,
                                    advanced_without_browser,
                                    cookie_file,
                                )
                            ydl.download([candidate_url])
                        download_succeeded = True
                        break
                    except DownloadError as fallback_error:
                        fallback_text = _strip_ytdlp_error_prefix(str(fallback_error)).splitlines()[0]
                        if _is_unsupported_url_error(fallback_text):
                            unsupported_message = _unsupported_url_error_message(candidate_url)
                            if status_callback:
                                status_callback(f"❌ {unsupported_message}", "red")
                            raise DownloadError(unsupported_message)
                        if status_callback:
                            status_callback(f"❌ Cookie file fallback cung loi: {fallback_text}", "red")
                        raise

                if status_callback:
                    status_callback(
                        f"⚠️ {browser_cookie_message} Dang thu lai khong dung browser cookies.",
                        "orange",
                    )
                ydl_opts.pop("cookiesfrombrowser", None)
                try:
                    with YoutubeDL(ydl_opts) as ydl:
                        if dry_run:
                            info = ydl.extract_info(candidate_url, download=False)
                            advanced_without_browser = dict(advanced)
                            advanced_without_browser["use_browser_cookies"] = False
                            return _summarize_extract_info(
                                info,
                                candidate_url,
                                url,
                                candidate_urls,
                                ydl_opts,
                                advanced_without_browser,
                                cookie_file,
                            )
                        ydl.download([candidate_url])
                    download_succeeded = True
                    break
                except DownloadError as fallback_error:
                    fallback_text = _strip_ytdlp_error_prefix(str(fallback_error)).splitlines()[0]
                    if _is_unsupported_url_error(fallback_text):
                        unsupported_message = _unsupported_url_error_message(candidate_url)
                        if status_callback:
                            status_callback(f"❌ {unsupported_message}", "red")
                        raise DownloadError(unsupported_message)
                    if status_callback:
                        status_callback(f"❌ Thu lai khong cookies cung loi: {fallback_text}", "red")
                    raise DownloadError(f"{browser_cookie_message} Thu lai khong cookies cung loi: {fallback_text}.")

            hit_429 = 'HTTP Error 429' in error_text
            hit_401 = 'HTTP Error 401' in error_text or 'Unauthorized' in error_text
            hit_403 = 'HTTP Error 403' in error_text or 'Forbidden' in error_text
            hit_404 = 'HTTP Error 404' in error_text or 'Not Found' in error_text
            hit_unsupported = _is_unsupported_url_error(error_text)
            
            # Better error messages for cookie/authentication issues
            if status_callback:
                if hit_401 or hit_403:
                    if is_sharepoint:
                        status_callback("❌ Lỗi xác thực (401/403). Kiểm tra lại cookie file - cần có FedAuth và rtFa cookies. Cookies có thể đã hết hạn.", "red")
                    else:
                        status_callback(f"❌ Lỗi xác thực (401/403). Kiểm tra lại cookie file hoặc quyền truy cập.", "red")
                elif hit_404:
                    # 404 is often due to invalid/expired link or missing permissions
                    if is_sharepoint:
                        status_callback("❌ Không tìm thấy trang (404). Kiểm tra lại URL SharePoint, đảm bảo dùng link dạng :v:/... và còn hiệu lực. Cũng cần cookie FedAuth/rtFa hợp lệ.", "red")
                    else:
                        status_callback("❌ Không tìm thấy trang (404). Kiểm tra lại URL hoặc quyền truy cập.", "red")
                elif 'Throttle.htm' in error_text and 'sharepoint.com' in url.lower():
                    status_callback("⚠️ SharePoint đang trả về trang Throttle (giới hạn tải). Mở video trong trình duyệt, lấy link dạng :v:/... và thử lại sau vài phút.", "orange")
                elif 'cookie' in error_text.lower() or 'authentication' in error_text.lower():
                    if is_sharepoint:
                        status_callback("❌ Lỗi liên quan đến cookies. Đảm bảo cookie file có FedAuth và rtFa cookies hợp lệ.", "red")
                    else:
                        status_callback(f"❌ Lỗi liên quan đến cookies: {error_text.splitlines()[0]}", "red")
                elif hit_unsupported:
                    status_callback(f"❌ {_unsupported_url_error_message(candidate_url)}", "red")
            
            if is_sharepoint and hit_429 and attempt < max_attempts:
                if status_callback:
                    status_callback(f"⏸ Bị giới hạn 429, đợi {backoff_seconds}s rồi thử lại (lần {attempt+1}/{max_attempts})", "orange")
                time.sleep(backoff_seconds)
                backoff_seconds *= 2
                attempt += 1
                continue
            
            # Don't retry on authentication errors
            if hit_401 or hit_403:
                if status_callback:
                    status_callback("❌ Lỗi không thể tự động sửa. Vui lòng kiểm tra URL/cookies/quyền truy cập và thử lại.", "red")
                raise DownloadError("Xác thực thất bại (401/403). Kiểm tra cookies/quyền truy cập.")

            if hit_unsupported:
                raise DownloadError(_unsupported_url_error_message(candidate_url))

            # On 404 for SharePoint, try next candidate URL form (normalized/original/with download=1)
            if hit_404 and is_sharepoint:
                if status_callback:
                    status_callback(f"⚠️ 404 với biến thể {url_idx}/{total_candidates}. Thử biến thể khác...", "orange")
                # If this was not the last candidate, advance to the next variant
                if url_idx < total_candidates:
                    url_idx += 1
                    attempt = 1
                    backoff_seconds = 15
                    continue
                # Last candidate failed
                if status_callback:
                    status_callback("❌ SharePoint trả về 404 cho mọi biến thể URL. Kiểm tra link/cookies/quyền.", "red")
                raise DownloadError("SharePoint trả về 404 cho mọi biến thể URL.")
            
            if status_callback:
                status_callback(f"❌ Không thể tải: {error_text.splitlines()[0]}", "red")
            raise
        except ValueError as e:
            # Re-raise validation errors as-is
            raise
        except Exception as e:
            error_msg = str(e)
            if status_callback:
                # Check if it's a cookie-related error
                if 'cookie' in error_msg.lower() or 'authentication' in error_msg.lower():
                    if is_sharepoint:
                        status_callback(f"❌ Lỗi cookies/xác thực: {error_msg}", "red")
                    else:
                        status_callback(f"❌ Lỗi: {error_msg}", "red")
                else:
                    status_callback(f"⚠️ Lỗi không xác định: {error_msg}", "red")
            raise

    # Safety net: never fall out of the loop silently pretending success
    if not dry_run and not download_succeeded:
        raise DownloadError("Khong tai duoc video sau khi thu het cac bien the URL.")
