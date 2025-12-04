import os
import re
import time
import shutil
from typing import Generator, Optional, Tuple

from flask import request, Response, send_from_directory
from flask.views import MethodView
from flask_smorest import Blueprint
from youtubesearchpython import VideosSearch
from yt_dlp import YoutubeDL
from pydub import AudioSegment

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
@blp.doc(parameters=[{
    "name": "url",
    "in": "query",
    "required": True,
    "schema": {"type": "string"},
    "description": "YouTube video URL to download as MP3 (<= 5 minutes)",
    "example": "https://www.youtube.com/watch?v=abc123"
}])
def download():
    """Download YouTube audio as MP3 (<= 5 minutes).

    Summary:
        Download YouTube audio as an MP3 and return a JSON payload containing a direct link
        to stream the file and an expiration timestamp.

    Returns:
        tuple: (JSON dict, HTTP status code). On success, includes:
            - img: Thumbnail URL.
            - direct_link: The path to stream the MP3 (GET /audios/<filename>).
            - expiration_timestamp: Unix epoch when the file will be deleted.

    Error Handling:
        - If 'url' is missing: 400 with message.
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

    # Probe metadata first to enforce duration limit
    ydl_probe_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    try:
        with YoutubeDL(ydl_probe_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        return {"error": f"Failed to retrieve video info: {str(e)}"}, 400

    duration = info.get("duration")  # seconds
    if duration is None:
        return {"error": "Unable to determine video duration."}, 400
    if duration > 300:
        return {"error": "Video is longer than 5 minutes and cannot be processed."}, 400

    video_id = info.get("id")
    if not video_id:
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

    try:
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        # yt-dlp errors when ffmpeg is missing; provide a more actionable hint
        msg = str(e)
        if "ffmpeg" in msg.lower():
            return (
                {
                    "error": f"Download failed: {msg}",
                    "action": "FFmpeg appears to be missing or inaccessible. Install FFmpeg and ensure it is on PATH.",
                },
                500,
            )
        return {"error": f"Download failed: {msg}"}, 500

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
