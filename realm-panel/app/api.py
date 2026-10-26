"""
HTTP API for the realm panel.

Endpoints
  GET    /api/health
  GET    /api/rules
  POST   /api/rules                body: { LISTEN_PORT, REMOTE_HOST, REMOTE_PORT, ... }
  GET    /api/rules/<id>
  PUT    /api/rules/<id>           body: { fields: { KEY: value, ... } }
  DELETE /api/rules/<id>
  GET    /api/vps                  list all VPS metadata
  GET    /api/vps/<host>           get one host's metadata
  PUT    /api/vps/<host>           upsert metadata fields
  DELETE /api/vps/<host>
  GET    /api/weights/preview      dry-run: shows computed scores/weights
  POST   /api/weights/recompute    run engine now + reload realm
  GET    /api/realm/status
  POST   /api/realm/restart
  GET    /api/monitor              last monitor state (hosts + rule selftests)
  POST   /api/monitor/probe        trigger immediate host probe
  POST   /api/monitor/selftest     run selftest on all enabled rules
  POST   /api/monitor/selftest/<id> run selftest on one rule
  GET    /api/nodes                list all relay/exit nodes
  POST   /api/nodes                register a new node
  GET    /api/nodes/<id>           get one node
  PUT    /api/nodes/<id>           update node fields
  DELETE /api/nodes/<id>           remove a node
  POST   /api/nodes/<id>/push      push the node its own rules (NODE_ID == id)
  POST   /api/nodes/push-all       push each enabled node its own rules
  GET    /api/nodes/<id>/config    preview a node's config ("local" = machine A)
  GET    /api/nodes/<id>/health    probe one node's /health endpoint
  GET    /api/nodes/<id>/install-cmd  one-line deploy command for that node
  POST   /api/nodes/import         bulk add: "名称 角色 IP [端口]" per line
  GET    /api/lines                list lines (线路)
  POST   /api/lines                one-click deploy relay → exit line
  DELETE /api/lines/<id>           remove line and its rules
  POST   /api/lines/<id>/redeploy  re-push the line's nodes
  POST   /api/lines/<id>/test      hop-by-hop TCP test
  POST   /api/links/parse          parse a share link (host/port)
"""
import logging
from datetime import date, datetime

from flask import Blueprint, jsonify, request

from . import config_io
from .realm_ctrl import restart_realm
from .weight_engine import recalculate_weights, _score_host, _remaining_days, _traffic_fraction
from .scheduler import trigger_now
from . import monitor as mon
from . import node_io
from . import node_client
from . import lines_io

log = logging.getLogger(__name__)
bp = Blueprint("api", __name__, url_prefix="/api")


# --------------------------- health ---------------------------
@bp.get("/health")
def health():
    return jsonify({"status": "ok"})


# --------------------------- rules ---------------------------
@bp.get("/rules")
def get_rules():
    rules = config_io.list_rules()
    # attach VPS meta summary for convenience
    meta = config_io.list_vps_meta()
    out = []
    for r in rules:
        host = r.get("REMOTE_HOST", "")
        if "," in host:
            host = host.split(",")[0].strip()
        r["_vps_meta"] = meta.get(host, {})
        out.append(r)
    return jsonify(out)


@bp.get("/rules/<int:rule_id>")
def get_one_rule(rule_id):
    r = config_io.get_rule(str(rule_id))
    if r is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(r)


@bp.put("/rules/<int:rule_id>")
def update_rule(rule_id):
    body = request.get_json(force=True)
    fields = body.get("fields", body)
    if not isinstance(fields, dict) or not fields:
        return jsonify({"error": "no fields provided"}), 400
    ok = config_io.update_rule_fields(str(rule_id), fields)
    if not ok:
        return jsonify({"error": "rule not found"}), 404
    config_io.regenerate_config()   # e.g. enable/disable must reach realm
    return jsonify({"ok": True})


