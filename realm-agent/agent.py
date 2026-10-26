#!/usr/bin/env python3
"""
Realm Relay Agent — lightweight HTTP daemon for remote realm management.

Deploy one instance on each relay/exit machine. The panel pushes
config.json over HTTP with a Bearer token; the agent writes the file
and restarts the realm systemd service (realm has no hot-reload).

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
import socket
import threading
import time
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
AGENT_VERSION = "2"

if not TOKEN:
    log.error("AGENT_TOKEN is not set — refusing to start (would accept any request)")
    sys.exit(1)


# ---------------------------------------------------------------------------
# realm process helpers
# ---------------------------------------------------------------------------

def _realm_pid() -> int | None:
    """Return the realm process PID by scanning /proc, or None."""
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                argv = (entry / "cmdline").read_bytes().split(b"\x00")
                exe = os.path.basename(argv[0].decode(errors="replace")) if argv else ""
                if exe == "realm":
                    return int(entry.name)
            except (PermissionError, FileNotFoundError, ProcessLookupError):
                pass
    except Exception:
        pass
    return None


def _systemctl(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=15)


def _reload_realm() -> dict:
    """
    Apply the current config.json. realm has no hot-reload (SIGHUP would
    terminate it), so restart the service. With zero endpoints realm
    refuses to run, so stop it instead.
    Returns {"ok": bool, "method": str, "detail": str}.
    """
    try:
        cfg = json.loads(Path(REALM_CONFIG).read_text())
        empty = not cfg.get("endpoints")
    except Exception:
        empty = False
    if empty:
        r = _systemctl("stop", REALM_SVC)
        log.info("no endpoints -> stopped %s", REALM_SVC)
        return {"ok": r.returncode == 0, "method": "stop", "detail": "no endpoints"}

    _systemctl("enable", REALM_SVC)
    r = _systemctl("restart", REALM_SVC)
    if r.returncode != 0:
        log.warning("systemctl restart failed: %s", r.stderr.strip())
        return {"ok": False, "method": "systemctl", "detail": r.stderr.strip() or "non-zero exit"}
    # realm exits immediately on a bad config / busy port — catch that
    time.sleep(0.8)
    if _systemctl("is-active", REALM_SVC).stdout.strip() != "active":
        logs = subprocess.run(["journalctl", "-u", REALM_SVC, "-n", "5", "--no-pager"],
                              capture_output=True, text=True, timeout=10).stdout
        return {"ok": False, "method": "systemctl", "detail": "realm 启动失败: " + logs[-400:]}
    log.info("restarted %s", REALM_SVC)
    return {"ok": True, "method": "systemctl", "detail": f"restart {REALM_SVC}"}


def _tcping(host: str, port: int, timeout: float = 3.0) -> dict:
    t0 = time.monotonic()
    try:
        with socket.create_connection((host.strip("[]"), int(port)), timeout=timeout):
            return {"ok": True, "ms": round((time.monotonic() - t0) * 1000, 1)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _realm_status() -> dict:
    pid = _realm_pid()
    if pid:
        return {"running": True, "pid": pid}
    # try systemctl is-active
    try:
        state = _systemctl("is-active", REALM_SVC).stdout.strip()
    except Exception as e:
        state = f"unknown ({e})"
    return {"running": state == "active", "pid": None, "systemctl": state}


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
                "agent_version": AGENT_VERSION,
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

            try:
                reload_result = _reload_realm()
            except Exception as e:
                reload_result = {"ok": False, "method": "error", "detail": str(e)}
            self._respond(200, {"ok": reload_result["ok"], "reload": reload_result,
                                "endpoints": len(cfg.get("endpoints") or [])})
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            return self._deny()
        if self.path in ("/reload", "/reload/"):
            result = _reload_realm()
            code = 200 if result["ok"] else 500
            self._respond(code, {"ok": result["ok"], "reload": result})
        elif self.path in ("/tcping", "/tcping/"):
            body = self._read_json() or {}
            host, port = str(body.get("host", "")), body.get("port")
            if not host or not str(port).isdigit():
                return self._respond(400, {"ok": False, "error": "host and port required"})
            self._respond(200, _tcping(host, int(port)))
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
