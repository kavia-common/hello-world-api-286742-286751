import os
import threading
from pathlib import Path
from flask import Flask
from flask_cors import CORS
from flask_smorest import Api
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv

# Load environment variables from .env file before any other initialization
# Look for .env in the flask_backend directory (parent of app/)
dotenv_path = Path(__file__).resolve().parent.parent / '.env'
load_dotenv(dotenv_path=dotenv_path)

# Initialize Flask app
app = Flask(__name__)
app.url_map.strict_slashes = False

# Enable CORS for all origins and routes
CORS(app, resources={r"/*": {"origins": "*"}})

# Rate Limiting (in-memory storage)
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri="memory://",
    default_limits=["30 per minute"],  # sensible defaults
    headers_enabled=True,
)
limiter.init_app(app)

# OpenAPI/Swagger configuration
app.config["API_TITLE"] = "My Flask API"
app.config["API_VERSION"] = "v1"
app.config["OPENAPI_VERSION"] = "3.0.3"
app.config["OPENAPI_SWAGGER_UI_URL"] = "https://cdn.jsdelivr.net/npm/swagger-ui-dist/"
app.config["OPENAPI_SWAGGER_UI_PATH"] = ""
app.config["OPENAPI_URL_PREFIX"] = "/docs"

# Ensure 'audios' directory exists at container root
BASE_DIR = os.path.dirname(os.path.dirname(__file__))  # .../flask_backend/app
AUDIOS_DIR = os.path.join(os.path.dirname(BASE_DIR), "audios")
os.makedirs(AUDIOS_DIR, exist_ok=True)

# Initialize API and register blueprints
api = Api(app)

# Import and register blueprints (import after limiter is created to avoid circulars)
from .routes.health import blp as health_blp  # noqa: E402
from .routes.hello import blp as hello_blp  # noqa: E402
from .routes.youtube import blp as yt_blp, delete_files_task, keep_alive  # noqa: E402

api.register_blueprint(health_blp)
api.register_blueprint(hello_blp)
api.register_blueprint(yt_blp)

# Start background housekeeping threads
try:
    threading.Thread(target=delete_files_task, daemon=True).start()
    threading.Thread(target=keep_alive, daemon=True).start()
except Exception as e:
    print(f"[app.__init__] Failed to start background tasks: {e}")