@bp.post("/rules")
def create_rule():
    body = request.get_json(force=True)
    if not isinstance(body, dict):
        return jsonify({"error": "expected JSON object"}), 400
    if not body.get("LISTEN_PORT"):
        return jsonify({"error": "LISTEN_PORT is required"}), 400
    rule = config_io.create_rule(body)
    res = config_io.regenerate_config()
    rule["_push"] = res.get("pushed", {})
    return jsonify(rule), 201


@bp.delete("/rules/<int:rule_id>")
def delete_rule(rule_id):
    ok = config_io.delete_rule(str(rule_id))
    if not ok:
        return jsonify({"error": "rule not found"}), 404
    config_io.regenerate_config()
    return jsonify({"ok": True})


# --------------------------- VPS metadata ---------------------------
@bp.get("/vps")
def get_vps():
    return jsonify(config_io.list_vps_meta())


@bp.get("/vps/<path:host>")
def get_vps_one(host: str):
    return jsonify(config_io.get_vps_meta(host))


@bp.put("/vps/<path:host>")
def put_vps(host: str):
    body = request.get_json(force=True)
    if not isinstance(body, dict):
        return jsonify({"error": "expected JSON object"}), 400
    config_io.upsert_vps_meta(host, body)
    return jsonify({"ok": True, "host": host, "meta": config_io.get_vps_meta(host)})


@bp.delete("/vps/<path:host>")
def del_vps(host: str):
    config_io.delete_vps_meta(host)
    return jsonify({"ok": True})


# --------------------------- weight engine ---------------------------
@bp.get("/weights/preview")
def weights_preview():
    """Dry run: compute scores/weights without writing."""
    rules = config_io.list_rules()
    meta  = config_io.list_vps_meta()
    groups: dict[str, list[dict]] = {}
    for r in rules:
        if r.get("BALANCE_MODE", "off") == "off":
            continue
        if r.get("RULE_ROLE", "1") != "1":
            continue
        port = r.get("LISTEN_PORT", "")
        key = f'{r.get("NODE_ID", "") or "local"}:{port}'
        groups.setdefault(key, []).append(r)

    preview = {}
    for port, group in groups.items():
        hosts: list[str] = []
        for rule in group:
            rh = rule.get("REMOTE_HOST", "")
            if "," in rh:
                for h in rh.split(","):
                    h = h.strip()
                    if h and h not in hosts:
                        hosts.append(h)
            else:
                if rh and rh not in hosts:
                    hosts.append(rh)
        if len(hosts) < 2:
            continue
        rows = []
        for h in hosts:
            m = meta.get(h, {})
            rows.append({
                "host": h,
                "expire_date": m.get("expire_date", ""),
                "remaining_days": _remaining_days(m),
                "traffic_total_gb": m.get("traffic_total_gb", 0),
                "traffic_used_gb":  m.get("traffic_used_gb", 0),
                "score": round(_score_host(h, meta), 4),
            })
        preview[port] = rows
    return jsonify(preview)


@bp.post("/weights/recompute")
def recompute():
    groups = recalculate_weights()
    config_io.regenerate_config()
    return jsonify({"ok": True, "groups": list(groups.keys())})


# --------------------------- realm control ---------------------------
@bp.get("/realm/status")
def realm_status():
    # simplistic check via docker
    try:
        import docker
        c = docker.from_env().containers.get("realm")
        return jsonify({"state": c.status})
    except Exception as e:
        return jsonify({"state": "unknown", "error": str(e)}), 200


@bp.post("/realm/restart")
def realm_restart():
    restart_realm()
    return jsonify({"ok": True})


# --------------------------- monitoring ---------------------------
@bp.get("/monitor")
def monitor_state():
    """Return last persisted monitor state (hosts + rule selftests)."""
    return jsonify(mon.get_state())


@bp.post("/monitor/probe")
def monitor_probe():
    """Trigger an immediate probe of all backend hosts."""
    return jsonify({"ok": True, "hosts": mon.probe_all_hosts()})


@bp.post("/monitor/selftest")
def monitor_selftest_all():
    """Run self-test on every enabled rule."""
    return jsonify({"ok": True, "rules": mon.selftest_all()})


