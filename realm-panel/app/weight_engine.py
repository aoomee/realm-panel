"""
Dynamic weight engine.

For every enabled load-balance rule group (same LISTEN_PORT, BALANCE_MODE != off)
this module computes a weight for each backend host based on:

  score = w_days * days_score + w_traffic * traffic_score

  days_score    = clamp(remaining_days / MAX_DAYS, 0, 1)
  traffic_score = clamp(remaining_gb   / total_gb,  0, 1)  (1.0 if unlimited)

Weights are normalised to integers in [1, 100] and written back to every
rule-*.conf that belongs to the group via WEIGHTS=.

If a host has no VPS metadata at all it gets a neutral score of 0.5 so it
still participates in the pool.
"""
import logging
import os
from datetime import date, datetime

from .config_io import (
    list_rules,
    list_vps_meta,
    update_rule_fields,
)

log = logging.getLogger(__name__)

# Tunable constants
MAX_DAYS = float(os.environ.get("WEIGHT_MAX_DAYS", "30"))   # days that map to score=1
W_DAYS    = float(os.environ.get("WEIGHT_W_DAYS",    "0.5"))
W_TRAFFIC = float(os.environ.get("WEIGHT_W_TRAFFIC", "0.5"))
MIN_WEIGHT = 1
MAX_WEIGHT = 100


def _remaining_days(meta: dict) -> float | None:
    """Return days until expiry, or None if no expiry configured."""
    exp = meta.get("expire_date", "")
    if not exp:
        return None
    try:
        exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
        delta = (exp_date - date.today()).days
        return max(delta, 0)
    except ValueError:
        return None


def _traffic_fraction(meta: dict) -> float | None:
    """Return fraction of traffic remaining (1.0 = all remaining), or None."""
    total = float(meta.get("traffic_total_gb", 0) or 0)
    if total <= 0:
        return None  # unlimited
    used = float(meta.get("traffic_used_gb", 0) or 0)
    remaining = max(total - used, 0)
    return min(remaining / total, 1.0)


def _score_host(host: str, meta_map: dict) -> float:
    """Compute a [0,1] score for a single host."""
    meta = meta_map.get(host, {})
    if not meta:
        return 0.5  # neutral

    days   = _remaining_days(meta)
    tfrac  = _traffic_fraction(meta)

    days_score    = min(days / MAX_DAYS, 1.0) if days is not None else 0.5
    traffic_score = tfrac if tfrac is not None else 1.0

    # If expiry is <=0 days away, kill the weight entirely
    if days is not None and days <= 0:
        return 0.0

    # If traffic is exhausted, kill the weight
    if tfrac is not None and tfrac <= 0:
        return 0.0

    return W_DAYS * days_score + W_TRAFFIC * traffic_score


def _scores_to_weights(scores: list[float]) -> list[int]:
    """Normalise float scores to integers in [MIN_WEIGHT, MAX_WEIGHT]."""
    if not scores:
        return []
    total = sum(scores)
    if total == 0:
        # all hosts dead → equal minimum weight (let realm failover handle it)
        return [MIN_WEIGHT] * len(scores)
    normalised = [s / total for s in scores]
    weights = [max(MIN_WEIGHT, round(n * MAX_WEIGHT)) for n in normalised]
    return weights


def recalculate_weights():
    """
    Main entry point called by the scheduler.
    Scans all rules, groups by LISTEN_PORT, computes weights, writes them back.
    """
    rules    = list_rules()
    meta_map = list_vps_meta()

    # Group rules by LISTEN_PORT where load balance is active
    # key: LISTEN_PORT → list of rule dicts
    port_groups: dict[str, list[dict]] = {}
    for rule in rules:
        if rule.get("BALANCE_MODE", "off") == "off":
            continue
        if rule.get("RULE_ROLE", "1") != "1":
            continue
        port = rule.get("LISTEN_PORT", "")
        key = f'{rule.get("NODE_ID", "") or "local"}:{port}'
        port_groups.setdefault(key, []).append(rule)

    for port, group in port_groups.items():
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
            continue  # nothing to balance

        scores  = [_score_host(h, meta_map) for h in hosts]
        weights = _scores_to_weights(scores)
        weights_str = ",".join(str(w) for w in weights)

        log.info("port %s: hosts=%s scores=%s weights=%s",
                 port, hosts, scores, weights)

        for rule in group:
            rule_id = rule.get("RULE_ID", "")
            if rule_id:
                update_rule_fields(rule_id, {"WEIGHTS": weights_str})

    return port_groups
