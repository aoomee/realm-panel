# realm-panel

Docker-based web management panel for [Realm](https://github.com/zhboner/realm) port-forwarding rules, with dynamic weight adjustment driven by VPS expiry time and remaining traffic quota.

## Architecture

```
┌─────────────────────────────────┐
│  realm container (host network) │  ← actual port forwarder
│  image: ghcr.io/zhboner/realm   │
└────────────┬────────────────────┘
             │ shared volume: /etc/realm
┌────────────▼────────────────────┐
│  panel container  :5000         │  ← Flask web UI + API
│  - rule CRUD                    │
│  - VPS metadata store           │
│  - weight engine (60 s cron)    │
│  - realm restart via Docker SDK │
└─────────────────────────────────┘
```

## Quick start

```bash
git clone https://github.com/zywe03/realm-xwPF
cd realm-xwPF
docker compose up -d
```

Open **http://your-server:5000** in a browser.

> The panel port is 5000 by default.  
> To change it, edit the `ports` entry in `docker-compose.yml` before starting.

## Initial rule setup

The panel manages existing Realm rules but does not create them from scratch.  
Use the original `xwPF.sh` script once to bootstrap your rules:

```bash
bash xwPF.sh
```

Rules are stored in `/etc/realm/rules/rule-*.conf` on the shared volume and will immediately appear in the panel.

## Panel pages

| Page | Purpose |
|---|---|
| 概览 | Live stats: rule count, VPS count, realm status |
| 转发规则 | Table of all rules; toggle enabled/balance/failover |
| VPS 管理 | Add/edit per-host metadata: expiry date, traffic quota |
| 权重引擎 | Preview computed scores; trigger manual recomputation |

## Dynamic weight engine

Every 60 seconds the scheduler:

1. Groups rules by `LISTEN_PORT` where `BALANCE_MODE != off` and `RULE_ROLE = 1`
2. For each host in the group, looks up VPS metadata and computes a score:

```
score = W_DAYS × days_score + W_TRAFFIC × traffic_score

days_score    = clamp(remaining_days / MAX_DAYS, 0, 1)
traffic_score = remaining_gb / total_gb   (1.0 if no quota set)
```

3. Normalises scores to integer weights `[1, 100]` and writes them back to the rule's `WEIGHTS` field
4. Regenerates `/etc/realm/config.json`
5. Sends `SIGHUP` to the realm container so it reloads without dropping connections

Hosts with an **expired date** or **exhausted traffic** receive a score of `0` and are effectively removed from rotation (weight = 1 as the minimum, but all traffic routes to the highest-weight peers).

### Tuning via environment variables

| Variable | Default | Meaning |
|---|---|---|
| `WEIGHT_INTERVAL` | `60` | Seconds between automatic recomputes |
| `WEIGHT_MAX_DAYS` | `30` | Days-until-expiry that maps to `days_score = 1.0` |
| `WEIGHT_W_DAYS` | `0.5` | Contribution of expiry to overall score |
| `WEIGHT_W_TRAFFIC` | `0.5` | Contribution of traffic to overall score |

Edit these in `docker-compose.yml` under `panel → environment`.

## VPS metadata format

Stored in `/etc/realm/panel/vps_meta.json`.  
Each entry is keyed by the host string that appears in the rule's `REMOTE_HOST` field.

```json
{
  "1.2.3.4": {
    "expire_date": "2027-03-01",
    "traffic_total_gb": 200,
    "traffic_used_gb": 47.3,
    "reset_day": 1,
    "note": "HK node A"
  }
}
```

`traffic_total_gb: 0` means unlimited (traffic score always 1.0).

## API reference

All endpoints are under `/api/`.

```
GET  /api/health
GET  /api/rules
GET  /api/rules/<id>
PUT  /api/rules/<id>          { fields: { KEY: value } }
GET  /api/vps
GET  /api/vps/<host>
PUT  /api/vps/<host>          { expire_date, traffic_total_gb, ... }
DELETE /api/vps/<host>
GET  /api/weights/preview     dry-run scores, no writes
POST /api/weights/recompute   run engine immediately
GET  /api/realm/status
POST /api/realm/restart
```

## Updating realm

```bash
docker compose pull realm
docker compose up -d realm
```

## Data persistence

All data (rules, config, metadata) lives on the `realm_data` named volume.  
Removing containers does **not** delete rules — only `docker volume rm realm_data` does.