@bp.post("/monitor/selftest/<int:rule_id>")
def monitor_selftest_one(rule_id):
    result = mon.selftest_rule(str(rule_id))
    if "error" in result:
        return jsonify(result), 404
    return jsonify(result)


# --------------------------- node registry ---------------------------

@bp.get("/nodes")
def get_nodes():
    return jsonify(node_io.list_nodes())


@bp.post("/nodes")
def create_node():
    body = request.get_json(force=True)
    if not isinstance(body, dict):
        return jsonify({"error": "expected JSON object"}), 400
    if not body.get("url") and not body.get("host"):
        return jsonify({"error": "请填写公网 IP/域名"}), 400
    node = node_io.create_node(body)   # token auto-generated when omitted
    return jsonify(node), 201


@bp.get("/nodes/<node_id>")
def get_node(node_id):
    node = node_io.get_node(node_id)
    if node is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(node)


@bp.put("/nodes/<node_id>")
def update_node(node_id):
    body = request.get_json(force=True)
    if not isinstance(body, dict) or not body:
        return jsonify({"error": "no fields provided"}), 400
    node = node_io.update_node(node_id, body)
    if node is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(node)


@bp.delete("/nodes/<node_id>")
def delete_node(node_id):
    used = [l["name"] for l in lines_io.list_lines().values()
            if l.get("relay_node") == node_id or node_id in l.get("exit_nodes", [])]
    if used:
        return jsonify({"error": "该节点仍被线路使用：" + "、".join(used) + "，请先删除这些线路"}), 409
    ok = node_io.delete_node(node_id)
    if not ok:
        return jsonify({"error": "not found"}), 404
    # its rules can no longer be delivered anywhere — drop them
    for r in config_io.list_rules():
        if r.get("NODE_ID") == node_id:
            config_io.delete_rule(r["RULE_ID"])
    return jsonify({"ok": True})


@bp.post("/nodes/<node_id>/push")
def push_one_node(node_id):
    """Push this node its own config (only rules assigned to it)."""
    if node_io.get_node(node_id) is None:
        return jsonify({"error": "not found"}), 404
    result = node_client.push_node(node_id, force=True)
    code = 200 if result.get("ok") else 502
    return jsonify(result), code


@bp.post("/nodes/push-all")
def push_all_nodes():
    """Push every enabled node its own filtered config."""
    results = node_client.push_config_to_all(force=True)
    all_ok = all(v.get("ok") or v.get("skipped") for v in results.values())
    return jsonify({"ok": all_ok, "results": results})


@bp.get("/nodes/<node_id>/config")
def node_config_preview(node_id):
    """Preview the config that would be pushed to a node ("local" = machine A)."""
    nid = "" if node_id == "local" else node_id
    return jsonify(config_io.build_config(nid))


@bp.get("/nodes/<node_id>/health")
def node_health(node_id):
    node = node_io.get_node(node_id)
    if node is None:
        return jsonify({"error": "not found"}), 404
    result = node_client.health_node(node)
    code = 200 if result.get("ok") else 502
    return jsonify(result), code


# --------------------------- install command / bulk import ---------------------------

def _install_cmd(node: dict, panel: str, gh_proxy: str = "") -> str:
    import shlex
    panel = panel.rstrip("/")
    env = [f"PANEL={shlex.quote(panel)}", f"TOKEN={shlex.quote(node['token'])}",
           f"PORT={shlex.quote(str(node.get('agent_port') or 5001))}"]
    if gh_proxy:
        env.append(f"GH_PROXY={shlex.quote(gh_proxy)}")
    return f"curl -fsSL {shlex.quote(panel + '/agent/install.sh')} | {' '.join(env)} bash"


def _panel_base() -> str:
    """Panel URL as seen by remote nodes: ?panel= override, else this request's origin."""
    p = (request.args.get("panel") or "").strip()
    return p or request.host_url.rstrip("/")


