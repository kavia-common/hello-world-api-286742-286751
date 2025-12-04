# Flask Backend - YouTube MP3 API

A Flask-based REST API for YouTube video processing, search, and MP3 conversion with transcription and summarization capabilities.

## Features

- YouTube video search (limited to videos ≤ 5 minutes)
- YouTube audio download as MP3 with cookie support
- Audio streaming with HTTP Range support (206 Partial Content)
- Transcription using Groq Whisper
- AI-powered summarization using Groq GPT
- Rate limiting and CORS support
- OpenAPI/Swagger documentation at `/docs`

## Requirements

### System Dependencies

**FFmpeg (Required)**
- FFmpeg is required for audio processing by yt-dlp and pydub
- The service will return HTTP 503 with a clear error message if FFmpeg is not available
- Install instructions:
  ```bash
  # Debian/Ubuntu
  sudo apt-get update && sudo apt-get install -y ffmpeg
  
  # macOS (Homebrew)
  brew install ffmpeg
  
  # Windows (Chocolatey)
  choco install ffmpeg
  
  # Windows (Scoop)
  scoop install ffmpeg
  ```

### Python Dependencies

All Python dependencies are listed in `requirements.txt`:
- Flask 3.0.3 with Werkzeug 3.x compatibility
- flask-smorest for OpenAPI/Swagger support
- flask-cors for CORS handling
- flask-limiter for rate limiting
- yt-dlp for YouTube downloading
- pydub for audio processing
- groq for AI transcription and summarization
- python-dotenv for environment variable management
- youtubesearchpython for YouTube search

Install with:
```bash
pip install -r requirements.txt
```

## Configuration

### Environment Variables

Configuration is managed via `.env` file in the flask_backend directory:

**Required:**
- `GROQ_API_KEY` - API key for Groq services (transcription and summarization)

**Optional:**
- `PORT` - Server port (default: 3001)
- `FLASK_ENV` - Flask environment (set to `production` for stability)
- `FLASK_DEBUG` - Debug mode flag (set to `0` to disable)
- `YTDLP_COOKIES_FILE` - Path to Netscape-format cookies.txt for yt-dlp

**Network/CORS:**
- `BACKEND_URL` - Backend URL for reference
- `FRONTEND_URL` - Frontend URL for CORS
- `ALLOWED_ORIGINS` - Comma-separated list of allowed CORS origins
- `ALLOWED_HEADERS` - Comma-separated list of allowed headers
- `ALLOWED_METHODS` - Comma-separated list of allowed HTTP methods

### Production Settings

For production stability and to prevent exit 143 errors:
- `debug=False` - Disables Flask debug mode
- `use_reloader=False` - Disables auto-reloader to prevent container restarts
- These are configured in `run.py`

## Running the Service

### Standard Method (Recommended)
```bash
python run.py
```

This uses the production-ready configuration with:
- Host: 0.0.0.0 (accepts connections from any interface)
- Port: From `PORT` environment variable or 3001
- Debug: Disabled
- Auto-reloader: Disabled

### Development Method
For development with auto-reload:
```bash
export FLASK_APP=app
export FLASK_ENV=development
flask run --host 0.0.0.0 --port 3001
```

## API Endpoints

### Health & Documentation
- `GET /` - Health check
- `GET /hello` - Hello World endpoint
- `GET /docs` - Swagger UI documentation

### YouTube Operations
- `GET /search?q=<query>` - Search YouTube (returns videos ≤ 5 minutes)
- `GET /download?url=<youtube_url>` - Download audio as MP3
- `POST /download` - Download audio via JSON body with optional cookies
- `POST /summarize` - Transcribe and summarize video

### Audio Streaming
- `GET /audios/<filename>` - Stream MP3 with Range support

## Error Handling

The API uses appropriate HTTP status codes:

**4xx Client Errors:**
- `400` - Bad request (missing/invalid parameters)
- `401` - Unauthorized (authentication required)
- `403` - Forbidden (bot detection or access denied)
- `404` - Resource not found

**5xx Server Errors:**
- `500` - Internal server error (unhandled exceptions)
- `503` - Service unavailable (missing FFmpeg or GROQ_API_KEY)

### FFmpeg Error Handling

If FFmpeg is not installed, endpoints that require it (`/download`, `/summarize`) will return:
```json
{
  "error": "FFmpeg is required but was not found on the system PATH.",
  "action": "Please install FFmpeg and ensure it is available on PATH.",
  "service_status": "unavailable",
  "tips": {
    "debian_ubuntu": "sudo apt-get update && sudo apt-get install -y ffmpeg",
    "macos_brew": "brew install ffmpeg",
    "windows_choco": "choco install ffmpeg",
    "windows_scoop": "scoop install ffmpeg"
  }
}
```
HTTP Status: `503 Service Unavailable`

