import os
import re
import time
import shutil
import tempfile
import base64
from typing import Generator, Optional, Tuple

from flask import request, Response, send_from_directory, current_app
from flask.views import MethodView
from flask_smorest import Blueprint
from youtubesearchpython import VideosSearch
from yt_dlp import YoutubeDL
from pydub import AudioSegment
from openai import OpenAI
import json

# Import the global limiter instance from the app package
from app import limiter  # type: ignore

# Blueprint for YouTube-related endpoints (mounted at root)
blp = Blueprint(
    "YouTube MP3",
    "youtube_mp3",
    url_prefix="",
    description="Endpoints for searching YouTube and downloading MP3 audio with range support",
)

# Constants and configuration
RETENTION_SECONDS = 2 * 60 * 60  # 2 hours
# Compute the absolute path to the 'audios' directory at the container root
BASE_DIR = os.path.dirname(os.path.dirname(__file__))  # flask_backend/app -> go up to flask_backend
AUDIOS_DIR = os.path.join(os.path.dirname(BASE_DIR), "audios")
os.makedirs(AUDIOS_DIR, exist_ok=True)

# Path to fallback cookies file
COOKIE_DIR = os.path.join(os.path.dirname(BASE_DIR), "cookie")
FALLBACK_COOKIES_FILE = os.path.join(COOKIE_DIR, "cookies.txt")

# User-Agent to use for yt-dlp requests
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def _now_ts() -> int:
    return int(time.time())


def _log(level: str, msg: str) -> None:
    """
    Lightweight logging wrapper that prefers Flask's logger when available,
    falling back to print in non-app contexts (e.g., during tests).
    """
    try:
        logger = current_app.logger  # type: ignore[attr-defined]
        getattr(logger, level, logger.info)(msg)
    except Exception:
        print(f"[{level.upper()}] {msg}")


def _preview_cookies_content(content: bytes) -> Tuple[bool, str, str]:
    """
    Inspect the given cookies.txt bytes and determine if it looks like Netscape format.
    Returns (is_netscape, header_or_first_line, reason_when_false).
    """
    try:
        # decode using utf-8 with fallback replacement to avoid exceptions
        text = content.decode("utf-8", errors="replace")
    except Exception:
        return False, "", "Unable to decode cookies as UTF-8 text."

    # Get the first non-empty line
    first_line = ""
    for line in text.splitlines():
        if line.strip():
            first_line = line.strip()
            break

    if not first_line:
        return False, "", "Empty cookies file."

    # Netscape cookie files typically start with this header comment
    if first_line.startswith("# Netscape HTTP Cookie File"):
        return True, first_line, ""

    # Otherwise, look for a typical tab-separated cookie line with >= 7 fields
    # Example: .example.com  TRUE    /   FALSE   0   sid    abcdef
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        # Netscape format must have at least 7 tab-separated columns
        # Domain, flag, path, secure, expiration, name, value
        parts = line.split("\t")
        if len(parts) >= 7:
            return True, first_line, ""

    # Heuristic: if the content appears to be base64 itself (common mistake),
    # warn about wrong format
    b64_chars = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\n\r")
    if set(text.strip()) <= b64_chars and len(text.strip()) > 0:
        return False, first_line, "Provided cookies look like base64 content, not Netscape cookies.txt text."

    return False, first_line, "Cookies content does not appear to be in Netscape cookies.txt format."


def _read_cookies_file(path: str) -> Tuple[Optional[bytes], str]:
    """
    Read a cookie file and return (decoded_content, format_description).
    Automatically detects and decodes base64-encoded content with padding fix.
    Returns (None, error_reason) on failure.
    """
    try:
        if not os.path.isfile(path) or not os.access(path, os.R_OK):
            return None, "File not found or not readable."
        
        with open(path, "rb") as f:
            content = f.read()
        
        _log("info", f"[_read_cookies_file] Reading {path}, size={len(content)}B")
        
        # First, try to interpret as-is
        is_net, first, reason = _preview_cookies_content(content)
        if is_net:
            _log("info", "[_read_cookies_file] Detected raw Netscape format")
            return content, "raw Netscape format"
        
        # If not valid Netscape, check if it might be base64-encoded
        try:
            text = content.decode("utf-8", errors="replace").strip()
            lines = text.splitlines()
            if len(lines) <= 2:  # base64 might have one or two lines
                b64_chars = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\n\r \t")
                if set(text) <= b64_chars and len(text.strip()) > 0:
                    _log("info", "[_read_cookies_file] Attempting base64 decode")
                    try:
                        decoded = base64.b64decode(text, validate=True)
                        is_net_decoded, first_decoded, reason_decoded = _preview_cookies_content(decoded)
                        if is_net_decoded:
                            _log("info", "[_read_cookies_file] Successfully decoded base64 Netscape format")
                            return decoded, "base64-decoded Netscape format"
                        else:
                            _log("warning", f"[_read_cookies_file] Base64 decoded but not valid Netscape: {reason_decoded}")
                            return None, f"File appears to be base64 but decoded content is not valid Netscape: {reason_decoded}"
                    except Exception as e:
                        # Try fixing padding
                        _log("info", f"[_read_cookies_file] Base64 decode failed ({str(e)}), attempting padding fix")
                        try:
                            # Add padding if needed
                            missing_padding = len(text) % 4
                            if missing_padding:
                                text += '=' * (4 - missing_padding)
                                _log("info", f"[_read_cookies_file] Added {4 - missing_padding} padding characters")
                            
                            decoded = base64.b64decode(text, validate=True)
                            is_net_decoded, first_decoded, reason_decoded = _preview_cookies_content(decoded)
                            if is_net_decoded:
                                _log("info", "[_read_cookies_file] Successfully decoded base64 after padding fix")
                                return decoded, "base64-decoded Netscape format (padding fixed)"
                            else:
                                _log("warning", "[_read_cookies_file] Padding fix successful but content not valid Netscape")
                                return None, f"File appears to be base64 but decoded content is not valid Netscape: {reason_decoded}"
                        except Exception as e2:
                            _log("error", f"[_read_cookies_file] Base64 decode failed even after padding fix: {str(e2)}")
                            return None, f"File appears to be base64 but decoding failed: {str(e2)}"
        except Exception:
            pass
        
        _log("warning", f"[_read_cookies_file] Could not detect valid format: {reason}")
        return None, reason
    except Exception as e:
        _log("error", f"[_read_cookies_file] Error reading file: {str(e)}")
        return None, f"Error reading file: {str(e)}"


def _preview_cookies_file(path: str, max_bytes: int = 4096) -> Tuple[bool, int, str, bool, str]:
    """
    Return (exists, size, first_line, is_netscape, reason) for a cookies file path.
    Automatically detects and decodes base64-encoded Netscape cookies with padding fix.
    """
    try:
        exists = os.path.isfile(path) and os.access(path, os.R_OK)
        if not exists:
            return False, 0, "", False, "File not found or not readable."
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            content = f.read(max_bytes)
        
        # First, try to interpret as-is
        is_net, first, reason = _preview_cookies_content(content)
        if is_net:
            return True, size, first, is_net, reason
        
        # If not valid Netscape, check if it might be base64-encoded
        try:
            text = content.decode("utf-8", errors="replace").strip()
            lines = text.splitlines()
            if len(lines) <= 2:  # base64 might have one or two lines
                b64_chars = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\n\r \t")
                if set(text) <= b64_chars and len(text.strip()) > 0:
                    # Attempt base64 decode
                    try:
                        decoded = base64.b64decode(text, validate=True)
                        is_net_decoded, first_decoded, reason_decoded = _preview_cookies_content(decoded)
                        if is_net_decoded:
                            return True, size, first_decoded, is_net_decoded, "base64-decoded Netscape format"
                        else:
                            return True, size, first, False, f"File appears to be base64 but decoded content is not valid Netscape: {reason_decoded}"
                    except Exception:
                        # Try fixing padding
                        try:
                            missing_padding = len(text) % 4
                            if missing_padding:
                                text += '=' * (4 - missing_padding)
                            decoded = base64.b64decode(text, validate=True)
                            is_net_decoded, first_decoded, reason_decoded = _preview_cookies_content(decoded)
                            if is_net_decoded:
                                return True, size, first_decoded, is_net_decoded, "base64-decoded Netscape format (padding fixed)"
                            else:
                                return True, size, first, False, f"Base64 padding fixed but content not valid Netscape: {reason_decoded}"
                        except Exception:
                            pass
        except Exception:
            pass
        
        # If we reach here, original validation failed and base64 decode either failed or wasn't applicable
        return True, size, first, is_net, reason
    except Exception as e:
        return False, 0, "", False, f"Error reading file: {e}"


