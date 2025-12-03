from flask import Flask
from flask_cors import CORS
from flask_smorest import Api

# Import blueprints
from .routes.health import blp as health_blp
from .routes.hello import blp as hello_blp


app = Flask(__name__)
app.url_map.strict_slashes = False

# Enable CORS for all origins and routes
CORS(app, resources={r"/*": {"origins": "*"}})

# OpenAPI/Swagger configuration
app.config["API_TITLE"] = "My Flask API"
app.config["API_VERSION"] = "v1"
app.config["OPENAPI_VERSION"] = "3.0.3"
app.config['OPENAPI_URL_PREFIX'] = '/docs'
app.config["OPENAPI_SWAGGER_UI_PATH"] = ""
app.config["OPENAPI_SWAGGER_UI_URL"] = "https://cdn.jsdelivr.net/npm/swagger-ui-dist/"

# Initialize API and register blueprints
api = Api(app)
api.register_blueprint(health_blp)
api.register_blueprint(hello_blp)
