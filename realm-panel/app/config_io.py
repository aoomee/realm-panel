"""
Read / write /etc/realm/rules/rule-*.conf files.
Also manages /etc/realm/panel/vps_meta.json which stores per-VPS
expiry dates and traffic quota information that the original scripts
do not track.
"""
import glob
import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

RULES_DIR = os.environ.get("RULES_DIR", "/etc/realm/rules")
META_PATH = os.environ.get("META_PATH", "/etc/realm/panel/vps_meta.json")
CONFIG_JSON = os.environ.get("CONFIG_JSON", "/etc/realm/config.json")
REALM_BIN  = os.environ.get("REALM_BIN",  "/usr/local/bin/realm")


# ---------------------------------------------------------------------------
# Low-level rule file helpers
# ---------------------------------------------------------------------------

def _parse_conf(path: str) -> dict:
    """Parse a bash-style key="value" config file into a dict."""
    data: dict[str, Any] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r'^([A-Z_]+)=(.*)$', line)
            if not m:
                continue
            key, val = m.group(1), m.group(2)
            # strip surrounding quotes
            if (val.startswith('"') and val.endswith('"')) or \
               (val.startswith("'") and val.endswith("'")):
                val = val[1:-1]
            data[key] = val
    return data


def _write_field(path: str, key: str, value: str):
    """Update a single key in a bash conf file in-place."""
    content = Path(path).read_text()
    pattern = re.compile(rf'^{re.escape(key)}=.*$', re.MULTILINE)
    replacement = f'{key}="{value}"'
    if pattern.search(content):
        content = pattern.sub(replacement, content)
    else:
        content = content.rstrip("\n") + f'\n{replacement}\n'
    Path(path).write_text(content)


# ---------------------------------------------------------------------------
# Rule CRUD
# ---------------------------------------------------------------------------

def list_rules() -> list[dict]:
    rules = []
    pattern = os.path.join(RULES_DIR, "rule-*.conf")
    for path in sorted(glob.glob(pattern)):
        try:
            d = _parse_conf(path)
            d["_path"] = path
            rules.append(d)
        except Exception:
            pass
    return rules


def get_rule(rule_id: str) -> dict | None:
    path = os.path.join(RULES_DIR, f"rule-{rule_id}.conf")
    if not os.path.exists(path):
        return None
    d = _parse_conf(path)
    d["_path"] = path
    return d


def update_rule_fields(rule_id: str, updates: dict) -> bool:
    path = os.path.join(RULES_DIR, f"rule-{rule_id}.conf")
    if not os.path.exists(path):
        return False
    for key, val in updates.items():
        _write_field(path, key, str(val))
    return True


def _next_rule_id() -> int:
    """Return the next available numeric rule ID."""
    existing = []
    for path in glob.glob(os.path.join(RULES_DIR, "rule-*.conf")):
        name = os.path.basename(path)
        m = re.match(r"rule-(\d+)\.conf", name)
        if m:
            existing.append(int(m.group(1)))
    return max(existing, default=0) + 1


def create_rule(fields: dict) -> dict:
    """
    Create a new rule-*.conf file.  Required keys in `fields`:
      LISTEN_PORT, REMOTE_HOST (or FORWARD_TARGET for RULE_ROLE=2), REMOTE_PORT
    Optional: RULE_NAME, RULE_ROLE (default "1"), LISTEN_IP, BALANCE_MODE, ...
    Returns the newly created rule dict.
    """
    import datetime as _dt
    os.makedirs(RULES_DIR, exist_ok=True)
    rule_id = _next_rule_id()
    path = os.path.join(RULES_DIR, f"rule-{rule_id}.conf")

    defaults = {
        "RULE_ID":               str(rule_id),
        "RULE_NAME":             fields.get("RULE_NAME", f"rule-{rule_id}"),
        "RULE_ROLE":             fields.get("RULE_ROLE", "1"),
        "SECURITY_LEVEL":        fields.get("SECURITY_LEVEL", "standard"),
        "LISTEN_PORT":           fields.get("LISTEN_PORT", ""),
        "LISTEN_IP":             fields.get("LISTEN_IP", "::"),
        "THROUGH_IP":            fields.get("THROUGH_IP", ""),
        "REMOTE_HOST":           fields.get("REMOTE_HOST", ""),
        "REMOTE_PORT":           fields.get("REMOTE_PORT", ""),
        "FORWARD_TARGET":        fields.get("FORWARD_TARGET", ""),
        "TLS_SERVER_NAME":       fields.get("TLS_SERVER_NAME", ""),
        "WS_PATH":               fields.get("WS_PATH", ""),
        "WS_HOST":               fields.get("WS_HOST", ""),
        "RULE_NOTE":             fields.get("RULE_NOTE", ""),
        "ENABLED":               "true",
        "CREATED_TIME":          _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "BALANCE_MODE":          fields.get("BALANCE_MODE", "off"),
        "TARGET_STATES":         "",
        "WEIGHTS":               fields.get("WEIGHTS", ""),
        "FAILOVER_ENABLED":      "false",
        "HEALTH_CHECK_INTERVAL": "4",
        "FAILURE_THRESHOLD":     "2",
        "SUCCESS_THRESHOLD":     "2",
        "CONNECTION_TIMEOUT":    "3",
        "MPTCP_MODE":            "off",
        "PROXY_MODE":            "off",
        "PROTOCOL":              fields.get("PROTOCOL", "both"),
    }

    lines = [f'{k}="{v}"' for k, v in defaults.items()]
    Path(path).write_text("\n".join(lines) + "\n")
    return _parse_conf(path)