# PUBLIC_INTERFACE
def compress_audio(file_path: str, bitrate: str = "256k") -> None:
    """Re-encode and compress the MP3 file to a target bitrate using pydub/ffmpeg."""
    try:
        audio = AudioSegment.from_file(file_path)
        # Export back to the same path with the specified bitrate
        audio.export(file_path, format="mp3", bitrate=bitrate)
    except Exception as e:
        # If compression fails, keep the original file
        print(f"[compress_audio] Warning: compression failed for {file_path}: {e}")


# PUBLIC_INTERFACE
def parse_duration_str(duration_str: Optional[str]) -> Optional[int]:
    """Parse a duration string like '4:35' or '1:02:10' into seconds. Returns None if cannot parse."""
    if not duration_str or not isinstance(duration_str, str):
        return None
    parts = duration_str.split(":")
    try:
        if len(parts) == 2:
            minutes, seconds = map(int, parts)
            return minutes * 60 + seconds
        elif len(parts) == 3:
            hours, minutes, seconds = map(int, parts)
            return hours * 3600 + minutes * 60 + seconds
        return None
    except Exception:
        return None


# PUBLIC_INTERFACE
def generate(file_path: str, start: int, end: int, chunk_size: int = 8192) -> Generator[bytes, None, None]:
    """Yield a file segment in chunks for partial content responses."""
    with open(file_path, "rb") as f:
        f.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            read_len = min(chunk_size, remaining)
            data = f.read(read_len)
            if not data:
                break
            yield data
            remaining -= len(data)


# PUBLIC_INTERFACE
def parse_range_header(range_header: Optional[str], file_size: int) -> Optional[Tuple[int, int]]:
    """Parse the Range header. Returns (start, end) byte positions inclusive, or None if invalid/absent."""
    if not range_header:
        return None
    # Example: bytes=0-1023
    m = re.match(r"bytes=(\d*)-(\d*)", range_header)
    if not m:
        return None

    start_str, end_str = m.groups()
    if start_str == "" and end_str == "":
        return None

    if start_str:
        start = int(start_str)
        end = file_size - 1 if not end_str else int(end_str)
    else:
        # suffix-byte-range-spec, e.g., bytes=-500 (last 500 bytes)
        suffix_length = int(end_str)
        start = max(file_size - suffix_length, 0)
        end = file_size - 1

    start = max(0, start)
    end = min(end, file_size - 1)
    if start > end:
        return None
    return start, end


# PUBLIC_INTERFACE
def make_partial_response(file_path: str, start: int, end: int) -> Response:
    """Return a 206 Partial Content response for the specified byte range."""
    file_size = os.path.getsize(file_path)
    chunk_gen = generate(file_path, start, end)
    rv = Response(chunk_gen, status=206, mimetype="audio/mpeg")
    rv.headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    rv.headers["Accept-Ranges"] = "bytes"
    rv.headers["Content-Length"] = str(end - start + 1)
    rv.headers["Cache-Control"] = "public, max-age=7200"
    # Provide a stable filename via Content-Disposition for clients/downloaders
    rv.headers["Content-Disposition"] = f'inline; filename="{os.path.basename(file_path)}"'
    return rv


# PUBLIC_INTERFACE
def make_entire_response(filename: str) -> Response:
    """Return the entire file using send_from_directory with proper headers."""
    rv = send_from_directory(AUDIOS_DIR, filename, mimetype="audio/mpeg", as_attachment=False)
    rv.headers["Accept-Ranges"] = "bytes"
    rv.headers["Cache-Control"] = "public, max-age=7200"
    rv.headers["Content-Disposition"] = f'inline; filename="{filename}"'
    return rv


# PUBLIC_INTERFACE
def delete_expired_files() -> None:
    """Delete .mp3 files from the AUDIOS_DIR older than RETENTION_SECONDS."""
    now = _now_ts()
    for name in os.listdir(AUDIOS_DIR):
        if not name.lower().endswith(".mp3"):
            continue
        fp = os.path.join(AUDIOS_DIR, name)
        try:
            mtime = int(os.path.getmtime(fp))
            if now - mtime > RETENTION_SECONDS:
                os.remove(fp)
        except Exception as e:
            print(f"[delete_expired_files] Failed to process {fp}: {e}")


# PUBLIC_INTERFACE
def delete_files_task() -> None:
    """Background task that periodically deletes expired audio files."""
    while True:
        try:
            delete_expired_files()
        except Exception as e:
            print(f"[delete_files_task] Error: {e}")
        time.sleep(300)  # run every 5 minutes


# PUBLIC_INTERFACE
def run() -> None:
    """No-op runner placeholder to align with provided structure."""
    pass


# PUBLIC_INTERFACE
def keep_alive() -> None:
    """Keep-alive task to prevent idling in some hosting environments."""
    while True:
        time.sleep(600)


