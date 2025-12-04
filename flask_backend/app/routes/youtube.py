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
from groq import Groq
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


def _preview_cookies_file(path: str, max_bytes: int = 4096) -> Tuple[bool, int, str, bool, str]:
    """
    Return (exists, size, first_line, is_netscape, reason) for a cookies file path.
    """
    try:
        exists = os.path.isfile(path) and os.access(path, os.R_OK)
        if not exists:
            return False, 0, "", False, "File not found or not readable."
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            content = f.read(max_bytes)
        is_net, first, reason = _preview_cookies_content(content)
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
      - handle cookies (header or body precedence over env)
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

    # Prefer header_b64 then cookies_b64, else env
    b64_value = header_b64 or cookies_b64
    if b64_value:
        try:
            decoded = base64.b64decode(b64_value, validate=True)
        except Exception:
            return None, {
                "error": "Invalid base64 in cookies value.",
                "guidance": "Pass base64-encoded Netscape cookies.txt text. Do not base64-encode base64 again.",
                "link": "https://github.com/yt-dlp/yt-dlp#how-do-i-pass-cookies",
            }, None, None, None

        is_net, first_line, reason = _preview_cookies_content(decoded)
        if not is_net:
            return None, {
                "error": "Cookies value is not Netscape cookies.txt format.",
                "reason": reason,
                "preview_first_line": first_line,
                "guidance": "Export cookies as Netscape-format cookies.txt (e.g., using 'Get cookies.txt' browser extension).",
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
        except Exception as e:
            return None, {"error": f"Failed to create temporary cookie file: {e}"}, None, None, None
    else:
        env_cookie_path = os.getenv("YTDLP_COOKIES_FILE")
        if env_cookie_path:
            exists, size, first_line, is_net, reason = _preview_cookies_file(env_cookie_path)
            if exists and is_net:
                cookiefile_path = env_cookie_path
                cookie_source = "env"

    # Probe
    ydl_probe_opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    if cookiefile_path:
        ydl_probe_opts["cookiefile"] = cookiefile_path
    try:
        with YoutubeDL(ydl_probe_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        # cleanup temp cookie if created
        if delete_tmp_cookiefile and cookiefile_path:
            try:
                if os.path.exists(cookiefile_path):
                    os.remove(cookiefile_path)
            except Exception:
                pass
        return None, {"error": f"Failed to retrieve video info: {str(e)}"}, None, None, None

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
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "256"}
        ],
    }
    if cookiefile_path:
        ydl_opts["cookiefile"] = cookiefile_path

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
        if delete_tmp_cookiefile and cookiefile_path:
            try:
                if os.path.exists(cookiefile_path):
                    os.remove(cookiefile_path)
            except Exception:
                pass
        return None, {"error": f"Download failed: {msg}"}, None, None, None

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
    return mp3_path, info, cookiefile_path, delete_tmp_cookiefile, cookie_source


