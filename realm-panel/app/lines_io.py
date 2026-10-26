"""
线路 (lines): one-click relay → exit forwarding chains.

A line ties a relay node (中转) to one or more exit nodes (落地) and
generates the underlying realm rules for you:

  mode "direct"  relay:relay_port ──▶ exit_host:target_port
                 (the proxy service on the exit listens publicly; the
                  exit needs no realm rule)

  mode "chain"   relay:relay_port ──▶ exit_host:exit_port ──▶ target_host:target_port
                 (realm on the exit forwards again, e.g. to 127.0.0.1:443,
                  so the real service port need not be public)

With several exits the relay rule load-balances across them; the weight
engine then adjusts the split from each exit's expiry / traffic meta.

The rules carry LINE_ID so deleting a line removes exactly its rules.
Storage: LINES_PATH (default /etc/realm/panel/lines.json).
"""
import json
import os
import random
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config_io, node_io, share_link
from .filelock import locked

LINES_PATH = os.environ.get("LINES_PATH", "/etc/realm/panel/lines.json")
PORT_MIN = int(os.environ.get("LINE_PORT_MIN", "20000"))
PORT_MAX = int(os.environ.get("LINE_PORT_MAX", "59999"))
# public address of machine A, used as the entry when the relay is the panel host
PANEL_PUBLIC_HOST = os.environ.get("PANEL_PUBLIC_HOST", "")


class LineError(ValueError):
    pass


def _load() -> dict:
    if os.path.exists(LINES_PATH):
        try:
            return json.loads(Path(LINES_PATH).read_text())
        except Exception:
            pass
    return {}


def _save(data: dict):
    os.makedirs(os.path.dirname(LINES_PATH), exist_ok=True)
    Path(LINES_PATH).write_text(json.dumps(data, indent=2, ensure_ascii=False))


def list_lines() -> dict:
    return _load()


def get_line(line_id: str) -> dict | None:
    return _load().get(line_id)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _used_ports(node_id: str) -> set[int]:
    used = set()
    for r in config_io.list_rules():
        if (r.get("NODE_ID", "") or "") == node_id:
            try:
                used.add(int(r.get("LISTEN_PORT", 0)))
            except ValueError:
                pass
    if node_id:
        n = node_io.get_node(node_id) or {}
        try:
            used.add(int(n.get("agent_port") or 5001))
        except ValueError:
            pass
    else:
        used.add(5000)  # panel itself
    return used


def _pick_port(node_ids: list[str]) -> int:
    used: set[int] = set()
    for nid in node_ids:
        used |= _used_ports(nid)
    for _ in range(500):
        p = random.randint(PORT_MIN, PORT_MAX)
        if p not in used:
            return p
    raise LineError("没有可用端口")


def _check_port(node_id: str, port: int, label: str):
    if not (1 <= port <= 65535):
        raise LineError(f"{label}端口无效: {port}")
    if port in _used_ports(node_id):
        raise LineError(f"{label}端口 {port} 已被该节点上的其他规则占用")


def _node_label(node_id: str) -> str:
    if not node_id:
        return "本机(A)"
    n = node_io.get_node(node_id) or {}
    return n.get("name") or node_id


def _int(v, default=0) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# deploy / remove
# ---------------------------------------------------------------------------

