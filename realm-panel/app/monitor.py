"""
Host monitoring + per-rule link self-test.

probe_all_hosts()
  TCP-connects to every unique REMOTE_HOST:REMOTE_PORT referenced by
  enabled rules.  Keeps a sliding ring of the last RING_SIZE probes per
  host, computes uptime % and average latency from the ring.

selftest_rule(rule_id)
  4-step diagnostic for a single rule:
    1. realm process/container is running
    2. /etc/realm/config.json contains the rule's listen endpoint
    3. TCP probe to REMOTE_HOST:REMOTE_PORT succeeds (up to 3 targets)
    4. Local listen port is in LISTEN state (/proc/net/tcp6 or tcp)

State is persisted to MONITOR_PATH so the UI can read it without
triggering a new probe on every page load.
"""
import json
import os
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

MONITOR_PATH  = os.environ.get("MONITOR_PATH",   "/etc/realm/panel/monitor.json")
PROBE_TIMEOUT = float(os.environ.get("MONITOR_TIMEOUT", "3"))
RING_SIZE     = int(os.environ.get("MONITOR_RING",    "20"))

_lock = Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load() -> dict:
    try:
        if os.path.exists(MONITOR_PATH):
            return json.loads(Path(MONITOR_PATH).read_text())
    except Exception:
        pass
    return {"hosts": {}, "rules": {}}


def _save(state: dict):
    os.makedirs(os.path.dirname(MONITOR_PATH), exist_ok=True)
    Path(MONITOR_PATH).write_text(json.dumps(state, indent=2, ensure_ascii=False))


def _tcp(host: str, port, timeout: float = PROBE_TIMEOUT) -> tuple[bool, float | None]:
    """Return (reachable, latency_ms)."""
    try:
        t0 = time.monotonic()
        with socket.create_connection((host, int(port)), timeout=timeout):
            pass
        return True, round((time.monotonic() - t0) * 1000, 1)
    except Exception:
        return False, None


def _port_listening(port) -> bool:
    """Return True if the port is in LISTEN state on this host."""
    hex_port = format(int(port), "04X")
    for f in ("/proc/net/tcp6", "/proc/net/tcp"):
        try:
            for line in Path(f).read_text().splitlines()[1:]:
                cols = line.split()
                # col[3] == "0A" means LISTEN; col[1] is local_addr:port
                if len(cols) > 3 and cols[3] == "0A" and cols[1].endswith(":" + hex_port):
                    return True
        except Exception:
            pass
    return False


# ---------------------------------------------------------------------------
# Host probing
# ---------------------------------------------------------------------------

def probe_all_hosts() -> dict:
    """Probe all unique backend hosts referenced by enabled rules."""
    from .config_io import list_rules

    targets: set[tuple[str, str]] = set()
    for r in list_rules():
        if r.get("ENABLED", "true") != "true":
            continue
        rh = r.get("REMOTE_HOST", "")
        rp = r.get("REMOTE_PORT", "")
        if not rh or not rp:
            continue
        for h in (x.strip() for x in rh.split(",") if x.strip()):
            targets.add((h, rp))

    with _lock:
        state = _load()
        hosts = state.get("hosts", {})

        for (host, port) in targets:
            key   = f"{host}:{port}"
            entry = hosts.get(key, {"latency_ring": [], "up_count": 0, "total_count": 0})

            ok, ms = _tcp(host, port)

            ring = entry.get("latency_ring", [])
            ring.append({"ts": _now(), "ok": ok, "ms": ms})
            ring = ring[-RING_SIZE:]

            ok_ms = [p["ms"] for p in ring if p["ok"] and p["ms"] is not None]

            entry.update({
                "latency_ring":  ring,
                "total_count":   entry.get("total_count", 0) + 1,
                "up_count":      entry.get("up_count",    0) + (1 if ok else 0),
                "last_ok":       ok,
                "last_ms":       ms,
                "last_checked":  _now(),
                "uptime_pct":    round(sum(1 for p in ring if p["ok"]) / len(ring) * 100, 1),
                "avg_ms":        round(sum(ok_ms) / len(ok_ms), 1) if ok_ms else None,
            })
            hosts[key] = entry

        state["hosts"] = hosts
        _save(state)

    return state["hosts"]


# ---------------------------------------------------------------------------
# Per-rule self-test
# ---------------------------------------------------------------------------

def selftest_rule(rule_id: str) -> dict:
    from .config_io import get_rule, CONFIG_JSON
    from .realm_ctrl import BACKEND, REALM_CONTAINER

    rule = get_rule(rule_id)
    if rule is None:
        return {"error": "rule not found"}

    steps: list[dict] = []

    # 1 — realm running?
    try:
        if BACKEND == "docker":
            import docker
            c = docker.from_env().containers.get(REALM_CONTAINER)
            realm_ok = c.status == "running"
            detail   = c.status
        else:
            out = subprocess.run(
                ["systemctl", "is-active", "realm"], capture_output=True, text=True
            )
            realm_ok = out.stdout.strip() == "active"
            detail   = out.stdout.strip()
    except Exception as e:
        realm_ok = False
        detail   = str(e)
    steps.append({"name": "realm 进程", "ok": realm_ok, "detail": detail})

    # 2 — config.json contains this rule's listen endpoint?
    listen_port = rule.get("LISTEN_PORT", "")
    try:
        cfg       = json.loads(Path(CONFIG_JSON).read_text())
        endpoints = cfg.get("endpoints", [])
        cfg_ok    = any(str(listen_port) in ep.get("listen", "") for ep in endpoints)
        detail    = f"端口 {listen_port} {'已找到' if cfg_ok else '未找到'}"
    except Exception as e:
        cfg_ok = False
        detail = str(e)
    steps.append({"name": "配置含该端点", "ok": cfg_ok, "detail": detail})

    # 3 — remote TCP reachable?
    rh = rule.get("REMOTE_HOST", "")
    rp = rule.get("REMOTE_PORT", "")
    if rh and rp:
        probe_results = []
        for h in [x.strip() for x in rh.split(",") if x.strip()][:3]:
            ok, ms = _tcp(h, rp)
            probe_results.append({"host": h, "ok": ok, "ms": ms})
        remote_ok = any(p["ok"] for p in probe_results)
        detail    = "  ".join(
            f"{p['host']} {'✓' if p['ok'] else '✗'}{(' '+str(p['ms'])+'ms') if p['ms'] else ''}"
            for p in probe_results
        )
    else:
        remote_ok     = False
        probe_results = []
        detail        = "无远端配置"
    steps.append({"name": "远端可达", "ok": remote_ok, "detail": detail})

    # 4 — local port bound?
    try:
        port_ok = _port_listening(listen_port)
    except Exception:
        port_ok = False
    steps.append({"name": "本地端口已绑定", "ok": port_ok, "detail": f":{listen_port}"})

    result = {
        "rule_id":    rule_id,
        "rule_name":  rule.get("RULE_NAME", ""),
        "listen_port": listen_port,
        "overall":    all(s["ok"] for s in steps),
        "steps":      steps,
        "tested_at":  _now(),
    }

    with _lock:
        state = _load()
        state.setdefault("rules", {})[rule_id] = result
        _save(state)

    return result


def selftest_all() -> dict:
    from .config_io import list_rules
    results: dict[str, dict] = {}
    for r in list_rules():
        if r.get("ENABLED", "true") != "true":
            continue
        rid = r.get("RULE_ID", "")
        if rid:
            results[rid] = selftest_rule(rid)
    return results


def get_state() -> dict:
    with _lock:
        return _load()
