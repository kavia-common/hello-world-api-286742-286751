# hello-world-api-286742-286751

A simple Flask REST API exposing:
- Health endpoint: GET /
- Hello endpoint: GET /hello
- YouTube search: GET /search?q=<query> (returns results under 5 minutes)
- YouTube download (MP3): GET /download?url=<youtube_url> (returns direct_link + expiration)
- Audio streaming with Range support: GET /audios/<filename>

API documentation is available via Swagger UI at /docs.
In Swagger UI, the /download endpoint exposes a required 'url' query parameter; provide a valid YouTube URL (e.g., https://www.youtube.com/watch?v=abc123) to test it.

Getting started:
- Dependencies are listed in flask_backend/requirements.txt
- The service runs using flask_backend/run.py
- The server binds to 0.0.0.0 and uses PORT env var if set, default 3001.

Example:
- Health: GET https://<host>:3001/
- Hello: GET https://<host>:3001/hello
- Search: GET https://<host>:3001/search?q=lofi
- Download: GET https://<host>:3001/download?url=https://www.youtube.com/watch?v=<id>
- Stream: GET https://<host>:3001/audios/<id>.mp3
- Docs:  GET https://<host>:3001/docs

Notes:
- FFmpeg is required at runtime for audio processing by yt-dlp and pydub.
  Please ensure FFmpeg is installed in the deployment environment and available on PATH.

  Quick install tips:
  - Debian/Ubuntu: `sudo apt-get update && sudo apt-get install -y ffmpeg`
  - macOS (Homebrew): `brew install ffmpeg`
  - Windows (Chocolatey): `choco install ffmpeg`
  - Windows (Scoop): `scoop install ffmpeg`

- Files in /audios are retained for 2 hours and then cleaned up automatically.
- Rate limiting is enabled per IP to protect the service.
- Streaming headers: The /audios/<filename> route returns `Content-Type: audio/mpeg`,
  includes `Accept-Ranges: bytes` (supports partial content), and sets
  `Content-Disposition: inline; filename="<filename>"`.