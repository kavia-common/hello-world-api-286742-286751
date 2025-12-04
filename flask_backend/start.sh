#!/bin/bash
# Production startup script for Flask backend
# Uses run.py with debug=False and use_reloader=False for stability

set -e

# Load environment variables from .env if present
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
fi

# Verify FFmpeg is available
if ! command -v ffmpeg &> /dev/null; then
    echo "WARNING: FFmpeg is not installed or not on PATH."
    echo "Audio processing features will return HTTP 503."
    echo "Install with: sudo apt-get install -y ffmpeg"
fi

# Verify GROQ_API_KEY is set
if [ -z "$GROQ_API_KEY" ]; then
    echo "ERROR: GROQ_API_KEY environment variable is not set."
    echo "Transcription and summarization features will not work."
    exit 1
fi

# Set production environment
export FLASK_ENV=production
export FLASK_DEBUG=0

# Start the server using run.py
echo "Starting Flask backend on port ${PORT:-3001}..."
echo "Debug mode: disabled"
echo "Auto-reloader: disabled"
echo "FFmpeg available: $(command -v ffmpeg &> /dev/null && echo 'yes' || echo 'no')"
echo "GROQ_API_KEY set: $([ -n "$GROQ_API_KEY" ] && echo 'yes' || echo 'no')"

python run.py
