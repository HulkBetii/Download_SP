# Video Downloader Tool

Tool tải video với tối ưu hóa tốc độ và chất lượng cao.

## Tính năng

- ✅ Tải video với độ phân giải cao nhất
- ⚡ Tối ưu hóa tốc độ tải với nhiều chế độ
- 🎯 Hỗ trợ nhiều định dạng video
- 🔄 Hiển thị tiến trình tải real-time
- 🍪 Hỗ trợ cookies (.txt và .json) cho video bị giới hạn
- 🔧 Tự động phát hiện và sử dụng ffmpeg

## Cài đặt

### Yêu cầu cơ bản
```bash
pip install yt-dlp
```

### Cài đặt ffmpeg (tùy chọn - để merge video chất lượng cao)

#### Windows
1. Tải ffmpeg từ: https://ffmpeg.org/download.html
2. Giải nén và thêm vào PATH
3. Hoặc sử dụng chocolatey: `choco install ffmpeg`

#### macOS
```bash
brew install ffmpeg
```

#### Linux (Ubuntu/Debian)
```bash
sudo apt update
sudo apt install ffmpeg
```

## Sử dụng

### Chạy Local Web UI (Khuyến nghị)
```bash
cd video_downloader_tool
python run.py
```

### Chạy Local Web UI (Cách khác)
```bash
cd video_downloader_tool
python main.py
```

### Chạy không mở trình duyệt
```bash
cd video_downloader_tool
python run.py --no-open
```

### Auto multi-source workflow

Use the main `Tai tu dong` button in the Local Web UI for best-effort downloads:

1. The app probes the URL with `yt-dlp`.
2. If `yt-dlp` cannot handle the page URL, the app opens the selected Chromium-compatible browser, defaulting to Coc Coc when available.
3. Play the video if the page requires user interaction.
4. The app sniffs media/manifest requests, ranks candidates, and downloads the best non-DRM candidate with captured browser context headers.
5. If the media token expires with 401/403, the app sniffs once more and retries.

Limits: this does not bypass DRM/Widevine/PlayReady, CAPTCHA, hard login walls, or geo-blocks without valid cookies/proxy access. Use `Media Inspector` when auto selection is uncertain.

### Chạy command line test
```bash
cd video_downloader_tool
python test_all_functions.py
```

## Hỗ trợ Cookies

### Định dạng được hỗ trợ

#### 1. File .txt (Netscape format)
```
# Netscape HTTP Cookie File
.youtube.com	TRUE	/	TRUE	1735689600	sessionid	your_session_id_here
.youtube.com	TRUE	/	TRUE	1735689600	LOGIN_INFO	your_login_info_here
```

#### 2. File .json
```json
[
  {
    "name": "sessionid",
    "value": "your_session_id_here",
    "domain": ".youtube.com",
    "path": "/",
    "secure": true,
    "httpOnly": true,
    "expires": 1735689600
  },
  {
    "name": "LOGIN_INFO",
    "value": "your_login_info_here",
    "domain": ".youtube.com",
    "path": "/",
    "secure": true,
    "httpOnly": false,
    "expires": 1735689600
  }
]
```

### Cách sử dụng cookies
1. Bật "Dùng cookie file (.txt/.json)" trong Local Web UI
2. Nhập đường dẫn file cookie (.txt hoặc .json)
3. Tool sẽ tự động phát hiện định dạng và sử dụng

## Chế độ tối ưu hóa

### 1. Cân bằng (Balanced)
- Số fragment đồng thời: 8
- Buffer size: 1KB
- Chunk size: 10MB
- Phù hợp cho hầu hết trường hợp

### 2. Tốc độ cao (Speed)
- Số fragment đồng thời: 16
- Buffer size: 2KB
- Chunk size: 20MB
- Tối ưu cho tốc độ tải nhanh
- **Không sử dụng ffmpeg** để tránh chậm

### 3. Chất lượng cao (Quality)
- Ưu tiên video 1080p+
- Số fragment đồng thời: 4
- Tối ưu cho chất lượng video
- **Yêu cầu ffmpeg** để merge video

## Tối ưu hóa đã thực hiện

### Tốc độ tải
- **Concurrent fragment downloads**: Tải nhiều fragment cùng lúc
- **Buffer size optimization**: Tăng buffer size để giảm I/O
- **HTTP chunk size**: Tăng chunk size để giảm overhead
- **Retry mechanism**: Tự động retry khi lỗi
- **Skip unavailable fragments**: Bỏ qua fragment không có sẵn

### Network optimization
- **Socket timeout**: Timeout hợp lý cho socket
- **Extractor retries**: Retry cho extractor
- **Error handling**: Xử lý lỗi tốt hơn

### UI improvements
- **Progress bar**: Hiển thị tiến trình tải
- **Speed display**: Hiển thị tốc độ tải real-time
- **ETA display**: Hiển thị thời gian còn lại
- **Optimization mode selection**: Chọn chế độ tối ưu hóa

