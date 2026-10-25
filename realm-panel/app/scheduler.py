"""
APScheduler wrapper.

Jobs:
  weight_rebalance  — runs every WEIGHT_INTERVAL seconds (default 60)
                      recomputes weights, regenerates config, soft-reloads realm
  host_monitor      — runs every MONITOR_INTERVAL seconds (default 30)
                      TCP-probes all backend hosts and persists latency/uptime state
"""
import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler

from .weight_engine import recalculate_weights
from .realm_ctrl import reload_realm
from .config_io import regenerate_config

log = logging.getLogger(__name__)
INTERVAL         = int(os.environ.get("WEIGHT_INTERVAL",  "60"))
MONITOR_INTERVAL = int(os.environ.get("MONITOR_INTERVAL", "30"))
_scheduler: BackgroundScheduler | None = None


def _weight_job():
    try:
        recalculate_weights()
        regenerate_config()
        reload_realm()
    except Exception as e:
        log.error("weight job failed: %s", e)


def _monitor_job():
    try:
        from .monitor import probe_all_hosts
        probe_all_hosts()
    except Exception as e:
        log.error("monitor job failed: %s", e)


def start_scheduler(app=None):
    global _scheduler
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(_weight_job,  "interval", seconds=INTERVAL,         id="weight_rebalance")
    _scheduler.add_job(_monitor_job, "interval", seconds=MONITOR_INTERVAL, id="host_monitor")
    _scheduler.start()
    log.info("Schedulers started: weight=%ds monitor=%ds", INTERVAL, MONITOR_INTERVAL)


def trigger_now():
    if _scheduler:
        _scheduler.get_job("weight_rebalance").modify(next_run_time=None)
    _weight_job()
