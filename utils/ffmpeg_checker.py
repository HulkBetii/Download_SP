# utils/ffmpeg_checker.py
"""
Utility để kiểm tra và cài đặt ffmpeg
"""

import subprocess
import sys
import platform


def check_ffmpeg():
    """
    Kiểm tra xem ffmpeg có sẵn không
    """
    try:
        result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True, check=True)
        return True, result.stdout.split('\n')[0]
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False, None


def get_ffmpeg_installation_guide():
    """
    Trả về hướng dẫn cài đặt ffmpeg cho hệ điều hành hiện tại
    """
    system = platform.system().lower()
    
    if system == 'windows':
        return """
🔧 Cài đặt ffmpeg trên Windows:

1. Tải ffmpeg từ: https://ffmpeg.org/download.html#build-windows
2. Giải nén file zip
3. Copy thư mục ffmpeg vào C:\\ffmpeg
4. Thêm C:\\ffmpeg\\bin vào PATH:
   - Mở System Properties > Advanced > Environment Variables
   - Thêm C:\\ffmpeg\\bin vào Path

Hoặc sử dụng Chocolatey:
   choco install ffmpeg

Hoặc sử dụng winget:
   winget install ffmpeg
"""
    
    elif system == 'darwin':  # macOS
        return """
🔧 Cài đặt ffmpeg trên macOS:

Sử dụng Homebrew:
   brew install ffmpeg

Hoặc sử dụng MacPorts:
   sudo port install ffmpeg
"""
    
    else:  # Linux
        return """
🔧 Cài đặt ffmpeg trên Linux:

Ubuntu/Debian:
   sudo apt update
   sudo apt install ffmpeg

CentOS/RHEL/Fedora:
   sudo yum install ffmpeg
   # hoặc
   sudo dnf install ffmpeg

Arch Linux:
   sudo pacman -S ffmpeg
"""


def main():
    """
    Main function để kiểm tra ffmpeg
    """
    print("🔍 Kiểm tra ffmpeg...")
    
    is_available, version = check_ffmpeg()
    
    if is_available:
        print(f"✅ ffmpeg đã được cài đặt: {version}")
        print("🎉 Tool sẽ sử dụng ffmpeg để merge video chất lượng cao!")
    else:
        print("⚠️ ffmpeg chưa được cài đặt")
        print("📝 Tool vẫn hoạt động bình thường nhưng sẽ sử dụng format đơn giản")
        print("\n" + get_ffmpeg_installation_guide())


if __name__ == "__main__":
    main()
