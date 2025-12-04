# hello-world-api-286742-286751

A simple Flask REST API exposing:
- Health endpoint: GET /
- Hello endpoint: GET /hello
- YouTube search: GET /search?q=<query> (returns results under 5 minutes)
- YouTube download (MP3): GET /download?url=<youtube_url> (returns direct_link + expiration)
- YouTube download (MP3) via JSON: POST /download with {"url": "<youtube_url>", "cookies_b64": "<base64>"} (returns direct_link + expiration)
- Audio streaming with Range support: GET /audios/<filename>
- Summarize: POST /summarize with {"url": "<youtube_url>", "cookies_b64": "<base64 netscape cookies.txt>"} (returns summary, recommended_title, full_transcript)

API documentation is available via Swagger UI at /docs.
In Swagger UI, the /download endpoints include:
- GET: required 'url' query parameter.
- POST: JSON requestBody with required 'url' and optional 'cookies_b64' (base64-encoded Netscape cookies.txt).

Getting started:
- Dependencies are listed in flask_backend/requirements.txt
- The service runs using flask_backend/run.py (production) or flask_backend/start.sh
- The server binds to 0.0.0.0 and uses PORT env var if set, default 3001
- For production stability: debug=False and use_reloader=False (configured in run.py)
- Configuration via .env file in flask_backend directory

Example:
- Health: GET https://<host>:3001/
- Hello: GET https://<host>:3001/hello
- Search: GET https://<host>:3001/search?q=lofi
- Download (GET): https://<host>:3001/download?url=https://www.youtube.com/watch?v=<id>
- Download (POST): https://<host>:3001/download
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

Cookie support (yt-dlp):
- Why: Some YouTube content requires cookies (e.g., age-restricted or region-locked).
- Server-side file (env): Set YTDLP_COOKIES_FILE in the environment to the absolute path of a Netscape-format cookies.txt file. If present and readable, it will be used for all requests.
  - Example (Linux/Mac): export YTDLP_COOKIES_FILE="/path/to/cookies.txt"
- Per-request override (header): For GET /download, send a request header "X-YTDLP-Cookies" containing a base64-encoded Netscape cookies.txt file content. This takes precedence over the env var for that request only. The server stores it in a secure temporary file and deletes it after use.
  - Example to base64-encode (Linux/Mac): `base64 -w 0 cookies.txt`
  - curl example:
    ```
    curl -G "https://<host>:3001/download" \
      --data-urlencode "url=https://www.youtube.com/watch?v=<id>" \
      -H "X-YTDLP-Cookies: $(base64 -w 0 cookies.txt)"
    ```

New: POST /download with cookies_b64 (JSON)
- You can also use a JSON body to pass the URL and optional cookies, which may be more convenient in some clients or when using Swagger UI.
- Body schema:
  - url (string, required): YouTube URL to download as MP3 (<= 5 minutes)
  - cookies_b64 (string, optional): base64-encoded Netscape cookies.txt content. If provided, takes precedence over YTDLP_COOKIES_FILE for that request.

curl examples:
- Without cookies:
  ```
  curl -X POST "https://<host>:3001/download" \
    -H "Content-Type: application/json" \
    -d '{"url":"https://www.youtube.com/watch?v=<id>"}'
  ```
- With cookies_b64:
  ```
  COOKIES_B64=$(base64 -w 0 cookies.txt)
  curl -X POST "https://<host>:3001/download" \
    -H "Content-Type: application/json" \
    -d "{\"url\":\"https://www.youtube.com/watch?v=<id>\",\"cookies_b64\":\"$COOKIES_B64\"}"
  ```

Using Swagger UI:

New: POST /summarize (Groq Whisper + gpt-oss-20b)
- Requires environment variable GROQ_API_KEY to be set.
- Body:
  {
    "url": "https://www.youtube.com/watch?v=<id>",
    "cookies_b64": "<base64 netscape cookies.txt>"
  }
- Response:
  {
    "summary": "...",
    "recommended_title": "...",
    "full_transcript": "..."
  }

curl example:
  COOKIES_B64=$(base64 -w 0 cookies.txt)
  curl -X POST "https://<host>:3001/summarize" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $GROQ_API_KEY (set in server env only; not needed in client)" \
    -d "{\"url\":\"https://www.youtube.com/watch?v=<id>\",\"cookies_b64\":\"$COOKIES_B64\"}"
- Open https://<host>:3001/docs
- Expand "YouTube MP3" -> POST /download
- Click "Try it out", set the JSON body:
  {
    "url": "https://www.youtube.com/watch?v=<id>",
    "cookies_b64": "<optional base64 of Netscape cookies.txt>"
  }
- Execute the request and follow the "direct_link" to stream the MP3.

How to export cookies:
- Use a browser extension that exports cookies in Netscape format (e.g., "cookies.txt" for Chrome/Firefox).
- Ensure the file is in the standard "Netscape HTTP Cookie File" format; yt-dlp requires this format.
- Security note: Treat cookies like credentials. Prefer the per-request field for single-use scenarios and avoid committing cookie files to source control.