@bp.get("/nodes/<node_id>/install-cmd")
def node_install_cmd(node_id):
    node = node_io.get_node(node_id)
    if node is None:
        return jsonify({"error": "not found"}), 404
    gh = (request.args.get("gh_proxy") or "").strip()
    return jsonify({"cmd": _install_cmd(node, _panel_base(), gh),
                    "uninstall": "systemctl disable --now realm-agent realm; "
                                 "rm -f /etc/systemd/system/realm-agent.service /usr/local/bin/realm-agent /etc/realm/agent.env; "
                                 "systemctl daemon-reload"})


_ROLE_ALIASES = {"relay": "relay", "中转": "relay", "入口": "relay",
                 "exit": "exit", "落地": "exit", "出口": "exit"}


@bp.post("/nodes/import")
def import_nodes():
    """
    Bulk-add nodes. body: {"text": "..."} one node per line:
        名称 角色 IP/域名 [Agent端口]
    separated by spaces, tabs or commas; 角色 = 中转/落地 (relay/exit).
    """
    import re
    text = (request.get_json(force=True) or {}).get("text", "")
    created, errors = [], []
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p for p in re.split(r"[,\s，]+", line) if p]
        if len(parts) < 3:
            errors.append(f"第{i}行格式不对：{raw}")
            continue
        role = _ROLE_ALIASES.get(parts[1].lower())
        if not role:
            errors.append(f"第{i}行角色应为 中转/落地：{parts[1]}")
            continue
        port = parts[3] if len(parts) > 3 else "5001"
        if not port.isdigit():
            errors.append(f"第{i}行端口无效：{port}")
            continue
        created.append(node_io.create_node({"name": parts[0], "role": role,
                                            "host": parts[2], "agent_port": port}))
    return jsonify({"created": created, "errors": errors})


# --------------------------- lines (线路) ---------------------------

@bp.get("/lines")
def get_lines():
    return jsonify(lines_io.list_lines())


def _apply_line_nodes(node_ids: list[str]) -> dict:
    """Regenerate local config and push the affected nodes; return per-node results."""
    config_io.regenerate_config(push_to_nodes=False)
    pushed = node_client.push_nodes(node_ids, force=True)
    out = {}
    nodes = node_io.list_nodes()
    for nid in node_ids:
        label = "本机(A)" if not nid else (nodes.get(nid, {}).get("name") or nid)
        if not nid:
            out[label] = {"ok": True, "detail": "本机配置已更新"}
        else:
            r = pushed.get(nid, {})
            out[label] = {"ok": bool(r.get("ok")), "detail": node_client._summary(r)}
    return out


@bp.post("/lines")
def create_line():
    body = request.get_json(force=True)
    if not isinstance(body, dict):
        return jsonify({"error": "expected JSON object"}), 400
    try:
        line = lines_io.deploy_line(body, fallback_entry_host=request.host.rsplit(":", 1)[0].strip("[]"))
    except lines_io.LineError as e:
        return jsonify({"error": str(e)}), 400
    line["_push"] = _apply_line_nodes(lines_io.affected_nodes(line))
    return jsonify(line), 201


@bp.delete("/lines/<line_id>")
def remove_line(line_id):
    line = lines_io.delete_line(line_id)
    if line is None:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True, "_push": _apply_line_nodes(lines_io.affected_nodes(line))})


@bp.post("/lines/<line_id>/redeploy")
def redeploy_line(line_id):
    line = lines_io.get_line(line_id)
    if line is None:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True, "_push": _apply_line_nodes(lines_io.affected_nodes(line))})


@bp.post("/lines/<line_id>/test")
def test_line(line_id):
    result = lines_io.test_line(line_id)
    if "error" in result:
        return jsonify(result), 404
    return jsonify(result)


@bp.post("/links/parse")
def parse_link():
    from . import share_link
    info = share_link.parse((request.get_json(force=True) or {}).get("link", ""))
    if not info:
        return jsonify({"error": "无法识别的链接"}), 400
    return jsonify(info)
