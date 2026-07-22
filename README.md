# Video Downloader Tool

Web UI chạy local để tải video, dựng trên `yt-dlp` cộng thêm một media sniffer
tự viết bằng Chrome DevTools Protocol cho những trang mà `yt-dlp` không hỗ trợ.

Dán link, bấm một nút. Tool tự chọn cách tải phù hợp.

## Cài đặt

Cần Python 3.11+ và `ffmpeg` trong PATH.

```bash
pip install -r requirements.txt
```

`ffmpeg` là bắt buộc nếu muốn chất lượng cao nhất — video và audio của nhiều
site được phát riêng và phải ghép lại. Thiếu nó thì tool vẫn chạy nhưng chỉ lấy
được luồng gộp sẵn, thường độ phân giải thấp hơn.

Một JavaScript runtime (`node` hoặc `deno`) là cần thiết cho YouTube: `yt-dlp`
dùng nó để giải chữ ký. Thiếu thì YouTube vẫn tải được nhưng thiếu format.

## Chạy

```bash
python run.py
```

Server mở ở `http://127.0.0.1:8765` và tự bật trình duyệt. Nếu cổng bận thì tool
tự dò lên tới 8785.

Tuỳ chọn:

```bash
python run.py --no-open --port 9000
```

## Cách dùng

Mặc định UI chỉ có một ô nhập và một nút. Dán link rồi bấm **Tải xuống**.
Nhiều link thì mỗi dòng một link — chúng sẽ vào hàng đợi và chạy tuần tự.

Mục **Nâng cao / Chẩn đoán** đóng sẵn, chứa cookie, proxy, tuỳ chọn `yt-dlp`,
Media Inspector và log. Chỉ cần mở khi có gì đó không như ý.

Nếu thiếu thành phần nào, một dải cảnh báo hiện ở đầu trang kèm nút **Cài đặt**
để tool tự bổ sung.

## Cách tool tự xoay xở

Khi cách trực tiếp thất bại, tool leo thang chứ không bỏ cuộc:

1. `yt-dlp` tải thẳng, có giả lập TLS fingerprint của trình duyệt
2. Đổi nguồn cookie nếu lỗi xác thực
3. Mở trình duyệt, bắt request media, tải kèm header đã bắt được
4. Hook `MediaSource` để gom segment khi trang chỉ lộ `blob:`
5. Ghi trực tiếp bằng `ffmpeg` — chốt chặn cuối, chạy theo thời gian thực

Với site cần đăng nhập, lần đầu hãy để sniffer mở ở chế độ hiện cửa sổ và đăng
nhập trong đó. Profile được giữ lại ở `%LOCALAPPDATA%/VideoDownloaderTool/` nên
các lần sau dùng lại.

## Giới hạn

**Không vượt DRM.** Nội dung bảo vệ bằng Widevine, PlayReady hay FairPlay nằm
ngoài phạm vi — Netflix, Disney+, Prime Video, Spotify và tương tự sẽ báo lỗi rõ
ràng thay vì thử vô ích. Đây là lựa chọn có chủ đích, không phải thiếu sót.

Các site có anti-bot mức JavaScript challenge hoặc fingerprint trình duyệt cũng
có thể không tải được.

## Cookie

Tool **không** tự nhặt file cookie trong thư mục project. Muốn dùng cookie thì
chỉ định rõ đường dẫn trong mục Nâng cao.

File cookie chứa session đăng nhập, giá trị tương đương mật khẩu. Đừng commit
chúng — `.gitignore` đã chặn sẵn các mẫu tên thường gặp.

## Test

```bash
python -m unittest test_errors test_runtime_deps test_plugins test_cdp_websocket \
  test_profile_cookies test_mse_capture test_phase6 test_strategy \
  test_downloader_options test_auto_pipeline test_media_sniffer test_web_ui
```

Liệt kê module tường minh là cố ý: `test_all_functions.py`,
`test_cookie_path_fix.py` và `test_sharepoint_download.py` là script chạy mạng
thật ngay khi import, nên `unittest discover` sẽ kích hoạt chúng ngoài ý muốn.