def _download_audio_to_local_mp3(url: str, cookies_b64: Optional[str], header_b64: Optional[str]) -> Tuple[Optional[str], Optional[dict], Optional[str], Optional[bool], Optional[str]]:
    """
    Reuse the same flow as /download to:
      - validate ffmpeg
      - handle cookies (header or body precedence over fallback file over env)
      - probe yt-dlp and enforce <= 5 minutes
      - download audio and return local mp3 path

    Returns:
      (mp3_path, info, cookiefile_path, delete_tmp_cookiefile, cookie_source)
      On failure, returns (None, {"error": ..., "service_status": ...}, cookiefile_path, delete_tmp_cookiefile, cookie_source)
    """
    if shutil.which("ffmpeg") is None:
        return None, {
            "error": "FFmpeg is required but was not found on the system PATH.",
            "action": "Please install FFmpeg and ensure it is available on PATH.",
            "service_status": "unavailable",
            "tips": {
                "debian_ubuntu": "sudo apt-get update && sudo apt-get install -y ffmpeg",
                "macos_brew": "brew install ffmpeg",
                "windows_choco": "choco install ffmpeg",
                "windows_scoop": "scoop install ffmpeg",
            },
        }, None, None, None

    cookiefile_path: Optional[str] = None
    delete_tmp_cookiefile: bool = False
    cookie_source = "none"

    # Prefer header_b64 then cookies_b64, else fallback file, else env
    b64_value = header_b64 or cookies_b64
    if b64_value:
        try:
            decoded = base64.b64decode(b64_value, validate=True)
        except Exception as e:
            return None, {
                "error": "Invalid base64 in cookies value.",
                "details": str(e),
                "guidance": "Pass base64-encoded Netscape cookies.txt text. Do not base64-encode base64 again.",
                "link": "https://github.com/yt-dlp/yt-dlp#how-do-i-pass-cookies",
            }, None, None, None

        is_net, first_line, reason = _preview_cookies_content(decoded)
        if not is_net:
            return None, {
                "error": "Cookies value is not in Netscape cookies.txt format.",
                "reason": reason,
                "preview_first_line": first_line,
                "guidance": "Export cookies as Netscape-format cookies.txt (e.g., using 'Get cookies.txt' browser extension). Ensure cookies include YouTube domain and necessary authentication tokens.",
                "link": "https://github.com/yt-dlp/yt-dlp#how-do-i-pass-cookies",
            }, None, None, None

        try:
            with tempfile.NamedTemporaryFile(prefix="ydl_cookies_", suffix=".txt", delete=False) as tf:
                try:
                    os.chmod(tf.name, 0o600)
                except Exception:
                    pass
                tf.write(decoded)
                cookiefile_path = tf.name
                delete_tmp_cookiefile = True
                cookie_source = "provided_b64"
                temp_abs_path = os.path.abspath(cookiefile_path)
                sanitized_first = first_line[:80] + ('...' if len(first_line) > 80 else '')
                _log("info", f"[download] ✓ Created secure temp cookie file: {temp_abs_path} (size={len(decoded)}B, permissions=0600, first_line='{sanitized_first}')")
                _log("info", "[download] Temp cookie file will be passed to yt-dlp and deleted after request completes")
        except Exception as e:
            return None, {"error": f"Failed to create temporary cookie file: {e}"}, None, None, None
    else:
        # Try fallback file first - resolve absolute path
        fallback_abs_path = os.path.abspath(FALLBACK_COOKIES_FILE)
        _log("info", f"[download] Checking fallback cookie file: {fallback_abs_path}")
        
        if os.path.exists(FALLBACK_COOKIES_FILE):
            # Use enhanced reader that detects and decodes base64
            decoded_content, format_desc = _read_cookies_file(FALLBACK_COOKIES_FILE)
            
            if decoded_content:
                # Content is valid - check if we need to write to temp file or use directly
                if "base64-decoded" in format_desc:
                    # Write decoded content to temp file
                    try:
                        with tempfile.NamedTemporaryFile(prefix="ydl_cookies_fallback_", suffix=".txt", delete=False) as tf:
                            try:
                                os.chmod(tf.name, 0o600)
                            except Exception:
                                pass
                            tf.write(decoded_content)
                            cookiefile_path = tf.name
                            delete_tmp_cookiefile = True
                            cookie_source = "file_b64"
                            _log("info", f"[download] ✓ Using fallback cookie file: {fallback_abs_path} ({format_desc}, size={len(decoded_content)}B, written to temp: {cookiefile_path})")
                    except Exception as e:
                        _log("warning", f"[download] ✗ Failed to write decoded fallback cookies to temp file: {e}")
                else:
                    # Use file directly (raw Netscape format)
                    cookiefile_path = FALLBACK_COOKIES_FILE
                    cookie_source = "file"
                    _log("info", f"[download] ✓ Using fallback cookie file: {fallback_abs_path} ({format_desc}, size={len(decoded_content)}B)")
            else:
                _log("warning", f"[download] ✗ Fallback cookie file invalid: {format_desc}. File will NOT be used.")
        else:
            _log("info", f"[download] Fallback cookie file does not exist at: {fallback_abs_path}")
        
        # Fall back to env if fallback file not available
        if not cookiefile_path:
            env_cookie_path = os.getenv("YTDLP_COOKIES_FILE")
            if env_cookie_path:
                env_abs_path = os.path.abspath(env_cookie_path)
                _log("info", f"[download] Checking env cookie file: {env_abs_path}")
                
                decoded_content, format_desc = _read_cookies_file(env_cookie_path)
                
                if decoded_content:
                    if "base64-decoded" in format_desc:
                        # Write decoded content to temp file
                        try:
                            with tempfile.NamedTemporaryFile(prefix="ydl_cookies_env_", suffix=".txt", delete=False) as tf:
                                try:
                                    os.chmod(tf.name, 0o600)
                                except Exception:
                                    pass
                                tf.write(decoded_content)
                                cookiefile_path = tf.name
                                delete_tmp_cookiefile = True
                                cookie_source = "env_b64"
                                _log("info", f"[download] ✓ Using env cookie file: {env_abs_path} ({format_desc}, size={len(decoded_content)}B, written to temp: {cookiefile_path})")
                        except Exception as e:
                            _log("warning", f"[download] ✗ Failed to write decoded env cookies to temp file: {e}")
                    else:
                        # Use file directly
                        cookiefile_path = env_cookie_path
                        cookie_source = "env"
                        _log("info", f"[download] ✓ Using env cookie file: {env_abs_path} ({format_desc}, size={len(decoded_content)}B)")
                else:
                    _log("warning", f"[download] ✗ Env cookie file invalid: {format_desc}. File will NOT be used.")
            else:
                _log("info", "[download] No YTDLP_COOKIES_FILE environment variable set")

    # Probe with enhanced options
    ydl_probe_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "user_agent": USER_AGENT,
    }
    if cookiefile_path:
        ydl_probe_opts["cookiefile"] = cookiefile_path
        _log("info", f"[download] → Passing cookiefile to yt-dlp probe: {os.path.abspath(cookiefile_path)}")
    
    _log("info", f"[download] Probing URL with cookie_source={cookie_source}, cookiefile={'set' if cookiefile_path else 'none'}")
    
    try:
        with YoutubeDL(ydl_probe_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        # cleanup temp cookie if created
        if delete_tmp_cookiefile and cookiefile_path:
            try:
                if os.path.exists(cookiefile_path):
                    os.remove(cookiefile_path)
                    _log("info", f"[download] Cleaned up temp cookie file after probe error: {cookiefile_path}")
            except Exception:
                pass
        error_msg = str(e)
        # Check if error is related to bot detection or authentication
        if any(keyword in error_msg.lower() for keyword in ["bot", "captcha", "sign in", "verify", "cookie", "login"]):
            return None, {
                "error": f"YouTube authentication required or bot detection triggered: {error_msg}",
                "suggestion": "This video may require authentication. Provide valid YouTube cookies via cookies_b64 parameter or place them in ./cookie/cookies.txt file.",
                "cookie_source": cookie_source,
                "status_code": 401 if "sign in" in error_msg.lower() or "login" in error_msg.lower() else 403,
            }, None, None, None
        return None, {"error": f"Failed to retrieve video info: {error_msg}", "cookie_source": cookie_source}, None, None, None

    duration = info.get("duration")
    if duration is None:
        if delete_tmp_cookiefile and cookiefile_path:
            try:
                if os.path.exists(cookiefile_path):
                    os.remove(cookiefile_path)
            except Exception:
                pass
        return None, {"error": "Unable to determine video duration."}, None, None, None
    if duration > 300:
        if delete_tmp_cookiefile and cookiefile_path:
            try:
                if os.path.exists(cookiefile_path):
                    os.remove(cookiefile_path)
            except Exception:
                pass
        return None, {"error": "Video is longer than 5 minutes and cannot be processed."}, None, None, None

    video_id = info.get("id")
    if not video_id:
        if delete_tmp_cookiefile and cookiefile_path:
            try:
                if os.path.exists(cookiefile_path):
                    os.remove(cookiefile_path)
            except Exception:
                pass
        return None, {"error": "Could not determine video id."}, None, None, None

    outtmpl = os.path.join(AUDIOS_DIR, "%(id)s.%(ext)s")
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "user_agent": USER_AGENT,
        "extractor_retries": 3,
        "fragment_retries": 3,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "256"}
        ],
    }
    if cookiefile_path:
        ydl_opts["cookiefile"] = cookiefile_path
        _log("info", f"[download] → Passing cookiefile to yt-dlp download: {os.path.abspath(cookiefile_path)}")

    _log("info", f"[download] Starting download for video_id={video_id} with cookie_source={cookie_source}")

    try:
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        msg = str(e)
        if "ffmpeg" in msg.lower():
            if delete_tmp_cookiefile and cookiefile_path:
                try:
                    if os.path.exists(cookiefile_path):
                        os.remove(cookiefile_path)
                except Exception:
                    pass
            return None, {
                "error": f"Download failed: {msg}",
                "action": "FFmpeg appears to be missing or inaccessible. Install FFmpeg and ensure it is on PATH.",
            }, None, None, None
        
        # Check for bot detection or cookie issues
        if any(keyword in msg.lower() for keyword in ["bot", "captcha", "sign in", "verify", "cookie", "login"]):
            if delete_tmp_cookiefile and cookiefile_path:
                try:
                    if os.path.exists(cookiefile_path):
                        os.remove(cookiefile_path)
                except Exception:
                    pass
            return None, {
                "error": f"YouTube authentication required or bot detection triggered: {msg}",
                "suggestion": "This video requires authentication. Provide valid YouTube cookies via cookies_b64 parameter or place them in ./cookie/cookies.txt file.",
                "cookie_source": cookie_source,
                "status_code": 401 if "sign in" in msg.lower() or "login" in msg.lower() else 403,
            }, None, None, None
            
        if delete_tmp_cookiefile and cookiefile_path:
            try:
                if os.path.exists(cookiefile_path):
                    os.remove(cookiefile_path)
            except Exception:
                pass
        return None, {"error": f"Download failed: {msg}", "cookie_source": cookie_source}, None, None, None

    mp3_path = os.path.join(AUDIOS_DIR, f"{video_id}.mp3")
    if not os.path.exists(mp3_path):
        candidates = [p for p in os.listdir(AUDIOS_DIR) if p.startswith(video_id) and p.endswith(".mp3")]
        if candidates:
            mp3_path = os.path.join(AUDIOS_DIR, candidates[0])
        else:
            if delete_tmp_cookiefile and cookiefile_path:
                try:
                    if os.path.exists(cookiefile_path):
                        os.remove(cookiefile_path)
                except Exception:
                    pass
            return None, {"error": "MP3 file not found after download."}, None, None, None

    # Compress to standard bitrate, ignore errors
    compress_audio(mp3_path, bitrate="256k")
    _log("info", f"[download] Successfully downloaded and processed video_id={video_id}")
    return mp3_path, info, cookiefile_path, delete_tmp_cookiefile, cookie_source


