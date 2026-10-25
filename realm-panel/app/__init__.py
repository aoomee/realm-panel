from flask import Flask, render_template
from flask_cors import CORS
from .scheduler import start_scheduler


def create_app():
    app = Flask(__name__)
    CORS(app)

    from .api import bp
    app.register_blueprint(bp)

    @app.get("/")
    def index():
        return render_template("index.html")

    start_scheduler(app)
    return app
