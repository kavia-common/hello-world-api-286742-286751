# Deployment Guide - Flask Backend

## Overview

This document covers deployment best practices, configuration, and troubleshooting for the Flask backend service.

## Pre-Deployment Checklist

### 1. System Requirements

- **Python 3.8+** - Required runtime
- **FFmpeg** - Required for audio processing
- **pip** - Python package manager

Verify:
```bash
python --version
ffmpeg -version
pip --version
```

### 2. Install Dependencies

```bash
cd flask_backend
pip install -r requirements.txt
```

Verify installation:
```bash
python -c "import flask, flask_cors, flask_smorest, flask_limiter, yt_dlp, pydub, groq, dotenv; print('OK')"
```

### 3. Environment Configuration

Copy and configure `.env` file:

**Required Variables:**
```env
GROQ_API_KEY=your_groq_api_key_here
```

**Production Settings (already configured):**
```env
FLASK_ENV=production
FLASK_DEBUG=0
PORT=3001
```

**Optional - Cookie Support:**
```env
YTDLP_COOKIES_FILE=/path/to/cookies.txt
```

## Deployment Methods

### Method 1: Direct Python (Recommended for Production)

```bash
cd flask_backend
python run.py
```

**Configuration in run.py:**
- `debug=False` - Disables Flask debug mode
- `use_reloader=False` - Disables auto-reloader to prevent exit 143
- `host="0.0.0.0"` - Binds to all interfaces
- `port=3001` - Default port (configurable via PORT env var)

### Method 2: Using Startup Script

```bash
cd flask_backend
./start.sh
```

This script:
- Loads environment variables from .env
- Verifies FFmpeg is available
- Verifies GROQ_API_KEY is set
- Starts the server with production settings

### Method 3: Development Mode (Not for Production)

```bash
export FLASK_APP=app
export FLASK_ENV=development
flask run --host 0.0.0.0 --port 3001
```

**Warning:** Development mode enables:
- Debug mode (exposes sensitive information on errors)
- Auto-reloader (can cause exit 143 in containers)
- Not suitable for production deployment

## Container Deployment

### Docker Considerations

If running in a Docker container:

1. **Disable Debug and Reloader** (already configured in run.py)
   ```python
   app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
   ```

2. **Environment Variables**
   - Pass GROQ_API_KEY as container environment variable
   - Optionally mount .env file or pass all vars
   
3. **FFmpeg Installation**
   Add to Dockerfile:
   ```dockerfile
   RUN apt-get update && apt-get install -y ffmpeg
   ```

4. **Volume Mounts**
   - Optional: Mount cookies file if using YTDLP_COOKIES_FILE
   - Note: audios/ directory is temporary (2-hour retention)

### Sample Dockerfile

```dockerfile
FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Environment defaults (override at runtime)
ENV PORT=3001
ENV FLASK_ENV=production
ENV FLASK_DEBUG=0

# Expose port
EXPOSE 3001

# Run application
CMD ["python", "run.py"]
```

## Verification Tests

### 1. Health Check

```bash
curl http://localhost:3001/
# Expected: {"message":"Healthy"}
```

### 2. Hello Endpoint

```bash
curl http://localhost:3001/hello
# Expected: {"message":"Hello, World!"}
```

### 3. API Documentation

```bash
curl -I http://localhost:3001/docs
# Expected: HTTP/1.1 200 OK
```

### 4. Error Handling Test

```bash
# Test missing URL (expect 400)
curl -X POST http://localhost:3001/summarize \
  -H "Content-Type: application/json" \
  -d '{}' \
  -w "\nHTTP_CODE:%{http_code}\n"
# Expected: HTTP_CODE:400

# Test missing cookies (expect 400)
curl -X POST http://localhost:3001/summarize \
  -H "Content-Type: application/json" \
  -d '{"url":"https://youtube.com/watch?v=test"}' \
  -w "\nHTTP_CODE:%{http_code}\n"
# Expected: HTTP_CODE:400

# Test invalid base64 (expect 400)
curl -X POST http://localhost:3001/summarize \
  -H "Content-Type: application/json" \
  -d '{"url":"https://youtube.com/watch?v=test","cookies_b64":"invalid!!!"}' \
  -w "\nHTTP_CODE:%{http_code}\n"
# Expected: HTTP_CODE:400
```

### 5. FFmpeg Availability

If FFmpeg is missing, endpoints should return 503:

