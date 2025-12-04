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

## Cookie Support

For age-restricted or region-locked YouTube content:

**Server-side (environment):**
```bash
export YTDLP_COOKIES_FILE="/path/to/cookies.txt"
```

**Per-request (header for GET /download):**
```bash
curl -G "http://localhost:3001/download" \
  --data-urlencode "url=https://www.youtube.com/watch?v=abc123" \
  -H "X-YTDLP-Cookies: $(base64 -w 0 cookies.txt)"
```

**Per-request (JSON body for POST):**
```bash
COOKIES_B64=$(base64 -w 0 cookies.txt)
curl -X POST "http://localhost:3001/download" \
  -H "Content-Type: application/json" \
  -d "{\"url\":\"https://www.youtube.com/watch?v=abc123\",\"cookies_b64\":\"$COOKIES_B64\"}"
```

Cookies must be in Netscape format. Export using browser extensions like "Get cookies.txt".

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

**HTTP 500 on /summarize:**
- Verify GROQ_API_KEY is set in environment
- Check FFmpeg is installed: `which ffmpeg`
- Review logs for specific error details

**Port already in use:**
- Change PORT in .env or kill existing process
- Check: `lsof -i :3001` or `netstat -an | grep 3001`

**Invalid cookies errors:**
- Ensure cookies are in Netscape format (not JSON or other formats)
- Base64 encode the cookies file, not pre-encoded content
- Verify cookies are fresh and not expired

## Security Notes

- Cookies are stored in secure temporary files (0600 permissions)
- Temporary cookie files are deleted after each request
- Rate limiting protects against abuse
- CORS is configured to restrict origins (production should limit to known domains)