# PUBLIC_INTERFACE
@blp.route("/summarize", methods=["POST"])
@limiter.limit("3 per minute")
@blp.doc(
    summary="Transcribe and summarize a YouTube video's audio using Groq.",
    description="Accepts JSON with 'url' (required) and 'cookies_b64' (optional base64 Netscape cookies.txt). "
                "If cookies_b64 is not provided, falls back to reading cookies from ./cookie/cookies.txt. "
                "Downloads audio using the same logic as /download (<= 5 minutes), transcribes via Groq Whisper, "
                "and generates a concise summary and recommended title via Groq gpt-oss-20b. "
                "Returns only summary, recommended_title, full_transcript.",
    requestBody={
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["url"],
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "YouTube video URL (<= 5 minutes)",
                        },
                        "cookies_b64": {
                            "type": "string",
                            "description": "Optional: base64-encoded Netscape cookies.txt content. If not provided, falls back to ./cookie/cookies.txt.",
                        },
                    },
                }
            }
        },
    },
)
def summarize():
    """
    Transcribe and summarize YouTube audio.

    Body:
      - url (str, required): YouTube URL (<= 5 minutes)
      - cookies_b64 (str, optional): base64 Netscape cookies.txt; if not provided, falls back to ./cookie/cookies.txt

    Returns:
      JSON with exactly:
        - summary (str)
        - recommended_title (str)
        - full_transcript (str)

    Errors:
      400 for missing/invalid inputs, 401/403 for authentication issues, 422 for parsing errors, 500 for server issues, 503 for missing dependencies.
    """
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    cookies_b64 = body.get("cookies_b64")

    if not url:
        return {"error": "Missing required JSON field 'url'."}, 400

    # Check ffmpeg availability early and return 503 if missing
    if shutil.which("ffmpeg") is None:
        return {
            "error": "FFmpeg is required but was not found on the system PATH.",
            "action": "Please install FFmpeg and ensure it is available on PATH.",
            "service_status": "unavailable",
            "tips": {
                "debian_ubuntu": "sudo apt-get update && sudo apt-get install -y ffmpeg",
                "macos_brew": "brew install ffmpeg",
                "windows_choco": "choco install ffmpeg",
                "windows_scoop": "scoop install ffmpeg",
            },
        }, 503

    # Ensure GROQ_API_KEY present
    groq_key = os.getenv("GROQ_API_KEY")
    if not groq_key:
        return {
            "error": "Server misconfiguration: GROQ_API_KEY is not set.",
            "action": "Set GROQ_API_KEY environment variable for Groq client authentication.",
            "service_status": "misconfigured"
        }, 503
    
    # Log configuration status (sanitized)
    _log("info", f"[/summarize] Configuration check: GROQ_API_KEY={'present' if groq_key else 'missing'}, length={len(groq_key) if groq_key else 0}")

    # Reuse download logic to fetch local mp3 (with cookies delivered via body or fallback file)
    try:
        mp3_path, info, cookiefile_path, delete_tmp_cookiefile, cookie_source = _download_audio_to_local_mp3(
            url=url,
            cookies_b64=cookies_b64,
            header_b64=None
        )

        # If error dict returned in 'info'
        if mp3_path is None:
            # Attempt cleanup of any temp cookie
            if delete_tmp_cookiefile and cookiefile_path:
                try:
                    if os.path.exists(cookiefile_path):
                        os.remove(cookiefile_path)
                        _log("info", f"[/summarize] Cleaned up temp cookie file: {cookiefile_path}")
                except Exception:
                    pass
            # Determine appropriate status code based on error type
            error_msg = info.get("error", "").lower() if info else ""
            status_code = info.get("status_code")
            if status_code:
                return info, status_code
            if "ffmpeg" in error_msg or "unavailable" in info.get("service_status", ""):
                return info, 503
            elif "authentication" in error_msg or "bot detection" in error_msg:
                return info, 401
            elif any(x in error_msg for x in ["invalid", "format", "base64", "cookies"]):
                return info, 400
            return info or {"error": "Unable to download audio."}, 400
    except Exception as e:
        _log("error", f"[/summarize] Unhandled exception in download: {str(e)}")
        return {"error": f"Download failed: {str(e)}"}, 500

    # Transcribe with OpenAI client pointing to Groq API
    transcript_text = ""
    try:
        # Log OpenAI client initialization with environment check
        groq_api_key = os.getenv("GROQ_API_KEY")
        if groq_api_key:
            _log("info", f"[/summarize] GROQ_API_KEY present: {groq_api_key[:8]}...{groq_api_key[-4:] if len(groq_api_key) > 12 else '***'}")
        else:
            _log("warning", "[/summarize] GROQ_API_KEY not found in environment")
        
        # Initialize OpenAI client with Groq base URL
        _log("info", "[/summarize] Initializing OpenAI client with Groq base_url for Whisper transcription")
        client = OpenAI(
            base_url="https://api.groq.com/openai/v1",
            api_key=groq_api_key
        )
        _log("info", f"[/summarize] ✓ OpenAI client (Groq) initialized: {type(client).__name__}")
        
        filename = os.path.basename(mp3_path)
        with open(mp3_path, "rb") as f:
            file_bytes = f.read()
            transcription = client.audio.transcriptions.create(
                file=(filename, file_bytes),
                model="whisper-large-v3",
                temperature=0,
                response_format="verbose_json",
            )
        # transcription may be an object; support dict-like as fallback
        transcript_text = getattr(transcription, "text", None) or (transcription.get("text") if isinstance(transcription, dict) else "")
        if not transcript_text:
            return {"error": "Transcription failed: empty transcript."}, 500
    except Exception as e:
        _log("error", f"[/summarize] OpenAI/Groq Whisper transcription error: {str(e)}")
        # Clean temp cookie if created
        if delete_tmp_cookiefile and cookiefile_path:
            try:
                if os.path.exists(cookiefile_path):
                    os.remove(cookiefile_path)
                    _log("info", f"[/summarize] Cleaned up temp cookie file after transcription error: {cookiefile_path}")
            except Exception:
                pass
        return {
            "error": "Groq transcription service unavailable or failed.",
            "details": str(e),
            "action": "Verify GROQ_API_KEY is set correctly and Groq service is accessible."
        }, 502

    # Build prompt and summarize with gpt-oss-20b
    def _chunk_text(txt: str, max_len: int = 8000):
        if len(txt) <= max_len:
            return [txt]
        chunks = []
        start = 0
        while start < len(txt):
            chunks.append(txt[start:start + max_len])
            start += max_len
        return chunks

    try:
        # Initialize OpenAI client with Groq base URL for summarization
        _log("info", "[/summarize] Initializing OpenAI client with Groq base_url for GPT summarization")
        client = OpenAI(
            base_url="https://api.groq.com/openai/v1",
            api_key=os.getenv("GROQ_API_KEY")
        )
        _log("info", f"[/summarize] ✓ OpenAI client (Groq) GPT initialized: {type(client).__name__}")
        
        # If transcript very large, include only first chunk to stay within token limits
        chunks = _chunk_text(transcript_text, 8000)
        prompt = (
            "You are an assistant. Given the full transcript of a video, produce:\n"
            "- summary: A concise, factual English summary (150-250 words) of the video's content.\n"
            "- recommended_title: A punchy, SEO-friendly English title (<= 80 characters).\n"
            "Return ONLY a minified JSON object with keys: summary, recommended_title.\n"
            f"Transcript:\n{chunks[0]}"
        )
        completion = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=1500,
            top_p=1,
        )
        content = completion.choices[0].message.content if completion and completion.choices else ""
        data = {}
        try:
            data = json.loads(content)
        except Exception:
            # Attempt to salvage JSON by stripping surrounding text
            try:
                start_idx = content.find("{")
                end_idx = content.rfind("}")
                if start_idx != -1 and end_idx != -1:
                    data = json.loads(content[start_idx:end_idx + 1])
            except Exception:
                return {"error": "Failed to parse summarization JSON from model."}, 422

        summary = data.get("summary", "").strip()
        recommended_title = data.get("recommended_title", "").strip()
        if not summary or not recommended_title:
            return {"error": "Model response missing required fields: summary or recommended_title."}, 422

        return {
            "summary": summary,
            "recommended_title": recommended_title,
            "full_transcript": transcript_text,
        }, 200
    except Exception as e:
        _log("error", f"[/summarize] OpenAI/Groq summarization error: {str(e)}")
        return {
            "error": "Groq summarization service unavailable or failed.",
            "details": str(e),
            "action": "Verify GROQ_API_KEY is set correctly and Groq service is accessible."
        }, 502
    finally:
        # cleanup temp cookie if created by helper
        if delete_tmp_cookiefile and cookiefile_path:
            try:
                if os.path.exists(cookiefile_path):
                    os.remove(cookiefile_path)
                    _log("info", f"[/summarize] ✓ Cleaned up temp cookie file: {cookiefile_path}")
            except Exception:
                pass


