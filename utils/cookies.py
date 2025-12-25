# utils/cookies.py

import hashlib
import json
import os
import tempfile
from http.cookiejar import MozillaCookieJar


def is_valid_cookie_file(path):
    """
    Kiểm tra xem file cookies có tồn tại và hợp lệ không
    Hỗ trợ cả định dạng .txt (Netscape) và .json
    """
    if not os.path.exists(path):
        return False

    try:
        file_ext = os.path.splitext(path)[1].lower()
        
        if file_ext == '.json':
            return is_valid_json_cookie_file(path)
        else:
            return is_valid_txt_cookie_file(path)
    except Exception:
        return False


def is_valid_txt_cookie_file(path):
    """
    Kiểm tra file cookie .txt (Netscape format)
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        if len(lines) == 0:
            return False

        # File phải chứa dòng có ít nhất 6 cột (domain, flag, path, secure, expires, name, value)
        for line in lines:
            if line.strip().startswith("#") or line.strip() == "":
                continue
            parts = line.strip().split("\t")
            if len(parts) >= 6:
                return True
    except Exception:
        return False

    return False


def is_valid_json_cookie_file(path):
    """
    Kiểm tra file cookie .json
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        # Kiểm tra cấu trúc JSON cơ bản
        if isinstance(data, list):
            # Format: [{"name": "...", "value": "...", "domain": "..."}, ...]
            return len(data) > 0 and all(isinstance(item, dict) for item in data)
        elif isinstance(data, dict):
            # Format: {"cookies": [...]} hoặc {"domain": {...}}
            return len(data) > 0
        else:
            return False
    except (json.JSONDecodeError, Exception):
        return False


def domain_matches(cookie_domain, target_domain):
    """
    Kiểm tra xem cookie domain có khớp với target domain không
    Hỗ trợ cả domain-wide cookies (bắt đầu với .) và host-only cookies
    :param cookie_domain: Domain từ cookie (có thể có hoặc không có dấu . ở đầu)
    :param target_domain: Domain cần kiểm tra
    :return: True nếu khớp, False nếu không
    """
    if not cookie_domain or not target_domain:
        return False
    
    # Normalize domains (lowercase, strip)
    cookie_domain = cookie_domain.lower().strip()
    target_domain = target_domain.lower().strip()
    
    # Exact match
    if cookie_domain == target_domain:
        return True
    
    # Domain-wide cookie (starts with .) - matches subdomains
    if cookie_domain.startswith('.'):
        # Remove leading dot for comparison
        cookie_base = cookie_domain[1:]
        # Match exact domain or subdomain
        if target_domain == cookie_base or target_domain.endswith('.' + cookie_base):
            return True
    
    # Host-only cookie - matches exact domain or parent domain
    if not cookie_domain.startswith('.'):
        # Exact match
        if target_domain == cookie_domain:
            return True
        # Match if target is subdomain of cookie domain
        if target_domain.endswith('.' + cookie_domain):
            return True
    
    return False


def extract_cookies_for_domain(cookie_path, domain):
    """
    Trích xuất cookies dành riêng cho một domain
    Hỗ trợ cả định dạng .txt và .json
    Automatically checks both exact domain and parent domains for better matching
    :param cookie_path: Đường dẫn file cookies (.txt hoặc .json)
    :param domain: Ví dụ: ".onedrive.live.com" hoặc "sharepoint.com" hoặc "moithuvemmo-my.sharepoint.com"
    :return: dict chứa các cookie
    """
    cookies = {}
    if not os.path.exists(cookie_path):
        return cookies

    file_ext = os.path.splitext(cookie_path)[1].lower()
    
    # For SharePoint, check multiple domain variations
    domains_to_check = [domain]
    if 'sharepoint.com' in domain.lower():
        # Add domain-wide cookie domain if checking specific subdomain
        if not domain.startswith('.'):
            # If checking moithuvemmo-my.sharepoint.com, also check .sharepoint.com
            if '.sharepoint.com' in domain:
                domains_to_check.append('.sharepoint.com')
        # Also check without leading dot if it has one
        if domain.startswith('.'):
            domains_to_check.append(domain[1:])
    
    # Extract cookies for all relevant domains
    all_cookies = {}
    for check_domain in domains_to_check:
        if file_ext == '.json':
            domain_cookies = extract_cookies_from_json(cookie_path, check_domain)
        else:
            domain_cookies = extract_cookies_from_txt(cookie_path, check_domain)
        # Merge cookies (later domains override earlier ones for same cookie name)
        all_cookies.update(domain_cookies)
    
    return all_cookies