def _deploy_line_impl(fields: dict, fallback_entry_host: str = "") -> dict:
    """
    Create a line and its rules. Returns the stored line dict.
    Raises LineError on invalid input. Does not push — caller does.

    fields:
      name, relay_node ("" = A), exit_nodes [ids], mode direct|chain,
      link (optional original share link), target_host, target_port,
      relay_port (blank = auto), exit_port (blank = auto, chain only),
      balance off|roundrobin|iphash, protocol, entry_host (optional override)
    """
    nodes = node_io.list_nodes()
    relay = (fields.get("relay_node") or "").strip()
    exits = [e for e in (fields.get("exit_nodes") or []) if e]
    mode = fields.get("mode") or "direct"
    if mode not in ("direct", "chain"):
        raise LineError("mode 必须是 direct 或 chain")

    if relay and relay not in nodes:
        raise LineError(f"中转节点不存在: {relay}")
    for e in exits:
        if e not in nodes:
            raise LineError(f"落地节点不存在: {e}")
        if e == relay:
            raise LineError("中转和落地不能是同一台机器")

    link = (fields.get("link") or "").strip()
    parsed = share_link.parse(link) if link else None
    if link and not parsed:
        raise LineError("无法识别的节点链接（支持 vless/vmess/trojan/ss/hy2/tuic/socks 等）")

    target_port = _int(fields.get("target_port")) or (_int(parsed["port"]) if parsed else 0)
    if not target_port:
        raise LineError("请填写落地服务端口，或粘贴落地节点链接自动识别")

    # exit addresses the relay forwards to
    if exits:
        exit_hosts = []
        for e in exits:
            h = node_io.node_host(nodes[e])
            if not h:
                raise LineError(f"落地节点 {nodes[e].get('name') or e} 没有填写公网 IP/域名")
            exit_hosts.append(h)
    else:
        # unmanaged landing (e.g. a third-party node): direct only
        if mode == "chain":
            raise LineError("链式模式需要选择受管落地节点")
        manual = (fields.get("target_host") or (parsed["host"] if parsed else "")).strip()
        if not manual:
            raise LineError("请选择落地节点，或填写落地地址")
        exit_hosts = [manual]

    relay_port = _int(fields.get("relay_port")) or _pick_port([relay])
    _check_port(relay, relay_port, "中转监听")

    exit_port = 0
    if mode == "chain":
        exit_port = _int(fields.get("exit_port")) or _pick_port(exits)
        for e in exits:
            _check_port(e, exit_port, f"落地({_node_label(e)})监听")

    entry_host = (fields.get("entry_host") or "").strip()
    if not entry_host:
        entry_host = node_io.node_host(nodes[relay]) if relay else (PANEL_PUBLIC_HOST or fallback_entry_host)
    if not entry_host:
        raise LineError("无法确定入口地址，请填写入口地址（中转机公网 IP/域名）")

    data = _load()
    n = 1
    while f"L{n}" in data:
        n += 1
    line_id = f"L{n}"
    name = (fields.get("name") or "").strip() or f"线路{n}"
    protocol = fields.get("protocol") or "both"
    balance = fields.get("balance") or ("roundrobin" if len(exit_hosts) > 1 else "off")
    if len(exit_hosts) == 1:
        balance = "off"

    rule_ids: list[str] = []
    try:
        # 1. relay rule
        r = config_io.create_rule({
            "RULE_NAME":    f"{name}-中转",
            "RULE_ROLE":    "1",
            "LISTEN_PORT":  str(relay_port),
            "LISTEN_IP":    "::",
            "REMOTE_HOST":  ",".join(exit_hosts),
            "REMOTE_PORT":  str(exit_port if mode == "chain" else target_port),
            "BALANCE_MODE": balance,
            "PROTOCOL":     protocol,
            "NODE_ID":      relay,
            "LINE_ID":      line_id,
            "RULE_NOTE":    f"线路 {line_id}",
        })
        rule_ids.append(r["RULE_ID"])

        # 2. exit rules (chain mode)
        if mode == "chain":
            th = (fields.get("target_host") or "127.0.0.1").strip()
            th_fmt = f"[{th}]" if ":" in th and not th.startswith("[") else th
            for e in exits:
                r = config_io.create_rule({
                    "RULE_NAME":      f"{name}-落地",
                    "RULE_ROLE":      "2",
                    "LISTEN_PORT":    str(exit_port),
                    "LISTEN_IP":      "::",
                    "FORWARD_TARGET": f"{th_fmt}:{target_port}",
                    "PROTOCOL":       protocol,
                    "NODE_ID":        e,
                    "LINE_ID":        line_id,
                    "RULE_NOTE":      f"线路 {line_id}",
                })
                rule_ids.append(r["RULE_ID"])
    except Exception:
        for rid in rule_ids:
            config_io.delete_rule(rid)
        raise

    out_link = share_link.rewrite(link, entry_host, relay_port, f"-{name}") if link else ""
    fmt_entry = f"[{entry_host}]" if ":" in entry_host else entry_host
    line = {
        "id":          line_id,
        "name":        name,
        "mode":        mode,
        "relay_node":  relay,
        "exit_nodes":  exits,
        "exit_hosts":  exit_hosts,
        "relay_port":  relay_port,
        "exit_port":   exit_port,
        "target_host": (fields.get("target_host") or "127.0.0.1") if mode == "chain" else "",
        "target_port": target_port,
        "balance":     balance,
        "protocol":    protocol,
        "entry_host":  entry_host,
        "entry":       f"{fmt_entry}:{relay_port}",
        "link":        link,
        "out_link":    out_link or "",
        "rule_ids":    rule_ids,
        "created_at":  datetime.now(timezone.utc).isoformat(),
    }
    data[line_id] = line
    _save(data)
    return line