@blp.route("/search")
@limiter.limit("20 per minute")
class Search(MethodView):
    # PUBLIC_INTERFACE
    def get(self):
        """Search YouTube for videos under 5 minutes."""
        query = request.args.get("q", "", type=str).strip()
        if not query:
            return {"error": "Missing required query parameter 'q'."}, 400

        try:
            videos_search = VideosSearch(query, limit=15)
            data = videos_search.result()
            results = []
            for item in data.get("result", []):
                # Skip live streams or missing durations
                dur_str = item.get("duration")
                duration = parse_duration_str(dur_str)
                if duration is None or duration > 300:
                    continue

                title = item.get("title")
                link = item.get("link")
                thumbs = item.get("thumbnails") or []
                thumb_url = thumbs[0]["url"] if thumbs else None

                if link and title:
                    results.append(
                        {
                            "title": title,
                            "url": link,
                            "thumbnail": thumb_url,
                        }
                    )

            return {"results": results}, 200
        except Exception as e:
            return {"error": f"Search failed: {str(e)}"}, 500


# PUBLIC_INTERFACE
@blp.route("/download")
@limiter.limit("6 per minute")
@blp.doc(parameters=[
    {
        "name": "url",
        "in": "query",
        "required": True,
        "schema": {"type": "string"},
        "description": "YouTube video URL to download as MP3 (<= 5 minutes)",
        "example": "https://www.youtube.com/watch?v=abc123"
    },
    {
        "name": "X-YTDLP-Cookies",
        "in": "header",
        "required": False,
        "schema": {"type": "string"},
        "description": "Optional base64-encoded Netscape cookies.txt content for yt-dlp. If provided, it is written to a secure temp file for this request and takes precedence over the server-side YTDLP_COOKIES_FILE."
    }
])
def download():
    """Download YouTube audio as MP3 (<= 5 minutes).

    Summary:
        Download YouTube audio as an MP3 and return a JSON payload containing a direct link
        to stream the file and an expiration timestamp.

    Cookie Support:
        - Server-side file: If environment variable YTDLP_COOKIES_FILE is set and points to an existing
          Netscape-format cookies.txt file, it will be passed to yt-dlp.
        - Per-request header override: If the "X-YTDLP-Cookies" request header is provided, it must contain
          base64-encoded Netscape cookies.txt content. The server will write this content to a secure temporary
          file for the duration of the request and pass it to yt-dlp, taking precedence over YTDLP_COOKIES_FILE.
          The temporary file is deleted after the request.

    Returns:
        tuple: (JSON dict, HTTP status code). On success, includes:
            - img: Thumbnail URL.
            - direct_link: The path to stream the MP3 (GET /audios/<filename>).
            - expiration_timestamp: Unix epoch when the file will be deleted.

    Error Handling:
        - If 'url' is missing: 400 with message.
        - If invalid base64 is supplied for the X-YTDLP-Cookies header: 400.
        - If ffmpeg is not installed: 500 with actionable guidance.
        - If video is too long (> 5 minutes): 400.
        - If extraction/download fails: 400/500 with message.
    """
    url = request.args.get("url", "", type=str).strip()
    if not url:
        return {"error": "Missing required query parameter 'url'."}, 400

    # Validate ffmpeg availability early to provide a friendly message
    if shutil.which("ffmpeg") is None:
        return (
            {
                "error": "FFmpeg is required but was not found on the system PATH.",
                "action": "Please install FFmpeg and ensure it is available on PATH.",
                "tips": {
                    "debian_ubuntu": "sudo apt-get update && sudo apt-get install -y ffmpeg",
                    "macos_brew": "brew install ffmpeg",
                    "windows_choco": "choco install ffmpeg",
                    "windows_scoop": "scoop install ffmpeg",
                },
            },
            500,
        )

    cookiefile_path: Optional[str] = None
    delete_tmp_cookiefile: bool = False
    cookie_source = "none"

    # Read optional base64-encoded Netscape cookies from header (takes precedence)
    header_b64 = request.headers.get("X-YTDLP-Cookies")
    if header_b64:
        _log("info", "[/download] Using cookies from header (X-YTDLP-Cookies). Decoding and validating format.")
        try:
            decoded = base64.b64decode(header_b64, validate=True)
        except Exception:
            return {
                "error": "Invalid base64 in X-YTDLP-Cookies header.",
                "guidance": "Pass base64-encoded Netscape cookies.txt text. Do not base64-encode base64 again.",
                "link": "https://github.com/yt-dlp/yt-dlp#how-do-i-pass-cookies",
            }, 400

        # Validate Netscape format before writing
        is_net, first_line, reason = _preview_cookies_content(decoded)
        if not is_net:
            return {
                "error": "X-YTDLP-Cookies is not Netscape cookies.txt format.",
                "reason": reason,
                "preview_first_line": first_line,
                "guidance": "Export cookies as Netscape-format cookies.txt (e.g., using 'Get cookies.txt' browser extension).",
                "link": "https://github.com/yt-dlp/yt-dlp#how-do-i-pass-cookies",
            }, 400

        # Create a secure temporary file for this request only
        try:
            with tempfile.NamedTemporaryFile(prefix="ydl_cookies_", suffix=".txt", delete=False) as tf:
                # Restrict permissions on POSIX systems
                try:
                    os.chmod(tf.name, 0o600)
                except Exception:
                    pass
                tf.write(decoded)
                cookiefile_path = tf.name
                delete_tmp_cookiefile = True
                cookie_source = "header"
                exists = os.path.isfile(cookiefile_path)
                size = os.path.getsize(cookiefile_path) if exists else 0
                sanitized_first = first_line[:80] + ('...' if len(first_line) > 80 else '')
                _log("info", f"[/download] ✓ Header cookies temp file: {cookiefile_path} exists={exists} size={size}B first='{sanitized_first}'")
        except Exception as e:
            return {"error": f"Failed to create temporary cookie file: {e}"}, 500
    else:
        # Try fallback file first - resolve absolute path
        fallback_abs_path = os.path.abspath(FALLBACK_COOKIES_FILE)
        _log("info", f"[/download] Checking fallback cookie file: {fallback_abs_path}")
        
        if os.path.exists(FALLBACK_COOKIES_FILE):
            # Use enhanced reader that detects and decodes base64
            decoded_content, format_desc = _read_cookies_file(FALLBACK_COOKIES_FILE)
            
            if decoded_content:
                # Content is valid - check if we need to write to temp file or use directly
                if "base64-decoded" in format_desc:
                    # Write decoded content to temp file
                    try:
                        with tempfile.NamedTemporaryFile(prefix="ydl_cookies_fallback_", suffix=".txt", delete=False) as tf:
                            try:
                                os.chmod(tf.name, 0o600)
                            except Exception:
                                pass
                            tf.write(decoded_content)
                            cookiefile_path = tf.name
                            delete_tmp_cookiefile = True
                            cookie_source = "file_b64"
                            _log("info", f"[/download] ✓ Using fallback cookie file: {fallback_abs_path} ({format_desc}, size={len(decoded_content)}B, written to temp: {cookiefile_path})")
                    except Exception as e:
                        _log("warning", f"[/download] ✗ Failed to write decoded fallback cookies to temp file: {e}")
                else:
                    # Use file directly (raw Netscape format)
                    cookiefile_path = FALLBACK_COOKIES_FILE
                    cookie_source = "file"
                    _log("info", f"[/download] ✓ Using fallback cookie file: {fallback_abs_path} ({format_desc}, size={len(decoded_content)}B)")
            else:
                _log("warning", f"[/download] ✗ Fallback cookie file invalid: {format_desc}. File will NOT be used.")
        else:
            _log("info", f"[/download] Fallback cookie file does not exist at: {fallback_abs_path}")
        
        # Fallback to server-side cookies file via environment variable
        if not cookiefile_path:
            env_cookie_path = os.getenv("YTDLP_COOKIES_FILE")
            if env_cookie_path:
                env_abs_path = os.path.abspath(env_cookie_path)
                _log("info", f"[/download] Checking env cookie file: {env_abs_path}")
                
                decoded_content, format_desc = _read_cookies_file(env_cookie_path)
                
                if decoded_content:
                    if "base64-decoded" in format_desc:
                        # Write decoded content to temp file
                        try:
                            with tempfile.NamedTemporaryFile(prefix="ydl_cookies_env_", suffix=".txt", delete=False) as tf:
                                try:
                                    os.chmod(tf.name, 0o600)
                                except Exception:
                                    pass
                                tf.write(decoded_content)
                                cookiefile_path = tf.name
                                delete_tmp_cookiefile = True
                                cookie_source = "env_b64"
                                _log("info", f"[/download] ✓ Using env cookie file: {env_abs_path} ({format_desc}, size={len(decoded_content)}B, written to temp: {cookiefile_path})")
                        except Exception as e:
                            _log("warning", f"[/download] ✗ Failed to write decoded env cookies to temp file: {e}")
                    else:
                        # Use file directly
                        cookiefile_path = env_cookie_path
                        cookie_source = "env"
                        _log("info", f"[/download] ✓ Using env cookie file: {env_abs_path} ({format_desc}, size={len(decoded_content)}B)")
                else:
                    _log("warning", f"[/download] ✗ Env cookie file invalid: {format_desc}. File will NOT be used.")
            else:
                _log("info", "[/download] No YTDLP_COOKIES_FILE environment variable set")

    # Probe metadata first to enforce duration limit
    ydl_probe_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "user_agent": USER_AGENT,
    }
    if cookiefile_path:
        ydl_probe_opts["cookiefile"] = cookiefile_path
        _log("info", f"[/download] → Passing cookiefile to yt-dlp probe: {os.path.abspath(cookiefile_path)}")
    
    _log("info", f"[/download] yt-dlp probe opts cookiefile={ydl_probe_opts.get('cookiefile')} source={cookie_source}")

    try:
        with YoutubeDL(ydl_probe_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        # Ensure cleanup of temp file on early failure
        if delete_tmp_cookiefile and cookiefile_path:
            try:
                if os.path.exists(cookiefile_path):
                    os.remove(cookiefile_path)
                    _log("info", f"[/download] Cleaned up temp cookie file after probe error: {cookiefile_path}")
            except Exception:
                pass
        debug = {
            "cookie_source": cookie_source,
            "cookiefile": cookiefile_path,
        }
        error_msg = str(e)
        if any(keyword in error_msg.lower() for keyword in ["bot", "captcha", "sign in", "verify", "login"]):
            status_code = 401 if "sign in" in error_msg.lower() or "login" in error_msg.lower() else 403
            return {
                "error": f"YouTube authentication required or bot detection triggered: {error_msg}",
                "suggestion": "This video requires authentication. Provide valid YouTube cookies via X-YTDLP-Cookies header or place them in ./cookie/cookies.txt file.",
                "debug": debug
            }, status_code
        return {"error": f"Failed to retrieve video info: {error_msg}", "debug": debug}, 400

    duration = info.get("duration")  # seconds
    if duration is None:
        if delete_tmp_cookiefile and cookiefile_path:
            try:
                if os.path.exists(cookiefile_path):
                    os.remove(cookiefile_path)
            except Exception:
                pass
        return {"error": "Unable to determine video duration."}, 400
    if duration > 300:
        if delete_tmp_cookiefile and cookiefile_path:
            try:
                if os.path.exists(cookiefile_path):
                    os.remove(cookiefile_path)
            except Exception:
                pass
        return {"error": "Video is longer than 5 minutes and cannot be processed."}, 400

    video_id = info.get("id")
    if not video_id:
        if delete_tmp_cookiefile and cookiefile_path:
            try:
                if os.path.exists(cookiefile_path):
                    os.remove(cookiefile_path)
            except Exception:
                pass
        return {"error": "Could not determine video id."}, 400

    outtmpl = os.path.join(AUDIOS_DIR, "%(id)s.%(ext)s")
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "user_agent": USER_AGENT,
        "extractor_retries": 3,
        "fragment_retries": 3,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "256",
            }
        ],
    }
    if cookiefile_path:
        ydl_opts["cookiefile"] = cookiefile_path
        _log("info", f"[/download] → Passing cookiefile to yt-dlp download: {os.path.abspath(cookiefile_path)}")
    
    _log("info", f"[/download] yt-dlp download opts cookiefile={ydl_opts.get('cookiefile')} source={cookie_source}")

    try:
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        # yt-dlp errors when ffmpeg is missing; provide a more actionable hint
        msg = str(e)
        if "ffmpeg" in msg.lower():
            if delete_tmp_cookiefile and cookiefile_path:
                try:
                    if os.path.exists(cookiefile_path):
                        os.remove(cookiefile_path)
                except Exception:
                    pass
            return (
                {
                    "error": f"Download failed: {msg}",
                    "action": "FFmpeg appears to be missing or inaccessible. Install FFmpeg and ensure it is on PATH.",
                },
                500,
            )
        if delete_tmp_cookiefile and cookiefile_path:
            try:
                if os.path.exists(cookiefile_path):
                    os.remove(cookiefile_path)
            except Exception:
                pass
        debug = {
            "cookie_source": cookie_source,
            "cookiefile": cookiefile_path,
        }
        if any(keyword in msg.lower() for keyword in ["bot", "captcha", "sign in", "verify", "login"]):
            status_code = 401 if "sign in" in msg.lower() or "login" in msg.lower() else 403
            return {
                "error": f"YouTube authentication required or bot detection triggered: {msg}",
                "suggestion": "This video requires authentication. Provide valid YouTube cookies via X-YTDLP-Cookies header or place them in ./cookie/cookies.txt file.",
                "debug": debug
            }, status_code
        return {"error": f"Download failed: {msg}", "debug": debug}, 500
    finally:
        # Ensure temp cookie file is deleted after the operation completes
        if delete_tmp_cookiefile and cookiefile_path:
            try:
                if os.path.exists(cookiefile_path):
                    os.remove(cookiefile_path)
                    _log("info", f"[/download] ✓ Deleted header temp cookies file: {cookiefile_path}")
            except Exception:
                # Silently ignore cleanup errors
                pass

    mp3_path = os.path.join(AUDIOS_DIR, f"{video_id}.mp3")
    if not os.path.exists(mp3_path):
        # Some videos use different extensions; find the first mp3 for this id
        candidates = [p for p in os.listdir(AUDIOS_DIR) if p.startswith(video_id) and p.endswith(".mp3")]
        if candidates:
            mp3_path = os.path.join(AUDIOS_DIR, candidates[0])
        else:
            return {"error": "MP3 file not found after download."}, 500

    # Final compression/standardization pass (ignore errors internally)
    compress_audio(mp3_path, bitrate="256k")

    expiration_ts = _now_ts() + RETENTION_SECONDS
    thumbnail = info.get("thumbnail")
    filename = os.path.basename(mp3_path)
    direct_link = f"/audios/{filename}"

    return {
        "img": thumbnail,
        "direct_link": direct_link,
        "expiration_timestamp": expiration_ts,
    }, 200


