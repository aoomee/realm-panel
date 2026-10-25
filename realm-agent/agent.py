#!/usr/bin/env python3
"""
Realm Relay Agent — lightweight HTTP daemon for remote realm management.

Deploy one instance on each relay/exit machine. The panel pushes
config.json over HTTP with a Bearer token; the agent writes the file
and sends SIGHUP to realm so it hot-reloads without dropping connections.

Environment variables:
  AGENT_TOKEN       Required. Shared secret; panel must send matching token.
  AGENT_PORT        Listening port (default 5001).
  REALM_CONFIG      Path to realm config.json (default /etc/realm/config.json).
  REALM_SERVICE     systemd service name for fallback restart (default realm).

Usage:
  AGENT_TOKEN=my-secret python3 agent.py
"""
import http.server
import json
import logging
import os
import signal
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [agent] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

TOKEN        = os.environ.get("AGENT_TOKEN", "")
PORT         = int(os.environ.get("AGENT_PORT", "5001"))
REALM_CONFIG = os.environ.get("REALM_CONFIG", "/etc/realm/config.json")
REALM_SVC    = os.environ.get("REALM_SERVICE", "realm")

if not TOKEN:
    log.error("AGENT_TOKEN is not set — refusing to start (would accept any request)")
    sys.exit(1)


# ---------------------------------------------------------------------------
# realm process helpers
# ---------------------------------------------------------------------------

def _realm_pid() -> int | None:
    """Return realm's PID by scanning /proc, or None if not found."""
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode()
                if "realm" in cmdline and "-c" in cmdline:
                    return int(entry.name)
            except (PermissionError, FileNotFoundError):
                pass
    except Exception:
        pass
    return None


def _reload_realm() -> dict:
    """
    Try SIGHUP first; fall back to systemctl restart.
    Returns {"ok": bool, "method": str, "detail": str}.
    """
    pid = _realm_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGHUP)
            log.info("sent SIGHUP to realm pid %d", pid)
            return {"ok": True, "method": "sighup", "detail": f"pid {pid}"}
        except ProcessLookupError:
            pass

    # fallback: systemctl restart (requires agent running as root or with sudo)
    result = subprocess.run(
        ["systemctl", "restart", REALM_SVC],
        capture_output=True, text=True, timeout=10
    )
    if result.returncode == 0:
        log.info("systemctl restart %s OK", REALM_SVC)
        return {"ok": True, "method": "systemctl", "detail": f"restart {REALM_SVC}"}
    log.warning("systemctl restart failed: %s", result.stderr.strip())
    return {"ok": False, "method": "systemctl", "detail": result.stderr.strip() or "non-zero exit"}


def _realm_status() -> dict:
    pid = _realm_pid()
    if pid:
        return {"running": True, "pid": pid}
    # try systemctl is-active
    result = subprocess.run(
        ["systemctl", "is-active", REALM_SVC],
        capture_output=True, text=True, timeout=5
    )
    active = result.stdout.strip() == "active"
    return {"running": active, "pid": None, "systemctl": result.stdout.strip()}


# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------

class AgentHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # redirect to logging
        log.info("HTTP %s - %s", self.address_string(), fmt % args)

    # ---- auth ----
    def _authorized(self) -> bool:
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {TOKEN}"

    def _deny(self):
        self._respond(401, {"error": "unauthorized"})

    # ---- response helpers ----
    def _respond(self, code: int, body: dict):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict | None:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return None

    # ---- routing ----
    def do_GET(self):
        if not self._authorized():
            return self._deny()
        if self.path in ("/health", "/health/"):
            status = _realm_status()
            self._respond(200, {
                "ok": True,
                "realm": status,
                "agent_time": datetime.now(timezone.utc).isoformat(),
            })
        elif self.path in ("/status", "/status/"):
            status = _realm_status()
            try:
                cfg_exists = Path(REALM_CONFIG).exists()
                cfg_mtime  = datetime.fromtimestamp(
                    Path(REALM_CONFIG).stat().st_mtime, tz=timezone.utc
                ).isoformat() if cfg_exists else None
            except Exception:
                cfg_exists, cfg_mtime = False, None
            self._respond(200, {
                "ok": True,
                "realm": status,
                "config_exists": cfg_exists,
                "config_mtime": cfg_mtime,
                "agent_time": datetime.now(timezone.utc).isoformat(),
            })
        else:
            self._respond(404, {"error": "not found"})

    def do_PUT(self):
        if not self._authorized():
            return self._deny()
        if self.path in ("/config", "/config/"):
            body = self._read_json()
            if body is None:
                return self._respond(400, {"error": "invalid JSON"})
            # accept {"config": {...}} or the raw config object directly
            cfg = body.get("config", body)
            if not isinstance(cfg, dict):
                return self._respond(400, {"error": "config must be a JSON object"})
            try:
                Path(REALM_CONFIG).parent.mkdir(parents=True, exist_ok=True)
                Path(REALM_CONFIG).write_text(
                    json.dumps(cfg, indent=2, ensure_ascii=False)
                )
                log.info("wrote %s (%d bytes)", REALM_CONFIG, Path(REALM_CONFIG).stat().st_size)
            except Exception as e:
                log.error("write config failed: %s", e)
                return self._respond(500, {"error": str(e)})

            reload_result = _reload_realm()
            self._respond(200, {"ok": True, "reload": reload_result})
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            return self._deny()
        if self.path in ("/reload", "/reload/"):
            result = _reload_realm()
            code = 200 if result["ok"] else 500
            self._respond(code, {"ok": result["ok"], "reload": result})
        else:
            self._respond(404, {"error": "not found"})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), AgentHandler)
    log.info("realm-agent listening on :%d  config=%s", PORT, REALM_CONFIG)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
        server.server_close()


if __name__ == "__main__":
    main()