def extract_cookies_from_txt(cookie_path, domain):
    """
    Trích xuất cookies từ file .txt
    """
    cookies = {}
    try:
        with open(cookie_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("#") or line.strip() == "":
                    continue
                parts = line.strip().split("\t")
                if len(parts) >= 7:
                    cookie_domain, _, _, _, _, name, value = parts
                    # Use proper domain matching instead of substring
                    if domain_matches(cookie_domain, domain):
                        cookies[name] = value
    except Exception:
        pass
    return cookies


def extract_cookies_from_json(cookie_path, domain):
    """
    Trích xuất cookies từ file .json
    """
    cookies = {}
    try:
        with open(cookie_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        if isinstance(data, list):
            # Format: [{"name": "...", "value": "...", "domain": "..."}, ...]
            for item in data:
                if isinstance(item, dict):
                    cookie_domain = item.get('domain', '')
                    name = item.get('name', '')
                    value = item.get('value', '')
                    # Use proper domain matching instead of substring
                    if domain_matches(cookie_domain, domain) and name and value:
                        cookies[name] = value
        elif isinstance(data, dict):
            # Format: {"cookies": [...]} hoặc {"domain": {...}}
            if 'cookies' in data and isinstance(data['cookies'], list):
                for item in data['cookies']:
                    if isinstance(item, dict):
                        cookie_domain = item.get('domain', '')
                        name = item.get('name', '')
                        value = item.get('value', '')
                        # Use proper domain matching instead of substring
                        if domain_matches(cookie_domain, domain) and name and value:
                            cookies[name] = value
            else:
                # Format: {"domain": {...}}
                for key, value in data.items():
                    if isinstance(value, dict) and domain_matches(key, domain):
                        for cookie_name, cookie_value in value.items():
                            if isinstance(cookie_value, str):
                                cookies[cookie_name] = cookie_value
    except Exception:
        pass
    return cookies


def convert_cookies_to_yt_dlp_format(cookie_path):
    """
    Chuyển đổi cookies sang định dạng mà yt-dlp có thể sử dụng
    :param cookie_path: Đường dẫn file cookies
    :return: dict với các tùy chọn yt-dlp
    """
    file_ext = os.path.splitext(cookie_path)[1].lower()
    
    if file_ext == '.json':
        # Convert JSON to Netscape format for yt-dlp
        netscape_path = convert_json_to_netscape(cookie_path)
        return {'cookiefile': netscape_path}
    else:
        # File .txt được hỗ trợ trực tiếp
        return {'cookiefile': cookie_path}


def convert_json_to_netscape(json_path):
    """
    Chuyển đổi file cookie JSON sang định dạng Netscape (.txt)
    :param json_path: Đường dẫn file JSON
    :return: Đường dẫn file Netscape đã tạo
    """
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        # Ghi file Netscape vào thư mục tạm với tên xác định theo đường dẫn nguồn,
        # tránh rải rác file rác cạnh file gốc và tránh lỗi khi thư mục nguồn chỉ đọc.
        source_key = hashlib.sha1(os.path.abspath(json_path).encode('utf-8', 'ignore')).hexdigest()[:16]
        netscape_path = os.path.join(tempfile.gettempdir(), f'video_downloader_cookies_{source_key}.txt')

        cookies = []
        if isinstance(data, dict) and 'cookies' in data:
            cookies = data['cookies']
        elif isinstance(data, list):
            cookies = data
        
        with open(netscape_path, 'w', encoding='utf-8') as f:
            # Write Netscape header
            f.write("# Netscape HTTP Cookie File\n")
            f.write("# https://curl.se/rfc/cookie_spec.html\n")
            f.write("# This file was generated by Video Downloader Tool\n\n")
            
            for cookie in cookies:
                if isinstance(cookie, dict):
                    domain = cookie.get('domain', '')
                    host_only = cookie.get('hostOnly', False)
                    path = cookie.get('path', '/')
                    secure = cookie.get('secure', False)
                    # Support multiple expiration field names
                    expires = cookie.get('expirationDate', 0) or cookie.get('expires', 0) or cookie.get('expiryDate', 0)
                    name = cookie.get('name', '')
                    value = cookie.get('value', '')
                    
                    # Skip if name or value is missing
                    if not name or not value:
                        continue
                    
                    # Fix domain format for Netscape
                    # Remove leading dot if present and host_only is True
                    if host_only and domain.startswith('.'):
                        domain = domain[1:]
                    # Add leading dot if host_only is False and domain doesn't start with dot
                    elif not host_only and not domain.startswith('.'):
                        domain = '.' + domain
                    
                    # Convert expires to Unix timestamp
                    # Handle both Unix timestamp and JavaScript timestamp (milliseconds)
                    if expires > 0:
                        # If expires is in milliseconds (JavaScript format), convert to seconds
                        if expires > 1000000000000:  # Likely milliseconds
                            expires = int(expires / 1000)
                        expires_str = str(int(expires))
                    else:
                        expires_str = '0'
                    
                    # Write cookie line in Netscape format
                    # domain, subdomain_flag, path, secure_flag, expiration, name, value
                    secure_flag = 'TRUE' if secure else 'FALSE'
                    # subdomain_flag: TRUE if cookie applies to subdomains, FALSE if only exact domain
                    subdomain_flag = 'FALSE' if host_only else 'TRUE'
                    
                    f.write(f"{domain}\t{subdomain_flag}\t{path}\t{secure_flag}\t{expires_str}\t{name}\t{value}\n")
        
        return netscape_path
        
    except Exception as e:
        print(f"Error converting JSON cookies: {e}")
        return json_path  # Return original path if conversion fails


def extract_domain_from_url(url):
    """
    Extract domain from URL for cookie matching
    :param url: URL to extract domain from
    :return: List of domains to check (e.g., ['sharepoint.com', '.sharepoint.com', 'subdomain.sharepoint.com'])
    """
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        hostname = parsed.hostname or ''
        
        if not hostname:
            return []
        
        domains = []
        # Add exact hostname
        domains.append(hostname)
        
        # For SharePoint, also check domain-wide and parent domains
        if 'sharepoint.com' in hostname.lower():
            # Add domain-wide cookie domain
            domains.append('.sharepoint.com')
            # Add parent domain if it's a subdomain
            if '.' in hostname:
                parts = hostname.split('.')
                if len(parts) >= 3:
                    # e.g., moithuvemmo-my.sharepoint.com -> sharepoint.com
                    parent = '.'.join(parts[-2:])
                    if parent not in domains:
                        domains.append('.' + parent)
        
        return domains
    except Exception:
        return []


def validate_sharepoint_cookies(cookie_path, url):
    """
    Validate that required SharePoint cookies are present
    :param cookie_path: Path to cookie file
    :param url: URL to check (to determine if SharePoint)
    :return: tuple (is_valid, missing_cookies, message)
    """
    if not cookie_path or not os.path.exists(cookie_path):
        return False, [], "Cookie file does not exist"
    
    # Check if this is a SharePoint URL
    url_lower = url.lower() if url else ''
    is_sharepoint = 'sharepoint.com' in url_lower or '1drv.ms' in url_lower
    
    if not is_sharepoint:
        return True, [], "Not a SharePoint URL, validation skipped"
    
    # Extract all relevant domains from URL
    domains_to_check = extract_domain_from_url(url)
    if not domains_to_check:
        # Fallback to standard SharePoint domains
        domains_to_check = ['sharepoint.com', '.sharepoint.com']
    
    # Extract cookies for all relevant domains
    all_cookies = {}
    for domain in domains_to_check:
        domain_cookies = extract_cookies_for_domain(cookie_path, domain)
        all_cookies.update(domain_cookies)
    
    # Required cookies for SharePoint authentication
    required_cookies = ['FedAuth', 'rtFa']
    missing = [r for r in required_cookies if r not in all_cookies]
    
    if missing:
        message = f"Missing required SharePoint cookies: {', '.join(missing)}. These are required for authentication."
        return False, missing, message
    
    # Check for expired cookies (optional warning)
    # Note: We load with ignore_expires=True, but we can still check
    expired_warnings = []
    try:
        file_ext = os.path.splitext(cookie_path)[1].lower()
        if file_ext == '.json':
            with open(cookie_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            cookies_list = data.get('cookies', []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            import time
            current_time = time.time()
            for cookie in cookies_list:
                if isinstance(cookie, dict):
                    name = cookie.get('name', '')
                    if name in required_cookies:
                        expires = cookie.get('expirationDate', 0) or cookie.get('expires', 0) or cookie.get('expiryDate', 0)
                        if expires > 0:
                            # Convert milliseconds to seconds if needed
                            if expires > 1000000000000:
                                expires = expires / 1000
                            if expires < current_time:
                                expired_warnings.append(name)
    except Exception:
        pass  # Ignore errors in expiration checking
    
    if expired_warnings:
        message = f"Warning: Some SharePoint cookies may be expired: {', '.join(expired_warnings)}. Consider refreshing your cookies."
        return True, [], message
    
    return True, [], "SharePoint cookies validated successfully"


def load_cookie_jar(cookie_path):
    """
    Load cookies into a MozillaCookieJar for requests/urllib usage
    """
    if not cookie_path or not os.path.exists(cookie_path):
        return None
    
    jar = MozillaCookieJar()
    try:
        jar.load(cookie_path, ignore_discard=True, ignore_expires=True)
        return jar
    except Exception:
        return None