# PUBLIC_INTERFACE
@blp.route("/download", methods=["POST"])
@limiter.limit("6 per minute")
@blp.doc(
    summary="Download YouTube audio as MP3 (<= 5 minutes) via JSON body.",
    description="Accepts a JSON body with 'url' (required) and optional 'cookies_b64'. "
                "If 'cookies_b64' is not provided, falls back to reading cookies from ./cookie/cookies.txt. "
                "If 'cookies_b64' is provided, it must be base64-encoded Netscape cookies.txt content "
                "and will be written to a secure temporary file for this request, taking precedence over "
                "the fallback file and server-side YTDLP_COOKIES_FILE. The temporary file is deleted after the request.",
    requestBody={
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["url"],
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "YouTube video URL to download as MP3 (<= 5 minutes)",
                            "example": "https://www.youtube.com/watch?v=abc123"
                        },
                        "cookies_b64": {
                            "type": "string",
                            "description": "Optional base64-encoded Netscape cookies.txt content for yt-dlp; takes precedence over fallback file and YTDLP_COOKIES_FILE."
                        }
                    }
                }
            }
        }
    }
)
def download_post():
    """Download YouTube audio as MP3 (<= 5 minutes) via POST JSON.

    Body:
        - url (str, required): YouTube video URL to download as MP3 (<= 5 minutes)
        - cookies_b64 (str, optional): base64-encoded Netscape cookies.txt content. If not provided,
          falls back to ./cookie/cookies.txt. If provided, it will be written to a secure temporary 
          file for the duration of the request and used by yt-dlp, taking precedence over fallback file
          and YTDLP_COOKIES_FILE.

    Returns:
        tuple: (JSON dict, HTTP status code). On success, includes:
            - img: Thumbnail URL.
            - direct_link: The path to stream the MP3 (GET /audios/<filename>).
            - expiration_timestamp: Unix epoch when the file will be deleted.

    Error Handling:
        - If 'url' is missing: 400 with message.
        - If invalid base64 is supplied for 'cookies_b64': 400.
        - If 'cookies_b64' content is not Netscape cookies.txt format: 400.
        - If ffmpeg is not installed: 500 with actionable guidance.
        - If video is too long (> 5 minutes): 400.
        - If extraction/download fails: 400/500 with message.
        - If authentication required: 401/403 with clear message.
    """
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    if not url:
        return {"error": "Missing required JSON field 'url'."}, 400

    # Validate ffmpeg availability early to provide a friendly message
    if shutil.which("ffmpeg") is None:
        return (
            {
                "error": "FFmpeg is required but was not found on the system PATH.",
                "action": "Please install FFmpeg and ensure it is available on PATH.",
                "tips": {
                    "debian_ubuntu": "sudo apt-get update && sudo apt-get install -y ffmpeg",
                    "macos_brew": "brew install ffmpeg",
                    "windows_choco": "choco install ffmpeg",
                    "windows_scoop": "scoop install ffmpeg",
                },
            },
            500,
        )

    cookiefile_path: Optional[str] = None
    delete_tmp_cookiefile: bool = False
    cookie_source = "none"

    # Read optional base64-encoded Netscape cookies from JSON body (takes precedence)
    cookies_b64 = body.get("cookies_b64")
    if cookies_b64:
        _log("info", "[POST /download] Using cookies from JSON body. Decoding and validating format.")
        try:
            decoded = base64.b64decode(cookies_b64, validate=True)
        except Exception:
            return {
                "error": "Invalid base64 in 'cookies_b64'.",
                "guidance": "Pass base64-encoded Netscape cookies.txt text. Do not base64-encode base64 again.",
                "link": "https://github.com/yt-dlp/yt-dlp#how-do-i-pass-cookies",
            }, 400

        # Validate Netscape format before writing
        is_net, first_line, reason = _preview_cookies_content(decoded)
        if not is_net:
            return {
                "error": "'cookies_b64' is not Netscape cookies.txt format.",
                "reason": reason,
                "preview_first_line": first_line,
                "guidance": "Export cookies as Netscape-format cookies.txt (e.g., using 'Get cookies.txt' browser extension).",
                "link": "https://github.com/yt-dlp/yt-dlp#how-do-i-pass-cookies",
            }, 400

        # Create a secure temporary file for this request only
        try:
            with tempfile.NamedTemporaryFile(prefix="ydl_cookies_", suffix=".txt", delete=False) as tf:
                # Restrict permissions on POSIX systems
                try:
                    os.chmod(tf.name, 0o600)
                except Exception:
                    pass
                tf.write(decoded)
                cookiefile_path = tf.name
                delete_tmp_cookiefile = True
                cookie_source = "body"
                exists = os.path.isfile(cookiefile_path)
                size = os.path.getsize(cookiefile_path) if exists else 0
                sanitized_first = first_line[:80] + ('...' if len(first_line) > 80 else '')
                _log("info", f"[POST /download] ✓ Body cookies temp file: {cookiefile_path} exists={exists} size={size}B first='{sanitized_first}'")
        except Exception as e:
            return {"error": f"Failed to create temporary cookie file: {e}"}, 500
    else:
        # Try fallback file first - resolve absolute path
        fallback_abs_path = os.path.abspath(FALLBACK_COOKIES_FILE)
        _log("info", f"[POST /download] Checking fallback cookie file: {fallback_abs_path}")
        
        if os.path.exists(FALLBACK_COOKIES_FILE):
            # Use enhanced reader that detects and decodes base64
            decoded_content, format_desc = _read_cookies_file(FALLBACK_COOKIES_FILE)
            
            if decoded_content:
                # Content is valid - check if we need to write to temp file or use directly
                if "base64-decoded" in format_desc:
                    # Write decoded content to temp file
                    try:
                        with tempfile.NamedTemporaryFile(prefix="ydl_cookies_fallback_", suffix=".txt", delete=False) as tf:
                            try:
                                os.chmod(tf.name, 0o600)
                            except Exception:
                                pass
                            tf.write(decoded_content)
                            cookiefile_path = tf.name
                            delete_tmp_cookiefile = True
                            cookie_source = "file_b64"
                            _log("info", f"[POST /download] ✓ Using fallback cookie file: {fallback_abs_path} ({format_desc}, size={len(decoded_content)}B, written to temp: {cookiefile_path})")
                    except Exception as e:
                        _log("warning", f"[POST /download] ✗ Failed to write decoded fallback cookies to temp file: {e}")
                else:
                    # Use file directly (raw Netscape format)
                    cookiefile_path = FALLBACK_COOKIES_FILE
                    cookie_source = "file"
                    _log("info", f"[POST /download] ✓ Using fallback cookie file: {fallback_abs_path} ({format_desc}, size={len(decoded_content)}B)")
            else:
                _log("warning", f"[POST /download] ✗ Fallback cookie file invalid: {format_desc}. File will NOT be used.")
        else:
            _log("info", f"[POST /download] Fallback cookie file does not exist at: {fallback_abs_path}")
        
        # Fallback to server-side cookies file via environment variable
        if not cookiefile_path:
            env_cookie_path = os.getenv("YTDLP_COOKIES_FILE")
            if env_cookie_path:
                env_abs_path = os.path.abspath(env_cookie_path)
                _log("info", f"[POST /download] Checking env cookie file: {env_abs_path}")
                
                decoded_content, format_desc = _read_cookies_file(env_cookie_path)
                
                if decoded_content:
                    if "base64-decoded" in format_desc:
                        # Write decoded content to temp file
                        try:
                            with tempfile.NamedTemporaryFile(prefix="ydl_cookies_env_", suffix=".txt", delete=False) as tf:
                                try:
                                    os.chmod(tf.name, 0o600)
                                except Exception:
                                    pass
                                tf.write(decoded_content)
                                cookiefile_path = tf.name
                                delete_tmp_cookiefile = True
                                cookie_source = "env_b64"
                                _log("info", f"[POST /download] ✓ Using env cookie file: {env_abs_path} ({format_desc}, size={len(decoded_content)}B, written to temp: {cookiefile_path})")
                        except Exception as e:
                            _log("warning", f"[POST /download] ✗ Failed to write decoded env cookies to temp file: {e}")
                    else:
                        # Use file directly
                        cookiefile_path = env_cookie_path
                        cookie_source = "env"
                        _log("info", f"[POST /download] ✓ Using env cookie file: {env_abs_path} ({format_desc}, size={len(decoded_content)}B)")
                else:
                    _log("warning", f"[POST /download] ✗ Env cookie file invalid: {format_desc}. File will NOT be used.")
            else:
                _log("info", "[POST /download] No YTDLP_COOKIES_FILE environment variable set")

    # Probe metadata first to enforce duration limit
    ydl_probe_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "user_agent": USER_AGENT,
    }
    if cookiefile_path:
        ydl_probe_opts["cookiefile"] = cookiefile_path
        _log("info", f"[POST /download] → Passing cookiefile to yt-dlp probe: {os.path.abspath(cookiefile_path)}")
    
    _log("info", f"[POST /download] yt-dlp probe opts cookiefile={ydl_probe_opts.get('cookiefile')} source={cookie_source}")

    try:
        with YoutubeDL(ydl_probe_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        # Ensure cleanup of temp file on early failure
        if delete_tmp_cookiefile and cookiefile_path:
            try:
                if os.path.exists(cookiefile_path):
                    os.remove(cookiefile_path)
                    _log("info", f"[POST /download] Cleaned up temp cookie file after probe error: {cookiefile_path}")
            except Exception:
                pass
        debug = {
            "cookie_source": cookie_source,
            "cookiefile": cookiefile_path,
        }
        error_msg = str(e)
        if any(keyword in error_msg.lower() for keyword in ["bot", "captcha", "sign in", "verify", "login"]):
            status_code = 401 if "sign in" in error_msg.lower() or "login" in error_msg.lower() else 403
            return {
                "error": f"YouTube authentication required or bot detection triggered: {error_msg}",
                "suggestion": "This video requires authentication. Provide valid YouTube cookies via cookies_b64 field or place them in ./cookie/cookies.txt file.",
                "debug": debug
            }, status_code
        return {"error": f"Failed to retrieve video info: {error_msg}", "debug": debug}, 400

    duration = info.get("duration")  # seconds
    if duration is None:
        if delete_tmp_cookiefile and cookiefile_path:
            try:
                if os.path.exists(cookiefile_path):
                    os.remove(cookiefile_path)
            except Exception:
                pass
        return {"error": "Unable to determine video duration."}, 400
    if duration > 300:
        if delete_tmp_cookiefile and cookiefile_path:
            try:
                if os.path.exists(cookiefile_path):
                    os.remove(cookiefile_path)
            except Exception:
                pass
        return {"error": "Video is longer than 5 minutes and cannot be processed."}, 400

    video_id = info.get("id")
    if not video_id:
        if delete_tmp_cookiefile and cookiefile_path:
            try:
                if os.path.exists(cookiefile_path):
                    os.remove(cookiefile_path)
            except Exception:
                pass
        return {"error": "Could not determine video id."}, 400

    outtmpl = os.path.join(AUDIOS_DIR, "%(id)s.%(ext)s")
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "user_agent": USER_AGENT,
        "extractor_retries": 3,
        "fragment_retries": 3,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "256",
            }
        ],
    }
    if cookiefile_path:
        ydl_opts["cookiefile"] = cookiefile_path
        _log("info", f"[POST /download] → Passing cookiefile to yt-dlp download: {os.path.abspath(cookiefile_path)}")
    
    _log("info", f"[POST /download] yt-dlp download opts cookiefile={ydl_opts.get('cookiefile')} source={cookie_source}")

    try:
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        msg = str(e)
        if "ffmpeg" in msg.lower():
            if delete_tmp_cookiefile and cookiefile_path:
                try:
                    if os.path.exists(cookiefile_path):
                        os.remove(cookiefile_path)
                except Exception:
                    pass
            return (
                {
                    "error": f"Download failed: {msg}",
                    "action": "FFmpeg appears to be missing or inaccessible. Install FFmpeg and ensure it is on PATH.",
                },
                500,
            )
        if delete_tmp_cookiefile and cookiefile_path:
            try:
                if os.path.exists(cookiefile_path):
                    os.remove(cookiefile_path)
            except Exception:
                pass
        debug = {
            "cookie_source": cookie_source,
            "cookiefile": cookiefile_path,
        }
        if any(keyword in msg.lower() for keyword in ["bot", "captcha", "sign in", "verify", "login"]):
            status_code = 401 if "sign in" in msg.lower() or "login" in msg.lower() else 403
            return {
                "error": f"YouTube authentication required or bot detection triggered: {msg}",
                "suggestion": "This video requires authentication. Provide valid YouTube cookies via cookies_b64 field or place them in ./cookie/cookies.txt file.",
                "debug": debug
            }, status_code
        return {"error": f"Download failed: {msg}", "debug": debug}, 500
    finally:
        # Ensure temp cookie file is deleted after the operation completes
        if delete_tmp_cookiefile and cookiefile_path:
            try:
                if os.path.exists(cookiefile_path):
                    os.remove(cookiefile_path)
                    _log("info", f"[POST /download] ✓ Deleted body temp cookies file: {cookiefile_path}")
            except Exception:
                pass

    mp3_path = os.path.join(AUDIOS_DIR, f"{video_id}.mp3")
    if not os.path.exists(mp3_path):
        candidates = [p for p in os.listdir(AUDIOS_DIR) if p.startswith(video_id) and p.endswith(".mp3")]
        if candidates:
            mp3_path = os.path.join(AUDIOS_DIR, candidates[0])
        else:
            return {"error": "MP3 file not found after download."}, 500

    compress_audio(mp3_path, bitrate="256k")

    expiration_ts = _now_ts() + RETENTION_SECONDS
    thumbnail = info.get("thumbnail")
    filename = os.path.basename(mp3_path)
    direct_link = f"/audios/{filename}"

    return {
        "img": thumbnail,
        "direct_link": direct_link,
        "expiration_timestamp": expiration_ts,
    }, 200


# PUBLIC_INTERFACE
@blp.route("/audios/<path:filename>")
@limiter.limit("60 per minute")
def audio_stream(filename: str):
    """Serve MP3 files with HTTP Range support (206 Partial Content).

    Parameters:
        filename (str): The path component identifying the audio file to stream.

    Returns:
        - 404 JSON if file not found.
        - 200 Response with full file and headers when Range header missing.
        - 206 Response with partial content and appropriate headers when Range header present.
    """
    file_path = os.path.join(AUDIOS_DIR, filename)
    if not os.path.isfile(file_path):
        return {"error": "File not found."}, 404

    file_size = os.path.getsize(file_path)
    range_header = request.headers.get("Range", None)
    byte_range = parse_range_header(range_header, file_size)

    if byte_range is None:
        # Entire file
        return make_entire_response(filename)

    start, end = byte_range
    return make_partial_response(file_path, start, end)
