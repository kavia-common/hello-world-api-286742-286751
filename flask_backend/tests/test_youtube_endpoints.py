import os
import json
import base64
import shutil
import pytest

# Import the Flask app and youtube module to access AUDIOS_DIR and patch internals
from app import app as flask_app  # noqa: E402
from app.routes import youtube as yt_module  # noqa: E402


@pytest.fixture(autouse=True)
def _test_config(tmp_path, monkeypatch):
    """
    Common test fixture:
    - Ensures app testing mode and test client.
    - Ensures FFmpeg presence checks can be controlled via monkeypatch.
    - Prevents background re-encoding by stubbing compress_audio to no-op.
    """
    flask_app.testing = True
    # avoid running actual ffmpeg/pydub work in tests
    monkeypatch.setattr(yt_module, "compress_audio", lambda *args, **kwargs: None)
    yield


def _fake_youtubedl_factory(expected_id="abc123", duration=120, thumbnail="http://thumb/img.jpg", create_mp3=True):
    """
    Build a fake YoutubeDL context manager class compatible with yt_dlp.YoutubeDL usage.
    - extract_info: returns dict with id/duration/thumbnail
    - download: writes a small fake mp3 file to AUDIOS_DIR with the expected id (if create_mp3 True)
    """

    class FakeYDL:
        def __init__(self, opts=None):
            self.opts = opts or {}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, url, download=False):
            return {
                "id": expected_id,
                "duration": duration,
                "thumbnail": thumbnail,
            }

        def download(self, urls):
            if not create_mp3:
                return
            # Write a minimal MP3-like file for testing
            mp3_path = os.path.join(yt_module.AUDIOS_DIR, f"{expected_id}.mp3")
            os.makedirs(os.path.dirname(mp3_path), exist_ok=True)
            with open(mp3_path, "wb") as f:
                # Write ID3 tag header and a few bytes to resemble MP3
                f.write(b"ID3")
                f.write(b"\x00" * 1024)

    return FakeYDL


def test_download_missing_url_returns_400(monkeypatch):
    client = flask_app.test_client()
    # Simulate ffmpeg exists to avoid earlier 500
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ffmpeg")
    resp = client.get("/download")
    assert resp.status_code == 400
    data = resp.get_json()
    assert "error" in data


def test_download_ffmpeg_missing_returns_500(monkeypatch):
    client = flask_app.test_client()
    # Simulate missing ffmpeg
    monkeypatch.setattr(shutil, "which", lambda name: None)
    resp = client.get("/download?url=https://youtu.be/short")
    assert resp.status_code == 500
    data = resp.get_json()
    assert "FFmpeg" in (data.get("error", "") + json.dumps(data))


def test_download_success_and_stream_headers(monkeypatch):
    client = flask_app.test_client()

    # Simulate ffmpeg present
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ffmpeg")
    # Patch yt_dlp.YoutubeDL to fake download and metadata
    FakeYDL = _fake_youtubedl_factory()
    monkeypatch.setattr(yt_module, "YoutubeDL", FakeYDL)

    # Hit /download
    resp = client.get("/download?url=https://www.youtube.com/watch?v=abc123")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert "direct_link" in payload
    assert payload["direct_link"].startswith("/audios/")
    filename = payload["direct_link"].split("/audios/")[1]

    # Now stream the audio without Range header (should be 200 full content)
    stream_resp = client.get(f"/audios/{filename}")
    assert stream_resp.status_code == 200
    # Validate headers
    assert stream_resp.mimetype in ("audio/mpeg", "audio/mp3")
    # Content-Disposition should include filename
    cd = stream_resp.headers.get("Content-Disposition", "")
    assert "filename=" in cd
    assert filename in cd

    # Now request with Range header to validate 206 and headers
    partial_resp = client.get(f"/audios/{filename}", headers={"Range": "bytes=0-99"})
    assert partial_resp.status_code == 206
    assert partial_resp.headers.get("Accept-Ranges") == "bytes"
    assert partial_resp.headers.get("Content-Range", "").startswith("bytes 0-")
    # Content-Disposition also for partial
    cd2 = partial_resp.headers.get("Content-Disposition", "")
    assert "filename=" in cd2
    assert filename in cd2


def test_download_uses_header_cookies_and_cleans_up_temp(monkeypatch):
    client = flask_app.test_client()

    # Simulate ffmpeg present
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ffmpeg")

    seen_opts = []

    class FakeYDL:
        def __init__(self, opts=None):
            self.opts = opts or {}
            seen_opts.append(self.opts)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, url, download=False):
            return {
                "id": "xyz789",
                "duration": 120,
                "thumbnail": "http://thumb/img2.jpg",
            }

        def download(self, urls):
            # Write a small fake mp3
            mp3_path = os.path.join(yt_module.AUDIOS_DIR, "xyz789.mp3")
            os.makedirs(os.path.dirname(mp3_path), exist_ok=True)
            with open(mp3_path, "wb") as f:
                f.write(b"ID3")
                f.write(b"\x00" * 256)

    monkeypatch.setattr(yt_module, "YoutubeDL", FakeYDL)

    # Prepare a simple cookies.txt-like content and base64-encode it
    cookies_content = b"# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tFALSE\t0\tsid\tabcdef\n"
    header_value = base64.b64encode(cookies_content).decode("ascii")

    # Call endpoint with header
    resp = client.get(
        "/download",
        query_string={"url": "https://www.youtube.com/watch?v=xyz789"},
        headers={"X-YTDLP-Cookies": header_value},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert "direct_link" in payload

    # Validate that cookiefile was passed to yt-dlp during both probe and download phases
    cookie_paths = []
    for opts in seen_opts:
        assert "cookiefile" in opts
        cookie_paths.append(opts["cookiefile"])
        # Temp file should have existed at the time yt-dlp was constructed
        assert os.path.isabs(opts["cookiefile"])
        assert os.path.exists(opts["cookiefile"])

    # After the request completes, the temp file should have been deleted
    # (header cookies take precedence, and temp file is removed in finally)
    for p in set(cookie_paths):
        assert not os.path.exists(p)
