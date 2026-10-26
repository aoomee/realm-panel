"""
Per-node registry and CRUD for remote relay/exit agents.

A "node" is a remote machine running realm-agent. Each node has:
  id       : stable string (slug), used as primary key
  name     : human label, e.g. "HK中转-01"
  role     : "relay" or "exit" (informational; drives UI grouping)
  host     : public IP / domain of the machine (used in line links + relay targets)
  agent_port: agent listening port (default 5001)
  url      : agent base URL, derived as http://{host}:{agent_port} unless given
  token    : bearer token matching that agent's AGENT_TOKEN
  note     : free text
  enabled  : "true" / "false" — disabled nodes are skipped during push
  created  : timestamp

Storage: JSON file at NODES_PATH (default /etc/realm/panel/nodes.json),
kept on the realm_data volume alongside vps_meta.json so it survives
container recreation.
"""
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from .filelock import locked

NODES_PATH = os.environ.get("NODES_PATH", "/etc/realm/panel/nodes.json")


def _load() -> dict:
    if os.path.exists(NODES_PATH):
        try:
            return json.loads(Path(NODES_PATH).read_text())
        except Exception:
            pass
    return {}


def _save(data: dict):
    os.makedirs(os.path.dirname(NODES_PATH), exist_ok=True)
    Path(NODES_PATH).write_text(json.dumps(data, indent=2, ensure_ascii=False))


def _slugify(name: str) -> str:
    """Make a URL-safe slug; falls back to a short random suffix if name is empty."""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name or "").strip("-").lower()
    if s:
        return s
    return "node-" + secrets.token_hex(3)


def _unique_id(name: str, existing: dict) -> str:
    base = _slugify(name)
    if base not in existing:
        return base
    i = 2
    while f"{base}-{i}" in existing:
        i += 1
    return f"{base}-{i}"


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def list_nodes() -> dict:
    """Return the full nodes dict {id: {...}}."""
    return _load()


def get_node(node_id: str) -> dict | None:
    return _load().get(node_id)


def _host_from_url(url: str) -> str:
    from urllib.parse import urlparse
    try:
        return urlparse(url).hostname or ""
    except Exception:
        return ""


def _fmt_url(host: str, port) -> str:
    h = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"http://{h}:{port}"


def node_host(node: dict) -> str:
    """Public address of a node (explicit host, else parsed from agent url)."""
    return (node.get("host") or "").strip() or _host_from_url(node.get("url", ""))


def _create_node_impl(fields: dict) -> dict:
    """
    fields: { name, role, host, agent_port, url?, token?, note, enabled }
    token is auto-generated when omitted; url is derived from host+port.
    Returns the created node dict (with generated id + timestamps).
    """
    data = _load()
    name = (fields.get("name") or "").strip()
    node_id = _unique_id(name or "node", data)
    now = datetime.now(timezone.utc).isoformat()
    url = (fields.get("url") or "").strip().rstrip("/")
    host = (fields.get("host") or "").strip() or _host_from_url(url)
    port = str(fields.get("agent_port") or "5001").strip()
    if not url and host:
        url = _fmt_url(host, port)
    node = {
        "id":         node_id,
        "name":       name,
        "role":       fields.get("role", "relay"),
        "host":       host,
        "agent_port": port,
        "url":        url,
        "token":      (fields.get("token") or "").strip() or secrets.token_hex(24),
        "note":       fields.get("note", ""),
        "enabled":    fields.get("enabled", "true"),
        "created_at": now,
        "updated_at": now,
        "last_push":  None,
        "last_push_ok": None,
    }
    data[node_id] = node
    _save(data)
    return node


def _update_node_impl(node_id: str, fields: dict) -> dict | None:
    data = _load()
    node = data.get(node_id)
    if not node:
        return None
    for k in ("name", "role", "url", "token", "note", "enabled", "host", "agent_port"):
        if k in fields:
            node[k] = fields[k] if k != "url" else (fields[k] or "").strip().rstrip("/")
    # host / port edits re-derive the agent url unless the url itself was given
    if ("host" in fields or "agent_port" in fields) and "url" not in fields and node.get("host"):
        node["url"] = _fmt_url(node["host"].strip(), node.get("agent_port") or "5001")
    node["updated_at"] = datetime.now(timezone.utc).isoformat()
    data[node_id] = node
    _save(data)
    return node


def _delete_node_impl(node_id: str) -> bool:
    data = _load()
    if node_id not in data:
        return False
    del data[node_id]
    _save(data)
    return True


def _set_push_result_impl(node_id: str, ok: bool, detail: str = "", config_hash: str | None = None):
    """Called by node_client after a push attempt, to record last outcome."""
    data = _load()
    node = data.get(node_id)
    if not node:
        return
    node["last_push"]    = datetime.now(timezone.utc).isoformat()
    node["last_push_ok"] = bool(ok)
    node["last_push_detail"] = detail[:500]
    # remember what the node acknowledged; cleared on failure so it's retried
    node["config_hash"] = config_hash
    data[node_id] = node
    _save(data)


def create_node(fields: dict) -> dict:
    with locked(NODES_PATH):
        return _create_node_impl(fields)


def update_node(node_id: str, fields: dict) -> dict | None:
    with locked(NODES_PATH):
        return _update_node_impl(node_id, fields)


def delete_node(node_id: str) -> bool:
    with locked(NODES_PATH):
        return _delete_node_impl(node_id)


def set_push_result(node_id: str, ok: bool, detail: str = "", config_hash: str | None = None):
    with locked(NODES_PATH):
        return _set_push_result_impl(node_id, ok, detail, config_hash)
