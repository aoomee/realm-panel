"""
Control the realm service inside the panel Docker stack.

Two strategies, chosen via env REALM_BACKEND:
  - "docker"   : use the Docker SDK to restart the realm container (default
                 when running panel as a container with the Docker socket mounted)
  - "systemd" : shell out to `systemctl restart realm` (for host install mode)
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
    """Hard restart the realm process/container."""
    try:
        if BACKEND == "docker":
            log.info("restarting realm container %s", REALM_CONTAINER)
            try:
                _docker().containers.get(REALM_CONTAINER).restart(timeout=5)
            except Exception:
                # container may not exist yet
                log.warning("realm container not found, skip restart")
        else:
            subprocess.run(["systemctl", "restart", "realm"], check=False)
    except Exception as e:
        log.error("restart failed: %s", e)


def reload_realm():
    """Soft reload: send SIGHUP to realm so it re-reads config.json."""
    try:
        if BACKEND == "docker":
            try:
                c = _docker().containers.get(REALM_CONTAINER)
                c.exec_run("kill -HUP 1")
            except Exception as e:
                log.debug("HUP via docker failed: %s", e)
                restart_realm()
        else:
            subprocess.run(["systemctl", "kill", "-HUP", "realm"], check=False)
    except Exception as e:
        log.error("reload failed: %s", e)
