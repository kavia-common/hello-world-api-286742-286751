import os
from app import app

if __name__ == "__main__":
    """
    Entrypoint for running the Flask app.

    Notes:
    - Host set to 0.0.0.0 for containerized environments.
    - Port read from PORT env var if present; defaults to 3001.
    - Debug mode explicitly disabled for production stability.
    - Auto-reloader disabled to prevent exit 143 and container restarts.
    """
    port = int(os.getenv("PORT", "3001"))
    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )
