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
"""
import logging
from datetime import date, datetime

from flask import Blueprint, jsonify, request

from . import config_io
from .realm_ctrl import restart_realm, reload_realm
from .weight_engine import recalculate_weights, _score_host, _remaining_days, _traffic_fraction
from .scheduler import trigger_now
from . import monitor as mon

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
    return jsonify({"ok": True})


@bp.post("/rules")
def create_rule():
    body = request.get_json(force=True)
    if not isinstance(body, dict):
        return jsonify({"error": "expected JSON object"}), 400
    if not body.get("LISTEN_PORT"):
        return jsonify({"error": "LISTEN_PORT is required"}), 400
    rule = config_io.create_rule(body)
    config_io.regenerate_config()
    reload_realm()
    return jsonify(rule), 201


@bp.delete("/rules/<int:rule_id>")
def delete_rule(rule_id):
    ok = config_io.delete_rule(str(rule_id))
    if not ok:
        return jsonify({"error": "rule not found"}), 404
    config_io.regenerate_config()
    reload_realm()
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
        groups.setdefault(port, []).append(r)

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
    reload_realm()
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
