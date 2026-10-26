import hmac
import os
from pathlib import Path

from flask import Flask, Response, abort, render_template, request, send_from_directory

from .scheduler import start_scheduler

# Files served to remote nodes by the install command. In the image they
# live in /panel/agent_dist (see Dockerfile); in a source checkout fall
# back to ../realm-agent.
_HERE = Path(__file__).resolve().parent
AGENT_DIST = os.environ.get("AGENT_DIST") or (
    "/panel/agent_dist" if Path("/panel/agent_dist").is_dir() else str(_HERE.parent.parent / "realm-agent")
)
PANEL_USER = os.environ.get("PANEL_USER", "admin")
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "")


def _check_auth() -> bool:
    auth = request.authorization
    if not auth or auth.type != "basic":
        return False
    return hmac.compare_digest(auth.username or "", PANEL_USER) and \
        hmac.compare_digest(auth.password or "", PANEL_PASSWORD)


def create_app():
    app = Flask(__name__)

    @app.before_request
    def _require_login():
        # /agent/* must stay public: remote nodes download the installer from it
        if not PANEL_PASSWORD or request.path.startswith("/agent/"):
            return None
        if _check_auth():
            return None
        return Response("需要登录", 401, {"WWW-Authenticate": 'Basic realm="realm-panel"'})

    @app.before_request
    def _same_origin_writes():
        # CSRF guard: a foreign website must not be able to drive the API
        # through the admin's browser (force=True JSON parsing accepts text/plain).
        if request.method in ("GET", "HEAD", "OPTIONS") or not request.path.startswith("/api/"):
            return None
        origin = request.headers.get("Origin") or request.headers.get("Referer") or ""
        if origin:
            from urllib.parse import urlparse
            allowed = {request.host, request.headers.get("X-Forwarded-Host", "")}
            if urlparse(origin).netloc not in allowed:
                return Response("cross-origin request blocked", 403)
        return None

    from .api import bp
    app.register_blueprint(bp)

    @app.get("/")
    def index():
        return render_template("index.html", auth_enabled=bool(PANEL_PASSWORD))

    # ---------------- public files for remote node installation ----------------
    @app.get("/agent/install.sh")
    def agent_install():
        return send_from_directory(AGENT_DIST, "install.sh", mimetype="text/x-shellscript")

    @app.get("/agent/agent.py")
    def agent_py():
        return send_from_directory(AGENT_DIST, "agent.py", mimetype="text/x-python")

    @app.get("/agent/realm/<name>")
    def agent_realm(name: str):
        if not (name.startswith("realm-") and name.endswith(".tar.gz")) or "/" in name:
            abort(404)
        return send_from_directory(os.path.join(AGENT_DIST, "realm"), name,
                                   mimetype="application/gzip")

    start_scheduler(app)
    return app
