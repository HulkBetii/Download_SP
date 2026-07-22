# core/config.py
"""
Cấu hình cho video downloader
"""

from urllib.parse import parse_qs, urlparse

# Trần số mục khi tải playlist/channel. Không có trần thì một link channel
# có thể kéo về hàng nghìn video mà người dùng không kịp phản ứng.
DEFAULT_PLAYLIST_LIMIT = 50


def resolve_playlist_mode(url):
    """Quyết định tải một video hay cả playlist, dựa trên chủ đích của URL.

    Đây KHÔNG phải chuyện chỉ bỏ `noplaylist` đi. URL dạng
    `watch?v=X&list=Y` cực kỳ phổ biến — mọi video mở từ playlist đều mang
    theo `&list=` — nên coi mọi URL có `list=` là playlist sẽ biến một cú dán
    link thành hàng trăm video. UI mới chỉ có một nút nên người dùng cũng
    không có chỗ nào để chặn lại.

    Chỉ coi là playlist khi URL chủ đích trỏ tới playlist/channel.

    :return: dict tùy chọn yt-dlp (`noplaylist`, và `playlistend` nếu cần)
    """
    text = str(url or "").strip()
    if not text:
        return {'noplaylist': True}

    try:
        parsed = urlparse(text)
    except ValueError:
        return {'noplaylist': True}

    path = (parsed.path or "").lower()
    query = parse_qs(parsed.query or "")

    # Có mã video cụ thể => người dùng muốn đúng video đó, dù link kèm list.
    if query.get('v'):
        return {'noplaylist': True}

    is_playlist_path = any(
        marker in path
        for marker in ('/playlist', '/channel/', '/user/', '/c/', '/sets/', '/album/')
    )
    is_handle_path = path.startswith('/@')
    has_bare_list = bool(query.get('list'))

    if is_playlist_path or is_handle_path or has_bare_list:
        return {'noplaylist': False, 'playlistend': DEFAULT_PLAYLIST_LIMIT}

    return {'noplaylist': True}

# Giá trị User-Agent mặc định (giống trình duyệt thật để tránh bị chặn)
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/129.0.0.0 Safari/537.36"
)

# Target impersonation mặc định khi curl_cffi có sẵn.
# Nhiều site so JA3/JA4 với User-Agent; header giả Chrome + TLS fingerprint Python
# là lý do bị chặn phổ biến nhất, nên bật mặc định thay vì để người dùng tự điền.
DEFAULT_IMPERSONATE_TARGET = "chrome"

# Header HTTP chung dùng cho mọi request của yt-dlp
DEFAULT_HTTP_HEADERS = {
    'User-Agent': DEFAULT_USER_AGENT,
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
}

# Cấu hình tối ưu hóa tốc độ tải
DOWNLOAD_CONFIG = {
    # Cấu hình format video - fallback nếu không có ffmpeg
    'format': 'best[ext=mp4]/best',  # Đơn giản hóa format để tránh cần merge
    'merge_output_format': 'mp4',
    'prefer_ffmpeg': False,  # Tắt ffmpeg để tránh lỗi
    'user_agent': DEFAULT_USER_AGENT,
    'http_headers': DEFAULT_HTTP_HEADERS,
    
    # Tối ưu hóa tốc độ tải
    'concurrent_fragment_downloads': 8,  # Số fragment tải đồng thời
    'buffersize': 1024,  # Buffer size (bytes)
    'http_chunk_size': 10485760,  # Chunk size 10MB
    'retries': 5,  # Tăng số lần retry để ổn định với video dài
    'fragment_retries': 10,  # Thử lại fragment nhiều hơn trước khi thất bại
    'skip_unavailable_fragments': False,  # Không bỏ qua fragment để tránh video bị thiếu
    'continuedl': True,  # Cho phép resume khi tải video dung lượng lớn
    
    # Tối ưu hóa network
    'socket_timeout': 30,  # Timeout cho socket (giây)
    'extractor_retries': 3,  # Retry cho extractor
    'ignoreerrors': False,  # Không bỏ qua lỗi
    
    # Cấu hình khác
    'quiet': True,
    'noplaylist': True,
    'restrictfilenames': False,  # Cho phép Unicode (tên file tiếng Việt)
    # Don't abort on error, try fallback formats instead
    'abort_on_error': False,
}

# Cấu hình post-processors - chỉ sử dụng nếu có ffmpeg
POST_PROCESSORS = []  # Bỏ post-processors để tránh lỗi ffmpeg

# Cấu hình cho các trường hợp đặc biệt
SPEED_OPTIMIZED_CONFIG = {
    **DOWNLOAD_CONFIG,
    'concurrent_fragment_downloads': 16,  # Tăng số fragment đồng thời
    'buffersize': 2048,  # Tăng buffer size
    'http_chunk_size': 20971520,  # Tăng chunk size lên 20MB
}

QUALITY_OPTIMIZED_CONFIG = {
    **DOWNLOAD_CONFIG,
    # Ưu tiên video >=1080p, merge audio tốt nhất, fallback nếu không có
    # Note: Format with + requires ffmpeg, so we provide fallbacks
    'format': 'bestvideo[height>=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
    'concurrent_fragment_downloads': 4,  # Giảm số fragment để ưu tiên chất lượng
    # Don't abort if ffmpeg not available, just use fallback format
    'abort_on_error': False,
}

# Cấu hình cho trường hợp có ffmpeg
FFMPEG_CONFIG = {
    'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
    'prefer_ffmpeg': True,
    'restrictfilenames': False,  # Cho phép Unicode (tên file tiếng Việt)
    'postprocessors': [{
        'key': 'FFmpegVideoConvertor',
        'preferedformat': 'mp4',
    }],
    # Don't abort if merge fails, try fallback format instead
    'abort_on_error': False,
}
