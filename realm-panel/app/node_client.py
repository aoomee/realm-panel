"""
HTTP client for pushing config to remote realm-agent instances.

Used by config_io / scheduler to distribute the regenerated config.json
to every enabled node. Each call is short (default 8s) so a slow or
unreachable node does not block the weight-rebalance job.
"""
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

from . import node_io

log = logging.getLogger(__name__)
TIMEOUT = float(os.environ.get("NODE_PUSH_TIMEOUT", "8"))


def _post_json(url: str, token: str, method: str, body: dict | None = None) -> dict:
    """Issue an authenticated JSON request; return parsed JSON or raise."""
    data = json.dumps(body or {}, ensure_ascii=False).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Content-Length": str(len(data)),
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read()
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {"_raw": raw.decode(errors="replace"), "_status": resp.status}


def push_config_to_node(node: dict, config: dict) -> dict:
    """
    Send the full realm config dict to one node's agent.
    Returns the agent's response dict (or {"ok": False, "error": ...}).
    """
    url = node.get("url", "").rstrip("/")
    token = node.get("token", "")
    if not url or not token:
        return {"ok": False, "error": "missing url or token"}
    try:
        return _post_json(f"{url}/config", token, "PUT", {"config": config})
    except urllib.error.HTTPError as e:
        log.warning("push to %s failed: HTTP %s", url, e.code)
        try:
            err_body = e.read().decode(errors="replace")
        except Exception:
            err_body = ""
        return {"ok": False, "error": f"HTTP {e.code}", "detail": err_body[:200]}
    except urllib.error.URLError as e:
        log.warning("push to %s failed: %s", url, e.reason)
        return {"ok": False, "error": str(e.reason)}
    except Exception as e:
        log.warning("push to %s failed: %s", url, e)
        return {"ok": False, "error": str(e)}


def reload_node(node: dict) -> dict:
    """Trigger a standalone SIGHUP/reload on one node."""
    url = node.get("url", "").rstrip("/")
    token = node.get("token", "")
    if not url or not token:
        return {"ok": False, "error": "missing url or token"}
    try:
        return _post_json(f"{url}/reload", token, "POST")
    except Exception as e:
        return {"ok": False, "error": str(e)}


def health_node(node: dict) -> dict:
    """Probe one node's agent /health (used by the node status poll)."""
    url = node.get("url", "").rstrip("/")
    token = node.get("token", "")
    if not url or not token:
        return {"ok": False, "error": "missing url or token"}
    try:
        return _post_json(f"{url}/health", token, "GET")
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}"}
    except urllib.error.URLError as e:
        return {"ok": False, "error": str(e.reason)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def push_node(node_id: str) -> dict:
    """Build the node's own config (only rules with NODE_ID == node_id) and push it."""
    from . import config_io
    node = node_io.get_node(node_id)
    if node is None:
        return {"ok": False, "error": "node not found"}
    cfg = config_io.build_config(node_id)
    result = push_config_to_node(node, cfg)
    result.setdefault("endpoints", len(cfg["endpoints"]))
    node_io.set_push_result(node_id, bool(result.get("ok")), str(result)[:300])
    return result


def push_config_to_all() -> dict[str, dict]:
    """
    Push each enabled node its own filtered config. Pushes run in parallel
    so 12 nodes × 8s timeout cannot stall the caller for 96s.
    Returns {node_id: result}.
    """
    from concurrent.futures import ThreadPoolExecutor
    nodes = node_io.list_nodes()
    out: dict[str, dict] = {}
    targets = []
    for node_id, node in nodes.items():
        if node.get("enabled", "true") != "true":
            out[node_id] = {"ok": False, "skipped": True, "error": "disabled"}
        else:
            targets.append(node_id)
    if targets:
        with ThreadPoolExecutor(max_workers=min(16, len(targets))) as ex:
            for node_id, res in zip(targets, ex.map(push_node, targets)):
                out[node_id] = res
    return out


def health_all() -> dict[str, dict]:
    """Probe every enabled node's /health endpoint in sequence."""
    nodes = node_io.list_nodes()
    out: dict[str, dict] = {}
    for node_id, node in nodes.items():
        if node.get("enabled", "true") != "true":
            out[node_id] = {"ok": False, "skipped": True}
            continue
        out[node_id] = health_node(node)
    return out