def affected_nodes(line: dict) -> list[str]:
    """Node ids whose config changes when this line is added/removed ("" = A)."""
    out = [line.get("relay_node", "")]
    if line.get("mode") == "chain":
        out += line.get("exit_nodes", [])
    return list(dict.fromkeys(out))


def _delete_line_impl(line_id: str) -> dict | None:
    """Remove the line and every rule tagged with it. Returns the removed line."""
    data = _load()
    line = data.pop(line_id, None)
    if line is None:
        return None
    for r in config_io.list_rules():
        if r.get("LINE_ID") == line_id:
            config_io.delete_rule(r["RULE_ID"])
    _save(data)
    return line


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------

def _tcp(host: str, port: int, timeout: float = 3.0) -> dict:
    t0 = time.monotonic()
    try:
        with socket.create_connection((host.strip("[]"), int(port)), timeout=timeout):
            return {"ok": True, "ms": round((time.monotonic() - t0) * 1000, 1)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def test_line(line_id: str) -> dict:
    """
    Hop-by-hop TCP check:
      1. 面板(A) → 入口 (entry_host:relay_port)
      2. 中转 → 每个落地 (via relay agent /tcping, or directly if relay is A)
      3. chain mode: 落地 → 目标服务 (via exit agent /tcping)
    """
    from . import node_client
    line = get_line(line_id)
    if not line:
        return {"error": "not found"}
    nodes = node_io.list_nodes()
    steps = []

    r = _tcp(line["entry_host"], line["relay_port"])
    steps.append({"step": f"A → 入口 {line['entry']}", **r})

    relay = line.get("relay_node", "")
    hop_port = line["exit_port"] if line["mode"] == "chain" else line["target_port"]
    for h in line.get("exit_hosts", []):
        if relay:
            r = node_client.tcping_via(nodes.get(relay, {}), h, hop_port)
        else:
            r = _tcp(h, hop_port)
        steps.append({"step": f"中转({_node_label(relay)}) → {h}:{hop_port}", **r})

    if line["mode"] == "chain":
        for e in line.get("exit_nodes", []):
            r = node_client.tcping_via(nodes.get(e, {}), line["target_host"], line["target_port"])
            steps.append({"step": f"落地({_node_label(e)}) → {line['target_host']}:{line['target_port']}", **r})

    return {"ok": all(s.get("ok") for s in steps), "steps": steps}


# all line mutations also allocate ports / create rule files, so serialise them
def deploy_line(fields: dict, fallback_entry_host: str = "") -> dict:
    with locked(LINES_PATH):
        return _deploy_line_impl(fields, fallback_entry_host)


def delete_line(line_id: str) -> dict | None:
    with locked(LINES_PATH):
        return _delete_line_impl(line_id)