### Authentication Error Handling

When YouTube requires authentication (age-restricted, private, or region-locked content), endpoints will return:
```json
{
  "error": "YouTube authentication required or bot detection triggered: ...",
  "suggestion": "This video requires authentication. Provide valid YouTube cookies via cookies_b64 parameter or place them in ./cookie/cookies.txt file.",
  "debug": {
    "cookie_source": "none",
    "cookiefile": null
  }
}
```
HTTP Status: `401 Unauthorized` or `403 Forbidden`

## Cookie Support

For age-restricted, private, or region-locked YouTube content, the API supports multiple cookie sources with the following priority:

### Priority Order (highest to lowest):
1. **Per-request cookies_b64** (POST /download and POST /summarize)
2. **Per-request X-YTDLP-Cookies header** (GET /download)
3. **Fallback file: ./cookie/cookies.txt** (relative to flask_backend container root)
4. **Environment variable: YTDLP_COOKIES_FILE**

### Cookie File Format

**All cookies must be in Netscape format.** This is a tab-separated text format used by cURL and many other tools.

**Example Netscape cookies.txt format:**
```
# Netscape HTTP Cookie File
# This is a generated file! Do not edit.
.youtube.com	TRUE	/	TRUE	1234567890	CONSENT	YES+1
.youtube.com	TRUE	/	FALSE	1234567890	VISITOR_INFO1_LIVE	abcdef123456
```

**Format Requirements:**
- First line should be: `# Netscape HTTP Cookie File`
- Each cookie line has 7 tab-separated fields:
  1. Domain
  2. Flag (TRUE/FALSE)
  3. Path
  4. Secure flag (TRUE/FALSE)
  5. Expiration timestamp
  6. Cookie name
  7. Cookie value

### Obtaining Cookies in Netscape Format

**Browser Extensions (Recommended):**
- Chrome/Edge: "Get cookies.txt LOCALLY" extension
- Firefox: "cookies.txt" extension
- These extensions export cookies in the correct Netscape format

**Steps:**
1. Install a "Get cookies.txt" browser extension
2. Log into YouTube in your browser
3. Visit any YouTube video
4. Click the extension icon to export cookies
5. Save the exported file

### Cookie Usage Examples

**1. Fallback File (Simplest for persistent use):**
```bash
# Place your cookies.txt in the flask_backend/cookie/ directory
mkdir -p flask_backend/cookie
cp ~/Downloads/cookies.txt flask_backend/cookie/cookies.txt

# The API will automatically use this file when cookies_b64 is not provided
curl -X POST "http://localhost:3001/download" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://www.youtube.com/watch?v=abc123"}'
```

**2. Per-request via JSON body (POST /download and POST /summarize):**
```bash
# Base64 encode your cookies file
COOKIES_B64=$(base64 -w 0 cookies.txt)

# Download endpoint
curl -X POST "http://localhost:3001/download" \
  -H "Content-Type: application/json" \
  -d "{\"url\":\"https://www.youtube.com/watch?v=abc123\",\"cookies_b64\":\"$COOKIES_B64\"}"

# Summarize endpoint
curl -X POST "http://localhost:3001/summarize" \
  -H "Content-Type: application/json" \
  -d "{\"url\":\"https://www.youtube.com/watch?v=abc123\",\"cookies_b64\":\"$COOKIES_B64\"}"
```

**3. Per-request via header (GET /download only):**
```bash
curl -G "http://localhost:3001/download" \
  --data-urlencode "url=https://www.youtube.com/watch?v=abc123" \
  -H "X-YTDLP-Cookies: $(base64 -w 0 cookies.txt)"
```

**4. Server-side environment variable:**
```bash
export YTDLP_COOKIES_FILE="/path/to/cookies.txt"
python run.py
```

### Cookie Source Logging

The application provides detailed logging for cookie file discovery, validation, and usage. This helps troubleshoot authentication issues and verify cookie file setup.

**Log output includes:**
- Absolute path resolution for `./cookie/cookies.txt` relative to flask_backend root
- File existence check and file size in bytes
- First non-comment line (sanitized to 80 characters to avoid leaking secrets)
- Netscape format validation with specific warnings if header is missing
- Confirmation when cookiefile is passed to yt-dlp
- Temporary cookiefile path and automatic cleanup confirmation

