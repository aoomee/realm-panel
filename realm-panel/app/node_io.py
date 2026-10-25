"""
Per-node registry and CRUD for remote relay/exit agents.

A "node" is a remote machine running realm-agent. Each node has:
  id       : stable string (slug), used as primary key
  name     : human label, e.g. "HK中转-01"
  role     : "relay" or "exit" (informational; drives UI grouping)
  url      : agent base URL, e.g. http://1.2.3.4:5001
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


def create_node(fields: dict) -> dict:
    """
    fields: { name, role, url, token, note, enabled }
    Returns the created node dict (with generated id + timestamps).
    """
    data = _load()
    name = (fields.get("name") or "").strip()
    node_id = _unique_id(name or "node", data)
    now = datetime.now(timezone.utc).isoformat()
    node = {
        "id":         node_id,
        "name":       name,
        "role":       fields.get("role", "relay"),
        "url":        (fields.get("url") or "").strip().rstrip("/"),
        "token":      (fields.get("token") or "").strip(),
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


def update_node(node_id: str, fields: dict) -> dict | None:
    data = _load()
    node = data.get(node_id)
    if not node:
        return None
    for k in ("name", "role", "url", "token", "note", "enabled"):
        if k in fields:
            node[k] = fields[k] if k != "url" else (fields[k] or "").strip().rstrip("/")
    node["updated_at"] = datetime.now(timezone.utc).isoformat()
    data[node_id] = node
    _save(data)
    return node


def delete_node(node_id: str) -> bool:
    data = _load()
    if node_id not in data:
        return False
    del data[node_id]
    _save(data)
    return True


def set_push_result(node_id: str, ok: bool, detail: str = ""):
    """Called by node_client after a push attempt, to record last outcome."""
    data = _load()
    node = data.get(node_id)
    if not node:
        return
    node["last_push"]    = datetime.now(timezone.utc).isoformat()
    node["last_push_ok"] = bool(ok)
    node["last_push_detail"] = detail[:500]
    data[node_id] = node
    _save(data)
