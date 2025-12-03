from flask_smorest import Blueprint
from flask.views import MethodView

# PUBLIC_INTERFACE
# Health check blueprint and route.
blp = Blueprint("Health", "health", url_prefix="/", description="Health check route")


@blp.route("/")
class HealthCheck(MethodView):
    # PUBLIC_INTERFACE
    def get(self):
        """Simple liveness probe endpoint."""
        return {"message": "Healthy"}