**Cookie source indicators:**
- `cookie_source=provided_b64` - Using cookies from request body/header (via cookies_b64 or X-YTDLP-Cookies)
- `cookie_source=file` - Using fallback file (./cookie/cookies.txt)
- `cookie_source=env` - Using environment variable (YTDLP_COOKIES_FILE)
- `cookie_source=none` - No cookies available

**Example log output:**
```
[INFO] [download] Checking fallback cookie file: /app/flask_backend/cookie/cookies.txt
[INFO] [download] Fallback file check: exists=True, size=1024B, is_netscape=True, first_line='# Netscape HTTP Cookie File...'
[INFO] [download] ✓ Using validated fallback cookie file: /app/flask_backend/cookie/cookies.txt (size=1024 bytes, format=Netscape)
[INFO] [download] → Passing cookiefile to yt-dlp probe: /app/flask_backend/cookie/cookies.txt
[INFO] [download] Probing URL with cookie_source=file, cookiefile=set
```

**Security notes on logging:**
- Cookie values are never logged in full
- First lines are truncated to 80 characters to prevent secret leakage
- Temporary files use secure permissions (0600) and are logged when created/deleted
- File paths are logged to help verify correct resolution

### Important Cookie Notes

1. **Fallback file location**: The `./cookie/cookies.txt` path is relative to the flask_backend container root (where run.py is located), not the working directory. Absolute path is logged for verification.
2. **Directory creation**: The `cookie/` directory will be created automatically if it doesn't exist
3. **Cookie freshness**: YouTube cookies can expire. If you get authentication errors, refresh your cookies
4. **Security**: Temporary cookie files created from base64 input are automatically deleted after each request and logged
5. **Format validation**: The API validates that cookies are in Netscape format and will reject invalid formats with helpful error messages. Missing format headers are specifically warned.
6. **Base64 encoding**: Only encode the cookies.txt file once - do not double-encode
7. **No cookies needed for public videos**: Most public YouTube videos work without cookies
8. **Auth-required detection**: When yt-dlp returns errors containing keywords like "bot", "captcha", "sign in", "verify", or "login", the API returns 401/403 with clear guidance to provide cookies
9. **Detailed logging**: All cookie operations (file checks, validation, yt-dlp handoff) are logged with sanitized output to help debug without exposing secrets

## File Management

- Downloaded MP3 files are stored in the `audios/` directory
- Files are automatically deleted after 2 hours
- Background cleanup task runs every 5 minutes

## Rate Limiting

Default rate limits (per IP):
- `/search`: 20 requests/minute
- `/download`: 6 requests/minute
- `/summarize`: 3 requests/minute
- `/audios/*`: 60 requests/minute

## Testing

Run tests with:
```bash
pytest tests/
```

## Troubleshooting

**Container exits with code 143:**
- Ensure `debug=False` and `use_reloader=False` in run.py
- Check that FLASK_ENV is set to `production` in .env

**HTTP 401/403 with authentication errors:**
- The video requires authentication (age-restricted, private, or region-locked)
- Provide valid YouTube cookies using one of the methods described above
- Ensure cookies are in Netscape format (use a browser extension)
- Check that cookies haven't expired - refresh if needed
- Verify the fallback file exists: `ls -la flask_backend/cookie/cookies.txt`

**HTTP 500 on /summarize:**
- Verify GROQ_API_KEY is set in environment
- Check FFmpeg is installed: `which ffmpeg`
- Review logs for specific error details

**Port already in use:**
- Change PORT in .env or kill existing process
- Check: `lsof -i :3001` or `netstat -an | grep 3001`

**Invalid cookies errors:**
- Ensure cookies are in Netscape format (not JSON or other formats)
- Use a browser extension like "Get cookies.txt LOCALLY" to export
- Base64 encode the cookies file, not pre-encoded content (encode only once)
- Verify cookies are fresh and not expired
- Check the first line contains: `# Netscape HTTP Cookie File`

**Fallback cookies not being used:**
- Verify file exists: `ls -la flask_backend/cookie/cookies.txt`
- Check file permissions are readable
- Review logs for cookie source: should show `cookie_source=file`
- Ensure file is in Netscape format (will be logged if invalid)

## Security Notes

- Cookies are stored in secure temporary files (0600 permissions) when provided via API
- Temporary cookie files are deleted after each request
- Fallback cookie file should have restricted permissions: `chmod 600 flask_backend/cookie/cookies.txt`
- Rate limiting protects against abuse
- CORS is configured to restrict origins (production should limit to known domains)
- Never commit cookies.txt files to version control (add to .gitignore)
