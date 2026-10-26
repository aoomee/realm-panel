"""
Control the local realm (machine A) inside the panel Docker stack.

realm has no hot-reload: SIGHUP terminates it, and inside the container
realm is PID 1, which ignores signals it has no handler for. Applying a new
config therefore means restarting realm. Callers should only do that when
the config actually changed (see config_io.regenerate_config).

Backends, chosen via env REALM_BACKEND:
  - "docker"  : restart the realm container through the Docker socket
  - "systemd" : `systemctl restart realm` (host install mode)
"""
import logging
import os
import subprocess

log = logging.getLogger(__name__)
BACKEND = os.environ.get("REALM_BACKEND", "docker")
REALM_CONTAINER = os.environ.get("REALM_CONTAINER", "realm")

_docker_client = None


def _docker():
    global _docker_client
    if _docker_client is None:
        import docker
        _docker_client = docker.from_env()
    return _docker_client


def restart_realm():
    """Restart (or start) the local realm process/container."""
    try:
        if BACKEND == "docker":
            log.info("restarting realm container %s", REALM_CONTAINER)
            try:
                _docker().containers.get(REALM_CONTAINER).restart(timeout=5)
            except Exception as e:
                log.warning("realm container restart failed: %s", e)
        else:
            subprocess.run(["systemctl", "restart", "realm"], check=False)
    except Exception as e:
        log.error("restart failed: %s", e)


def stop_realm():
    """Stop local realm (used when it has no endpoints — realm won't run empty)."""
    try:
        if BACKEND == "docker":
            c = _docker().containers.get(REALM_CONTAINER)
            if c.status == "running":
                c.stop(timeout=5)
        else:
            subprocess.run(["systemctl", "stop", "realm"], check=False)
    except Exception as e:
        log.warning("stop realm failed: %s", e)


def apply_config(config: dict):
    """Make local realm run `config` (already written to disk)."""
    if config.get("endpoints"):
        restart_realm()
    else:
        log.info("local config has no endpoints, stopping realm")
        stop_realm()


def reload_realm():
    """Kept for compatibility: re-apply whatever config.json holds now."""
    import json
    from .config_io import CONFIG_JSON
    try:
        with open(CONFIG_JSON) as f:
            apply_config(json.load(f))
    except Exception as e:
        log.error("reload failed: %s", e)
        restart_realm()
