from flask_smorest import Blueprint
from flask.views import MethodView

# Define a blueprint for the hello routes
blp = Blueprint(
    "Hello",
    "hello",
    url_prefix="/hello",
    description="Hello World endpoint"
)


@blp.route("/")
class HelloWorld(MethodView):
    # PUBLIC_INTERFACE
    def get(self):
        """Return a simple Hello, World! JSON response.
        ---
        responses:
          200:
            description: Successful hello world response
            content:
              application/json:
                schema:
                  type: object
                  properties:
                    message:
                      type: string
                      description: Greeting message
                      example: Hello, World!
        """
        return {"message": "Hello, World!"}