```bash
# Temporarily rename ffmpeg to test
sudo mv /usr/bin/ffmpeg /usr/bin/ffmpeg.bak

curl -X POST http://localhost:3001/download \
  -H "Content-Type: application/json" \
  -d '{"url":"https://youtube.com/watch?v=test"}' \
  -w "\nHTTP_CODE:%{http_code}\n"
# Expected: HTTP_CODE:503 with clear error message

# Restore ffmpeg
sudo mv /usr/bin/ffmpeg.bak /usr/bin/ffmpeg
```

## Troubleshooting

### Issue: Container exits with code 143

**Cause:** Flask development server auto-reloader causes SIGTERM

**Solution:** Ensure run.py has:
```python
app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
```

Also verify .env has:
```env
FLASK_ENV=production
FLASK_DEBUG=0
```

### Issue: HTTP 500 on /summarize

**Possible Causes:**

1. **Missing GROQ_API_KEY**
   - Check: `echo $GROQ_API_KEY`
   - Fix: Add to .env file
   - Expected response: 503 with clear message

2. **Missing FFmpeg**
   - Check: `which ffmpeg`
   - Fix: Install FFmpeg
   - Expected response: 503 with clear message

3. **Invalid YouTube URL**
   - Ensure URL is accessible and < 5 minutes
   - Expected response: 400 with error message

### Issue: HTTP 400 on /summarize

**Expected Behavior** - These are client errors:

- Missing `url` field → 400
- Missing `cookies_b64` field → 400
- Invalid base64 in `cookies_b64` → 400
- Cookies not in Netscape format → 400
- Video longer than 5 minutes → 400

### Issue: Port Already in Use

```bash
# Find process using port 3001
lsof -i :3001
# or
netstat -an | grep 3001

# Kill the process
kill -9 <PID>

# Or change port in .env
echo "PORT=3002" >> .env
```

### Issue: Rate Limit Exceeded (429)

**Cause:** Rate limiter is protecting the service

**Current Limits:**
- /search: 20 requests/minute
- /download: 6 requests/minute
- /summarize: 3 requests/minute
- /audios/*: 60 requests/minute

**Solution:** Wait for the rate limit window to reset (1 minute)

## Monitoring

### Key Metrics to Monitor

1. **Process Status**
   ```bash
   ps aux | grep python | grep run.py
   ```

2. **Port Binding**
   ```bash
   netstat -tuln | grep 3001
   ```

3. **Disk Usage** (audios directory)
   ```bash
   du -sh audios/
   ls -lh audios/
   ```

4. **FFmpeg Availability**
   ```bash
   which ffmpeg && echo "OK" || echo "MISSING"
   ```

### Logs

Flask logs are written to stdout/stderr. In production:

```bash
# Redirect to file
python run.py > app.log 2>&1

# Or use systemd/supervisord for process management
```

## Production Recommendations

1. **Process Manager**: Use systemd, supervisord, or similar
2. **Reverse Proxy**: Use nginx or Apache in front of Flask
3. **SSL/TLS**: Terminate SSL at reverse proxy
4. **Resource Limits**: Set memory/CPU limits for the container
5. **Health Checks**: Configure periodic health checks on `/`
6. **Monitoring**: Set up alerting for 5xx errors
7. **Backup**: GROQ_API_KEY should be stored securely (secrets manager)
8. **Rate Limiting**: Adjust based on expected traffic
9. **File Cleanup**: Ensure background cleanup thread is running (automatic)
10. **Updates**: Keep dependencies updated (security patches)

## Performance Tuning

### For High Traffic

Consider using a production WSGI server:

```bash
# Install gunicorn
pip install gunicorn

# Run with gunicorn
gunicorn -w 4 -b 0.0.0.0:3001 app:app
```

Configuration:
- `-w 4`: 4 worker processes (adjust based on CPU cores)
- `-b 0.0.0.0:3001`: Bind to all interfaces on port 3001
- `--timeout 120`: Increase timeout for long transcription operations

### Memory Considerations

- Each worker loads the full app into memory
- Audio files are temporary (auto-deleted after 2 hours)
- Groq API calls are I/O bound, not CPU intensive

## Security Checklist

- [ ] GROQ_API_KEY is not committed to version control
- [ ] CORS origins are restricted in production
- [ ] Rate limiting is enabled
- [ ] Debug mode is disabled (FLASK_DEBUG=0)
- [ ] Cookie files have restricted permissions (0600)
- [ ] Temporary cookie files are cleaned up after use
- [ ] SSL/TLS is configured at reverse proxy
- [ ] File uploads are restricted (only audio processing)
- [ ] Error messages don't expose sensitive information in production