def delete_rule(rule_id: str) -> bool:
    """Delete a rule-*.conf file. Returns False if not found."""
    path = os.path.join(RULES_DIR, f"rule-{rule_id}.conf")
    if not os.path.exists(path):
        return False
    os.remove(path)
    return True


# ---------------------------------------------------------------------------
# VPS metadata (expiry + traffic quota)
# ---------------------------------------------------------------------------

def _load_meta() -> dict:
    if os.path.exists(META_PATH):
        try:
            return json.loads(Path(META_PATH).read_text())
        except Exception:
            pass
    return {}


def _save_meta(data: dict):
    os.makedirs(os.path.dirname(META_PATH), exist_ok=True)
    Path(META_PATH).write_text(json.dumps(data, indent=2, ensure_ascii=False))


def list_vps_meta() -> dict:
    return _load_meta()


def get_vps_meta(host: str) -> dict:
    return _load_meta().get(host, {})


def upsert_vps_meta(host: str, fields: dict):
    """
    fields may include:
      expire_date  : "YYYY-MM-DD"
      traffic_total_gb : float  (monthly cap in GB, 0 = unlimited)
      traffic_used_gb  : float  (currently used GB this cycle)
      reset_day    : int   (day-of-month billing resets, default 1)
      note         : str
    """
    meta = _load_meta()
    entry = meta.get(host, {})
    entry.update(fields)
    meta[host] = entry
    _save_meta(meta)


def delete_vps_meta(host: str):
    meta = _load_meta()
    meta.pop(host, None)
    _save_meta(meta)


# ---------------------------------------------------------------------------
# Realm config.json regeneration (mirrors generate_complete_config in core.sh)
# ---------------------------------------------------------------------------

def _rule_to_endpoint(rule: dict) -> str | None:
    """Convert a rule-*.conf dict to a Realm JSON endpoint fragment."""
    if rule.get("ENABLED", "true") != "true":
        return None

    listen_ip = rule.get("LISTEN_IP", "::")
    listen_port = rule.get("LISTEN_PORT", "")
    listen = f"{listen_ip}:{listen_port}"

    role = rule.get("RULE_ROLE", "1")
    if role == "2":
        remote = rule.get("FORWARD_TARGET", "")
    else:
        remote_host = rule.get("REMOTE_HOST", "")
        remote_port = rule.get("REMOTE_PORT", "")
        # multi-host (load balance): pick first live or use all as balance list
        if "," in remote_host:
            hosts = [h.strip() for h in remote_host.split(",")]
        else:
            hosts = [remote_host]
        # for now join back; Realm balance is handled via weights in config
        remote = f"{hosts[0]}:{remote_port}"

    security = rule.get("SECURITY_LEVEL", "standard")
    parts = {
        "listen": listen,
        "remote": remote,
    }

    if security not in ("standard", "", None):
        parts["transport"] = _build_transport(rule, security)

    return json.dumps(parts, ensure_ascii=False)


def _build_transport(rule: dict, security: str) -> dict:
    t: dict[str, Any] = {}
    if "tls" in security:
        tls: dict[str, Any] = {}
        sni = rule.get("TLS_SERVER_NAME", "")
        if sni:
            tls["sni"] = sni
        cert = rule.get("TLS_CERT_PATH", "")
        key  = rule.get("TLS_KEY_PATH", "")
        if cert:
            tls["cert"] = cert
        if key:
            tls["key"] = key
        t["tls"] = tls
    if "ws" in security:
        ws: dict[str, Any] = {}
        path = rule.get("WS_PATH", "")
        host = rule.get("WS_HOST", "")
        if path:
            ws["path"] = path
        if host:
            ws["host"] = host
        t["websocket"] = ws
    return t


def regenerate_config():
    """Rebuild /etc/realm/config.json from rule files."""
    rules = list_rules()
    endpoint_parts = []
    for r in rules:
        ep = _rule_to_endpoint(r)
        if ep:
            endpoint_parts.append(f"\n        {ep}")

    endpoints_str = ",".join(endpoint_parts)
    config = {
        "network": {"no_tcp": False, "use_udp": True},
        "endpoints": json.loads(f"[{endpoints_str}]") if endpoints_str.strip() else []
    }
    os.makedirs(os.path.dirname(CONFIG_JSON), exist_ok=True)
    Path(CONFIG_JSON).write_text(json.dumps(config, indent=4, ensure_ascii=False))