# PUBLIC_INTERFACE
@blp.route("/summarize", methods=["POST"])
@limiter.limit("3 per minute")
@blp.doc(
    summary="Transcribe and summarize a YouTube video's audio using Groq.",
    description="Accepts JSON with 'url' (required) and 'cookies_b64' (required base64 Netscape cookies.txt). "
                "Downloads audio using the same logic as /download (<= 5 minutes), transcribes via Groq Whisper, "
                "and generates a concise summary and recommended title via Groq gpt-oss-20b. "
                "Returns only summary, recommended_title, full_transcript.",
    requestBody={
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["url", "cookies_b64"],
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "YouTube video URL (<= 5 minutes)",
                        },
                        "cookies_b64": {
                            "type": "string",
                            "description": "Required: base64-encoded Netscape cookies.txt content (same as /download).",
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
      - cookies_b64 (str, required): base64 Netscape cookies.txt; required to mirror /download-secured flow.

    Returns:
      JSON with exactly:
        - summary (str)
        - recommended_title (str)
        - full_transcript (str)

    Errors:
      400 for missing/invalid inputs, 422 for parsing errors, 500 for server issues, 503 for missing dependencies.
    """
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    cookies_b64 = body.get("cookies_b64")

    if not url:
        return {"error": "Missing required JSON field 'url'."}, 400
    if not cookies_b64:
        # Require per design to mirror protected flow
        return {"error": "Missing required JSON field 'cookies_b64' (base64 Netscape cookies.txt)."}, 400

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
    if not os.getenv("GROQ_API_KEY"):
        return {
            "error": "Server misconfiguration: GROQ_API_KEY is not set.",
            "action": "Set GROQ_API_KEY environment variable for Groq client authentication.",
            "service_status": "misconfigured"
        }, 503

    # Reuse download logic to fetch local mp3 (with cookies delivered via body)
    try:
        mp3_path, info, cookiefile_path, delete_tmp_cookiefile, _cookie_source = _download_audio_to_local_mp3(
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
                except Exception:
                    pass
            # Determine appropriate status code based on error type
            error_msg = info.get("error", "").lower() if info else ""
            if "ffmpeg" in error_msg:
                return info, 503
            elif any(x in error_msg for x in ["invalid", "format", "base64", "cookies"]):
                return info, 400
            return info or {"error": "Unable to download audio."}, 400
    except Exception as e:
        _log("error", f"[/summarize] Unhandled exception in download: {str(e)}")
        return {"error": f"Download failed: {str(e)}"}, 500

    # Transcribe with Groq Whisper
    transcript_text = ""
    try:
        client = Groq()
        with open(mp3_path, "rb") as f:
            transcription = client.audio.transcriptions.create(
                file=(mp3_path, f.read()),
                model="whisper-large-v3",
                temperature=0,
                response_format="verbose_json",
            )
        # transcription may be an object; support dict-like as fallback
        transcript_text = getattr(transcription, "text", None) or (transcription.get("text") if isinstance(transcription, dict) else "")
        if not transcript_text:
            return {"error": "Transcription failed: empty transcript."}, 500
    except Exception as e:
        _log("error", f"[/summarize] Transcription error: {str(e)}")
        # Clean temp cookie if created
        if delete_tmp_cookiefile and cookiefile_path:
            try:
                if os.path.exists(cookiefile_path):
                    os.remove(cookiefile_path)
            except Exception:
                pass
        return {"error": f"Transcription error: {str(e)}"}, 500

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
        client = Groq()
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
            model="openai/gpt-oss-20b",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_completion_tokens=1500,
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
        _log("error", f"[/summarize] Summarization error: {str(e)}")
        return {"error": f"Summarization error: {str(e)}"}, 500
    finally:
        # cleanup temp cookie if created by helper
        if delete_tmp_cookiefile and cookiefile_path:
            try:
                if os.path.exists(cookiefile_path):
                    os.remove(cookiefile_path)
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
                _log("info", f"[/download] Header cookies temp file: {cookiefile_path} exists={exists} size={size}B first='{first_line[:120]}'")
        except Exception as e:
            return {"error": f"Failed to create temporary cookie file: {e}"}, 500
    else:
        # Fallback to server-side cookies file via environment variable
        env_cookie_path = os.getenv("YTDLP_COOKIES_FILE")
        if env_cookie_path:
            exists, size, first_line, is_net, reason = _preview_cookies_file(env_cookie_path)
            _log("info", f"[/download] Env YTDLP_COOKIES_FILE='{env_cookie_path}' exists={exists} size={size}B first='{first_line[:120]}' netscape={is_net} reason='{reason}'")
            if exists:
                if is_net:
                    cookiefile_path = env_cookie_path
                    cookie_source = "env"
                else:
                    # Log and ignore invalid env cookie file; continue without cookies.
                    _log("warning", f"[/download] Env cookies file found but format invalid; ignoring. reason='{reason}'")
            else:
                _log("warning", "[/download] YTDLP_COOKIES_FILE set but file does not exist or is not readable; continuing without cookies.")

    # Probe metadata first to enforce duration limit
    ydl_probe_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    if cookiefile_path:
        ydl_probe_opts["cookiefile"] = cookiefile_path
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
            except Exception:
                pass
        debug = {
            "cookie_source": cookie_source,
            "cookiefile": cookiefile_path,
        }
        return {"error": f"Failed to retrieve video info: {str(e)}", "debug": debug}, 400

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
        return {"error": f"Download failed: {msg}", "debug": debug}, 500
    finally:
        # Ensure temp cookie file is deleted after the operation completes
        if delete_tmp_cookiefile and cookiefile_path:
            try:
                if os.path.exists(cookiefile_path):
                    os.remove(cookiefile_path)
                    _log("info", f"[/download] Deleted header temp cookies file: {cookiefile_path}")
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
                "If 'cookies_b64' is provided, it must be base64-encoded Netscape cookies.txt content "
                "and will be written to a secure temporary file for this request, taking precedence over "
                "the server-side YTDLP_COOKIES_FILE. The temporary file is deleted after the request.",
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
                            "description": "Optional base64-encoded Netscape cookies.txt content for yt-dlp; takes precedence over YTDLP_COOKIES_FILE."
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
        - cookies_b64 (str, optional): base64-encoded Netscape cookies.txt content. If provided,
          it will be written to a secure temporary file for the duration of the request and used
          by yt-dlp, taking precedence over YTDLP_COOKIES_FILE.

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
                _log("info", f"[POST /download] Body cookies temp file: {cookiefile_path} exists={exists} size={size}B first='{first_line[:120]}'")
        except Exception as e:
            return {"error": f"Failed to create temporary cookie file: {e}"}, 500
    else:
        # Fallback to server-side cookies file via environment variable
        env_cookie_path = os.getenv("YTDLP_COOKIES_FILE")
        if env_cookie_path:
            exists, size, first_line, is_net, reason = _preview_cookies_file(env_cookie_path)
            _log("info", f"[POST /download] Env YTDLP_COOKIES_FILE='{env_cookie_path}' exists={exists} size={size}B first='{first_line[:120]}' netscape={is_net} reason='{reason}'")
            if exists:
                if is_net:
                    cookiefile_path = env_cookie_path
                    cookie_source = "env"
                else:
                    # Log and ignore invalid env cookie file; continue without cookies.
                    _log("warning", f"[POST /download] Env cookies file found but format invalid; ignoring. reason='{reason}'")
            else:
                _log("warning", "[POST /download] YTDLP_COOKIES_FILE set but file does not exist or is not readable; continuing without cookies.")

    # Probe metadata first to enforce duration limit
    ydl_probe_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    if cookiefile_path:
        ydl_probe_opts["cookiefile"] = cookiefile_path
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
            except Exception:
                pass
        debug = {
            "cookie_source": cookie_source,
            "cookiefile": cookiefile_path,
        }
        return {"error": f"Failed to retrieve video info: {str(e)}", "debug": debug}, 400

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
        return {"error": f"Download failed: {msg}", "debug": debug}, 500
    finally:
        # Ensure temp cookie file is deleted after the operation completes
        if delete_tmp_cookiefile and cookiefile_path:
            try:
                if os.path.exists(cookiefile_path):
                    os.remove(cookiefile_path)
                    _log("info", f"[POST /download] Deleted body temp cookies file: {cookiefile_path}")
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