## Cấu trúc thư mục

```
video_downloader_tool/
├── core/
│   ├── __init__.py
│   ├── auto_pipeline.py # Auto probe/sniff/select/download workflow
│   ├── downloader.py    # Core download logic
│   ├── media_sniffer.py # Browser CDP media sniffer
│   └── config.py        # Configuration settings
├── web_ui/
│   ├── __init__.py
│   ├── server.py        # Local web server and app state
│   └── static/
│       ├── index.html   # Web UI shell
│       ├── app.css      # UI styling
│       └── app.js       # Browser client logic
├── utils/
│   ├── __init__.py
│   ├── cookies.py       # Cookie utilities
│   └── ffmpeg_checker.py # FFmpeg checker
├── examples/
│   └── cookies_example.json # Example cookie file
├── __init__.py
├── main.py              # Main web launcher
├── run.py               # Simple web launcher (recommended)
└── test_all_functions.py # Test script
```

## Troubleshooting

### Lỗi thường gặp
1. **Import errors**: Sử dụng `python run.py`; nếu đang ở cửa sổ terminal cũ, đóng rồi mở lại từ thư mục project.
2. **"download_video" is not defined**: Kiểm tra import statement
3. **Tốc độ tải chậm**: Thử chế độ "Tốc độ cao"
4. **"ffmpeg is not installed"**: 
   - Tool sẽ tự động sử dụng format đơn giản
   - Hoặc cài đặt ffmpeg theo hướng dẫn trên
5. **Cookie file không hoạt động**:
   - Kiểm tra định dạng file (.txt hoặc .json)
   - Đảm bảo file có quyền đọc
   - Xem ví dụ trong thư mục `examples/`
6. **Video tải không đủ thời lượng**:
   - Tool giờ sẽ dừng khi thiếu fragment thay vì bỏ qua (để tránh video bị cắt)
   - Kiểm tra kết nối mạng hoặc thử lại ở thời điểm khác khi nhận được lỗi thiếu fragment
   - Với server chặn yêu cầu dài, hãy ưu tiên chế độ Cân bằng/Chất lượng để giảm tải song song
7. **SharePoint/OneDrive trả về Throttle.htm**:
   - Tool đã giả lập trình duyệt (User-Agent + headers) và tự động đọc `Throttle.htm` để lấy lại link gốc `:v:/...` khi có thể, nhưng Microsoft vẫn có thể giới hạn khi tải quá nhanh
   - Đảm bảo link bạn dùng có dạng `:v:/...` hoặc `stream.aspx?id=...` (không phải `_layouts/15/Throttle.htm`)
   - Luôn đính kèm cookie FedAuth/rtFa (qua mục Cookie file) và thử lại sau vài phút nếu vẫn bị giới hạn
8. **HTTP Error 429 (Too Many Requests)**:
   - Tool sẽ tự động chuyển sang chế độ tải chậm, giới hạn tốc độ và thử lại với backoff 15s → 30s → 60s cho tới 3 lần
   - Nếu vẫn lỗi, hãy tạm dừng vài phút rồi thử lại vì Microsoft đã khóa tạm thời IP/tài khoản của bạn
   - Tránh chạy song song nhiều video SharePoint và nên giữ cửa sổ trình duyệt mở cùng tài khoản

### Yêu cầu hệ thống
- Python 3.7+
- yt-dlp
- ffmpeg (tùy chọn - cho merge video chất lượng cao)
- Trình duyệt hiện đại

### Lưu ý về ffmpeg
- **Không bắt buộc**: Tool hoạt động bình thường không có ffmpeg
- **Tự động phát hiện**: Tool sẽ kiểm tra và sử dụng ffmpeg nếu có
- **Fallback**: Nếu không có ffmpeg, sẽ sử dụng format đơn giản
- **Chế độ tốc độ**: Luôn bỏ qua ffmpeg để tối ưu tốc độ

### Lưu ý về cookies
- **Hỗ trợ 2 định dạng**: .txt (Netscape) và .json
- **Tự động phát hiện**: Tool sẽ tự động nhận diện định dạng
- **Validation**: Kiểm tra tính hợp lệ của file trước khi sử dụng
- **Error handling**: Thông báo rõ ràng nếu file không hợp lệ

## Advanced web UI notes

- The app now runs as a local web UI from `run.py` or `main.py`.
- Browser cookies are supported through yt-dlp's built-in browser cookie loader.
- The `Bat media` action opens a Chromium-compatible browser such as Coc Coc, Chrome, or Edge, watches network media requests, and adds detected `.mp4`, `.m3u8`, or `.mpd` URLs to the queue.
- `curl_cffi` is optional. If it is installed, impersonation is enabled; otherwise the UI explains the fallback.
- The UI includes a probe/analyze action for quick capability checks before downloading.
- Default output folder is `~/Downloads`, and you can change it from the UI.
